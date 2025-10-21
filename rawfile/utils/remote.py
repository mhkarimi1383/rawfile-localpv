import utils.rawfile


def init_rawfile(
    volume_id: str,
    size: int,
    thin_provision: bool = False,
    freezefs: bool = False,
    copy_on_write: bool | None = None,
    snapshot_id: str | None = None,
    temporary_snapshot: bool = False,
):
    """Wrapper function for backwards compatibility - calls VolumeManager.create_volume"""
    from utils.volume_manager import manager as volume_manager
    
    volume_manager.create_volume(
        volume_id=volume_id,
        size=size,
        thin_provision=thin_provision,
        freezefs=freezefs,
        copy_on_write=copy_on_write,
        snapshot_id=snapshot_id,
        temporary_snapshot=temporary_snapshot,
    )


def scrub(volume_id) -> int:
    """Wrapper function for backwards compatibility - calls VolumeManager.delete_volume"""
    from utils.volume_manager import manager as volume_manager
    
    return volume_manager.delete_volume(volume_id=volume_id)


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
