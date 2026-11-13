"""Plays a playlist: each entry is one module + style + settings for N seconds."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from bongo.module import Context, Module, Style

log = logging.getLogger(__name__)


ONCE_SLACK = 5.0   # seconds a play-once entry may run past its expected length before it's stopped anyway

@dataclass
class Entry:
    module: str
    style: str | None = None
    seconds: float = 30
    settings: dict = field(default_factory=dict)
    enabled: bool = True
    once: bool = False   # play through once and hand over (splash, boot); ``seconds`` is then only a cap

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
    last, last_at = None, -1e9   # the style's latest frame: while a screen covers it, it keeps its own pace
    while not stop.is_set():
        if clock() >= end and not (live and live.paused):
            break
        if live and live.skip.is_set():
            live.skip.clear()
            break
        if getattr(style, "finished", False) and not (live and live.paused):   # a style can hand over early
            break                                                                # (Spotify idle, a splash played through)
        started = clock()
        now = style.ctx.now()
        if last is None or started - last_at >= frame_time - 1e-3:
            last, last_at = style.render(now), started
            if overlays:
                last = overlays(last, now)
        frame = last
        wait = frame_time
        under = frame             # what's playing, without any covering screen: the hub's public picture
        if live and live.overlay:
            covered = live.overlay(frame)
            if covered is not frame:   # a screen over the style (PIN, address) scrolls at 20 fps whatever the style's rate;
                frame, wait = covered, min(frame_time, 1 / 20)   # the style underneath still renders at its own
            else:
                last = None            # uncovered: every pass renders, exactly as before
        out = place(frame) if place else frame
        panel.show(out)
        if live:
            live.publish(out, public=out if under is frame else (place(under) if place else under))
        shown += 1
        stop.wait(max(0.0, wait - (clock() - started)))
    return shown


def play(playlist: list[Entry], modules: dict[str, Module], panel, ctx_factory=Context,
         stop: threading.Event | None = None, loops: int | None = None, place=None,
         schedule=None, live=None, overlays=None) -> None:
    """Cycle the playlist until ``stop`` (or ``loops`` passes). A ``Schedule`` plays its boot screen
    at start-up, fills ``splash`` items from the Splash pool and swaps in the sleep playlist, at
    sleep brightness, during its hours (an entry is cut short when sleep starts or ends). ``live``
    lets the dashboard skip, pause, push a message, force sleep and override brightness.
    ``overlays`` (bongo.overlays.Overlays) draw over the day playlist, not splash, sleep or pushes."""
    stop = stop or threading.Event()
    day = [e for e in playlist if e.enabled]
    if not day:
        raise ValueError("playlist has no enabled entries")

    def run(entry: Entry, upcoming: Entry | None = None, index: int | None = None, extras=None,
            limit: float | None = None) -> bool:
        shown = entry
        if entry.module == "splash":  # a Splash item: the next file from the Splash pool
            entry = schedule.splash if schedule else None
            if entry is None:
                return False
            extras = None  # overlays never cover the splash
        try:
            style = entry.build(modules, ctx_factory())
        except Exception:
            log.exception("skipping playlist entry %s", entry.module)
            return False
        style.once = entry.once
        # a GIF playing once hands over itself; its expected length is only a cap, with room for a slow Pi
        moving = entry.once and getattr(style, "moving", False)
        cap = entry.seconds + ONCE_SLACK if moving else entry.seconds
        seconds = cap if limit is None else max(0.0, min(cap, limit))
        if live:
            live.playing(shown, upcoming, index)
        try:
            play_entry(style, seconds, panel, stop, place=place, live=live, overlays=extras)
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

    boot = schedule.boot_entry() if schedule else None
    if boot:
        run(boot, day[0])
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
        limit = None if forced or not schedule else schedule.seconds_until_change(now)
        if not run(entry, current[(index + 1) % len(current)], where, None if asleep else overlays,
                   limit):  # where: slot in the saved playlist; limit: stop when sleep starts/ends
            stop.wait(0.2)  # never spin if every entry is broken
