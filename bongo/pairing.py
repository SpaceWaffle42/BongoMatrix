"""PIN pairing for the dashboard, as in v2: the panel shows a PIN, the browser types it in.

Paired browsers get a long random token in a cookie; only its SHA-256 is stored. Five wrong
PINs lock pairing for a minute. A new PIN is drawn after every successful pairing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

MAX_DEVICES = 8
PIN_SECONDS = 120
LOCK_AFTER, LOCK_SECONDS = 5, 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Pairing:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        self.pin = self._new_pin()
        self.showing_until = 0.0
        self.failures, self.locked_until = 0, 0.0

    @staticmethod
    def _new_pin() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    # -- stored devices ------------------------------------------------------------------
    def devices(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, devices: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(devices, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- the flow ------------------------------------------------------------------------
    def show_pin(self) -> None:
        """Put a PIN on the panel for PIN_SECONDS. A fresh PIN each time it appears; pressing
        again while it's up just keeps it there, so nobody can swap it under someone typing."""
        with self._lock:
            if not self.showing:
                self.pin = self._new_pin()
            self.showing_until = self.clock() + PIN_SECONDS

    @property
    def showing(self) -> bool:
        return self.clock() < self.showing_until

    def pair(self, pin: str, name: str) -> str:
        """Returns a new device token, or raises PermissionError with a readable reason."""
        with self._lock:
            now = self.clock()
            if now < self.locked_until:
                raise PermissionError(f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1} s.")
            if not self.showing:  # the PIN only works while it's on the panel
                raise PermissionError("Show a PIN on the panel first, then type it here.")
            if not secrets.compare_digest(str(pin).replace(" ", "").encode(), self.pin.encode()):
                self.failures += 1
                if self.failures >= LOCK_AFTER:
                    self.failures, self.locked_until = 0, now + LOCK_SECONDS
                    raise PermissionError(f"Too many wrong PINs. Pairing is paused for {LOCK_SECONDS} s.")
                raise PermissionError(f"That PIN didn't match. {LOCK_AFTER - self.failures} tries left.")
            devices = self.devices()
            if len(devices) >= MAX_DEVICES:
                raise PermissionError(f"This display already has {MAX_DEVICES} paired devices. Remove one first.")
            token = secrets.token_urlsafe(32)
            stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            devices.append({"id": secrets.token_hex(4), "name": (name or "Browser").strip()[:40],
                            "hash": _hash(token), "paired": stamp, "seen": stamp})
            self._save(devices)
            self.failures, self.pin, self.showing_until = 0, self._new_pin(), 0.0
            return token

    def check(self, token: str | None) -> dict | None:
        if not token:
            return None
        if len(token) > 100:
            return None
        digest = _hash(token)
        with self._lock:
            devices = self.devices()
            device = next((d for d in devices if secrets.compare_digest(d["hash"], digest)), None)
            if device:
                now = dt.datetime.now(dt.timezone.utc)
                if now - dt.datetime.fromisoformat(device["seen"]) > dt.timedelta(minutes=5):
                    device["seen"] = now.isoformat(timespec="seconds")  # coarse, so the SD card isn't written per request
                    self._save(devices)
            return device

    def remove(self, device_id: str) -> bool:
        with self._lock:
            devices = self.devices()
            kept = [d for d in devices if d["id"] != device_id]
            if len(kept) == len(devices):
                return False
            self._save(kept)
            return True

    # -- what the panel shows while pairing ----------------------------------------------
    def overlay(self, fonts):
        """A Live.overlay: while a PIN is requested, the panel shows PAIR and the PIN."""
        def draw(frame: Image.Image) -> Image.Image:
            if not self.showing:
                return frame
            w, h = frame.size
            img = Image.new("RGB", (w, h), (7, 9, 12))
            small, big = fonts("5x7"), fonts("7x13B" if w >= 52 else "6x10")
            pin = f"{self.pin[:3]} {self.pin[3:]}" if big.width(self.pin + " ") <= w - 2 else self.pin
            top = max(0, (h - 22) // 2)
            small.draw(img, (w - small.width("PAIR")) // 2, top + 7, "PAIR", (143, 180, 255))
            big.draw(img, (w - big.width(pin)) // 2, top + 21, pin, (255, 227, 179))
            return img
        return draw
