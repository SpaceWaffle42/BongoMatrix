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
    looping: bool = False   # loop even a GIF made to play once (Sleep: shown again and again in whole passes)
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


SAY_AGAIN = 600.0  # s before a screen that is still broken is mentioned in the log again (one line)
OVERRUN = 120.0   # s a busy screen (a line still scrolling) may stay past its time: a backstop, never reached normally


def busy(style) -> bool:
    """Whether a style that has had its time is still in the middle of something (``Style.busy``)."""
    try:
        return bool(getattr(style, "busy", False))
    except Exception:   # a broken ``busy`` mustn't look like the screen failing: it has simply had its time
        log.exception("%s: busy failed", type(style).__name__)
        return False


def play_entry(style: Style, seconds: float, panel, stop: threading.Event | None = None,
               clock=time.monotonic, place=None, live=None, overlays=None, hold: bool = True,
               overrun: float = OVERRUN, until: float | None = None, lit=None) -> int:
    """Render one entry until its time is up; returns frames shown.
    ``place`` puts the module's safe-area frame onto the full panel (see SafeArea.place).
    ``live`` (bongo.live.Live) can skip or hold the entry and sees every frame. ``hold`` False: Pause
    doesn't keep this one up (a pushed message shows for its time, then gives the paused screen back).
    ``overrun``: how long past its time a ``busy`` style may stay (0: the time is a hard stop).
    ``until``: seconds to a hard end that even Pause doesn't hold (sleep starting or ending).
    ``lit(covered)``: told each frame whether a covering screen (PIN, setup, address) is up, so the panel can
    be bright enough to read it at night."""
    stop = stop or threading.Event()
    frame_time = 1 / style.fps
    end = clock() + seconds
    hard = None if until is None else clock() + until
    try:
        style.hand_over_at = style.ctx.now().timestamp() + seconds   # (Style.hand_over_at: nothing new started after this holds it up)
    except Exception:
        pass
    shown = 0
    last, last_at = None, -1e9   # the style's latest frame: while a screen covers it, it keeps its own pace
    while not stop.is_set():
        held = bool(hold and live and live.paused)
        if held and getattr(style, "hand_over_at", None) is not None:
            style.hand_over_at = None   # paused: no end in sight, so what takes turns carries on taking them
        elif not held and hold and live and getattr(style, "hand_over_at", 0) is None:
            try:                        # unpaused: the end is back (now, if its time has already gone)
                style.hand_over_at = style.ctx.now().timestamp() + max(0.0, end - clock())
            except Exception:
                pass
        if hard is not None and clock() >= hard:
            break   # sleep starts or ends: paused or not, the player moves on to it
        if clock() >= end and not held:
            if not (clock() < end + min(overrun, getattr(style, "most_overrun", overrun)) and busy(style)):
                break   # (busy: a line is still scrolling, so it gets to finish)
        if live and live.skip.is_set():
            live.skip.clear()
            style.nexted = True   # (Next, not the screen: ``run`` doesn't count it as skipping itself)
            break
        if getattr(style, "finished", False) and not (held and shown):   # a style can hand over
            break   # early (Spotify idle, a splash played through); Pause holds one only once it has drawn
        started = clock()
        now = style.ctx.now()
        for name in (live.take_acts() if live and live.acts else ()):   # buttons pressed in the dashboard (Module.actions)
            try:
                style.act(name)
            except Exception:   # a broken button mustn't end the screen
                log.exception("%s: the %r button failed", type(style).__name__, name)
            last = None         # redraw now, whatever its pace
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
        if lit:
            lit(frame is not under)
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
    at start-up (unless that's in the night), fills ``splash`` items from the Splash pool and swaps in the sleep playlist, at
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
            limit: float | None = None, hold: bool = True, edge: bool = True, bright: bool = False) -> bool:
        shown = entry
        if entry.module == "splash":  # a Splash item: the next file from the Splash pool
            entry = schedule.splash if schedule else None
            if entry is None:
                return False
            extras = None  # overlays never cover the splash
        try:
            style = entry.build(modules, ctx_factory())
        except Exception as error:
            broken(f"skipping playlist entry {entry.module}", entry, error)
            return False
        style.once = entry.once
        style.looping = entry.looping
        # a GIF playing once hands over itself; its expected length is only a cap, with room for a slow Pi
        moving = entry.once and getattr(style, "moving", False)
        cap = entry.seconds + ONCE_SLACK if moving else entry.seconds
        seconds = cap if limit is None else max(0.0, min(cap, limit))
        if live:
            live.playing(shown, upcoming, index)
        try:
            frames = play_entry(style, seconds, panel, stop, place=place, live=live, overlays=extras, hold=hold,
                                overrun=OVERRUN if limit is None else max(0.0, min(OVERRUN, limit - cap)),   # never into sleep
                                until=limit if edge else None,   # (edge: the limit is sleep starting or ending)
                                lit=(lambda covered: lift(True)) if bright else lift)   # (a pushed message: all of it)
            lift(False)
        except Exception as error:  # one broken module must not stop the panel
            broken(f"{entry.module}/{entry.style} failed while playing; moving on", entry, error)
            return False
        gone = frames == 0 and not getattr(style, "nexted", False)   # it skipped itself (Spotify, nothing on),
        # or had no time at all: never a spin. (Next pressed before its first frame isn't the screen's doing.)
        skipped[0] = skipped[0] + 1 if gone else 0
        return True

    said: dict[tuple, float] = {}   # a broken screen's problem -> when it was last logged

    def broken(what, entry, error):
        """Log a screen that can't be built or failed, in full the first time and then one line every
        ``SAY_AGAIN`` s: a playlist of nothing but broken screens came round twice a second, each time with a
        whole traceback (some 250,000 a day on the SD card)."""
        key = (entry.module, entry.style, type(error).__name__, str(error))
        now = time.monotonic()
        if key not in said:
            log.error("%s", what, exc_info=error)
        elif now - said[key] >= SAY_AGAIN:
            log.error("%s (still: %s: %s)", what, type(error).__name__, error)
        else:
            return
        if len(said) > 200:
            said.clear()
        said[key] = now

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

    def lift(on):
        """A covering screen (PIN, setup, address) or a pushed message is up: day brightness while it is, so it
        can be read in the sleep hours too. A brightness set by hand in the dashboard is left as it is."""
        if not hasattr(panel, "lift"):
            return
        day = schedule.day_brightness if schedule else None
        panel.lift(day if on and day is not None and not (live and live.brightness is not None) else None)

    def show_dark(wait):
        """A dark panel (forced sleep, or every screen skipped), still with the PIN, setup or address screen
        over it when one is up, so pairing and setup work then too; such a screen keeps its own pace."""
        dark = ctx_factory().canvas()
        covered = live.overlay(dark) if live and live.overlay else dark
        lift(covered is not dark)
        out = place(covered) if place else covered
        panel.show(out)
        if live:
            live.publish(out, public=place(dark) if place else dark)
        stop.wait(min(wait, 1 / 20) if covered is not dark else wait)
        if live:
            live.skip.clear()   # nothing was playing: a Wake or Next pressed meanwhile mustn't skip the next screen

    skipped = [0]   # screens in a row that skipped themselves without a frame (or failed)
    splashes = bool(schedule and schedule.splash_sources)   # Splash switched off: its items have nothing to play

    def due(now, start):
        """Places in ``day`` that play at ``now``, in playing order from ``start``."""
        return [k % len(day) for k in range(start, start + len(day))
                if day[k % len(day)].plays_at(now) and (splashes or day[k % len(day)].module != "splash")]

    def troubled(entry) -> bool:
        """Whether ``entry``'s screens skip themselves just now (Module.trouble: Weather with no API key)."""
        module = modules.get(entry.module)
        try:
            return bool(module and getattr(module, "trouble", None) and module.trouble())
        except Exception:   # a broken check counts as nothing wrong: the screen says for itself
            return False

    def after(index, entry, now):
        """What follows ``entry``: the next one that plays when it ends (itself, if it's the only one; None: nothing).
        One that will skip itself (``troubled``) isn't what's next: the dashboard's Up next would be wrong."""
        later = due(now + dt.timedelta(seconds=entry.seconds), index + 1)
        later = [k for k in later if not troubled(day[k])]
        return day[later[0]] if later else None

    boot = schedule.boot_entry() if schedule else None
    if boot:
        now = ctx_factory().now()
        night = bool(live and live.asleep(now)) or schedule.asleep(now)
        brightness(now, night)   # before the boot screen: the panel opens at day brightness
        if not night:            # (starting up in the night goes straight to the sleep screen)
            first = due(now, 0)
            run(boot, day[first[0]] if first else None, limit=schedule.seconds_until_change(now))   # (not into sleep)
    was_asleep = False
    position: dict[tuple, int] = {}  # where the sleep playlist left off
    cursor = 0                       # the place in ``day`` whose turn is next
    again = None                     # a place to play again whatever its times (see the paused push below)
    resume = None                    # the place paused when sleep cut it short: held again in the morning
    played = 0
    while not stop.is_set() and (loops is None or played < loops * len(day)):
        if live and live.reload.is_set():
            return  # the caller reloads the config and calls play() again
        now = ctx_factory().now()
        pushed = live.take_push() if live else None
        if pushed:
            run(pushed, hold=False, bright=True)   # a message shows for its own time even while paused
            continue
        forced = bool(live and live.asleep(now))
        hours = bool(schedule and schedule.asleep(now))    # the sleep hours (Settings > Sleep)
        if live and live.awake and (forced or not hours):
            live.awake = False                             # those hours are over (or Sleep now): asleep as usual next time
        asleep = forced or (hours and not (live and live.awake))
        woke = was_asleep and not asleep
        if woke and schedule:
            schedule.wake()  # tomorrow night picks a new Sleep screen
        if not (live and live.paused):
            resume = None
        was_asleep = asleep
        if live:
            live.sleeping, live.sleep_hours = asleep, hours
            if asleep:
                live.idle = False   # asleep, not "nothing set to play"
        brightness(now, asleep)
        if forced and not (schedule and schedule.sleep_playlist):
            if live.entry is not None:
                live.playing(None, None)   # nothing is on a dark panel (so nothing there has buttons to press)
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
            if woke and resume is not None:   # paused through the night: the morning holds the same screen
                again = resume
            if again is not None and live and live.paused:   # the paused screen a message cut in on: back to it,
                now_due = [again] + [k for k in now_due if k != again]   # even if its time has ended meanwhile
            again = resume = None        # (resumed since: it takes its turn by its times, like any other)
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
        edge = not (live and live.awake and hours)   # woken in the sleep hours: their end changes nothing
        ran = run(entry, upcoming, where, None if asleep else overlays,
                  limit, edge=edge)   # where: slot in the saved playlist; limit: stop when sleep starts/ends
        if live and live.paused and not asleep:
            resume = index           # (the night may cut it short)
        if live and live.paused and live.pushes:   # a message cut in on the paused screen: come back to it after
            played -= 1
            if asleep:
                position[key] = index
            else:
                cursor = again = index
        if not ran:
            skipped[0] += 1   # a screen that can't be built or broke counts as skipped too
        if skipped[0] >= turns:   # every screen skipped itself (Spotify, nothing on) or failed: dark, not a spin,
            show_dark(1)          # and a frame, so the watchdog doesn't take a broken screen for a stuck player
        elif not ran:
            stop.wait(0.2)  # never spin if every entry is broken
