"""Splash and Sleep screens: config rules that pick from your media, as in v2.

    "splash": {"sources": ["built-in/splash_loading_squares.gif", "uploaded/logo.png"],
               "speed": 100, "fit": "fit"}
    "sleep":  {"from": "23:00", "to": "07:00", "brightness": 15,
               "sources": ["built-in/sleep_default.gif"], "speed": 100, "fit": "fit"}

Splash plays at start-up. Each time, the next file comes from a shuffled bag, so every
source plays once before any repeats. A GIF plays exactly one pass at ``speed`` percent; a
still holds for the average pass of the row's GIFs when that is 5-10 s, else 7 s (v2's rule).

Between ``from`` and ``to`` (overnight ranges allowed) the panel dims to ``brightness`` and
shows one Sleep source, chosen at random when the night starts and kept until wake. The
dashboard's "Sleep now" uses the same screen. Either rule can be left out or switched off
with ``"enabled": false``.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from fractions import Fraction

from PIL import Image, ImageSequence

from bongo import media
from bongo.player import Entry

STILL_SECONDS = 7.0
SCREEN_TYPES = {".gif", ".png", ".jpg", ".jpeg", ".webp"}  # what v2 allowed on Splash/Sleep
FITS = ("fit", "fill", "pixel")
SLEEP_ENTRY_SECONDS = 60  # a sleep screen is re-shown (same file) every minute, so wakes are prompt


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
    return float(sum(delays) / Fraction(str(speed)) * 100 / 1000) if delays else None


def hold_seconds(name: str, sources: list[str], speed: float) -> float:
    """How long one Splash file stays up: a GIF's own pass; a still borrows the GIFs' average."""
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
    allowed = {"sources", "speed", "fit", "enabled"} | ({"from", "to", "brightness"} if sleep else set())
    problems = [f"{where}: unknown keys {sorted(set(rule) - allowed)}"] if set(rule) - allowed else []
    sources = rule.get("sources")
    if not isinstance(sources, list) or not sources:
        problems.append(f"{where}.sources: pick at least one image or GIF")
    else:
        for s in sources:
            screen = "sleep" if sleep else "splash"
            if not media.valid_name(s) or "." + s.rsplit(".", 1)[-1].lower() not in SCREEN_TYPES:
                problems.append(f"{where}.sources: {s!r} isn't a GIF or image from built-in/ or uploaded/")
            elif s.startswith("built-in/") and media.screen_of(s) != screen:
                problems.append(f"{where}.sources: {s!r} isn't a built-in {screen} screen (those are built-in/{screen}_*)")
    speed = rule.get("speed", 100)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 10 <= speed <= 400:
        problems.append(f"{where}.speed: a percentage from 10 to 400")
    if rule.get("fit", "fit") not in FITS:
        problems.append(f"{where}.fit: one of {', '.join(FITS)}")
    if not isinstance(rule.get("enabled", True), bool):
        problems.append(f"{where}.enabled: true or false")
    return problems


# Shuffle bags and the night's pick outlive a config reload (which rebuilds the Schedule).
_bags: dict[tuple, list[str]] = {}
_night: dict[tuple, str] = {}


@dataclass
class Schedule:
    splash_sources: list[str] = field(default_factory=list)
    splash_speed: float = 100
    splash_fit: str = "fit"
    sleep_from: str | None = None
    sleep_to: str | None = None
    sleep_brightness: int | None = None
    sleep_sources: list[str] = field(default_factory=list)
    sleep_speed: float = 100
    sleep_fit: str = "fit"
    day_brightness: int | None = None
    rng: random.Random = field(default_factory=random.Random)

    @classmethod
    def from_config(cls, config: dict) -> "Schedule":
        splash = config.get("splash") or {}
        sleep = config.get("sleep") or {}
        if splash.get("enabled") is False:
            splash = {}
        if sleep.get("enabled") is False:
            sleep = {}
        return cls(
            splash_sources=list(splash.get("sources") or []),
            splash_speed=splash.get("speed", 100), splash_fit=splash.get("fit", "fit"),
            sleep_from=sleep.get("from"), sleep_to=sleep.get("to"),
            sleep_brightness=sleep.get("brightness"),
            sleep_sources=list(sleep.get("sources") or []),
            sleep_speed=sleep.get("speed", 100), sleep_fit=sleep.get("fit", "fit"),
            day_brightness=config.get("panel", {}).get("brightness"),
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
                     seconds=hold_seconds(name, self.splash_sources, self.splash_speed))

    @splash.setter
    def splash(self, value) -> None:  # main() switches the splash off after start-up
        if value is None:
            self.splash_sources = []

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
        return Entry("media", self.sleep_fit, settings={"file": _night[key], "speed": self.sleep_speed / 100},
                     seconds=SLEEP_ENTRY_SECONDS)

    def wake(self) -> None:
        """Called by the player when sleep ends, so the next night picks again."""
        _night.pop(tuple(self.sleep_sources), None)

    @property
    def sleep_playlist(self) -> list[Entry]:
        entry = self.sleep_entry()
        return [entry] if entry else []

    def brightness(self, now: dt.datetime) -> int | None:
        if self.asleep(now) and self.sleep_brightness is not None:
            return self.sleep_brightness
        return self.day_brightness

    def playlist(self, now: dt.datetime, day: list[Entry]) -> list[Entry]:
        return self.sleep_playlist if self.asleep(now) else day
