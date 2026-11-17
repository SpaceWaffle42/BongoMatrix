"""OpenWeather weather, fetched in the background and cached per place and API key.

OpenWeather's free plan only: the current weather and the 5 day / 3 hour forecast, two calls per
refresh (the plan allows 60 a minute and a million a month). Always asked for in metric; the screen's
own units are worked out here, so screens in °C and °F share one fetch.

``latest(settings)`` never blocks: it returns the cached reading (or None) and starts a refresh when
the cache is stale, so rendering stays smooth with a slow or absent network. BONGO_OFFLINE=1 stops all
fetching. The key is never logged: it is cut out of anything that could show it (``hide``).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace

from . import key as keys

log = logging.getLogger(__name__)
BASE = "https://api.openweathermap.org/data/2.5/"


@dataclass(frozen=True)
class Hour:
    time: dt.datetime   # the place's own local time, without a time zone
    temp: float
    code: int
    is_day: bool


@dataclass(frozen=True)
class Reading:
    temp: float
    feels: float
    humidity: int
    wind: float
    code: int
    is_day: bool
    high: float
    low: float
    hours: tuple[Hour, ...]   # every 3 hours, from OpenWeather's forecast
    fetched: float  # time.time() of the fetch
    at: dt.datetime | None = None   # the place's own local time then (no time zone)
    offset: int = 0                 # the place's offset from UTC, in seconds

    def here(self) -> dt.datetime | None:
        """The place's own local time now (by the Matrix's clock, not its time zone)."""
        return None if self.at is None else self.at + dt.timedelta(seconds=max(0.0, time.time() - self.fetched))


class Refused(Exception):
    """OpenWeather said no to the API key (401): wrong, or new and not working yet."""


# OpenWeather condition id -> the WMO weather code the look knows (look.condition): the first range it falls in
WMO = ((200, 299, 95),                 # thunderstorm
       (300, 300, 51), (301, 399, 53),  # drizzle: light, the rest
       (500, 500, 61), (520, 520, 61),  # light rain, light showers
       (502, 504, 65), (522, 522, 65),  # heavy rain, heavy showers
       (511, 511, 66),                 # freezing rain
       (500, 599, 63),                 # the rest of the rain and showers
       (600, 600, 71), (601, 601, 73), (602, 602, 75),   # light, moderate and heavy snow
       (611, 616, 77),                 # sleet (the look's nearest: snow grains)
       (620, 621, 85), (622, 622, 86),  # snow showers
       (600, 699, 73),
       (771, 781, 95),                 # squalls, tornado
       (700, 799, 45),                 # mist, fog, haze, smoke, dust
       (800, 800, 0),                  # clear
       (801, 801, 1), (802, 802, 2),   # a few clouds, scattered clouds
       (803, 804, 3))                  # cloudy


def wmo(condition_id: int) -> int:
    return next((code for low, high, code in WMO if low <= condition_id <= high), 3)


def hide(text) -> str:
    """``text`` with any API key in it cut out (an address in an error message)."""
    return re.sub(r"(appid=)[^&\s'\"]*", r"\1(hidden)", str(text))


def fetch_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (fixed https host)
            return json.load(response)
    except urllib.error.HTTPError as error:   # (its message and address are left behind: the address has the key)
        if error.code == 401:
            raise Refused("OpenWeather refused the API key") from None
        raise OSError(f"OpenWeather answered {error.code}") from None


def address(kind: str, latitude: float, longitude: float, key: str) -> str:
    """``kind``: "weather" (now) or "forecast" (5 days, every 3 hours)."""
    query = {"lat": latitude, "lon": longitude, "units": "metric", "appid": key}
    return f"{BASE}{kind}?{urllib.parse.urlencode(query)}"


def _daylight(icon: str, at: int, sunrise=None, sunset=None) -> bool:
    if icon.endswith(("d", "n")):
        return icon.endswith("d")
    return sunrise is None or sunset is None or sunrise <= at < sunset


def parse(current: dict, forecast: dict, fetched: float | None = None) -> Reading:
    """The two answers as one reading, in °C and m/s (``converted`` gives a screen its own units)."""
    seconds = int(current.get("timezone", (forecast.get("city") or {}).get("timezone", 0)))
    offset = dt.timedelta(seconds=seconds)

    def local(stamp):
        return (dt.datetime.fromtimestamp(stamp, dt.timezone.utc) + offset).replace(tzinfo=None)

    now, main, sky = current["dt"], current["main"], current["weather"][0]
    sun = current.get("sys") or {}
    hours = tuple(Hour(local(e["dt"]), float(e["main"]["temp"]), wmo(int(e["weather"][0]["id"])),
                       _daylight(e["weather"][0].get("icon", ""), e["dt"]) if e["weather"][0].get("icon")
                       else (e.get("sys") or {}).get("pod", "d") == "d")
                  for e in forecast.get("list") or [])
    today = local(now).date()
    temps = [float(main["temp"])] + [h.temp for h in hours if h.time.date() == today]
    return Reading(
        temp=float(main["temp"]), feels=float(main.get("feels_like", main["temp"])),
        humidity=int(main.get("humidity", 0)), wind=float((current.get("wind") or {}).get("speed", 0.0)),
        code=wmo(int(sky["id"])), is_day=_daylight(sky.get("icon", ""), now, sun.get("sunrise"), sun.get("sunset")),
        high=max(temps), low=min(temps), hours=hours,
        fetched=time.time() if fetched is None else fetched, at=local(now), offset=seconds,
    )


WIND = {"kmh": 3.6, "mph": 2.236936, "ms": 1.0, "kn": 1.943844}   # from m/s


def converted(reading: Reading, units: str, wind_unit: str) -> Reading:
    """``reading`` in the screen's units: °F for "fahrenheit", the wind in ``wind_unit``."""
    temp = (lambda c: c * 9 / 5 + 32) if units == "fahrenheit" else (lambda c: c)
    return replace(reading, temp=temp(reading.temp), feels=temp(reading.feels), high=temp(reading.high),
                   low=temp(reading.low), wind=reading.wind * WIND.get(wind_unit, 1.0),
                   hours=tuple(replace(h, temp=temp(h.temp)) for h in reading.hours))


RETRY_SECONDS, RETRY_MOST = 60, 900   # after a failed fetch: wait a minute, doubling each time, up to 15
OLD_SECONDS = 3 * 3600                # a reading older than this isn't "the weather" any more: don't show it as if it were

_cache: dict[str, Reading] = {}
_in_flight: set[str] = set()
_fails: dict[str, int] = {}        # failed fetches in a row, per slot
_retry_at: dict[str, float] = {}   # no new fetch before this time.time()
_refused: set[str] = set()         # keys (their tags) OpenWeather said no to, until one of them works
_lock = threading.Lock()


def slot(settings: dict, key: str | None = None) -> str:
    """Where one place's weather is kept: by place and key (a new key fetches afresh), not by units."""
    return _slot(settings["latitude"], settings["longitude"], keys.get() if key is None else key)


def _slot(latitude, longitude, key: str) -> str:
    return f"{float(latitude):.4f},{float(longitude):.4f},{keys.tag(key)}"


_days: dict[str, tuple] = {}       # per slot: (the place's date, lowest, highest) seen so far that day


def _the_day(where: str, reading: Reading, before: Reading | None) -> Reading:
    """``reading`` with today's high and low over the whole day so far, not only what's still to come:
    every reading fetched today, and the forecast slots that have passed since the last fetch. (Kept in
    memory: after a restart the first day starts from then.) Call with ``_lock`` held."""
    if reading.at is None:
        return reading
    today = reading.at.date()
    seen = [reading.temp]
    if before is not None:   # what the last fetch said these past hours would be
        seen += [h.temp for h in before.hours if h.time.date() == today and h.time <= reading.at]
    day, low, high = _days.get(where, (today, min(seen), max(seen)))
    if day != today:
        low, high = min(seen), max(seen)
    low, high = min(low, *seen), max(high, *seen)
    _days[where] = (today, low, high)
    return replace(reading, low=min(low, reading.low), high=max(high, reading.high))   # (and what's still to come)


def _keep(where: str, reading: Reading, key: str) -> None:
    """A good reading for ``where``: cached, and any failures or refusal of its key forgotten."""
    _cache[where] = _the_day(where, reading, _cache.get(where))
    _fails.pop(where, None)
    _retry_at.pop(where, None)
    _refused.discard(keys.tag(key))


def _refresh(where: str, latitude: float, longitude: float, key: str) -> None:
    try:
        reading = parse(fetch_json(address("weather", latitude, longitude, key)),
                        fetch_json(address("forecast", latitude, longitude, key)))
        with _lock:
            _keep(where, reading, key)
    except Exception as error:  # network down, bad payload, key refused: keep the last good reading, try again later
        with _lock:             # (not on the next frame: that was a thread and a log line 20 times a second)
            if isinstance(error, Refused):
                _refused.add(keys.tag(key))   # (a new key can take up to two hours to start working)
            _fails[where] = _fails.get(where, 0) + 1
            wait = min(RETRY_SECONDS * 2 ** (_fails[where] - 1), RETRY_MOST)
            _retry_at[where] = time.time() + wait
        log.warning("weather refresh failed: %s; trying again in %d s", hide(error), wait)
    finally:
        with _lock:
            _in_flight.discard(where)


def latest(settings: dict) -> Reading | None:
    """The weather for this screen's place, in its units (None: none yet, no key, or hours old)."""
    key = keys.get()
    if not key:
        return None
    where = slot(settings, key)
    with _lock:
        reading = _cache.get(where)
        stale = reading is None or time.time() - reading.fetched > settings["refresh_minutes"] * 60
        if stale and where not in _in_flight and time.time() >= _retry_at.get(where, 0) \
                and not os.environ.get("BONGO_OFFLINE"):
            _in_flight.add(where)
            threading.Thread(target=_refresh, args=(where, settings["latitude"], settings["longitude"], key),
                             daemon=True, name="weather").start()
    if reading is None or time.time() - reading.fetched > max(OLD_SECONDS, settings["refresh_minutes"] * 120):
        return None   # the network has been down for hours: yesterday's sunshine isn't shown as now
    return converted(reading, settings["units"], settings["wind_unit"])


def failing(settings: dict) -> bool:
    """Whether the last try to fetch this weather failed (so "no reading" means no connection, not "not yet")."""
    with _lock:
        return _fails.get(slot(settings), 0) > 0


NO_KEY = "Add your OpenWeather API key"
REFUSED = "Your OpenWeather API key isn't working"


def trouble() -> str | None:
    """What stops every Weather screen showing anything, in words (None: nothing). The screens skip
    themselves meanwhile, and the dashboard says this on them (Module.trouble)."""
    key = keys.get()
    if not key:
        return NO_KEY
    with _lock:
        return REFUSED if keys.tag(key) in _refused else None


def verify(key: str, latitude: float = 51.5074, longitude: float = -0.1278) -> list[str]:
    """Try a key when it's saved in the dashboard, with the weather for the Matrix's place. Only a refusal
    is a problem: no network, or OpenWeather having a bad moment, doesn't stop the key being saved. When it
    works, that weather is kept, and any waiting after failures with this key is over: the screens show
    it straight away."""
    if not key or os.environ.get("BONGO_OFFLINE"):
        return []
    try:
        current = fetch_json(address("weather", latitude, longitude, key))
        reading = parse(current, fetch_json(address("forecast", latitude, longitude, key)))
    except Refused:
        with _lock:
            _refused.add(keys.tag(key))
        return ["That OpenWeather API key isn't working yet: new keys can take up to two hours. "
                "Check it was copied whole."]
    except Exception as error:
        log.info("couldn't try the OpenWeather key just now: %s", hide(error))
        return []
    tag = keys.tag(key)
    with _lock:
        for where in [w for w in list(_fails) + list(_retry_at) if w.endswith("," + tag)]:
            _fails.pop(where, None)
            _retry_at.pop(where, None)
        _keep(_slot(latitude, longitude, key), reading, key)
    return []


def seed(settings: dict, reading: Reading) -> None:
    """Put a reading (in °C and m/s, as ``parse`` gives it) in the cache (previews and tests)."""
    with _lock:
        _cache[slot(settings)] = reading
