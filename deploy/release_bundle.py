"""Fail-closed validation for approved Matrix release manifests."""

from __future__ import annotations

import argparse
import ctypes
import errno
import io
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import NoReturn, Protocol

for _argument_root in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent / "release"):
    if (_argument_root / "release_argparse.py").is_file():
        sys.path.insert(0, str(_argument_root))
        break
from release_argparse import add_store_once_argument

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_MODE_RE = re.compile(r"0[0-7]{3}\Z")
_MANIFEST_KEYS = {"version", "files"}
_FILE_KEYS = {"path", "sha256", "uid", "gid", "mode"}
_BACKUP_ABSENT_KEYS = {"path", "present_before"}
_BACKUP_PRESENT_KEYS = _BACKUP_ABSENT_KEYS | {"sha256", "uid", "gid", "mode"}
_SYSTEMCTL = "/usr/bin/systemctl"
_SYSTEMD_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
_SYSTEMD_CONTROL_BUDGET_SECONDS = 300.0
_SYSTEMD_CALL_TIMEOUT_SECONDS = 15.0
_FORBIDDEN_DIRECTORY_PARTS = {
    "data",
    "secrets",
    "credentials",
    "venv",
    ".venv",
    "__pycache__",
    "cache",
    ".cache",
}


class ReleaseManifestError(ValueError):
    """The approved release manifest or its source tree is not exact."""


class ServiceControl(Protocol):
    def is_active(self) -> bool: ...

    def stop(self) -> None: ...

    def daemon_reload_needed(self) -> bool: ...

    def daemon_reload(self) -> None: ...

    def start(self) -> None: ...

    def run_health_checks(self) -> None: ...

    def recovery_commands(self) -> tuple[str, ...]: ...


def _reject(message: str) -> NoReturn:
    raise ReleaseManifestError(message)


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("duplicate JSON key")
        result[key] = value
    return result


def _release_files(release_root: Path) -> set[str]:
    files: set[str] = set()
    for directory, directory_names, file_names in os.walk(
        release_root, followlinks=False
    ):
        directory_path = Path(directory)
        for name in directory_names:
            if stat.S_ISLNK((directory_path / name).lstat().st_mode):
                _reject("release tree contains a symbolic link")
        for name in file_names:
            path = directory_path / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                _reject("release tree contains a non-regular file")
            files.add(path.relative_to(release_root).as_posix())
    return files


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        _reject("manifest path must be a non-empty string")
    if "\\" in value or PurePosixPath(value).as_posix() != value:
        _reject("manifest path is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        _reject("manifest path must be relative and canonical")
    return value


def validate_release_manifest(
    release_root: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> dict[str, object]:
    """Validate an exact approved manifest against every release source file."""

    root = Path(release_root)
    if not root.is_dir() or root.is_symlink():
        _reject("release root must be a real directory")

    try:
        manifest = json.loads(
            Path(manifest_path).read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("release manifest is unreadable") from error

    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        _reject("release manifest fields are not exact")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        _reject("unsupported release manifest version")
    entries = manifest["files"]
    if not isinstance(entries, list):
        _reject("release manifest files must be a list")

    source_paths = _release_files(root)
    manifest_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
            _reject("release file fields are not exact")
        relative_path = _validate_relative_path(entry["path"])
        if relative_path in manifest_paths:
            _reject("release manifest contains a duplicate path")
        manifest_paths.add(relative_path)

        source_hash = entry["sha256"]
        if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
            _reject("release source hash must be lowercase SHA-256")
        for field in ("uid", "gid"):
            value = entry[field]
            if type(value) is not int or value < 0:
                _reject(f"intended destination {field} must be a non-negative integer")
        intended_mode = entry["mode"]
        if not isinstance(intended_mode, str) or not _MODE_RE.fullmatch(
            intended_mode
        ):
            _reject("intended destination mode must be four-digit octal")

        source_path = root.joinpath(*PurePosixPath(relative_path).parts)
        if relative_path not in source_paths:
            _reject("manifest path has no regular release source")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if digest != source_hash:
            _reject("release source hash does not match approved manifest")

    if manifest_paths != source_paths:
        _reject("release manifest does not contain every release source file")
    return manifest


def verify_staged_release(
    staged_tool: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> dict[str, object]:
    """Verify the staged verifier and exact release tree before any action."""

    manifest = validate_release_manifest(release_root, manifest_path)
    entries = manifest["files"]
    if not isinstance(entries, list):
        _reject("release manifest is not validated")
    tool_entry = next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("path") == "deploy/release_bundle.py"
        ),
        None,
    )
    if tool_entry is None:
        _reject("release manifest does not protect the staged release tool")

    tool_path = Path(staged_tool)
    try:
        tool_stat = tool_path.lstat()
    except OSError as error:
        raise ReleaseManifestError("staged release tool is unreadable") from error
    if not stat.S_ISREG(tool_stat.st_mode) or stat.S_ISLNK(tool_stat.st_mode):
        _reject("staged release tool must be a regular file")
    try:
        descriptor = os.open(
            tool_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise ReleaseManifestError("staged release tool is unreadable") from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            _reject("staged release tool must be a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)

    if digest.hexdigest() != tool_entry["sha256"]:
        _reject("staged release tool hash does not match approved manifest")
    return manifest


def _read_prior_destination(
    destination_root: Path | int, relative_path: str
) -> tuple[bytes, os.stat_result] | None:
    return _read_confined_regular(destination_root, relative_path, missing_ok=True)


def _open_absolute_directory(path: Path) -> int:
    """Open an absolute directory one no-follow component at a time."""

    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_fresh_directory(path: Path, uid: int, gid: int) -> int:
    """Create and return a fresh 0700 directory without following parents."""

    if type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0:
        _reject("bundle owner must be non-negative integers")
    try:
        parent_fd = _open_absolute_directory(path.parent)
    except OSError as error:
        raise ReleaseManifestError("bundle parent is unsafe") from error
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as error:
            raise ReleaseManifestError("bundle must be fresh") from error
        bundle_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(bundle_fd, 0o700)
            os.fchown(bundle_fd, uid, gid)
            os.fsync(bundle_fd)
            os.fsync(parent_fd)
            return bundle_fd
        except BaseException:
            os.close(bundle_fd)
            raise
    except OSError as error:
        raise ReleaseManifestError("fresh bundle creation failed") from error
    finally:
        os.close(parent_fd)


def _open_or_create_confined_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _publish_new_confined(
    root_fd: int, relative_path: str, content: bytes, *, mode: int = 0o400
) -> None:
    """Publish a new file atomically below an already pinned directory."""

    parts = PurePosixPath(_validate_relative_path(relative_path)).parts
    directory_fd = _open_or_create_confined_directory(root_fd, parts[:-1])
    temporary_name = f".release-bundle-{os.getpid()}-{secrets.token_hex(8)}"
    file_fd: int | None = None
    published = False
    try:
        try:
            os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _reject("bundle publication target already exists")
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(content)
        while view:
            count = os.write(file_fd, view)
            if count <= 0:
                raise OSError("short bundle write")
            view = view[count:]
        os.fchmod(file_fd, mode)
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(
            temporary_name, parts[-1],
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        published = True
        os.fsync(directory_fd)
    except OSError as error:
        raise ReleaseManifestError("confined bundle publication failed") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _publish_new_path(path: Path, content: bytes, *, mode: int = 0o400) -> None:
    try:
        parent_fd = _open_absolute_directory(path.parent)
    except OSError as error:
        raise ReleaseManifestError("publication parent is unsafe") from error
    try:
        _publish_new_confined(parent_fd, path.name, content, mode=mode)
    finally:
        os.close(parent_fd)


def _read_regular_path(path: Path) -> bytes:
    data = _read_confined_regular(path.parent, path.name, missing_ok=False)
    if data is None:
        _reject("required file is missing")
    return data[0]


def _require_fresh_path(path: Path, label: str) -> None:
    try:
        parent_fd = _open_absolute_directory(path.parent)
    except OSError as error:
        raise ReleaseManifestError(f"{label} parent is unsafe") from error
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        _reject(f"{label} must be fresh")
    finally:
        os.close(parent_fd)


def prepare_acceptance_manifest(
    release_root: str | os.PathLike[str], source_sha: str,
    candidate_tracked_files: list[str],
) -> dict[str, object]:
    """Publish the installed verifier manifest before the outer release spec is built."""
    root = Path(release_root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        _reject("acceptance release root must be a real absolute directory")
    if not isinstance(source_sha, str) or not _SOURCE_SHA_RE.fullmatch(source_sha):
        _reject("acceptance source SHA is invalid")
    marker = root / ".matrix-release.json"
    _require_fresh_path(marker, "installed acceptance manifest")
    source_paths = _release_files(root)
    if (
        type(candidate_tracked_files) is not list
        or any(type(path) is not str for path in candidate_tracked_files)
        or candidate_tracked_files != sorted(source_paths)
    ):
        _reject("candidate tracked files do not match the complete release")
    required = {"webapp.py", "scripts/verify_priority_1_3_remote.py"}
    if not required.issubset(source_paths):
        _reject("acceptance verifier topology is incomplete")
    verifier = root / "scripts/verify_priority_1_3_remote.py"
    if not stat.S_IMODE(verifier.lstat().st_mode) & 0o111:
        _reject("remote acceptance verifier is not executable")
    entries: list[dict[str, str]] = []
    for relative in sorted(source_paths):
        data = _read_regular_path(root.joinpath(*PurePosixPath(relative).parts))
        entries.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    manifest: dict[str, object] = {
        "schema": 1,
        "source_sha": source_sha,
        "candidate_tracked_files": candidate_tracked_files,
        "files": entries,
    }
    _publish_new_path(marker, _canonical_json(manifest), mode=0o400)
    return manifest


def create_backup_manifest(
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest: dict[str, object],
    backup_manifest_path: str | os.PathLike[str],
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> dict[str, object]:
    """Archive destination bytes scoped exactly by a validated release manifest."""

    root = Path(destination_root)
    if not root.is_dir() or root.is_symlink():
        _reject("destination root must be a real directory")
    if not isinstance(release_manifest, dict):
        _reject("release manifest is not validated")
    entries = release_manifest.get("files")
    if (
        set(release_manifest) != _MANIFEST_KEYS
        or release_manifest.get("version") != 1
        or not isinstance(entries, list)
    ):
        _reject("release manifest is not validated")
    try:
        root_fd = _open_absolute_directory(root)
    except OSError as error:
        raise ReleaseManifestError("destination root must be a real directory") from error

    try:
        records: list[dict[str, object]] = []
        archives: list[tuple[str, bytes]] = []
        seen_paths: set[str] = set()
        for release_entry in entries:
            if not isinstance(release_entry, dict) or set(release_entry) != _FILE_KEYS:
                _reject("release manifest is not validated")
            relative_path = _validate_relative_path(release_entry["path"])
            if relative_path in seen_paths:
                _reject("release manifest contains a duplicate path")
            seen_paths.add(relative_path)
            prior = _read_prior_destination(root_fd, relative_path)
            if prior is None:
                records.append({"path": relative_path, "present_before": False})
                continue

            prior_bytes, prior_stat = prior
            records.append(
                {
                    "path": relative_path,
                    "present_before": True,
                    "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                    "uid": prior_stat.st_uid,
                    "gid": prior_stat.st_gid,
                    "mode": f"{stat.S_IMODE(prior_stat.st_mode):04o}",
                }
            )
            archives.append((relative_path, prior_bytes))

        backup_manifest: dict[str, object] = {"version": 1, "files": records}
        bundle_path = Path(bundle).absolute()
        manifest_path = Path(backup_manifest_path)
        try:
            manifest_relative = manifest_path.absolute().relative_to(bundle_path).as_posix()
        except ValueError:
            _reject("backup manifest must be inside the bundle")
        if manifest_relative != "backup-manifest.json":
            _reject("backup manifest location is not exact")
        bundle_fd = _create_fresh_directory(
            bundle_path,
            os.geteuid() if owner_uid is None else owner_uid,
            os.getegid() if owner_gid is None else owner_gid,
        )
        try:
            for relative_path, prior_bytes in archives:
                _publish_new_confined(
                    bundle_fd, f"archive/{relative_path}", prior_bytes, mode=0o400
                )
            encoded = (
                json.dumps(backup_manifest, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            _publish_new_confined(bundle_fd, manifest_relative, encoded, mode=0o400)
            os.fsync(bundle_fd)
        finally:
            os.close(bundle_fd)
        return backup_manifest
    finally:
        os.close(root_fd)


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def backup_release(
    staged_tool: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    release_spec_path: str | os.PathLike[str],
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> dict[str, object]:
    """Create one fresh backup bundle and its immutable provenance."""

    _require_fresh_path(Path(bundle), "bundle")
    _require_fresh_path(Path(release_manifest_path), "release manifest")
    spec_bytes = _read_regular_path(Path(release_spec_path))
    release_manifest = validate_release_manifest(release_root, release_spec_path)
    try:
        pinned_spec = json.loads(
            spec_bytes.decode("utf-8"), object_pairs_hook=_json_object
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("release spec is unreadable") from error
    if pinned_spec != release_manifest:
        _reject("release spec changed during backup validation")
    for entry in release_manifest["files"]:
        if not isinstance(entry, dict):
            _reject("release spec is invalid")
        _reject_forbidden_release_path(_validate_relative_path(entry["path"]))
    verify_staged_release(staged_tool, release_root, release_spec_path)
    canonical_manifest = _canonical_json(release_manifest)
    _publish_new_path(Path(release_manifest_path), canonical_manifest)
    result = create_backup_manifest(
        destination_root,
        bundle,
        release_manifest,
        backup_manifest_path,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    bundle_fd = _open_absolute_directory(Path(bundle))
    try:
        _publish_new_confined(bundle_fd, "release-spec.json", spec_bytes)
        _publish_new_confined(bundle_fd, "release-manifest.json", canonical_manifest)
        tool_data = _read_confined_regular(
            Path(staged_tool).parent, Path(staged_tool).name, missing_ok=False
        )
        if tool_data is None:
            _reject("staged tool is missing")
        tool_hash = hashlib.sha256(tool_data[0]).hexdigest().encode() + b"\n"
        _publish_new_confined(bundle_fd, "staged-tool.sha256", tool_hash)
        os.fsync(bundle_fd)
    finally:
        os.close(bundle_fd)
    return result


def _read_confined_regular(
    root: Path | int, relative_path: str, *, missing_ok: bool
) -> tuple[bytes, os.stat_result] | None:
    """Read a regular file without following any path component symlink."""

    relative_path = _validate_relative_path(relative_path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        if isinstance(root, int):
            directory_fd = os.dup(root)
        else:
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                _reject("confined root must be a real directory")
            directory_fd = os.open(root, directory_flags)
    except OSError as error:
        raise ReleaseManifestError("confined root is unsafe") from error
    try:
        parts = PurePosixPath(relative_path).parts
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise ReleaseManifestError("required confined path is missing")
            except OSError as error:
                raise ReleaseManifestError(
                    "confined path traversal is unsafe"
                ) from error
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ReleaseManifestError("required confined file is missing")
        except OSError as error:
            raise ReleaseManifestError("confined file is unsafe") from error
        try:
            opened_stat = os.fstat(file_fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                _reject("confined content must be a regular file")
            chunks = []
            while chunk := os.read(file_fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks), opened_stat
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _reject_forbidden_release_path(relative_path: str) -> None:
    path = PurePosixPath(relative_path)
    lower_parts = tuple(part.lower() for part in path.parts)
    lower_path = "/".join(lower_parts)
    name = lower_parts[-1]
    stem = PurePosixPath(name).stem
    if any(part in _FORBIDDEN_DIRECTORY_PARTS for part in lower_parts):
        _reject("release path targets mutable state, secrets, venv, or cache")
    if lower_path.startswith("resources/media/"):
        _reject("release path targets user media")
    if (
        name == ".env"
        or name.endswith((".key", ".pem"))
        or "credential" in name
        or "secret" in name
    ):
        _reject("release path targets secrets or credentials")
    if "paired_device" in name or "paired-device" in name:
        _reject("release path targets the paired-device registry")
    if (
        name == "config.json"
        or stem in {"state", "revision"}
        or "runtime-state" in name
    ):
        _reject("release path targets mutable configuration or revision state")
    if name.endswith((".service", ".socket", ".timer")):
        _reject("release path targets a systemd unit")


def _load_backup_manifest(
    bundle: Path,
    backup_manifest_path: str | os.PathLike[str],
) -> dict[str, object]:
    manifest_path = Path(backup_manifest_path)
    try:
        relative_path = (
            manifest_path.absolute().relative_to(bundle.absolute()).as_posix()
        )
    except ValueError:
        _reject("backup manifest must be inside the bundle")
    manifest_data = _read_confined_regular(bundle, relative_path, missing_ok=False)
    if manifest_data is None:
        _reject("backup manifest is missing")
    try:
        manifest = json.loads(
            manifest_data[0].decode("utf-8"), object_pairs_hook=_json_object
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("backup manifest is unreadable") from error
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        _reject("backup manifest fields are not exact")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        _reject("unsupported backup manifest version")
    if not isinstance(manifest["files"], list):
        _reject("backup manifest files must be a list")
    return manifest


def preflight_release(
    staged_tool: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    *,
    destination_fd: int | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Read-only, fail-closed verification before any release action."""

    release_manifest = verify_staged_release(
        staged_tool, release_root, release_manifest_path
    )
    release_entries = release_manifest["files"]
    if not isinstance(release_entries, list):
        _reject("release manifest is not validated")
    for entry in release_entries:
        if not isinstance(entry, dict):
            _reject("release manifest is not validated")
        _reject_forbidden_release_path(_validate_relative_path(entry.get("path")))

    destination: Path | int = (
        destination_fd if destination_fd is not None else Path(destination_root)
    )
    if destination_fd is None and (
        not destination.is_dir() or destination.is_symlink()
    ):
        _reject("destination root must be a real directory")
    bundle_path = Path(bundle)
    if not bundle_path.is_dir() or bundle_path.is_symlink():
        _reject("bundle must be a real directory")
    backup_manifest = _load_backup_manifest(bundle_path, backup_manifest_path)
    backup_entries = backup_manifest["files"]
    if not isinstance(backup_entries, list) or len(backup_entries) != len(
        release_entries
    ):
        _reject("backup manifest scope does not match release manifest")

    archived_paths: set[str] = set()
    for release_entry, backup_entry in zip(
        release_entries, backup_entries, strict=True
    ):
        if not isinstance(release_entry, dict) or not isinstance(backup_entry, dict):
            _reject("backup file record is invalid")
        relative_path = _validate_relative_path(backup_entry.get("path"))
        if relative_path != release_entry.get("path"):
            _reject("backup manifest scope does not match release manifest")
        present = backup_entry.get("present_before")
        if type(present) is not bool:
            _reject("backup present_before must be Boolean")

        current = _read_confined_regular(destination, relative_path, missing_ok=True)
        if not present:
            if set(backup_entry) != _BACKUP_ABSENT_KEYS:
                _reject("absent backup record must not contain prior metadata")
            if current is not None:
                _reject("absent-before destination appeared after backup")
            archive = _read_confined_regular(
                bundle_path, f"archive/{relative_path}", missing_ok=True
            )
            if archive is not None:
                _reject("absent-before record must not have an archive")
            continue

        if set(backup_entry) != _BACKUP_PRESENT_KEYS:
            _reject("present backup record metadata is not exact")
        prior_hash = backup_entry["sha256"]
        prior_mode = backup_entry["mode"]
        if not isinstance(prior_hash, str) or not _SHA256_RE.fullmatch(prior_hash):
            _reject("backup hash must be lowercase SHA-256")
        if not isinstance(prior_mode, str) or not _MODE_RE.fullmatch(prior_mode):
            _reject("backup mode must be four-digit octal")
        for field in ("uid", "gid"):
            if type(backup_entry[field]) is not int or backup_entry[field] < 0:
                _reject(f"backup {field} must be a non-negative integer")
        archive = _read_confined_regular(
            bundle_path, f"archive/{relative_path}", missing_ok=False
        )
        if archive is None or hashlib.sha256(archive[0]).hexdigest() != prior_hash:
            _reject("archived prior bytes do not match backup manifest")
        archived_paths.add(relative_path)
        if current is None:
            _reject("present-before destination disappeared after backup")
        current_bytes, current_stat = current
        if (
            hashlib.sha256(current_bytes).hexdigest() != prior_hash
            or current_stat.st_uid != backup_entry["uid"]
            or current_stat.st_gid != backup_entry["gid"]
            or f"{stat.S_IMODE(current_stat.st_mode):04o}" != prior_mode
        ):
            _reject("destination no longer matches recorded pre-backup state")

    archive_root = bundle_path / "archive"
    if archive_root.exists():
        if archive_root.is_symlink() or not archive_root.is_dir():
            _reject("archive root must be a real directory")
        if _release_files(archive_root) != archived_paths:
            _reject("archive scope does not match present-before records")
    elif archived_paths:
        _reject("required backup archive is missing")
    _validate_backup_for_rollback(release_manifest, backup_manifest, bundle_path)
    return release_manifest, backup_manifest


def _open_confined_parent(
    root: Path | int, relative_path: str, *, create: bool = False
) -> tuple[int, str]:
    """Open a destination parent without following symlinks."""

    parts = PurePosixPath(_validate_relative_path(relative_path)).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.dup(root) if isinstance(root, int) else os.open(root, flags)
    except OSError as error:
        raise ReleaseManifestError("destination root is unsafe") from error
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except OSError as error:
                raise ReleaseManifestError(
                    "destination parent traversal is unsafe"
                ) from error
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd, parts[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def _atomic_replace_confined(
    root: Path | int,
    relative_path: str,
    content: bytes,
    uid: int,
    gid: int,
    mode: str,
    *,
    on_publish: Callable[[], None] | None = None,
) -> None:
    """Durably replace one confined regular file with exact metadata."""

    directory_fd, name = _open_confined_parent(root, relative_path, create=True)
    temporary_name = f".release-bundle-{os.getpid()}-{secrets.token_hex(8)}"
    temporary_fd: int | None = None
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short write while preparing release file")
            view = view[written:]
        os.fchmod(temporary_fd, int(mode, 8))
        os.fchown(temporary_fd, uid, gid)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        if on_publish is not None:
            on_publish()
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _remove_confined(
    root: Path | int,
    relative_path: str,
    *,
    on_remove: Callable[[], None] | None = None,
) -> None:
    directory_fd, name = _open_confined_parent(root, relative_path)
    try:
        try:
            target_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(target_stat.st_mode):
            _reject("rollback target is not a regular file")
        os.unlink(name, dir_fd=directory_fd)
        if on_remove is not None:
            on_remove()
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _verify_backup_record(
    destination_root: Path | int,
    bundle: Path,
    backup_entry: dict[str, object],
) -> None:
    relative_path = _validate_relative_path(backup_entry["path"])
    restored = _read_confined_regular(destination_root, relative_path, missing_ok=True)
    if not backup_entry["present_before"]:
        if restored is not None:
            _reject("absent-before path remains after rollback")
        return
    if restored is None:
        _reject("present-before path is missing after rollback")
    restored_bytes, restored_stat = restored
    if (
        hashlib.sha256(restored_bytes).hexdigest() != backup_entry["sha256"]
        or restored_stat.st_uid != backup_entry["uid"]
        or restored_stat.st_gid != backup_entry["gid"]
        or f"{stat.S_IMODE(restored_stat.st_mode):04o}" != backup_entry["mode"]
    ):
        _reject("rollback verification failed")
    archive = _read_confined_regular(
        bundle, f"archive/{relative_path}", missing_ok=False
    )
    if archive is None or restored_bytes != archive[0]:
        _reject("rollback bytes do not match archived prior content")


def _assert_recorded_prebackup_state(
    destination: Path | int, bundle: Path, backup_entry: dict[str, object]
) -> None:
    relative_path = _validate_relative_path(backup_entry["path"])
    current = _read_confined_regular(destination, relative_path, missing_ok=True)
    if backup_entry["present_before"] is False:
        if current is not None:
            _reject("destination changed from recorded pre-backup state")
        return
    if current is None:
        _reject("destination changed from recorded pre-backup state")
    content, metadata = current
    if (
        hashlib.sha256(content).hexdigest() != backup_entry["sha256"]
        or metadata.st_uid != backup_entry["uid"]
        or metadata.st_gid != backup_entry["gid"]
        or f"{stat.S_IMODE(metadata.st_mode):04o}" != backup_entry["mode"]
    ):
        _reject("destination changed from recorded pre-backup state")
    archive = _read_confined_regular(
        bundle, f"archive/{relative_path}", missing_ok=False
    )
    if archive is None or archive[0] != content:
        _reject("backup archive changed")


def _snapshot_backup_records(
    bundle: Path, backup_entries: list[object]
) -> tuple[tuple[str, bool, bytes | None, int | None, int | None, str | None], ...]:
    """Retain immutable recovery authority before any service callback."""

    records = []
    for backup_entry in backup_entries:
        if not isinstance(backup_entry, dict):
            _reject("backup manifest is not validated")
        relative_path = _validate_relative_path(backup_entry["path"])
        if backup_entry["present_before"]:
            archive = _read_confined_regular(
                bundle, f"archive/{relative_path}", missing_ok=False
            )
            if archive is None or hashlib.sha256(archive[0]).hexdigest() != backup_entry["sha256"]:
                _reject("required rollback archive changed")
            records.append((
                relative_path, True, archive[0], backup_entry["uid"],
                backup_entry["gid"], backup_entry["mode"],
            ))
        else:
            records.append((relative_path, False, None, None, None, None))
    return tuple(records)


def _restore_backup_indices(
    destination: Path | int,
    backup_records: tuple[
        tuple[str, bool, bytes | None, int | None, int | None, str | None], ...
    ],
    indices: list[int],
) -> list[str]:
    diagnostics: list[str] = []
    for index in reversed(indices):
        try:
            relative_path, present, content, uid, gid, mode = backup_records[index]
            if present:
                if content is None or uid is None or gid is None or mode is None:
                    _reject("immutable backup snapshot is invalid")
                _atomic_replace_confined(
                    destination, relative_path, content, uid, gid, mode,
                )
            else:
                _remove_confined(destination, relative_path)
            restored = _read_confined_regular(destination, relative_path, missing_ok=True)
            if present:
                if restored is None:
                    _reject("immutable backup restoration is missing")
                restored_bytes, restored_stat = restored
                if (
                    restored_bytes != content
                    or restored_stat.st_uid != uid
                    or restored_stat.st_gid != gid
                    or f"{stat.S_IMODE(restored_stat.st_mode):04o}" != mode
                ):
                    _reject("immutable backup restoration failed")
            elif restored is not None:
                _reject("absent-before path remains after recovery")
        except BaseException as error:
            diagnostics.append(f"restore {index}: {error}")
    return diagnostics


def _recover_initial_service_state(
    service: ServiceControl, initially_active: bool
) -> list[str]:
    diagnostics: list[str] = []
    try:
        if service.daemon_reload_needed():
            service.daemon_reload()
    except BaseException as error:
        diagnostics.append(f"daemon-reload: {error}")

    try:
        currently_active = bool(service.is_active())
    except BaseException as error:
        diagnostics.append(f"is-active: {error}")
        return diagnostics

    if not initially_active:
        if currently_active:
            try:
                service.stop()
            except BaseException as error:
                diagnostics.append(f"stop: {error}")
        return diagnostics

    if not currently_active:
        try:
            service.start()
        except BaseException as error:
            diagnostics.append(f"start: {error}")
            return diagnostics
    try:
        health = getattr(service, "run_health_checks", None)
        if health is not None:
            health()
    except BaseException as error:
        diagnostics.append(f"health: {error}")
    return diagnostics


def _prove_service_stopped_for_file_recovery(service: ServiceControl) -> list[str]:
    """Return diagnostics unless inactive state is positively established."""

    diagnostics: list[str] = []
    try:
        active = bool(service.is_active())
    except BaseException as error:
        return [f"is-active before file recovery: {error}"]
    if active:
        try:
            service.stop()
        except BaseException as error:
            return [f"stop before file recovery: {error}"]
    try:
        if service.is_active():
            diagnostics.append("stop before file recovery: service remains active")
    except BaseException as error:
        diagnostics.append(f"is-active after stop before file recovery: {error}")
    return diagnostics


def _raise_with_recovery(
    primary: BaseException, diagnostics: list[str], service: ServiceControl
) -> NoReturn:
    if not diagnostics:
        raise primary.with_traceback(primary.__traceback__)
    commands_method = getattr(service, "recovery_commands", None)
    try:
        commands = tuple(commands_method()) if commands_method else ()
    except BaseException:
        commands = ()
    instructions = "; then run: ".join(commands) or "restore files and service state"
    raise ReleaseManifestError(
        f"{primary}; recovery diagnostics: {'; '.join(diagnostics)}; "
        f"service recovery failed; run: {instructions}"
    ) from primary


def _assert_pinned_root_identity(path: Path, descriptor: int) -> None:
    """Fail closed unless the trusted pathname still names the pinned root."""

    try:
        path_stat = path.stat(follow_symlinks=False)
        pinned_stat = os.fstat(descriptor)
    except OSError as error:
        raise ReleaseManifestError("destination root identity is unavailable") from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino)
        != (pinned_stat.st_dev, pinned_stat.st_ino)
    ):
        _reject("destination root identity changed during transaction")


def deploy_release(
    staged_tool: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    *,
    service: ServiceControl,
    after_replace: Callable[[str], None] | None = None,
) -> None:
    """Deploy transactionally through one pinned destination-root descriptor."""

    destination_path = Path(destination_root)
    try:
        destination_fd = _open_absolute_directory(destination_path)
    except OSError as error:
        raise ReleaseManifestError("destination root is unsafe") from error
    try:
        _assert_pinned_root_identity(destination_path, destination_fd)
        release_manifest, backup_manifest = preflight_release(
            staged_tool, release_root, destination_path, bundle,
            release_manifest_path, backup_manifest_path,
            destination_fd=destination_fd,
        )
        release_entries = release_manifest["files"]
        backup_entries = backup_manifest["files"]
        if not isinstance(release_entries, list) or not isinstance(backup_entries, list):
            _reject("validated manifest scope is unavailable")
        release = Path(release_root)
        bundle_path = Path(bundle)
        backup_records = _snapshot_backup_records(bundle_path, backup_entries)
        approved_sources: dict[str, bytes] = {}
        for entry in release_entries:
            if not isinstance(entry, dict):
                _reject("release manifest is not validated")
            relative_path = _validate_relative_path(entry["path"])
            source = _read_confined_regular(release, relative_path, missing_ok=False)
            if source is None or hashlib.sha256(source[0]).hexdigest() != entry["sha256"]:
                _reject("release source changed after preflight")
            approved_sources[relative_path] = source[0]
        initially_active = bool(service.is_active())
        touched: list[int] = []
        try:
            if initially_active:
                service.stop()
            _assert_pinned_root_identity(destination_path, destination_fd)
            # Stop can run arbitrary hooks; close the preflight-to-write window.
            for backup_entry in backup_entries:
                if not isinstance(backup_entry, dict):
                    _reject("backup manifest is not validated")
                _assert_recorded_prebackup_state(destination_fd, bundle_path, backup_entry)
            for index, (entry, backup_entry) in enumerate(
                zip(release_entries, backup_entries, strict=True)
            ):
                if not isinstance(entry, dict) or not isinstance(backup_entry, dict):
                    _reject("manifest is not validated")
                relative_path = _validate_relative_path(entry["path"])
                _assert_pinned_root_identity(destination_path, destination_fd)
                _assert_recorded_prebackup_state(destination_fd, bundle_path, backup_entry)
                source_bytes = approved_sources[relative_path]

                def published(index: int = index) -> None:
                    if index not in touched:
                        touched.append(index)

                _atomic_replace_confined(
                    destination_fd, relative_path, source_bytes, entry["uid"], entry["gid"],
                    entry["mode"], on_publish=published,
                )
                if after_replace is not None:
                    after_replace(relative_path)
            _verify_deployed_manifest(destination_fd, release_manifest)
            if service.daemon_reload_needed():
                service.daemon_reload()
                _assert_pinned_root_identity(destination_path, destination_fd)
            if initially_active:
                service.start()
                _assert_pinned_root_identity(destination_path, destination_fd)
                health = getattr(service, "run_health_checks", None)
                if health is not None:
                    health()
                    _assert_pinned_root_identity(destination_path, destination_fd)
            # Service callbacks remain inside the transaction and are untrusted.
            _verify_deployed_manifest(destination_fd, release_manifest)
            _assert_approved_release_authority(
                release, Path(release_manifest_path), bundle_path,
                release_manifest, approved_sources,
            )
        except BaseException as primary:
            diagnostics = _prove_service_stopped_for_file_recovery(service)
            if not diagnostics:
                diagnostics.extend(_restore_backup_indices(
                    destination_fd, backup_records, touched
                ))
            diagnostics.extend(_recover_initial_service_state(service, initially_active))
            _raise_with_recovery(primary, diagnostics, service)
    finally:
        os.close(destination_fd)


def _dry_run_lines(
    release_manifest: dict[str, object], backup_manifest: dict[str, object]
) -> list[str]:
    """Return deterministic, manifest-ordered preflight/action output."""

    release_entries = release_manifest.get("files")
    backup_entries = backup_manifest.get("files")
    if not isinstance(release_entries, list) or not isinstance(backup_entries, list):
        _reject("validated manifest scope is unavailable")
    lines: list[str] = []
    for release_entry, backup_entry in zip(
        release_entries, backup_entries, strict=True
    ):
        if not isinstance(release_entry, dict) or not isinstance(backup_entry, dict):
            _reject("validated manifest record is unavailable")
        relative_path = _validate_relative_path(release_entry.get("path"))
        present = backup_entry.get("present_before")
        if type(present) is not bool or backup_entry.get("path") != relative_path:
            _reject("validated manifest scopes do not match")
        preflight = (
            f"PREFLIGHT path={relative_path} "
            f"source_sha256={release_entry['sha256']} "
            f"present_before={'true' if present else 'false'}"
        )
        if present:
            preflight += f" backup_sha256={backup_entry['sha256']}"
        lines.append(preflight)
        lines.append(
            f"DEPLOY path={relative_path} sha256={release_entry['sha256']} "
            f"uid={release_entry['uid']} gid={release_entry['gid']} "
            f"mode={release_entry['mode']}"
        )
        if present:
            lines.append(
                f"ROLLBACK path={relative_path} action=restore "
                f"present_before=true sha256={backup_entry['sha256']} "
                f"uid={backup_entry['uid']} gid={backup_entry['gid']} "
                f"mode={backup_entry['mode']}"
            )
        else:
            lines.append(
                f"ROLLBACK path={relative_path} action=delete present_before=false"
            )
    return lines


class _SystemdServiceControl:
    def __init__(
        self,
        name: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.name = _validate_service_name(name)
        self._health: tuple[str, int, int] | None = None
        self._monotonic = monotonic or time.monotonic
        self._deadline = (
            self._monotonic() + _SYSTEMD_CONTROL_BUDGET_SECONDS
            if deadline is None else float(deadline)
        )

    def configure_health(self, url: str, status: int, timeout_seconds: int) -> None:
        if (url, status, timeout_seconds) != ("http://127.0.0.1:5000/", 302, 30):
            _reject("recovery health configuration is not exact")
        self._health = (url, status, timeout_seconds)

    def _remaining_timeout(self) -> float:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            _reject("service control deadline expired")
        return min(_SYSTEMD_CALL_TIMEOUT_SECONDS, remaining)

    def _invoke(
        self, arguments: tuple[str, ...], *, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (_SYSTEMCTL, *arguments),
            check=check,
            text=True,
            capture_output=True,
            timeout=self._remaining_timeout(),
            shell=False,
            env=dict(_SYSTEMD_ENV),
        )

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._invoke((*arguments, self.name), check=check)

    def is_active(self) -> bool:
        result = self._run("is-active", "--quiet", check=False)
        if result.returncode not in (0, 3):
            _reject("service active-state check failed")
        return result.returncode == 0

    def stop(self) -> None:
        self._run("stop")

    def daemon_reload_needed(self) -> bool:
        return True

    def daemon_reload(self) -> None:
        self._invoke(("daemon-reload",))

    def start(self) -> None:
        self._run("start")

    def run_health_checks(self) -> None:
        if self._run("is-active", "--quiet", check=False).returncode != 0:
            _reject("service health check failed")
        if self._health is None:
            return
        url, expected_status, timeout_seconds = self._health
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 5000:
            _reject("recovery health URL is not exact loopback HTTP")
        deadline = min(self._deadline, self._monotonic() + timeout_seconds)
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                _reject("application readiness deadline expired")
            connection = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=min(2.0, remaining)
            )
            try:
                connection.request("GET", parsed.path or "/")
                response = connection.getresponse()
                response.read()
                if response.status == expected_status:
                    return
            except OSError:
                pass
            finally:
                connection.close()
            time.sleep(min(0.1, max(0.0, deadline - self._monotonic())))

    def recovery_commands(self) -> tuple[str, ...]:
        return (
            f"{_SYSTEMCTL} start {self.name}",
            f"{_SYSTEMCTL} is-active --quiet {self.name}",
        )


class _NoServiceControl:
    def is_active(self) -> bool:
        return False

    def stop(self) -> None:
        return None

    def daemon_reload_needed(self) -> bool:
        return False

    def daemon_reload(self) -> None:
        return None

    def start(self) -> None:
        return None

    def run_health_checks(self) -> None:
        return None

    def recovery_commands(self) -> tuple[str, ...]:
        return ()


def _service_control(name: str | None) -> ServiceControl:
    return _SystemdServiceControl(name) if name is not None else _NoServiceControl()


def _validate_service_name(name: str) -> str:
    if not name or name.startswith("-") or any(
        character.isspace() for character in name
    ):
        _reject("service name is invalid")
    return name


def _verify_deployed(
    release_root: Path,
    destination_root: Path | int,
    release_manifest_path: Path,
) -> dict[str, object]:
    manifest = validate_release_manifest(release_root, release_manifest_path)
    _verify_deployed_manifest(destination_root, manifest)
    return manifest


def _verify_deployed_manifest(
    destination_root: Path | int, manifest: dict[str, object]
) -> None:
    """Verify destination against an already-approved manifest authority."""

    entries = manifest.get("files")
    if not isinstance(entries, list):
        _reject("release manifest is not validated")
    for entry in entries:
        if not isinstance(entry, dict):
            _reject("release manifest is not validated")
        relative_path = _validate_relative_path(entry.get("path"))
        _reject_forbidden_release_path(relative_path)
        deployed = _read_confined_regular(destination_root, relative_path, missing_ok=False)
        if deployed is None:
            _reject("deployed release file is missing")
        content, metadata = deployed
        if (
            hashlib.sha256(content).hexdigest() != entry["sha256"]
            or metadata.st_uid != entry["uid"]
            or metadata.st_gid != entry["gid"]
            or f"{stat.S_IMODE(metadata.st_mode):04o}" != entry["mode"]
        ):
            _reject("deployed release does not match intended bytes and metadata")


def _assert_approved_release_authority(
    release_root: Path,
    release_manifest_path: Path,
    bundle: Path,
    approved_manifest: dict[str, object],
    approved_sources: dict[str, bytes],
) -> None:
    """Reject mutable source or provenance substitution inside the transaction."""

    current_manifest = validate_release_manifest(release_root, release_manifest_path)
    if _canonical_json(current_manifest) != _canonical_json(approved_manifest):
        _reject("approved release manifest changed during transaction")
    for relative_path, approved_bytes in approved_sources.items():
        current = _read_confined_regular(release_root, relative_path, missing_ok=False)
        if current is None or current[0] != approved_bytes:
            _reject("approved release source changed during transaction")
    stored_manifest = _read_confined_regular(
        bundle, "release-manifest.json", missing_ok=False
    )
    stored_spec = _read_confined_regular(bundle, "release-spec.json", missing_ok=False)
    if stored_manifest is None or stored_manifest[0] != _canonical_json(approved_manifest):
        _reject("approved release provenance changed during transaction")
    try:
        spec_object = (
            json.loads(stored_spec[0].decode("utf-8"), object_pairs_hook=_json_object)
            if stored_spec is not None else None
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError(
            "approved release provenance changed during transaction"
        ) from error
    if spec_object != approved_manifest:
        _reject("approved release provenance changed during transaction")


def _snapshot_backup_manifest(
    destination_root: Path, release_manifest: dict[str, object]
) -> dict[str, object]:
    entries = release_manifest.get("files")
    if not isinstance(entries, list):
        _reject("release manifest is not validated")
    records: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            _reject("release manifest is not validated")
        relative_path = _validate_relative_path(entry.get("path"))
        prior = _read_prior_destination(destination_root, relative_path)
        if prior is None:
            records.append({"path": relative_path, "present_before": False})
        else:
            content, metadata = prior
            records.append({
                "path": relative_path, "present_before": True,
                "sha256": hashlib.sha256(content).hexdigest(),
                "uid": metadata.st_uid, "gid": metadata.st_gid,
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            })
    return {"version": 1, "files": records}


def _verify_bundle_provenance(
    staged_tool: Path,
    release_root: Path,
    bundle: Path,
    release_manifest_path: Path,
    release_spec_path: Path | None = None,
) -> dict[str, object]:
    release_manifest = validate_release_manifest(release_root, release_manifest_path)
    expected_manifest = _canonical_json(release_manifest)
    stored_manifest = _read_confined_regular(bundle, "release-manifest.json", missing_ok=False)
    stored_spec = _read_confined_regular(bundle, "release-spec.json", missing_ok=False)
    stored_hash = _read_confined_regular(bundle, "staged-tool.sha256", missing_ok=False)
    if stored_manifest is None or stored_manifest[0] != expected_manifest:
        _reject("bundle release manifest provenance mismatch")
    if stored_spec is None:
        _reject("bundle release spec provenance is missing")
    try:
        stored_spec_object = json.loads(
            stored_spec[0].decode("utf-8"), object_pairs_hook=_json_object
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("bundle release spec is unreadable") from error
    if stored_spec_object != release_manifest:
        _reject("bundle release spec does not match release manifest")
    if release_spec_path is not None:
        spec = _read_confined_regular(
            release_spec_path.parent, release_spec_path.name, missing_ok=False
        )
        if spec is None or stored_spec[0] != spec[0]:
            _reject("bundle release spec provenance mismatch")
    tool = _read_confined_regular(staged_tool.parent, staged_tool.name, missing_ok=False)
    if tool is None or stored_hash is None:
        _reject("bundle staged tool provenance is missing")
    expected_hash = hashlib.sha256(tool[0]).hexdigest().encode() + b"\n"
    if stored_hash[0] != expected_hash:
        _reject("bundle staged tool provenance mismatch")
    try:
        bundle_stat = bundle.lstat()
    except OSError as error:
        raise ReleaseManifestError("bundle is unavailable") from error
    if (
        not stat.S_ISDIR(bundle_stat.st_mode)
        or stat.S_ISLNK(bundle_stat.st_mode)
        or stat.S_IMODE(bundle_stat.st_mode) != 0o700
        or bundle_stat.st_uid != os.geteuid()
        or bundle_stat.st_gid != os.getegid()
    ):
        _reject("bundle ownership or mode is invalid")
    return release_manifest


def verify_release(
    staged_tool: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    *,
    mode: str,
) -> None:
    release = Path(release_root)
    destination = Path(destination_root)
    bundle_path = Path(bundle)
    manifest = _verify_bundle_provenance(
        Path(staged_tool), release, bundle_path, Path(release_manifest_path)
    )
    backup = _load_backup_manifest(bundle_path, backup_manifest_path)
    release_entries = manifest.get("files")
    backup_entries = backup.get("files")
    if not isinstance(release_entries, list) or not isinstance(backup_entries, list):
        _reject("manifest scope is invalid")
    _validate_backup_for_rollback(manifest, backup, bundle_path)
    if mode == "deploy":
        _verify_deployed(release, destination, Path(release_manifest_path))
    elif mode == "rollback":
        if len(release_entries) != len(backup_entries):
            _reject("backup manifest scope does not match release manifest")
        for release_entry, backup_entry in zip(release_entries, backup_entries, strict=True):
            if not isinstance(release_entry, dict) or not isinstance(backup_entry, dict):
                _reject("backup schema is invalid")
            if release_entry.get("path") != backup_entry.get("path"):
                _reject("backup manifest scope does not match release manifest")
            _verify_backup_record(destination, bundle_path, backup_entry)
    else:
        _reject("verify mode must be deploy or rollback")


def _tree_identities_from_fd(
    directory_fd: int, prefix: str = ""
) -> dict[str, tuple[int, int, int]]:
    identities: dict[str, tuple[int, int, int]] = {}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            metadata = entry.stat(follow_symlinks=False)
            identities[relative] = (metadata.st_dev, metadata.st_ino, metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                try:
                    identities.update(_tree_identities_from_fd(child_fd, relative))
                finally:
                    os.close(child_fd)
            elif not stat.S_ISREG(metadata.st_mode):
                _reject("cleanup stage contains unsafe content")
    return identities


def _require_leaf_identity(
    parent_fd: int, name: str, expected: tuple[int, int, int]
) -> os.stat_result:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ReleaseManifestError("cleanup tree identity changed") from error
    if (current.st_dev, current.st_ino, current.st_mode) != expected:
        _reject("cleanup tree identity changed")
    return current


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Rename relative to pinned directories without replacing a destination."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        src_dir_fd,
        os.fsencode(source),
        dst_dir_fd,
        os.fsencode(destination),
        1,  # RENAME_NOREPLACE
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _require_fd_entry_identity(parent_fd: int, name: str, descriptor: int) -> None:
    """Require a no-follow parent entry to still name an open directory."""

    opened = os.fstat(descriptor)
    current = _require_leaf_identity(
        parent_fd, name, (opened.st_dev, opened.st_ino, opened.st_mode)
    )
    if not stat.S_ISDIR(current.st_mode):
        _reject("cleanup quarantine identity changed")


def _quarantine_cleanup_leaf(
    parent_fd: int, name: str, expected: tuple[int, int, int]
) -> str:
    """Atomically isolate a leaf, then prove the isolated inode was approved."""

    quarantine = f".release-cleanup-{secrets.token_hex(16)}"
    try:
        os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        _reject("cleanup quarantine collision")
    os.rename(
        name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
    )
    try:
        _require_leaf_identity(parent_fd, quarantine, expected)
    except BaseException:
        # A substitute moved by the atomic rename must not be deleted. Put it
        # back when the original leaf name is now free; otherwise retain it in
        # quarantine for fail-closed operator recovery.
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.rename(
                    quarantine, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
                )
            except OSError:
                pass
        raise
    return quarantine


def _create_private_cleanup_quarantine(parent_fd: int) -> tuple[str, int]:
    """Create a fresh root-only deletion authority below a pinned parent."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(8):
        name = f".release-cleanup-root-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            os.fchmod(descriptor, 0o700)
            os.fchown(descriptor, 0, 0)
            metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                _reject("cleanup quarantine ownership is unsafe")
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return name, descriptor
        except BaseException:
            os.close(descriptor)
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    _reject("cleanup quarantine collision")


def _remove_tree_contents_fd(
    directory_fd: int,
    expected: dict[str, tuple[int, int, int]],
    prefix: str = "",
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        identity = expected.get(relative)
        if identity is None:
            _reject("cleanup tree scope changed")
        metadata = _require_leaf_identity(directory_fd, name, identity)
        quarantine = _quarantine_cleanup_leaf(directory_fd, name, identity)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(quarantine, flags, dir_fd=directory_fd)
            try:
                child_stat = os.fstat(child_fd)
                if (child_stat.st_dev, child_stat.st_ino, child_stat.st_mode) != identity:
                    _reject("cleanup directory identity changed")
                _remove_tree_contents_fd(child_fd, expected, relative)
            finally:
                os.close(child_fd)
            _require_leaf_identity(directory_fd, quarantine, identity)
            os.rmdir(quarantine, dir_fd=directory_fd)
        elif stat.S_ISREG(metadata.st_mode):
            _require_leaf_identity(directory_fd, quarantine, identity)
            os.unlink(quarantine, dir_fd=directory_fd)
        else:
            _reject("cleanup stage contains unsafe content")
        os.fsync(directory_fd)


def cleanup_stage(
    staged_tool: Path,
    release_root: Path,
    destination_root: Path,
    bundle: Path,
    release_manifest_path: Path,
    backup_manifest_path: Path,
    *,
    before_remove: Callable[[], None] | None = None,
) -> None:
    stage = staged_tool.parent.absolute()
    release_spec_path = stage / "release-spec.json"
    if (
        release_root.absolute().parent != stage
        or release_manifest_path.absolute().parent != stage
        or staged_tool.absolute() != stage / "release_bundle.py"
    ):
        _reject("cleanup stage layout is invalid")
    manifest = _verify_bundle_provenance(
        staged_tool, release_root, bundle, release_manifest_path, release_spec_path
    )
    try:
        verify_release(staged_tool, release_root, destination_root, bundle,
                       release_manifest_path, backup_manifest_path, mode="deploy")
    except ReleaseManifestError:
        verify_release(staged_tool, release_root, destination_root, bundle,
                       release_manifest_path, backup_manifest_path, mode="rollback")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        _reject("release manifest is invalid")
    expected = {"release_bundle.py", "release-spec.json", "release-manifest.json"}
    expected.update(
        f"release/{_validate_relative_path(entry.get('path'))}"
        for entry in entries if isinstance(entry, dict)
    )
    if len(expected) != len(entries) + 3 or _confined_tree_files(stage) != expected:
        _reject("cleanup stage scope is not exact")
    try:
        parent_fd = _open_absolute_directory(stage.parent)
    except OSError as error:
        raise ReleaseManifestError("cleanup parent is unsafe") from error
    stage_fd: int | None = None
    quarantine_fd: int | None = None
    quarantine_name: str | None = None
    stage_moved = False
    try:
        parent_stat = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != 0
            or parent_stat.st_mode & 0o022
        ):
            _reject("cleanup parent is unsafe")
        stage_fd = os.open(
            stage.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        stage_stat = os.fstat(stage_fd)
        stage_identity = (stage_stat.st_dev, stage_stat.st_ino, stage_stat.st_mode)
        expected_identities = _tree_identities_from_fd(stage_fd)
        if before_remove is not None:
            before_remove()
        _require_leaf_identity(parent_fd, stage.name, stage_identity)
        quarantine_name, quarantine_fd = _create_private_cleanup_quarantine(parent_fd)
        os.rename(
            stage.name, "stage", src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd
        )
        stage_moved = True
        _require_leaf_identity(quarantine_fd, "stage", stage_identity)
        os.fsync(quarantine_fd)
        os.fsync(parent_fd)
        _remove_tree_contents_fd(stage_fd, expected_identities)
        _require_leaf_identity(quarantine_fd, "stage", stage_identity)
        os.rmdir("stage", dir_fd=quarantine_fd)
        stage_moved = False
        os.fsync(quarantine_fd)
        try:
            _require_fd_entry_identity(parent_fd, quarantine_name, quarantine_fd)
        except ReleaseManifestError as identity_error:
            raise OSError("cleanup quarantine identity changed") from identity_error
        os.rmdir(quarantine_name, dir_fd=parent_fd)
        quarantine_name = None
        os.fsync(parent_fd)
    except OSError as error:
        # Preserve whatever approved stage content remains. Restore its original
        # name only when that leaf is still free; never overwrite a substitute.
        if stage_moved and quarantine_fd is not None:
            try:
                _rename_noreplace(
                    "stage", stage.name,
                    src_dir_fd=quarantine_fd, dst_dir_fd=parent_fd,
                )
                stage_moved = False
                os.fsync(parent_fd)
            except OSError:
                pass
        raise ReleaseManifestError("confined cleanup failed") from error
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if (
            quarantine_name is not None
            and quarantine_fd is not None
            and not stage_moved
        ):
            try:
                _require_fd_entry_identity(parent_fd, quarantine_name, quarantine_fd)
                os.rmdir(quarantine_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except (OSError, ReleaseManifestError):
                pass
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        os.close(parent_fd)


def _tree_files_from_fd(directory_fd: int, prefix: str = "") -> set[str]:
    files: set[str] = set()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                try:
                    files.update(_tree_files_from_fd(child_fd, relative))
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(relative)
            else:
                _reject("bundle contains symbolic link or non-regular content")
    return files


def _confined_tree_files(root: Path) -> set[str]:
    try:
        descriptor = _open_absolute_directory(root)
    except OSError as error:
        raise ReleaseManifestError("bundle tree is unsafe") from error
    try:
        return _tree_files_from_fd(descriptor)
    finally:
        os.close(descriptor)


def _validate_backup_for_rollback(
    release_manifest: dict[str, object],
    backup_manifest: dict[str, object],
    bundle: Path,
) -> tuple[list[object], list[object]]:
    release_entries = release_manifest.get("files")
    backup_entries = backup_manifest.get("files")
    if not isinstance(release_entries, list) or not isinstance(backup_entries, list):
        _reject("manifest scope is invalid")
    if len(release_entries) != len(backup_entries):
        _reject("backup manifest scope does not match release manifest")
    expected_archives: set[str] = set()
    for release_entry, backup_entry in zip(release_entries, backup_entries, strict=True):
        if not isinstance(release_entry, dict) or not isinstance(backup_entry, dict):
            _reject("backup file record is invalid")
        relative_path = _validate_relative_path(backup_entry.get("path"))
        if release_entry.get("path") != relative_path:
            _reject("backup manifest scope does not match release manifest")
        present = backup_entry.get("present_before")
        if type(present) is not bool:
            _reject("backup present_before must be Boolean")
        if not present:
            if set(backup_entry) != _BACKUP_ABSENT_KEYS:
                _reject("absent backup record must not contain prior metadata")
            continue
        if set(backup_entry) != _BACKUP_PRESENT_KEYS:
            _reject("present backup record metadata is not exact")
        digest = backup_entry.get("sha256")
        mode = backup_entry.get("mode")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            _reject("backup hash must be lowercase SHA-256")
        if not isinstance(mode, str) or not _MODE_RE.fullmatch(mode):
            _reject("backup mode must be four-digit octal")
        for field in ("uid", "gid"):
            value = backup_entry.get(field)
            if type(value) is not int or value < 0 or value > 2**32 - 2:
                _reject(f"backup {field} is outside the supported range")
        archive_path = f"archive/{relative_path}"
        archive = _read_confined_regular(bundle, archive_path, missing_ok=False)
        if archive is None or hashlib.sha256(archive[0]).hexdigest() != digest:
            _reject("archived prior bytes do not match backup manifest")
        expected_archives.add(archive_path)
    actual = _confined_tree_files(bundle)
    expected = expected_archives | {
        "backup-manifest.json", "release-spec.json", "release-manifest.json",
        "staged-tool.sha256",
    }
    # Legacy API-created bundles contain only backup evidence; full release bundles
    # are exact and provenance-bearing.
    if {"release-spec.json", "release-manifest.json", "staged-tool.sha256"} & actual:
        if actual != expected:
            _reject("bundle scope is not exact")
    elif actual != expected_archives | {"backup-manifest.json"}:
        _reject("backup archive scope is not exact")
    return release_entries, backup_entries


def _verify_release_entry(destination: Path | int, entry: dict[str, object]) -> None:
    relative_path = _validate_relative_path(entry["path"])
    current = _read_confined_regular(destination, relative_path, missing_ok=False)
    if current is None:
        _reject("deployed file disappeared")
    content, metadata = current
    if (
        hashlib.sha256(content).hexdigest() != entry["sha256"]
        or metadata.st_uid != entry["uid"]
        or metadata.st_gid != entry["gid"]
        or f"{stat.S_IMODE(metadata.st_mode):04o}" != entry["mode"]
    ):
        _reject("deployed state changed before rollback")


def _snapshot_deployed_records(
    destination: Path | int, release_entries: list[object]
) -> tuple[tuple[str, bytes, int, int, str], ...]:
    """Retain exact pre-rollback deployed bytes and metadata in memory."""

    records = []
    for entry in release_entries:
        if not isinstance(entry, dict):
            _reject("release manifest is invalid")
        _verify_release_entry(destination, entry)
        relative_path = _validate_relative_path(entry["path"])
        current = _read_confined_regular(destination, relative_path, missing_ok=False)
        if current is None:
            _reject("deployed file disappeared")
        content, metadata = current
        records.append((
            relative_path, content, metadata.st_uid, metadata.st_gid,
            f"{stat.S_IMODE(metadata.st_mode):04o}",
        ))
    return tuple(records)


def _restore_release_indices(
    destination: Path | int,
    deployed_records: tuple[tuple[str, bytes, int, int, str], ...],
    indices: list[int],
) -> list[str]:
    diagnostics: list[str] = []
    for index in reversed(indices):
        try:
            relative_path, content, uid, gid, mode = deployed_records[index]
            _atomic_replace_confined(
                destination, relative_path, content, uid, gid, mode,
            )
            restored = _read_confined_regular(destination, relative_path, missing_ok=False)
            if restored is None:
                _reject("deployed snapshot restoration is missing")
            restored_bytes, restored_stat = restored
            if (
                restored_bytes != content
                or restored_stat.st_uid != uid
                or restored_stat.st_gid != gid
                or f"{stat.S_IMODE(restored_stat.st_mode):04o}" != mode
            ):
                _reject("deployed snapshot restoration failed")
        except BaseException as error:
            diagnostics.append(f"restore deployed {index}: {error}")
    return diagnostics



def recover_release(
    staged_tool: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    *,
    service: ServiceControl,
    initially_active: bool,
    after_boundary: Callable[[str], None] | None = None,
) -> None:
    """Idempotently restore a verified backup without trusting mixed destination bytes."""

    destination_path = Path(destination_root)
    release = Path(release_root)
    bundle_path = Path(bundle)
    _verify_bundle_provenance(
        staged_tool, release, bundle_path, Path(release_manifest_path)
    )
    release_manifest = validate_release_manifest(release, release_manifest_path)
    backup_manifest = _load_backup_manifest(bundle_path, backup_manifest_path)
    _, backup_entries = _validate_backup_for_rollback(
        release_manifest, backup_manifest, bundle_path
    )
    backup_records = _snapshot_backup_records(bundle_path, backup_entries)
    try:
        destination_fd = _open_absolute_directory(destination_path)
    except OSError as error:
        raise ReleaseManifestError("destination root is unsafe") from error
    try:
        _assert_pinned_root_identity(destination_path, destination_fd)
        if service.is_active():
            service.stop()
            if after_boundary is not None:
                after_boundary("stop")
        _assert_pinned_root_identity(destination_path, destination_fd)
        if service.is_active():
            _reject("service remains active before recovery writes")
        for index, record in enumerate(backup_records):
            relative_path, present, content, uid, gid, mode = record
            _assert_pinned_root_identity(destination_path, destination_fd)
            if present:
                if content is None or uid is None or gid is None or mode is None:
                    _reject("immutable backup snapshot is invalid")
                _atomic_replace_confined(
                    destination_fd, relative_path, content, uid, gid, mode
                )
                boundary = f"restore:{index}"
            else:
                _remove_confined(destination_fd, relative_path)
                boundary = f"remove:{index}"
            if after_boundary is not None:
                after_boundary(boundary)
            backup_entry = backup_entries[index]
            if not isinstance(backup_entry, dict):
                _reject("backup manifest is invalid")
            _verify_backup_record(destination_fd, bundle_path, backup_entry)
        if service.daemon_reload_needed():
            service.daemon_reload()
            if after_boundary is not None:
                after_boundary("daemon_reload")
        _assert_pinned_root_identity(destination_path, destination_fd)
        if initially_active:
            if not service.is_active():
                service.start()
                if after_boundary is not None:
                    after_boundary("start")
            if not service.is_active():
                _reject("service is inactive after recovery")
            service.run_health_checks()
            if after_boundary is not None:
                after_boundary("readiness")
        elif service.is_active():
            service.stop()
            if service.is_active():
                _reject("service is active after inactive recovery")
        _assert_pinned_root_identity(destination_path, destination_fd)
        for backup_entry in backup_entries:
            if not isinstance(backup_entry, dict):
                _reject("backup manifest is invalid")
            _verify_backup_record(destination_fd, bundle_path, backup_entry)
    finally:
        os.close(destination_fd)

def rollback_release(
    release_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    *,
    service: ServiceControl,
) -> None:
    """Restore through one pinned root, including rollback-of-rollback."""

    release = Path(release_root)
    destination_path = Path(destination_root)
    bundle_path = Path(bundle)
    try:
        destination_fd = _open_absolute_directory(destination_path)
    except OSError as error:
        raise ReleaseManifestError("destination root is unsafe") from error
    try:
        _assert_pinned_root_identity(destination_path, destination_fd)
        release_manifest = _verify_deployed(
            release, destination_fd, Path(release_manifest_path)
        )
        backup_manifest = _load_backup_manifest(bundle_path, backup_manifest_path)
        release_entries, backup_entries = _validate_backup_for_rollback(
            release_manifest, backup_manifest, bundle_path
        )
        deployed_records = _snapshot_deployed_records(destination_fd, release_entries)
        initially_active = bool(service.is_active())
        touched: list[int] = []
        try:
            if initially_active:
                service.stop()
            _assert_pinned_root_identity(destination_path, destination_fd)
            for entry in release_entries:
                if not isinstance(entry, dict):
                    _reject("release manifest is invalid")
                _verify_release_entry(destination_fd, entry)
            for index, (release_entry, backup_entry) in enumerate(
                zip(release_entries, backup_entries, strict=True)
            ):
                if not isinstance(release_entry, dict) or not isinstance(backup_entry, dict):
                    _reject("manifest is invalid")
                _assert_pinned_root_identity(destination_path, destination_fd)
                _verify_release_entry(destination_fd, release_entry)
                relative_path = _validate_relative_path(backup_entry["path"])

                def changed(index: int = index) -> None:
                    if index not in touched:
                        touched.append(index)

                if backup_entry["present_before"]:
                    archive = _read_confined_regular(
                        bundle_path, f"archive/{relative_path}", missing_ok=False
                    )
                    if archive is None:
                        _reject("required rollback archive is missing")
                    _atomic_replace_confined(
                        destination_fd, relative_path, archive[0], backup_entry["uid"],
                        backup_entry["gid"], backup_entry["mode"], on_publish=changed,
                    )
                else:
                    _remove_confined(destination_fd, relative_path, on_remove=changed)
                _verify_backup_record(destination_fd, bundle_path, backup_entry)
            for backup_entry in backup_entries:
                if not isinstance(backup_entry, dict):
                    _reject("backup manifest is invalid")
                _verify_backup_record(destination_fd, bundle_path, backup_entry)
            if service.daemon_reload_needed():
                service.daemon_reload()
                _assert_pinned_root_identity(destination_path, destination_fd)
            if initially_active:
                service.start()
                _assert_pinned_root_identity(destination_path, destination_fd)
                service.run_health_checks()
                _assert_pinned_root_identity(destination_path, destination_fd)
            for backup_entry in backup_entries:
                if not isinstance(backup_entry, dict):
                    _reject("backup manifest is invalid")
                _verify_backup_record(destination_fd, bundle_path, backup_entry)
        except BaseException as primary:
            diagnostics = _prove_service_stopped_for_file_recovery(service)
            if not diagnostics:
                diagnostics.extend(
                    _restore_release_indices(destination_fd, deployed_records, touched)
                )
            diagnostics.extend(_recover_initial_service_state(service, initially_active))
            _raise_with_recovery(primary, diagnostics, service)
    finally:
        os.close(destination_fd)


_RELEASE_EXPORT_REQUIRED = frozenset({
    "acceptance_artifacts.py",
    "release_argparse.py",
    "webapp.py",
    "requirements.txt",
    "app/__init__.py",
    "app/wsgi.py",
    "deploy/release_bundle.py",
    "deploy/guarded_priority_1_3_release.py",
    "deploy/install_priority_1_3_remote.py",
    "deploy/matrix_priority_1_3_remote.py",
    "deploy/verify_priority_1_3_acceptance.py",
    "scripts/verify_priority_1_3_remote.py",
})
_NON_PRODUCTION_EXPORT_ROOTS = frozenset({
    ".git", ".github", ".gitlab", ".hermes", "ci", "data", "docs", "skills", "tests",
})
_NON_PRODUCTION_EXPORT_FILES = frozenset({
    ".gitignore", ".gitlab-ci.yml", "README.md", "code_review.md",
})


def _exact_commit_archive(repository: Path, source_commit: str) -> bytes:
    if not repository.is_absolute() or not repository.is_dir() or repository.is_symlink():
        _reject("release repository must be a real absolute directory")
    if not isinstance(source_commit, str) or _SOURCE_SHA_RE.fullmatch(source_commit) is None:
        _reject("release source commit must be an exact lowercase Git SHA")
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{source_commit}^{{commit}}"],
        cwd=repository, check=False, text=True, capture_output=True, shell=False,
    )
    if resolved.returncode != 0 or resolved.stderr or resolved.stdout.strip() != source_commit:
        _reject("release source commit is not an exact Git commit")
    archived = subprocess.run(
        ["git", "archive", "--format=tar", source_commit], cwd=repository,
        check=False, capture_output=True, shell=False,
    )
    if archived.returncode != 0 or archived.stderr or not archived.stdout:
        _reject("exact Git commit export failed")
    return archived.stdout


def _extract_exact_archive(archive: bytes, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as exported:
            for member in exported.getmembers():
                path = PurePosixPath(member.name)
                if (path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
                        or not (member.isdir() or member.isfile())):
                    _reject("exact Git commit export contains unsafe topology")
            exported.extractall(destination)
    except (tarfile.TarError, OSError) as error:
        raise ReleaseManifestError("exact Git commit export is unreadable") from error


def _is_production_export_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (
        relative not in _NON_PRODUCTION_EXPORT_FILES
        and path.parts[0] not in _NON_PRODUCTION_EXPORT_ROOTS
        and not path.parts[0].startswith(".git")
    )


def _copy_production_export(export: Path, release_root: Path) -> list[str]:
    release_root.mkdir(mode=0o700)
    exported_files = _release_files(export)
    selected = sorted(path for path in exported_files if _is_production_export_path(path))
    if not _RELEASE_EXPORT_REQUIRED.issubset(selected):
        _reject("required production file is absent from exact commit export")
    for relative in selected:
        source = export.joinpath(*PurePosixPath(relative).parts)
        destination = release_root.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    verifier = release_root / "scripts/verify_priority_1_3_remote.py"
    if stat.S_IMODE(verifier.stat(follow_symlinks=False).st_mode) & 0o111 == 0:
        _reject("remote acceptance verifier from exact commit export is not executable")
    return selected


def _exercise_installed_wsgi(release_root: Path, python: Path, scratch_parent: Path) -> None:
    if not python.is_absolute():
        _reject("production Python must be absolute")
    with tempfile.TemporaryDirectory(prefix="matrix-wsgi-check-", dir=scratch_parent) as directory:
        runtime_parent = Path(directory)
        runtime_parent.chmod(0o755)
        runtime = runtime_parent / "installed"
        shutil.copytree(release_root, runtime)
        runtime_data = runtime / "data"
        runtime_data.mkdir(mode=0o700)
        _publish_new_path(
            runtime_data / "paired-devices.json",
            _canonical_json([{
                "id": "A" * 32,
                "name": "release WSGI smoke",
                "paired_at": 0,
            }]),
            mode=0o600,
        )
        environment = {
            "HOME": str(runtime_parent), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0", "MATRIX_LOCK_FILE": str(runtime_parent / "matrix-display.lock"),
        }
        command = [
            str(python), "-I", "-B", "-c",
            (
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "import rgbmatrix;"
                "import display_controller,matrix_framework;"
                "smoke_matrix=type('_ReleaseWsgiSmokeMatrix',(),{"
                "'release_wsgi_smoke':True,'clear':lambda self:None,'swap':lambda self:None});"
                "matrix_framework.Matrix=smoke_matrix;"
                "display_controller.DisplayController.start=lambda self:True;"
                "import app.wsgi as entry;assert entry.app is not None"
            ),
            str(runtime),
        ]
        try:
            completed = subprocess.run(
                command, cwd="/", env=environment, check=False, text=True,
                capture_output=True, timeout=60, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ReleaseManifestError("installed WSGI production import failed") from error
        if completed.returncode != 0 or completed.stdout or completed.stderr:
            _reject("installed WSGI production import failed")


def _freeze_outer_release_spec(release_root: Path, spec_path: Path, uid: int, gid: int) -> dict[str, object]:
    if type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0:
        _reject("release destination owner is invalid")
    marker = release_root / ".matrix-release.json"
    try:
        inner = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("acceptance manifest must precede outer release spec") from error
    source_files = _release_files(release_root) - {".matrix-release.json"}
    entries = inner.get("files") if isinstance(inner, dict) else None
    if (set(inner) != {"schema", "source_sha", "candidate_tracked_files", "files"}
            or inner.get("schema") != 1
            or not isinstance(entries, list)
            or inner.get("candidate_tracked_files") != sorted(source_files)
            or {entry.get("path") for entry in entries if isinstance(entry, dict)} != source_files
            or len(entries) != len(source_files)):
        _reject("acceptance manifest must precede outer release spec")
    manifest_entries = []
    for relative in sorted(_release_files(release_root)):
        path = release_root.joinpath(*PurePosixPath(relative).parts)
        manifest_entries.append({
            "path": relative, "sha256": hashlib.sha256(_read_regular_path(path)).hexdigest(),
            "uid": uid, "gid": gid, "mode": f"{stat.S_IMODE(path.stat(follow_symlinks=False).st_mode):04o}",
        })
    spec = {"version": 1, "files": manifest_entries}
    _publish_new_path(spec_path, _canonical_json(spec), mode=0o400)
    validate_release_manifest(release_root, spec_path)
    return spec


def prepare_release_tree(
    repository: str | os.PathLike[str], source_commit: str, stage: str | os.PathLike[str],
    python: str | os.PathLike[str], uid: int, gid: int,
) -> dict[str, object]:
    """Build one immutable release stage from one exact committed Git export."""
    stage_path = Path(stage)
    if not stage_path.is_absolute():
        _reject("release stage must be absolute")
    _require_fresh_path(stage_path, "release stage")
    archive = _exact_commit_archive(Path(repository), source_commit)
    stage_path.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="matrix-commit-export-", dir=stage_path.parent) as directory:
            exported = Path(directory) / "export"
            _extract_exact_archive(archive, exported)
            release_root = stage_path / "release"
            candidate_tracked_files = _copy_production_export(exported, release_root)
        prepare_acceptance_manifest(release_root, source_commit, candidate_tracked_files)
        _exercise_installed_wsgi(release_root, Path(python), stage_path.parent)
        shutil.copy2(release_root / "deploy/release_bundle.py", stage_path / "release_bundle.py")
        return _freeze_outer_release_spec(release_root, stage_path / "release-spec.json", uid, gid)
    except BaseException:
        shutil.rmtree(stage_path, ignore_errors=True)
        raise

def build_parser() -> argparse.ArgumentParser:
    options = (
        "required flags: --release-root --destination-root --bundle "
        "--release-manifest --backup-manifest; preflight/backup/deploy: --release-spec; "
        "recover: --service --initial-service-state and active readiness; "
        "optional: --service --dry-run; verify: --mode deploy|rollback"
    )
    parser = argparse.ArgumentParser(
        description="Verify and apply an exact approved Matrix release bundle.",
        epilog=options,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-acceptance-manifest", description=options)
    add_store_once_argument(prepare, "--release-root", required=True)
    add_store_once_argument(prepare, "--source-sha", required=True)
    add_store_once_argument(prepare, "--candidate-tracked-files-json", required=True)
    prepare_tree = subparsers.add_parser("prepare-release-tree", description=options)
    for option in ("--repository", "--source-commit", "--stage", "--python"):
        add_store_once_argument(prepare_tree, option, required=True)
    add_store_once_argument(prepare_tree, "--uid", required=True, type=int)
    add_store_once_argument(prepare_tree, "--gid", required=True, type=int)
    common_commands = ("backup", "preflight", "deploy", "rollback", "verify", "cleanup")
    for command in common_commands:
        subparser = subparsers.add_parser(command, description=options)
        add_store_once_argument(subparser, "--release-root", required=True)
        add_store_once_argument(subparser, "--destination-root", required=True)
        add_store_once_argument(subparser, "--bundle", required=True)
        add_store_once_argument(subparser, "--release-manifest", required=True)
        add_store_once_argument(subparser, "--backup-manifest", required=True)
        add_store_once_argument(subparser, "--service")
        subparser.add_argument("--dry-run", action="store_true")
        if command in ("backup", "preflight", "deploy"):
            add_store_once_argument(subparser, "--release-spec", required=True)
        if command == "verify":
            add_store_once_argument(subparser, "--mode", choices=("deploy", "rollback"), required=True)
    recover = subparsers.add_parser("recover", description=options)
    for option in (
        "--release-root", "--destination-root", "--bundle", "--release-manifest",
        "--backup-manifest", "--service",
    ):
        add_store_once_argument(recover, option, required=True)
    add_store_once_argument(
        recover, "--initial-service-state", required=True,
        choices=("active", "inactive"),
    )
    add_store_once_argument(recover, "--health-url")
    add_store_once_argument(recover, "--health-status", type=int)
    add_store_once_argument(recover, "--health-timeout-seconds", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "prepare-acceptance-manifest":
        try:
            candidate_tracked_files = json.loads(arguments.candidate_tracked_files_json)
        except json.JSONDecodeError as error:
            raise ReleaseManifestError("candidate tracked files are invalid") from error
        prepare_acceptance_manifest(
            arguments.release_root, arguments.source_sha, candidate_tracked_files,
        )
        return 0
    if arguments.command == "prepare-release-tree":
        prepare_release_tree(
            arguments.repository, arguments.source_commit, arguments.stage,
            arguments.python, arguments.uid, arguments.gid,
        )
        return 0
    staged_tool = Path(__file__)
    release_root = Path(arguments.release_root)
    destination_root = Path(arguments.destination_root)
    bundle = Path(arguments.bundle)
    release_manifest_path = Path(arguments.release_manifest)
    backup_manifest_path = Path(arguments.backup_manifest)
    release_spec_value = getattr(arguments, "release_spec", None)
    release_spec_path = Path(release_spec_value) if release_spec_value else None
    if arguments.service is not None:
        _validate_service_name(arguments.service)

    if arguments.command == "recover":
        initially_active = arguments.initial_service_state == "active"
        readiness = (
            arguments.health_url, arguments.health_status,
            arguments.health_timeout_seconds,
        )
        if not initially_active:
            if any(value is not None for value in readiness):
                _reject("inactive recovery forbids readiness arguments")
        elif readiness != ("http://127.0.0.1:5000/", 302, 30):
            _reject("active recovery requires exact loopback readiness arguments")
        service = _service_control(arguments.service)
        configure = getattr(service, "configure_health", None)
        if initially_active and configure is not None:
            configure(*readiness)
        recover_release(
            staged_tool, release_root, destination_root, bundle,
            release_manifest_path, backup_manifest_path,
            service=service, initially_active=initially_active,
        )
        return 0

    if arguments.command == "backup":
        if arguments.dry_run:
            _reject("backup does not support --dry-run")
        if release_spec_path is None:
            _reject("backup requires --release-spec")
        backup_release(
            staged_tool, release_root, destination_root, bundle,
            release_manifest_path, backup_manifest_path, release_spec_path,
            owner_uid=os.geteuid(), owner_gid=os.getegid(),
        )
        return 0

    if arguments.command == "preflight" and arguments.dry_run and not bundle.exists():
        if release_spec_path is None:
            _reject("pre-backup preflight requires --release-spec")
        manifest = verify_staged_release(staged_tool, release_root, release_spec_path)
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                _reject("release spec is invalid")
            _reject_forbidden_release_path(_validate_relative_path(entry["path"]))
        predicted = _snapshot_backup_manifest(destination_root, manifest)
        for line in _dry_run_lines(manifest, predicted):
            print(line)
        if arguments.service is not None:
            print(f"SERVICE name={arguments.service} action=stop-if-active")
            print(f"SERVICE name={arguments.service} action=start-and-health-if-initially-active")
        return 0

    if arguments.command in ("preflight", "deploy"):
        if release_spec_path is not None:
            _verify_bundle_provenance(
                staged_tool, release_root, bundle, release_manifest_path,
                release_spec_path,
            )
        release_manifest, backup_manifest = preflight_release(
            staged_tool, release_root, destination_root, bundle,
            release_manifest_path, backup_manifest_path
        )
        if arguments.dry_run:
            for line in _dry_run_lines(release_manifest, backup_manifest):
                print(line)
            if arguments.service is not None:
                print(f"SERVICE name={arguments.service} action=stop-if-active")
                print(
                    f"SERVICE name={arguments.service} "
                    "action=start-and-health-if-initially-active"
                )
            return 0
        if arguments.command == "preflight":
            return 0
        deploy_release(
            staged_tool, release_root, destination_root, bundle,
            release_manifest_path, backup_manifest_path,
            service=_service_control(arguments.service),
        )
        verify_release(
            staged_tool, release_root, destination_root, bundle,
            release_manifest_path, backup_manifest_path, mode="deploy"
        )
        return 0

    if arguments.command == "rollback":
        _verify_bundle_provenance(
            staged_tool, release_root, bundle, release_manifest_path
        )
        release_manifest = _verify_deployed(
            release_root, destination_root, release_manifest_path
        )
        backup_manifest = _load_backup_manifest(bundle, backup_manifest_path)
        _validate_backup_for_rollback(release_manifest, backup_manifest, bundle)
        if arguments.dry_run:
            for line in _dry_run_lines(release_manifest, backup_manifest):
                if line.startswith("ROLLBACK "):
                    print(line)
            return 0
        rollback_release(
            release_root, destination_root, bundle, release_manifest_path,
            backup_manifest_path, service=_service_control(arguments.service)
        )
        return 0

    if arguments.dry_run:
        _reject(f"{arguments.command} does not support --dry-run")
    if arguments.command == "verify":
        verify_release(
            staged_tool, release_root, destination_root, bundle,
            release_manifest_path, backup_manifest_path, mode=arguments.mode,
        )
        return 0
    cleanup_stage(
        staged_tool, release_root, destination_root, bundle,
        release_manifest_path, backup_manifest_path,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseManifestError as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
