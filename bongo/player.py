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


FOLLOW_EVERY = 30.0   # s between looks at the brightness while a screen is up (Follow daylight: the sun has moved on)
SAY_AGAIN = 600.0  # s before a screen that is still broken is mentioned in the log again (one line)
OVERRUN = 120.0   # s a busy screen (a line still scrolling) may stay past its time: a backstop, never reached normally
# A screen that says it is live (``Style.stay``) keeps the panel past its time for as long as it says so. A module
# with a mistake in it, or one that means harm, must never hold the panel for good: each module may be kept
# STAY_MOST seconds in any STAY_WINDOW, and when that is used up its screens hand over on time like any other.
STAY_MOST = 4 * 3600.0
STAY_WINDOW = 12 * 3600.0
BACK = 1.0         # s a live screen a message cut in on is given back (kept past that only while it is still live)


def busy(style) -> bool:
    """Whether a style that has had its time is still in the middle of something (``Style.busy``)."""
    try:
        return bool(getattr(style, "busy", False))
    except Exception:   # a broken ``busy`` mustn't look like the screen failing: it has simply had its time
        log.exception("%s: busy failed", type(style).__name__)
        return False


def staying(style) -> bool:
    """Whether a style says something is live just now, so it should keep the panel (``Style.stay``)."""
    try:
        return bool(getattr(style, "stay", False))
    except Exception:   # a broken ``stay`` is a screen with nothing live: it hands over on time
        log.exception("%s: stay failed", type(style).__name__)
        return False


def stay_left(spent: list, now: float) -> float:
    """Seconds a module may still be kept (``Style.stay``), given when and how long it was before:
    ``spent`` is its [(when it ended, seconds kept), ...] and loses what is older than STAY_WINDOW."""
    spent[:] = [(when, seconds) for when, seconds in spent if now - when < STAY_WINDOW]
    return max(0.0, STAY_MOST - sum(seconds for _, seconds in spent))


def play_entry(style: Style, seconds: float, panel, stop: threading.Event | None = None,
               clock=time.monotonic, place=None, live=None, overlays=None, hold: bool = True,
               overrun: float = OVERRUN, until: float | None = None, lit=None, cover: bool = True,
               stay: float = 0.0) -> int:
    """Render one entry until its time is up; returns frames shown.
    ``place`` puts the module's safe-area frame onto the full panel (see SafeArea.place).
    ``live`` (bongo.live.Live) can skip or hold the entry and sees every frame. ``hold`` False: Pause
    doesn't keep this one up (a pushed message shows for its time, then gives the paused screen back).
    ``overrun``: how long past its time a ``busy`` style may stay (0: the time is a hard stop).
    ``until``: seconds to a hard end that even Pause doesn't hold (sleep starting or ending).
    ``lit(covered)``: told each frame whether a covering screen (PIN, setup, address) is up, so the panel can
    be bright enough to read it at night. ``cover`` False: none of those screens over this one (the boot
    screen: it comes before anything else; they follow it). ``stay``: how long past its time a style that
    says it is live (``Style.stay``) may keep the panel (0: its time is its time); how long it did is left
    in ``style.stayed``, and ``style.kept`` says whether it was still being kept when it ended."""
    stop = stop or threading.Event()
    frame_time = 1 / style.fps
    end = clock() + seconds
    hard = None if until is None else clock() + until
    style.stayed, style.kept, ticked = 0.0, False, None
    try:
        style.hand_over_at = style.ctx.now().timestamp() + seconds   # (Style.hand_over_at: nothing new started after this holds it up)
    except Exception:
        pass
    shown = 0
    last, last_at = None, -1e9   # the style's latest frame: while a screen covers it, it keeps its own pace
    preview = getattr(live, "preview", None) if cover else None   # the editor's Preview on the panel (bongo/panelpreview.py)
    while not stop.is_set():
        held = bool(hold and live and live.paused)
        kept = style.kept = bool(stay > 0 and style.stayed < stay and staying(style))   # it's live: it keeps the panel
        if live:
            live.staying = kept
        if kept and not held and ticked is not None and clock() >= end:
            style.stayed += clock() - max(ticked, end)   # (only time past its own, and not while paused)
        ticked = clock()
        if (held or kept) and getattr(style, "hand_over_at", None) is not None:
            style.hand_over_at = None   # paused or live: no end in sight, so what takes turns carries on taking them
        elif not (held or kept) and (hold and live or stay > 0) and getattr(style, "hand_over_at", 0) is None:
            try:                        # unpaused, or over: the end is back (now, if its time has already gone)
                style.hand_over_at = style.ctx.now().timestamp() + max(0.0, end - clock())
            except Exception:
                pass
        if hard is not None and clock() >= hard:
            break   # sleep starts or ends: paused, live or not, the player moves on to it
        if clock() >= end and not held and not kept:
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
        if preview:               # a draft from the editor covers the screen, at its own rate; the screen underneath
            draft = preview.cover(frame, frame_time)   # carries on unseen (it is still what the web is shown: a draft can be private)
            if draft is not frame:
                frame, wait = draft, min(frame_time, preview.pace)
        plain = frame             # (a draft is never lit up like the screens below: it shows as bright as the panel is now)
        if live and live.overlay and cover:
            covered = live.overlay(frame)
            if covered is not frame:   # a screen over the style (PIN, address) scrolls at 20 fps whatever the style's rate;
                frame, wait = covered, min(wait, 1 / 20)   # the style underneath still renders at its own
            elif frame is under:
                last = None            # uncovered: every pass renders, exactly as before
        if lit:
            lit(frame is not plain)
        out = place(frame) if place else frame
        panel.show(out)
        if live:
            live.publish(out, public=out if under is frame else (place(under) if place else under))
        shown += 1
        stop.wait(max(0.0, wait - (clock() - started)))
    if live:
        live.staying = False
    return shown


def play(playlist: list[Entry], modules: dict[str, Module], panel, ctx_factory=Context,
         stop: threading.Event | None = None, loops: int | None = None, place=None,
         schedule=None, live=None, overlays=None) -> None:
    """Cycle the playlist until ``stop`` (or ``loops`` passes). A ``Schedule`` plays its boot screen
    at start-up before anything else (in the sleep hours at sleep brightness), fills ``splash`` items from the Splash pool and swaps in the sleep playlist, at
    sleep brightness, during its hours (an entry is cut short when sleep starts or ends). ``live``
    lets the dashboard skip, pause, push a message, force sleep and override brightness.
    An entry with ``times`` only plays inside them: it is looked at when its turn comes, so the one on the
    panel finishes its seconds. With nothing to play (every entry has times, none now) the panel is dark.
    A day screen that says it is live (``Style.stay``) keeps the panel past its seconds, within its module's
    allowance (``stay_left``), never in the sleep hours; a message that cuts in on it gives the panel back to it.
    ``overlays`` (bongo.overlays.Overlays) draw over the day playlist, not splash, sleep or pushes."""
    stop = stop or threading.Event()
    if live:
        live.idle = False
        live.rules_changed()   # (a brightness set by hand under the last settings: it ends by this schedule's rule)
    day = [e for e in playlist if e.enabled]
    if not day:
        raise ValueError("playlist has no enabled entries")

    def run(entry: Entry, upcoming: Entry | None = None, index: int | None = None, extras=None,
            limit: float | None = None, hold: bool = True, edge: bool = True, bright: bool = False,
            cover: bool = True, stay: bool = False, back: float | None = None) -> bool:
        """``back``: this is a live screen that a pushed message cut in on, given back the panel (``time.monotonic()``
        when it was cut). Not a fresh turn: a moment (BACK) and then only while it is still live, and all of it,
        the message's time too, is taken from its module's allowance."""
        shown = entry
        cut[0] = False
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
        if hasattr(style, "follow") and before[0] is not None:   # back to back with the one before (Lava's colour glide)
            try:
                style.follow(before[0])
            except Exception:   # a hand-over that goes wrong is only a cut
                log.exception("%s: follow failed", type(style).__name__)
        # a GIF playing once hands over itself; its expected length is only a cap, with room for a slow Pi
        moving = entry.once and getattr(style, "moving", False)
        cap = entry.seconds + ONCE_SLACK if moving else entry.seconds
        spent = stays.setdefault(entry.module, [])   # (Style.stay: what this module has had of its allowance)
        began = time.monotonic()
        if back is not None:
            spent.append((began, max(0.0, began - back)))   # the message's time, while it was kept waiting
        most = stay_left(spent, began) if stay else 0.0
        if back is not None:                          # (a moment, and no more than is left of its allowance)
            cap = min(cap, BACK, max(0.05, most))
            most = max(0.0, most - cap)               # (that moment comes out of it too)
        seconds = cap if limit is None else max(0.0, min(cap, limit))
        if live:
            live.playing(shown, upcoming, index)
        try:
            frames = play_entry(style, seconds, panel, stop, place=place, live=live, overlays=extras, hold=hold,
                                overrun=OVERRUN if limit is None else max(0.0, min(OVERRUN, limit - cap)),   # never into sleep
                                until=limit if edge else None,   # (edge: the limit is sleep starting or ending)
                                lit=(lambda covered: each_frame(True)) if bright else each_frame,   # (a pushed message: all of it)
                                cover=cover, stay=most if limit is None else max(0.0, min(most, limit - cap)))
            lift(False)
        except Exception as error:  # one broken module must not stop the panel
            broken(f"{entry.module}/{entry.style} failed while playing; moving on", entry, error)
            return False
        finally:
            if live:
                live.staying = False
            if back is not None:
                spent.append((time.monotonic(), time.monotonic() - began))   # given back: every second of it counts
            elif getattr(style, "stayed", 0.0) > 0:
                spent.append((time.monotonic(), style.stayed))
        cut[0] = bool(getattr(style, "kept", False))   # live, and still being kept, when something ended it
        if frames:
            before[0] = style   # (one that skipped itself was never on the panel: the next carries on from the one before)
        if getattr(style, "nexted", False):
            before[0] = None    # Next pressed: the next screen starts afresh, even the same one again
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

    following = bool(schedule and schedule.daylight)   # Follow daylight (bongo/daylight.py)
    look_at = [float("inf")]   # time.monotonic() when a screen that is still up has its brightness looked at again
    seen = {"hand": None, "asleep": False, "mode": None, "fade_ends": 0.0, "awake": None}

    dims = hasattr(panel, "set_brightness")

    def brightness(now, forced_sleep):
        if not dims:
            return
        seen["asleep"] = forced_sleep
        by_hand = live.held_brightness(now, schedule.next_change if following else None) if live else None
        seen["hand"] = by_hand
        timed = by_hand is not None and live.brightness_until is not None
        look_at[0] = time.monotonic() + FOLLOW_EVERY if following or timed else float("inf")
        if following:   # what a covering screen is lifted to (``lift``): the brightness the panel would have awake
            seen["awake"] = round(schedule.level(now, asleep=False, rising=False))
        if by_hand is not None:  # set by hand: straight there
            seen["mode"] = "hand"
            panel.set_brightness(by_hand)
            if live:
                live.level = by_hand
            return
        if not schedule:
            return
        asleep = forced_sleep and schedule.sleep_brightness is not None
        level = schedule.level(now, asleep=forced_sleep, rising=not (live and live.woke_early))
        if level is None:
            return
        level = max(1, min(100, round(level)))
        mode, was = "sleep" if asleep else "day", seen["mode"]
        seen["mode"] = mode
        if live:
            live.level = level
        if not hasattr(panel, "ramp_to"):
            panel.set_brightness(level)
        elif following and mode == was == "day":
            # the sun moving on: a point or two at a time, spread over the time to the next look, so nothing
            # is seen to step. (A day <-> night glide still under way keeps its own length.)
            left = seen["fade_ends"] - time.monotonic()
            panel.ramp_to(level, max(FOLLOW_EVERY, left), schedule.fade_curve if left > 0 else "linear")
        else:   # day <-> night, and back from a brightness set by hand: the glide set in Settings > Sleep (bongo.fade)
            seen["fade_ends"] = time.monotonic() + schedule.fade_seconds
            panel.ramp_to(level, schedule.fade_seconds, schedule.fade_curve)

    def lift(on):
        """A covering screen (PIN, setup, address) or a pushed message is up: day brightness while it is, so it
        can be read in the sleep hours too (with Follow daylight: what the daylight asks for now). A brightness
        set by hand in the dashboard is left as it is."""
        if not hasattr(panel, "lift"):
            return
        day = seen["awake"] if seen["awake"] is not None else (schedule.day_brightness if schedule else None)
        panel.lift(day if on and day is not None and not (live and live.brightness is not None) else None)

    def each_frame(covered):
        """Told by the screen that is up, every frame: whether something covers it; and the moment to look at the
        brightness again without waiting for the next screen (set by hand just now, or the sun has moved on)."""
        lift(covered)
        if dims and ((live is not None and live.brightness != seen["hand"]) or time.monotonic() >= look_at[0]):
            brightness(ctx_factory().now(), seen["asleep"])

    def show_dark(wait):
        """A dark panel (forced sleep, or every screen skipped), still with the PIN, setup or address screen
        over it when one is up, so pairing and setup work then too; such a screen keeps its own pace."""
        before[0] = None   # (after a dark panel the next screen starts afresh: nothing to carry on from)
        dark = ctx_factory().canvas()
        preview = getattr(live, "preview", None)
        end = time.monotonic() + wait
        while True:
            draft = preview.cover(dark) if preview else dark   # the editor's Preview on the panel shows on a dark
            covered = live.overlay(draft) if live and live.overlay else draft   # panel too, as bright as the panel is
            lift(covered is not draft)
            out = place(covered) if place else covered
            panel.show(out)
            if live:
                live.publish(out, public=place(dark) if place else dark)
            if covered is draft is dark:
                stop.wait(wait)
                break
            left = end - time.monotonic()   # something covers the dark: it moves at its own pace, for as long as the
            if covered is not draft:        # dark would have lasted (a draft), or one frame (the PIN, as it always has)
                stop.wait(min(wait, 1 / 20))
                break
            stop.wait(max(0.0, min(left, preview.pace)))
            if left <= preview.pace or stop.is_set() or live.reload.is_set() or live.skip.is_set():
                break
        if live:
            live.skip.clear()   # nothing was playing: a Wake or Next pressed meanwhile mustn't skip the next screen

    before = [None]   # the screen played last, for a screen that carries on from it (``Style.follow``)
    cut = [False]     # the screen just played was live and being kept (``Style.stay``) when something cut in on it
    given = [None]    # (place in ``day``, when it was cut): the live screen to give the panel back to after a message
    stays = getattr(live, "stays", None)   # module -> [(when, seconds kept)]: with ``live`` it outlasts a config save
    stays = {} if stays is None else stays
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
    if boot:   # the start-up screen comes first, day or night, before any other screen (PIN, Wi-Fi setup, address)
        now = ctx_factory().now()
        night = bool(live and live.asleep(now)) or schedule.asleep(now)
        brightness(now, night)   # in the sleep hours at sleep brightness: a restart at 03:00 mustn't light the room
        first = due(now, 0)
        run(boot, day[first[0]] if first else None, limit=schedule.seconds_until_change(now),   # (not into sleep)
            cover=False)
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
            live.woke_early = not forced                   # ...and the panel is up already: no gentle rise after them
        asleep = forced or (hours and not (live and live.awake))
        if live and asleep:
            live.woke_early = False
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
        back = given[0][1] if given[0] is not None and not asleep and given[0][0] == index else None
        given[0] = None
        ran = run(entry, upcoming, where, None if asleep else overlays,
                  limit, edge=edge,   # where: slot in the saved playlist; limit: stop when sleep starts/ends
                  stay=not asleep and not hours, back=back)   # (live or not, no screen is kept in the sleep hours)
        if live and live.paused and not asleep:
            resume = index           # (the night may cut it short)
        if live and live.paused and live.pushes:   # a message cut in on the paused screen: come back to it after
            played -= 1
            if asleep:
                position[key] = index
            else:
                cursor = again = index
        elif live and live.pushes and cut[0] and not asleep:   # a message cut in on a screen that is live: it gets
            played -= 1                                        # the panel back after (``run``'s ``back``)
            cursor = index
            given[0] = (index, time.monotonic())
        if not ran:
            skipped[0] += 1   # a screen that can't be built or broke counts as skipped too
        if skipped[0] >= turns:   # every screen skipped itself (Spotify, nothing on) or failed: dark, not a spin,
            show_dark(1)          # and a frame, so the watchdog doesn't take a broken screen for a stuck player
        elif not ran:
            stop.wait(0.2)  # never spin if every entry is broken
