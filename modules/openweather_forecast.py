"""OpenWeather 3-hour forecast cards based on the legacy Matrix app."""

from __future__ import annotations

import time
from dataclasses import dataclass

from modules.openweather_common import OpenWeatherClient, OpenWeatherError, composite_icon
from modules.weather_app import _hex_rgb, _render_error, _wait


@dataclass(frozen=True)
class PreparedForecast:
    periods: object = None
    icons: object = None
    error: str | None = None
    cancelled: bool = False


def prepare(config, stop_event=None, client=None, api_key=None):
    settings = config.get("openweather_forecast", {})
    if stop_event and stop_event.is_set():
        return PreparedForecast(cancelled=True)
    if not api_key:
        return PreparedForecast(error="KEY NOT SET")
    client = client or OpenWeatherClient()
    location = settings.get("location", "Brighton, GB")
    units = settings.get("units", "celsius")
    max_items = settings.get("forecast_items", 6)
    try:
        periods = client.fetch_forecast(
            api_key=api_key,
            location=location,
            latitude=settings.get("latitude"),
            longitude=settings.get("longitude"),
            units=units,
            max_items=max_items,
        )
    except OpenWeatherError as exc:
        return PreparedForecast(error=str(exc))
    except Exception:
        return PreparedForecast(error="OpenWeather error")
    if stop_event and stop_event.is_set():
        return PreparedForecast(cancelled=True)

    background_hex = settings.get("background_colour", "#000000")
    icons = {}
    try:
        for period in periods:
            if stop_event and stop_event.is_set():
                return PreparedForecast(cancelled=True)
            if period.icon_code not in icons:
                icons[period.icon_code] = composite_icon(
                    client.fetch_icon(period.icon_code), _hex_rgb(background_hex)
                )
                if stop_event and stop_event.is_set():
                    return PreparedForecast(cancelled=True)
    except OpenWeatherError as exc:
        return PreparedForecast(error=str(exc))
    except Exception:
        return PreparedForecast(error="OpenWeather error")
    return PreparedForecast(periods=periods, icons=icons)


def run(
    matrix, config, duration=30, stop_event=None, client=None, api_key=None,
    prepared=None,
):
    settings = config.get("openweather_forecast", {})
    prepared = prepared or prepare(
        config, stop_event=stop_event, client=client, api_key=api_key
    )
    if prepared.cancelled:
        return False
    if prepared.error:
        _render_error(matrix, prepared.error, duration, stop_event)
        return False
    periods = prepared.periods
    icons = prepared.icons
    location = settings.get("location", "Brighton, GB")
    units = settings.get("units", "celsius")
    suffix = "F" if units == "fahrenheit" else "C"
    background_hex = settings.get("background_colour", "#000000")
    background = matrix.colour_rgb(background_hex)
    text = matrix.colour_rgb(settings.get("text_colour", "#dcd5d5"))
    high = matrix.colour_rgb(settings.get("high_colour", "#ff71ce"))
    low = matrix.colour_rgb(settings.get("low_colour", "#01cdfe"))
    ticker = matrix.colour_rgb(settings.get("ticker_colour", "#dcd5d5"))
    main_font = matrix.font("5x7")
    small_font = matrix.font("4x6")
    ticker_y = settings.get("ticker_y", 28)
    direction = settings.get("scroll_direction", "left")
    step = -1 if direction == "right" else 1
    frame_delay = {"slow": 0.1, "normal": 0.05, "fast": 0.025}.get(
        settings.get("scroll_speed", "normal"), 0.05
    )
    card_seconds = settings.get("card_seconds", 5)
    canvas = matrix.matrix.CreateFrameCanvas()
    active_index = None
    scroll_x = canvas.width
    scroll = ""
    started = time.monotonic()
    end_time = started + duration

    while not (stop_event and stop_event.is_set()):
        now = time.monotonic()
        if now >= end_time:
            break
        index = int((now - started) // card_seconds) % len(periods)
        period = periods[index]
        if index != active_index:
            active_index = index
            scroll = f"{location} | {period.status} | Feels like {period.feels_like}{suffix}"
            if direction == "right":
                text_width = matrix.draw_text(
                    small_font, -10000, ticker_y, ticker, scroll, canvas=canvas
                )
                scroll_x = -text_width
            else:
                scroll_x = canvas.width
        matrix.fill(*_hex_rgb(background_hex), canvas=canvas)
        matrix.draw_image(
            icons[period.icon_code],
            settings.get("icon_x", 0),
            settings.get("icon_y", 0),
            canvas=canvas,
        )
        matrix.draw_text_with_border(
            small_font, settings.get("time_x", 2), settings.get("time_y", 22),
            text, background, period.label, canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("temperature_x", 26), settings.get("temperature_y", 15),
            text, background, f"{period.temperature}{suffix}", canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("high_x", 50), settings.get("high_y", 11),
            high, background, str(period.high), canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("low_x", 50), settings.get("low_y", 20),
            low, background, str(period.low), canvas=canvas,
        )
        scroll_x, _ = matrix.widget_scroll(
            scroll, small_font, scroll_x, ticker_y, ticker,
            canvas=canvas, step=step, wrap=True,
        )
        canvas = matrix.swap(canvas)
        if _wait(stop_event, frame_delay):
            break
    return True
