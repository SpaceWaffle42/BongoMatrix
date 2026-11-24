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
import secrets
import threading
import time
from dataclasses import asdict

from PIL import Image

from bongo.panelpreview import PanelPreview


FROZEN_SECONDS = 180   # no frame for this long means the player is stuck (the slowest honest wait, a video
                       # being decoded on a Pi Zero, is capped at 120 s)


ACT_KEEPS = 2.0        # s a pressed button waits for the screen's next frame before it is forgotten


class Live:
    def __init__(self):
        self._lock = threading.Lock()
        self.beat_at = time.monotonic()   # the player's last sign of life: every frame, and ``beat``
        self.shown = 0                    # frames the player has shown since the start (bongo/__main__.py: ``keep_playing``)
        self.frame_at = None              # when the last of them was shown (``health``)
        self.boot = secrets.token_hex(4)  # this run of the display: a restart is a new one (see ``health``)
        self.panel_up = False             # the panel has started, so frames can be shown
        self._frame: Image.Image | None = None
        self._public: Image.Image | None = None   # the same without covering screens (bongo/hub.py)
        self.entry = None           # the Entry on the panel now
        self.upcoming = None        # the Entry after it
        self.started = 0.0          # time.time() it started
        self.skip = threading.Event()
        self.paused = False
        self.sleep_until: dt.datetime | None = None
        self.awake = False          # Wake pressed during the sleep hours: awake until they end (the player clears it)
        self.sleeping = False       # what the player is doing now (Sleep now, or the sleep hours)
        self.sleep_hours = False    # whether it's the sleep hours now (Settings > Sleep), awake or not
        self.woke_early = False     # Wake was pressed in the sleep hours that have just ended: the panel is up already,
        #                             so no gentle rise after them (Follow daylight; the player clears it when it next sleeps)
        self.idle = False           # nothing is set to play now (every screen has times, and none is now)
        self.brightness: int | None = None  # set by hand (``set_brightness``); None follows the schedule
        self.brightness_until: dt.datetime | None = None   # when that ends by itself (None: when it is cleared)
        self._brightness_new = False        # set by hand since the player last looked: it says how long it lasts
        self._brightness_given = False      # ...unless whoever set it gave its end (kept whatever the settings say)
        self.level: int | None = None       # the brightness the player has the panel at, or gliding to (by hand or not)
        self.staying = False        # the screen on now says it is live, so it keeps the panel past its time (Style.stay)
        self.stays: dict = {}       # module -> [(when, seconds kept)]: what each has had of its allowance (bongo/player.py)
        self.pushes: collections.deque = collections.deque(maxlen=5)
        self.overlay = None         # callable(frame) -> frame, e.g. the pairing PIN
        self.index = None           # playlist position on now (None for splash, sleep, pushes)
        self.reload = threading.Event()  # config saved: the player restarts with it
        self.acts: collections.deque = collections.deque(maxlen=8)   # (module, name, when): buttons pressed (Module.actions)
        # the screen editor's "Preview on the panel": an unsaved draft the player draws over the playlist for a while
        self.preview = PanelPreview(on_end=self._preview_ended)
        self.mask = None            # callable(entry as a dict) -> the same without its secret settings (bongo/standins.py):
        #                             set by the dashboard, so nothing ``status`` says can carry one

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
        self.beat_at = self.frame_at = time.monotonic()
        self.shown += 1

    def beat(self) -> None:
        """The player is alive though it has no frame to show (waiting for a config that can play)."""
        self.beat_at = time.monotonic()

    def health(self) -> dict:
        """For the updater (bongo/update.py): which run this is, whether the panel started, and how
        long since the player last showed a frame. The dashboard answers before the panel starts, so
        an answer alone doesn't mean the display works."""
        return {"boot": self.boot, "panel": self.panel_up, "quiet": round(time.monotonic() - self.beat_at, 1),
                # frames shown by this run, and how long since the last (None: never one). "quiet" also counts
                # the player saying it's alive with nothing to show, so it can't tell a dark panel from a working one
                "frames": self.shown, "frame_age": None if self.frame_at is None else round(time.monotonic() - self.frame_at, 1)}

    def watch(self, stop: threading.Event, limit: float = FROZEN_SECONDS, every: float = 5.0, frozen=None,
              notify=None) -> None:
        """The watchdog, on its own thread. A style that never returns freezes the panel while the
        dashboard carries on as if all were well, and saving a fix can't reach a stuck player. So
        after ``limit`` seconds without a frame the process ends, and systemd starts it again.
        While the player is alive it tells systemd so (``WATCHDOG=1``, bongo/sdnotify.py): if this whole
        process hangs, this thread included, systemd restarts it (``WatchdogSec=`` in the unit)."""
        if notify is None:
            from bongo import sdnotify
            notify = sdnotify.notify
        while not stop.wait(every):
            quiet = time.monotonic() - self.beat_at
            if quiet > limit:
                on, after = self.entry, self.upcoming
                logging.getLogger(__name__).critical(
                    "the panel has shown nothing for %d s (on: %s, next: %s); restarting the display", quiet,
                    f"{on.module}/{on.style}" if on else "nothing", f"{after.module}/{after.style}" if after else "nothing")
                (frozen or (lambda: os._exit(70)))()   # not sys.exit: the stuck thread would never let it finish
                return
            notify("WATCHDOG=1")

    def take_acts(self) -> list:
        """The buttons pressed for the module on the panel now, in order. Ones for any other module are dropped,
        and so are stale ones (pressed while nothing was drawing: a dark panel), so nothing happens long after."""
        if not self.acts:
            return []
        with self._lock:
            on = self.entry.module if self.entry else None
            fresh = time.monotonic() - ACT_KEEPS
            names = [name for module, name, when in self.acts if module == on and when >= fresh]
            self.acts.clear()
        return names

    def take_push(self):
        if self.pushes and self.preview.active:
            return None         # a draft is being previewed on the panel: the message waits until that ends
        with self._lock:
            if not self.pushes:
                return None
            self.skip.clear()   # the skip that came with it has done its job: it mustn't end the push itself
            return self.pushes.popleft()   # (with nothing playing, as in a dark Sleep now, nobody else clears it)

    def sleep_left(self, now: dt.datetime) -> float | None:
        """Seconds of Sleep now left (None: not asleep that way)."""
        until = self.sleep_until
        if until is None:
            return None
        # A naive time is the Pi's own local time (ctx.now() with no timezone set), so astimezone() reads it
        # right; giving it the other's zone would be off by the Pi's UTC offset (hours, in Berlin or New York).
        if (until.tzinfo is None) != (now.tzinfo is None):
            until, now = until.astimezone(), now.astimezone()
        return (until - now).total_seconds()

    def asleep(self, now: dt.datetime) -> bool:
        left = self.sleep_left(now)
        if left is None:
            return False
        if left <= 0:
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
        self.preview.end("saved")   # a previewed draft was built for the config as it was: Save ends it
        self.reload.set()
        self.skip.set()

    def act(self, module: str, name: str) -> None:
        """A button of ``module`` was pressed: its screen on the panel hears of it before its next frame."""
        with self._lock:
            self.acts.append((module, name, time.monotonic()))

    def set_brightness(self, level: int | None, until: dt.datetime | None = None) -> None:
        """Hold the panel at ``level`` (1 to 100) whatever the schedule says; None: follow the schedule again.
        This is how anything sets the brightness by hand: the dashboard's slider, or a module passing on a
        dimmer from somewhere else. ``until``: the moment it ends by itself (an aware time). Left out, the
        player decides: with Follow daylight on, the next sunrise, sunset or start or end of the sleep hours;
        without, it stays until it's cleared."""
        with self._lock:
            self.brightness = None if level is None else max(1, min(100, int(level)))
            self.brightness_until = until if level is not None else None
            self._brightness_given = level is not None and until is not None
            self._brightness_new = level is not None and until is None

    def rules_changed(self) -> None:
        """The settings that say how long a brightness set by hand lasts may have changed (the player has started
        with a saved config: Follow daylight switched on or off, new sleep hours, another place). One that is
        held, and whose end nobody gave, gets its end worked out again by the rule now in force, from now."""
        with self._lock:
            if self.brightness is not None and not self._brightness_given:
                self._brightness_new = True

    def held_brightness(self, now: dt.datetime, ends=None) -> int | None:
        """Player side: the brightness set by hand that holds at ``now`` (None: none, or it has just ended).
        ``ends(now)``: when one set without an end is to end (None: not by itself)."""
        with self._lock:
            if self.brightness is None:
                return None
            if self._brightness_new:
                self._brightness_new = False
                self.brightness_until = ends(now) if ends else None
            until = self.brightness_until
            if until is not None:
                if (until.tzinfo is None) != (now.tzinfo is None):   # (as in ``sleep_left``: a naive time is the Pi's own)
                    until, now = until.astimezone(), now.astimezone()
                if now >= until:
                    self.brightness = self.brightness_until = None
            return self.brightness

    def push(self, entry) -> None:
        held = self.preview.active   # (a draft on the panel: the message shows when that ends, see take_push)
        with self._lock:
            self.pushes.append(entry)
            if not held:
                self.skip.set()  # interrupt whatever is on now
        if held and not self.preview.active:
            self._preview_ended()    # (it ended just then: nobody else would bring the message on)

    def _preview_ended(self) -> None:
        """The panel is the playlist's again: a message pushed meanwhile cuts in now, as it would have."""
        if self.pushes:
            self.skip.set()

    def status(self) -> dict:
        with self._lock:
            entry, upcoming, started, index = self.entry, self.upcoming, self.started, self.index
        until = self.sleep_until  # read once: the player may clear it meanwhile
        by_hand, hand_until = self.brightness, self.brightness_until
        def describe(e):
            if e is None:
                return None
            told = {k: v for k, v in asdict(e).items() if k != "enabled"}
            return self.mask(told) if self.mask else told
        return {
            "entry": describe(entry), "next": describe(upcoming), "index": index,
            "elapsed": round(time.time() - started, 1) if entry else 0,
            "paused": self.paused, "brightness": by_hand,
            # brightness_until: when the one set by hand ends by itself; level: what the panel is at, by hand or not
            "brightness_until": hand_until.isoformat() if by_hand is not None and hand_until else None,
            "level": self.level,
            "sleep_until": until.isoformat() if until else None,
            "asleep": bool(until) or self.sleeping,
            "sleep_hours": self.sleep_hours,
            "idle": self.idle,
            "staying": self.staying,
            "pushes": len(self.pushes),
            "preview": self.preview.public(),   # a draft from the editor is on the panel: who, and which screen
        }
