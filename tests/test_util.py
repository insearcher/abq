import json
import os
import time

import pytest

from abq.util import FileLock, atomic_write_json, now_iso


def test_now_iso_shape():
    stamp = now_iso()
    assert stamp.endswith("Z")
    assert "+00:00" not in stamp
    assert stamp[10] == "T"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = str(tmp_path / "data.json")
    atomic_write_json(target, {"a": 1})

    assert json.loads(open(target).read()) == {"a": 1}
    assert os.listdir(tmp_path) == ["data.json"]


def test_lock_is_exclusive_and_released(tmp_path):
    target = str(tmp_path / "inbox.json")
    with FileLock(target):
        assert os.path.isdir(target + ".lock")
        with pytest.raises(TimeoutError):
            with FileLock(target, timeout=0.2):
                pass
    assert not os.path.exists(target + ".lock")


def test_stale_lock_is_broken(tmp_path):
    target = str(tmp_path / "inbox.json")
    os.mkdir(target + ".lock")
    os.utime(target + ".lock", (time.time() - 3600, time.time() - 3600))

    with FileLock(target, timeout=0.5):
        pass
