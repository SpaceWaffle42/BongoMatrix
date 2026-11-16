"""Sun style: today's arc from sunrise to sunset, the sun (or at night the moon) riding on it."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
from PIL import Image, ImageDraw

from bongo.fonts import names
from bongo.module import Style

from . import astro
from .look import add_blob, gradient, local, rgb, to_image

#            elevation  top colour        bottom colour
SKY = ((-18, (4, 7, 24), (2, 3, 12)), (-12, (10, 12, 40), (20, 16, 50)), (-6, (38, 28, 86), (150, 70, 80)),
       (0, (60, 60, 140), (240, 130, 60)), (8, (45, 100, 190), (235, 170, 120)), (25, (30, 110, 210), (120, 185, 240)))
SUN, MOON = (255, 214, 90), (232, 230, 216)


def sky_colours(elev: float):
    xs = [s[0] for s in SKY]
    return [tuple(np.interp(elev, xs, [s[k][c] for s in SKY]) for c in range(3)) for k in (1, 2)]


class Sun(Style):
    fps = 5
    # The day length in the middle ("9H 5M"): 6x10 is two sizes up from the tiny font, which was
    # hard to read on a real panel. Too wide for the canvas falls back to the tiny font.
    # the sky follows the sun by default (dawn, day, dusk, night); turn that off for a fixed one
    defaults = dict(day_length_font="6x10", sky_follows_sun=True, background_top="#2A6FB0", background_bottom="#9CC8E8")
    choices = {"day_length_font": names()}

    def setup(self):
        self.tiny = self.ctx.fonts("tom-thumb")
        self.span_font = self.ctx.fonts(self.settings["day_length_font"])

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        now = local(now)
        lat, lon = self.settings["latitude"], self.settings["longitude"]
        elev = astro.sun_elevation(now, lat, lon)
        solar_day = (now.astimezone(dt.timezone.utc) + dt.timedelta(hours=lon / 15)).date()
        times = astro.sun_times(solar_day, lat, lon)
        top, bottom = sky_colours(elev) if self.settings["sky_follows_sun"] else \
            (rgb(self.settings["background_top"]), rgb(self.settings["background_bottom"]))
        horizon = h - 8 if h >= 20 else h - 7
        arr = gradient(w, h, top, bottom)
        arr[horizon:] = np.array(bottom) * 0.28  # ground: dark, so the times stay readable
        cx, x0 = w / 2, 3
        rx = (w - 1) / 2 - x0
        ry = max(3.0, min(horizon - 4.0, rx))
        p, daytime = self.progress(now, times, solar_day, lat, lon, elev)
        img = to_image(arr)
        arr = self.arc(img, cx, horizon, rx, ry, p, daytime)
        px, py = cx - rx * math.cos(math.pi * p), horizon - ry * math.sin(math.pi * p)
        full = (2.6 if h >= 24 else 1.8) + (0 if daytime else 0.3)
        span = self.span_text(times, now, elev)
        where = self.span_place(span, cx, horizon, rx, ry, px, py, full)    # (font, words, x, the disc's radius)
        r = where[3] if where else full
        k = r / full                                         # a disc shrunk to keep clear of the words: its glow too
        keep = arr[horizon + 1:].copy()  # the sun sets behind the ground
        if daytime:
            add_blob(arr, px, py, r, SUN, glow=3.2 * k, power=0.6)
            add_blob(arr, px, py, r * 0.55, (255, 250, 225))
        else:
            add_blob(arr, px, py, r, MOON, glow=3.0 * k, power=0.4)
            cut = np.zeros_like(arr)  # carve a crescent: re-darken the bite with the sky behind it
            add_blob(cut, px + r * 0.6, py - r * 0.3, r * 0.85, (1, 1, 1))
            sky_here = np.asarray(img, float)
            arr = arr * (1 - cut) + sky_here * cut
        arr[horizon + 1:] = keep
        img = to_image(arr)
        self.labels(img, times, now, horizon, ry, elev, top, where)
        return img

    def progress(self, now, times, day, lat, lon, elev):
        """(0..1 position along the arc, is_day). Night maps sunset -> next sunrise onto the same arc."""
        if times is None:
            return 0.5, elev > 0
        rise, set_ = times
        if rise <= now <= set_:
            return (now - rise) / (set_ - rise), True
        one = dt.timedelta(days=1)
        if now < rise:
            prev = astro.sun_times(day - one, lat, lon)
            start, end = (prev[1] if prev else set_ - one), rise
        else:
            nxt = astro.sun_times(day + one, lat, lon)
            start, end = set_, (nxt[0] if nxt else rise + one)
        return min(1.0, max(0.0, (now - start) / (end - start))), False

    def arc(self, img, cx, horizon, rx, ry, p, daytime):
        w, h = img.size
        k = 4
        pts = [((cx - rx * math.cos(math.pi * i / 80)) * k, (horizon - ry * math.sin(math.pi * i / 80)) * k)
               for i in range(81)]
        done = int(p * 80)
        arr = np.asarray(img, float)
        for sel, alpha in ((pts, 0.30 if daytime else 0.16), (pts[:done + 1], 0.6 if daytime else 0.25)):
            mask = Image.new("L", (w * k, h * k))
            if len(sel) > 1:
                ImageDraw.Draw(mask).line(sel, fill=255, width=k)
            m = np.asarray(mask.resize((w, h), Image.Resampling.BOX), float)[..., None] / 255 * alpha
            arr = arr * (1 - m) + np.array((255, 244, 222)) * m
        line = np.zeros((h, w, 1))
        line[int(horizon), 1:w - 1] = 0.3 if daytime else 0.15
        return arr * (1 - line) + np.array((255, 244, 222)) * line

    @staticmethod
    def span_text(times, now, elev):
        """The day length ("9H 5M"; 24H or 0H when the sun doesn't rise or set)."""
        if not times:
            return "24H" if elev > 0 else "0H"
        length = int((times[1] - times[0]).total_seconds() // 60)
        return f"{length // 60}H {length % 60}M"

    def labels(self, img, times, now, horizon, ry, elev, sky, where=None):
        w, h = img.size
        accent, text = rgb(self.settings["accent"]), rgb(self.settings["text_colour"])
        tz = now.tzinfo
        rs, ss = (f"{times[0].astimezone(tz):%H:%M}", f"{times[1].astimezone(tz):%H:%M}") if times else ("--:--", "--:--")
        span = self.span_text(times, now, elev)
        base = h - 1
        if self.span_in_sky(ry):
            self.tiny.draw(img, 1, base, rs, text)
            self.tiny.draw(img, w - 1 - self.tiny.width(ss), base, ss, text)
            font, words, x, _ = where
            font.draw(img, x, horizon - 4, words, text, outline=tuple(int(c * 0.3) for c in sky))
        else:
            if 2 * self.tiny.width(rs) + 6 <= w:
                self.tiny.draw(img, 1, base, rs, text)
                self.tiny.draw(img, w - 1 - self.tiny.width(ss), base, ss, text)
            else:
                self.tiny.draw(img, (w - self.tiny.width(span)) // 2, base, span, accent)

    def span_in_sky(self, ry):
        """Whether the day length goes in the sky under the arc (there's room for the times below, and the arc is tall)."""
        return 2 * self.tiny.width("00:00") + 6 <= self.ctx.width and ry >= 12

    SMALLEST = 1.2   # px: the least a disc shrinks to while it passes the words

    def span_place(self, span, cx, horizon, rx, ry, px, py, full):
        """(font, words, x, disc radius) for the day length in the sky, or None if it isn't shown there.
        The words are the same all day on a given screen: in the chosen font if, wherever the sun or moon is
        on its arc, a small disc can be kept clear of them; else the same without the space (16H38M); only
        then in the tiny font. Each frame they go centred, or aside (to the left or right edge) when the disc
        passes their row, and only then does the disc shrink."""
        if not self.span_in_sky(ry):
            return None
        w, baseline = self.ctx.width, horizon - 4
        chosen = self.__dict__.setdefault("_span_look", {})
        if span not in chosen:
            on_arc = [(cx - rx * math.cos(math.pi * i / 200), horizon - ry * math.sin(math.pi * i / 200)) for i in range(201)]
            clear = lambda f, t: f.width(t) <= w - 8 and all(self.spot(f, t, x, y, self.SMALLEST, baseline) is not None
                                                            for x, y in on_arc)
            looks = ((self.span_font, span), (self.span_font, span.replace(" ", "")))
            chosen[span] = next((look for look in looks if clear(*look)), (self.tiny, span))
        font, words = chosen[span]
        r = full
        while True:
            x = self.spot(font, words, px, py, r, baseline)
            if x is not None or r <= self.SMALLEST:
                return font, words, ((w - font.width(words)) // 2 if x is None else x), r
            r = max(self.SMALLEST, r - 0.2)

    def spot(self, font, text, px, py, r, baseline):
        """Where ``text`` can go so the disc of radius ``r`` (the pixels it lights, not its glow) isn't
        behind it or its outline: centred, else at the edge away from the disc, else the other edge;
        None if nowhere. Fixed spots, so the words don't creep along as the disc moves."""
        w, tw = self.ctx.width, font.width(text)
        first, last = math.floor(py - r), math.ceil(py + r) - 1      # the disc's lit rows (and columns, below)
        if last < baseline - font.cap - 1 or first > baseline:       # above or below the words' outline
            return (w - tw) // 2
        left, right = math.floor(px - r), math.ceil(px + r) - 1
        spots = ((w - tw) // 2, w - 1 - tw, 1) if px < w / 2 else ((w - tw) // 2, 1, w - 1 - tw)
        return next((x for x in spots if right < x - 1 or left > x + tw), None)
