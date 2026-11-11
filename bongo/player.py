"""Plays a playlist: each entry is one module + style + settings for N seconds."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from bongo.module import Context, Module, Style

log = logging.getLogger(__name__)


@dataclass
class Entry:
    module: str
    style: str | None = None
    seconds: float = 30
    settings: dict = field(default_factory=dict)
    enabled: bool = True

    def build(self, modules: dict[str, Module], ctx: Context) -> Style:
        if self.module not in modules:
            raise ValueError(f"unknown module {self.module!r}")
        return modules[self.module].create(self.style, self.settings, ctx)


def play_entry(style: Style, seconds: float, panel, stop: threading.Event | None = None,
               clock=time.monotonic, place=None, live=None, overlays=None) -> int:
    """Render one entry until its time is up; returns frames shown.
    ``place`` puts the module's safe-area frame onto the full panel (see SafeArea.place).
    ``live`` (bongo.live.Live) can skip or hold the entry and sees every frame."""
    stop = stop or threading.Event()
    frame_time = 1 / style.fps
    end = clock() + seconds
    shown = 0
    while not stop.is_set():
        if clock() >= end and not (live and live.paused):
            break
        if live and live.skip.is_set():
            live.skip.clear()
            break
        started = clock()
        now = style.ctx.now()
        frame = style.render(now)
        if overlays:
            frame = overlays(frame, now)
        if live and live.overlay:
            frame = live.overlay(frame)
        out = place(frame) if place else frame
        panel.show(out)
        if live:
            live.publish(out)
        shown += 1
        stop.wait(max(0.0, frame_time - (clock() - started)))
    return shown


def play(playlist: list[Entry], modules: dict[str, Module], panel, ctx_factory=Context,
         stop: threading.Event | None = None, loops: int | None = None, place=None,
         schedule=None, live=None, overlays=None) -> None:
    """Cycle the playlist until ``stop`` (or ``loops`` passes). A ``Schedule`` adds a splash at
    start-up and swaps in the sleep playlist, at sleep brightness, during its hours. ``live``
    lets the dashboard skip, pause, push a message, force sleep and override brightness.
    ``overlays`` (bongo.overlays.Overlays) draw over the day playlist, not splash, sleep or pushes."""
    stop = stop or threading.Event()
    day = [e for e in playlist if e.enabled]
    if not day:
        raise ValueError("playlist has no enabled entries")

    def run(entry: Entry, upcoming: Entry | None = None, index: int | None = None, extras=None) -> bool:
        try:
            style = entry.build(modules, ctx_factory())
        except Exception:
            log.exception("skipping playlist entry %s", entry.module)
            return False
        if live:
            live.playing(entry, upcoming, index)
        try:
            play_entry(style, entry.seconds, panel, stop, place=place, live=live, overlays=extras)
        except Exception:  # one broken module must not stop the panel
            log.exception("%s/%s failed while playing; moving on", entry.module, entry.style)
            return False
        return True

    def brightness(now, forced_sleep):
        if not hasattr(panel, "set_brightness"):
            return
        if live and live.brightness is not None:  # the dashboard's override: straight there
            panel.set_brightness(live.brightness)
            return
        if not schedule:
            return
        asleep = forced_sleep and schedule.sleep_brightness is not None
        level = schedule.sleep_brightness if asleep else schedule.brightness(now)
        if level is None:
            return
        if hasattr(panel, "ramp_to"):  # day <-> night glides (bongo.fade)
            panel.ramp_to(level, schedule.fade_seconds, schedule.fade_curve)
        else:
            panel.set_brightness(level)

    splash = schedule.splash if schedule else None  # each read takes the next file from the bag
    if splash:
        run(splash, day[0])
    was_asleep = False
    position: dict[tuple, int] = {}  # where each playlist (day or sleep) left off
    played = 0
    while not stop.is_set() and (loops is None or played < loops * len(day)):
        if live and live.reload.is_set():
            return  # the caller reloads the config and calls play() again
        now = ctx_factory().now()
        pushed = live.take_push() if live else None
        if pushed:
            run(pushed)
            continue
        forced = bool(live and live.asleep(now))
        asleep = forced or bool(schedule and schedule.asleep(now))
        if was_asleep and not asleep and schedule:
            schedule.wake()  # tomorrow night picks a new Sleep screen
        was_asleep = asleep
        brightness(now, forced)
        if forced and not (schedule and schedule.sleep_playlist):
            dark = ctx_factory().canvas()
            panel.show(place(dark) if place else dark)
            stop.wait(0.05 if getattr(panel, "ramping", False) else 1)  # dark until sleep_until or a wake
            continue
        if forced:
            current = schedule.sleep_playlist
        else:
            current = [e for e in (schedule.playlist(now, day) if schedule else day) if e.enabled] or day
        key = tuple(map(id, current))  # the list is rebuilt each pass; its entries are not
        index = position.get(key, 0)
        position[key] = index + 1
        played += 1
        entry = current[index % len(current)]
        where = None if forced else next((i for i, e in enumerate(playlist) if e is entry), None)
        if not run(entry, current[(index + 1) % len(current)], where, None if asleep else overlays):  # where: slot in the saved playlist
            stop.wait(0.2)  # never spin if every entry is broken
