"""Place search for location settings: type a name, pick a real place, get its coordinates.

Uses OpenWeather's free Geocoding API with the Matrix's OpenWeather API key (Settings > Weather), so a
location is always one that exists and its latitude/longitude always match it. Without a key there is no
search (``NoKey``): the dashboard then takes latitude and longitude typed by hand. The key is never
logged: the weather module's ``fetch_json`` turns an HTTP error into one without the address, and the
dashboard logs any other failure through ``weather.data.hide``, which cuts the key out of it.
"""
from __future__ import annotations

import time
import urllib.parse

URL = "https://api.openweathermap.org/geo/1.0/direct"
_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_SECONDS = 24 * 3600


class NoKey(Exception):
    """No OpenWeather API key saved yet: nothing to search with."""


def fetch_json(url: str):
    from bongo.modules.weather import data   # (its fetch: a timeout, and the key never in an error)
    return data.fetch_json(url)


def label(place: dict) -> str:
    """"Cheltenham, England, GB": town, region (when it adds something) and country code."""
    parts = [place.get("name", "")]
    region = place.get("state")
    if region and region != parts[0]:
        parts.append(region)
    if place.get("country"):
        parts.append(str(place["country"]).upper())
    return ", ".join(p for p in parts if p)


def search(query: str, count: int = 5) -> list[dict]:
    """Up to ``count`` places matching ``query``: [{label, name, region, country, latitude, longitude}].
    Raises NoKey with no OpenWeather key, ``weather.data.Refused`` when OpenWeather refuses it, OSError
    when it can't be reached."""
    from bongo.modules.weather import key as keys
    key = keys.get()
    if not key:   # (asked first, so the dashboard can say so before anything is typed)
        raise NoKey("Add your OpenWeather API key in Settings > Weather to search for places")
    query = " ".join(str(query).split())[:80]
    if len(query) < 2:
        return []
    hit = _cache.get(query.lower())
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1][:count]
    answer = fetch_json(f"{URL}?{urllib.parse.urlencode({'q': query, 'limit': 5, 'appid': key})}")
    out, seen = [], set()
    for p in answer if isinstance(answer, list) else []:
        try:
            lat, lon = round(float(p["lat"]), 4), round(float(p["lon"]), 4)
        except (KeyError, TypeError, ValueError):
            continue
        if label(p) in seen:   # (OpenWeather can list one town twice, a few metres apart)
            continue
        seen.add(label(p))
        out.append({"label": label(p), "name": p.get("name", ""), "region": p.get("state") or "",
                    "country": str(p.get("country") or "").upper(), "latitude": lat, "longitude": lon})
    if len(_cache) > 200:
        _cache.clear()
    _cache[query.lower()] = (time.time(), out)
    return out[:count]
