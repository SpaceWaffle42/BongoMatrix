"""Sun style: today's arc from sunrise to sunset, the sun (or at night the moon) riding on it."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
from PIL import Image, ImageDraw

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

    def setup(self):
        self.tiny = self.ctx.fonts("tom-thumb")

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        now = local(now)
        lat, lon = self.settings["latitude"], self.settings["longitude"]
        elev = astro.sun_elevation(now, lat, lon)
        solar_day = (now.astimezone(dt.timezone.utc) + dt.timedelta(hours=lon / 15)).date()
        times = astro.sun_times(solar_day, lat, lon)
        top, bottom = sky_colours(elev)
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
        r = 2.6 if h >= 24 else 1.8
        keep = arr[horizon + 1:].copy()  # the sun sets behind the ground
        if daytime:
            add_blob(arr, px, py, r, SUN, glow=3.2, power=0.6)
            add_blob(arr, px, py, r * 0.55, (255, 250, 225))
        else:
            r += 0.3
            add_blob(arr, px, py, r, MOON, glow=3.0, power=0.4)
            cut = np.zeros_like(arr)  # carve a crescent: re-darken the bite with the sky behind it
            add_blob(cut, px + r * 0.6, py - r * 0.3, r * 0.85, (1, 1, 1))
            sky_here = np.asarray(img, float)
            arr = arr * (1 - cut) + sky_here * cut
        arr[horizon + 1:] = keep
        img = to_image(arr)
        self.labels(img, times, now, horizon, ry, elev, top)
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

    def labels(self, img, times, now, horizon, ry, elev, sky):
        w, h = img.size
        accent, text = rgb(self.settings["accent"]), rgb(self.settings["text_colour"])
        tz = now.tzinfo
        if times:
            rise, set_ = times
            length = int((set_ - rise).total_seconds() // 60)
            rs, ss = f"{rise.astimezone(tz):%H:%M}", f"{set_.astimezone(tz):%H:%M}"
            span = f"{length // 60}H {length % 60}M"
        else:
            rs = ss = "--:--"
            span = "24H" if elev > 0 else "0H"
        base = h - 1
        if 2 * self.tiny.width(rs) + 6 <= w:
            self.tiny.draw(img, 1, base, rs, text)
            self.tiny.draw(img, w - 1 - self.tiny.width(ss), base, ss, text)
            if ry >= 12:
                self.tiny.draw(img, (w - self.tiny.width(span)) // 2, horizon - 4, span, text, outline=tuple(int(c * 0.3) for c in sky))
        else:
            self.tiny.draw(img, (w - self.tiny.width(span)) // 2, base, span, accent)
