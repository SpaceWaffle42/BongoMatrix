"""Moon style: lit disc with the right terminator, maria, earthshine, twinkling stars."""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageFilter

from bongo.module import Style

from . import astro
from .look import gradient, local, rgb, to_image

MARIA = ((-.30, -.25, .30, .45), (.12, -.38, .22, .40), (.30, .02, .24, .35), (-.12, .20, .30, .30),
         (-.45, .10, .14, .30), (.05, .55, .18, .22), (.40, .38, .12, .22))  # x, y, radius, darkness
SS = 4  # supersampling


def moon_image(size: int, fraction: float, waxing: bool, north: bool = True) -> Image.Image:
    """RGBA moon of ``size`` px: drawn at 4x and box-downsampled."""
    n = size * SS
    c = (np.arange(n) + 0.5) / n * 2 - 1
    x, y = np.meshgrid(c, c)
    r2 = x * x + y * y
    z = np.sqrt(np.clip(1 - r2, 0, 1))
    phase_angle = math.acos(2 * fraction - 1)  # 0 = full, pi = new
    side = 1 if waxing == north else -1  # northern waxing: lit on the right
    lit = np.clip((x * side * math.sin(phase_angle) + z * math.cos(phase_angle)) * 6 + 0.5, 0, 1)
    flip = 1 if north else -1  # southern sky shows the moon upside down
    maria = np.zeros_like(x)
    for mx, my, mr, mk in MARIA:
        maria += mk * np.exp(-((x - mx * flip) ** 2 + (y - my * flip) ** 2) / (mr * mr))
    texture = (1 - np.clip(maria, 0, 0.6))[..., None]
    limb = (0.78 + 0.22 * z)[..., None]
    bright = np.array((238, 233, 214), float) * texture * limb
    earth = np.array((54, 66, 100), float) * texture * (0.5 + 0.5 * z[..., None]) * (0.42 + 0.2 * (1 - fraction))
    rgb_ = earth * (1 - lit[..., None]) + bright * lit[..., None]
    alpha = np.clip((1 - np.sqrt(r2)) * n / 2 + 0.5, 0, 1)
    img = Image.fromarray(np.dstack([rgb_, alpha * 255]).clip(0, 255).astype("uint8"), "RGBA")
    return img.resize((size, size), Image.Resampling.BOX)


class Moon(Style):
    fps = 10

    def setup(self):
        self.tiny, self.small = self.ctx.fonts("tom-thumb"), self.ctx.fonts("5x7")
        area = self.ctx.width * self.ctx.height
        self.stars = [(self.ctx.rng.randrange(self.ctx.width), self.ctx.rng.randrange(self.ctx.height),
                       self.ctx.rng.random() * 6.28, self.ctx.rng.uniform(5, 12), self.ctx.rng.uniform(0.5, 1))
                      for _ in range(max(4, area // 45))]

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        now = local(now)
        age, frac, waxing = astro.moon_phase(now)
        accent, text = rgb(self.settings["accent"]), rgb(self.settings["text_colour"])
        arr = gradient(w, h, (12, 20, 56), (3, 5, 16))
        t = now.timestamp()
        for sx, sy, ph, period, power in self.stars:
            b = 0.55 + 0.45 * math.sin(6.283 * t / period + ph)
            arr[sy, sx] += np.array((150, 165, 220)) * b * power * 0.5
        footer = h >= 20
        d = max(7, min(h - (9 if footer else 4), int(w * 0.46)))
        mx, my = 2 + (1 if w > 40 else 0), (h - (6 if footer else 0) - d) // 2
        moon = moon_image(d, frac, waxing, self.settings["latitude"] >= 0)
        glow = Image.new("L", (w, h))
        glow.paste(moon.getchannel("A").point(lambda v: int(v * (0.15 + 0.5 * frac))), (mx, my))
        halo = np.asarray(glow.filter(ImageFilter.GaussianBlur(2.2)), float)[..., None] / 255
        arr += halo * np.array((120, 140, 200))
        img = to_image(arr)
        img.paste(moon, (mx, my), moon)
        self.labels(img, mx + d + 3, my, d, now, frac, waxing, accent, text, footer)
        return img

    def labels(self, img, x0, top, d, now, frac, waxing, accent, text, footer):
        w, h = self.ctx.width, self.ctx.height
        room = w - x0 - 1
        words = astro.phase_name(frac, waxing).upper().split()
        font = next((f for f in (self.small, self.tiny) if all(f.width(s) <= room for s in words)), None)
        pct = f"{round(frac * 100)}%"
        lines = ([(s, text) for s in words] if font else []) + [(pct, accent)]
        font = font or self.tiny
        pitch = font.cap + 3
        area_h = h - (7 if footer else 0)
        y = (area_h - (len(lines) * pitch - 3)) // 2 + font.cap
        if len(lines) * pitch - 3 > area_h:  # tiny canvases: just the percentage
            lines, y = lines[-1:], (area_h - font.cap) // 2 + font.cap
        for i, (s, colour) in enumerate(lines):
            f = self.tiny if i == len(lines) - 1 and len(lines) > 1 else font
            f.draw(img, x0, y + i * pitch, s, colour)
        if footer:
            phases = astro.next_phases(now)
            name, when = min(((n, phases[n]) for n in ("full", "new")), key=lambda kv: kv[1])
            day = when.astimezone(now.tzinfo)
            s = f"{name.upper()} {day.day} {day:%b}".upper()
            if self.tiny.width(s) > w - 2:
                s = f"{name.upper()} {day.day}"
            self.tiny.draw(img, max(1, (w - self.tiny.width(s)) // 2), h - 1, s, accent)
