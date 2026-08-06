"""Keyless, configurable weather module backed by Open-Meteo."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from modules.scroll_timing import frame_delay_seconds


@dataclass(frozen=True)
class WeatherSnapshot:
    location: str
    status: str
    temperature: int
    feels_like: int
    high: int
    low: int
    humidity: int


WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers",
    81: "Heavy showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Storm with hail",
    99: "Heavy hail storm",
}


class OpenMeteoClient:
    geocoding_url = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_url = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, timeout=8):
        self.timeout = timeout

    @staticmethod
    def status_for_code(code):
        return WEATHER_CODES.get(int(code), "Unknown")

    def _get_json(self, base_url, params):
        request = Request(
            f"{base_url}?{urlencode(params)}",
            headers={"User-Agent": "MatrixDisplay/2.0"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def search_locations(self, query, count=6):
        geo = self._get_json(
            self.geocoding_url,
            {"name": query.strip(), "count": count, "language": "en", "format": "json"},
        )
        suggestions = []
        seen = set()
        for place in geo.get("results") or []:
            parts = [place.get("name"), place.get("admin1"), place.get("country_code")]
            label = ", ".join(str(part).strip() for part in parts if part)
            key = (label, place.get("latitude"), place.get("longitude"))
            if not label or key in seen:
                continue
            seen.add(key)
            suggestions.append({
                "label": label,
                "latitude": place["latitude"],
                "longitude": place["longitude"],
            })
        return suggestions

    def fetch(self, location, latitude=None, longitude=None, units="celsius"):
        if latitude is not None and longitude is not None:
            place = {
                "name": location,
                "latitude": latitude,
                "longitude": longitude,
            }
        else:
            place = None
        query = location.split(",", 1)[0].strip()
        if place is None:
            geo = self._get_json(
                self.geocoding_url,
                {"name": query, "count": 10, "language": "en", "format": "json"},
            )
            results = geo.get("results") or []
            if not results:
                raise ValueError(f"Location not found: {location}")

            country_hint = location.rsplit(",", 1)[1].strip().upper() if "," in location else ""
            place = next(
                (
                    item
                    for item in results
                    if country_hint
                    and country_hint in {str(item.get("country_code", "")).upper(), str(item.get("country", "")).upper()}
                ),
                results[0],
            )
        weather = self._get_json(
            self.forecast_url,
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "forecast_days": 1,
                "temperature_unit": units,
            },
        )
        current = weather["current"]
        daily = weather["daily"]
        label = location if latitude is not None else f"{place['name']}, {place.get('country_code', '')}".rstrip(", ")
        return WeatherSnapshot(
            location=label,
            status=self.status_for_code(current["weather_code"]),
            temperature=round(current["temperature_2m"]),
            feels_like=round(current["apparent_temperature"]),
            high=round(daily["temperature_2m_max"][0]),
            low=round(daily["temperature_2m_min"][0]),
            humidity=round(current["relative_humidity_2m"]),
        )


def _wait(stop_event, seconds):
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def _hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _render_error(matrix, message, duration, stop_event):
    canvas = matrix.matrix.CreateFrameCanvas()
    matrix.clear(canvas)
    white = matrix.colour("white")
    red = matrix.colour("red")
    black = matrix.colour("black")
    matrix.draw_text_with_border(matrix.font("5x7"), 7, 12, red, black, "WEATHER", canvas=canvas)
    matrix.draw_text_with_border(matrix.font("4x6"), 2, 25, white, black, message[:15], canvas=canvas)
    matrix.swap(canvas)
    _wait(stop_event, min(duration if duration > 0 else 5, 5))


def run(matrix, config, duration=20, stop_event=None, client=None):
    if stop_event and stop_event.is_set():
        return False
    client = client or OpenMeteoClient()
    weather_config = config.get("weather", {})
    location = weather_config.get("location", "London, England, GB")
    units = weather_config.get("units", "celsius")
    unit_suffix = "F" if units == "fahrenheit" else "C"
    temperature_y = weather_config.get("temperature_y", 17)
    ticker_y = weather_config.get("ticker_y", 31)
    scroll_direction = weather_config.get("scroll_direction", "left")
    frame_delay = frame_delay_seconds(weather_config.get("scroll_speed", 0.05))
    now_x = weather_config.get("now_x", 1)
    now_y = weather_config.get("now_y", 7)
    temperature_x = weather_config.get("temperature_x", 2)
    high_x = weather_config.get("high_x", 35)
    high_y = weather_config.get("high_y", 8)
    low_x = weather_config.get("low_x", 35)
    low_y = weather_config.get("low_y", 15)
    line_y = weather_config.get("line_y", ticker_y - 11)
    try:
        weather = client.fetch(
            location,
            latitude=weather_config.get("latitude"),
            longitude=weather_config.get("longitude"),
            units=units,
        )
    except Exception as exc:
        _render_error(matrix, str(exc), duration, stop_event)
        return False

    canvas = matrix.matrix.CreateFrameCanvas()
    background_hex = weather_config.get("background_colour", "#000000")
    white = matrix.colour_rgb(weather_config["text_colour"]) if "text_colour" in weather_config else matrix.colour("retro_soft_grey")
    black = matrix.colour_rgb(background_hex) if "background_colour" in weather_config else matrix.colour("black")
    pink = matrix.colour_rgb(weather_config["high_colour"]) if "high_colour" in weather_config else matrix.colour("vapor_pink")
    cyan = matrix.colour_rgb(weather_config["low_colour"]) if "low_colour" in weather_config else matrix.colour("vapor_cyan")
    yellow = matrix.colour_rgb(weather_config["ticker_colour"]) if "ticker_colour" in weather_config else matrix.colour("neon_yellow")
    blue = matrix.colour_rgb(weather_config["line_colour"]) if "line_colour" in weather_config else matrix.colour("retro_aqua")
    small = matrix.font("4x6")
    main = matrix.font("5x7")
    scroll = f"{weather.location} | {weather.status} | Feels {weather.feels_like}{unit_suffix} | Humidity {weather.humidity}%"
    text_width = matrix.draw_text(
        small, -10000, ticker_y, yellow, scroll, canvas=canvas
    )
    if scroll_direction == "right":
        scroll_x = -text_width
        scroll_step = -1
    else:
        scroll_x = canvas.width
        scroll_step = 1
    loops_remaining = weather_config.get("scroll_loops", 1)

    while loops_remaining > 0 and not (stop_event and stop_event.is_set()):
        if "background_colour" in weather_config:
            matrix.fill(*_hex_rgb(background_hex), canvas=canvas)
        else:
            matrix.clear(canvas)
        matrix.draw_text_with_border(small, now_x, now_y, white, black, "NOW", canvas=canvas)
        matrix.draw_text_with_border(main, temperature_x, temperature_y, white, black, f"{weather.temperature}{unit_suffix}", canvas=canvas)
        matrix.draw_text_with_border(small, high_x, high_y, pink, black, f"H {weather.high}", canvas=canvas)
        matrix.draw_text_with_border(small, low_x, low_y, cyan, black, f"L {weather.low}", canvas=canvas)
        matrix.draw_line(0, line_y, 63, line_y, blue, canvas=canvas)
        scroll_x, _ = matrix.widget_scroll(
            scroll, small, scroll_x, ticker_y, yellow,
            canvas=canvas, step=scroll_step, wrap=False,
        )
        if scroll_step >= 0 and scroll_x + text_width < 0:
            loops_remaining -= 1
            scroll_x = canvas.width
        elif scroll_step < 0 and scroll_x > canvas.width:
            loops_remaining -= 1
            scroll_x = -text_width
        canvas = matrix.swap(canvas)
        if _wait(stop_event, frame_delay):
            break
    return True


def overview(matrix, config, duration=20, stop_event=None):
    return run(matrix, config, duration, stop_event)
