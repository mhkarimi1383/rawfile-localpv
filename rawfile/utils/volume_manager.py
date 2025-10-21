from pathlib import Path
import time
from dataclasses import dataclass
from subprocess import CalledProcessError
from glob import glob

import consts
from consts import D_PERMS, RESOURCE_EXHAUSTED_EXIT_CODE, VOLUME_IN_USE_EXIT_CODE
from utils.lock import VolLock
from utils.snapshot_manager import manager as snapshot_manager
from utils.rawfile import (
    attached_loops,
    img_dir,
    img_file,
    img_size,
    metadata,
    patch_metadata,
    truncate,
    fallocate,
    get_capacity,
    gc_if_needed,
)
from utils.logs import logger
from volume_schema import LATEST_SCHEMA_VERSION


@dataclass
class Volume:
    volume_id: str
    size_bytes: int
    creation_time: float
    ready: bool
    thin_provision: bool


@dataclass
class VolumeList:
    data: list[Volume]
    next_token: int | None


class VolumeManager:
    def _get_volume_path(self, volume_id: str) -> Path:
        """Get the path to the volume's img_dir"""
        return img_dir(volume_id)

    def create_volume(
        self,
        volume_id: str,
        size: int,
        thin_provision: bool = False,
        freezefs: bool = False,
        copy_on_write: bool | None = None,
        snapshot_id: str | None = None,
        temporary_snapshot: bool = False,
    ) -> Volume:
        """Create a new volume with the specified parameters.

        This method moves logic from init_rawfile() in utils/remote.py.
        It performs capacity check, creates img_dir and metadata,
        handles snapshot restoration if snapshot_id provided,
        and creates disk.img via truncate (thin) or fallocate (thick provision).

        Args:
            volume_id: Unique identifier for the volume
            size: Size of the volume in bytes
            thin_provision: Whether to use thin provisioning (truncate vs fallocate)
            freezefs: Whether filesystem freeze was requested (stored in metadata)
            copy_on_write: Whether copy-on-write was requested (stored in metadata)
            snapshot_id: Optional snapshot ID to restore from (format: "volume_id/snapshot_name")
            temporary_snapshot: Whether to restore from temporary snapshot directory

        Returns:
            Volume: Volume dataclass with volume info and ready=True

        Raises:
            CalledProcessError: If insufficient capacity (RESOURCE_EXHAUSTED_EXIT_CODE)
        """
        # Check capacity before starting
        if get_capacity() < size:
            raise CalledProcessError(returncode=RESOURCE_EXHAUSTED_EXIT_CODE, cmd="")

        # Create img_dir
        volume_img_dir = img_dir(volume_id)
        volume_img_dir.mkdir(mode=D_PERMS, exist_ok=True)

        with VolLock(volume_id):
            volume_img_file = img_file(volume_id)
            creating_marker = Path(f"{volume_img_dir}/.creating")

            # Create marker file to indicate creation in progress
            creating_marker.touch()

            try:
                # Check if volume already exists with sufficient size (idempotency)
                if volume_img_file.exists() and volume_img_file.stat().st_size >= size:
                    creating_marker.unlink(missing_ok=True)
                    return Volume(
                        volume_id=volume_id,
                        size_bytes=volume_img_file.stat().st_size,
                        creation_time=volume_img_file.stat().st_ctime,
                        ready=True,
                        thin_provision=thin_provision,
                    )

                # Create snapshots directory and temp subdirectory
                snapshots_directory = Path(volume_img_dir.joinpath("snapshots"))
                snapshots_directory.mkdir(exist_ok=True)
                Path(snapshots_directory.joinpath("temp")).mkdir(exist_ok=True)

                # Create or update metadata
                patch_metadata(
                    volume_id,
                    {
                        "schema_version": LATEST_SCHEMA_VERSION,
                        "volume_id": volume_id,
                        "created_at": time.time(),
                        "img_file": volume_img_file.as_posix(),
                        "snapshots_dir": snapshots_directory.as_posix(),
                        "size": size,
                        "thin_provision": thin_provision,
                        "freezefs": freezefs,
                        "copy_on_write": copy_on_write,
                    },
                )

                # Create the disk image file
                volume_img_file.touch()

                # Handle snapshot restoration if snapshot_id provided
                if snapshot_id:
                    source_volume_id, snapshot_name = snapshot_id.rsplit("/", 1)
                    source_metadata = metadata(source_volume_id)
                    # Use the larger of requested size or source size
                    size = max(size, source_metadata["size"])
                    thin_provision = source_metadata.get("thin_provision", False)
                    logger.info(
                        "Cloning volume data",
                        source_volume=source_volume_id,
                        source_snapshot=snapshot_name,
                    )
                    snapshot_manager.restore_snapshot(
                        source_volume_id, snapshot_name, volume_img_file, temporary_snapshot
                    )

                # Provision the disk space
                if thin_provision:
                    truncate(volume_img_file, size)
                else:
                    fallocate(volume_img_file, size)

                creation_time = time.time()
                logger.info("Initialized volume", volume_id=volume_id, size=size)

                # Remove creating marker on success
                creating_marker.unlink(missing_ok=True)

                return Volume(
                    volume_id=volume_id,
                    size_bytes=size,
                    creation_time=creation_time,
                    ready=True,
                    thin_provision=thin_provision,
                )

            except Exception as e:
                # Clean up creating marker on failure
                creating_marker.unlink(missing_ok=True)
                raise e

    def delete_volume(self, volume_id: str) -> int:
        """Delete a volume by marking it for deletion and calling garbage collection.

        This method moves logic from scrub() in utils/remote.py.
        It checks if volume is attached/in-use, marks volume for deletion,
        and calls gc_if_needed() for actual cleanup.

        Args:
            volume_id: Unique identifier for the volume to delete

        Returns:
            int: Deleted volume size in bytes (0 if volume doesn't exist)

        Raises:
            CalledProcessError: If volume is in use (VOLUME_IN_USE_EXIT_CODE)
        """
        volume_img_dir = img_dir(volume_id)

        # If volume doesn't exist, return 0 (idempotency)
        if not volume_img_dir.exists():
            return 0

        with VolLock(volume_id):
            volume_img_file = img_file(volume_id)
            volume_size = img_size(volume_id)

            # Check if volume is attached/in-use
            loops = attached_loops(volume_img_file.resolve().as_posix())
            if len(loops) > 0:
                raise CalledProcessError(returncode=VOLUME_IN_USE_EXIT_CODE, cmd="")

            # Mark volume for deletion with timestamps
            now = time.time()
            deleted_at = now
            gc_at = now
            patch_metadata(volume_id, {"deleted_at": deleted_at, "gc_at": gc_at})

            # Perform garbage collection
            gc_if_needed(volume_id, dry_run=False)

            return volume_size

    def list_volumes(
        self,
        volume_id: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        ready: bool | None = None,
    ) -> VolumeList:
        """List available volumes with optional filtering and pagination.

        Pattern follows SnapshotManager.list_snapshots.
        Uses glob pattern to find volumes in DATA_DIR.
        Checks for .creating marker to determine ready status.

        Args:
            volume_id: Optional volume ID to filter by
            offset: Skip first N volumes (pagination)
            limit: Return max N volumes (pagination)
            ready: Filter by ready status (True=ready, False=creating, None=all)

        Returns:
            VolumeList: VolumeList dataclass with volume list and next_token for pagination
        """
        # Build glob pattern
        pattern = f"{consts.DATA_DIR}/*/disk.img"
        if volume_id:
            pattern = f"{consts.DATA_DIR}/{volume_id}/disk.img"

        volumes = []
        volume_files = sorted(glob(pattern, recursive=False))
        count = 0
        idx = 0

        for idx, vol_filename in enumerate(volume_files):
            if offset and idx < offset:
                continue

            vol_file = Path(vol_filename)
            vol_dir = vol_file.parent
            vol_id = vol_dir.name

            # Check for .creating marker
            creating_marker = Path(f"{vol_dir}/.creating")
            creating = creating_marker.exists()

            # Filter by ready status if specified
            if ready is not None and ready == creating:
                continue

            # Get volume metadata if available
            try:
                vol_metadata = metadata(vol_id)
                creation_time = vol_metadata.get("created_at", vol_file.stat().st_ctime)
                thin_prov = vol_metadata.get("thin_provision", False)
            except Exception:
                # If metadata is missing or corrupted, use file stats
                creation_time = vol_file.stat().st_ctime
                thin_prov = False

            volumes.append(
                Volume(
                    volume_id=vol_id,
                    size_bytes=vol_file.stat().st_size,
                    creation_time=creation_time,
                    ready=not creating,
                    thin_provision=thin_prov,
                )
            )
            count += 1
            if limit and count >= limit:
                break

        # Calculate next_token for pagination
        next_token = None
        if offset and volumes and (idx + 1) < len(volume_files):
            next_token = offset + len(volumes)

        return VolumeList(data=volumes, next_token=next_token)


manager = VolumeManager()

__all__ = ["manager"]
