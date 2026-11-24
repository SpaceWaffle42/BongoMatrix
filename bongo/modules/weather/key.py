"""The OpenWeather API key: one for the whole Matrix, set in Settings > Weather.

Like Spotify's sign-in it lives in its own file, ``data/openweather.json``, never in the config: the
dashboard can read the whole config, and the key is yours alone. Nothing here logs or returns the
key itself; the dashboard is only told whether there is one and whether it works.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

from bongo import paths, private

FILE = Path(os.environ.get("BONGO_OPENWEATHER", paths.DATA / "openweather.json"))
_lock = threading.Lock()
_read: tuple = (None, "")   # (the file's mtime when last read, the key in it)


def get() -> str:
    """The saved key ("" when there is none). Read again only when the file changes."""
    global _read
    try:
        stamp = FILE.stat().st_mtime_ns
    except OSError:
        return ""
    with _lock:
        if _read[0] != stamp:
            try:
                key = str(json.loads(FILE.read_text()).get("key") or "").strip()
            except (OSError, ValueError, AttributeError):
                key = ""
            _read = (stamp, key)
        return _read[1]


def tag(key: str) -> str:
    """A short stand-in for a key, safe to keep and compare (the cache, and which key was refused)."""
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else "none"


def clean(key: str) -> str:
    """A pasted key without the spaces, quotes or line break that came with it (nothing else changed)."""
    return "".join(str(key or "").split()).strip("\"'`")


def valid(key: str) -> bool:
    """Whether ``key`` looks like an OpenWeather API key: 32 letters (a-z, A-Z) and digits."""
    return re.fullmatch(r"[A-Za-z0-9]{32}", key or "") is not None


def save(key: str) -> None:
    global _read
    private.write(FILE, json.dumps({"key": key}))   # (yours alone on the Pi, from the moment it exists)
    with _lock:
        _read = (None, "")


def forget() -> None:
    global _read
    FILE.unlink(missing_ok=True)
    with _lock:
        _read = (None, "")
