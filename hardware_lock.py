"""Cross-process ownership lock for the physical RGB Matrix."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path


def _require_trusted_directory(info, *, final=False):
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or mode & 0o022
        or (final and mode != 0o755)
    ):
        raise RuntimeError("Unsafe display lock ancestry")


def _open_trusted_parent(path):
    if not path.is_absolute():
        raise RuntimeError("Unsafe display lock ancestry")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _require_trusted_directory(os.fstat(fd))
        components = path.parts[1:]
        for index, component in enumerate(components):
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            _require_trusted_directory(os.fstat(fd), final=index == len(components) - 1)
        return fd
    except BaseException as error:
        os.close(fd)
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError("Unsafe display lock ancestry") from error


def _require_canonical_lock(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o644
        or info.st_nlink != 1
    ):
        raise RuntimeError("Unsafe display lock metadata")


class DisplayOwnershipLock:
    def __init__(self, path="/run/matrix-display/matrix-display.lock"):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return self
        parent_fd = _open_trusted_parent(self.path.parent)
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                fd = os.open(self.path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    fd = os.open(
                        self.path.name, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL,
                        0o644, dir_fd=parent_fd
                    )
                except FileExistsError:
                    fd = os.open(self.path.name, flags, dir_fd=parent_fd)
                else:
                    os.fchmod(fd, 0o644)
                    os.fsync(fd)
                    os.fsync(parent_fd)
        except BaseException as error:
            os.close(parent_fd)
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"Unsafe display lock path: {self.path}") from error
        try:
            _require_canonical_lock(os.fstat(fd))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError(
                        f"Physical Matrix is already owned; lock unavailable: {self.path}"
                    ) from error
                raise
            current_parent_fd = _open_trusted_parent(self.path.parent)
            try:
                parent_info = os.fstat(parent_fd)
                current_parent_info = os.fstat(current_parent_fd)
                if (parent_info.st_dev, parent_info.st_ino) != (
                    current_parent_info.st_dev,
                    current_parent_info.st_ino,
                ):
                    raise RuntimeError(f"Unsafe display lock path: {self.path}")
                current = os.stat(self.path.name, dir_fd=current_parent_fd, follow_symlinks=False)
                opened = os.fstat(fd)
                _require_canonical_lock(current)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise RuntimeError(f"Unsafe display lock path: {self.path}")
            finally:
                os.close(current_parent_fd)
            self._fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise
        finally:
            os.close(parent_fd)

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
