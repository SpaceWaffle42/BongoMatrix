"""Owner-only storage for device secrets that must not enter configuration."""

from __future__ import annotations

import os
import re
import secrets
import stat
import threading
from pathlib import Path


_API_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{16,128}")


class SecretDurabilityError(RuntimeError):
    """The visible secret state changed, but directory durability is uncertain."""

    def __init__(self, configured):
        super().__init__("Secret changed, but storage durability could not be confirmed")
        self.configured = bool(configured)


class SecretStore:
    """Persist one API key through no-follow, owner-checked directory handles."""

    def __init__(self, path):
        self.path = Path(path)
        self.root = self.path.parent.parent
        self._directory_name = self.path.parent.name
        self._filename = self.path.name
        if not self._directory_name or not self._filename:
            raise ValueError("Secret path must contain a directory and filename")
        self._lock = threading.RLock()

    @staticmethod
    def _validate(value):
        if not isinstance(value, str) or not _API_KEY_PATTERN.fullmatch(value):
            raise ValueError("API key must be 16-128 letters, numbers, dots, dashes, or underscores")
        return value

    @staticmethod
    def _check_owned_directory(fd, message):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError(message)

    def _open_root(self):
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise RuntimeError("Refusing unsafe secret root") from None
        try:
            self._check_owned_directory(fd, "Refusing unsafe secret root")
        except Exception:
            os.close(fd)
            raise
        return fd

    def _open_parent(self, create):
        root_fd = self._open_root()
        if root_fd is None:
            if create:
                raise RuntimeError("Secret root does not exist")
            return None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            try:
                directory_fd = os.open(self._directory_name, flags, dir_fd=root_fd)
            except FileNotFoundError:
                if not create:
                    return None
                os.mkdir(self._directory_name, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
                directory_fd = os.open(self._directory_name, flags, dir_fd=root_fd)
            try:
                self._check_owned_directory(directory_fd, "Refusing unsafe secret directory")
                os.fchmod(directory_fd, 0o700)
            except Exception:
                os.close(directory_fd)
                raise
            return directory_fd
        except OSError:
            raise RuntimeError("Refusing unsafe secret directory") from None
        finally:
            os.close(root_fd)

    @staticmethod
    def _check_secret_file(info):
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Refusing unsafe secret path")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("Secret file must be owner-only")

    def get(self):
        with self._lock:
            directory_fd = self._open_parent(create=False)
            if directory_fd is None:
                return None
            try:
                try:
                    fd = os.open(
                        self._filename,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    return None
                try:
                    self._check_secret_file(os.fstat(fd))
                    try:
                        value = os.read(fd, 129).decode("ascii")
                    except UnicodeError:
                        raise RuntimeError("Stored API key is invalid") from None
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)
            if len(value) > 128:
                raise RuntimeError("Stored API key is too long")
            try:
                return self._validate(value)
            except ValueError:
                raise RuntimeError("Stored API key is invalid") from None

    def configured(self):
        return self.get() is not None

    def status(self):
        return {"configured": self.configured()}

    def set(self, value):
        value = self._validate(value)
        payload = value.encode("ascii")
        with self._lock:
            directory_fd = self._open_parent(create=True)
            temporary_name = f".openweather-{secrets.token_hex(12)}"
            fd = -1
            try:
                fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.fchmod(fd, 0o600)
                written = 0
                while written < len(payload):
                    written += os.write(fd, payload[written:])
                os.fsync(fd)
                os.close(fd)
                fd = -1
                os.replace(
                    temporary_name,
                    self._filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                try:
                    os.fsync(directory_fd)
                except OSError:
                    raise SecretDurabilityError(configured=True) from None
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(directory_fd)

    def clear(self):
        with self._lock:
            directory_fd = self._open_parent(create=False)
            if directory_fd is None:
                return
            try:
                try:
                    info = os.stat(self._filename, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                self._check_secret_file(info)
                os.unlink(self._filename, dir_fd=directory_fd)
                try:
                    os.fsync(directory_fd)
                except OSError:
                    raise SecretDurabilityError(configured=False) from None
            finally:
                os.close(directory_fd)
