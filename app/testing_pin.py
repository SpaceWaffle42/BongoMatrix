"""Fail-closed loading for the temporary pre-1.0 testing PIN."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")
_PIN_PATTERN = re.compile(rb"[0-9]{6}\n?")


def _is_pre_one(application_version):
    match = _VERSION_PATTERN.fullmatch(str(application_version))
    return bool(match and tuple(map(int, match.groups())) < (1, 0, 0))


def load_testing_pin(path, application_version, *, expected_uid=None):
    """Return a secure six-digit testing PIN only for pre-1.0 applications."""
    if not _is_pre_one(application_version):
        return None
    if expected_uid is None:
        expected_uid = os.geteuid()

    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return None
        content = os.read(fd, 8)
        if _PIN_PATTERN.fullmatch(content) is None:
            return None
        return content.rstrip(b"\n").decode("ascii")
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(fd)
