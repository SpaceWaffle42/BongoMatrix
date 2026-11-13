"""Splash and Sleep screens: config rules that pick from your media, as in v2.

    "splash": {"sources": ["built-in/splash_loading_squares.gif", "uploaded/splash_logo.png"],
               "speed": 100, "fit": "fit", "boot": "built-in/splash_loading_squares.gif"}
    "sleep":  {"from": "23:00", "to": "07:00", "brightness": 15, "fade": 5, "curve": "smooth",
               "sources": ["built-in/sleep_default.gif"], "speed": 100, "fit": "fit"}

Built-in files serve the screen they were made for (``splash_*`` or ``sleep_*``); anything you
uploaded, image, GIF or video, whatever it's called, can be a Splash, boot or Sleep screen.

Splash plays wherever a ``splash`` item sits in the playlist (as in v2). Each time, the next
file comes from a shuffled bag, so every source plays once before any repeats. ``boot`` is an
optional single file played once at start-up; without one, start-up is nothing special. A GIF or a
video plays exactly one pass at ``speed`` percent (the Media module hands over at its end); a still
holds for the average pass of the row's GIFs when that is 5-10 s, else 7 s (v2's rule).

Between ``from`` and ``to`` (overnight ranges allowed) the panel dims to ``brightness`` (gliding
over ``fade`` seconds along ``curve``, see bongo.fade; it brightens back the same way) and
shows one Sleep source, chosen at random when the night starts and kept until wake. The
dashboard's "Sleep now" uses the same screen. Either rule can be left out or switched off
with ``"enabled": false``.
"""
from __future__ import annotations

import datetime as dt
import functools
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction

from PIL import Image, ImageSequence

from bongo import fade, media
from bongo.player import Entry

STILL_SECONDS = 7.0
VIDEO_CAP = 15.0   # seconds: the Media module plays a video's first 15 s (see bongo.modules.media)
FITS = ("fit", "fill", "pixel")
SLEEP_ENTRY_SECONDS = 60  # a sleep screen is re-shown (same file) about every minute, in whole passes, so wakes are prompt


def within(now: dt.datetime, start: str, end: str) -> bool:
    """True when ``now``'s clock time is in [start, end); start > end wraps past midnight."""
    t, a, b = now.time().replace(tzinfo=None), dt.time.fromisoformat(start), dt.time.fromisoformat(end)
    if a == b:
        return True
    return a <= t < b if a < b else (t >= a or t < b)


def gif_pass(name: str, speed: float) -> float | None:
    """Seconds for one pass of a GIF at ``speed`` percent (frame delays floored at 10 ms), or None."""
    path = media.resolve(name)
    if path is None or path.suffix.lower() != ".gif":
        return None
    try:
        with Image.open(path) as im:
            delays = [Fraction(max(10, int(f.info.get("duration") or 100))) for f in ImageSequence.Iterator(im)]
    except Exception:
        return None
    if len(delays) < 2:
        return None   # one frame: a still
    return float(sum(delays) / Fraction(str(speed)) * 100 / 1000)


@functools.lru_cache(maxsize=8)
def _video_frames(path: str, mtime: float) -> int:
    """How many frames the Media module gets from this video (20 fps, up to 300), decoded tiny."""
    from bongo.modules.media import MAX_FRAMES, VIDEO_FPS
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an", "-vf", f"fps={VIDEO_FPS},scale=2:2",
                          "-frames:v", str(MAX_FRAMES), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, timeout=120, check=True).stdout
    return len(raw) // 12


def video_pass(name: str, speed: float) -> float | None:
    """Seconds for one pass of a video at ``speed`` percent, as the Media module loops it, or None
    (not a video, missing, no ffmpeg, or it won't decode)."""
    path = media.resolve(name)
    if path is None or not media.is_video(path) or not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return None
    try:
        from bongo.modules.media import VIDEO_FPS
        frames = _video_frames(str(path), path.stat().st_mtime)
    except Exception:
        return None
    if frames < 2:
        return None
    return float(Fraction(frames * 1000 // VIDEO_FPS) / Fraction(str(speed)) * 100 / 1000)


def hold_seconds(name: str, sources: list[str], speed: float) -> float:
    """How long one Splash file stays up: a GIF's own pass; a still borrows the GIFs' average. A video
    is allowed its longest pass: it hands over by itself when it has played through (one that can't be
    played, with no ffmpeg or a missing file, is treated as a still)."""
    if name.rsplit(".", 1)[-1].lower() in {v[1:] for v in media.VIDEOS} and media.resolve(name) \
            and shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return VIDEO_CAP * 100 / speed
    one = gif_pass(name, speed)
    if one is not None:
        return one
    passes = [p for p in (gif_pass(s, speed) for s in dict.fromkeys(sources)) if p is not None]
    mean = sum(passes) / len(passes) if passes else 0
    return mean if 5 <= mean <= 10 else STILL_SECONDS


def screen_problems(where: str, rule, *, sleep: bool = False) -> list[str]:
    """Readable problems with a splash or sleep rule (used by config.validate)."""
    if not isinstance(rule, dict):
        return [f"{where}: must be an object"]
    allowed = {"sources", "speed", "fit", "enabled"} | ({"boot"} if not sleep else set()) | ({"from", "to", "brightness", "fade", "curve"} if sleep else set())
    problems = [f"{where}: unknown keys {sorted(set(rule) - allowed)}"] if set(rule) - allowed else []
    sources = rule.get("sources")
    if not isinstance(sources, list) or not sources:
        problems.append(f"{where}.sources: pick at least one image, GIF or video")
    else:
        screen = "sleep" if sleep else "splash"
        for s in sources:
            if not media.valid_name(s):
                problems.append(f"{where}.sources: {s!r} isn't an image, GIF or video from built-in/ or uploaded/")
            elif not media.for_screen(s, screen):
                made = media.screen_of(s)
                problems.append(f"{where}.sources: {s!r} is the built-in {made} screen, so it can't be a {screen} screen"
                                if made else f"{where}.sources: {s!r} isn't a built-in screen")
    boot = rule.get("boot")
    if not sleep and boot is not None and not media.for_screen(boot, "splash"):
        problems.append(f"{where}.boot: {boot!r} isn't an image, GIF or video you uploaded, or a built-in splash")
    speed = rule.get("speed", 100)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 10 <= speed <= 400:
        problems.append(f"{where}.speed: a percentage from 10 to 400")
    if rule.get("fit", "fit") not in FITS:
        problems.append(f"{where}.fit: one of {', '.join(FITS)}")
    if not isinstance(rule.get("enabled", True), bool):
        problems.append(f"{where}.enabled: true or false")
    seconds = rule.get("fade", fade.DEFAULT_SECONDS)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= fade.MAX_SECONDS:
        problems.append(f"{where}.fade: seconds from 0 to {fade.MAX_SECONDS}")
    if rule.get("curve", fade.DEFAULT_CURVE) not in fade.CURVES:
        problems.append(f"{where}.curve: one of {', '.join(fade.CURVES)}")
    return problems


# Shuffle bags and the night's pick outlive a config reload (which rebuilds the Schedule).
_bags: dict[tuple, list[str]] = {}
_night: dict[tuple, str] = {}


@dataclass
class Schedule:
    splash_sources: list[str] = field(default_factory=list)
    splash_speed: float = 100
    splash_fit: str = "fit"
    boot: str | None = None           # played once at start-up, if set
    sleep_from: str | None = None
    sleep_to: str | None = None
    sleep_brightness: int | None = None
    sleep_sources: list[str] = field(default_factory=list)
    sleep_speed: float = 100
    sleep_fit: str = "fit"
    day_brightness: int | None = None
    fade_seconds: float = fade.DEFAULT_SECONDS
    fade_curve: str = fade.DEFAULT_CURVE
    rng: random.Random = field(default_factory=random.Random)

    @classmethod
    def from_config(cls, config: dict) -> "Schedule":
        def own(rule, screen):
            # your uploads go anywhere; a built-in file only on the screen it was made for
            return [s for s in (rule.get("sources") or []) if media.for_screen(s, screen)]

        splash = config.get("splash") or {}
        sleep = config.get("sleep") or {}
        if splash.get("enabled") is False:
            splash = {}
        if sleep.get("enabled") is False:
            sleep = {}
        return cls(
            splash_sources=own(splash, "splash"),
            splash_speed=splash.get("speed", 100), splash_fit=splash.get("fit", "fit"),
            boot=next(iter(own({"sources": [splash.get("boot")]}, "splash")), None) if splash.get("boot") else None,
            sleep_from=sleep.get("from"), sleep_to=sleep.get("to"),
            sleep_brightness=sleep.get("brightness"),
            sleep_sources=own(sleep, "sleep"),
            sleep_speed=sleep.get("speed", 100), sleep_fit=sleep.get("fit", "fit"),
            day_brightness=config.get("panel", {}).get("brightness"),
            fade_seconds=sleep.get("fade", fade.DEFAULT_SECONDS),
            fade_curve=sleep.get("curve", fade.DEFAULT_CURVE),
        )

    # -- splash --------------------------------------------------------------------------
    @property
    def splash(self) -> Entry | None:
        """The next Splash screen (shuffled bag: all sources play before any repeats)."""
        if not self.splash_sources:
            return None
        key = tuple(self.splash_sources)
        bag = _bags.get(key) or []
        if not bag:
            bag = list(dict.fromkeys(self.splash_sources))
            self.rng.shuffle(bag)
        name = bag.pop()
        _bags[key] = bag
        return Entry("media", self.splash_fit, settings={"file": name, "speed": self.splash_speed / 100},
                     seconds=hold_seconds(name, self.splash_sources, self.splash_speed), once=True)

    def boot_entry(self) -> Entry | None:
        """The start-up screen: one pass of the boot file (a still holds STILL_SECONDS)."""
        if not self.boot:
            return None
        return Entry("media", self.splash_fit, settings={"file": self.boot, "speed": self.splash_speed / 100},
                     seconds=hold_seconds(self.boot, [self.boot], self.splash_speed), once=True)

    # -- sleep ---------------------------------------------------------------------------
    @property
    def sleeps(self) -> bool:
        return bool(self.sleep_sources)

    def asleep(self, now: dt.datetime) -> bool:
        return bool(self.sleeps and self.sleep_from and within(now, self.sleep_from, self.sleep_to))

    def sleep_entry(self) -> Entry | None:
        """Tonight's Sleep screen: one source picked at random, kept until wake."""
        if not self.sleep_sources:
            return None
        key = tuple(self.sleep_sources)
        if key not in _night:
            _night[key] = self.rng.choice(self.sleep_sources)
        # whole passes, so each new entry (it starts on frame 1) picks up exactly where the last left off
        one = gif_pass(_night[key], self.sleep_speed) or video_pass(_night[key], self.sleep_speed)
        seconds = one * max(1, round(SLEEP_ENTRY_SECONDS / one)) if one else SLEEP_ENTRY_SECONDS
        return Entry("media", self.sleep_fit, settings={"file": _night[key], "speed": self.sleep_speed / 100},
                     seconds=seconds)

    def wake(self) -> None:
        """Called by the player when sleep ends, so the next night picks again."""
        _night.pop(tuple(self.sleep_sources), None)

    @property
    def sleep_playlist(self) -> list[Entry]:
        entry = self.sleep_entry()
        return [entry] if entry else []

    def seconds_until_change(self, now: dt.datetime) -> float | None:
        """Seconds until sleep starts or ends (None: no sleep times), so an entry stops in time."""
        if not (self.sleeps and self.sleep_from and self.sleep_to):
            return None
        mark = dt.time.fromisoformat(self.sleep_to if self.asleep(now) else self.sleep_from)
        when = dt.datetime.combine(now.date(), mark, tzinfo=now.tzinfo)
        if when <= now:
            when += dt.timedelta(days=1)
        return (when - now).total_seconds()

    def brightness(self, now: dt.datetime) -> int | None:
        if self.asleep(now) and self.sleep_brightness is not None:
            return self.sleep_brightness
        return self.day_brightness

    def playlist(self, now: dt.datetime, day: list[Entry]) -> list[Entry]:
        return self.sleep_playlist if self.asleep(now) else day
