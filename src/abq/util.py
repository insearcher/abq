"""Small shared helpers: time, atomic writes, cooperative locking."""

from __future__ import annotations

import datetime
import json
import os
import time

LOCK_STALE_SEC = 10.0
LOCK_TIMEOUT_SEC = 3.0
LOCK_RETRY_DELAY = 0.05


def now_iso() -> str:
    """UTC timestamp in the format the providers use."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def atomic_write_json(path: str, data) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class FileLock:
    """Directory-based lock, compatible with proper-lockfile used by Claude Code.

    Claude Code guards mailbox writes with `<file>.lock` as a directory, so we
    take the same lock instead of racing it.
    """

    def __init__(self, target: str, timeout: float = LOCK_TIMEOUT_SEC) -> None:
        self.path = f"{target}.lock"
        self.timeout = timeout

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.stat(self.path).st_mtime
                    if age > LOCK_STALE_SEC:
                        os.rmdir(self.path)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {self.path}")
                time.sleep(LOCK_RETRY_DELAY)

    def __exit__(self, *exc) -> None:
        try:
            os.rmdir(self.path)
        except FileNotFoundError:
            pass
