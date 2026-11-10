"""Metro: the refreshed look. Wax rises and sinks like a real lamp, shaded with a soft
halo, anti-aliased edges and a hot core, under clean type with a soft dark halo."""
import math

import numpy as np
from PIL import Image, ImageFilter

from bongo.fonts import align_x, middle
from bongo.module import Style

from .field import field
from .palettes import METRO, rgb

SAMPLES = 2  # supersampling per axis for smooth edges


def _smooth(a, lo, hi):
    t = np.clip((a - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


class Metro(Style):
    fps = 30
    choices = {"palette": list(METRO)}
    defaults = dict(
        palette="magma",          # name from palettes.METRO
        blob_count=4, speed=1.0,  # speed scales how fast the wax drifts
        glow=0.6,                 # halo strength around the wax, 0-1
        text_halo=0.75,           # how much the lava darkens around the text, 0-1
        time_font="10x20", date_font="5x7", time_y=None, date_y=None,  # None: fit the canvas
        date_format="%a %d %b", uppercase_date=True,
    )

    def setup(self):
        s, rng, w, h = self.settings, self.ctx.rng, self.ctx.width, self.ctx.height
        if s["palette"] not in METRO:
            raise ValueError(f"lava_lamp/metro: no palette {s['palette']!r} (have {sorted(METRO)})")
        p = {k: np.array(rgb(v), float) for k, v in METRO[s["palette"]].items()}
        self.p = p
        # Each blob: lane, rise/sink period, phase, sway, size, breathing.
        self.blobs = [dict(
            x=w * (i + rng.uniform(0.2, 0.8)) / s["blob_count"],
            period=rng.uniform(18, 34), phase=rng.uniform(0, 2 * math.pi),
            sway=rng.uniform(2, 6), sway_period=rng.uniform(7, 13),
            r=rng.uniform(4.5, 6.5), breathe=rng.uniform(5, 9),
        ) for i in range(s["blob_count"])]
        n = SAMPLES
        self.ys, self.xs = (np.mgrid[0:h * n, 0:w * n] + 0.5) / n - 0.5
        rows = np.linspace(0, 1, h * n)[:, None, None]
        self.background = (1 - rows) * p["bg_top"] + rows * p["bg_bottom"]
        self.time_font, self.date_font = self.ctx.fonts(s["time_font"]), self.ctx.fonts(s["date_font"])

    def _positions(self, t):
        h = self.ctx.height
        for b in self.blobs:
            rise = math.sin(2 * math.pi * t / b["period"] + b["phase"])
            yield (b["x"] + b["sway"] * math.sin(2 * math.pi * t / b["sway_period"] + b["phase"]),
                   h / 2 - rise * (h / 2 + 2),
                   b["r"] * (1 + 0.15 * math.sin(2 * math.pi * t / b["breathe"])))

    def render(self, now):
        s, p = self.settings, self.p
        g = field(self.xs, self.ys, self._positions(now.timestamp() * s["speed"]))[..., None]
        halo = s["glow"] * _smooth(g, 0.12, 0.95)
        colour = self.background * (1 - halo) + p["glow"] * halo
        heat = np.clip((g - 1) / 1.6, 0, 1)
        wax = np.where(heat < 0.45, p["edge"] + (p["mid"] - p["edge"]) * heat / 0.45,
                       p["mid"] + (p["core"] - p["mid"]) * (heat - 0.45) / 0.55)
        body = _smooth(g, 0.88, 1.08)
        colour = colour * (1 - body) + wax * body
        n = SAMPLES
        h, w = self.ctx.height, self.ctx.width
        pixels = colour.reshape(h, n, w, n, 3).mean(axis=(1, 3))
        img = Image.fromarray(pixels.clip(0, 255).astype(np.uint8), "RGB")

        show_date = s["show_date"] and h >= 26  # too short for both lines: the time wins
        date_y = s["date_y"] if s["date_y"] is not None else h - 3
        if s["time_y"] is not None:
            time_y = s["time_y"]
        else:
            time_y = h - 14 if show_date else middle(self.time_font, h)
        labels = []
        if s["show_time"]:
            labels.append((self.time_font, now.strftime(s["time_format"]), s["time_align"], time_y, p["text"]))
        if show_date:
            date = now.strftime(s["date_format"])
            labels.append((self.date_font, date.upper() if s["uppercase_date"] else date,
                           s["date_align"], date_y, p["accent"]))
        placed = [(font, label, align_x(align, font.width(label), w), y, ink)
                  for font, label, align, y, ink in labels]
        if s["text_halo"] and placed:
            # Soft dark halo hugging the glyphs keeps text readable over bright wax.
            mask = Image.new("L", img.size)
            for font, label, x, y, _ in placed:
                font.draw(mask, x, y, label, 255)
            mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1))
            mask = mask.point(lambda v: int(v * s["text_halo"]))
            dark = Image.new("RGB", img.size, tuple(int(c) for c in p["bg_bottom"]))
            img = Image.composite(dark, img, mask)
        for font, label, x, y, ink in placed:
            font.draw(img, x, y, label, tuple(int(c) for c in ink))
        return img
