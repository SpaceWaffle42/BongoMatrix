"""Fail-closed validation for approved Matrix release manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import NoReturn

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODE_RE = re.compile(r"0[0-7]{3}\Z")
_MANIFEST_KEYS = {"version", "files"}
_FILE_KEYS = {"path", "sha256", "uid", "gid", "mode"}


class ReleaseManifestError(ValueError):
    """The approved release manifest or its source tree is not exact."""


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
