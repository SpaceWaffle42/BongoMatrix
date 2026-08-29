#!/usr/bin/env python3
"""Crash-safe, source-only Priority 1.5 B1 release transaction."""
from __future__ import annotations

import argparse
import base64
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
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Callable

from PIL import Image, ImageSequence

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))


class _StoreOnce(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_priority_1_5_seen", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_priority_1_5_seen", seen)
        if self.dest in seen:
            parser.error(f"{option_string} may be specified only once")
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


def add_store_once_argument(parser, name, **kwargs):
    kwargs["action"] = _StoreOnce
    parser.add_argument(name, **kwargs)

GIT = "/usr/bin/git"
SYSTEMCTL = "/usr/bin/systemctl"
FFPROBE = "/usr/bin/ffprobe"
JOURNALCTL = "/usr/bin/journalctl"
DISPLAY_LOCK = Path("/run/matrix-display/matrix-display.lock")
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SERVICE = re.compile(r"[A-Za-z0-9@_.:-]+\.service")
FORBIDDEN_PREFIXES = ("data/", "venv/", ".git/", "resources/media/")
STATE_KEYS = {
    "version", "boundary", "baseline_sha", "baseline_tree_sha", "source_sha", "tree_sha",
    "release_root", "release_manifest", "fixture_manifest", "baseline_manifest",
    "destination", "destination_owner", "backup_root", "service", "service_baseline",
    "interpreter", "state_path", "self",
}
PUBLIC_FINAL_KEYS = {
    "version", "boundary", "accepted_source_sha", "accepted_tree_sha",
    "release_manifest_sha256", "baseline_manifest_sha256", "status",
}

# Generated once with a bit-exact one-frame ffmpeg invocation.  Embedding makes
# the committed-object builder independent of host encoders and their metadata.
_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
_MP4 = base64.b64decode(
    "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAALybW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAGQAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAkF0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAGQAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAIAAAACAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAABkAAAAAAABAAAAAAG5bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAABABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABZG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAASRzdGJsAAAAwHN0c2QAAAAAAAAAAQAAALBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAIAAgBIAAAASAAAAAAAAAABDExhdmMgbGlieDI2NAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAK/+EAGWdkAAqs2V+IiMBEAAADAAQAAAMAUDxIllgBAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAA2xAAAAAAAAAAGHN0dHMAAAAAAAAAAQAAAAEAAAQAAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAABAAAAAQAAABRzdHN6AAAAAAAAAr0AAAABAAAAFHN0Y28AAAAAAAAAAQAAAyIAAAA9dWR0YQAAADVtZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAAhpbHN0AAAACGZyZWUAAALFbWRhdAAAAqAGBf//nNxF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjUgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDI1IC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRjdD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTMgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJfYmlhcz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MiBrZXlpbnQ9MjUwIGtleWludF9taW49MTAgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAVZYiEADf//uED+BTPe/mMat1RZaZB"
)


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


def _fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def _publish_state(path: Path, state: dict):
    """Populate self-authority from one fresh inode, then rename that inode once."""
    parent = path.parent
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    published = False
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, os.geteuid(), os.getegid())
        metadata = os.fstat(fd)
        state["self"] = {
            "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
            "uid": metadata.st_uid, "gid": metadata.st_gid, "mode": "0600", "sha256": "",
        }
        state["self"]["sha256"] = _state_digest(state)
        raw = canonical_json(state)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable state write")
            view = view[count:]
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


def _mkdir_fresh(path: Path, mode: int = 0o700):
    if path.exists() or path.is_symlink():
        raise ValueError(f"path must be fresh: {path}")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"unsafe parent: {parent}")
    path.mkdir(mode=mode)
    os.chmod(path, mode)
    _fsync_dir(parent)


def _git(repository: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        [GIT, "-C", str(repository), *args], check=True, capture_output=True,
        text=not binary, timeout=30, shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout


def _commit(repository: Path, value: str) -> tuple[str, str]:
    if SHA1.fullmatch(value) is None:
        raise ValueError("commit identity must be lowercase full SHA")
    commit = _git(repository, "rev-parse", "--verify", f"{value}^{{commit}}").strip()
    if commit != value:
        raise ValueError("requested identity is not an exact commit")
    tree = _git(repository, "rev-parse", "--verify", f"{value}^{{tree}}").strip()
    if SHA1.fullmatch(tree) is None:
        raise ValueError("commit tree identity is invalid")
    return commit, tree


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
    keys = {"version", "boundary", "baseline_sha", "source_sha", "tree_sha",
            "reviewed_diff_sha256", "fixture_manifest_sha256", "files"}
    if type(value) is not dict or set(value) != keys or value["version"] != 1 or value["boundary"] != expected_boundary:
        raise ValueError("release manifest schema is invalid")
    for key in ("baseline_sha", "source_sha", "tree_sha"):
        if type(value[key]) is not str or SHA1.fullmatch(value[key]) is None:
            raise ValueError("release Git identity is invalid")
    for key in ("reviewed_diff_sha256", "fixture_manifest_sha256"):
        if type(value[key]) is not str or SHA256.fullmatch(value[key]) is None:
            raise ValueError("release digest is invalid")
    files = value["files"]
    if type(files) is not list or not files:
        raise ValueError("release files must be nonempty")
    names = []
    for entry in files:
        if type(entry) is not dict or set(entry) != {"mode", "path", "sha256", "size"}:
            raise ValueError("release file entry schema is invalid")
        relative = _safe_relative(entry["path"])
        if relative.startswith(FORBIDDEN_PREFIXES):
            raise ValueError("mutable or private path is forbidden from source release")
        if entry["mode"] not in ("0644", "0755") or type(entry["size"]) is not int or entry["size"] < 0:
            raise ValueError("release file metadata is invalid")
        if type(entry["sha256"]) is not str or SHA256.fullmatch(entry["sha256"]) is None:
            raise ValueError("release file digest is invalid")
        path = _contained_path(release_root, relative)
        data, metadata = _read_regular(path)
        if (len(data), _sha(data), f"{stat.S_IMODE(metadata.st_mode):04o}") != (
                entry["size"], entry["sha256"], entry["mode"]):
            raise ValueError("release file bytes or metadata mismatch")
        names.append(relative)
    if names != sorted(set(names)):
        raise ValueError("release paths are unordered or duplicate")
    actual = []
    for path in sorted(release_root.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("release tree contains unsafe object")
        if stat.S_ISREG(info.st_mode):
            actual.append(path.relative_to(release_root).as_posix())
    if actual != names:
        raise ValueError("release tree has missing or extra files")
    return value


def _decoded_fixture_metadata(path: Path, kind: str) -> tuple[int, int, int, list[int]]:
    if kind == "gif":
        try:
            with Image.open(path) as image:
                fallback = image.info.get("duration", 100)
                durations = []
                for frame in ImageSequence.Iterator(image):
                    duration = frame.info.get("duration", fallback)
                    if type(duration) is not int:
                        raise ValueError("GIF fixture timeline mismatch")
                    durations.append(duration)
                return image.width, image.height, len(durations), durations
        except (OSError, ValueError) as error:
            raise ValueError("GIF fixture decoder mismatch") from error
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,nb_frames,avg_frame_rate,duration:frame=duration_time",
         "-of", "json", str(path)], check=False, capture_output=True, text=True,
        timeout=15, shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise ValueError("MP4 fixture decoder mismatch")
    try:
        probe = json.loads(result.stdout, object_pairs_hook=_unique)
        streams = probe["streams"]
        frames = probe["frames"]
        if type(streams) is not list or len(streams) != 1 or type(frames) is not list or not frames:
            raise ValueError
        stream = streams[0]
        width, height = stream["width"], stream["height"]
        fallback = Fraction(1, 1) / Fraction(stream["avg_frame_rate"])
        durations = []
        for frame in frames:
            seconds = Fraction(frame.get("duration_time")) if frame.get("duration_time") else fallback
            milliseconds = seconds * 1000
            if milliseconds.denominator != 1:
                raise ValueError
            durations.append(milliseconds.numerator)
        if "nb_frames" in stream and int(stream["nb_frames"]) != len(frames):
            raise ValueError
        if type(width) is not int or type(height) is not int:
            raise ValueError
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError("MP4 fixture decoder mismatch") from error
    return width, height, len(frames), durations


def _validate_fixture_manifest(raw: bytes, root: Path):
    value = _parse_json(raw, "fixture manifest")
    if type(value) is not dict or set(value) != {"version", "boundary", "source_sha", "tree_sha", "fixtures"}:
        raise ValueError("fixture manifest schema is invalid")
    if value["version"] != 1 or value["boundary"] != "b1":
        raise ValueError("fixture manifest boundary is invalid")
    fixtures = value["fixtures"]
    if type(fixtures) is not list or not fixtures:
        raise ValueError("fixture list is invalid")
    names = []
    for entry in fixtures:
        keys = {"path", "sha256", "size", "kind", "width", "height", "frames", "durations_ms"}
        if type(entry) is not dict or set(entry) != keys:
            raise ValueError("fixture entry schema is invalid")
        relative = _safe_relative(entry["path"])
        if not relative.startswith("fixtures/") or entry["kind"] not in ("gif", "mp4"):
            raise ValueError("fixture path or kind is invalid")
        integers = (entry["size"], entry["width"], entry["height"], entry["frames"])
        if any(type(item) is not int for item in integers) or entry["size"] < 0 or any(item <= 0 for item in integers[1:]):
            raise ValueError("fixture dimensions are invalid")
        durations = entry["durations_ms"]
        if type(durations) is not list or len(durations) != entry["frames"] or any(
                type(item) is not int or not 20 <= item <= 10000 for item in durations):
            raise ValueError("fixture timeline is invalid")
        fixture_path = _contained_path(root, relative)
        data, _ = _read_regular(fixture_path)
        if len(data) != entry["size"] or _sha(data) != entry["sha256"]:
            raise ValueError("fixture bytes mismatch")
        actual = _decoded_fixture_metadata(fixture_path, entry["kind"])
        if actual != (entry["width"], entry["height"], entry["frames"], durations):
            raise ValueError("fixture metadata mismatch")
        names.append(relative)
    if names != sorted(set(names)):
        raise ValueError("fixture paths are unordered or duplicate")
    return value


def build_b1(arguments) -> dict[str, object]:
    repository = _absolute(arguments.repository, "repository", must_exist=True)
    if not repository.is_dir() or repository.is_symlink():
        raise ValueError("repository is unsafe")
    baseline, _baseline_tree = _commit(repository, arguments.baseline_commit)
    source, tree = _commit(repository, arguments.source_commit)
    if baseline == source:
        raise ValueError("baseline and source commits must differ")
    if SHA256.fullmatch(arguments.expected_reviewed_diff_sha256 or "") is None:
        raise ValueError("reviewed diff digest is invalid")
    python = _absolute(arguments.python, "python", must_exist=True)
    py_target = python.resolve(strict=True)
    py_info = py_target.lstat()
    if not stat.S_ISREG(py_info.st_mode) or not os.access(python, os.X_OK):
        raise ValueError("python interpreter is unsafe")
    stage = _absolute(arguments.stage_root, "stage root")
    created = False
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
                raise ValueError("B1 source diff contains a deletion, rename, or unsupported change")
            relative = _safe_relative(pieces[index].decode("utf-8"))
            index += 1
            if relative.startswith(FORBIDDEN_PREFIXES):
                raise ValueError("B1 source diff contains mutable/private paths")
            changed.append(relative)
        if changed != sorted(set(changed)) or not changed:
            raise ValueError("changed-file allowlist is empty, duplicate, or unordered")
        records = []
        for relative in changed:
            data = _git(repository, "show", f"{source}:{relative}", binary=True)
            tree_line = _git(repository, "ls-tree", source, "--", relative).strip()
            if not tree_line:
                raise ValueError("changed path is absent from source tree")
            mode_value, object_type, _rest = tree_line.split(None, 2)
            if object_type != "blob" or mode_value not in ("100644", "100755"):
                raise ValueError("changed Git object mode/type is unsupported")
            mode = "0755" if mode_value == "100755" else "0644"
            destination = release.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _durable_new_file(destination, data, int(mode, 8))
            records.append({"mode": mode, "path": relative, "sha256": _sha(data), "size": len(data)})
        fixture_specs = (("fixtures/b1-reference.gif", "gif", _GIF), ("fixtures/b1-reference.mp4", "mp4", _MP4))
        fixture_records = []
        for relative, kind, data in fixture_specs:
            path = stage.joinpath(*PurePosixPath(relative).parts)
            _durable_new_file(path, data, 0o644)
            width, height, frames, durations = _decoded_fixture_metadata(path, kind)
            fixture_records.append({
                "path": relative, "sha256": _sha(data), "size": len(data), "kind": kind,
                "width": width, "height": height, "frames": frames,
                "durations_ms": durations,
            })
        fixture_manifest = {"version": 1, "boundary": "b1", "source_sha": source,
                            "tree_sha": tree, "fixtures": fixture_records}
        fixture_bytes = canonical_json(fixture_manifest)
        _atomic_write(stage / "physical-fixtures.json", fixture_bytes, 0o600)
        release_manifest = {
            "version": 1, "boundary": "b1", "baseline_sha": baseline, "source_sha": source,
            "tree_sha": tree, "reviewed_diff_sha256": _sha(diff),
            "fixture_manifest_sha256": _sha(fixture_bytes), "files": records,
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
        return {
            "boundary": "b1", "diff_sha256": _sha(diff), "file_count": len(records),
            "fixture_count": len(fixture_records), "fixture_manifest": str(stage / "physical-fixtures.json"),
            "fixture_manifest_sha256": _sha(fixture_bytes), "release_manifest": str(stage / "release-manifest.json"),
            "release_manifest_sha256": _sha(release_bytes), "source_sha": source,
            "stage_root": str(stage), "tree_sha": tree,
        }
    except BaseException:
        if created:
            shutil.rmtree(stage, ignore_errors=True)
        raise


class SystemdService:
    def __init__(self, service: str):
        if SERVICE.fullmatch(service) is None:
            raise ValueError("service name is invalid")
        self.name = service

    def _run(self, *args, check=True):
        return subprocess.run(
            [SYSTEMCTL, *args, self.name], check=check, capture_output=True, text=True,
            timeout=15, shell=False,
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

    def status(self, *, warning_since=None):
        properties_result = self._run(
            "show",
            "--property=Id,ActiveState,SubState,MainPID,NRestarts,ControlGroup",
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

        connection = http.client.HTTPConnection("127.0.0.1", 5000, timeout=2)
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
        holder_pid = None
        locks_raw, _ = _read_regular(Path("/proc/locks"), max_bytes=1024 * 1024)
        expected_device = f"{os.major(lock_metadata.st_dev):02x}:{os.minor(lock_metadata.st_dev):02x}"
        for line in locks_raw.decode("ascii").splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[1] != "FLOCK":
                continue
            device, separator, inode = fields[5].rpartition(":")
            if separator and device.lower() == expected_device and inode == str(lock_metadata.st_ino):
                try:
                    holder_pid = int(fields[4])
                except ValueError as error:
                    raise RuntimeError("display lock owner is malformed") from error
                break
        if holder_pid != main_pid:
            raise RuntimeError("display lock is not owned by the service main process")

        warning_count = 0
        if warning_since is not None:
            if type(warning_since) is not int or warning_since < 0:
                raise ValueError("warning baseline is invalid")
            journal = subprocess.run(
                [
                    JOURNALCTL, "--unit", self.name, "--since", f"@{warning_since}",
                    "--priority", "warning", "--output", "cat", "--no-pager",
                ],
                check=True, capture_output=True, timeout=15, shell=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if len(journal.stdout) > 1024 * 1024:
                raise RuntimeError("service warning evidence exceeds bound")
            warning_count = sum(bool(line) for line in journal.stdout.splitlines())

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


class Priority15Transaction:
    def __init__(self, state_path: Path, state: dict, *, service=None):
        self.state_path = state_path
        self.state = state
        self.service = service or SystemdService(state["service"])
        self.release_manifest = None
        self.baseline_manifest = None

    @classmethod
    def load(cls, state_path, *, service=None):
        path = _absolute(str(state_path), "state", must_exist=True)
        raw, metadata = _read_regular(path)
        if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("state must be owner-bound mode 0600")
        state = _parse_json(raw, "transaction state")
        if type(state) is not dict or set(state) != STATE_KEYS or state["version"] != 1 or state["boundary"] != "b1":
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
        tx = cls(path, state, service=service)
        tx._revalidate()
        return tx

    def _revalidate(self):
        release_raw, _ = _validate_file_authority(self.state["release_manifest"])
        fixture_raw, _ = _validate_file_authority(self.state["fixture_manifest"])
        baseline_raw, _ = _validate_file_authority(self.state["baseline_manifest"])
        release_root = _validate_directory_authority(self.state["release_root"], "release root")
        destination_root = _validate_directory_authority(self.state["destination"], "destination")
        backup = _validate_directory_authority(self.state["backup_root"], "backup root")
        owner = self.state["destination_owner"]
        if (type(owner) is not dict or set(owner) != {"uid", "gid"}
                or any(type(owner[key]) is not int for key in owner)
                or (owner["uid"], owner["gid"]) != (
                    self.state["destination"]["uid"], self.state["destination"]["gid"],
                )):
            raise ValueError("destination owner authority is invalid")
        baseline_service = self.state["service_baseline"]
        if (type(baseline_service) is not dict
                or set(baseline_service) != {"n_restarts", "warning_since"}
                or any(type(baseline_service[key]) is not int for key in baseline_service)
                or min(baseline_service.values()) < 0):
            raise ValueError("service baseline authority is invalid")
        release = _validate_release_manifest(release_raw, release_root)
        fixture = _validate_fixture_manifest(fixture_raw, Path(self.state["fixture_manifest"]["path"]).parent)
        baseline = _parse_json(baseline_raw, "baseline source manifest")
        if release["fixture_manifest_sha256"] != _sha(fixture_raw):
            raise ValueError("fixture manifest digest mismatch")
        if (release["baseline_sha"], release["source_sha"], release["tree_sha"]) != (
                self.state["baseline_sha"], self.state["source_sha"], self.state["tree_sha"]):
            raise ValueError("release/state Git identity mismatch")
        if (fixture["source_sha"], fixture["tree_sha"]) != (self.state["source_sha"], self.state["tree_sha"]):
            raise ValueError("fixture/state Git identity mismatch")
        keys = {"version", "boundary", "source_sha", "tree_sha", "files"}
        if type(baseline) is not dict or set(baseline) != keys or baseline["version"] != 1 or baseline["boundary"] != "b1":
            raise ValueError("baseline manifest schema is invalid")
        if (baseline["source_sha"], baseline["tree_sha"]) != (self.state["baseline_sha"], self.state["baseline_tree_sha"]):
            raise ValueError("baseline manifest identity mismatch")
        if [entry.get("path") for entry in baseline["files"]] != [entry["path"] for entry in release["files"]]:
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
        return Path(self.state["backup_root"]["path"])

    def _phase(self):
        if self.release_manifest is None or self.baseline_manifest is None:
            raise RuntimeError("transaction manifests were not validated")
        candidate = True
        baseline = True
        for release_entry, baseline_entry in zip(self.release_manifest["files"], self.baseline_manifest["files"]):
            path = _contained_path(self.destination, release_entry["path"])
            try:
                data, metadata = _read_regular(path)
                observed = (
                    _sha(data), len(data), f"{stat.S_IMODE(metadata.st_mode):04o}",
                    metadata.st_uid, metadata.st_gid,
                )
            except FileNotFoundError:
                observed = None
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
        self._stop()
        if self.release_manifest is None or self.baseline_manifest is None:
            raise RuntimeError("transaction manifests were not validated")
        try:
            owner = self.state["destination_owner"]
            for release_entry, baseline_entry in zip(self.release_manifest["files"], self.baseline_manifest["files"]):
                relative = release_entry["path"]
                destination = _contained_path(self.destination, relative)
                if target == "candidate":
                    data, _ = _read_regular(_contained_path(self.release_root, relative))
                    mode = int(release_entry["mode"], 8)
                    _atomic_write(destination, data, mode, uid=owner["uid"], gid=owner["gid"])
                elif baseline_entry["present"]:
                    data, _ = _read_regular(_contained_path(self.backup_root / "source", relative))
                    _atomic_write(
                        destination, data, int(baseline_entry["mode"], 8),
                        uid=owner["uid"], gid=owner["gid"],
                    )
                else:
                    try:
                        info = destination.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                            raise ValueError("rollback unlink target is unsafe")
                        destination.unlink()
                        _fsync_dir(destination.parent)
                if after_boundary is not None:
                    after_boundary(f"{relative}:published")
            if self._phase() != target:
                raise RuntimeError("requested source phase did not verify")
            self.service.start_ready()
            if not self.service.is_active():
                raise RuntimeError("service is inactive after readiness")
        except BaseException:
            try:
                self._stop()
            except BaseException:
                pass
            raise

    def recover(self, target: str):
        self.transition(target, operation="recover")

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
        return {"version": 1, "boundary": "b1", "source": phase,
                "source_sha": self.state["source_sha"] if phase == "candidate" else self.state["baseline_sha"],
                "service": service}

    def finalize(self):
        status = self.status()
        if status["source"] != "candidate":
            raise ValueError("finalize requires verified candidate source")
        report = {
            "version": 1, "boundary": "b1", "accepted_source_sha": self.state["source_sha"],
            "accepted_tree_sha": self.state["tree_sha"],
            "release_manifest_sha256": self.state["release_manifest"]["sha256"],
            "baseline_manifest_sha256": self.state["baseline_manifest"]["sha256"],
            "status": "accepted",
        }
        if set(report) != PUBLIC_FINAL_KEYS:
            raise RuntimeError("internal final report schema error")
        return report


def prepare_b1(arguments):
    release_root = _absolute(arguments.release_root, "release root", must_exist=True)
    release_manifest_path = _absolute(arguments.release_manifest, "release manifest", must_exist=True)
    fixture_manifest_path = _absolute(arguments.fixture_manifest, "fixture manifest", must_exist=True)
    destination = _absolute(arguments.destination, "destination", must_exist=True)
    backup = _absolute(arguments.backup_root, "backup root")
    state_path = _absolute(arguments.state, "state")
    interpreter = _absolute(arguments.interpreter, "interpreter", must_exist=True)
    if not release_root.is_dir() or release_root.is_symlink() or not destination.is_dir() or destination.is_symlink():
        raise ValueError("release and destination must be real directories")
    if any(first == second for index, first in enumerate((release_root, destination, backup, state_path.parent))
           for second in (release_root, destination, backup, state_path.parent)[index + 1:]):
        raise ValueError("release, destination, backup, and authority roots must be distinct")
    if SERVICE.fullmatch(arguments.service or "") is None:
        raise ValueError("service name is invalid")
    interpreter_target = interpreter.resolve(strict=True)
    interpreter_info = interpreter_target.lstat()
    if not stat.S_ISREG(interpreter_info.st_mode) or not os.access(interpreter, os.X_OK):
        raise ValueError("interpreter is unsafe")
    release_raw, _ = _read_regular(release_manifest_path)
    fixture_raw, _ = _read_regular(fixture_manifest_path)
    release = _validate_release_manifest(release_raw, release_root)
    fixture = _validate_fixture_manifest(fixture_raw, fixture_manifest_path.parent)
    expected = {
        "baseline_sha": arguments.expected_baseline_source_sha,
        "source_sha": arguments.expected_source_sha,
        "tree_sha": arguments.expected_tree_sha,
    }
    for value in (*expected.values(), arguments.expected_baseline_tree_sha):
        if SHA1.fullmatch(value or "") is None:
            raise ValueError("expected Git identity is invalid")
    if any(release[key] != value for key, value in expected.items()):
        raise ValueError("release manifest expected identity mismatch")
    if (fixture["source_sha"], fixture["tree_sha"], _sha(fixture_raw)) != (
            arguments.expected_source_sha, arguments.expected_tree_sha, release["fixture_manifest_sha256"]):
        raise ValueError("fixture manifest expected identity mismatch")
    if state_path.exists() or state_path.is_symlink():
        raise ValueError("state authority must be fresh")
    if not state_path.parent.exists():
        parent_parent = state_path.parent.parent
        parent_info = parent_parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise ValueError("state ancestry is unsafe")
        state_path.parent.mkdir(mode=0o700)
        _fsync_dir(state_path.parent.parent)
    elif state_path.parent.is_symlink() or not state_path.parent.is_dir():
        raise ValueError("state parent is unsafe")
    if state_path.parent.resolve(strict=True) != state_path.parent:
        raise ValueError("state ancestry is unsafe")
    service = SystemdService(arguments.service)
    if not service.is_active():
        raise ValueError("prepare-b1 requires the baseline service active")
    initial_service = service.status()
    if (type(initial_service) is not dict or initial_service.get("service") != arguments.service
            or type(initial_service.get("n_restarts")) is not int
            or initial_service["n_restarts"] < 0):
        raise ValueError("baseline service evidence is invalid")
    service_baseline = {
        "n_restarts": initial_service["n_restarts"], "warning_since": int(time.time()),
    }
    destination_info = destination.lstat()
    destination_owner = {"uid": destination_info.st_uid, "gid": destination_info.st_gid}
    created_backup = False
    try:
        _mkdir_fresh(backup)
        created_backup = True
        source_backup = backup / "source"
        source_backup.mkdir(mode=0o700)
        baseline_files = []
        for entry in release["files"]:
            relative = entry["path"]
            source = _contained_path(destination, relative)
            try:
                data, metadata = _read_regular(source)
            except FileNotFoundError:
                baseline_files.append({"path": relative, "present": False, "sha256": None,
                                       "size": 0, "mode": None})
            else:
                mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
                if mode not in ("0644", "0755"):
                    raise ValueError("baseline source mode is unsupported")
                if (metadata.st_uid, metadata.st_gid) != (
                        destination_owner["uid"], destination_owner["gid"]):
                    raise ValueError("baseline source ownership mismatches destination authority")
                saved = source_backup / relative
                saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _durable_new_file(saved, data, int(mode, 8))
                baseline_files.append({"path": relative, "present": True, "sha256": _sha(data),
                                       "size": len(data), "mode": mode})
        baseline_manifest = {
            "version": 1, "boundary": "b1", "source_sha": arguments.expected_baseline_source_sha,
            "tree_sha": arguments.expected_baseline_tree_sha, "files": baseline_files,
        }
        baseline_path = backup / "baseline-source-manifest.json"
        _durable_new_file(baseline_path, canonical_json(baseline_manifest), 0o600)
        for path in (source_backup, backup):
            _fsync_dir(path)
        state = {
            "version": 1, "boundary": "b1", "baseline_sha": arguments.expected_baseline_source_sha,
            "baseline_tree_sha": arguments.expected_baseline_tree_sha,
            "source_sha": arguments.expected_source_sha, "tree_sha": arguments.expected_tree_sha,
            "release_root": _directory_authority(release_root), "release_manifest": _manifest_file(release_manifest_path),
            "fixture_manifest": _manifest_file(fixture_manifest_path),
            "baseline_manifest": _manifest_file(baseline_path), "destination": _directory_authority(destination),
            "destination_owner": destination_owner,
            "backup_root": _directory_authority(backup), "service": arguments.service,
            "service_baseline": service_baseline, "interpreter": str(interpreter),
            "state_path": str(state_path), "self": None,
        }
        _publish_state(state_path, state)
        Priority15Transaction.load(state_path, service=service)
        return state
    except BaseException:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        if created_backup:
            shutil.rmtree(backup, ignore_errors=True)
        raise


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-b1")
    for option in ("--repository", "--baseline-commit", "--source-commit",
                   "--expected-reviewed-diff-sha256", "--stage-root", "--python"):
        add_store_once_argument(build, option, required=True)
    prepare = commands.add_parser("prepare-b1")
    for option in ("--release-root", "--release-manifest", "--fixture-manifest",
                   "--expected-baseline-source-sha", "--expected-baseline-tree-sha",
                   "--expected-source-sha", "--expected-tree-sha", "--destination",
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
    if arguments.command == "build-b1":
        print(canonical_json(build_b1(arguments)).decode(), end="")
        return 0
    if arguments.command == "prepare-b1":
        prepare_b1(arguments)
        return 0
    tx = Priority15Transaction.load(Path(arguments.state))
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
        print(f"priority-1.5 transaction failed: {error}", file=sys.stderr)
        raise SystemExit(2)
