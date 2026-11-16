"""Shared bits of the Clock styles: the time as text, glow, and the 12/24-hour setting."""
from __future__ import annotations

import datetime as dt

import numpy as np
from PIL import Image, ImageFilter

HOURS = ["24", "12"]  # "12": 12-hour time without a leading zero (9:41, not 09:41)


def hm(now: dt.datetime, hours: str, seconds: bool = False) -> str:
    """"17:05" or "5:05" (12-hour), with ":SS" when ``seconds``."""
    h = now.hour if hours == "24" else (now.hour % 12 or 12)
    text = f"{h:02d}:{now.minute:02d}" if hours == "24" else f"{h}:{now.minute:02d}"
    return text + (f":{now.second:02d}" if seconds else "")


def digits(now: dt.datetime, hours: str, seconds: bool = False) -> str:
    """The time as digits only, always two per field: "1705" / "170542" (12-hour keeps "0505")."""
    h = now.hour if hours == "24" else (now.hour % 12 or 12)
    return f"{h:02d}{now.minute:02d}" + (f"{now.second:02d}" if seconds else "")


def colon_on(now: dt.datetime) -> bool:
    return now.microsecond < 500_000


def glow(img: Image.Image, radius: float = 2, strength: float = 1.0) -> Image.Image:
    """Light bleeding around bright pixels, as an LED or a tube would."""
    blur = img.filter(ImageFilter.GaussianBlur(radius))
    out = np.asarray(img, float) + np.asarray(blur, float) * strength
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def centre(font, text: str, baseline: int, img: Image.Image, colour, width: int | None = None) -> None:
    font.draw(img, ((width or img.width) - font.width(text)) // 2, baseline, text, colour)


def tall(font, ch: str, height_scale: int = 2) -> Image.Image:
    """A glyph as an L image trimmed to its ink and stretched ``height_scale`` times taller, for
    the tall narrow digits of flip cards and nixie tubes (crisp: whole rows repeat)."""
    m = Image.new("L", (font.width(ch) + 4, 40))
    font.draw(m, 2, 30, ch, 255)
    box = m.getbbox()
    if box is None:
        return Image.new("L", (1, 1))
    g = m.crop(box)
    return g.resize((g.width, g.height * height_scale), Image.NEAREST)


def ink_box(font, text: str):
    """(left, top, right, bottom) of ``text`` drawn at x 0 on baseline 30 (bottom exclusive)."""
    m = Image.new("L", (font.width(text) + 4, 40))
    font.draw(m, 0, 30, text, 255)
    return m.getbbox() or (0, 30, 0, 30)


def scaled(img: Image.Image, width: int, height: int, background=(0, 0, 0)) -> Image.Image:
    """Styles draw on 64 x 32; a different canvas gets the drawing centred (or cropped) on it, on the
    style's ``background`` so the edges match the middle."""
    if img.size == (width, height):
        return img
    out = Image.new("RGB", (width, height), background)
    out.paste(img, ((width - img.width) // 2, (height - img.height) // 2))
    return out
