import os
from consts import D_PERMS
from utils.lock import VolLock
import time
from pathlib import Path
from subprocess import CalledProcessError

import utils.rawfile
from utils.logs import logger
from consts import RESOURCE_EXHAUSTED_EXIT_CODE, VOLUME_IN_USE_EXIT_CODE
from volume_schema import LATEST_SCHEMA_VERSION
from utils.snapshot_manager import manager as snapshot_manager


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
