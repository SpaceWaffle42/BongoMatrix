"""Fail-closed filesystem resolution for preview media references."""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path, PurePosixPath


_MEDIA_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MEDIA_EXTENSIONS = frozenset({".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4"})
_ERROR_MESSAGES = {
    "invalid_reference": "media reference is invalid",
    "not_found": "media resource was not found",
    "not_regular": "media resource is not a regular file",
    "unsafe_path": "media resource path is unsafe",
}


class MediaResolutionError(RuntimeError):
    """A deterministic media lookup failure safe to expose to callers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class PinnedMediaHandle:
    """Own one read-only descriptor for a resolved regular media file."""

    __slots__ = ("_descriptor",)

    def __init__(self, descriptor: int):
        self._descriptor = descriptor

    @property
    def closed(self) -> bool:
        return self._descriptor is None

    def fileno(self) -> int:
        if self._descriptor is None:
            raise ValueError("media handle is closed")
        return self._descriptor

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        os.close(descriptor)

    def __enter__(self):
        self.fileno()
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()
        return False


class MediaResolver:
    """Reject unresolved or unsafe media without exposing filesystem paths."""

    def __init__(self, media_root):
        root = Path(media_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("media root must be a directory")
        self._root = root

    @staticmethod
    def _normalize(reference: str) -> tuple[str, ...]:
        if not isinstance(reference, str) or not reference or "\x00" in reference:
            raise MediaResolutionError("invalid_reference")

        path = PurePosixPath(reference)
        raw_parts = path.parts
        if path.is_absolute() or ".." in raw_parts:
            raise MediaResolutionError("invalid_reference")

        normalized = os.path.normpath(reference)
        normalized_path = PurePosixPath(normalized)
        parts = normalized_path.parts
        if normalized_path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
            raise MediaResolutionError("invalid_reference")
        if len(parts) == 1:
            filename = parts[0]
        elif len(parts) == 2 and parts[0] == "built-in":
            filename = parts[1]
        else:
            raise MediaResolutionError("invalid_reference")
        if (
            _MEDIA_FILENAME.fullmatch(filename) is None
            or Path(filename).suffix.lower() not in _MEDIA_EXTENSIONS
        ):
            raise MediaResolutionError("invalid_reference")
        return parts

    def resolve(self, reference: str) -> PinnedMediaHandle:
        """Open and return one confined regular file descriptor."""
        parts = self._normalize(reference)
        candidate = self._root.joinpath(*parts)

        for component_count in range(1, len(parts)):
            component = self._root.joinpath(*parts[:component_count])
            try:
                component_status = component.lstat()
            except FileNotFoundError:
                raise MediaResolutionError("not_found") from None
            except OSError:
                raise MediaResolutionError("unsafe_path") from None
            if stat.S_ISLNK(component_status.st_mode):
                raise MediaResolutionError("unsafe_path")
            if not stat.S_ISDIR(component_status.st_mode):
                raise MediaResolutionError("not_found")

        try:
            candidate_status = candidate.lstat()
        except FileNotFoundError:
            raise MediaResolutionError("not_found") from None
        except OSError:
            raise MediaResolutionError("unsafe_path") from None
        if stat.S_ISLNK(candidate_status.st_mode):
            raise MediaResolutionError("unsafe_path")

        descriptor = None
        try:
            descriptor = os.open(
                candidate,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            descriptor_status = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_status.st_mode):
                raise MediaResolutionError("not_regular")
            proc_target = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
            if not proc_target.is_relative_to(self._root):
                raise MediaResolutionError("unsafe_path")
            handle = PinnedMediaHandle(descriptor)
            descriptor = None
            return handle
        except MediaResolutionError:
            raise
        except FileNotFoundError:
            raise MediaResolutionError("not_found") from None
        except OSError as error:
            if error.errno == errno.ENOENT:
                raise MediaResolutionError("not_found") from None
            if error.errno in {errno.ELOOP, errno.EXDEV}:
                raise MediaResolutionError("unsafe_path") from None
            raise MediaResolutionError("unsafe_path") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
