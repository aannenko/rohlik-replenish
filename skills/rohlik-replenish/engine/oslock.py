"""One command at a time: an OS-level exclusive lock on an open handle (spec §5.1).

The operating system releases it when the process dies.
"""

from __future__ import annotations

import os
from pathlib import Path


class LockBusy(Exception):
    pass


class DataLock:
    def __init__(self, directory: Path):
        self.path = directory / "lock"
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockBusy("another run is in progress") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise LockBusy("another run is in progress") from exc
        except BaseException:
            fh.close()
            raise
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> "DataLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
