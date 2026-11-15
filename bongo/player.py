"""Plays a playlist: each entry is one module + style + settings for N seconds."""
from __future__ import annotations

import datetime as dt
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
    times: list = field(default_factory=list)   # [{"from": "08:00", "to": "10:00"}, ...]; none: all day

    def plays_at(self, now) -> bool:
        """Whether this screen plays at ``now`` (by the clock on the wall): all day, or inside one of its times."""
        if not self.times:
            return True
        from bongo.schedule import within   # (schedule imports this module)
        return any(within(now, t["from"], t["to"]) for t in self.times)

    def build(self, modules: dict[str, Module], ctx: Context) -> Style:
        if self.module not in modules:
            raise ValueError(f"unknown module {self.module!r}")
        return modules[self.module].create(self.style, self.settings, ctx)


def play_entry(style: Style, seconds: float, panel, stop: threading.Event | None = None,
               clock=time.monotonic, place=None, live=None, overlays=None, hold: bool = True) -> int:
    """Render one entry until its time is up; returns frames shown.
    ``place`` puts the module's safe-area frame onto the full panel (see SafeArea.place).
    ``live`` (bongo.live.Live) can skip or hold the entry and sees every frame. ``hold`` False: Pause
    doesn't keep this one up (a pushed message shows for its time, then gives the paused screen back)."""
    stop = stop or threading.Event()
    frame_time = 1 / style.fps
    end = clock() + seconds
    shown = 0
    last, last_at = None, -1e9   # the style's latest frame: while a screen covers it, it keeps its own pace
    while not stop.is_set():
        held = bool(hold and live and live.paused)
        if clock() >= end and not held:
            break
        if live and live.skip.is_set():
            live.skip.clear()
            break
        if getattr(style, "finished", False) and not (held and shown):   # a style can hand over
            break   # early (Spotify idle, a splash played through); Pause holds one only once it has drawn
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
    An entry with ``times`` only plays inside them: it is looked at when its turn comes, so the one on the
    panel finishes its seconds. With nothing to play (every entry has times, none now) the panel is dark.
    ``overlays`` (bongo.overlays.Overlays) draw over the day playlist, not splash, sleep or pushes."""
    stop = stop or threading.Event()
    if live:
        live.idle = False
    day = [e for e in playlist if e.enabled]
    if not day:
        raise ValueError("playlist has no enabled entries")

    def run(entry: Entry, upcoming: Entry | None = None, index: int | None = None, extras=None,
            limit: float | None = None, hold: bool = True) -> bool:
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
            frames = play_entry(style, seconds, panel, stop, place=place, live=live, overlays=extras, hold=hold)
        except Exception:  # one broken module must not stop the panel
            log.exception("%s/%s failed while playing; moving on", entry.module, entry.style)
            return False
        gone = frames == 0 and getattr(style, "finished", False)   # it skipped itself (Spotify, nothing on)
        skipped[0] = skipped[0] + 1 if gone else 0
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
        level = schedule.sleep_brightness if asleep else schedule.day_brightness
        if level is None:
            return
        if hasattr(panel, "ramp_to"):  # day <-> night glides (bongo.fade)
            panel.ramp_to(level, schedule.fade_seconds, schedule.fade_curve)
        else:
            panel.set_brightness(level)

    def show_dark(wait):
        """A dark panel (forced sleep, or every screen skipped), still with the PIN, setup or address screen
        over it when one is up, so pairing and setup work then too; such a screen keeps its own pace."""
        dark = ctx_factory().canvas()
        covered = live.overlay(dark) if live and live.overlay else dark
        out = place(covered) if place else covered
        panel.show(out)
        if live:
            live.publish(out, public=place(dark) if place else dark)
        stop.wait(min(wait, 1 / 20) if covered is not dark else wait)
        if live:
            live.skip.clear()   # nothing was playing: a Wake or Next pressed meanwhile mustn't skip the next screen

    skipped = [0]   # screens in a row that skipped themselves without a frame
    def due(now, start):
        """Places in ``day`` that play at ``now``, in playing order from ``start``."""
        return [k % len(day) for k in range(start, start + len(day)) if day[k % len(day)].plays_at(now)]

    def after(index, entry, now):
        """What follows ``entry``: the next one that plays when it ends (itself, if it's the only one; None: nothing)."""
        later = due(now + dt.timedelta(seconds=entry.seconds), index + 1)
        return day[later[0]] if later else None

    boot = schedule.boot_entry() if schedule else None
    if boot:
        first = due(ctx_factory().now(), 0)
        run(boot, day[first[0]] if first else None)
    was_asleep = False
    position: dict[tuple, int] = {}  # where the sleep playlist left off
    cursor = 0                       # the place in ``day`` whose turn is next
    again = None                     # a place to play again whatever its times (see the paused push below)
    played = 0
    while not stop.is_set() and (loops is None or played < loops * len(day)):
        if live and live.reload.is_set():
            return  # the caller reloads the config and calls play() again
        now = ctx_factory().now()
        pushed = live.take_push() if live else None
        if pushed:
            run(pushed, hold=False)   # a message shows for its own time even while paused
            continue
        forced = bool(live and live.asleep(now))
        hours = bool(schedule and schedule.asleep(now))    # the sleep hours (Settings > Sleep)
        if live and live.awake and (forced or not hours):
            live.awake = False                             # those hours are over (or Sleep now): asleep as usual next time
        asleep = forced or (hours and not (live and live.awake))
        if was_asleep and not asleep and schedule:
            schedule.wake()  # tomorrow night picks a new Sleep screen
        was_asleep = asleep
        if live:
            live.sleeping, live.sleep_hours = asleep, hours
            if asleep:
                live.idle = False   # asleep, not "nothing set to play"
        brightness(now, asleep)
        if forced and not (schedule and schedule.sleep_playlist):
            show_dark(0.05 if getattr(panel, "ramping", False) else 1)  # dark until sleep_until or a wake
            continue
        if asleep:
            current = schedule.sleep_playlist
            key = tuple(map(id, current))  # the list is rebuilt each pass; its entries are not
            index = position.get(key, 0)
            position[key] = index + 1
            entry, upcoming, turns = current[index % len(current)], current[(index + 1) % len(current)], len(current)
            again = None
        else:
            now_due = due(now, cursor)   # by its times (Timing > When it plays), each time its turn comes
            if again is not None and live and live.paused:   # the paused screen a message cut in on: back to it,
                now_due = [again] + [k for k in now_due if k != again]   # even if its time has ended meanwhile
            again = None                 # (resumed since: it takes its turn by its times, like any other)
            if not now_due:              # every screen has times, and none of them is now: dark until one is
                if live:
                    live.idle = True
                    if live.entry is not None:
                        live.playing(None, None)
                played += 1
                show_dark(1)
                continue
            index = now_due[0]
            cursor = index + 1
            entry, upcoming, turns = day[index], after(index, day[index], now), len(now_due)
        if live:
            live.idle = False
        played += 1
        where = None if forced else next((i for i, e in enumerate(playlist) if e is entry), None)
        if forced:                                         # Sleep now: until the moment it ends
            limit = live.sleep_left(now)
        else:
            limit = schedule.seconds_until_change(now) if schedule else None
        ran = run(entry, upcoming, where, None if asleep else overlays,
                  limit)   # where: slot in the saved playlist; limit: stop when sleep starts/ends
        if live and live.paused and live.pushes:   # a message cut in on the paused screen: come back to it after
            played -= 1
            if asleep:
                position[key] = index
            else:
                cursor = again = index
        if not ran:
            stop.wait(0.2)  # never spin if every entry is broken
        elif skipped[0] >= turns:   # every screen skipped itself (Spotify, nothing on): dark, not a spin
            show_dark(1)
