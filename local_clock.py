"""Wall-clock time for the Matrix, taken from the Weather location.

Owner 2026-10-14: "all time should default from the weather location".
A feature's own explicitly saved zone wins; otherwise the Weather location's
IANA zone is used; if that is unknown ("") the device clock is kept.
"""
import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _device_now():
    return dt.datetime.now()


def _valid(zone):
    if not isinstance(zone, str) or not zone:
        return None
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return zone


def valid_zone_info(zone):
    zone = _valid(zone)
    return ZoneInfo(zone) if zone else None


def weather_zone(config):
    weather = config.get("weather") if isinstance(config, dict) else None
    return _valid(weather.get("timezone")) if isinstance(weather, dict) else None


def effective_zone(config, explicit=None):
    """The feature's saved zone if set, else the Weather zone, else None (device)."""
    return _valid(explicit) or weather_zone(config)


def now_in(zone, *, utc_now=None):
    """Naive local wall-clock time in ``zone``; device clock when zone is None."""
    zone = _valid(zone)
    if zone is None:
        return _device_now()
    moment = utc_now if utc_now is not None else _utc_now()
    return moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None)


def to_utc(naive, zone):
    """Absolute UTC instant for naive wall time ``naive`` in ``zone`` (device zone if None)."""
    if zone:
        aware = naive.replace(tzinfo=ZoneInfo(zone))
    else:
        aware = naive.astimezone()  # naive is device-local wall time
    return aware.astimezone(dt.timezone.utc)


def local_now(config, *, utc_now=None):
    return now_in(weather_zone(config), utc_now=utc_now)
