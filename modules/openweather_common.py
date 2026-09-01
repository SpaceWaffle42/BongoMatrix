"""Shared OpenWeather transport and normalized provider models."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

from api_timeouts import DEFAULT_API_TIMEOUT_SECONDS

MAX_RESPONSE_BYTES = 256 * 1024
_ICON_CODE = re.compile(r"\d{2}[dn]")


class OpenWeatherError(RuntimeError):
    """Provider failure safe to show without exposing the request URL or key."""


@dataclass(frozen=True)
class OpenWeatherSnapshot:
    location: str
    status: str
    temperature: int
    feels_like: int
    high: int
    low: int
    humidity: int
    icon_code: str


@dataclass(frozen=True)
class OpenWeatherForecastPeriod:
    label: str
    status: str
    temperature: int
    feels_like: int
    high: int
    low: int
    icon_code: str


class OpenWeatherClient:
    current_url = "https://api.openweathermap.org/data/2.5/weather"
    forecast_url = "https://api.openweathermap.org/data/2.5/forecast"
    icon_url = "https://openweathermap.org/img/wn/{icon_code}@2x.png"

    def __init__(self, timeout=DEFAULT_API_TIMEOUT_SECONDS, deadline=None):
        self.timeout = timeout
        self.deadline = deadline

    def _remaining_timeout(self, message):
        timeout = float(self.timeout)
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise OpenWeatherError(message)
        return timeout

    def _get_json(self, base_url, params):
        request = Request(
            f"{base_url}?{urlencode(params)}",
            headers={"User-Agent": "MatrixDisplay/2.0", "Accept": "application/json"},
        )
        try:
            with urlopen(  # nosemgrep: bandit.B310-1 -- fixed reviewed HTTPS provider URL
                request,
                timeout=self._remaining_timeout("OpenWeather request timed out"),
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 401:
                raise OpenWeatherError("OpenWeather key rejected") from None
            if code == 429:
                raise OpenWeatherError("OpenWeather rate limit reached") from None
            raise OpenWeatherError("OpenWeather request failed") from None
        except TimeoutError:
            raise OpenWeatherError("OpenWeather request timed out") from None
        except (URLError, OSError):
            raise OpenWeatherError("OpenWeather unavailable") from None
        self._remaining_timeout("OpenWeather request timed out")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise OpenWeatherError("OpenWeather response too large")
        try:
            result = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenWeatherError("OpenWeather returned invalid data") from None
        if not isinstance(result, dict):
            raise OpenWeatherError("OpenWeather returned invalid data")
        return result

    def fetch_icon(self, icon_code):
        if not isinstance(icon_code, str) or not _ICON_CODE.fullmatch(icon_code):
            raise OpenWeatherError("OpenWeather returned an invalid icon")
        request = Request(
            self.icon_url.format(icon_code=icon_code),
            headers={"User-Agent": "MatrixDisplay/2.0", "Accept": "image/png"},
        )
        try:
            with urlopen(  # nosemgrep: bandit.B310-1 -- fixed reviewed HTTPS provider URL
                request,
                timeout=self._remaining_timeout("OpenWeather icon timed out"),
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except TimeoutError:
            raise OpenWeatherError("OpenWeather icon timed out") from None
        except (HTTPError, URLError, OSError):
            raise OpenWeatherError("OpenWeather icon unavailable") from None
        self._remaining_timeout("OpenWeather icon timed out")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise OpenWeatherError("OpenWeather icon too large")
        try:
            with Image.open(BytesIO(payload)) as source:
                if source.width < 1 or source.height < 1 or source.width > 512 or source.height > 512:
                    raise OpenWeatherError("OpenWeather icon dimensions are invalid")
                source.load()
                return source.convert("RGBA").resize((20, 20), Image.Resampling.LANCZOS)
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
            raise OpenWeatherError("OpenWeather icon is invalid") from None

    @staticmethod
    def _provider_units(units):
        return "imperial" if units == "fahrenheit" else "metric"

    def fetch_current(self, *, api_key, location, latitude=None, longitude=None, units="celsius"):
        params = {
            "appid": api_key,
            "units": self._provider_units(units),
            "lang": "en",
        }
        if latitude is not None and longitude is not None:
            params.update({"lat": latitude, "lon": longitude})
        else:
            params["q"] = location
        payload = self._get_json(self.current_url, params)
        try:
            conditions = payload["weather"][0]
            measurements = payload["main"]
            icon_code = str(conditions["icon"])
            if not _ICON_CODE.fullmatch(icon_code):
                raise ValueError
            return OpenWeatherSnapshot(
                location=location,
                status=str(conditions["description"]).strip().capitalize(),
                temperature=round(measurements["temp"]),
                feels_like=round(measurements["feels_like"]),
                high=round(measurements["temp_max"]),
                low=round(measurements["temp_min"]),
                humidity=round(measurements["humidity"]),
                icon_code=icon_code,
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise OpenWeatherError("OpenWeather returned incomplete data") from None

    def fetch_forecast(
        self, *, api_key, location, latitude=None, longitude=None,
        units="celsius", max_items=6,
    ):
        params = {
            "appid": api_key,
            "units": self._provider_units(units),
            "lang": "en",
            "cnt": max_items,
        }
        if latitude is not None and longitude is not None:
            params.update({"lat": latitude, "lon": longitude})
        else:
            params["q"] = location
        payload = self._get_json(self.forecast_url, params)
        try:
            offset = int(payload.get("city", {}).get("timezone", 0))
            if not -86400 <= offset <= 86400:
                raise ValueError
            periods = []
            for item in payload["list"][:max_items]:
                conditions = item["weather"][0]
                measurements = item["main"]
                icon_code = str(conditions["icon"])
                if not _ICON_CODE.fullmatch(icon_code):
                    raise ValueError
                local_time = datetime.fromtimestamp(item["dt"] + offset, tz=timezone.utc)
                periods.append(OpenWeatherForecastPeriod(
                    label=local_time.strftime("%a %H:%M"),
                    status=str(conditions["description"]).strip().capitalize(),
                    temperature=round(measurements["temp"]),
                    feels_like=round(measurements["feels_like"]),
                    high=round(measurements["temp_max"]),
                    low=round(measurements["temp_min"]),
                    icon_code=icon_code,
                ))
            if not periods:
                raise ValueError
            return periods
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            raise OpenWeatherError("OpenWeather returned incomplete forecast data") from None


def composite_icon(icon, background_rgb):
    """Flatten a provider icon's alpha onto the module's selected background."""
    foreground = icon.convert("RGBA")
    background = Image.new("RGBA", foreground.size, (*background_rgb, 255))
    return Image.alpha_composite(background, foreground).convert("RGB")
