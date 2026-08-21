#!/usr/bin/env python3
"""Root-owned physical Matrix verifier installed independently of candidate code."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import importlib
import itertools

import os
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


SERVICE = "matrix-web.service"
APP_ROOT = Path("/home/admin/matrix")
BINDINGS_ROOT = Path("/usr/local/lib/matrix-ci-python")
LABELS_FILE = Path("/run/matrix-ci-physical/claim")
CLAIM_FILE = LABELS_FILE
REPORT_FILE = Path("/run/matrix-ci-physical/report")
REPORT_TEMPORARIES = "report-temporaries"
LOCK_FILE = "/run/matrix-display/matrix-display.lock"
EXPECTED_DIMENSIONS = (64, 32)
BRIGHTNESS_REQUESTS = (0, 1, 25, 50, 75, 100, 101)
MAX_REPORT_BYTES = 65536
MAX_STATE_FILE_BYTES = 1024 * 1024
MAX_STATE_AGGREGATE_BYTES = 2 * 1024 * 1024
MAX_STATE_ENTRIES = 256
MAX_STATE_DEPTH = 32
MAX_HTTP_STATUS_BYTES = 256
SERVICE_STOP_WAIT_SECONDS = 100
SERVICE_READY_WAIT_SECONDS = 60
RENAME_NOREPLACE = 1
_TEMPORARY_SEQUENCE = itertools.count()
_STATE_DIGEST_DOMAIN = b"matrix-state-tree\x00schema-v1\x00"
_STATE_RECORD_ROOT = 1
_STATE_RECORD_DIRECTORY = 2
_STATE_RECORD_FILE = 3

_CLEANUP_WARNINGS = {
    "unlink": "physical verifier cleanup warning: temporary unlink failed\n",
    "fsync": "physical verifier cleanup warning: directory sync failed\n",
}


def _cleanup_warning(kind):
    """Emit one fixed non-secret warning; diagnostics can never affect lifecycle."""
    try:
        sys.stderr.write(_CLEANUP_WARNINGS[kind])
        sys.stderr.flush()
    except BaseException:
        pass


def _rename_noreplace(old_name, new_name, directory_fd, new_directory_fd=None):
    target_fd = directory_fd if new_directory_fd is None else new_directory_fd
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace publication is unavailable")
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint
    )
    renameat2.restype = ctypes.c_int
    if renameat2(directory_fd, os.fsencode(old_name), target_fd, os.fsencode(new_name), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), new_name)


def _create_unique_temporary(runtime_fd, prefix):
    while True:
        name = f"{prefix}.{os.getpid()}.{next(_TEMPORARY_SEQUENCE)}"
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=runtime_fd,
            )
            return name, fd
        except FileExistsError:
            continue


def _unlink_owned_temporary(runtime_fd, name, identity):
    try:
        current = os.stat(name, dir_fd=runtime_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            return
        os.unlink(name, dir_fd=runtime_fd)
    except OSError:
        _cleanup_warning("unlink")
        return
    try:
        os.fsync(runtime_fd)
    except OSError:
        _cleanup_warning("fsync")


_LAST_SCAVENGE_VISITED = 0


def _scavenge_quiescent_temporaries(temporary_fd, *, limit=128):
    """Remove bounded entries only after PID 1 has stopped the sole writer."""
    global _LAST_SCAVENGE_VISITED
    visited = 0
    removed = 0
    unlink_warning_emitted = False
    with os.scandir(temporary_fd) as entries:
        for entry in entries:
            if visited >= limit:
                break
            visited += 1
            try:
                os.unlink(entry.name, dir_fd=temporary_fd)
                removed += 1
            except OSError:
                if not unlink_warning_emitted:
                    _cleanup_warning("unlink")
                    unlink_warning_emitted = True
    _LAST_SCAVENGE_VISITED = visited
    if removed:
        try:
            os.fsync(temporary_fd)
        except OSError:
            _cleanup_warning("fsync")
    return removed


def _scavenge_stale_temporaries(temporary_fd, prefix, *, limit=128):
    """Compatibility entry point restricted to the report-temporary namespace."""
    if prefix != REPORT_FILE.name + ".tmp":
        raise RuntimeError("invalid report temporary namespace")
    return _scavenge_quiescent_temporaries(temporary_fd, limit=limit)


def _trusted_directory(info, *, exact_mode=None):
    mode = stat.S_IMODE(info.st_mode)
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == 0
        and info.st_gid == 0
        and not mode & 0o022
        and (exact_mode is None or mode == exact_mode)
    )


def _open_trusted_directory(path: Path):
    if not path.is_absolute():
        raise RuntimeError(f"unsafe trusted directory: {path}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not _trusted_directory(os.fstat(fd)):
            raise RuntimeError(f"unsafe trusted directory ancestry: {path}")
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            if not _trusted_directory(os.fstat(fd)):
                raise RuntimeError(f"unsafe trusted directory ancestry: {path}")
        return fd
    except BaseException as error:
        os.close(fd)
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"unsafe trusted directory ancestry: {path}") from error


def _open_runtime_directory(path: Path):
    fd = _open_trusted_directory(path)
    if not _trusted_directory(os.fstat(fd), exact_mode=0o700):
        os.close(fd)
        raise RuntimeError(f"unsafe physical verifier runtime directory: {path}")
    return fd


def _open_temporary_namespace(runtime_fd, name):
    try:
        os.mkdir(name, 0o700, dir_fd=runtime_fd)
        os.fsync(runtime_fd)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=runtime_fd)
    if not _trusted_directory(os.fstat(fd), exact_mode=0o700):
        os.close(fd)
        raise RuntimeError("unsafe physical verifier temporary namespace")
    return fd


def _run(
    *argv: str, check: bool = True, timeout: float = 15
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, check=check, text=True, capture_output=True, timeout=timeout
    )


def _stable_state_identity(info):
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _state_uint(value, width, field):
    if type(value) is not int or value < 0 or value >= 1 << (width * 8):
        raise RuntimeError(f"state tree {field} is not encodable")
    return value.to_bytes(width, "big")


def _state_record_prefix(record_type, path, info, *, content_length=None):
    if record_type not in (_STATE_RECORD_ROOT, _STATE_RECORD_DIRECTORY, _STATE_RECORD_FILE):
        raise RuntimeError("state tree record type is not encodable")
    if not isinstance(path, bytes):
        raise RuntimeError("state tree path is not encodable")
    encoded = bytearray((record_type,))
    encoded.extend(_state_uint(len(path), 4, "path length"))
    encoded.extend(path)
    for value in _stable_state_identity(info):
        encoded.extend(_state_uint(value, 8, "metadata"))
    if record_type == _STATE_RECORD_FILE:
        encoded.extend(_state_uint(content_length, 8, "content length"))
    elif content_length is not None:
        raise RuntimeError("state tree content length is not encodable")
    return bytes(encoded)


def _state_directory(info):
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_IMODE(info.st_mode) & 0o022
    )


def _open_state_root(root: Path):
    if not root.is_absolute():
        raise RuntimeError("unsafe state tree root")
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, component in enumerate(root.parts[1:]):
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            child_info = os.fstat(child_fd)
            if not _state_directory(child_info):
                os.close(child_fd)
                raise RuntimeError("unsafe state tree directory")
            if index == len(root.parts[1:]) - 1:
                return parent_fd, component, child_fd, child_info
            os.close(parent_fd)
            parent_fd = child_fd
    except BaseException:
        os.close(parent_fd)
        raise
    os.close(parent_fd)
    raise RuntimeError("unsafe state tree root")


def _tree_digest(root: Path) -> str:
    """Digest an inactive candidate tree without pathname-based content reads."""
    digest = hashlib.sha256()
    digest.update(_STATE_DIGEST_DOMAIN)
    entries = 0
    aggregate = 0
    parent_fd = root_fd = None

    def walk(directory_fd, parts, depth):
        nonlocal entries, aggregate
        try:
            names = sorted(entry.name for entry in os.scandir(directory_fd))
            directory_before = os.fstat(directory_fd)
        except OSError as error:
            raise RuntimeError("unsafe state tree directory") from error
        for name in names:
            entries += 1
            if entries > MAX_STATE_ENTRIES:
                raise RuntimeError("state tree entry bound exceeded")
            try:
                admitted = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise RuntimeError("unsafe state tree entry") from error
            relative = b"/".join((*parts, os.fsencode(name)))
            if stat.S_ISDIR(admitted.st_mode):
                if not _state_directory(admitted):
                    raise RuntimeError("unsafe state tree directory")
                digest.update(_state_record_prefix(
                    _STATE_RECORD_DIRECTORY, relative, admitted,
                ))
                if depth >= MAX_STATE_DEPTH:
                    raise RuntimeError("state tree depth bound exceeded")
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise RuntimeError("unsafe state tree directory") from error
                try:
                    opened = os.fstat(child_fd)
                    if (
                        not _state_directory(opened)
                        or _stable_state_identity(admitted) != _stable_state_identity(opened)
                    ):
                        raise RuntimeError("state tree directory identity changed")
                    walk(child_fd, (*parts, os.fsencode(name)), depth + 1)
                    closed = os.fstat(child_fd)
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        _stable_state_identity(opened) != _stable_state_identity(closed)
                        or _stable_state_identity(opened) != _stable_state_identity(current)
                    ):
                        raise RuntimeError("state tree directory identity changed")
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(admitted.st_mode) or admitted.st_nlink != 1:
                raise RuntimeError("unsafe state tree non-regular entry")
            if admitted.st_size > MAX_STATE_FILE_BYTES:
                raise RuntimeError("state tree file bound exceeded")
            try:
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise RuntimeError("unsafe state tree file") from error
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or _stable_state_identity(admitted) != _stable_state_identity(opened)
                    or opened.st_size > MAX_STATE_FILE_BYTES
                ):
                    raise RuntimeError("state tree file identity changed")
                digest.update(_state_record_prefix(
                    _STATE_RECORD_FILE, relative, opened,
                    content_length=opened.st_size,
                ))
                file_bytes = 0
                while True:
                    chunk = os.read(file_fd, min(65536, MAX_STATE_FILE_BYTES + 1 - file_bytes))
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    aggregate += len(chunk)
                    if file_bytes > MAX_STATE_FILE_BYTES:
                        raise RuntimeError("state tree file bound exceeded")
                    if aggregate > MAX_STATE_AGGREGATE_BYTES:
                        raise RuntimeError("state tree aggregate bound exceeded")
                    digest.update(chunk)
                closed = os.fstat(file_fd)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    file_bytes != opened.st_size
                    or _stable_state_identity(opened) != _stable_state_identity(closed)
                    or _stable_state_identity(opened) != _stable_state_identity(current)
                ):
                    raise RuntimeError("state tree file identity changed")
            finally:
                os.close(file_fd)
        try:
            names_after = sorted(entry.name for entry in os.scandir(directory_fd))
            directory_after = os.fstat(directory_fd)
        except OSError as error:
            raise RuntimeError("unsafe state tree directory") from error
        if (
            names_after != names
            or _stable_state_identity(directory_before)
            != _stable_state_identity(directory_after)
        ):
            raise RuntimeError("state tree directory changed during snapshot")

    try:
        parent_fd, root_name, root_fd, root_info = _open_state_root(root)
        digest.update(_state_record_prefix(_STATE_RECORD_ROOT, b"", root_info))
        walk(root_fd, (), 0)
        closed_root = os.fstat(root_fd)
        current_root = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _stable_state_identity(root_info) != _stable_state_identity(closed_root)
            or _stable_state_identity(root_info) != _stable_state_identity(current_root)
        ):
            raise RuntimeError("state tree root identity changed")
        return digest.hexdigest()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError("unsafe state tree") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


class FixedDisplayOwnershipLock:
    """Root-owned verifier copy of the production nonblocking lock contract."""

    def __init__(self, path=LOCK_FILE):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return self
        parent_fd = _open_trusted_directory(self.path.parent)
        try:
            if not _trusted_directory(os.fstat(parent_fd), exact_mode=0o755):
                raise RuntimeError(f"unsafe display lock ancestry: {self.path}")
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                fd = os.open(self.path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    fd = os.open(
                        self.path.name,
                        os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL,
                        0o644,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    fd = os.open(self.path.name, flags, dir_fd=parent_fd)
                else:
                    os.fchmod(fd, 0o644)
                    os.fsync(fd)
                    os.fsync(parent_fd)
        except BaseException:
            os.close(parent_fd)
            raise
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o644
                or info.st_nlink != 1
            ):
                raise RuntimeError(f"unsafe display lock path: {self.path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError(
                        f"physical Matrix is already owned: {self.path}"
                    ) from error
                raise
            current_parent_fd = _open_trusted_directory(self.path.parent)
            try:
                parent_info = os.fstat(parent_fd)
                current_parent_info = os.fstat(current_parent_fd)
                if (
                    not _trusted_directory(current_parent_info, exact_mode=0o755)
                    or (parent_info.st_dev, parent_info.st_ino)
                    != (current_parent_info.st_dev, current_parent_info.st_ino)
                ):
                    raise RuntimeError(f"unsafe display lock path: {self.path}")
                current = os.stat(self.path.name, dir_fd=current_parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_uid != 0
                    or current.st_gid != 0
                    or stat.S_IMODE(current.st_mode) != 0o644
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise RuntimeError(f"unsafe display lock path: {self.path}")
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


class FixedMatrixAdapter:
    """Minimal fixed adapter around the root-owned RGB Matrix bindings."""

    def __init__(self, rgbmatrix):
        options = rgbmatrix.RGBMatrixOptions()
        options.rows = 32
        options.cols = 64
        options.gpio_slowdown = 4
        options.chain_length = 1
        options.parallel = 1
        options.brightness = 100
        options.hardware_mapping = "adafruit-hat-pwm"
        options.led_rgb_sequence = "RGB"
        options.drop_privileges = False
        options.limit_refresh_rate_hz = 120
        options.pwm_bits = 8
        try:
            self.matrix = rgbmatrix.RGBMatrix(options=options)
        except BaseException:
            _emergency_native_blackout(rgbmatrix, options)
            raise
        try:
            self.canvas = self.matrix.CreateFrameCanvas()
        except BaseException:
            _blackout_owned_driver(self.matrix)
            raise

    def fill(self, red, green, blue):
        self.canvas.Fill(red, green, blue)

    def swap(self):
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def clear(self):
        self.canvas.Clear()

    def set_brightness(self, value):
        self.matrix.brightness = max(1, min(100, int(value)))


def _blackout_owned_driver(driver):
    """Independently attempt every blackout route exposed by the pinned binding."""
    canvas = None
    try:
        driver.brightness = 1
    except BaseException:
        pass
    try:
        driver.Clear()
    except BaseException:
        pass
    try:
        canvas = driver.CreateFrameCanvas()
    except BaseException:
        pass
    if canvas is not None:
        try:
            canvas.Clear()
        except BaseException:
            pass
        try:
            driver.SwapOnVSync(canvas)
        except BaseException:
            pass
    close = getattr(driver, "close", None)
    if close is not None:
        try:
            close()
        except BaseException:
            pass


def _emergency_native_blackout(rgbmatrix, options):
    """Best-effort terminal blackout when native construction raised.

    The Cython binding does not expose the failed RGBMatrix instance (and has no
    public close method); its __dealloc__ owns that object.  A fresh driver is the
    smallest real-library seam available to retake GPIO ownership and black the
    panel.  Every supported operation is attempted and the caller preserves the
    original construction error.
    """
    rescue = None
    try:
        rescue = rgbmatrix.RGBMatrix(options=options)
    except BaseException:
        return
    _blackout_owned_driver(rescue)


def _read_bounded_fd(fd, limit):
    chunks = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    raise RuntimeError("RGB Matrix binding manifest exceeded bound")


def _require_root_file(info, description):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o644
        or info.st_nlink != 1
    ):
        raise RuntimeError(f"unsafe RGB Matrix binding path: {description}")


def _validate_binding_tree():
    expected_files = ("__init__.py", "core.so", "graphics.so")
    try:
        root_fd = _open_trusted_directory(BINDINGS_ROOT)
    except RuntimeError as error:
        raise RuntimeError(f"unsafe RGB Matrix binding ancestry: {BINDINGS_ROOT}") from error
    try:
        if set(os.listdir(root_fd)) != {"rgbmatrix", "rgbmatrix.sha256"}:
            raise RuntimeError("unexpected RGB Matrix binding entry in import root")
        manifest_fd = os.open(
            "rgbmatrix.sha256", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd
        )
        try:
            _require_root_file(os.fstat(manifest_fd), "rgbmatrix.sha256")
            manifest_payload = _read_bounded_fd(manifest_fd, 1024)
        finally:
            os.close(manifest_fd)
        try:
            manifest_text = manifest_payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise RuntimeError("invalid RGB Matrix binding manifest") from error
        manifest = {}
        for line in manifest_text.splitlines(keepends=True):
            match = re.fullmatch(r"([0-9a-f]{64})  (rgbmatrix/(?:__init__\.py|core\.so|graphics\.so))\n", line)
            if match is None or match.group(2) in manifest:
                raise RuntimeError("invalid RGB Matrix binding manifest")
            manifest[match.group(2)] = match.group(1)
        expected_paths = {f"rgbmatrix/{name}" for name in expected_files}
        if set(manifest) != expected_paths:
            raise RuntimeError("invalid RGB Matrix binding manifest")

        package_fd = os.open(
            "rgbmatrix",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        try:
            if not _trusted_directory(os.fstat(package_fd), exact_mode=0o755):
                raise RuntimeError("unsafe RGB Matrix binding package")
            entries = set(os.listdir(package_fd))
            if entries != set(expected_files):
                raise RuntimeError("unexpected RGB Matrix binding entry in package")
            for name in expected_files:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=package_fd)
                try:
                    _require_root_file(os.fstat(fd), f"rgbmatrix/{name}")
                    digest = hashlib.sha256(_read_bounded_fd(fd, 16 * 1024 * 1024)).hexdigest()
                finally:
                    os.close(fd)
                if digest != manifest[f"rgbmatrix/{name}"]:
                    raise RuntimeError(f"RGB Matrix binding digest mismatch: {name}")
        finally:
            os.close(package_fd)
    finally:
        os.close(root_fd)


def _require_loaded_module(module, expected_path):
    module_file = getattr(module, "__file__", None)
    if module_file is None or Path(module_file) != expected_path:
        raise RuntimeError(f"RGB Matrix binding loaded outside fixed root: {module_file}")


def _load_fixed_rgbmatrix():
    _validate_binding_tree()
    sys.path.insert(0, str(BINDINGS_ROOT))
    rgbmatrix = importlib.import_module("rgbmatrix")
    core = importlib.import_module("rgbmatrix.core")
    graphics = importlib.import_module("rgbmatrix.graphics")
    package = BINDINGS_ROOT / "rgbmatrix"
    _require_loaded_module(rgbmatrix, package / "__init__.py")
    _require_loaded_module(core, package / "core.so")
    _require_loaded_module(graphics, package / "graphics.so")
    return rgbmatrix


class ApplianceOps:
    def service_active(self, *, timeout: float = 15) -> bool:
        return _run(
            "/bin/systemctl", "is-active", "--quiet", SERVICE,
            check=False, timeout=timeout,
        ).returncode == 0

    def http_ready(self, *, timeout: float = 2, deadline: float | None = None) -> bool:
        operation_deadline = time.monotonic() + timeout
        if deadline is not None:
            operation_deadline = min(operation_deadline, deadline)
        connection = None
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.setblocking(False)
            result = connection.connect_ex(("127.0.0.1", 5000))
            if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                return False
            if result:
                remaining = operation_deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _, writable, exceptional = select.select(
                    [], [connection], [connection], remaining
                )
                if exceptional or not writable:
                    return False
                if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    return False

            request = memoryview(
                b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            while request:
                remaining = operation_deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _, writable, exceptional = select.select(
                    [], [connection], [connection], remaining
                )
                if exceptional or not writable:
                    return False
                sent = connection.send(request)
                if sent <= 0:
                    return False
                request = request[sent:]

            status = bytearray()
            while len(status) < MAX_HTTP_STATUS_BYTES:
                remaining = operation_deadline - time.monotonic()
                if remaining <= 0:
                    return False
                readable, _, exceptional = select.select(
                    [connection], [], [connection], remaining
                )
                if exceptional or not readable:
                    return False
                chunk = connection.recv(MAX_HTTP_STATUS_BYTES - len(status))
                if not chunk:
                    return False
                status.extend(chunk)
                if time.monotonic() >= operation_deadline:
                    return False
                marker = status.find(b"\r\n")
                if marker >= 0:
                    line = bytes(status[:marker])
                    return re.fullmatch(
                        rb"HTTP/1\.[01] (?:200|302)(?: [^\r\n]*)?", line
                    ) is not None
            return False
        except Exception:
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def snapshot(self) -> dict[str, str]:
        return {
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(),
            "data": _tree_digest(APP_ROOT / "data"),
            "media": _tree_digest(APP_ROOT / "resources" / "media"),
            "restarts": _run("/bin/systemctl", "show", SERVICE, "-p", "NRestarts", "--value").stdout.strip(),
        }

    def stop_service(self) -> None:
        deadline = time.monotonic() + SERVICE_STOP_WAIT_SECONDS

        def remaining_timeout():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("matrix-web.service stop wait timed out")
            return min(15, remaining)

        result = _run(
            "/bin/systemctl", "stop", "--no-block", SERVICE,
            check=False, timeout=remaining_timeout(),
        )
        if result.returncode != 0:
            raise RuntimeError("matrix-web.service stop request failed")
        while time.monotonic() < deadline:
            result = _run(
                "/bin/systemctl", "show", SERVICE,
                "-p", "ActiveState", "-p", "Job",
                check=False, timeout=remaining_timeout(),
            )
            if result.returncode != 0:
                raise RuntimeError("matrix-web.service stop state unavailable")
            records = {}
            for line in result.stdout.splitlines():
                if "=" not in line:
                    raise RuntimeError("matrix-web.service stop state malformed")
                name, value = line.split("=", 1)
                if name in records:
                    raise RuntimeError("matrix-web.service stop state malformed")
                records[name] = value
            if set(records) != {"ActiveState", "Job"}:
                raise RuntimeError("matrix-web.service stop state malformed")
            job = records["Job"]
            if job == "":
                if records["ActiveState"] != "inactive":
                    raise RuntimeError("matrix-web.service did not stop")
                return
            if re.fullmatch(r"[1-9][0-9]*", job) is None:
                raise RuntimeError("matrix-web.service stop state malformed")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.05, remaining))
        raise RuntimeError("matrix-web.service stop wait timed out")

    def start_service(self) -> None:
        self._ready_deadline = time.monotonic() + SERVICE_READY_WAIT_SECONDS
        remaining = self._ready_deadline - time.monotonic()
        if remaining <= 0:
            del self._ready_deadline
            raise RuntimeError("matrix-web.service start request deadline expired")
        try:
            result = _run(
                "/bin/systemctl", "start", "--no-block", SERVICE,
                check=False,
                timeout=min(15, remaining),
            )
        except BaseException:
            del self._ready_deadline
            raise
        if result.returncode != 0:
            del self._ready_deadline
            raise RuntimeError("matrix-web.service start request failed")

    def wait_ready(self) -> None:
        deadline = getattr(self, "_ready_deadline", None)
        if deadline is None:
            deadline = time.monotonic() + SERVICE_READY_WAIT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("matrix-web.service did not recover")
                if self.service_active(timeout=min(15, remaining)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("matrix-web.service did not recover")
                    if self.http_ready(timeout=min(2, remaining), deadline=deadline):
                        return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("matrix-web.service did not recover")
                time.sleep(min(0.25, remaining))
        finally:
            if hasattr(self, "_ready_deadline"):
                del self._ready_deadline


def exercise_matrix(matrix_factory: Callable):
    try:
        matrix = matrix_factory()
    except BaseException:
        cleanup_partial = getattr(matrix_factory, "cleanup_partial", None)
        if cleanup_partial is not None:
            try:
                cleanup_partial()
            except BaseException:
                pass
        raise
    report = {"dimensions": [], "brightness": []}
    primary_error = None
    try:
        matrix.fill(0, 0, 0)
        matrix.swap()
        dimensions = (int(matrix.matrix.width), int(matrix.matrix.height))
        if dimensions != EXPECTED_DIMENSIONS:
            raise RuntimeError(f"physical matrix dimensions mismatch: {dimensions}")
        report["dimensions"] = list(dimensions)
        for requested in BRIGHTNESS_REQUESTS:
            expected = max(1, min(100, requested))
            matrix.set_brightness(requested)
            observed = int(matrix.matrix.brightness)
            if observed != expected:
                raise RuntimeError(
                    f"brightness readback mismatch: requested={requested} expected={expected} observed={observed}"
                )
            report["brightness"].append({"requested": requested, "observed": observed})
    except BaseException as error:
        primary_error = error

    cleanup_errors = []
    # Keep the panel black and minimally bright until production retakes ownership.
    for cleanup in (lambda: matrix.set_brightness(1), matrix.clear, matrix.swap):
        try:
            cleanup()
        except BaseException as error:
            cleanup_errors.append(error)
    if primary_error is not None:
        raise primary_error
    if cleanup_errors:
        raise cleanup_errors[0]
    return report


def _safe_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}".replace("\r", " ").replace("\n", " ")
    return "".join(character if " " <= character <= "~" else "?" for character in text)[:512] or "verification failed"


def run_verification(sha: str, pipeline_id: str, *, ops=None, lock_factory=None, matrix_factory=None):
    ops = ops or ApplianceOps()
    failure = None
    before = None
    active = ops.service_active()
    if active:
        if not ops.http_ready():
            failure = RuntimeError("production service must be HTTP ready before verification")
        try:
            # The systemd unit owns recovery before this first mutating operation.
            ops.stop_service()
        except BaseException as error:
            failure = failure or error
        if ops.service_active():
            # Never produce evidence while the root application can rewrite it.
            raise RuntimeError("production service remained active after stop")
        if failure is None:
            try:
                before = ops.snapshot()
            except BaseException as error:
                failure = error
    else:
        failure = RuntimeError("production service must be active before verification")

    if failure is None:
        try:
            if lock_factory is None or matrix_factory is None:
                lock_factory = lock_factory or FixedDisplayOwnershipLock
                matrix_factory = matrix_factory or (
                    lambda: FixedMatrixAdapter(_load_fixed_rgbmatrix())
                )
            with lock_factory():
                exercise_matrix(matrix_factory)
        except BaseException as error:
            failure = error

    if before is not None:
        try:
            after = ops.snapshot()
            if after != before:
                raise RuntimeError(f"appliance state changed: before={before!r} after={after!r}")
        except BaseException as error:
            if failure is None:
                failure = error

    report = {
        "SOURCE_SHA": sha,
        "PIPELINE_ID": pipeline_id,
        "PHYSICAL_DEVICE_ACCEPTANCE": "FAIL" if failure is not None else "PASS",
    }
    if failure is not None:
        report["ERROR"] = _safe_error(failure)
    return report


def _remove_claim():
    runtime_fd = _open_runtime_directory(CLAIM_FILE.parent)
    try:
        try:
            info = os.stat(CLAIM_FILE.name, dir_fd=runtime_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise RuntimeError("unsafe physical verifier durable claim")
        os.unlink(CLAIM_FILE.name, dir_fd=runtime_fd)
        os.fsync(runtime_fd)
    finally:
        os.close(runtime_fd)


def restore_only(ops=None, *, scavenge_stale_temporaries=False):
    ops = ops or ApplianceOps()
    if not ops.service_active():
        ops.start_service()
    ops.wait_ready()
    _remove_claim()
    if scavenge_stale_temporaries:
        runtime_fd = _open_runtime_directory(REPORT_FILE.parent)
        temporary_fd = None
        try:
            temporary_fd = _open_temporary_namespace(runtime_fd, REPORT_TEMPORARIES)
            _scavenge_stale_temporaries(
                temporary_fd, REPORT_FILE.name + ".tmp"
            )
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            os.close(runtime_fd)


def _labels():
    runtime_fd = _open_runtime_directory(LABELS_FILE.parent)
    try:
        fd = os.open(
            LABELS_FILE.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=runtime_fd,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise RuntimeError("unsafe physical verifier audit labels")
            payload = os.read(fd, 256)
        finally:
            os.close(fd)
    finally:
        os.close(runtime_fd)
    try:
        parts = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as error:
        raise RuntimeError("invalid physical verifier audit labels") from error
    if len(parts) != 2 or len(parts[0]) != 40 or any(c not in "0123456789abcdef" for c in parts[0]) or not parts[1].isdecimal():
        raise RuntimeError("invalid physical verifier audit labels")
    return parts


def _write_report(values):
    payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    if len(payload) > MAX_REPORT_BYTES:
        raise RuntimeError("physical verifier report exceeded bound")
    runtime_fd = _open_runtime_directory(REPORT_FILE.parent)
    temporary_fd = _open_temporary_namespace(runtime_fd, REPORT_TEMPORARIES)
    temporary_name = None
    fd = None
    temporary_identity = None
    try:
        try:
            temporary_name, fd = _create_unique_temporary(
                temporary_fd, REPORT_FILE.name + ".tmp"
            )
        except OSError as error:
            raise RuntimeError("unsafe physical verifier report publication") from error
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise RuntimeError("unsafe physical verifier report metadata")
        temporary_identity = (info.st_dev, info.st_ino)
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise RuntimeError("physical verifier report write made no progress")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            _rename_noreplace(
                temporary_name, REPORT_FILE.name, temporary_fd, runtime_fd
            )
        except OSError as error:
            raise RuntimeError("unsafe physical verifier report publication") from error
        os.fsync(runtime_fd)
    except BaseException:
        if temporary_identity is not None:
            _unlink_owned_temporary(
                temporary_fd, temporary_name, temporary_identity
            )
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(temporary_fd)
        os.close(runtime_fd)


def _interrupted(_signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


STOP_REQUESTED = False


def wait_for_stop(wait=signal.pause):
    while not STOP_REQUESTED:
        wait()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-only", action="store_true")
    parser.add_argument("--scavenge-stale-temporaries", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root is required")
    if args.restore_only:
        restore_only(scavenge_stale_temporaries=args.scavenge_stale_temporaries)
        return 0
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, _interrupted)
    sha, pipeline_id = _labels()
    report = run_verification(sha, pipeline_id)
    _write_report(report)
    wait_for_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
