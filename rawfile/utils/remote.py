import utils.rawfile
<<<<<<< HEAD
=======
from utils.logs import logger
from consts import RESOURCE_EXHAUSTED_EXIT_CODE, VOLUME_IN_USE_EXIT_CODE
from volume_schema import LATEST_SCHEMA_VERSION
from utils.snapshot_manager import manager as snapshot_manager


def scrub(volume_id) -> int:
    img_dir = utils.rawfile.img_dir(volume_id)
    if not img_dir.exists():
        return 0

    img_file = utils.rawfile.img_file(volume_id)
    img_size = utils.rawfile.img_size(volume_id)
    loops = utils.rawfile.attached_loops(img_file.resolve().as_posix())
    if len(loops) > 0:
        raise CalledProcessError(returncode=VOLUME_IN_USE_EXIT_CODE, cmd="")

    now = time.time()
    deleted_at = now
    gc_at = now
    utils.rawfile.patch_metadata(volume_id, {"deleted_at": deleted_at, "gc_at": gc_at})
    utils.rawfile.gc_if_needed(volume_id, dry_run=False)
    return img_size


def init_rawfile(
    volume_id: str,
    size: int,
    thin_provision: bool = False,
    freezefs: bool = False,
    copy_on_write: bool | None = None,
    snapshot_id: str | None = None,
    temporary_snapshot: bool = False,
):
    if utils.rawfile.get_capacity() < size:
        raise CalledProcessError(returncode=RESOURCE_EXHAUSTED_EXIT_CODE, cmd="")

    img_dir = utils.rawfile.img_dir(volume_id)
    img_dir.mkdir(mode=D_PERMS, exist_ok=True)

    with VolLock(volume_id):
        img_file = Path(f"{img_dir}/disk.img")
        if img_file.exists() and os.path.getsize(img_file) >= size:
            return
        snapshots_dir = Path(img_dir.joinpath("snapshots"))
        os.makedirs(name=snapshots_dir, exist_ok=True)
        os.makedirs(name=snapshots_dir.joinpath("temp"), exist_ok=True)
        utils.rawfile.patch_metadata(
            volume_id,
            {
                "schema_version": LATEST_SCHEMA_VERSION,
                "volume_id": volume_id,
                "created_at": time.time(),
                "img_file": img_file.as_posix(),
                "snapshots_dir": snapshots_dir.as_posix(),
                "size": size,
                "thin_provision": thin_provision,
                "freezefs": freezefs,
                "copy_on_write": copy_on_write,
            },
        )
        img_file.touch()
        if snapshot_id:
            volume_id, name = snapshot_id.rsplit("/", 1)
            metadata = utils.rawfile.metadata(volume_id)
            size = max(size, metadata["size"])
            thin_provision = metadata.get("thin_provision", False)
            logger.info(
                "Cloning volume data", source_volume=volume_id, source_snapshot=name
            )
            snapshot_manager.restore_snapshot(
                volume_id, name, img_file, temporary_snapshot
            )
        if thin_provision:
            utils.rawfile.truncate(img_file, size)
        else:
            utils.rawfile.fallocate(img_file, size)
        logger.info("Initialized volume", volume_id=volume_id, size=size)
>>>>>>> develop


def get_capacity():
    cap = utils.rawfile.get_capacity()
    return max(0, cap)


def is_attached(volume_id):
    img_dir = utils.rawfile.img_dir(volume_id)
    if not img_dir.exists():
        return False

    img_file = utils.rawfile.img_file(volume_id)
    loops = utils.rawfile.attached_loops(img_file.as_posix())
    return len(loops) > 0
