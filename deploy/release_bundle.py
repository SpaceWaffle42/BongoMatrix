"""Fail-closed validation for approved Matrix release manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import NoReturn, Protocol

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODE_RE = re.compile(r"0[0-7]{3}\Z")
_MANIFEST_KEYS = {"version", "files"}
_FILE_KEYS = {"path", "sha256", "uid", "gid", "mode"}
_BACKUP_ABSENT_KEYS = {"path", "present_before"}
_BACKUP_PRESENT_KEYS = _BACKUP_ABSENT_KEYS | {"sha256", "uid", "gid", "mode"}
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
    destination_root: Path, relative_path: str
) -> tuple[bytes, os.stat_result] | None:
    current = destination_root
    parts = PurePosixPath(relative_path).parts
    for part in parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(current_stat.st_mode) or stat.S_ISLNK(current_stat.st_mode):
            _reject("destination path traverses non-directory or symbolic link content")

    destination = current / parts[-1]
    try:
        destination_stat = destination.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(destination_stat.st_mode) or stat.S_ISLNK(
        destination_stat.st_mode
    ):
        _reject("destination content must be a regular file")
    try:
        descriptor = os.open(
            destination,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError as error:
        raise ReleaseManifestError("destination content changed during backup") from error
    except OSError as error:
        raise ReleaseManifestError("destination content is unsafe") from error
    try:
        destination_stat = os.fstat(descriptor)
        if not stat.S_ISREG(destination_stat.st_mode):
            _reject("destination content must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as destination_file:
            prior_bytes = destination_file.read()
        return prior_bytes, destination_stat
    finally:
        os.close(descriptor)


def create_backup_manifest(
    destination_root: str | os.PathLike[str],
    bundle: str | os.PathLike[str],
    release_manifest: dict[str, object],
    backup_manifest_path: str | os.PathLike[str],
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
        prior = _read_prior_destination(root, relative_path)
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
    bundle_path = Path(bundle)
    if bundle_path.exists() and (not bundle_path.is_dir() or bundle_path.is_symlink()):
        _reject("bundle must be a real directory")
    bundle_path.mkdir(parents=True, exist_ok=True)
    for relative_path, prior_bytes in archives:
        archive_path = bundle_path / "archive" / relative_path
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(prior_bytes)

    manifest_path = Path(backup_manifest_path)
    try:
        manifest_path.relative_to(bundle_path)
    except ValueError:
        _reject("backup manifest must be inside the bundle")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(backup_manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return backup_manifest


def _read_confined_regular(
    root: Path, relative_path: str, *, missing_ok: bool
) -> tuple[bytes, os.stat_result] | None:
    """Read a regular file without following any path component symlink."""

    relative_path = _validate_relative_path(relative_path)
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise ReleaseManifestError("confined root is unavailable") from error
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        _reject("confined root must be a real directory")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
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

    destination = Path(destination_root)
    if not destination.is_dir() or destination.is_symlink():
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
    return release_manifest, backup_manifest


def _open_confined_parent(root: Path, relative_path: str) -> tuple[int, str]:
    """Open an existing destination parent without following symlinks."""

    parts = PurePosixPath(_validate_relative_path(relative_path)).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, flags)
    except OSError as error:
        raise ReleaseManifestError("destination root is unsafe") from error
    try:
        for part in parts[:-1]:
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
    root: Path,
    relative_path: str,
    content: bytes,
    uid: int,
    gid: int,
    mode: str,
) -> None:
    """Durably replace one confined regular file with exact metadata."""

    directory_fd, name = _open_confined_parent(root, relative_path)
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
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _remove_confined(root: Path, relative_path: str) -> None:
    directory_fd, name = _open_confined_parent(root, relative_path)
    try:
        try:
            target_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(target_stat.st_mode):
            _reject("rollback target is not a regular file")
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _verify_backup_record(
    destination_root: Path,
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
    """Deploy an exact release and roll back every touched path on failure."""

    release_manifest, backup_manifest = preflight_release(
        staged_tool,
        release_root,
        destination_root,
        bundle,
        release_manifest_path,
        backup_manifest_path,
    )
    release_entries = release_manifest["files"]
    backup_entries = backup_manifest["files"]
    if not isinstance(release_entries, list) or not isinstance(backup_entries, list):
        _reject("validated manifest scope is unavailable")

    destination = Path(destination_root)
    release = Path(release_root)
    bundle_path = Path(bundle)
    initially_active = bool(service.is_active())
    if initially_active:
        service.stop()

    touched: list[int] = []
    try:
        for index, entry in enumerate(release_entries):
            if not isinstance(entry, dict):
                _reject("release manifest is not validated")
            relative_path = _validate_relative_path(entry["path"])
            source = _read_confined_regular(release, relative_path, missing_ok=False)
            if source is None or hashlib.sha256(source[0]).hexdigest() != entry["sha256"]:
                _reject("release source changed after preflight")
            _atomic_replace_confined(
                destination,
                relative_path,
                source[0],
                entry["uid"],
                entry["gid"],
                entry["mode"],
            )
            touched.append(index)
            if after_replace is not None:
                after_replace(relative_path)
    except BaseException:
        for index in reversed(touched):
            backup_entry = backup_entries[index]
            if not isinstance(backup_entry, dict):
                _reject("backup manifest is not validated")
            relative_path = _validate_relative_path(backup_entry["path"])
            if backup_entry["present_before"]:
                archive = _read_confined_regular(
                    bundle_path, f"archive/{relative_path}", missing_ok=False
                )
                if archive is None:
                    _reject("required rollback archive is missing")
                _atomic_replace_confined(
                    destination,
                    relative_path,
                    archive[0],
                    backup_entry["uid"],
                    backup_entry["gid"],
                    backup_entry["mode"],
                )
            else:
                _remove_confined(destination, relative_path)
            _verify_backup_record(destination, bundle_path, backup_entry)
        raise
    finally:
        if service.daemon_reload_needed():
            service.daemon_reload()
        if initially_active:
            service.start()
