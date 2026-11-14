"""Weather from Open-Meteo (no API key). Three styles share one location and one reading."""
from bongo.module import Module

from .styles import Forecast, Overview, Ticker

MODULE = Module(
    id="weather",
    name="Weather",
    description="Current conditions and the next few hours, from Open-Meteo.",
    styles={"overview": Overview, "ticker": Ticker, "forecast": Forecast},
    defaults=dict(
        location="London", latitude=51.5074, longitude=-0.1278,
        own_location=False,  # False: the Matrix's location from Settings; True: this screen's own
        units="celsius", wind_unit="kmh", refresh_minutes=10,
        scroll_location=True,  # a location too long for its space scrolls like a ticker
        # the look (Advanced): today's colours by default; the sky changes with the weather unless turned off
        text_colour="#FFF4DE", accent="#FFD9A8", detail_colour="#9AA1AC",
        sky_background=True, background="#0B0C0E",
    ),
    choices={"units": ["celsius", "fahrenheit"], "wind_unit": ["kmh", "mph", "ms", "kn"]},
    basic=("location", "own_location", "units", "scroll_location"),
    limits={"latitude": (-90, 90), "longitude": (-180, 180), "refresh_minutes": (5, 1440), "scroll_speed": (1, 200),
            "columns": (1, 8), "step_hours": (1, 12)},
    places={"location": ("latitude", "longitude")},  # pick a real place; coordinates follow
)
