from pathlib import Path

from config import config
from consts import FORMAT_OPTIONS_KEY
import grpc
import time
from csi import csi_pb2, csi_pb2_grpc
from csi.csi_pb2 import (
    CreateVolumeRequest,
    ListSnapshotsResponse,
    NodeExpandVolumeRequest,
    NodePublishVolumeRequest,
    NodeStageVolumeRequest,
    NodeUnpublishVolumeRequest,
    NodeUnstageVolumeRequest,
    Snapshot,
)
from declarative import (
    mount,
    unmount,
)
from utils import task_manager
from utils.rawfile import (
    attach_loop,
    be_absent,
    detach_loops,
    gc_if_needed,
    img_file,
    metadata,
)
from google.protobuf.timestamp_pb2 import Timestamp
from utils.logs import log_grpc_request
from filesystem import get_from_device_or_fallback, from_device
from filesystem.utils import get_device_for_mountpoint
from rawfile_servicer import check_access_type, get_access_type
from utils.rawfile import (
    AccessType,
)
from utils.devices import path_stats
from filesystem.base import UnknownFileSystemError
from utils.lock import VolLock
from utils.task_manager import TaskManager
from utils.snapshot_manager import manager as snapshot_manager
from utils.volume_manager import manager as volume_manager
import consts


class Bd2FsIdentityServicer(csi_pb2_grpc.IdentityServicer):
    def __init__(self, bds: csi_pb2_grpc.IdentityServicer, task_manager: TaskManager):
        self.bds = bds
        self._task_manager = task_manager

    @log_grpc_request
    def GetPluginInfo(self, request, context):
        return self.bds.GetPluginInfo(request, context)

    @log_grpc_request
    def GetPluginCapabilities(self, request, context):
        return self.bds.GetPluginCapabilities(request, context)

    # @log_grpc_request
    def Probe(self, request, context):
        return self.bds.Probe(request, context)


class Bd2FsNodeServicer(csi_pb2_grpc.NodeServicer):
    def __init__(self, bds: csi_pb2_grpc.NodeServicer, task_manager: TaskManager):
        self.bds = bds
        self._task_manager = task_manager

    # @log_grpc_request
    def NodeGetCapabilities(self, request, context):
        return self.bds.NodeGetCapabilities(request, context)

    @log_grpc_request
    def NodePublishVolume(self, request, context):
        with VolLock(request.volume_id):
            staging_dev = f"{request.staging_target_path}/device"

            path = Path(request.target_path)
            access_type_actions = {
                AccessType.mount: path.mkdir,
                AccessType.block: path.touch,
            }
            access_type_actions[get_access_type(request)](exist_ok=True)
            mount(
                device=staging_dev,
                mountpoint=request.target_path,
                readonly=request.readonly,
                options=request.volume_capability.mount.mount_flags or [],
            )
            return csi_pb2.NodePublishVolumeResponse()

    @log_grpc_request
    def NodeUnpublishVolume(self, request, context):
        with VolLock(request.volume_id):
            unmount(request.target_path, clear_mountpoint=True)
            return csi_pb2.NodeUnpublishVolumeResponse()

    @log_grpc_request
    def NodeGetInfo(self, request, context):
        return self.bds.NodeGetInfo(request, context)

    @log_grpc_request
    def NodeStageVolume(self, request, context):
        with VolLock(request.volume_id):
            bd_stage_request = NodeStageVolumeRequest()
            bd_stage_request.CopyFrom(request)
            block_path = f"{request.staging_target_path}/block"
            device_path = f"{request.staging_target_path}/device"
            bd_stage_request.staging_target_path = block_path
            Path(bd_stage_request.staging_target_path).mkdir(
                exist_ok=True, parents=True
            )
            self.bds.NodeStageVolume(bd_stage_request, context)

            bd_publish_request = NodePublishVolumeRequest()
            bd_publish_request.volume_id = request.volume_id
            bd_publish_request.publish_context.update(request.publish_context)
            bd_publish_request.staging_target_path = (
                bd_stage_request.staging_target_path
            )
            bd_publish_request.target_path = device_path
            bd_publish_request.volume_capability.CopyFrom(request.volume_capability)
            bd_publish_request.readonly = False
            bd_publish_request.secrets.update(request.secrets)
            bd_publish_request.volume_context.update(request.volume_context)

            self.bds.NodePublishVolume(bd_publish_request, context)

            if get_access_type(request) is AccessType.mount:
                format_options_str = bd_publish_request.volume_context.get(
                    FORMAT_OPTIONS_KEY, ""
                )
                format_options = []
                if len(format_options_str):
                    format_options = format_options_str.split(" ")
                default_fs = request.volume_capability.mount.fs_type
                fs = get_from_device_or_fallback(
                    bd_publish_request.target_path, (default_fs or config.default_fs)
                )
                fs.mountpoint = f"{request.staging_target_path}/mount"
                fs.format_and_mount(
                    mount_options=bd_publish_request.volume_capability.mount.mount_flags
                    or [],
                    format_options=format_options,
                )

            return csi_pb2.NodeStageVolumeResponse()

    @log_grpc_request
    def NodeUnstageVolume(self, request, context):
        with VolLock(request.volume_id):
            mount_path = f"{request.staging_target_path}/mount"
            device_path = f"{request.staging_target_path}/device"
            unmount(mount_path, clear_mountpoint=True)

            bd_unpublish_request = NodeUnpublishVolumeRequest()
            bd_unpublish_request.volume_id = request.volume_id
            bd_unpublish_request.target_path = device_path
            self.bds.NodeUnpublishVolume(bd_unpublish_request, context)

            bd_unstage_request = NodeUnstageVolumeRequest()
            bd_unstage_request.CopyFrom(request)
            block_path = f"{request.staging_target_path}/block"
            bd_unstage_request.staging_target_path = block_path
            self.bds.NodeUnstageVolume(bd_unstage_request, context)
            be_absent(bd_unstage_request.staging_target_path)

            return csi_pb2.NodeUnstageVolumeResponse()

    # @log_grpc_request
    def NodeGetVolumeStats(self, request, context):
        volume_path = request.volume_path
        if Path(volume_path).is_block_device():
            return self.bds.NodeGetVolumeStats(request, context)
        stats = path_stats(volume_path)
        return csi_pb2.NodeGetVolumeStatsResponse(
            usage=[
                csi_pb2.VolumeUsage(
                    available=stats["fs_avail"],
                    total=stats["fs_size"],
                    used=stats["fs_size"] - stats["fs_avail"],
                    unit=csi_pb2.VolumeUsage.Unit.BYTES,
                ),
                csi_pb2.VolumeUsage(
                    available=stats["fs_files_avail"],
                    total=stats["fs_files"],
                    used=stats["fs_files"] - stats["fs_files_avail"],
                    unit=csi_pb2.VolumeUsage.Unit.INODES,
                ),
            ]
        )

    @log_grpc_request
    def NodeExpandVolume(self, request, context):
        with VolLock(request.volume_id):
            if get_access_type(request) is AccessType.block:
                device_path = f"{request.staging_target_path}/device"
                request.volume_path = device_path
                self.bds.NodeExpandVolume(request, context)
                size = request.capacity_range.required_bytes
                return csi_pb2.NodeExpandVolumeResponse(capacity_bytes=size)

            # FIXME: hacky way to determine if `volume_path` is staged path,
            # or the mount itself
            # Based on CSI 1.4.0 specifications:
            # > The staging_target_path field is not required,
            # for backwards compatibility,
            # but the CO SHOULD supply it.
            # Apparently, k8s 1.18 does not supply it. So:
            dev_path = get_device_for_mountpoint(request.volume_path)
            volume_path = request.volume_path
            if dev_path is None:
                dev_path = f"{request.volume_path}/device"
                volume_path = f"{request.volume_path}/mount"

            bd_request = NodeExpandVolumeRequest()
            bd_request.CopyFrom(request)
            bd_request.volume_path = dev_path
            self.bds.NodeExpandVolume(bd_request, context)

            # Based on CSI 1.4.0 specifications:
            # > If volume_capability is omitted the SP MAY determine
            # > access_type from given volume_path for the volume and perform
            # > node expansion.
            # Apparently k8s 1.18 omits this field.
            fs = from_device(dev_path)
            if not fs:
                raise UnknownFileSystemError(device=dev_path)
            fs.mountpoint = volume_path
            fs.resize()

            size = request.capacity_range.required_bytes
            return csi_pb2.NodeExpandVolumeResponse(capacity_bytes=size)


class Bd2FsControllerServicer(csi_pb2_grpc.ControllerServicer):
    def __init__(self, bds: csi_pb2_grpc.ControllerServicer, task_manager: TaskManager):
        self.bds = bds
        self._task_manager = task_manager

    @log_grpc_request
    def ControllerGetCapabilities(self, request, context):
        return self.bds.ControllerGetCapabilities(request, context)

    @log_grpc_request
    def CreateVolume(self, request: CreateVolumeRequest, context):
        if len(request.volume_capabilities) != 1:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Exactly one cap is supported"
            )

        volume_capability = request.volume_capabilities[0]

        AccessModeEnum = csi_pb2.VolumeCapability.AccessMode.Mode
        if volume_capability.access_mode.mode not in [
            AccessModeEnum.SINGLE_NODE_WRITER
        ]:
            access_mode = AccessModeEnum.Name(volume_capability.access_mode.mode)
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"Unsupported access mode: {access_mode}",
            )

        access_type = volume_capability.WhichOneof("access_type")
        check_access_type(access_type)

        request.capacity_range.required_bytes = max(
            request.capacity_range.required_bytes, 10 * 1024 * 1024
        )  # At least 10MB

        # Extract volume creation parameters
        volume_id = request.name
        size = request.capacity_range.required_bytes

        # Extract volume parameters from request (handle None case)
        params = request.parameters or {}
        thin_provision = params.get("thin_provision", "false").lower() == "true"
        freezefs = params.get("freezefs", "false").lower() == "true"
        copy_on_write_param = params.get("copy_on_write", None)
        copy_on_write = (
            copy_on_write_param.lower() == "true" if copy_on_write_param else None
        )
        snapshot_id = (
            request.volume_content_source.snapshot.snapshot_id
            if request.volume_content_source.HasField("snapshot")
            else None
        )

        # Extract node_name for topology (required for CSI compliance)
        try:
            node_name = request.accessibility_requirements.preferred[0].segments[
                "hostname"
            ]
        except (IndexError, KeyError):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Missing topology information"
            )

        # Helper function to check if volume exists and is ready
        def _get_current_volume():
            _volumes = volume_manager.list_volumes(volume_id=volume_id, ready=True)
            if not _volumes.data:
                return None
            return _volumes.data[0]

        # Check if volume already exists and is ready
        current = _get_current_volume()
        if not current:
            # Start background task
            self._task_manager.run_task(
                task_manager.TaskName.CREATE_VOLUME,
                volume_id=volume_id,
                size=size,
                thin_provision=thin_provision,
                freezefs=freezefs,
                copy_on_write=copy_on_write,
                snapshot_id=snapshot_id,
            )

        # Poll for up to 30 seconds (same as CreateSnapshot)
        start_time = time.time()
        while time.time() - start_time < 30:
            current = _get_current_volume()
            if current and current.ready:
                # Volume ready - return with ready_to_use=True
                return csi_pb2.CreateVolumeResponse(
                    volume=csi_pb2.Volume(
                        capacity_bytes=current.size_bytes,
                        volume_id=current.volume_id,
                        content_source=request.volume_content_source
                        if snapshot_id
                        else None,
                        accessible_topology=[
                            csi_pb2.Topology(segments={"hostname": node_name})
                        ],
                    )
                )
            time.sleep(0.5)

        # Timeout - volume still creating in background, return ready_to_use=False
        return csi_pb2.CreateVolumeResponse(
            volume=csi_pb2.Volume(
                capacity_bytes=size,
                volume_id=volume_id,
                content_source=request.volume_content_source if snapshot_id else None,
                accessible_topology=[
                    csi_pb2.Topology(segments={"hostname": node_name})
                ],
            )
        )

    @log_grpc_request
    def DeleteVolume(self, request, context):
        try:
            lock = VolLock(request.volume_id)
        except FileNotFoundError:
            return csi_pb2.DeleteVolumeResponse()
        else:
            with lock:
                return self.bds.DeleteVolume(request, context)

    def GetCapacity(self, request, context):
        return self.bds.GetCapacity(request, context)

    @log_grpc_request
    def ControllerExpandVolume(self, request, context):
        response = self.bds.ControllerExpandVolume(request, context)
        return response

    @log_grpc_request
    def CreateSnapshot(self, request: csi_pb2.CreateSnapshotRequest, context):
        with VolLock(request.source_volume_id):
            volume_meta = metadata(request.source_volume_id)
            freezefs = volume_meta.get("freezefs", False)
            copy_on_write_param = volume_meta.get("copy_on_write", None)
            copy_on_write = (
                copy_on_write_param
                if copy_on_write_param is not None
                else consts.COW_SUPPORTED
            )

        def _get_current_snapshot():
            _current = snapshot_manager.list_snapshots(
                volume_id=request.source_volume_id, snapshot_name=request.name
            )
            if not _current.data:
                return None
            return _current.data[0]

        nano = 10**9
        current = _get_current_snapshot()
        if not current:
            self._task_manager.run_task(
                task_manager.TaskName.CREATE_SNAPSHOT,
                volume_id=request.source_volume_id,
                name=request.name,
                copy_on_write=copy_on_write,
                freeze_fs=freezefs,
            )
        start_time = time.time()
        while time.time() - start_time < 30:
            current = _get_current_snapshot()
            if current and current.ready:
                return csi_pb2.CreateSnapshotResponse(
                    snapshot=csi_pb2.Snapshot(
                        size_bytes=current.size_bytes,
                        snapshot_id=current.snapshot_id,
                        source_volume_id=current.volume_id,
                        ready_to_use=current.ready,
                        creation_time=Timestamp(
                            seconds=int(current.creation_time),
                            nanos=int((current.creation_time % 1) * nano),
                        ),
                    )
                )
            time.sleep(0.5)
        creation_time_ns = time.time_ns()
        snapshot_id = f"{request.source_volume_id}/{request.name}"
        return csi_pb2.CreateSnapshotResponse(
            snapshot=csi_pb2.Snapshot(
                size_bytes=0,
                snapshot_id=snapshot_id,
                source_volume_id=request.source_volume_id,
                ready_to_use=False,
                creation_time=Timestamp(
                    seconds=creation_time_ns // nano,
                    nanos=creation_time_ns % nano,
                ),
            )
        )

    @log_grpc_request
    def DeleteSnapshot(self, request: csi_pb2.DeleteSnapshotRequest, context):
        snapshot_id = request.snapshot_id
        volume_id, name = snapshot_id.rsplit("/", 1)
        if (
            len(
                snapshot_manager.list_snapshots(
                    volume_id=volume_id, snapshot_name=name
                ).data
            )
            < 0
        ):
            with VolLock(volume_id):
                file = img_file(volume_id)

                loop_dev = attach_loop(file)
                fs = from_device(loop_dev)
                if not fs:
                    raise UnknownFileSystemError(device=loop_dev, volume_id=volume_id)
                fs.delete_snapshot(name=name)

                if fs.mountpoint is None:
                    detach_loops(file)

                return csi_pb2.DeleteSnapshotResponse()
        snapshot_manager.delete_snapshot(volume_id, name)
        gc_if_needed(volume_id)
        return csi_pb2.DeleteSnapshotResponse()

    @log_grpc_request
    def ListSnapshots(self, request: csi_pb2.ListSnapshotsRequest, context):
        volume_id, name = None, None
        if request.snapshot_id:
            snapshot_id = request.snapshot_id
            volume_id, name = snapshot_id.rsplit("/", 1)
        offset = None
        try:
            offset = int(request.starting_token) if request.starting_token else None
        except ValueError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid starting_token")
        limit = request.max_entries or None
        snapshot_list = snapshot_manager.list_snapshots(
            volume_id=volume_id, snapshot_name=name, offset=offset, limit=limit
        )
        nano = 10**9
        return csi_pb2.ListSnapshotsResponse(
            entries=[
                ListSnapshotsResponse.Entry(
                    snapshot=Snapshot(
                        size_bytes=snapshot.size_bytes,
                        snapshot_id=snapshot.snapshot_id,
                        source_volume_id=snapshot.volume_id,
                        creation_time=Timestamp(
                            seconds=int(snapshot.creation_time),
                            nanos=int((snapshot.creation_time % 1) * nano),
                        ),
                        ready_to_use=snapshot.ready,
                    )
                )
                for snapshot in snapshot_list.data
            ],
            next_token=str(snapshot_list.next_token),
        )
