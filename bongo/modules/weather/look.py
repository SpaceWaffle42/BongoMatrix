"""Shared look for the weather styles: conditions, colours, pixel icons."""
from __future__ import annotations

from PIL import Image, ImageDraw

from bongo import sprites

# WMO weather code -> condition
def condition(code: int) -> str:
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "cloudy"


LABELS = {"clear": "Clear", "partly": "Partly cloudy", "cloudy": "Cloudy", "fog": "Fog",
          "rain": "Rain", "snow": "Snow", "storm": "Thunderstorm"}
NIGHT_LABELS = {"clear": "Clear night"}

# Background gradients (top, bottom) per condition; night variants for clear/partly.
SKIES = {
    "clear": ("#123A6B", "#0A1A30"), "partly": ("#163452", "#0A1626"),
    "cloudy": ("#262D36", "#101318"), "fog": ("#2A2E33", "#121416"),
    "rain": ("#1A2838", "#090F17"), "snow": ("#283246", "#11161F"),
    "storm": ("#261C36", "#0C0914"),
    "clear_night": ("#0B1230", "#03050F"), "partly_night": ("#0F1528", "#04060D"),
}
TEXT, ACCENT, DIM = (255, 244, 222), (255, 217, 168), (154, 161, 172)


def label(code: int, is_day: bool) -> str:
    cond = condition(code)
    return LABELS[cond] if is_day else NIGHT_LABELS.get(cond, LABELS[cond])


def sky(code: int, is_day: bool, width: int, height: int) -> Image.Image:
    cond = condition(code)
    top, bottom = SKIES.get(f"{cond}_night" if not is_day else cond, SKIES[cond])
    t, b = _rgb(top), _rgb(bottom)
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        f = y / max(1, height - 1)
        draw.line([(0, y), (width, y)], fill=tuple(round(t[i] + (b[i] - t[i]) * f) for i in range(3)))
    return img


SIZES = (20, 16, 14, 11)   # the sizes every icon is drawn at: the ones the screens use


def icon_size(room: int, most: int) -> int | None:
    """The size to draw an icon in ``room`` pixels, at most ``most``: the biggest drawn size that fits, so on a
    smaller screen (a safe area) the icon shrinks before any text has to. None when even the smallest
    drawing (11) doesn't fit: the screen then makes room some other way."""
    return next((s for s in SIZES if s <= most and INK[s] <= room), None)


INK = {20: 19, 16: 16, 14: 14, 11: 11}   # rows each drawing really uses (the 20s keep their last row clear)


def icon(code: int, is_day: bool, size: int, phase: float = 0.0) -> Image.Image:
    """The RGBA icon for a weather code, ``size`` pixels square, at ``phase`` seconds into its movement (the
    sun's rays, falling rain and snow, drifting fog, lightning).

    Each is a pixel drawing made for its size, never a scaled one, so every pixel is fully on or off on the
    LEDs: Animator projects ``resources/animator/weather_<condition>_<size>.json``. Save your own under the
    same name from the Animator and the screens show yours (it loops, whatever it was saved as). A size
    nothing was drawn for gets the biggest drawing that fits, in the middle; under 11 pixels the smallest
    one is cropped to fit. The picture is shared: paste it, don't draw on it."""
    cond = condition(code)
    name = f"weather_{cond}_night" if not is_day and cond in ("clear", "partly") else f"weather_{cond}"
    art = None
    for drawn in sorted(SIZES, key=lambda s: (s > size, -s if s <= size else s)):   # the biggest that fits, else the smallest
        try:
            sprite = sprites.load(f"{name}_{drawn}")
            if sprite.size != (drawn, drawn):              # yours, saved at another size: the built-in one
                sprite = sprites.load(f"{name}_{drawn}", own=False)
        except FileNotFoundError:
            continue
        if sprite.loop == "once":                          # the clock never restarts, so "once" would freeze on its last frame
            phase %= sum(sprite.ms) / 1000
        art = sprite.at(phase)
        if not art.getbbox() and not any(f.getbbox() for f in sprite.frames):   # yours, with nothing drawn: the built-in one
            art = sprites.load(f"{name}_{drawn}", own=False).at(phase)
        break
    if art is None:
        raise FileNotFoundError(f"no drawing of {name} in resources/animator")
    if art.size == (size, size):
        return art
    out = Image.new("RGBA", (size, size))
    out.paste(art, ((size - art.width) // 2, (size - art.height) // 2))
    return out


def temp_text(value: float) -> str:
    return f"{round(value)}°"


def _rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
