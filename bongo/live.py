"""Live state shared by the player thread and the dashboard.

The player reports what it is showing and publishes every frame here; the dashboard
reads that and asks for changes (skip, pause, push a message, sleep now, brightness).
Everything is guarded by one lock, and nothing here blocks the player.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import threading
import time
from dataclasses import asdict

from PIL import Image


class Live:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame: Image.Image | None = None
        self._public: Image.Image | None = None   # the same without covering screens (bongo/hub.py)
        self.entry = None           # the Entry on the panel now
        self.upcoming = None        # the Entry after it
        self.started = 0.0          # time.time() it started
        self.skip = threading.Event()
        self.paused = False
        self.sleep_until: dt.datetime | None = None
        self.brightness: int | None = None  # dashboard override; None follows the schedule
        self.pushes: collections.deque = collections.deque(maxlen=5)
        self.overlay = None         # callable(frame) -> frame, e.g. the pairing PIN
        self.index = None           # playlist position on now (None for splash, sleep, pushes)
        self.reload = threading.Event()  # config saved: the player restarts with it

    # -- player side -------------------------------------------------------------------
    def playing(self, entry, upcoming, index=None) -> None:
        with self._lock:
            self.entry, self.upcoming, self.started, self.index = entry, upcoming, time.time(), index

    def publish(self, frame: Image.Image, public: Image.Image | None = None) -> None:
        """``frame``: what the panel shows (the paired dashboard's preview). ``public``: the same without
        any covering screen (PIN, setup password, address), for the hub; defaults to ``frame``."""
        with self._lock:
            self._frame = frame
            self._public = frame if public is None else public

    def take_push(self):
        with self._lock:
            return self.pushes.popleft() if self.pushes else None

    def asleep(self, now: dt.datetime) -> bool:
        until = self.sleep_until
        if until is None:
            return False
        if until.tzinfo is None and now.tzinfo is not None:
            until = until.replace(tzinfo=now.tzinfo)
        if now.tzinfo is None and until.tzinfo is not None:
            now = now.replace(tzinfo=until.tzinfo)
        if now >= until:
            self.sleep_until = None
            return False
        return True

    # -- dashboard side ----------------------------------------------------------------
    def frame_png(self, public: bool = False) -> bytes | None:
        """The latest frame as PNG; ``public``: never with the PIN, setup or address screen on it."""
        with self._lock:
            frame = self._public if public else self._frame
        if frame is None:
            return None
        out = io.BytesIO()
        frame.save(out, "PNG")
        return out.getvalue()

    def apply_config(self) -> None:
        """Ask the player to restart from the saved config (safe area, playlist, schedule)."""
        self.reload.set()
        self.skip.set()

    def push(self, entry) -> None:
        with self._lock:
            self.pushes.append(entry)
        self.skip.set()  # interrupt whatever is on now

    def status(self) -> dict:
        with self._lock:
            entry, upcoming, started, index = self.entry, self.upcoming, self.started, self.index
        until = self.sleep_until  # read once: the player may clear it meanwhile
        def describe(e):
            return None if e is None else {k: v for k, v in asdict(e).items() if k != "enabled"}
        return {
            "entry": describe(entry), "next": describe(upcoming), "index": index,
            "elapsed": round(time.time() - started, 1) if entry else 0,
            "paused": self.paused, "brightness": self.brightness,
            "sleep_until": until.isoformat() if until else None,
            "pushes": len(self.pushes),
        }
