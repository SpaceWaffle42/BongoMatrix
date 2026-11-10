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
        units="celsius", wind_unit="kmh", refresh_minutes=10,
        scroll_location=True,  # a location too long for its space scrolls like a ticker
    ),
    choices={"units": ["celsius", "fahrenheit"], "wind_unit": ["kmh", "mph", "ms", "kn"]},
    basic=("location", "latitude", "longitude", "units", "scroll_location"),
)
