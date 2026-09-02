#!/usr/bin/env python3
"""Crash-safe, source-only Priority 1.7 B1/B2 release transaction."""
from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Callable

class _StoreOnce(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_priority_1_7_seen", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_priority_1_7_seen", seen)
        if self.dest in seen:
            parser.error(f"{option_string} may be specified only once")
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


def add_store_once_argument(parser, name, **kwargs):
    kwargs["action"] = _StoreOnce
    parser.add_argument(name, **kwargs)

GIT = "/usr/bin/git"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
DISPLAY_LOCK = Path("/run/matrix-display/matrix-display.lock")
PRODUCTION_DESTINATION = "/home/admin/matrix"
PRODUCTION_INTERPRETER = "/home/admin/matrix/venv/bin/python"
PRODUCTION_SERVICE = "matrix-web.service"
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SERVICE = re.compile(r"[A-Za-z0-9@_.:-]+\.service")
FORBIDDEN_PREFIXES = ("data/", "venv/", ".git/", "resources/media/")
NON_RUNTIME_PREFIXES = ("tests/", ".hermes/", "docs/")
NON_RUNTIME_PATHS = {
    ".gitlab-ci.yml", "README.md", "Future Developments.md",
    "deploy/priority_1_7_transaction.py",
}
STATE_KEYS = {
    "version", "boundary", "baseline_sha", "baseline_tree_sha", "source_sha", "tree_sha",
    "release_root", "release_manifest", "fixture_manifest", "baseline_manifest",
    "destination", "destination_owner", "backup_root", "service", "service_baseline",
    "interpreter", "state_path", "backup_archive", "mutable_state", "self",
}
PUBLIC_FINAL_KEYS = {
    "version", "boundary", "accepted_source_sha", "accepted_tree_sha",
    "release_manifest_sha256", "baseline_manifest_sha256", "mutable_state",
    "service", "status",
}

def _pixel_bitmap(cells):
    bitmap = bytearray(64 * 32 * 4)
    for x, y, red, green, blue in cells:
        offset = (y * 64 + x) * 4
        bitmap[offset:offset + 4] = bytes((red, green, blue, 255))
    return base64.b64encode(bitmap).decode("ascii")


def _pixel_fixture_records():
    corners = [
        [0, 0, 255, 0, 0], [63, 0, 0, 255, 0],
        [0, 31, 0, 0, 255], [63, 31, 0, 0, 0],
        [31, 15, 255, 0, 255],
    ]
    translated = [[0, 0, 255, 0, 0], [2, 0, 0, 0, 255]]
    return [
        {
            "name": "corners_hole_black_composite", "width": 64, "height": 32,
            "layers": [
                {"type": "rectangle", "properties": {
                    "x": 32, "y": 16, "width": 1, "height": 1,
                    "rotation": 0, "colour": "#ffff00", "filled": True,
                }},
                {"type": "pixels", "properties": {
                    "bitmap_rgba": _pixel_bitmap(corners), "offset_x": 0,
                    "offset_y": 0, "rotation": 0,
                }},
            ],
            "expected_cells": corners + [[32, 16, 255, 255, 0]],
        },
        {
            "name": "translated_clipped_rotation", "width": 64, "height": 32,
            "layers": [{"type": "pixels", "properties": {
                "bitmap_rgba": _pixel_bitmap(translated), "offset_x": -1,
                "offset_y": 31, "rotation": 90,
            }}],
            "expected_cells": [[0, 30, 255, 0, 0]],
        },
    ]

class InjectedCrash(RuntimeError):
    """Deterministic test-only interruption raised at a durable boundary."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_json(raw: bytes, description: str):
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{description} is not valid duplicate-safe JSON") from error
    if raw != canonical_json(value):
        raise ValueError(f"{description} is not canonical JSON")
    return value


def _safe_relative(value: object) -> str:
    if type(value) is not str or not value or value != unicodedata.normalize("NFC", value):
        raise ValueError("manifest path is empty or non-NFC")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.endswith("/") or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError("manifest path is unsafe")
    if "\\" in value or "\x00" in value:
        raise ValueError("manifest path is unsafe")
    return value


def _absolute(value: str, name: str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be an absolute normalized path")
    if must_exist and not path.exists():
        raise ValueError(f"{name} does not exist")
    return path


def _read_regular(path: Path, *, max_bytes: int = 256 * 1024 * 1024):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"unsafe regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"file identity changed: {path}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"file exceeds bound: {path}")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError(f"file changed while read: {path}")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _read_descriptor(fd: int, *, max_bytes: int = 256 * 1024 * 1024):
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("unsafe regular file descriptor")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("file descriptor exceeds bound")
    after = os.fstat(fd)
    if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("file descriptor changed while read")
    return b"".join(chunks), after


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)


def _open_parent_at(root_fd: int, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(_safe_relative(relative)).parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _read_at(parent_fd: int, name: str, *, allow_absent: bool = False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
    except FileNotFoundError:
        if allow_absent:
            return None
        raise
    try:
        return _read_descriptor(fd)
    finally:
        os.close(fd)


def _unlink_exact_candidate_at(parent_fd: int, name: str, release_entry: dict,
                               owner: dict, *, remove: bool = True):
    """Validate an absent-baseline path and optionally unlink the exact candidate."""
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError("rollback unlink target is unsafe")
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ValueError("rollback unlink target is unsafe") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("rollback unlink target is unsafe")
        try:
            data, verified = _read_descriptor(descriptor)
        except ValueError as error:
            raise ValueError("rollback unlink target is unsafe") from error
        expected = (
            release_entry["sha256"], release_entry["size"], release_entry["mode"],
            owner["uid"], owner["gid"],
        )
        observed = (
            _sha(data), len(data), f"{stat.S_IMODE(verified.st_mode):04o}",
            verified.st_uid, verified.st_gid,
        )
        if observed != expected:
            raise ValueError("rollback unlink target is unsafe")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        pinned_metadata = (
            verified.st_dev, verified.st_ino, verified.st_uid, verified.st_gid,
            verified.st_mode, verified.st_nlink, verified.st_size,
            verified.st_mtime_ns, verified.st_ctime_ns,
        )
        current_metadata = (
            current.st_dev, current.st_ino, current.st_uid, current.st_gid,
            current.st_mode, current.st_nlink, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        )
        if current_metadata != pinned_metadata:
            raise ValueError("rollback unlink target is unsafe")
        if remove:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def _atomic_write_at(parent_fd: int, name: str, data: bytes, mode: int, *, uid: int, gid: int):
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                 mode, dir_fd=parent_fd)
    published = False
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable write")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        published = True
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if not published:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _same_namespace_inode(fd: int, path: Path) -> bool:
    opened = os.fstat(fd)
    current = path.lstat()
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def _fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_noreplace(source: str, destination: str, *, parent_fd: int):
    """Atomically publish one sibling without ever replacing an existing name."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOSYS, "renameat2 RENAME_NOREPLACE is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(parent_fd, os.fsencode(source), parent_fd,
                 os.fsencode(destination), 1) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _fresh_sibling_name(path: Path) -> str:
    return f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}"


def _atomic_write(path: Path, data: bytes, mode: int, *, uid=None, gid=None):
    parent = path.parent
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"unsafe publication parent: {parent}")
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, mode)
    published = False
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable write")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fchown(fd, os.geteuid() if uid is None else uid, os.getegid() if gid is None else gid)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        published = True
        _fsync_dir(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if not published:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _durable_new_file(path: Path, data: bytes, mode: int):
    """Create one immutable stage/backup file with durable bytes and metadata."""
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable file write")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _contained_path(root: Path, relative: str) -> Path:
    """Resolve a manifest path without permitting an intermediate symlink."""
    parts = PurePosixPath(_safe_relative(relative)).parts
    current = root
    for part in parts[:-1]:
        current = current / part
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("manifest parent path is unsafe")
    target = current / parts[-1]
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("manifest target path is unsafe")
    return target


def _state_digest(state: dict) -> str:
    unsigned = dict(state)
    unsigned["self"] = dict(state["self"])
    unsigned["self"]["sha256"] = ""
    return _sha(canonical_json(unsigned))


def _populate_state_fd(path: Path, state: dict, fd: int):
    """Populate and sync the locked state inode before it becomes visible."""
    metadata = os.fstat(fd)
    state["self"] = {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid, "mode": "0600", "sha256": "",
    }
    state["self"]["sha256"] = _state_digest(state)
    raw = canonical_json(state)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short durable state write")
        view = view[count:]
    os.fsync(fd)


def _mkdir_fresh(path: Path, mode: int = 0o700):
    if path.exists() or path.is_symlink():
        raise ValueError(f"path must be fresh: {path}")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"unsafe parent: {parent}")
    path.mkdir(mode=mode)
    os.chmod(path, mode)
    _fsync_dir(parent)


def _git(repository: Path, *args: str, binary: bool = False,
         validated_owner_uid: int | None = None):
    environment = {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
    }
    if validated_owner_uid is not None:
        metadata = repository.lstat()
        if (
            type(validated_owner_uid) is not int
            or validated_owner_uid < 0
            or os.geteuid() != 0
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != validated_owner_uid
        ):
            raise ValueError("Git repository owner authority is invalid")
        environment["SUDO_UID"] = str(validated_owner_uid)
    result = subprocess.run(
        [GIT, "-C", str(repository), *args], check=True, capture_output=True,
        text=not binary, timeout=30, shell=False,
        env=environment,
    )
    return result.stdout


def _commit(repository: Path, value: str, *, validated_owner_uid: int | None = None) -> tuple[str, str]:
    if SHA1.fullmatch(value) is None:
        raise ValueError("commit identity must be lowercase full SHA")
    commit = _git(
        repository, "rev-parse", "--verify", f"{value}^{{commit}}",
        validated_owner_uid=validated_owner_uid,
    ).strip()
    if commit != value:
        raise ValueError("requested identity is not an exact commit")
    tree = _git(
        repository, "rev-parse", "--verify", f"{value}^{{tree}}",
        validated_owner_uid=validated_owner_uid,
    ).strip()
    if SHA1.fullmatch(tree) is None:
        raise ValueError("commit tree identity is invalid")
    return commit, tree


def _approved_baseline_snapshot(destination: Path, release: dict, baseline_sha: str,
                                baseline_tree_sha: str, owner: dict) -> list[dict]:
    """Prove changed predecessors from Git objects, then capture matching live bytes."""
    git_dir = destination / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise ValueError("destination must be a preserved Git checkout")
    owner_uid = owner["uid"]
    commit, tree = _commit(destination, baseline_sha, validated_owner_uid=owner_uid)
    if (commit, tree) != (baseline_sha, baseline_tree_sha):
        raise ValueError("destination Git object database lacks the approved baseline tree")
    if _git(
        destination, "rev-parse", "--verify", "HEAD^{commit}",
        validated_owner_uid=owner_uid,
    ).strip() != baseline_sha:
        raise ValueError("destination checkout is not at the approved baseline commit")
    root_fd = _open_directory(destination)
    records = []
    try:
        for entry in release["runtime_files"]:
            relative = entry["path"]
            line = _git(
                destination, "ls-tree", baseline_sha, "--", relative,
                validated_owner_uid=owner_uid,
            ).strip()
            parent_fd, name = _open_parent_at(root_fd, relative)
            try:
                live = _read_at(parent_fd, name, allow_absent=True)
            finally:
                os.close(parent_fd)
            if not line:
                if live is not None:
                    raise ValueError("live predecessor does not equal approved baseline")
                records.append({"path": relative, "present": False, "sha256": None,
                                "size": 0, "mode": None, "data": None})
                continue
            mode_value, object_type, object_sha, object_path = line.split(None, 3)
            if object_type != "blob" or object_path != relative or mode_value not in ("100644", "100755"):
                raise ValueError("approved baseline path object is unsupported")
            expected_data = _git(
                destination, "cat-file", "blob", object_sha, binary=True,
                validated_owner_uid=owner_uid,
            )
            expected_mode = "0755" if mode_value == "100755" else "0644"
            if live is None:
                raise ValueError("live predecessor does not equal approved baseline")
            data, metadata = live
            if (data, f"{stat.S_IMODE(metadata.st_mode):04o}", metadata.st_uid, metadata.st_gid) != (
                    expected_data, expected_mode, owner["uid"], owner["gid"]):
                raise ValueError("live predecessor does not equal approved baseline")
            records.append({"path": relative, "present": True, "sha256": _sha(data),
                            "size": len(data), "mode": expected_mode, "data": data})
    finally:
        os.close(root_fd)
    return records


def _manifest_file(path: Path, expected_path: str | None = None):
    data, metadata = _read_regular(path)
    if expected_path is not None and path.as_posix() != expected_path:
        raise ValueError("manifest canonical path mismatch")
    return {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}", "sha256": _sha(data),
    }


def _directory_authority(path: Path):
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"unsafe authority directory: {path}")
    return {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _validate_directory_authority(record: object, description: str) -> Path:
    keys = {"path", "dev", "ino", "uid", "gid", "mode"}
    if type(record) is not dict or set(record) != keys:
        raise ValueError(f"{description} identity schema is invalid")
    path = _absolute(record["path"], description, must_exist=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{description} identity is unsafe")
    if any(type(record[key]) is not int for key in ("dev", "ino", "uid", "gid")) or (
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
        f"{stat.S_IMODE(metadata.st_mode):04o}",
    ) != (record["dev"], record["ino"], record["uid"], record["gid"], record["mode"]):
        raise ValueError(f"{description} identity mismatch")
    return path


def _validate_file_authority(record: object) -> tuple[bytes, os.stat_result]:
    keys = {"path", "dev", "ino", "uid", "gid", "mode", "sha256"}
    if type(record) is not dict or set(record) != keys:
        raise ValueError("file authority schema is invalid")
    path = _absolute(record["path"], "authority file", must_exist=True)
    data, metadata = _read_regular(path)
    if any(type(record[key]) is not int for key in ("dev", "ino", "uid", "gid")):
        raise ValueError("file authority metadata is invalid")
    if (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
            f"{stat.S_IMODE(metadata.st_mode):04o}", _sha(data)) != (
            record["dev"], record["ino"], record["uid"], record["gid"], record["mode"], record["sha256"]):
        raise ValueError("file authority identity or digest mismatch")
    return data, metadata


def _validate_release_manifest(raw: bytes, release_root: Path, *, expected_boundary="b1"):
    value = _parse_json(raw, "release manifest")
    keys = {"version", "boundary", "baseline_sha", "baseline_tree_sha", "source_sha", "tree_sha",
            "reviewed_diff_sha256", "fixture_manifest_sha256", "runtime",
            "changed_files", "runtime_files"}
    if type(value) is not dict or set(value) != keys or value["version"] != 1 or value["boundary"] != expected_boundary:
        raise ValueError("release manifest schema is invalid")
    for key in ("baseline_sha", "baseline_tree_sha", "source_sha", "tree_sha"):
        if type(value[key]) is not str or SHA1.fullmatch(value[key]) is None:
            raise ValueError("release Git identity is invalid")
    for key in ("reviewed_diff_sha256", "fixture_manifest_sha256"):
        if type(value[key]) is not str or SHA256.fullmatch(value[key]) is None:
            raise ValueError("release digest is invalid")
    runtime = value["runtime"]
    if (
        type(runtime) is not dict
        or set(runtime) != {"destination", "interpreter", "service"}
        or any(type(runtime[key]) is not str for key in runtime)
        or not Path(runtime["destination"]).is_absolute()
        or not Path(runtime["interpreter"]).is_absolute()
        or SERVICE.fullmatch(runtime["service"]) is None
    ):
        raise ValueError("release runtime identity is invalid")
    def validate_entries(entries, description):
        if type(entries) is not list or not entries:
            raise ValueError(f"{description} files must be nonempty")
        names = []
        for entry in entries:
            if type(entry) is not dict or set(entry) != {"mode", "path", "sha256", "size"}:
                raise ValueError(f"{description} file entry schema is invalid")
            relative = _safe_relative(entry["path"])
            if relative.startswith(FORBIDDEN_PREFIXES):
                raise ValueError("mutable or private path is forbidden from source release")
            if entry["mode"] not in ("0644", "0755") or type(entry["size"]) is not int or entry["size"] < 0:
                raise ValueError(f"{description} file metadata is invalid")
            if type(entry["sha256"]) is not str or SHA256.fullmatch(entry["sha256"]) is None:
                raise ValueError(f"{description} file digest is invalid")
            names.append(relative)
        if names != sorted(set(names)):
            raise ValueError(f"{description} paths are unordered or duplicate")
        return names

    changed_files = value["changed_files"]
    runtime_files = value["runtime_files"]
    changed_names = validate_entries(changed_files, "changed")
    runtime_names = validate_entries(runtime_files, "runtime")
    expected_runtime = [
        entry for entry in changed_files
        if entry["path"] not in NON_RUNTIME_PATHS
        and not entry["path"].startswith(NON_RUNTIME_PREFIXES)
    ]
    if runtime_files != expected_runtime:
        raise ValueError("runtime dependency closure does not match reviewed changed paths")
    for entry in runtime_files:
        relative = entry["path"]
        path = _contained_path(release_root, relative)
        data, metadata = _read_regular(path)
        if (len(data), _sha(data), f"{stat.S_IMODE(metadata.st_mode):04o}") != (
                entry["size"], entry["sha256"], entry["mode"]):
            raise ValueError("release file bytes or metadata mismatch")
    actual = []
    for path in sorted(release_root.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("release tree contains unsafe object")
        if stat.S_ISREG(info.st_mode):
            actual.append(path.relative_to(release_root).as_posix())
    if actual != runtime_names:
        raise ValueError("release tree has missing or extra files")
    if not set(runtime_names).issubset(changed_names):
        raise ValueError("runtime files are not reviewed changed paths")
    return value


def _validate_fixture_manifest(raw: bytes, root: Path, *, expected_boundary="b1"):
    value = _parse_json(raw, "fixture manifest")
    if type(value) is not dict or set(value) != {
        "version", "boundary", "source_sha", "tree_sha", "fixtures",
    }:
        raise ValueError("fixture manifest schema is invalid")
    if value["version"] != 1 or value["boundary"] != expected_boundary:
        raise ValueError("fixture manifest boundary is invalid")
    if SHA1.fullmatch(value["source_sha"] or "") is None or SHA1.fullmatch(value["tree_sha"] or "") is None:
        raise ValueError("fixture Git identity is invalid")
    fixtures = value["fixtures"]
    expected_names = ["corners_hole_black_composite", "translated_clipped_rotation"]
    if type(fixtures) is not list or [entry.get("name") for entry in fixtures if type(entry) is dict] != expected_names:
        raise ValueError("fixture list is invalid")
    for entry in fixtures:
        if type(entry) is not dict or set(entry) != {
            "name", "width", "height", "layers", "expected_cells",
        }:
            raise ValueError("fixture entry schema is invalid")
        if entry["width"] != 64 or entry["height"] != 32:
            raise ValueError("fixture dimensions are invalid")
        if type(entry["layers"]) is not list or not entry["layers"]:
            raise ValueError("fixture layers are invalid")
        if type(entry["expected_cells"]) is not list:
            raise ValueError("fixture expected cells are invalid")
        for cell in entry["expected_cells"]:
            if (
                type(cell) is not list or len(cell) != 5
                or any(type(component) is not int for component in cell)
                or not 0 <= cell[0] < 64 or not 0 <= cell[1] < 32
                or any(not 0 <= component <= 255 for component in cell[2:])
            ):
                raise ValueError("fixture expected cell is invalid")
        for layer in entry["layers"]:
            if type(layer) is not dict or set(layer) != {"type", "properties"}:
                raise ValueError("fixture layer is invalid")
            properties = layer["properties"]
            if layer["type"] == "pixels":
                if type(properties) is not dict or set(properties) != {
                    "bitmap_rgba", "offset_x", "offset_y", "rotation",
                }:
                    raise ValueError("fixture pixel layer is invalid")
                try:
                    bitmap = base64.b64decode(properties["bitmap_rgba"], validate=True)
                except (TypeError, ValueError) as error:
                    raise ValueError("fixture bitmap is invalid") from error
                if len(bitmap) != 8192:
                    raise ValueError("fixture bitmap is invalid")
    return value


def build_release(arguments) -> dict[str, object]:
    boundary = getattr(arguments, "boundary", None)
    if boundary not in ("b1", "b2"):
        raise ValueError("release boundary is invalid")
    repository = _absolute(arguments.repository, "repository", must_exist=True)
    if not repository.is_dir() or repository.is_symlink():
        raise ValueError("repository is unsafe")
    baseline, baseline_tree = _commit(repository, arguments.baseline_commit)
    source, tree = _commit(repository, arguments.source_commit)
    if baseline == source:
        raise ValueError("baseline and source commits must differ")
    if SHA256.fullmatch(arguments.expected_reviewed_diff_sha256 or "") is None:
        raise ValueError("reviewed diff digest is invalid")
    python = _absolute(arguments.python, "python")
    if str(python) != PRODUCTION_INTERPRETER:
        raise ValueError("production interpreter identity mismatch")
    final_stage = _absolute(arguments.stage_root, "stage root")
    stage = final_stage.with_name(final_stage.name + ".preparing")
    if final_stage.exists() or final_stage.is_symlink():
        raise ValueError("stage root must be fresh")
    if stage.exists() or stage.is_symlink():
        raise ValueError("stage preparation authority already exists")
    callback = getattr(arguments, "after_boundary", None)
    created = False
    published = False
    try:
        _mkdir_fresh(stage)
        created = True
        release = stage / "release"
        release.mkdir(mode=0o700)
        fixtures_root = stage / "fixtures"
        fixtures_root.mkdir(mode=0o700)
        diff = _git(repository, "diff", "--binary", "--full-index", "--no-ext-diff",
                    f"{baseline}...{source}", binary=True)
        if _sha(diff) != arguments.expected_reviewed_diff_sha256:
            raise ValueError("reviewed diff SHA-256 mismatch")
        raw_names = _git(repository, "diff", "--name-status", "-z", "--no-ext-diff",
                         f"{baseline}...{source}", binary=True)
        pieces = raw_names.split(b"\0")
        if pieces and pieces[-1] == b"":
            pieces.pop()
        changed = []
        index = 0
        while index < len(pieces):
            status_code = pieces[index].decode("ascii")
            index += 1
            if status_code not in ("A", "M") or index >= len(pieces):
                raise ValueError("source diff contains a deletion, rename, or unsupported change")
            relative = _safe_relative(pieces[index].decode("utf-8"))
            index += 1
            if relative.startswith(FORBIDDEN_PREFIXES):
                raise ValueError("source diff contains mutable/private paths")
            changed.append(relative)
        if changed != sorted(set(changed)) or not changed:
            raise ValueError("changed-file allowlist is empty, duplicate, or unordered")
        changed_records = []
        runtime_records = []
        for relative in changed:
            data = _git(repository, "show", f"{source}:{relative}", binary=True)
            tree_line = _git(repository, "ls-tree", source, "--", relative).strip()
            if not tree_line:
                raise ValueError("changed path is absent from source tree")
            mode_value, object_type, _rest = tree_line.split(None, 2)
            if object_type != "blob" or mode_value not in ("100644", "100755"):
                raise ValueError("changed Git object mode/type is unsupported")
            mode = "0755" if mode_value == "100755" else "0644"
            record = {"mode": mode, "path": relative, "sha256": _sha(data), "size": len(data)}
            changed_records.append(record)
            if relative in NON_RUNTIME_PATHS or relative.startswith(NON_RUNTIME_PREFIXES):
                continue
            destination = release.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _durable_new_file(destination, data, int(mode, 8))
            runtime_records.append(record)
        if not runtime_records:
            raise ValueError("deployable runtime manifest is empty")
        transaction_relative = "deploy/priority_1_7_transaction.py"
        transaction_bytes = _git(repository, "show", f"{source}:{transaction_relative}", binary=True)
        current_transaction, _ = _read_regular(Path(__file__).resolve())
        if transaction_bytes != current_transaction:
            raise ValueError("executing transaction does not match source commit")
        _durable_new_file(stage / "priority_1_7_transaction.py", transaction_bytes, 0o700)
        fixture_records = _pixel_fixture_records()
        fixture_manifest = {"version": 1, "boundary": boundary, "source_sha": source,
                            "tree_sha": tree, "fixtures": fixture_records}
        fixture_bytes = canonical_json(fixture_manifest)
        _atomic_write(stage / "physical-fixtures.json", fixture_bytes, 0o600)
        release_manifest = {
            "version": 1, "boundary": boundary, "baseline_sha": baseline,
            "baseline_tree_sha": baseline_tree, "source_sha": source,
            "tree_sha": tree, "reviewed_diff_sha256": _sha(diff),
            "fixture_manifest_sha256": _sha(fixture_bytes),
            "runtime": {
                "destination": PRODUCTION_DESTINATION,
                "interpreter": PRODUCTION_INTERPRETER,
                "service": PRODUCTION_SERVICE,
            },
            "changed_files": changed_records, "runtime_files": runtime_records,
        }
        release_bytes = canonical_json(release_manifest)
        _atomic_write(stage / "release-manifest.json", release_bytes, 0o600)
        for directory in sorted((path for path in stage.rglob("*") if path.is_dir()), reverse=True):
            _fsync_dir(directory)
        for path in (stage / "release-manifest.json", stage / "physical-fixtures.json"):
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(stage)
        if callback is not None:
            callback("stage:before-publication")
        parent_fd = _open_directory(final_stage.parent)
        try:
            _rename_noreplace(stage.name, final_stage.name, parent_fd=parent_fd)
            published = True
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        if callback is not None:
            callback("stage:published")
        return {
            "boundary": boundary, "changed_file_count": len(changed_records),
            "runtime_file_count": len(runtime_records), "diff_sha256": _sha(diff),
            "fixture_count": len(fixture_records),
            "fixture_manifest": str(final_stage / "physical-fixtures.json"),
            "fixture_manifest_sha256": _sha(fixture_bytes),
            "release_manifest": str(final_stage / "release-manifest.json"),
            "release_manifest_sha256": _sha(release_bytes), "source_sha": source,
            "stage_root": str(final_stage), "tree_sha": tree,
        }
    except BaseException:
        if created:
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _remaining_service_timeout(deadline, cap):
    if deadline is None:
        return cap
    if type(deadline) not in (int, float) or deadline != deadline:
        raise ValueError("service observation deadline is invalid")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("service observation deadline expired")
    return min(cap, remaining)


class SystemdService:
    def __init__(self, service: str):
        if SERVICE.fullmatch(service) is None:
            raise ValueError("service name is invalid")
        self.name = service

    def _run(self, *args, check=True, deadline=None):
        return subprocess.run(
            [SYSTEMCTL, *args, self.name], check=check, capture_output=True, text=True,
            timeout=_remaining_service_timeout(deadline, 15), shell=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def is_active(self):
        result = self._run("is-active", "--quiet", check=False)
        if result.returncode not in (0, 3):
            raise RuntimeError("service active-state query failed")
        return result.returncode == 0

    def stop(self):
        self._run("stop")
        if self.is_active():
            raise RuntimeError("service did not become inactive")

    def start_ready(self):
        self._run("start")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.is_active():
                connection = http.client.HTTPConnection("127.0.0.1", 5000, timeout=2)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    response.read()
                    if response.status == 302:
                        return
                except OSError:
                    pass
                finally:
                    connection.close()
            time.sleep(0.1)
        raise RuntimeError("bounded service readiness failed")

    def status(self, *, warning_since=None, deadline=None):
        properties_result = self._run(
            "show",
            "--property=Id,ActiveState,SubState,MainPID,NRestarts,ControlGroup",
            deadline=deadline,
        )
        properties = {}
        for line in properties_result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in properties:
                raise RuntimeError("service status properties are malformed")
            properties[key] = value
        if set(properties) != {
            "Id", "ActiveState", "SubState", "MainPID", "NRestarts", "ControlGroup",
        }:
            raise RuntimeError("service status properties are incomplete")
        try:
            main_pid = int(properties["MainPID"])
            n_restarts = int(properties["NRestarts"])
        except ValueError as error:
            raise RuntimeError("service status integer is malformed") from error
        if (
            properties["Id"] != self.name
            or properties["ActiveState"] != "active"
            or properties["SubState"] != "running"
            or main_pid <= 0
            or n_restarts < 0
            or not properties["ControlGroup"].startswith("/")
            or properties["ControlGroup"].split("/")[-1] != self.name
        ):
            raise RuntimeError("service identity or active state is invalid")

        connection = http.client.HTTPConnection(
            "127.0.0.1", 5000, timeout=_remaining_service_timeout(deadline, 2),
        )
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            status_code = response.status
        finally:
            connection.close()

        lock_raw, lock_metadata = _read_regular(DISPLAY_LOCK, max_bytes=1)
        if lock_raw:
            raise RuntimeError("display lock marker is not empty")
        def display_lock_holder():
            locks_raw, _ = _read_regular(Path("/proc/locks"), max_bytes=1024 * 1024)
            expected_device = f"{os.major(lock_metadata.st_dev):02x}:{os.minor(lock_metadata.st_dev):02x}"
            owner = None
            matching_records = 0
            for line in locks_raw.decode("ascii").splitlines():
                fields = line.split()
                if len(fields) < 6:
                    continue
                device, separator, inode = fields[5].rpartition(":")
                if not separator or device.lower() != expected_device or inode != str(lock_metadata.st_ino):
                    continue
                if fields[1] != "FLOCK":
                    continue
                if (
                    len(fields) != 8
                    or re.fullmatch(r"[0-9]+:", fields[0]) is None
                    or fields[2] != "ADVISORY"
                    or fields[3] not in {"READ", "WRITE"}
                    or re.fullmatch(r"[1-9][0-9]*", fields[4]) is None
                    or fields[6:] != ["0", "EOF"]
                ):
                    raise RuntimeError("display lock record is malformed")
                matching_records += 1
                if matching_records != 1:
                    raise RuntimeError("display lock record is duplicated")
                if fields[3] == "WRITE":
                    owner = int(fields[4])
            return owner

        holder_pid = display_lock_holder()
        if holder_pid is None:
            raise RuntimeError("display lock has no owner")
        validate_holder_cgroup: Callable[[], None] | None = None
        if holder_pid != main_pid:
            expected_cgroup = f"0::{properties['ControlGroup']}\n".encode("ascii")

            def _validate_holder_cgroup_impl():
                try:
                    cgroup_raw, _ = _read_regular(
                        Path(f"/proc/{holder_pid}/cgroup"), max_bytes=64 * 1024,
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    raise RuntimeError("display lock owner cgroup evidence is invalid") from error
                if cgroup_raw != expected_cgroup:
                    raise RuntimeError("display lock is not owned by service cgroup")

            validate_holder_cgroup = _validate_holder_cgroup_impl
            validate_holder_cgroup()
            if display_lock_holder() != holder_pid:
                raise RuntimeError("display lock ownership changed during validation")
            validate_holder_cgroup()

        warning_count = 0
        if warning_since is not None:
            if type(warning_since) is not int or warning_since < 0:
                raise ValueError("warning baseline is invalid")
            journal = subprocess.run(
                [
                    JOURNALCTL, "--unit", self.name, "--since", f"@{warning_since}",
                    "--priority", "warning", "--output", "cat", "--no-pager",
                ],
                check=True, capture_output=True,
                timeout=_remaining_service_timeout(deadline, 15), shell=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if len(journal.stdout) > 1024 * 1024:
                raise RuntimeError("service warning evidence exceeds bound")
            warning_count = sum(bool(line) for line in journal.stdout.splitlines())

        if holder_pid != main_pid:
            if validate_holder_cgroup is None:
                raise RuntimeError("display lock owner cgroup validator is unavailable")
            validate_holder_cgroup()
            if display_lock_holder() != holder_pid:
                raise RuntimeError("display lock ownership changed during validation")
            validate_holder_cgroup()
        if display_lock_holder() != holder_pid:
            raise RuntimeError("display lock ownership changed during validation")
        closing_raw, closing_metadata = _read_regular(DISPLAY_LOCK, max_bytes=1)
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")
        if closing_raw or any(
            getattr(closing_metadata, field) != getattr(lock_metadata, field)
            for field in identity_fields
        ):
            raise RuntimeError("display lock path changed during validation")

        _remaining_service_timeout(deadline, 15)
        return {
            "service": self.name,
            "active": True,
            "substate": properties["SubState"],
            "http_status": status_code,
            "main_pid": main_pid,
            "n_restarts": n_restarts,
            "control_group": properties["ControlGroup"],
            "display_lock": {
                "path": str(DISPLAY_LOCK), "dev": lock_metadata.st_dev,
                "ino": lock_metadata.st_ino, "uid": lock_metadata.st_uid,
                "gid": lock_metadata.st_gid,
                "mode": f"{stat.S_IMODE(lock_metadata.st_mode):04o}", "locked": True,
            },
            "warning_count": warning_count,
        }


class Priority17Transaction:
    def __init__(self, state_path: Path, state: dict, *, service=None, state_fd: int = -1):
        self.state_path = state_path
        self.state = state
        self._state_fd = state_fd
        self.service = service or SystemdService(state["service"])
        self.release_manifest = None
        self.baseline_manifest = None

    @classmethod
    def load(cls, state_path, *, service=None):
        path = _absolute(str(state_path), "state", must_exist=True)
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("transaction state is locked by another command") from error
            metadata = os.fstat(fd)
            current = path.lstat()
            if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("state identity changed after lock")
            raw, metadata = _read_descriptor(fd)
        except BaseException:
            os.close(fd)
            raise
        if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("state must be owner-bound mode 0600")
        state = _parse_json(raw, "transaction state")
        if type(state) is not dict or set(state) != STATE_KEYS or state["version"] != 1 or state["boundary"] not in ("b1", "b2"):
            raise ValueError("transaction state schema is invalid")
        if state["state_path"] != str(path):
            raise ValueError("transaction state canonical path mismatch")
        identity = state["self"]
        expected_keys = {"path", "dev", "ino", "uid", "gid", "mode", "sha256"}
        if type(identity) is not dict or set(identity) != expected_keys:
            raise ValueError("state self identity schema is invalid")
        if any(type(identity[key]) is not int for key in ("dev", "ino", "uid", "gid")):
            raise ValueError("state self identity metadata is invalid")
        expected = (
            str(path), metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
            f"{stat.S_IMODE(metadata.st_mode):04o}", _state_digest(state),
        )
        actual = (
            identity["path"], identity["dev"], identity["ino"], identity["uid"],
            identity["gid"], identity["mode"], identity["sha256"],
        )
        if actual != expected:
            raise ValueError("state identity mismatch")
        tx = cls(path, state, service=service, state_fd=fd)
        try:
            tx._revalidate()
            return tx
        except BaseException:
            tx.close()
            raise

    def close(self):
        if self._state_fd >= 0:
            os.close(self._state_fd)
            self._state_fd = -1

    def __del__(self):
        self.close()

    def _validate_state_lock(self):
        if self._state_fd < 0:
            raise RuntimeError("transaction state lock is not held")
        metadata = os.fstat(self._state_fd)
        current = self.state_path.lstat()
        if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("state identity changed after lock")

    def _revalidate(self):
        self._validate_state_lock()
        release_raw, _ = _validate_file_authority(self.state["release_manifest"])
        fixture_raw, _ = _validate_file_authority(self.state["fixture_manifest"])
        baseline_raw, _ = _validate_file_authority(self.state["baseline_manifest"])
        release_root = _validate_directory_authority(self.state["release_root"], "release root")
        destination_root = _validate_directory_authority(self.state["destination"], "destination")
        _validate_directory_authority(self.state["backup_root"], "backup root")
        backup = _validate_directory_authority(self.state["backup_archive"], "backup archive")
        owner = self.state["destination_owner"]
        if (type(owner) is not dict or set(owner) != {"uid", "gid"}
                or any(type(owner[key]) is not int for key in owner)
                or (owner["uid"], owner["gid"]) != (
                    self.state["destination"]["uid"], self.state["destination"]["gid"],
                )):
            raise ValueError("destination owner authority is invalid")
        baseline_service = self.state["service_baseline"]
        if (type(baseline_service) is not dict
                or set(baseline_service) != {"main_pid", "n_restarts", "warning_since"}
                or any(type(baseline_service[key]) is not int for key in baseline_service)
                or min(baseline_service.values()) < 0
                or baseline_service["main_pid"] <= 0):
            raise ValueError("service baseline authority is invalid")
        interpreter = self.state["interpreter"]
        interpreter_keys = {
            "path", "resolved_path", "dev", "ino", "uid", "gid", "mode",
            "sha256", "version",
        }
        if (
            type(interpreter) is not dict or set(interpreter) != interpreter_keys
            or type(interpreter.get("path")) is not str
            or _running_interpreter_authority(Path(interpreter["path"])) != interpreter
        ):
            raise ValueError("production interpreter authority is invalid")
        mutable_state = self.state["mutable_state"]
        if (
            type(mutable_state) is not list
            or [entry.get("path") for entry in mutable_state if type(entry) is dict]
            != ["data", "resources/media"]
        ):
            raise ValueError("mutable state authority is invalid")
        for entry in mutable_state:
            if type(entry) is not dict or set(entry) != {"path", "present", "tree_sha256"}:
                raise ValueError("mutable state authority is invalid")
            if type(entry["present"]) is not bool:
                raise ValueError("mutable state authority is invalid")
            if entry["present"]:
                if SHA256.fullmatch(entry["tree_sha256"] or "") is None:
                    raise ValueError("mutable state authority is invalid")
            elif entry["tree_sha256"] is not None:
                raise ValueError("mutable state authority is invalid")
        if _snapshot_mutable_state(destination_root) != mutable_state:
            raise ValueError("mutable state changed during transaction")
        release = _validate_release_manifest(
            release_raw, release_root, expected_boundary=self.state["boundary"],
        )
        fixture = _validate_fixture_manifest(
            fixture_raw, Path(self.state["fixture_manifest"]["path"]).parent,
            expected_boundary=self.state["boundary"],
        )
        baseline = _parse_json(baseline_raw, "baseline source manifest")
        if release["runtime"] != {
            "destination": self.state["destination"]["path"],
            "interpreter": self.state["interpreter"]["path"],
            "service": self.state["service"],
        }:
            raise ValueError("release/state runtime identity mismatch")
        if release["fixture_manifest_sha256"] != _sha(fixture_raw):
            raise ValueError("fixture manifest digest mismatch")
        if (
            release["baseline_sha"], release["baseline_tree_sha"],
            release["source_sha"], release["tree_sha"],
        ) != (
            self.state["baseline_sha"], self.state["baseline_tree_sha"],
            self.state["source_sha"], self.state["tree_sha"],
        ):
            raise ValueError("release/state Git identity mismatch")
        if (fixture["source_sha"], fixture["tree_sha"]) != (self.state["source_sha"], self.state["tree_sha"]):
            raise ValueError("fixture/state Git identity mismatch")
        keys = {"version", "boundary", "source_sha", "tree_sha", "files"}
        if type(baseline) is not dict or set(baseline) != keys or baseline["version"] != 1 or baseline["boundary"] != self.state["boundary"]:
            raise ValueError("baseline manifest schema is invalid")
        if (baseline["source_sha"], baseline["tree_sha"]) != (self.state["baseline_sha"], self.state["baseline_tree_sha"]):
            raise ValueError("baseline manifest identity mismatch")
        if [entry.get("path") for entry in baseline["files"]] != [entry["path"] for entry in release["runtime_files"]]:
            raise ValueError("baseline manifest allowlist mismatch")
        for entry in baseline["files"]:
            if type(entry) is not dict or set(entry) != {"path", "present", "sha256", "size", "mode"}:
                raise ValueError("baseline source entry schema is invalid")
            relative = _safe_relative(entry["path"])
            if type(entry["present"]) is not bool or type(entry["size"]) is not int or entry["size"] < 0:
                raise ValueError("baseline source entry metadata is invalid")
            if entry["present"]:
                if entry["mode"] not in ("0644", "0755") or SHA256.fullmatch(entry["sha256"] or "") is None:
                    raise ValueError("baseline source entry is invalid")
                data, metadata = _read_regular(backup / "source" / relative)
                if (len(data), _sha(data), f"{stat.S_IMODE(metadata.st_mode):04o}") != (
                        entry["size"], entry["sha256"], entry["mode"]):
                    raise ValueError("baseline snapshot mismatch")
            elif (entry["sha256"], entry["size"], entry["mode"]) != (None, 0, None):
                raise ValueError("absent baseline source entry is invalid")
        self.release_manifest = release
        self.baseline_manifest = baseline

    @property
    def destination(self):
        return Path(self.state["destination"]["path"])

    @property
    def release_root(self):
        return Path(self.state["release_root"]["path"])

    @property
    def backup_root(self):
        return Path(self.state["backup_archive"]["path"])

    def _phase(self, destination_root_fd: int | None = None):
        if self.release_manifest is None or self.baseline_manifest is None:
            raise RuntimeError("transaction manifests were not validated")
        candidate = True
        baseline = True
        owns_root_fd = destination_root_fd is None
        root_fd = _open_directory(self.destination) if owns_root_fd else destination_root_fd
        try:
            for release_entry, baseline_entry in zip(self.release_manifest["runtime_files"], self.baseline_manifest["files"]):
                relative = release_entry["path"]
                parent_fd, name = _open_parent_at(root_fd, relative)
                try:
                    live = _read_at(parent_fd, name, allow_absent=True)
                    observed = None if live is None else (
                        _sha(live[0]), len(live[0]), f"{stat.S_IMODE(live[1].st_mode):04o}",
                        live[1].st_uid, live[1].st_gid,
                    )
                    parent_path = self.destination.joinpath(*PurePosixPath(relative).parts[:-1])
                    if not _same_namespace_inode(parent_fd, parent_path):
                        raise ValueError("destination namespace changed")
                finally:
                    os.close(parent_fd)
                owner = self.state["destination_owner"]
                candidate &= observed == (
                    release_entry["sha256"], release_entry["size"], release_entry["mode"],
                    owner["uid"], owner["gid"],
                )
                baseline_expected = None if not baseline_entry["present"] else (
                    baseline_entry["sha256"], baseline_entry["size"], baseline_entry["mode"],
                    owner["uid"], owner["gid"],
                )
                baseline &= observed == baseline_expected
            if not _same_namespace_inode(root_fd, self.destination):
                raise ValueError("destination namespace changed")
        finally:
            if owns_root_fd:
                os.close(root_fd)
        if candidate == baseline:
            raise ValueError("source tree is mixed or ambiguous")
        return "candidate" if candidate else "baseline"

    def _stop(self):
        if self.service.is_active():
            self.service.stop()
        if self.service.is_active():
            raise RuntimeError("service remains active before source mutation")

    def transition(self, target: str, *, operation: str, after_boundary: Callable[[str], None] | None = None):
        if target not in ("candidate", "baseline") or operation not in ("deploy", "rollback", "redeploy", "recover"):
            raise ValueError("transaction target or operation is invalid")
        self._revalidate()
        if operation != "recover":
            if not self.service.is_active():
                raise ValueError("inactive transaction requires explicit recovery")
            try:
                phase = self._phase()
            except ValueError as error:
                raise ValueError("ambiguous source requires explicit recovery") from error
            required_phase = "candidate" if operation == "rollback" else "baseline"
            if phase != required_phase:
                raise ValueError(f"{operation} requires exact {required_phase} source")
        if self.release_manifest is None or self.baseline_manifest is None:
            raise RuntimeError("transaction manifests were not validated")
        destination_root_fd = _open_directory(self.destination)
        release_root_fd = _open_directory(self.release_root)
        backup_source_fd = _open_directory(self.backup_root / "source")
        pinned = []
        mutation_started = False
        try:
            for release_entry, baseline_entry in zip(self.release_manifest["runtime_files"], self.baseline_manifest["files"]):
                relative = release_entry["path"]
                destination_parent, destination_name = _open_parent_at(destination_root_fd, relative)
                source_parent = None
                source_name = None
                if target == "candidate" or baseline_entry["present"]:
                    source_root_fd = release_root_fd if target == "candidate" else backup_source_fd
                    source_parent, source_name = _open_parent_at(source_root_fd, relative)
                pinned.append((relative, release_entry, baseline_entry, destination_parent,
                               destination_name, source_parent, source_name))
            if target == "baseline":
                owner = self.state["destination_owner"]
                for (_relative, release_entry, baseline_entry, destination_parent,
                     destination_name, _source_parent, _source_name) in pinned:
                    if not baseline_entry["present"]:
                        _unlink_exact_candidate_at(
                            destination_parent, destination_name, release_entry, owner,
                            remove=False,
                        )
            mutation_started = True
            self._stop()
            if _snapshot_mutable_state(self.destination) != self.state["mutable_state"]:
                raise ValueError("mutable state changed before source publication")
            owner = self.state["destination_owner"]
            for (relative, release_entry, baseline_entry, destination_parent, destination_name,
                 source_parent, source_name) in pinned:
                if after_boundary is not None:
                    after_boundary(f"{relative}:before-publication")
                if target == "candidate":
                    data, _ = _read_at(source_parent, source_name)
                    _atomic_write_at(destination_parent, destination_name, data,
                                     int(release_entry["mode"], 8),
                                     uid=owner["uid"], gid=owner["gid"])
                elif baseline_entry["present"]:
                    data, _ = _read_at(source_parent, source_name)
                    _atomic_write_at(destination_parent, destination_name, data,
                                     int(baseline_entry["mode"], 8),
                                     uid=owner["uid"], gid=owner["gid"])
                else:
                    _unlink_exact_candidate_at(
                        destination_parent, destination_name, release_entry, owner,
                    )
                if after_boundary is not None:
                    after_boundary(f"{relative}:published")
            for relative, _release, _baseline, destination_parent, _name, source_parent, _source_name in pinned:
                destination_parent_path = self.destination.joinpath(*PurePosixPath(relative).parts[:-1])
                if not _same_namespace_inode(destination_parent, destination_parent_path):
                    raise ValueError("destination namespace changed")
                if source_parent is not None:
                    source_root = self.release_root if target == "candidate" else self.backup_root / "source"
                    source_parent_path = source_root.joinpath(*PurePosixPath(relative).parts[:-1])
                    if not _same_namespace_inode(source_parent, source_parent_path):
                        raise ValueError("source namespace changed")
            if self._phase(destination_root_fd) != target:
                raise RuntimeError("requested source phase did not verify")
            if not _same_namespace_inode(destination_root_fd, self.destination):
                raise ValueError("destination namespace changed")
            self.service.start_ready()
            if not self.service.is_active():
                raise RuntimeError("service is inactive after readiness")
            if not _same_namespace_inode(destination_root_fd, self.destination):
                raise ValueError("destination namespace changed")
            for relative, _release, _baseline, destination_parent, _name, _source_parent, _source_name in pinned:
                destination_parent_path = self.destination.joinpath(*PurePosixPath(relative).parts[:-1])
                if not _same_namespace_inode(destination_parent, destination_parent_path):
                    raise ValueError("destination namespace changed")
            evidence = self.status()
            if evidence["service"]["main_pid_changed"] is not True:
                raise RuntimeError("service main PID did not change after source transition")
        except BaseException:
            if mutation_started:
                try:
                    self._stop()
                except BaseException:
                    pass
            raise
        finally:
            for item in pinned:
                os.close(item[3])
                if item[5] is not None:
                    os.close(item[5])
            os.close(destination_root_fd)
            os.close(release_root_fd)
            os.close(backup_source_fd)

    def recover(self, target: str, *, after_boundary=None):
        self.transition(target, operation="recover", after_boundary=after_boundary)

    def status(self):
        self._revalidate()
        phase = self._phase()
        baseline = self.state["service_baseline"]
        service = self.service.status(warning_since=baseline["warning_since"])
        required = {
            "service", "active", "substate", "http_status", "main_pid", "n_restarts",
            "control_group", "display_lock", "warning_count",
        }
        if type(service) is not dict:
            raise RuntimeError("status requires exact healthy service evidence")
        lock = service.get("display_lock")
        if (
            set(service) != required
            or service.get("service") != self.state["service"]
            or service.get("active") is not True
            or service.get("substate") != "running"
            or service.get("http_status") != 302
            or type(service.get("main_pid")) is not int
            or service["main_pid"] <= 0
            or type(service.get("n_restarts")) is not int
            or service["n_restarts"] != baseline["n_restarts"]
            or type(service.get("control_group")) is not str
            or not service["control_group"].endswith("/" + self.state["service"])
            or type(service.get("warning_count")) is not int
            or service["warning_count"] != 0
            or type(lock) is not dict
            or set(lock) != {"path", "dev", "ino", "uid", "gid", "mode", "locked"}
            or lock.get("path") != str(DISPLAY_LOCK)
            or lock.get("locked") is not True
            or any(type(lock.get(key)) is not int or lock[key] < 0 for key in ("dev", "ino", "uid", "gid"))
            or not re.fullmatch(r"[0-7]{4}", lock.get("mode", ""))
        ):
            raise RuntimeError("status requires exact healthy service evidence")
        service = dict(service)
        service["n_restarts_baseline"] = baseline["n_restarts"]
        service["previous_main_pid"] = baseline["main_pid"]
        service["main_pid_changed"] = service["main_pid"] != baseline["main_pid"]
        return {"version": 1, "boundary": self.state["boundary"], "source": phase,
                "source_sha": self.state["source_sha"] if phase == "candidate" else self.state["baseline_sha"],
                "mutable_state": self.state["mutable_state"], "service": service}

    def finalize(self):
        status = self.status()
        if status["source"] != "candidate":
            raise ValueError("finalize requires verified candidate source")
        report = {
            "version": 1, "boundary": self.state["boundary"], "accepted_source_sha": self.state["source_sha"],
            "accepted_tree_sha": self.state["tree_sha"],
            "release_manifest_sha256": self.state["release_manifest"]["sha256"],
            "baseline_manifest_sha256": self.state["baseline_manifest"]["sha256"],
            "mutable_state": status["mutable_state"], "service": status["service"],
            "status": "FINALIZED",
        }
        if set(report) != PUBLIC_FINAL_KEYS:
            raise RuntimeError("internal final report schema error")
        return report


def _baseline_manifest(approved: list[dict], baseline_sha: str, baseline_tree_sha: str,
                       boundary: str):
    files = []
    for captured in approved:
        if captured["present"]:
            files.append({
                "path": captured["path"], "present": True,
                "sha256": captured["sha256"], "size": captured["size"],
                "mode": captured["mode"],
            })
        else:
            files.append({
                "path": captured["path"], "present": False,
                "sha256": None, "size": 0, "mode": None,
            })
    return {
        "version": 1, "boundary": boundary, "source_sha": baseline_sha,
        "tree_sha": baseline_tree_sha, "files": files,
    }


def _backup_metadata(metadata: os.stat_result, *, digest: str | None = None):
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
        metadata.st_mode, metadata.st_nlink, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns, digest,
    )

def _snapshot_backup_tree(root_fd: int):
    """Capture one complete tree only through an already admitted root descriptor."""
    records = {}

    def visit(directory_fd: int, prefix: PurePosixPath):
        for name in sorted(os.listdir(directory_fd)):
            relative = (prefix / name).as_posix()
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if _backup_metadata(opened) != _backup_metadata(before):
                        raise ValueError("published backup namespace changed")
                    records[relative] = ("directory", _backup_metadata(opened), None)
                    visit(child_fd, prefix / name)
                    if _backup_metadata(os.fstat(child_fd)) != _backup_metadata(opened):
                        raise ValueError("published backup changed while traversed")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                fd = os.open(
                    name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                try:
                    data, opened = _read_descriptor(fd)
                finally:
                    os.close(fd)
                if _backup_metadata(opened) != _backup_metadata(before):
                    raise ValueError("published backup namespace changed")
                records[relative] = ("file", _backup_metadata(opened, digest=_sha(data)), data)
            else:
                raise ValueError("published backup contains an unsafe object")

    visit(root_fd, PurePosixPath())
    return records


def _revalidate_backup_namespace(backup: Path, root_fd: int, root_metadata: tuple,
                                 snapshot: dict):
    if not _same_namespace_inode(root_fd, backup):
        raise ValueError("backup namespace changed")
    if _backup_metadata(os.fstat(root_fd)) != root_metadata:
        raise ValueError("backup namespace changed")
    if _snapshot_backup_tree(root_fd) != snapshot:
        raise ValueError("backup namespace changed")


def _directory_authority_fd(path: Path, fd: int):
    metadata = os.fstat(fd)
    return {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _file_authority_snapshot(path: Path, record: tuple):
    kind, metadata, data = record
    if kind != "file":
        raise ValueError("backup manifest is not a regular file")
    dev, ino, uid, gid, mode, _nlink, _size, _mtime, _ctime, digest = metadata
    return {
        "path": str(path), "dev": dev, "ino": ino, "uid": uid, "gid": gid,
        "mode": f"{stat.S_IMODE(mode):04o}", "sha256": digest,
    }, data


def _validate_prepared_backup(backup: Path, approved: list[dict], baseline_manifest: dict,
                              *, root_fd: int | None = None):
    """Descriptor-pin and validate a published orphan through state publication."""
    if root_fd is None:
        root_fd = _open_directory(backup)
    root_info = os.fstat(root_fd)
    authority_owner = (os.geteuid(), os.getegid())
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or (root_info.st_uid, root_info.st_gid) != authority_owner
            or stat.S_IMODE(root_info.st_mode) != 0o700):
        os.close(root_fd)
        raise ValueError("published backup root is unsafe")
    try:
        root_metadata = _backup_metadata(root_info)
        snapshot = _snapshot_backup_tree(root_fd)
        expected_directories = {"source"}
        expected_files = {"baseline-source-manifest.json"}
        for captured in approved:
            if not captured["present"]:
                continue
            relative = PurePosixPath(captured["path"])
            expected_files.add((PurePosixPath("source") / relative).as_posix())
            parent = PurePosixPath("source")
            for part in relative.parts[:-1]:
                parent /= part
                expected_directories.add(parent.as_posix())
        actual_directories = {path for path, record in snapshot.items() if record[0] == "directory"}
        actual_files = {path for path, record in snapshot.items() if record[0] == "file"}
        if actual_directories != expected_directories or actual_files != expected_files:
            raise ValueError("published backup tree is incomplete or contains extras")
        for relative in expected_directories:
            metadata = snapshot[relative][1]
            if ((metadata[2], metadata[3]) != authority_owner
                    or stat.S_IMODE(metadata[4]) != 0o700):
                raise ValueError("published backup directory metadata mismatch")
        manifest_path = backup / "baseline-source-manifest.json"
        manifest_authority, raw = _file_authority_snapshot(
            manifest_path, snapshot["baseline-source-manifest.json"],
        )
        manifest_metadata = snapshot["baseline-source-manifest.json"][1]
        if ((manifest_metadata[2], manifest_metadata[3]) != authority_owner
                or stat.S_IMODE(manifest_metadata[4]) != 0o600
                or raw != canonical_json(baseline_manifest)):
            raise ValueError("published backup manifest mismatch")
        for captured in approved:
            if not captured["present"]:
                continue
            record = snapshot[(PurePosixPath("source") / captured["path"]).as_posix()]
            metadata, data = record[1], record[2]
            if ((metadata[2], metadata[3]) != authority_owner
                    or f"{stat.S_IMODE(metadata[4]):04o}" != captured["mode"]
                    or len(data) != captured["size"] or _sha(data) != captured["sha256"]):
                raise ValueError("published backup source mismatch")
        _revalidate_backup_namespace(backup, root_fd, root_metadata, snapshot)
        return {
            "fd": root_fd, "root_metadata": root_metadata, "snapshot": snapshot,
            "root_authority": _directory_authority_fd(backup, root_fd),
            "manifest_authority": manifest_authority,
        }
    except BaseException:
        os.close(root_fd)
        raise


def _running_interpreter_authority(path: Path) -> dict[str, object]:
    try:
        requested = path.stat()
        running_path = Path("/proc/self/exe").resolve(strict=True)
        running = running_path.stat()
    except OSError as error:
        raise ValueError("production interpreter identity is unavailable") from error
    if (
        not stat.S_ISREG(requested.st_mode)
        or (requested.st_dev, requested.st_ino) != (running.st_dev, running.st_ino)
        or not os.access(path, os.X_OK)
    ):
        raise ValueError("transaction is not running under the required production interpreter")
    data, opened = _read_regular(running_path, max_bytes=64 * 1024 * 1024)
    return {
        "path": str(path), "resolved_path": str(running_path),
        "dev": opened.st_dev, "ino": opened.st_ino,
        "uid": opened.st_uid, "gid": opened.st_gid,
        "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
        "sha256": _sha(data),
        "version": [sys.version_info.major, sys.version_info.minor, sys.version_info.micro],
    }


def _mutable_tree_record(destination: Path, relative: str) -> dict[str, object]:
    root = _contained_path(destination, relative)
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return {"path": relative, "present": False, "tree_sha256": None}
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise ValueError(f"mutable state root {relative} is unsafe")
    records = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        child = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"mutable state root {relative} contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            records.append({
                "path": child, "type": "directory", "uid": metadata.st_uid,
                "gid": metadata.st_gid, "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            })
        elif stat.S_ISREG(metadata.st_mode):
            data, opened = _read_regular(path)
            records.append({
                "path": child, "type": "file", "uid": opened.st_uid,
                "gid": opened.st_gid, "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
                "size": len(data), "sha256": _sha(data),
            })
        else:
            raise ValueError(f"mutable state root {relative} contains an unsafe entry")
    return {
        "path": relative, "present": True,
        "tree_sha256": _sha(canonical_json(records)),
    }


def _snapshot_mutable_state(destination: Path) -> list[dict[str, object]]:
    return [
        _mutable_tree_record(destination, "data"),
        _mutable_tree_record(destination, "resources/media"),
    ]


def _contains_bitmap_key(value) -> bool:
    if type(value) is dict:
        return "bitmap_rgba" in value or any(_contains_bitmap_key(item) for item in value.values())
    if type(value) is list:
        return any(_contains_bitmap_key(item) for item in value)
    return False


def _validate_boundary_configuration(destination: Path, boundary: str) -> None:
    config_path = destination / "data/config.json"
    try:
        raw, _ = _read_regular(config_path, max_bytes=1024 * 1024)
        config = _parse_json(raw, "production configuration")
    except FileNotFoundError:
        return
    if type(config) is not dict:
        raise ValueError("production configuration is invalid")
    if boundary == "b1" and _contains_bitmap_key(config):
        raise ValueError("B1 production configuration must not contain a pixel bitmap")


def _remove_exact_backup_tree(root_fd: int, parent_fd: int, name: str, snapshot: dict):
    """Remove only the caller-owned tree still bound to its pinned descriptor."""
    root = os.fstat(root_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (root.st_dev, root.st_ino) != (current.st_dev, current.st_ino):
        return False
    if _snapshot_backup_tree(root_fd) != snapshot:
        return False
    files = [path for path, record in snapshot.items() if record[0] == "file"]
    directories = [path for path, record in snapshot.items() if record[0] == "directory"]
    for relative in sorted(files, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        directory_fd, leaf = _open_parent_at(root_fd, relative)
        try:
            current = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if _backup_metadata(current, digest=snapshot[relative][1][-1]) != snapshot[relative][1]:
                return False
            os.unlink(leaf, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    for relative in sorted(directories, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        directory_fd, leaf = _open_parent_at(root_fd, relative)
        try:
            current = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (snapshot[relative][1][0], snapshot[relative][1][1]):
                return False
            os.rmdir(leaf, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (root.st_dev, root.st_ino) != (current.st_dev, current.st_ino):
        return False
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return True


def _ensure_backup_parent_at(root_fd: int, relative: str):
    """Create/open one mode-0700 parent chain beneath a pinned private root."""
    parts = PurePosixPath(_safe_relative(relative)).parts[:-1]
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            metadata = os.fstat(child)
            if ((metadata.st_uid, metadata.st_gid) != (os.geteuid(), os.getegid())
                    or stat.S_IMODE(metadata.st_mode) != 0o700):
                os.close(child)
                raise ValueError("temporary backup directory metadata mismatch")
            os.fsync(current)
            os.close(current)
            current = child
        return current, PurePosixPath(relative).name
    except BaseException:
        os.close(current)
        raise


def _fsync_backup_tree_at(root_fd: int):
    """Durably flush every admitted directory from leaves to the pinned root."""
    for name in sorted(os.listdir(root_fd)):
        metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                _fsync_backup_tree_at(child)
            finally:
                os.close(child)
        elif not stat.S_ISREG(metadata.st_mode):
            raise ValueError("temporary backup contains an unsafe object")
    os.fsync(root_fd)


def _build_and_publish_backup(backup: Path, approved: list[dict], baseline_manifest: dict,
                              *, after_boundary=None):
    parent = backup.parent
    parent_fd = _open_directory(parent)
    temporary_name = _fresh_sibling_name(backup)
    temporary_fd = -1
    published = False
    authority = None
    try:
        if not _same_namespace_inode(parent_fd, parent):
            raise ValueError("backup parent namespace changed")
        os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        temporary_fd = os.open(
            temporary_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        root = os.fstat(temporary_fd)
        if ((root.st_uid, root.st_gid) != (os.geteuid(), os.getegid())
                or stat.S_IMODE(root.st_mode) != 0o700):
            raise ValueError("temporary backup root metadata mismatch")
        os.mkdir("source", mode=0o700, dir_fd=temporary_fd)
        os.fsync(temporary_fd)
        for captured in approved:
            if not captured["present"]:
                continue
            relative = (PurePosixPath("source") / captured["path"]).as_posix()
            destination_parent, destination_name = _ensure_backup_parent_at(
                temporary_fd, relative,
            )
            try:
                _atomic_write_at(
                    destination_parent, destination_name, captured["data"],
                    int(captured["mode"], 8), uid=os.geteuid(), gid=os.getegid(),
                )
            finally:
                os.close(destination_parent)
        _atomic_write_at(
            temporary_fd, "baseline-source-manifest.json",
            canonical_json(baseline_manifest), 0o600,
            uid=os.geteuid(), gid=os.getegid(),
        )
        _fsync_backup_tree_at(temporary_fd)
        temporary_snapshot = _snapshot_backup_tree(temporary_fd)
        if not _same_namespace_inode(parent_fd, parent):
            raise ValueError("backup parent namespace changed")
        try:
            if after_boundary is not None:
                after_boundary("backup:before-publication")
            _rename_noreplace(temporary_name, backup.name, parent_fd=parent_fd)
        except FileExistsError:
            _remove_exact_backup_tree(temporary_fd, parent_fd, temporary_name, temporary_snapshot)
        else:
            published = True
            os.fsync(parent_fd)
        if not _same_namespace_inode(parent_fd, parent):
            raise ValueError("backup parent namespace changed")
        if published:
            if after_boundary is not None:
                after_boundary("backup:published")
            admitted_fd = temporary_fd
            temporary_fd = -1
            authority = _validate_prepared_backup(
                backup, approved, baseline_manifest, root_fd=admitted_fd,
            )
    except BaseException:
        if temporary_fd >= 0 and not published:
            try:
                snapshot = _snapshot_backup_tree(temporary_fd)
                _remove_exact_backup_tree(temporary_fd, parent_fd, temporary_name, snapshot)
            except (FileNotFoundError, OSError, ValueError):
                pass
        raise
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        os.close(parent_fd)
    if authority is not None:
        return authority
    return _validate_prepared_backup(backup, approved, baseline_manifest)


def prepare_release(arguments):
    boundary = getattr(arguments, "boundary", None)
    if boundary not in ("b1", "b2"):
        raise ValueError("release boundary is invalid")
    release_root = _absolute(arguments.release_root, "release root", must_exist=True)
    release_manifest_path = _absolute(arguments.release_manifest, "release manifest", must_exist=True)
    fixture_manifest_path = _absolute(arguments.fixture_manifest, "fixture manifest", must_exist=True)
    destination = _absolute(arguments.destination, "destination", must_exist=True)
    backup_root = _absolute(arguments.backup_root, "backup root")
    backup_archive = backup_root / "archive"
    state_path = _absolute(arguments.state, "state")
    interpreter = _absolute(arguments.interpreter, "interpreter", must_exist=True)
    if not release_root.is_dir() or release_root.is_symlink() or not destination.is_dir() or destination.is_symlink():
        raise ValueError("release and destination must be real directories")
    if any(first == second for index, first in enumerate((release_root, destination, backup_root))
           for second in (release_root, destination, backup_root)[index + 1:]):
        raise ValueError("release, destination, and backup roots must be distinct")
    if state_path.parent != backup_root:
        raise ValueError("state must be directly inside backup root")
    if SERVICE.fullmatch(arguments.service or "") is None:
        raise ValueError("service name is invalid")
    interpreter_target = interpreter.resolve(strict=True)
    interpreter_info = interpreter_target.lstat()
    if not stat.S_ISREG(interpreter_info.st_mode) or not os.access(interpreter, os.X_OK):
        raise ValueError("interpreter is unsafe")
    release_raw, _ = _read_regular(release_manifest_path)
    fixture_raw, _ = _read_regular(fixture_manifest_path)
    release = _validate_release_manifest(
        release_raw, release_root, expected_boundary=boundary,
    )
    runtime = release["runtime"]
    if (
        (str(destination), str(interpreter), arguments.service)
        != (runtime["destination"], runtime["interpreter"], runtime["service"])
    ):
        raise ValueError(
            "production runtime identity mismatch: "
            f"observed={(str(destination), str(interpreter), arguments.service)!r} "
            f"expected={(runtime['destination'], runtime['interpreter'], runtime['service'])!r}"
        )
    interpreter_authority = _running_interpreter_authority(interpreter)
    fixture = _validate_fixture_manifest(
        fixture_raw, fixture_manifest_path.parent, expected_boundary=boundary,
    )
    expected = {
        "baseline_sha": arguments.expected_baseline_source_sha,
        "source_sha": arguments.expected_source_sha,
    }
    for value in expected.values():
        if SHA1.fullmatch(value or "") is None:
            raise ValueError("expected Git identity is invalid")
    if any(release[key] != value for key, value in expected.items()):
        raise ValueError("release manifest expected identity mismatch")
    baseline_tree_sha = release["baseline_tree_sha"]
    source_tree_sha = release["tree_sha"]
    if (fixture["source_sha"], fixture["tree_sha"], _sha(fixture_raw)) != (
            arguments.expected_source_sha, source_tree_sha, release["fixture_manifest_sha256"]):
        raise ValueError("fixture manifest expected identity mismatch")
    destination_info = destination.lstat()
    destination_owner = {"uid": destination_info.st_uid, "gid": destination_info.st_gid}
    _validate_boundary_configuration(destination, boundary)
    mutable_state = _snapshot_mutable_state(destination)
    approved = _approved_baseline_snapshot(
        destination, release, arguments.expected_baseline_source_sha,
        baseline_tree_sha, destination_owner,
    )
    if not backup_root.exists():
        parent_info = backup_root.parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise ValueError("backup ancestry is unsafe")
        backup_root.mkdir(mode=0o700)
        _fsync_dir(backup_root.parent)
    backup_root_info = backup_root.lstat()
    if (
        not stat.S_ISDIR(backup_root_info.st_mode)
        or stat.S_ISLNK(backup_root_info.st_mode)
        or (backup_root_info.st_uid, backup_root_info.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(backup_root_info.st_mode) != 0o700
        or backup_root.resolve(strict=True) != backup_root
    ):
        raise ValueError("backup root is unsafe")
    if state_path.exists() or state_path.is_symlink():
        raise ValueError("state authority must be fresh")

    baseline_manifest = _baseline_manifest(
        approved, arguments.expected_baseline_source_sha, baseline_tree_sha, boundary,
    )
    service = SystemdService(arguments.service)
    if not service.is_active():
        raise ValueError("prepare requires the baseline service active")
    initial_service = service.status()
    if (type(initial_service) is not dict or initial_service.get("service") != arguments.service
            or type(initial_service.get("n_restarts")) is not int
            or initial_service["n_restarts"] < 0
            or type(initial_service.get("main_pid")) is not int
            or initial_service["main_pid"] <= 0):
        raise ValueError("baseline service evidence is invalid")
    service_baseline = {
        "main_pid": initial_service["main_pid"],
        "n_restarts": initial_service["n_restarts"], "warning_since": int(time.time()),
    }

    callback = getattr(arguments, "after_boundary", None)
    if backup_archive.exists() or backup_archive.is_symlink():
        backup_authority = _validate_prepared_backup(
            backup_archive, approved, baseline_manifest,
        )
    else:
        backup_authority = _build_and_publish_backup(
            backup_archive, approved, baseline_manifest, after_boundary=callback,
        )

    if _snapshot_mutable_state(destination) != mutable_state:
        os.close(backup_authority["fd"])
        raise ValueError("mutable state changed during prepare")
    try:
        parent_fd = _open_directory(state_path.parent)
    except BaseException:
        os.close(backup_authority["fd"])
        raise
    state_fd = -1
    temporary_name = _fresh_sibling_name(state_path)
    temporary_identity = None
    state_published = False
    completed = False
    try:
        state_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600, dir_fd=parent_fd,
        )
        os.fchmod(state_fd, 0o600)
        os.fchown(state_fd, os.geteuid(), os.getegid())
        temporary_metadata = os.fstat(state_fd)
        temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
        fcntl.flock(state_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {
            "version": 1, "boundary": boundary,
            "baseline_sha": arguments.expected_baseline_source_sha,
            "baseline_tree_sha": baseline_tree_sha,
            "source_sha": arguments.expected_source_sha, "tree_sha": source_tree_sha,
            "release_root": _directory_authority(release_root), "release_manifest": _manifest_file(release_manifest_path),
            "fixture_manifest": _manifest_file(fixture_manifest_path),
            "baseline_manifest": backup_authority["manifest_authority"],
            "destination": _directory_authority(destination),
            "destination_owner": destination_owner,
            "backup_root": _directory_authority(backup_root),
            "backup_archive": backup_authority["root_authority"],
            "service": arguments.service,
            "service_baseline": service_baseline, "interpreter": interpreter_authority,
            "state_path": str(state_path), "mutable_state": mutable_state, "self": None,
        }
        _populate_state_fd(state_path, state, state_fd)
        _revalidate_backup_namespace(
            backup_archive, backup_authority["fd"], backup_authority["root_metadata"],
            backup_authority["snapshot"],
        )
        if callback is not None:
            callback("state:before-publication")
        _rename_noreplace(temporary_name, state_path.name, parent_fd=parent_fd)
        state_published = True
        _revalidate_backup_namespace(
            backup_archive, backup_authority["fd"], backup_authority["root_metadata"],
            backup_authority["snapshot"],
        )
        if not _same_namespace_inode(state_fd, state_path):
            raise ValueError("state identity changed after publication")
        os.fsync(parent_fd)
        tx = Priority17Transaction(state_path, state, service=service, state_fd=state_fd)
        state_fd = -1
        try:
            tx._revalidate()
        finally:
            tx.close()
        completed = True
        if callback is not None:
            callback("state:published")
        return {
            "version": 1, "status": "BACKUP_READY", "boundary": boundary,
            "baseline_sha": state["baseline_sha"],
            "baseline_tree_sha": state["baseline_tree_sha"],
            "source_sha": state["source_sha"], "tree_sha": state["tree_sha"],
            "backup_root": state["backup_root"], "backup_archive": state["backup_archive"],
            "state": state["self"], "mutable_state": state["mutable_state"],
        }
    finally:
        if state_published and not completed:
            try:
                current = os.stat(state_path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == temporary_identity:
                    os.unlink(state_path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
        if state_fd >= 0:
            os.close(state_fd)
        if temporary_identity is not None:
            try:
                current = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == temporary_identity:
                    os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)
        os.close(backup_authority["fd"])


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    add_store_once_argument(build, "--boundary", choices=("b1", "b2"), required=True)
    for option in ("--repository", "--baseline-commit", "--source-commit",
                   "--expected-reviewed-diff-sha256", "--stage-root", "--python"):
        add_store_once_argument(build, option, required=True)
    prepare = commands.add_parser("prepare")
    add_store_once_argument(prepare, "--boundary", choices=("b1", "b2"), required=True)
    for option in ("--release-root", "--release-manifest", "--fixture-manifest",
                   "--expected-baseline-source-sha", "--expected-source-sha", "--destination",
                   "--backup-root", "--service", "--interpreter", "--state"):
        add_store_once_argument(prepare, option, required=True)
    for command in ("deploy", "status", "rollback", "redeploy", "finalize"):
        sub = commands.add_parser(command)
        add_store_once_argument(sub, "--state", required=True)
    recover = commands.add_parser("recover")
    add_store_once_argument(recover, "--state", required=True)
    add_store_once_argument(recover, "--target", choices=("baseline", "candidate"), required=True)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if arguments.command == "build":
        print(canonical_json(build_release(arguments)).decode(), end="")
        return 0
    if arguments.command == "prepare":
        print(canonical_json(prepare_release(arguments)).decode(), end="")
        return 0
    tx = Priority17Transaction.load(Path(arguments.state))
    if arguments.command == "deploy":
        tx.transition("candidate", operation="deploy")
    elif arguments.command == "rollback":
        tx.transition("baseline", operation="rollback")
    elif arguments.command == "redeploy":
        tx.transition("candidate", operation="redeploy")
    elif arguments.command == "recover":
        tx.recover(arguments.target)
    elif arguments.command == "status":
        print(canonical_json(tx.status()).decode(), end="")
    else:
        print(canonical_json(tx.finalize()).decode(), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"priority-1.7 transaction failed: {error}", file=sys.stderr)
        raise SystemExit(2)
