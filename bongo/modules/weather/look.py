"""Shared look for the weather styles: conditions, colours, anti-aliased pixel icons."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw

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
SUN, MOON, CLOUD, CLOUD_DARK = "#FFC94A", "#E8E6D8", "#DCE3EA", "#8C97A6"
DROP, FLAKE, BOLT = "#5AB4FF", "#FFFFFF", "#FFD23F"


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


def icon(code: int, is_day: bool, size: int, phase: float = 0.0) -> Image.Image:
    """RGBA icon, drawn 4x large and box-downsampled so small icons stay smooth on the LEDs.
    ``phase`` (seconds) animates rain, snow and the sun's rays."""
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s))
    d = ImageDraw.Draw(img)
    cond = condition(code)

    def disc(cx, cy, r, fill):
        d.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=fill)

    def sun_or_moon(cx, cy, r):
        if is_day:
            for i in range(8):
                a = i * math.pi / 4 + phase * 0.4
                d.line([((cx + math.cos(a) * r * 1.35) * s, (cy + math.sin(a) * r * 1.35) * s),
                        ((cx + math.cos(a) * r * 1.75) * s, (cy + math.sin(a) * r * 1.75) * s)],
                       fill=SUN, width=max(1, s // 12))
            disc(cx, cy, r, SUN)
        else:
            disc(cx, cy, r, MOON)
            disc(cx + r * 0.55, cy - r * 0.35, r * 0.85, (0, 0, 0, 0))

    def cloud(cy, fill, shade):
        disc(0.34, cy + 0.02, 0.17, shade)
        disc(0.58, cy - 0.06, 0.22, fill)
        disc(0.33, cy, 0.16, fill)
        disc(0.78, cy + 0.04, 0.15, fill)
        d.rounded_rectangle([0.18 * s, cy * s, 0.92 * s, (cy + 0.19) * s], radius=0.09 * s, fill=fill)

    if cond == "clear":
        sun_or_moon(0.5, 0.5, 0.24)
    elif cond == "partly":
        sun_or_moon(0.36, 0.34, 0.2)
        cloud(0.55, CLOUD, CLOUD_DARK)
    elif cond == "fog":
        for i, y in enumerate((0.3, 0.5, 0.7)):
            off = 0.06 * math.sin(phase + i)
            d.rounded_rectangle([(0.12 + off) * s, y * s, (0.88 + off) * s, (y + 0.09) * s],
                                radius=0.045 * s, fill=CLOUD if i != 1 else CLOUD_DARK)
    else:
        dark = cond in ("rain", "storm")
        cloud(0.32, CLOUD_DARK if dark else CLOUD, "#5E6875" if dark else CLOUD_DARK)
        if cond == "rain":
            for i, x in enumerate((0.32, 0.52, 0.72)):
                y = 0.6 + ((phase * 0.9 + i * 0.33) % 1) * 0.28
                d.line([(x * s, y * s), ((x - 0.05) * s, (y + 0.12) * s)], fill=DROP, width=max(1, s // 10))
        elif cond == "snow":
            for i, x in enumerate((0.3, 0.52, 0.74)):
                y = 0.62 + ((phase * 0.5 + i * 0.37) % 1) * 0.28
                disc(x + 0.03 * math.sin(phase * 2 + i), y, 0.055, FLAKE)
        elif cond == "storm":
            d.polygon([(0.55 * s, 0.52 * s), (0.38 * s, 0.76 * s), (0.5 * s, 0.76 * s),
                       (0.42 * s, 0.98 * s), (0.66 * s, 0.68 * s), (0.53 * s, 0.68 * s),
                       (0.62 * s, 0.52 * s)], fill=BOLT)
    return img.resize((size, size), Image.Resampling.BOX)


def temp_text(value: float) -> str:
    return f"{round(value)}°"


def _rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
