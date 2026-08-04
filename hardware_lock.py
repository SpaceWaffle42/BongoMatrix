"""Cross-process ownership lock for the physical RGB Matrix."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path


class DisplayOwnershipLock:
    def __init__(self, path="/run/matrix-display.lock"):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return self
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.path, flags, 0o644)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"Unsafe display lock path: {self.path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError(
                        f"Physical Matrix is already owned; lock unavailable: {self.path}"
                    ) from error
                raise
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
            self._fd = fd
            return self
        except Exception:
            os.close(fd)
            raise

    def release(self):
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
