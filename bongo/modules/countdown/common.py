"""Shared by both countdown styles: time maths, gradient, text halo, progress bar, done state."""
import datetime as dt
import math

import numpy as np
from PIL import Image, ImageFilter


def cap(font):
    """Cap height (the 'H') of a BDF font, measured locally so this module needs nothing new."""
    glyph = font.glyphs.get(ord("H"))
    return glyph[2] if glyph else font.ascent


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def parse(text, now):
    """ISO string or ``next:<holiday>`` -> datetime comparable with ``now`` (naive stays naive,
    aware adopts now's zone)."""
    if text.startswith("next:"):
        from bongo.holidays import resolve
        return resolve(text, now)[0]
    d = dt.datetime.fromisoformat(text)
    if now.tzinfo is None:
        return d.replace(tzinfo=None)
    return d.replace(tzinfo=now.tzinfo) if d.tzinfo is None else d


def remaining(s, now):
    """(whole seconds left, progress 0-1 between start and target, target reached?)."""
    target = parse(s["target"], now)
    start = parse(s["start"], now) if s["start"] else target - dt.timedelta(days=30)
    left = (target - now).total_seconds()
    span = (target - start).total_seconds()
    done = 1.0 if span <= 0 else min(1.0, max(0.0, (now - start).total_seconds() / span))
    return max(0, int(left)), done, left <= 0


class Base:
    """Mixin giving a style its background, text drawing and bar. Styles supply fonts and layout."""
    def setup(self):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        top, bottom = np.array(rgb(s["background_top"]), float), np.array(rgb(s["background_bottom"]), float)
        rows = np.linspace(0, 1, h)[:, None, None]
        grad = ((1 - rows) * top + rows * bottom).repeat(w, axis=1)
        self.background = Image.fromarray(grad.astype(np.uint8), "RGB")
        self.ink, self.accent, self.dark = rgb(s["text_colour"]), rgb(s["accent"]), rgb(s["background_bottom"])
        self.load_fonts()

    def line(self, font, text, y, ink, now, x=None):
        """One label centred, or scrolling if too wide. Returns draw items (font, x, y, text, ink)."""
        w, tw = self.ctx.width, font.width(text)
        if tw <= w - 2:
            return [(font, (w - tw) // 2 if x is None else x, y, text, ink)]
        gap = 12
        off = int(now.timestamp() * 14) % (tw + gap)
        return [(font, 1 - off, y, text, ink), (font, 1 - off + tw + gap, y, text, ink)]

    def paint(self, items, progress=None):
        img = self.background.copy()
        mask = Image.new("L", img.size)
        for font, x, y, text, _ in items:
            font.draw(mask, x, y, text, 255)
        mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1)).point(lambda v: int(v * .75))
        img = Image.composite(Image.new("RGB", img.size, self.dark), img, mask)
        for font, x, y, text, ink in items:
            font.draw(img, x, y, text, ink)
        if progress is not None and self.settings["progress_bar"]:
            w, y = self.ctx.width, self.ctx.height - 1
            filled = round(progress * w)
            dim = tuple(c // 5 for c in self.accent)
            for x in range(w):
                img.putpixel((x, y), self.accent if x < filled else dim)
        return img

    def done_frame(self, now):
        """Target passed: done_message, fitted to the widest font that fits, with a slow pulse."""
        msg = self.settings["done_message"]
        font = next((f for f in map(self.ctx.fonts, ("6x13B", "6x10", "5x7")) if f.width(msg) <= self.ctx.width - 2),
                    self.ctx.fonts("6x10"))
        k = 0.8 + 0.2 * math.sin(now.timestamp() * 2)
        ink = tuple(int(a + (b - a) * k) for a, b in zip(self.ink, self.accent))
        ink = tuple(int(c * (0.75 + 0.25 * k)) for c in ink)
        return self.paint(self.line(font, msg, (self.ctx.height + 10) // 2, ink, now), 1.0)


def fit_font(fonts, names, text, max_w, max_h):
    """First font in ``names`` (biggest first) whose ``text`` fits max_w wide and cap height max_h."""
    for name in names:
        f = fonts(name)
        if f.width(text) <= max_w and cap(f) <= max_h:
            return f
    return fonts(names[-1])


def mix(a, b, k):
    """Blend colour a towards b by k (0-1)."""
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))
