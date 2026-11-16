"""Shared by the countdown styles: picking the event, gradient, text halo, progress bar, done state."""
import datetime as dt
import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageFilter

from bongo import icons
from bongo.holidays import label

from bongo.scroll import HOLD, Scrolls


def cap(font):
    """Cap height (the 'H') of a BDF font, measured locally so this module needs nothing new."""
    glyph = font.glyphs.get(ord("H"))
    return glyph[2] if glyph else font.ascent


@lru_cache(maxsize=4096)
def ink(font, text) -> tuple[int, int]:
    """(first, last + 1) lit column of ``text`` drawn at x = 0: a font's width counts the blank column
    after each glyph, which is a pixel too many when fitting digits to a small card."""
    m = Image.new("L", (font.width(text) + 4, 40))
    font.draw(m, 2, 30, text, 255)
    box = m.getbbox()
    return (box[0] - 2, box[2] - 2) if box else (0, 0)


@lru_cache(maxsize=64)
def colon_drop(font) -> int:
    """Rows a font's colon hangs below its digits (one, in the 6x10 and 7x13B), to lift it by."""
    lit = [Image.new("L", (font.width(t) + 2, 40)) for t in ("0", ":")]
    for m, t in zip(lit, ("0", ":")):
        font.draw(m, 0, 30, t, 255)
    return max(0, (lit[1].getbbox() or (0, 0, 0, 0))[3] - (lit[0].getbbox() or (0, 0, 0, 0))[3])


def time_left(left: int) -> str:
    """Under a day HH:MM, and under ten minutes M:SS ("9:59", "0:30"): the last minute used to sit at
    "00:00". One digit before the colon can't be taken for HH:MM's two ("12:04" could be either)."""
    if left >= 600:
        return f"{left // 3600:02d}:{left % 3600 // 60:02d}"
    return f"{left // 60}:{left % 60:02d}"


def seconds(a, b) -> float:
    """Seconds from ``a`` to ``b``, by the real clock: two times in the same zone subtract by the wall
    clock in Python, which put a countdown an hour out on the days the clocks change."""
    if a.tzinfo is not None and b.tzinfo is not None:
        a, b = a.astimezone(dt.timezone.utc), b.astimezone(dt.timezone.utc)
    return (b - a).total_seconds()


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Base(Scrolls):
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

    def counted(self) -> list[dict]:
        """Your dates; with none, the holidays the Calendar shows."""
        return self.settings["events"] or [{"title": label(h), "holiday": h} for h in self.settings.get("holidays", [])]

    def pick(self, now):
        """Choose the event to show now; sets title, target, message and accent for this frame.
        Returns (whole seconds left, progress 0-1, is it on now?), or None with nothing coming up."""
        from .events import current
        self.now_showing = on = current(self.counted(), now)
        if on is None:
            self.accent, self.icon = self.module_accent, None
            return None
        e = on.event
        self.title = e["title"].upper()
        self.target = on.target
        self.message = e.get("message") or self.settings["done_message"]
        look = self.settings.get("_colours")                  # colour coded (Settings > Calendars)
        ticked = e.get("holiday") and not self.settings["events"]   # a ticked holiday, not your date on one
        plain = rgb(look["holidays"] if ticked else look["own"]) if look else self.module_accent
        self.accent = rgb(e["colour"]) if e.get("colour") else plain
        from .events import icon_of
        name = icon_of(e) if self.settings["icons"] else None
        self.icon = icons.icon(name) if name else None
        left = seconds(now, on.target)
        span = seconds(on.start, on.target)
        progress = 1.0 if span <= 0 else min(1.0, max(0.0, seconds(on.start, now) / span))
        return max(0, int(left)), progress, left <= 0

    def days_left(self, now) -> int:
        """Days to go as a calendar counts them (sleeps): on 14 November, Christmas is 41 days off,
        whatever the time of day. The Calendar's own pages say the same."""
        target = self.target if now.tzinfo is None or self.target.tzinfo is None else self.target.astimezone(now.tzinfo)
        return max(1, (target.date() - now.date()).days)

    def idle_frame(self, now):
        """Nothing left to count down to: a quiet line of idle text (or a blank background)."""
        text = self.settings["idle_text"]
        if not text:
            return self.paint([])
        font = next((f for f in map(self.ctx.fonts, ("6x10", "5x7")) if f.width(text) <= self.ctx.width - 2),
                    self.ctx.fonts("5x7"))
        return self.paint(self.label_items(font, text, (self.ctx.height + 8) // 2, self.ink, now))

    def label_items(self, font, text, y, ink, now, x=None):
        """One label centred, or scrolling if too wide: held at its start, then round and round at 14 px/s.
        It starts from the beginning when the screen comes on, and the screen stays up until it has gone
        round once (``Scrolls``). Returns draw items (font, x, y, text, ink)."""
        w, tw = self.ctx.width, font.width(text)
        if tw <= w - 2:
            return [(font, (w - tw) // 2 if x is None else x, y, text, ink)]
        gap = 12
        need = HOLD + (tw + gap) / 14 * max(1, int(self.settings.get("times_to_scroll", 1)))
        t = self.scrolled(("line", y, text), now, need) % need
        off = int(max(0.0, t - HOLD) * 14) % (tw + gap)
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
            filled = min(round(progress * w), w - 1)   # never full while it's still counting, however narrow the panel
            dim = tuple(c // 5 for c in self.accent)
            for x in range(w):
                img.putpixel((x, y), self.accent if x < filled else dim)
        return img

    def done_frame(self, now):
        """Target passed: what has arrived, named on a small line, and the message under it in the
        biggest font that fits, with a slow pulse. In capitals, like the rest of the screen (the
        lower-case letters of these fonts run together)."""
        msg, w, h = self.message.upper(), self.ctx.width, self.ctx.height
        # (not the bold 6x13: its capitals touch each other, and the halo joins them up)
        font = next((f for f in map(self.ctx.fonts, ("6x10", "5x7", "tom-thumb")) if f.width(msg) <= w - 2),
                    self.ctx.fonts("5x7"))   # too long for any: the 5x7 scrolls
        k = 0.8 + 0.2 * math.sin(now.timestamp() * 2)
        ink = tuple(int(a + (b - a) * k) for a, b in zip(self.ink, self.accent))
        ink = tuple(int(c * (0.75 + 0.25 * k)) for c in ink)
        small = self.ctx.fonts("5x7")
        if h >= 24 and self.title and self.title != msg:      # room for both lines, a clear row or more between
            name = mix(self.dark, self.accent, 0.65)          # the name quieter than the news under it
            return self.paint(self.label_items(small, self.title, 9, name, now)
                              + self.label_items(font, msg, 9 + (h - 9 + cap(font)) // 2, ink, now), 1.0)
        return self.paint(self.label_items(font, msg, (h + cap(font) + 1) // 2, ink, now), 1.0)


def time_items(font, x, y, text, ink_colour):
    """Draw items for ``text`` with any colon lifted level with the digits (``colon_drop``)."""
    items = []
    for part in text.partition(":"):
        if part:
            items.append((font, x, y - (colon_drop(font) if part == ":" else 0), part, ink_colour))
            x += font.width(part)
    return items


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
