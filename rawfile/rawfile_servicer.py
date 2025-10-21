from pathlib import Path
from subprocess import CalledProcessError

import consts
import grpc
from config import config
from internal_svc import SIGNATURE_METADATA
import utils.rawfile
from consts import (
    FORMAT_OPTIONS_KEY,
    PROVISIONER_NAME,
    PROVISIONER_VERSION,
    RESOURCE_EXHAUSTED_EXIT_CODE,
    VOLUME_IN_USE_EXIT_CODE,
    CSI_K8S_PVC_NAME_KEY,
)
from internal import internal_pb2, internal_pb2_grpc
from csi import csi_pb2, csi_pb2_grpc
from utils.rawfile import be_absent, be_symlink, metadata
from google.protobuf.wrappers_pb2 import BoolValue
from orchestrator.k8s import node_ip_mapping, volume_to_node
from utils.remote import get_capacity
from utils.logs import log_grpc_request
from utils.commands import run
from utils.rawfile import (
    AccessType,
    attach_loop,
    detach_loops,
)
from utils.devices import mountpoint_to_dev, device_stats
from utils.units import normalize_parameters, str_to_bool
from analytics.ga4 import send_event, Usage
from utils.snapshot_manager import manager as snapshot_manager

NODE_NAME_TOPOLOGY_KEY = "hostname"


def get_access_type(request):
    access_type = request.volume_capability.WhichOneof("access_type")
    return check_access_type(access_type)


def check_access_type(access_type):
    try:
        return AccessType[access_type]
    except KeyError:
        raise Exception(f"Unsupported access type: {access_type}")


class RawFileIdentityServicer(csi_pb2_grpc.IdentityServicer):
    @log_grpc_request
    def GetPluginInfo(self, request, context):
        return csi_pb2.GetPluginInfoResponse(
            name=PROVISIONER_NAME, vendor_version=PROVISIONER_VERSION
        )

    @log_grpc_request
    def GetPluginCapabilities(self, request, context):
        Cap = csi_pb2.PluginCapability
        return csi_pb2.GetPluginCapabilitiesResponse(
            capabilities=[
                Cap(service=Cap.Service(type=Cap.Service.CONTROLLER_SERVICE)),
                Cap(
                    service=Cap.Service(
                        type=Cap.Service.VOLUME_ACCESSIBILITY_CONSTRAINTS
                    )
                ),
                Cap(
                    volume_expansion=Cap.VolumeExpansion(
                        type=Cap.VolumeExpansion.ONLINE
                    )
                ),
            ]
        )

    # @log_grpc_request
    def Probe(self, request, context):
        return csi_pb2.ProbeResponse(ready=BoolValue(value=True))


class RawFileNodeServicer(csi_pb2_grpc.NodeServicer):
    def __init__(self, node_name):
        self.node_name = node_name

    # @log_grpc_request
    def NodeGetCapabilities(self, request, context):
        Cap = csi_pb2.NodeServiceCapability
        return csi_pb2.NodeGetCapabilitiesResponse(
            capabilities=[
                Cap(rpc=Cap.RPC(type=Cap.RPC.STAGE_UNSTAGE_VOLUME)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.GET_VOLUME_STATS)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.EXPAND_VOLUME)),
            ]
        )

    @log_grpc_request
    def NodePublishVolume(self, request, context):
        target_path = request.target_path
        staging_path = request.staging_target_path
        staging_dev_path = Path(f"{staging_path}/dev")
        be_symlink(path=target_path, to=staging_dev_path)
        return csi_pb2.NodePublishVolumeResponse()

    @log_grpc_request
    def NodeUnpublishVolume(self, request, context):
        target_path = request.target_path
        be_absent(path=target_path)
        return csi_pb2.NodeUnpublishVolumeResponse()

    @log_grpc_request
    def NodeGetInfo(self, request, context):
        return csi_pb2.NodeGetInfoResponse(
            node_id=self.node_name,
            accessible_topology=csi_pb2.Topology(
                segments={NODE_NAME_TOPOLOGY_KEY: self.node_name}
            ),
        )

    @log_grpc_request
    def NodeStageVolume(self, request, context):
        img_file = utils.rawfile.img_file(request.volume_id)
        loop_file = attach_loop(img_file)
        staging_path = request.staging_target_path
        staging_dev_path = Path(f"{staging_path}/dev")
        be_symlink(path=staging_dev_path, to=loop_file)
        return csi_pb2.NodeStageVolumeResponse()

    @log_grpc_request
    def NodeUnstageVolume(self, request, context):
        img_file = utils.rawfile.img_file(request.volume_id)
        staging_path = request.staging_target_path
        staging_dev_path = Path(f"{staging_path}/dev")
        be_absent(staging_dev_path)
        detach_loops(img_file)
        return csi_pb2.NodeUnstageVolumeResponse()

    # @log_grpc_request
    def NodeGetVolumeStats(self, request, context):
        volume_path = request.volume_path
        if Path(volume_path).is_block_device():
            dev = volume_path
        else:
            dev = mountpoint_to_dev(volume_path)
        stats = device_stats(dev=dev)
        return csi_pb2.NodeGetVolumeStatsResponse(
            usage=[
                csi_pb2.VolumeUsage(
                    total=stats["dev_size"],
                    unit=csi_pb2.VolumeUsage.Unit.BYTES,
                ),
            ]
        )

    @log_grpc_request
    def NodeExpandVolume(self, request, context):
        volume_path = request.volume_path
        size = request.capacity_range.required_bytes
        volume_path = Path(volume_path).resolve()
        run(f"losetup -c {volume_path}")
        return csi_pb2.NodeExpandVolumeResponse(capacity_bytes=size)


class RawFileControllerServicer(csi_pb2_grpc.ControllerServicer):
    @log_grpc_request
    def ControllerGetCapabilities(self, request, context):
        Cap = csi_pb2.ControllerServiceCapability
        return csi_pb2.ControllerGetCapabilitiesResponse(
            capabilities=[
                Cap(rpc=Cap.RPC(type=Cap.RPC.CREATE_DELETE_VOLUME)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.GET_CAPACITY)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.EXPAND_VOLUME)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.CREATE_DELETE_SNAPSHOT)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.LIST_SNAPSHOTS)),
                Cap(rpc=Cap.RPC(type=Cap.RPC.CLONE_VOLUME)),
            ]
        )

    @log_grpc_request
    def CreateVolume(self, request: csi_pb2.CreateVolumeRequest, context):
        # TODO: volume_capabilities

        if len(request.volume_capabilities) != 1:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Exactly one cap is supported"
            )

        volume_capability = request.volume_capabilities[0]

        AccessModeEnum = csi_pb2.VolumeCapability.AccessMode.Mode
        if volume_capability.access_mode.mode not in [
            AccessModeEnum.SINGLE_NODE_WRITER
        ]:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"Unsupported access mode: {AccessModeEnum.Name(volume_capability.access_mode.mode)}",
            )

        MIN_SIZE = 16 * 1024 * 1024  # 16MiB: can't format xfs with smaller volumes
        size = max(MIN_SIZE, request.capacity_range.required_bytes)
        params = normalize_parameters(request.parameters)
        thin_provision = str_to_bool(params.get("thinprovision", "no"))
        format_options = params.get("formatoptions", "").strip()
        copy_on_write_param = params.get("copyonwrite", None)
        copy_on_write = None
        if copy_on_write_param is not None:
            copy_on_write = str_to_bool(copy_on_write_param)
        freezefs = str_to_bool(params.get("freezefs", "no"))

        snapshot_id = None
        remove_source_snapshot = False
        if request.volume_content_source:
            if all(
                (
                    request.volume_content_source.snapshot,
                    request.volume_content_source.snapshot.snapshot_id,
                )
            ):
                snapshot_id = request.volume_content_source.snapshot.snapshot_id
            elif all(
                (
                    request.volume_content_source.volume,
                    request.volume_content_source.volume.volume_id,
                )
            ):
                remove_source_snapshot = True
                source_meta = metadata(request.volume_content_source.volume.volume_id)
                current_snapshot = snapshot_manager.list_snapshots(
                    volume_id=request.volume_content_source.volume.volume_id,
                    snapshot_name=f"{request.volume_content_source.volume.volume_id}-clone-source",
                    temporary=True,
                )
                if current_snapshot.data and current_snapshot.data[0].ready:
                    snapshot_id = current_snapshot.data[0].snapshot_id
                else:
                    snapshot_id = snapshot_manager.create_snapshot(
                        volume_id=request.volume_content_source.volume.volume_id,
                        name=f"{request.name}-clone-source",
                        temporary=True,
                        copy_on_write=source_meta.get("copy_on_write", None)
                        if source_meta.get("copy_on_write", None) is not None
                        else (consts.COW_SUPPORTED or False),
                        freeze_fs=source_meta.get("freezefs", False),
                    ).snapshot_id
        try:
            node_name = request.accessibility_requirements.preferred[0].segments[
                NODE_NAME_TOPOLOGY_KEY
            ]
        except IndexError:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "No preferred topology set. Is external-provisioner running in strict-topology mode?",
            )
        except KeyError:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Topology key not found... why?"
            )
        try:
            init_rawfile(
                volume_id=request.name,
                size=size,
                thin_provision=thin_provision,
                freezefs=freezefs,
                copy_on_write=copy_on_write,
                snapshot_id=snapshot_id,
                temporary_snapshot=remove_source_snapshot,
            )
        except CalledProcessError as exc:
            if exc.returncode == RESOURCE_EXHAUSTED_EXIT_CODE:
                context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED, "Not enough disk space"
                )
            else:
                raise exc
        finally:
            if remove_source_snapshot and snapshot_id:
                vol_id, snap_name = snapshot_id.rsplit("/", 1)
                snapshot_manager.delete_snapshot(
                    volume_id=vol_id, name=snap_name, temporary=remove_source_snapshot
                )

        def volume_provision(usage: Usage):
            pvc_name = params.get(CSI_K8S_PVC_NAME_KEY, "")
            usage.volume_provision(pvc_name, request.name, size)

        send_event(volume_provision)

        return csi_pb2.CreateVolumeResponse(
            volume=csi_pb2.Volume(
                volume_id=request.name,
                volume_context={FORMAT_OPTIONS_KEY: format_options},
                capacity_bytes=size,
                accessible_topology=[
                    csi_pb2.Topology(segments={NODE_NAME_TOPOLOGY_KEY: node_name})
                ],
                content_source=request.volume_content_source,
            )
        )

    @log_grpc_request
    def DeleteVolume(self, request, context):
        from utils.volume_manager import manager as volume_manager

        size = 0
        try:
            size = volume_manager.delete_volume(volume_id=request.volume_id)
        except CalledProcessError as exc:
            if exc.returncode == VOLUME_IN_USE_EXIT_CODE:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Volume in use")
            else:
                raise exc

        def volume_deprovision(usage: Usage):
            if size > 0:
                usage.volume_deprovision(request.volume_id, size)

        send_event(volume_deprovision)
        return csi_pb2.DeleteVolumeResponse()

    def GetCapacity(self, request, context):
        return csi_pb2.GetCapacityResponse(
            available_capacity=int(get_capacity()),
        )

    @log_grpc_request
    def ControllerExpandVolume(self, request, context):
        volume_id = request.volume_id
        node_name = volume_to_node(volume_id)
        size = request.capacity_range.required_bytes

        node_ip = node_ip_mapping.get_node_ip(node_name)
        metadata = [(SIGNATURE_METADATA, config.csi_driver.internal_signature)]
        channel = grpc.insecure_channel(f"{node_ip}:{config.csi_driver.internal_port}")
        stub = internal_pb2_grpc.InternalStub(channel)
        response = stub.ExpandRawFile(
            internal_pb2.ExpandRawFileRequest(volume_id=volume_id, new_size=size),
            metadata=metadata,
            timeout=15,
        )

        if response.status == internal_pb2.ExpandRawFileStatus.RESOURCE_EXHAUSTED:
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Not enough disk space")

        node_expansion_required = True
        if get_access_type(request) is AccessType.block:
            node_expansion_required = response.is_attached

        return csi_pb2.ControllerExpandVolumeResponse(
            capacity_bytes=size,
            # unstaged block volumes don't require node expansion
            node_expansion_required=node_expansion_required,
        )
