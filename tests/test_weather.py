import datetime as dt
import random
import time

import pytest

from bongo.module import Context
from bongo.modules.weather import MODULE as WEATHER, data
from bongo.modules.weather.look import condition, icon, label

NOW = dt.datetime(2026, 11, 10, 14, 20)


def payload(code=2, day=1, temp=12.4):
    hours = [NOW.replace(minute=0) + dt.timedelta(hours=h) for h in range(-2, 30)]
    return {"current": {"temperature_2m": temp, "apparent_temperature": temp - 3, "relative_humidity_2m": 81,
                        "weather_code": code, "wind_speed_10m": 14.2, "is_day": day},
            "hourly": {"time": [h.isoformat(timespec="minutes") for h in hours],
                       "temperature_2m": [float(i) for i in range(len(hours))],
                       "weather_code": [61] * len(hours), "is_day": [1] * len(hours)},
            "daily": {"temperature_2m_max": [15.0], "temperature_2m_min": [6.0]}}


def seeded(style, **settings):
    lamp = WEATHER.create(style, settings or None, Context(rng=random.Random(1)))
    data.seed(lamp.settings, data.parse(payload(), fetched=time.time()))
    return lamp


def test_parse():
    r = data.parse(payload(code=63, day=0))
    assert (r.temp, r.code, r.is_day, r.high, r.low, r.humidity) == (12.4, 63, False, 15.0, 6.0, 81)
    assert r.hours[0].time == dt.datetime(2026, 11, 10, 12, 0)


@pytest.mark.parametrize("code,expected", [(0, "clear"), (2, "partly"), (3, "cloudy"), (45, "fog"),
                                           (61, "rain"), (81, "rain"), (73, "snow"), (96, "storm")])
def test_conditions(code, expected):
    assert condition(code) == expected


def test_night_label():
    assert label(0, False) == "Clear night" and label(0, True) == "Clear"


@pytest.mark.parametrize("size", [9, 11, 16, 20])
def test_icons_are_square_rgba(size):
    for code in (0, 2, 3, 45, 61, 71, 95):
        assert icon(code, True, size).size == (size, size)


def test_offline_shows_waiting_screen_and_never_blocks():
    lamp = WEATHER.create("overview", {"location": "Offline", "latitude": 1.0}, Context())
    started = time.monotonic()
    assert lamp.render(NOW).size == (64, 32)
    assert time.monotonic() - started < 0.5


def test_failed_refresh_keeps_last_reading():
    lamp = seeded("overview")
    url = data.request_url(lamp.settings)
    before = data._cache[url]
    data._refresh(url)  # network is disabled in tests
    assert data._cache[url] is before


@pytest.mark.parametrize("style", ["overview", "ticker", "forecast"])
def test_seeded_styles_draw_more_than_the_waiting_screen(style):
    lamp = seeded(style)
    waiting = lamp.waiting(NOW)
    assert lamp.render(NOW).tobytes() != waiting.tobytes()


def test_ticker_scrolls():
    lamp = seeded("ticker")
    assert lamp.render(NOW).tobytes() != lamp.render(NOW + dt.timedelta(seconds=1)).tobytes()


def test_url_carries_units_and_location():
    url = data.request_url(WEATHER.settings_for("overview", {"units": "fahrenheit", "latitude": 40.7}))
    assert "temperature_unit=fahrenheit" in url and "latitude=40.7" in url
