"""Live state shared by the player thread and the dashboard.

The player reports what it is showing and publishes every frame here; the dashboard
reads that and asks for changes (skip, pause, push a message, sleep now, brightness).
Everything is guarded by one lock, and nothing here blocks the player.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import logging
import os
import threading
import time
from dataclasses import asdict

from PIL import Image


FROZEN_SECONDS = 180   # no frame for this long means the player is stuck (the slowest honest wait, a video
                       # being decoded on a Pi Zero, is capped at 120 s)


class Live:
    def __init__(self):
        self._lock = threading.Lock()
        self.beat_at = time.monotonic()   # the player's last sign of life: every frame, and ``beat``
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
        """``frame``: what the panel shows (nothing on the web reads it). ``public``: the same without any
        covering screen (PIN, setup password, address), for the hub and the dashboard's preview; defaults
        to ``frame``."""
        with self._lock:
            self._frame = frame
            self._public = frame if public is None else public
        self.beat_at = time.monotonic()

    def beat(self) -> None:
        """The player is alive though it has no frame to show (waiting for a config that can play)."""
        self.beat_at = time.monotonic()

    def watch(self, stop: threading.Event, limit: float = FROZEN_SECONDS, every: float = 5.0, frozen=None) -> None:
        """The watchdog, on its own thread. A style that never returns freezes the panel while the
        dashboard carries on as if all were well, and saving a fix can't reach a stuck player. So
        after ``limit`` seconds without a frame the process ends, and systemd starts it again."""
        while not stop.wait(every):
            quiet = time.monotonic() - self.beat_at
            if quiet > limit:
                on, after = self.entry, self.upcoming
                logging.getLogger(__name__).critical(
                    "the panel has shown nothing for %d s (on: %s, next: %s); restarting the display", quiet,
                    f"{on.module}/{on.style}" if on else "nothing", f"{after.module}/{after.style}" if after else "nothing")
                (frozen or (lambda: os._exit(70)))()   # not sys.exit: the stuck thread would never let it finish
                return

    def take_push(self):
        with self._lock:
            if not self.pushes:
                return None
            self.skip.clear()   # the skip that came with it has done its job: it mustn't end the push itself
            return self.pushes.popleft()   # (with nothing playing, as in a dark Sleep now, nobody else clears it)

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
        """The latest frame as PNG; ``public``: never with the PIN, setup or address screen on it (what
        every web page shows: the hub and the dashboard's preview alike; only the panel shows those)."""
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
