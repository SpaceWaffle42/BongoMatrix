"""PIN pairing for the dashboard, as in v2: the panel shows a PIN, the browser types it in.

Paired browsers get a long random token in a cookie; only its SHA-256 is stored. Five wrong PINs
from one address stop that address trying for a minute (nobody else: a stranger on the network can't
lock the owner out). After GUESSES wrong PINs in all, from anywhere, a new PIN is drawn and the panel
shows it. A new PIN is drawn after every successful pairing.

Asking for the PIN needs no pairing (how else would a first browser get in?), so anything on the
network can ask. It can't keep the panel covered that way: one showing lasts PIN_MOST at the most
however often it's asked for, and each showing that ends with nobody pairing makes strangers wait
longer before the next (REST_SECONDS, doubling up to REST_MOST). A paired browser never waits.
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
PIN_MOST = 300                       # one showing, however many times it is asked for again
REST_SECONDS, REST_MOST = 30, 3600   # between showings nobody paired from: 30 s, doubling each time, to an hour
FORGET_SECONDS = 6 * 3600            # ...counted afresh once the panel has been left alone this long
                                     # (all of it is forgotten when the display restarts: an owner is never shut out)
LOCK_AFTER, LOCK_SECONDS = 5, 60     # wrong PINs from one address, and how long that address then waits
GUESSES = 20                         # wrong PINs at one PIN, from all addresses together, before a new one is drawn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Pairing:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        self.pin = self._new_pin()
        self.showing_until = 0.0
        self.shown_at = 0.0     # when this showing began (it can be kept up, but only to shown_at + PIN_MOST)
        self.wasted = 0         # showings in a row that ended with nobody pairing
        self.rest_until = 0.0   # ...and so when a browser that isn't paired may next ask
        self.wrong: dict[str, list] = {}   # address -> [wrong PINs in a row, when it may try again]
        self.guessed = 0                    # wrong PINs at the one showing now, from anyone

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
    def show_pin(self, paired: bool = True) -> float:
        """Put a PIN on the panel for PIN_SECONDS. A fresh PIN each time it appears; pressing
        again while it's up just keeps it there (to PIN_MOST in all), so nobody can swap it under
        someone typing. Returns 0, or the seconds a browser that isn't ``paired`` must wait first:
        the panel isn't anyone's to cover for as long as they like."""
        with self._lock:
            now = self.clock()
            if not self.showing:
                if self.showing_until:                       # the last showing ran out with nobody pairing
                    ended = self.showing_until
                    self.wasted = 1 if now - ended > FORGET_SECONDS else self.wasted + 1
                    self.showing_until = 0.0
                    self.rest_until = ended + min(REST_MOST, REST_SECONDS * 2 ** (self.wasted - 1))
                if not paired and now < self.rest_until:
                    return self.rest_until - now
                if now - self.rest_until > FORGET_SECONDS:
                    self.wasted = 0
                self.pin, self.shown_at, self.guessed = self._new_pin(), now, 0
            self.showing_until = max(self.showing_until, min(now + PIN_SECONDS, self.shown_at + PIN_MOST))
            return 0.0

    @property
    def showing(self) -> bool:
        return self.clock() < self.showing_until

    def pair(self, pin: str, name: str, who: str = "") -> str:
        """Returns a new device token, or raises PermissionError with a readable reason. ``who``: the
        address asking, so its wrong PINs count against it and nobody else."""
        with self._lock:
            now = self.clock()
            mine = self.wrong.get(who) or [0, 0.0]
            if now < mine[1]:
                raise PermissionError(f"Too many wrong PINs. Try again in {int(mine[1] - now) + 1} s.")
            if not self.showing:  # the PIN only works while it's on the panel
                raise PermissionError("Show a PIN on the panel first, then type it here.")
            if not secrets.compare_digest(str(pin).replace(" ", "").encode(), self.pin.encode()):
                if len(self.wrong) > 200:    # (never grows without end: the ones no longer waiting are forgotten)
                    self.wrong = {k: v for k, v in self.wrong.items() if v[1] > now}
                mine[0] += 1
                self.wrong[who] = mine
                self.guessed += 1
                if self.guessed >= GUESSES:   # enough guesses at this one, whoever made them: the panel shows the next
                    self.pin, self.guessed = self._new_pin(), 0
                if mine[0] >= LOCK_AFTER:
                    mine[0], mine[1] = 0, now + LOCK_SECONDS
                    raise PermissionError(f"Too many wrong PINs. Pairing is paused for {LOCK_SECONDS} s.")
                raise PermissionError(f"That PIN didn't match. {LOCK_AFTER - mine[0]} tries left.")
            devices = self.devices()
            if len(devices) >= MAX_DEVICES:
                raise PermissionError(f"This display already has {MAX_DEVICES} paired devices. Remove one first.")
            token = secrets.token_urlsafe(32)
            stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            devices.append({"id": secrets.token_hex(4), "name": (name or "Browser").strip()[:40],
                            "hash": _hash(token), "paired": stamp, "seen": stamp})
            self._save(devices)
            self.wrong.pop(who, None)
            self.pin, self.showing_until, self.guessed = self._new_pin(), 0.0, 0
            self.wasted, self.rest_until = 0, 0.0
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
