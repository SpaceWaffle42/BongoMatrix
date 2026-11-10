"""Place search for location settings: type a name, pick a real place, get its coordinates.

Uses Open-Meteo's keyless geocoding (the same provider as the weather), so a location is
always one that exists and its latitude/longitude always match it.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_BYTES = 256 * 1024
_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_SECONDS = 24 * 3600


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "BongoMatrix/3", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 (fixed https host)
        return json.loads(response.read(MAX_BYTES))


def label(place: dict) -> str:
    """"Cheltenham, England, GB": town, region (when it adds something) and country code."""
    parts = [place.get("name", "")]
    region = place.get("admin1")
    if region and region != parts[0]:
        parts.append(region)
    if place.get("country_code"):
        parts.append(place["country_code"].upper())
    return ", ".join(p for p in parts if p)


def search(query: str, count: int = 8) -> list[dict]:
    """Up to ``count`` places matching ``query``: [{label, name, region, country, latitude, longitude}]."""
    query = " ".join(str(query).split())[:80]
    if len(query) < 2:
        return []
    key = query.lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1][:count]
    data = fetch_json(f"{URL}?{urllib.parse.urlencode({'name': query, 'count': 10, 'language': 'en', 'format': 'json'})}")
    out = []
    for p in data.get("results") or []:
        try:
            lat, lon = round(float(p["latitude"]), 4), round(float(p["longitude"]), 4)
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"label": label(p), "name": p.get("name", ""), "region": p.get("admin1") or "",
                    "country": p.get("country") or "", "latitude": lat, "longitude": lon})
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = (time.time(), out)
    return out[:count]
