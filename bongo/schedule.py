"""Splash and sleep: playlist rules rather than modules.

    "splash": {"module": "media", "settings": {"file": "built-in/splash_loading_squares.gif"}, "seconds": 4}
    "sleep":  {"from": "23:00", "to": "07:00", "brightness": 15,
               "playlist": [{"module": "media", "settings": {"file": "built-in/sleep.gif"}}]}

Splash plays once at start-up. Between ``from`` and ``to`` (overnight ranges allowed) the
sleep playlist replaces the normal one and the panel dims. Either can be left out.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from bongo.player import Entry


def within(now: dt.datetime, start: str, end: str) -> bool:
    """True when ``now``'s clock time is in [start, end); start > end wraps past midnight."""
    t, a, b = now.time().replace(tzinfo=None), dt.time.fromisoformat(start), dt.time.fromisoformat(end)
    if a == b:
        return True
    return a <= t < b if a < b else (t >= a or t < b)


@dataclass
class Schedule:
    splash: Entry | None = None
    sleep_from: str | None = None
    sleep_to: str | None = None
    sleep_brightness: int | None = None
    sleep_playlist: list[Entry] = field(default_factory=list)
    day_brightness: int | None = None

    @classmethod
    def from_config(cls, config: dict) -> "Schedule":
        splash = config.get("splash")
        sleep = config.get("sleep") or {}
        if sleep:
            for key in ("from", "to", "playlist"):
                if key not in sleep:
                    raise ValueError(f"sleep needs '{key}'")
        return cls(
            splash=Entry(**{"seconds": 4, **splash}) if splash else None,
            sleep_from=sleep.get("from"), sleep_to=sleep.get("to"),
            sleep_brightness=sleep.get("brightness"),
            sleep_playlist=[Entry(**{"seconds": 60, **e}) for e in sleep.get("playlist", [])],
            day_brightness=config.get("panel", {}).get("brightness"),
        )

    def asleep(self, now: dt.datetime) -> bool:
        return bool(self.sleep_playlist) and within(now, self.sleep_from, self.sleep_to)

    def brightness(self, now: dt.datetime) -> int | None:
        if self.asleep(now) and self.sleep_brightness is not None:
            return self.sleep_brightness
        return self.day_brightness

    def playlist(self, now: dt.datetime, day: list[Entry]) -> list[Entry]:
        return self.sleep_playlist if self.asleep(now) else day
