#!/usr/bin/env python3
"""Install digest-pinned RGB Matrix bindings into a fresh trusted import root."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
from pathlib import Path

EXPECTED_FILES = ("__init__.py", "core.so", "graphics.so")
MAX_FILE_BYTES = 16 * 1024 * 1024


def _trusted_directory(info):
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == 0
        and info.st_gid == 0
        and not stat.S_IMODE(info.st_mode) & 0o022
    )


def _open_directory(path: Path):
    if not path.is_absolute():
        raise RuntimeError(f"path must be absolute: {path}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not _trusted_directory(os.fstat(fd)):
            raise RuntimeError(f"unsafe directory ancestry: {path}")
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            if not _trusted_directory(os.fstat(fd)):
                raise RuntimeError(f"unsafe directory ancestry: {path}")
        return fd
    except BaseException as error:
        os.close(fd)
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"unsafe directory ancestry: {path}") from error


def _parse_manifest(payload: bytes):
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("invalid pinned RGB manifest") from error
    result = {}
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(
            r"([0-9a-f]{64})  (rgbmatrix/(?:__init__\.py|core\.so|graphics\.so))\n",
            line,
        )
        if match is None or match.group(2) in result:
            raise RuntimeError("invalid pinned RGB manifest")
        result[match.group(2)] = match.group(1)
    if set(result) != {f"rgbmatrix/{name}" for name in EXPECTED_FILES}:
        raise RuntimeError("invalid pinned RGB manifest")
    return result


def _read_source(source_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_nlink != 1
            or info.st_size > MAX_FILE_BYTES
        ):
            raise RuntimeError(f"unsafe RGB source file: {name}")
        payload = bytearray()
        while len(payload) <= MAX_FILE_BYTES:
            chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(payload)))
            if not chunk:
                return bytes(payload)
            payload.extend(chunk)
        raise RuntimeError(f"RGB source file exceeded bound: {name}")
    finally:
        os.close(fd)


def _write_file(directory_fd, name, payload):
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def install(source: Path, destination: Path, manifest_payload: bytes):
    """Verify source bytes against an independent manifest, then publish fresh."""
    manifest = _parse_manifest(manifest_payload)
    source_fd = _open_directory(source)
    try:
        if set(os.listdir(source_fd)) != set(EXPECTED_FILES):
            raise RuntimeError("unexpected RGB source entry")
        files = {name: _read_source(source_fd, name) for name in EXPECTED_FILES}
    finally:
        os.close(source_fd)
    for name, payload in files.items():
        if hashlib.sha256(payload).hexdigest() != manifest[f"rgbmatrix/{name}"]:
            raise RuntimeError(f"RGB source digest mismatch: {name}")

    parent_fd = _open_directory(destination.parent)
    destination_fd = None
    package_fd = None
    try:
        try:
            os.mkdir(destination.name, 0o755, dir_fd=parent_fd)
        except OSError as error:
            raise RuntimeError("RGB destination must be a fresh trusted path") from error
        destination_fd = os.open(
            destination.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        if not _trusted_directory(os.fstat(destination_fd)):
            raise RuntimeError("unsafe RGB destination")
        os.mkdir("rgbmatrix", 0o755, dir_fd=destination_fd)
        package_fd = os.open(
            "rgbmatrix",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=destination_fd,
        )
        for name in EXPECTED_FILES:
            _write_file(package_fd, name, files[name])
        _write_file(destination_fd, "rgbmatrix.sha256", manifest_payload)
        os.fsync(package_fd)
        os.fsync(destination_fd)
        os.fsync(parent_fd)
    finally:
        if package_fd is not None:
            os.close(package_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(parent_fd)


def _read_manifest(path: Path):
    parent_fd = _open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_nlink != 1
                or info.st_size > 1024
            ):
                raise RuntimeError("unsafe pinned RGB manifest file")
            return os.read(fd, 1025)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root is required")
    install(args.source, args.destination, _read_manifest(args.manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
