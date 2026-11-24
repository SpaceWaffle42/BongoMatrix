"""Saving files so a power cut leaves the old file or the new one, never half of one, and never takes back
a save that was already reported done.

``os.replace`` alone isn't enough on an SD card: the new file's contents, and the rename itself, can still be
in memory when the power goes. After the cut the card can hold an empty file, or the old one again. So:

- ``replace(tmp, target)``: flush ``tmp`` to the card, rename it over ``target``, then flush the folder
  (the folder is where a rename is kept). For writers that already made their temporary file.
- ``write_text(path, text, mode)``: all of it, for a small text file (a temporary file of its own beside
  ``path``, so two writers at once can't mix).

Folder flushes quietly do nothing where a system can't do them (Windows), so the same code runs everywhere.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def sync_file(path) -> None:
    """Flush a file's contents to the card."""
    fd = os.open(path, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_folder(path) -> None:
    """Flush a folder's entries (a rename, a new file) to the card. Nothing where folders can't be opened."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace(tmp, target) -> None:
    """``tmp`` takes ``target``'s place, on the card for good: contents first, then the rename."""
    sync_file(tmp)
    os.replace(tmp, target)
    sync_folder(Path(target).parent)


def write_text(path, text: str, mode: int = 0o644) -> None:
    """``text`` into ``path`` whole or not at all, with permissions ``mode`` from the moment the file exists."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)   # (the umask may have narrowed it)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sync_folder(path.parent)
