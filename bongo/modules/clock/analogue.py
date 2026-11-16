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
    defaults = {"second_hand": True, "background": "#000000", "face_colour": "#464650", "tick_colour": "#C8C8D2",
                "hand_colour": "#F0F0F0", "second_colour": "#FF4632", "accent": "#FFB547", "text_colour": "#EBEBEB",
                "month_colour": "#AFAFB9"}

    def render(self, now):
        c = {k: _rgb(v) for k, v in self.settings.items() if isinstance(v, str) and v.startswith("#")}
        w, h = self.ctx.width, self.ctx.height
        size = min(H, h, w)                                  # the face is a square as tall as the safe area allows
        img = Image.new("RGB", (H * S, H * S), c["background"])
        d = ImageDraw.Draw(img)
        cx, cy, r = 64, 64, 54
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c["face_colour"], width=4)
        for k in range(12):
            a = k / 12 * 2 * math.pi
            ln = 12 if k % 3 == 0 else 6
            d.line([cx + (r - 4) * math.sin(a), cy - (r - 4) * math.cos(a),
                    cx + (r - 4 - ln) * math.sin(a), cy - (r - 4 - ln) * math.cos(a)],
                   fill=c["tick_colour"], width=4 if k % 3 == 0 else 3)
        sec = now.second + now.microsecond / 1e6
        mins = now.minute + sec / 60
        hrs = now.hour % 12 + mins / 60
        _hand(d, cx, cy, hrs / 12, 30, 7, c["hand_colour"])
        _hand(d, cx, cy, mins / 60, 46, 5, c["hand_colour"])
        out = Image.new("RGB", (w, h), c["background"])
        top = (h - size) // 2
        out.paste(img.resize((size, size), Image.LANCZOS), (0, top))
        if self.settings["second_hand"]:                    # drawn sharp at panel size, so it never smears
            a = now.second / 60 * 2 * math.pi                # it ticks, once a second
            od = ImageDraw.Draw(out)
            mid, reach = size // 2, round(12 * size / H)
            od.line([mid, top + mid, round(mid + reach * math.sin(a)), round(top + mid - reach * math.cos(a))],
                    fill=c["second_colour"])
            od.rectangle([mid - 1, top + mid - 1, mid, top + mid],
                         fill=tuple(min(255, v + 10) for v in c["second_colour"]))   # its pivot
        # the day, date and month beside it: one font (6x10: baselines 9, 19, 29 on a full panel), the
        # 3x5 one where that is too wide, and the month given up before the lines would touch
        texts = [(now.strftime("%a").upper(), c["accent"]), (now.strftime("%d"), c["text_colour"]),
                 (now.strftime("%b").upper(), c["month_colour"])]
        font = next((f for f in (self.ctx.fonts("6x10"), self.ctx.fonts("tom-thumb"))
                     if size + 1 + f.width("WED") <= w), None)
        if font is not None:
            texts = texts if h >= 3 * font.cap + 2 else texts[:2]
            pitch = min(10, (h - font.cap) // (len(texts) - 1))
            first = max(font.cap, (h - (len(texts) - 1) * pitch - font.cap - 1) // 2 + font.cap)
            x = size + min(6, w - size - font.width("WED"))
            for i, (text, colour) in enumerate(texts):
                font.draw(out, x, first + i * pitch, text, colour)
        return out


class Minimal(Style):
    """Minimal: hands on a wide face; the seconds fill a ring of dots."""
    group = "Analogue"
    fps = 10
    defaults = {"background": "#000000", "hour_hand_colour": "#FFB547", "minute_hand_colour": "#F0F0F0",
                "centre_colour": "#FF503C", "second_colour": "#FF783C", "seconds_gone_colour": "#C85F32",
                "dot_colour": "#5A5A64"}

    def render(self, now):
        c = {k: _rgb(v) for k, v in self.settings.items() if isinstance(v, str) and v.startswith("#")}
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w * S, h * S), c["background"])
        d = ImageDraw.Draw(img)
        # drawn to fit the whole canvas (a full panel: an ellipse 110 x 50 about 128, 64 at 4x), so a
        # safe area or a bigger panel keeps the ring and hands inside it and the background everywhere
        cx, cy = w * S // 2, h * S // 2
        rx, ry = round(w * S / 2 * 110 / 128), round(h * S / 2 * 50 / 64)
        k = min(w / W, h / H)                                # how much thicker (or thinner) than on a full panel
        thick = lambda px: max(S - 1, round(px * k))         # (never under a pixel)
        dots = []                                            # the ring, set later on whole LEDs (no smeared dashes)
        for n in range(60):
            a = n / 60 * 2 * math.pi
            dots.append((min(w - 1, max(0, round((cx + rx * math.sin(a)) / S - 0.5))),
                         min(h - 1, max(0, round((cy - ry * math.cos(a)) / S - 0.5)))))
        sec = now.second + now.microsecond / 1e6
        mins = now.minute + sec / 60
        hrs = now.hour % 12 + mins / 60
        _hand(d, cx, cy, hrs / 12, 26 * ry / 50, thick(9), c["hour_hand_colour"], rx / ry)
        _hand(d, cx, cy, mins / 60, 42 * ry / 50, thick(5), c["minute_hand_colour"], rx / ry)
        r = thick(5)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c["centre_colour"])
        out = img.resize((w, h), Image.LANCZOS)
        for n, (x, y) in enumerate(dots):
            if n <= now.second:
                out.putpixel((x, y), c["second_colour"] if n == now.second else c["seconds_gone_colour"])
            elif n % 5 == 0:
                out.putpixel((x, y), c["dot_colour"])
        return scaled(out, w, h, c["background"])


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
