"""The weather on the tree: what the last cached OpenWeather reading says, and the rain, snow, fog, wind and
lightning drawn over the picture. Nothing is fetched here: a Weather screen for the same place keeps the
reading fresh, and without one (or without an API key) the tree follows the season alone."""
from __future__ import annotations

import math
from dataclasses import dataclass

from bongo.modules.weather import data
from bongo.modules.weather.look import condition

from .year import mix, unit

WINDY, GALE = 8.0, 14.0     # m/s: leaves stir; leaves fly and the strands of rain lean


@dataclass(frozen=True)
class Weather:
    kind: str = "clear"      # clear, partly, cloudy, rain, snow, storm, fog
    wind: int = 0            # 0 calm, 1 windy, 2 a gale
    frost: bool = False      # at or below freezing (and not snowing)
    day: bool | None = None  # the reading's own day or night, if there is one

    @property
    def wet(self) -> bool:
        return self.kind in ("rain", "storm")


CALM = Weather()


def now(home: dict | None) -> Weather:
    """The weather at the Matrix's location, from the cache (CALM when there's nothing to go on)."""
    if not home:
        return CALM
    try:
        reading = data.cached(home["latitude"], home["longitude"])
    except (KeyError, TypeError, ValueError):
        return CALM
    if reading is None:
        return CALM
    kind = condition(reading.code)
    wind = 2 if reading.wind >= GALE else 1 if reading.wind >= WINDY else 0
    return Weather(kind, wind, reading.temp <= 0 and kind != "snow", reading.is_day)


RAIN, RAIN_TAIL, SNOW, MIST, STREAK, BOLT = (90, 150, 255), (44, 84, 170), (205, 220, 250), (112, 120, 134), (150, 160, 176), (250, 246, 205)


def _put(img, x, y, c, keep_out) -> None:
    x, y = int(round(x)), int(round(y))
    if 0 <= x < img.width and 0 <= y < img.height and not keep_out(x, y):
        img.putpixel((x, y), c)


def draw(img, w: Weather, t: float, keep_out) -> None:
    """Rain, snow, mist, wind and lightning over ``img`` at ``t`` seconds; never where ``keep_out(x, y)``
    (the time and the space around it)."""
    W, H = img.size
    lean = 0.5 * w.wind
    if w.wet:
        for i in range(22 if w.kind == "storm" else 15):
            speed = 24 + 10 * unit("rain", i, 1)
            y = (unit("rain", i, 0) * H + t * speed) % (H - 1)
            x = (unit("rain", i, 2) * W + y * lean) % W
            _put(img, x, y, RAIN, keep_out)
            _put(img, x - lean, y - 1, RAIN_TAIL, keep_out)
    if w.kind == "snow":
        for i in range(14):
            y = (unit("snow", i, 0) * H + t * (3 + 3 * unit("snow", i, 1))) % (H - 1)
            x = (unit("snow", i, 2) * W + math.sin(t * 0.8 + i) * 1.5 + y * lean) % W
            _put(img, x, y, SNOW, keep_out)
    if w.kind == "fog":                                     # long, slow, faint bands of mist across the whole width
        for i in range(5):
            y = 4 + i * 6 + int(unit("fog", i, 0) * 3)
            x0 = (t * (0.8 + 0.6 * unit("fog", i, 2)) + unit("fog", i, 1) * W) % (W + 24) - 24
            for j in range(26 + int(unit("fog", i, 3) * 14)):
                if unit("gap", i, j) > 0.2:
                    _put(img, x0 + j, y, MIST, keep_out)
    if w.wind:
        for i in range(2 + 2 * w.wind):                     # the wind itself: short, bright, quick streaks
            y = 2 + int(unit("wind", i, 0) * 9) + (18 if i % 2 else 0)
            x0 = (unit("wind", i, 1) * (W + 12) + t * (30 + 14 * unit("wind", i, 2))) % (W + 12) - 6
            for j in range(3):
                if not any(img.getpixel((int(x0 - j) % W, y))):   # only in the open, never across the tree
                    _put(img, x0 - j, y, mix((0, 0, 0), STREAK, 1 - j * 0.3), keep_out)
    if strike(w, t) >= 0:                                   # a bolt in a top corner, above the time and clear of the tree
        side = int(t // 6.5) % 2
        x0 = 5 if side == 0 else W - 7
        for dx, dy in ((0, 0), (1, 1), (1, 2), (0, 3), (-1, 4), (0, 5), (1, 6), (2, 7)):
            _put(img, x0 + dx, dy, BOLT, keep_out)


def strike(w: Weather, t: float) -> float:
    """How far into a lightning strike ``t`` is (seconds), or -1: none showing. One every 6.5 s in a storm."""
    if w.kind != "storm":
        return -1
    k = t % 6.5 - 6.2
    return k if k >= 0 else -1
