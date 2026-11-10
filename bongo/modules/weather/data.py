"""Open-Meteo (keyless) weather, fetched in the background and cached per location.

``latest(settings)`` never blocks: it returns the cached reading (or None) and starts a
refresh when the cache is stale, so rendering stays smooth with a slow or absent network.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)
URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class Hour:
    time: dt.datetime
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
    hours: tuple[Hour, ...]
    fetched: float  # time.time() of the fetch


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (fixed https host)
        return json.load(response)


def request_url(settings: dict) -> str:
    query = {
        "latitude": settings["latitude"], "longitude": settings["longitude"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                   "wind_speed_10m,is_day",
        "hourly": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto", "forecast_days": 2,
        "temperature_unit": settings["units"], "wind_speed_unit": settings["wind_unit"],
    }
    return f"{URL}?{urllib.parse.urlencode(query)}"


def parse(payload: dict, fetched: float | None = None) -> Reading:
    cur, hourly, daily = payload["current"], payload["hourly"], payload["daily"]
    hours = tuple(
        Hour(dt.datetime.fromisoformat(t), temp, code, bool(day))
        for t, temp, code, day in zip(hourly["time"], hourly["temperature_2m"],
                                      hourly["weather_code"], hourly["is_day"])
    )
    return Reading(
        temp=cur["temperature_2m"], feels=cur["apparent_temperature"],
        humidity=int(cur["relative_humidity_2m"]), wind=cur["wind_speed_10m"],
        code=int(cur["weather_code"]), is_day=bool(cur["is_day"]),
        high=daily["temperature_2m_max"][0], low=daily["temperature_2m_min"][0],
        hours=hours, fetched=time.time() if fetched is None else fetched,
    )


_cache: dict[str, Reading] = {}
_in_flight: set[str] = set()
_lock = threading.Lock()


def _refresh(url: str) -> None:
    try:
        reading = parse(fetch_json(url))
        with _lock:
            _cache[url] = reading
    except Exception as error:  # network down, bad payload: keep the last good reading
        log.warning("weather refresh failed: %s", error)
    finally:
        with _lock:
            _in_flight.discard(url)


def latest(settings: dict) -> Reading | None:
    url = request_url(settings)
    with _lock:
        reading = _cache.get(url)
        stale = reading is None or time.time() - reading.fetched > settings["refresh_minutes"] * 60
        if stale and url not in _in_flight:
            _in_flight.add(url)
            threading.Thread(target=_refresh, args=(url,), daemon=True, name="weather").start()
    return reading


def seed(settings: dict, reading: Reading) -> None:
    """Put a reading in the cache (previews and tests)."""
    with _lock:
        _cache[request_url(settings)] = reading
