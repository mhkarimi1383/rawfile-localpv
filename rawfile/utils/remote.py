import utils.rawfile


def get_capacity():
    import utils.rawfile

    cap = utils.rawfile.get_capacity()
    return max(0, cap)


def is_attached(volume_id):
    import utils.rawfile

    img_dir = utils.rawfile.img_dir(volume_id)
    if not img_dir.exists():
        return False

    img_file = utils.rawfile.img_file(volume_id)
    loops = utils.rawfile.attached_loops(img_file.as_posix())
    return len(loops) > 0
