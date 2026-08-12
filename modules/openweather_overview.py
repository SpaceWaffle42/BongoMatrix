"""OpenWeather current-conditions overview based on the legacy Matrix app."""

from __future__ import annotations

from dataclasses import dataclass
import time

from font_catalog import text_width
from modules.openweather_common import OpenWeatherClient, OpenWeatherError, composite_icon
from modules.scroll_timing import duration_milliseconds, frame_delay_seconds, scroll_frames
from modules.weather_app import _hex_rgb, _render_error, _wait


@dataclass(frozen=True)
class PreparedOverview:
    weather: object = None
    icon: object = None
    error: str | None = None
    cancelled: bool = False


def ticker_text(weather, units):
    suffix = "F" if units == "fahrenheit" else "C"
    return f"{weather.location} | {weather.status} | Feels like {weather.feels_like}{suffix}"


def prepare_ticker_plan(config, *, client=None, api_key=None, deadline=None):
    """Fetch normalized current text without downloading the display icon."""
    if not api_key:
        raise OpenWeatherError("OpenWeather key is not configured")
    settings = config["openweather_overview"]
    location = config["weather"]
    remaining = 5 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("provider timing deadline exceeded")
    client = client or OpenWeatherClient(timeout=min(5, remaining))
    if client is not None and hasattr(client, "timeout"):
        client.timeout = min(client.timeout, remaining)
    weather = client.fetch_current(
        api_key=api_key, location=location["location"], latitude=location.get("latitude"),
        longitude=location.get("longitude"), units=settings["units"],
    )
    text = ticker_text(weather, settings["units"])
    frames = scroll_frames(text_width("4x6", text), direction=settings["scroll_direction"])
    return {"texts": [text], "frames_per_loop": frames,
            "total_frames": frames * settings["scroll_loops"], "loops": settings["scroll_loops"],
            "frame_delay": settings["scroll_speed"],
            "milliseconds": duration_milliseconds(frames, settings["scroll_loops"], settings["scroll_speed"])}


def prepare(config, stop_event=None, client=None, api_key=None):
    settings = config.get("openweather_overview", {})
    location_settings = config.get("weather", settings)
    if stop_event and stop_event.is_set():
        return PreparedOverview(cancelled=True)
    if not api_key:
        return PreparedOverview(error="KEY NOT SET")
    client = client or OpenWeatherClient()
    location = location_settings.get("location", "London, England, GB")
    units = settings.get("units", "celsius")
    try:
        weather = client.fetch_current(
            api_key=api_key,
            location=location,
            latitude=location_settings.get("latitude"),
            longitude=location_settings.get("longitude"),
            units=units,
        )
        if stop_event and stop_event.is_set():
            return PreparedOverview(cancelled=True)
        icon = client.fetch_icon(weather.icon_code)
        if stop_event and stop_event.is_set():
            return PreparedOverview(cancelled=True)
    except OpenWeatherError as exc:
        return PreparedOverview(error=str(exc))
    except Exception:
        return PreparedOverview(error="OpenWeather error")
    background_hex = settings.get("background_colour", "#000000")
    return PreparedOverview(
        weather=weather,
        icon=composite_icon(icon, _hex_rgb(background_hex)),
    )


def run(
    matrix, config, duration=20, stop_event=None, client=None, api_key=None,
    prepared=None,
):
    settings = config.get("openweather_overview", {})
    prepared = prepared or prepare(
        config, stop_event=stop_event, client=client, api_key=api_key
    )
    if prepared.cancelled:
        return False
    if prepared.error:
        _render_error(matrix, prepared.error, duration, stop_event)
        return False
    weather = prepared.weather
    icon = prepared.icon
    suffix = "F" if settings.get("units", "celsius") == "fahrenheit" else "C"

    canvas = matrix.matrix.CreateFrameCanvas()
    background_hex = settings.get("background_colour", "#000000")
    background = matrix.colour_rgb(background_hex)
    text = matrix.colour_rgb(settings.get("text_colour", "#dcd5d5"))
    high = matrix.colour_rgb(settings.get("high_colour", "#ff71ce"))
    low = matrix.colour_rgb(settings.get("low_colour", "#01cdfe"))
    ticker = matrix.colour_rgb(settings.get("ticker_colour", "#dcd5d5"))
    main_font = matrix.font("5x7")
    ticker_font = matrix.font("4x6")
    ticker_y = settings.get("ticker_y", 28)
    scroll = ticker_text(weather, settings.get("units", "celsius"))
    direction = settings.get("scroll_direction", "left")
    text_width = matrix.draw_text(
        ticker_font, -10000, ticker_y, ticker, scroll, canvas=canvas
    )
    if direction == "right":
        scroll_x = -text_width
        step = -1
    else:
        scroll_x = canvas.width
        step = 1
    frame_delay = frame_delay_seconds(settings.get("scroll_speed", 0.05))
    loops_remaining = settings.get("scroll_loops", 1)

    while loops_remaining > 0 and not (stop_event and stop_event.is_set()):
        matrix.fill(*_hex_rgb(background_hex), canvas=canvas)
        matrix.draw_image(
            icon,
            settings.get("icon_x", 0),
            settings.get("icon_y", 0),
            canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("now_x", 2), settings.get("now_y", 22),
            text, background, "NOW", canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("temperature_x", 26), settings.get("temperature_y", 15),
            text, background, f"{weather.temperature}{suffix}", canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("high_x", 50), settings.get("high_y", 11),
            high, background, str(weather.high), canvas=canvas,
        )
        matrix.draw_text_with_border(
            main_font, settings.get("low_x", 50), settings.get("low_y", 20),
            low, background, str(weather.low), canvas=canvas,
        )
        scroll_x, _ = matrix.widget_scroll(
            scroll, ticker_font, scroll_x, ticker_y, ticker,
            canvas=canvas, step=step, wrap=False,
        )
        if step >= 0 and scroll_x + text_width < 0:
            loops_remaining -= 1
            scroll_x = canvas.width
        elif step < 0 and scroll_x > canvas.width:
            loops_remaining -= 1
            scroll_x = -text_width
        canvas = matrix.swap(canvas)
        if _wait(stop_event, frame_delay):
            break
    return True
