"""Saving a file only the Matrix's own user can read (an API key, a sign-in), whole or not at all."""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

_lock = threading.Lock()


def write(path: Path, text: str) -> None:
    """``text`` into ``path``, mode 0600 from the moment the file exists (never readable by others, even
    briefly), through a temporary file of its own so two saves at once can't mix or lose each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as out:
                out.write(text)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
