"""Wall-clock time for the Matrix, taken from the Weather location.

Owner 2026-10-14: "all time should default from the weather location. but
fallback to UTC". A feature's own explicitly saved zone wins; otherwise the
Weather location's IANA zone is used; if that is unknown ("") or invalid, UTC.
The device clock's zone is never used.
"""
import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FALLBACK_ZONE = "UTC"


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _valid(zone):
    if not isinstance(zone, str) or not zone:
        return None
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return zone


def valid_zone_info(zone):
    """ZoneInfo for ``zone``, or UTC when it is blank/unknown."""
    return ZoneInfo(_valid(zone) or FALLBACK_ZONE)


def weather_zone(config):
    """The Weather location's zone, or UTC when unknown."""
    weather = config.get("weather") if isinstance(config, dict) else None
    zone = _valid(weather.get("timezone")) if isinstance(weather, dict) else None
    return zone or FALLBACK_ZONE


def effective_zone(config, explicit=None):
    """The feature's saved zone if set, else the Weather zone, else UTC."""
    return _valid(explicit) or weather_zone(config)


def now_in(zone, *, utc_now=None):
    """Naive local wall-clock time in ``zone`` (UTC when blank/unknown)."""
    moment = utc_now if utc_now is not None else _utc_now()
    return moment.astimezone(valid_zone_info(zone)).replace(tzinfo=None)


def to_utc(naive, zone):
    """Absolute UTC instant for naive wall time ``naive`` in ``zone`` (UTC when blank/unknown)."""
    return naive.replace(tzinfo=valid_zone_info(zone)).astimezone(dt.timezone.utc)


def local_now(config, *, utc_now=None):
    return now_in(weather_zone(config), utc_now=utc_now)
