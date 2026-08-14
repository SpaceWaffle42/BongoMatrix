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
