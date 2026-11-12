"""Analogue: a round dial with the day and date beside it, and a minimal wide face whose
seconds fill a ring of dots. Drawn at 4x and scaled down, for smooth hands."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw

from bongo.module import Style

from .common import scaled

W, H, S = 64, 32, 4


def _hand(d, cx, cy, frac, length, width, colour, sx=1.0):
    a = frac * 2 * math.pi
    d.line([cx, cy, cx + length * math.sin(a) * sx, cy - length * math.cos(a)], fill=colour, width=width)


class Dial(Style):
    """Dial: a round face with ticks and a sweeping second hand, the day and date beside it."""
    group = "Analogue"
    fps = 20
    defaults = {"second_hand": True}

    def render(self, now):
        img = Image.new("RGB", (W * S, H * S))
        d = ImageDraw.Draw(img)
        cx, cy, r = 64, 64, 54
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(70, 70, 80), width=4)
        for k in range(12):
            a = k / 12 * 2 * math.pi
            ln = 12 if k % 3 == 0 else 6
            d.line([cx + (r - 4) * math.sin(a), cy - (r - 4) * math.cos(a),
                    cx + (r - 4 - ln) * math.sin(a), cy - (r - 4 - ln) * math.cos(a)],
                   fill=(200, 200, 210), width=4 if k % 3 == 0 else 3)
        sec = now.second + now.microsecond / 1e6
        mins = now.minute + sec / 60
        hrs = now.hour % 12 + mins / 60
        _hand(d, cx, cy, hrs / 12, 30, 7, (240, 240, 240))
        _hand(d, cx, cy, mins / 60, 46, 5, (240, 240, 240))
        out = img.resize((W, H), Image.LANCZOS)
        if self.settings["second_hand"]:                    # drawn sharp at panel size, so it never smears
            a = now.second / 60 * 2 * math.pi                # it ticks, once a second
            od = ImageDraw.Draw(out)
            od.line([16, 16, round(16 + 12 * math.sin(a)), round(16 - 12 * math.cos(a))], fill=(255, 70, 50))
            od.rectangle([15, 15, 16, 16], fill=(255, 80, 60))
        f610 = self.ctx.fonts("6x10")                         # one font, rows 2-8, 12-18, 22-28
        f610.draw(out, 38, 9, now.strftime("%a").upper(), (255, 181, 71))
        f610.draw(out, 38, 19, now.strftime("%d"), (235, 235, 235))
        f610.draw(out, 38, 29, now.strftime("%b").upper(), (175, 175, 185))
        return scaled(out, self.ctx.width, self.ctx.height)


class Minimal(Style):
    """Minimal: hands on a wide face; the seconds fill a ring of dots."""
    group = "Analogue"
    fps = 10
    defaults = {}

    def render(self, now):
        img = Image.new("RGB", (W * S, H * S))
        d = ImageDraw.Draw(img)
        cx, cy, rx, ry = W * S // 2, 64, 110, 50
        dots = []                                            # the ring, set later on whole LEDs (no smeared dashes)
        for k in range(60):
            a = k / 60 * 2 * math.pi
            dots.append((round((cx + rx * math.sin(a)) / S - 0.5), round((cy - ry * math.cos(a)) / S - 0.5)))
        sec = now.second + now.microsecond / 1e6
        mins = now.minute + sec / 60
        hrs = now.hour % 12 + mins / 60
        _hand(d, cx, cy, hrs / 12, 26, 9, (255, 181, 71), rx / ry)
        _hand(d, cx, cy, mins / 60, 42, 5, (240, 240, 240), rx / ry)
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(255, 80, 60))
        out = img.resize((W, H), Image.LANCZOS)
        for k, (x, y) in enumerate(dots):
            if k <= now.second:
                out.putpixel((x, y), (255, 120, 60) if k == now.second else (200, 95, 50))
            elif k % 5 == 0:
                out.putpixel((x, y), (90, 90, 100))
        return scaled(out, self.ctx.width, self.ctx.height)
