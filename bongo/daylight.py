"""Panel brightness that follows daylight (Settings > Device > Follow daylight).

    "daylight": {"follow": true, "after_dark": 35, "gradual": 50}

Outside the sleep hours the panel goes from ``after_dark`` to the daytime brightness (``panel.brightness``)
as the sun climbs from civil twilight (6 degrees below the horizon) to a height E, eased at both ends. E is
never above that day's highest sun, so the panel always reaches the daytime brightness at midday, in
December too. ``gradual`` (0 to 100) sets E: 0 is Quick (3 degrees: the change is all around sunrise and
sunset), 50 Middle (8 degrees), 100 Gradual (the day's highest sun: the panel follows the sun's height all
day). The sun is worked out for the Matrix's location (Settings > Location) with the Sky module's
formulas; with no location there is nothing to follow, and the panel keeps the plain daytime brightness.

The sleep hours keep their own brightness, and for ``WAKE_MINUTES`` after they end the panel rises from it
to the curve (``Schedule.level``, which the player and the dashboard's graph both use).
"""
from __future__ import annotations

import datetime as dt
import functools
import logging
from dataclasses import dataclass

DEFAULTS = {"follow": False, "after_dark": 35, "gradual": 50}
TWILIGHT = -6.0             # degrees: the sun's height where the panel starts to leave After dark
QUICK, MIDDLE = 3.0, 8.0    # E, in degrees, with How gradual at 0 and at 50 (at 100 it is the day's highest sun)
LEAST_CLIMB = 3.0           # degrees above TWILIGHT the sun must get for the full daytime brightness: in a polar
#                             winter that only reaches twilight the panel goes part of the way (and nothing divides by 0)
WAKE_MINUTES = 30           # after the sleep hours end: from the sleep brightness up to the curve
STEP = 5                    # minutes between the points of the dashboard's graph
WORDS = ((12, "Quick"), (37, "Fairly quick"), (62, "Middle"), (87, "Fairly gradual"), (100, "Gradual"))
UTC = dt.timezone.utc
log = logging.getLogger(__name__)
_said: set = set()


def failed(doing: str) -> None:
    """Something here went wrong while ``doing``: say so in the log, once, with the reason. The caller carries
    on with the plain daytime brightness: the panel never stops for this."""
    if doing not in _said:
        _said.add(doing)
        log.exception("Follow daylight: %s failed; using the plain daytime brightness", doing)


def settings(config: dict) -> dict:
    """The saved daylight settings over the defaults (an older config has none: off)."""
    saved = config.get("daylight")
    return {**DEFAULTS, **(saved if isinstance(saved, dict) else {})}


def problems(raw) -> list[str]:
    """Readable problems with the ``daylight`` settings (used by config.validate)."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["daylight: must be an object"]
    out = [f"daylight: unknown keys {sorted(set(raw) - set(DEFAULTS))}"] if set(raw) - set(DEFAULTS) else []
    if not isinstance(raw.get("follow", False), bool):
        out.append("daylight.follow: true or false")
    for key, low in (("after_dark", 1), ("gradual", 0)):
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= 100:
            out.append(f"daylight.{key}: must be a whole number from {low} to 100")
    return out


def word(gradual: float) -> str:
    """How gradual, in a word (what the dashboard shows beside the slider)."""
    return next(name for top, name in WORDS if gradual <= top)


def number(value, low: float, high: float, otherwise=None):
    """``value`` as a number from ``low`` to ``high`` (one outside is brought to the nearer end), or ``otherwise``
    if it isn't a number at all: text, null, true, NaN. A config edited by hand can hold anything, a whole
    number of 400 digits included (too big to be a float), and none of it may stop the Matrix playing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return otherwise
    return float(min(high, max(low, value)))


def located(home) -> bool:
    """Whether ``home`` (the config's location) is somewhere the sun can be worked out for."""
    try:
        return -90 <= float(home["latitude"]) <= 90 and -180 <= float(home["longitude"]) <= 180
    except (TypeError, KeyError, ValueError, OverflowError):
        return False


def aware(now: dt.datetime) -> dt.datetime:
    """``now`` as a moment in time: a naive one is the Pi's own local time (ctx.now() with no time zone set)."""
    return now if now.tzinfo else now.astimezone()


def clock(moment: dt.datetime, now: dt.datetime) -> str:
    """"16:07": ``moment`` as the Matrix's clock reads then (the zone ``now`` is in): the minute it falls in, never
    rounded up. The one way a time is put into words here, so the card's sunset and "until 16:07" on the Now
    page can't differ by a minute."""
    return aware(moment).astimezone(now.tzinfo).strftime("%H:%M")   # (``now`` naive: the Pi's own zone, as it is at ``moment``)


def _astro():
    from bongo.modules.astronomy import astro   # (the Sky module's sun: compact formulas, nothing to install)
    return astro


@functools.lru_cache(maxsize=4)
def sun_table(start: dt.datetime, minutes: int, latitude: float, longitude: float) -> dict[int, tuple[float, float]]:
    """{minutes after ``start``: (the sun's height, its highest that day)} every STEP minutes of a day that
    begins at ``start`` (UTC) and is ``minutes`` long. It depends on the place and the date only, so moving a
    slider (which asks for the graph again) costs no astronomy."""
    astro = _astro()
    return {m: astro.sun_heights(start + dt.timedelta(minutes=m), latitude, longitude) for m in range(0, minutes + 1, STEP)}


def day_span(now: dt.datetime) -> tuple[dt.datetime, int]:
    """(today's first moment by the Matrix's clock, in UTC; how many minutes today really has): 1440, but 1380
    the day the clocks go forward and 1500 the day they go back. ``now`` naive: the Pi's own zone."""
    zone, today = now.tzinfo, now.date()
    start = dt.datetime.combine(today, dt.time(), tzinfo=zone).astimezone(UTC)
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), tzinfo=zone).astimezone(UTC)
    return start, max(STEP, round((end - start).total_seconds() / 60))


def moments(day: dt.date, wall: dt.time, zone) -> list[dt.datetime]:
    """The moments (UTC) on ``day`` when the clock on the wall reads ``wall``: one; none on the day the clocks
    skip it; two on the day they go back over it."""
    out = []
    for fold in (0, 1):
        said = dt.datetime.combine(day, wall, tzinfo=zone).replace(fold=fold)
        moment = said.astimezone(UTC)
        if moment.astimezone(zone).replace(tzinfo=None) == said.replace(tzinfo=None) and moment not in out:
            out.append(moment)
    return out


def clock_change(start: dt.datetime, minutes: int, zone) -> int | None:
    """The minute after ``start`` at which the clocks change on a day ``minutes`` long (None: they don't)."""
    offset = lambda m: (start + dt.timedelta(minutes=m)).astimezone(zone).utcoffset()   # noqa: E731
    if offset(0) == offset(minutes):
        return None
    low, high = 0, minutes
    while high - low > 1:
        mid = (low + high) // 2
        low, high = (mid, high) if offset(mid) == offset(0) else (low, mid)
    return high


@dataclass(frozen=True)
class Daylight:
    day: int          # the daytime brightness (panel.brightness): what the panel reaches at midday
    dark: int         # after dark; never above ``day``
    gradual: float    # 0 Quick .. 50 Middle .. 100 Gradual
    latitude: float
    longitude: float

    @classmethod
    def from_config(cls, config: dict) -> "Daylight | None":
        """None: not following daylight (switched off, or no location to work the sun out for)."""
        s, home, panel = settings(config), config.get("location"), config.get("panel")
        day = number(panel.get("brightness") if isinstance(panel, dict) else None, 1, 100)
        if s["follow"] is not True or day is None or not located(home):
            return None
        # (a hand-edited config: anything that isn't a number is the default, and validate() says so)
        dark = number(s["after_dark"], 1, 100, DEFAULTS["after_dark"])
        return cls(day=day, dark=min(day, dark), gradual=number(s["gradual"], 0, 100, DEFAULTS["gradual"]),
                   latitude=float(home["latitude"]), longitude=float(home["longitude"]))

    def reach(self, peak: float) -> float:
        """E: the sun's height at which the panel is at its daytime brightness, on a day whose highest sun is ``peak``."""
        g = self.gradual
        e = QUICK + (MIDDLE - QUICK) * g / 50 if g <= 50 else MIDDLE + (max(peak, MIDDLE) - MIDDLE) * (g - 50) / 50
        return max(min(e, peak), TWILIGHT + LEAST_CLIMB)

    def at(self, height: float, peak: float) -> float:
        """The brightness with the sun ``height`` degrees up, on a day whose highest sun is ``peak``."""
        f = max(0.0, min(1.0, (height - TWILIGHT) / (self.reach(peak) - TWILIGHT)))
        return self.dark + (self.day - self.dark) * f * f * (3 - 2 * f)

    def level(self, now: dt.datetime) -> float:
        """The brightness the daylight asks for at ``now`` (no sleep hours in it: see Schedule.level)."""
        return self.at(*_astro().sun_heights(aware(now), self.latitude, self.longitude))

    def sun_events(self, now: dt.datetime) -> list[tuple[dt.datetime, str]]:
        """The sunrises and sunsets around ``now`` (yesterday's to the day after tomorrow's) in order:
        [(moment in UTC, "sunrise" or "sunset"), ...]. None of them in a polar day or night."""
        astro, out = _astro(), []
        solar = (aware(now).astimezone(UTC) + dt.timedelta(hours=self.longitude / 15)).date()
        for days in (-1, 0, 1, 2):
            try:
                times = astro.sun_times(solar + dt.timedelta(days=days), self.latitude, self.longitude)
            except (ValueError, ZeroDivisionError, OverflowError):   # (at a pole itself)
                times = None
            if times:
                out += [(times[0], "sunrise"), (times[1], "sunset")]
        return sorted(out)


def curve(schedule, now: dt.datetime) -> dict:
    """Today's brightness by the Matrix's clock, for the dashboard's graph. Along it, a minute is a real minute
    since the day began, so on the days the clocks change the day is 23 or 25 hours long and every point is
    still the panel's brightness at that moment (``schedule.level``, which is what the player asks too).

    ``minutes``: how long today is. ``points`` [[minute, level], ...] from the day's start to its end (two at
    one minute where the brightness steps: sleep starting). ``hours`` [[minute, "03:00"], ...]: where each
    hour by the clock on the wall falls (one is missing, or there twice, when the clocks change). ``sleep``
    [[from, to], ...] the sleep hours. ``now``: where the clock is. ``sunrise``, ``sunset``: today's ("HH:MM",
    None when the sun doesn't rise or set today)."""
    zone, today, light = now.tzinfo, now.date(), schedule.daylight   # (zone None: the Pi's own, whatever its clocks do)
    start, minutes = day_span(now)
    at = lambda minute: (start + dt.timedelta(minutes=minute)).astimezone(zone)   # noqa: E731
    table = sun_table(start, minutes, light.latitude, light.longitude) if light else {}
    corners = set()   # where the line may turn or step: sleep starting and ending, the rise over, the clocks changing
    if schedule.sleeps and schedule.sleep_from and schedule.sleep_to:
        begins, ends = dt.time.fromisoformat(schedule.sleep_from), dt.time.fromisoformat(schedule.sleep_to)
        risen = (dt.datetime.combine(today, ends) + dt.timedelta(minutes=WAKE_MINUTES)).time()
        for day in (today, today + dt.timedelta(days=1)):
            for wall in (begins, ends, risen):
                corners |= {(moment - start).total_seconds() / 60 for moment in moments(day, wall, zone)}
    corners.add(clock_change(start, minutes, zone))
    corners = {int(c) if c == int(c) else round(c, 2) for c in corners if c is not None and 0 <= c <= minutes}

    def level(minute, back=0):
        moment = at(minute - back / 60)   # (a real second earlier: not one by the clock on the wall, which may have jumped)
        lit = light and (light.at(*table[minute]) if minute in table and not back else light.level(moment))
        return round(schedule.level(moment, lit=lit), 1)
    points, spans = [], []
    for minute in sorted(set(range(0, minutes + 1, STEP)) | {minutes} | corners):
        here = level(minute)
        if minute in corners and minute > 0:
            before = level(minute, back=1)
            if abs(before - here) >= 0.5:
                points.append([minute, before])   # a step (sleep starting): the line goes straight down from here
        points.append([minute, here])
        if bool(schedule.asleep(at(minute))) != bool(spans and len(spans[-1]) == 1):
            spans[-1].append(minute) if spans and len(spans[-1]) == 1 else spans.append([minute])
    if spans and len(spans[-1]) == 1:
        spans[-1].append(minutes)
    hours = [[m, clock(at(m), now)] for m in range(0, minutes, 30) if at(m).minute == 0]
    sun = {"sunrise": None, "sunset": None}
    for moment, name in light.sun_events(now) if light else ():
        if moment.astimezone(zone).date() == today and sun[name] is None:
            sun[name] = clock(moment, now)
    return {"minutes": minutes, "points": points, "hours": hours, "sleep": [s for s in spans if s[0] < s[1]],
            "now": {"minute": round((aware(now) - start).total_seconds() / 60, 1), "level": round(schedule.level(now), 1),
                    "clock": clock(now, now)},
            **sun}
