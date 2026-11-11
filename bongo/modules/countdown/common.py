"""Shared by the countdown styles: picking the event, gradient, text halo, progress bar, done state."""
import datetime as dt
import math

import numpy as np
from PIL import Image, ImageFilter

from bongo import icons


def cap(font):
    """Cap height (the 'H') of a BDF font, measured locally so this module needs nothing new."""
    glyph = font.glyphs.get(ord("H"))
    return glyph[2] if glyph else font.ascent


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Base:
    """Mixin giving a style its background, text drawing and bar. Styles supply fonts and layout."""
    def setup(self):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        top, bottom = np.array(rgb(s["background_top"]), float), np.array(rgb(s["background_bottom"]), float)
        rows = np.linspace(0, 1, h)[:, None, None]
        grad = ((1 - rows) * top + rows * bottom).repeat(w, axis=1)
        self.background = Image.fromarray(grad.astype(np.uint8), "RGB")
        self.ink, self.dark = rgb(s["text_colour"]), rgb(s["background_bottom"])
        self.accent = self.module_accent = rgb(s["accent"])
        self.load_fonts()

    def pick(self, now):
        """Choose the event to show now; sets title, target, message and accent for this frame.
        Returns (whole seconds left, progress 0-1, is it on now?), or None with nothing coming up."""
        from .events import current
        self.now_showing = on = current(self.settings["events"], now)
        if on is None:
            self.accent, self.icon = self.module_accent, None
            return None
        e = on.event
        self.title = e["title"].upper()
        self.target = on.target
        self.message = e.get("message") or self.settings["done_message"]
        self.accent = rgb(e["colour"]) if e.get("colour") else self.module_accent
        from .events import icon_of
        name = icon_of(e) if self.settings["icons"] else None
        self.icon = icons.icon(name) if name else None
        left = (on.target - now).total_seconds()
        span = (on.target - on.start).total_seconds()
        progress = 1.0 if span <= 0 else min(1.0, max(0.0, (now - on.start).total_seconds() / span))
        return max(0, int(left)), progress, left <= 0

    def idle_frame(self, now):
        """Nothing left to count down to: a quiet line of idle text (or a blank background)."""
        text = self.settings["idle_text"]
        if not text:
            return self.paint([])
        font = next((f for f in map(self.ctx.fonts, ("6x10", "5x7")) if f.width(text) <= self.ctx.width - 2),
                    self.ctx.fonts("5x7"))
        return self.paint(self.line(font, text, (self.ctx.height + 8) // 2, self.ink, now))

    def line(self, font, text, y, ink, now, x=None):
        """One label centred, or scrolling if too wide. Returns draw items (font, x, y, text, ink)."""
        w, tw = self.ctx.width, font.width(text)
        if tw <= w - 2:
            return [(font, (w - tw) // 2 if x is None else x, y, text, ink)]
        gap = 12
        off = int(now.timestamp() * 14) % (tw + gap)
        return [(font, 1 - off, y, text, ink), (font, 1 - off + tw + gap, y, text, ink)]

    def paint(self, items, progress=None, icon_at=None):
        img = self.background.copy()
        mask = Image.new("L", img.size)
        for font, x, y, text, _ in items:
            font.draw(mask, x, y, text, 255)
        mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1)).point(lambda v: int(v * .75))
        img = Image.composite(Image.new("RGB", img.size, self.dark), img, mask)
        for font, x, y, text, ink in items:
            font.draw(img, x, y, text, ink)
        if icon_at is not None and getattr(self, "icon", None) is not None:
            img.paste(self.icon, icon_at, self.icon)
        if progress is not None and self.settings["progress_bar"]:
            w, y = self.ctx.width, self.ctx.height - 1
            filled = round(progress * w)
            dim = tuple(c // 5 for c in self.accent)
            for x in range(w):
                img.putpixel((x, y), self.accent if x < filled else dim)
        return img

    def done_frame(self, now):
        """Target passed: done_message, fitted to the widest font that fits, with a slow pulse."""
        msg = self.message
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
