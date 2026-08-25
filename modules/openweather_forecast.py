"""OpenWeather 3-hour forecast cards based on the legacy Matrix app."""

from __future__ import annotations

from dataclasses import dataclass
import time

from font_catalog import text_width
from modules.openweather_common import OpenWeatherClient, OpenWeatherError, composite_icon
from modules.scroll_timing import duration_milliseconds, frame_delay_seconds, scroll_frames
from modules.weather_app import (
    _hex_rgb, _render_error, _wait, measure_ticker, ticker_positions,
)


@dataclass(frozen=True)
class PreparedForecast:
    periods: object = None
    icons: object = None
    error: str | None = None
    cancelled: bool = False


def ticker_text(location, period, units):
    suffix = "F" if units == "fahrenheit" else "C"
    return f"{location} | {period.status} | Feels like {period.feels_like}{suffix}"


def prepare_ticker_plan(config, *, client=None, api_key=None, deadline=None):
    """Fetch normalized forecast text without downloading card icons."""
    if not api_key:
        raise OpenWeatherError("OpenWeather key is not configured")
    settings = config["openweather_forecast"]
    location = config["weather"]
    remaining = 5 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("provider timing deadline exceeded")
    client = client or OpenWeatherClient(timeout=min(5, remaining))
    if client is not None and hasattr(client, "timeout"):
        client.timeout = min(client.timeout, remaining)
    periods = client.fetch_forecast(
        api_key=api_key, location=location["location"], latitude=location.get("latitude"),
        longitude=location.get("longitude"), units=settings["units"],
        max_items=settings["forecast_items"],
    )
    if not periods:
        raise OpenWeatherError("OpenWeather forecast is unavailable")
    texts = [ticker_text(location["location"], period, settings["units"]) for period in periods]
    frames = [scroll_frames(text_width("4x6", text), direction=settings["scroll_direction"]) for text in texts]
    total_frames = sum(frames) * settings["scroll_loops"]
    return {"texts": texts, "frames_per_loop": frames, "total_frames": total_frames,
            "loops": settings["scroll_loops"], "frame_delay": settings["scroll_speed"],
            "milliseconds": duration_milliseconds(total_frames, 1, settings["scroll_speed"])}


def prepare(config, stop_event=None, client=None, api_key=None):
    settings = config.get("openweather_forecast", {})
    location_settings = config.get("weather", settings)
    if stop_event and stop_event.is_set():
        return PreparedForecast(cancelled=True)
    if not api_key:
        return PreparedForecast(error="KEY NOT SET")
    client = client or OpenWeatherClient()
    location = location_settings.get("location", "London, England, GB")
    units = settings.get("units", "celsius")
    max_items = settings.get("forecast_items", 6)
    try:
        periods = client.fetch_forecast(
            api_key=api_key,
            location=location,
            latitude=location_settings.get("latitude"),
            longitude=location_settings.get("longitude"),
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


def forecast_frames(matrix, config, prepared, stop_event=None):
    """Submit and yield every prepared forecast card and ticker traversal."""
    settings = config.get("openweather_forecast", {})
    periods = prepared.periods
    icons = prepared.icons
    location = config.get("weather", settings).get(
        "location", "London, England, GB"
    )
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
    frame_delay = frame_delay_seconds(settings.get("scroll_speed", 0.05))
    loops_per_card = settings.get("scroll_loops", 1)
    canvas = matrix.matrix.CreateFrameCanvas()

    for period in periods:
        if stop_event and stop_event.is_set():
            break
        scroll = ticker_text(location, period, units)
        text_width = measure_ticker(
            matrix, small_font, ticker_y, ticker, scroll, canvas,
        )
        for scroll_x in ticker_positions(
            canvas.width, text_width, loops_per_card, direction,
        ):
            if stop_event and stop_event.is_set():
                return
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
            matrix.widget_scroll(
                scroll, small_font, scroll_x, ticker_y, ticker,
                canvas=canvas, step=step, wrap=False,
            )
            if stop_event and stop_event.is_set():
                return
            submitted = canvas
            canvas = matrix.swap(canvas)
            yield submitted, frame_delay


def run(
    matrix, config, duration=30, stop_event=None, client=None, api_key=None,
    prepared=None,
):
    prepared = prepared or prepare(
        config, stop_event=stop_event, client=client, api_key=api_key
    )
    if prepared.cancelled:
        return False
    if prepared.error:
        _render_error(matrix, prepared.error, duration, stop_event)
        return False
    for _canvas, frame_delay in forecast_frames(matrix, config, prepared, stop_event):
            if _wait(stop_event, frame_delay):
                return True
    return True
