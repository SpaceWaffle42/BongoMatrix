"""Binary: each digit of the time as a column of four dots (binary-coded decimal)."""
from __future__ import annotations

from PIL import Image, ImageDraw

from bongo.module import Style

from .common import HOURS, digits


def layout(w: int, h: int, columns: int, labels: bool):
    """(dot size, gap inside a pair, gap between pairs, labels?) for the biggest dots that fit ``w`` x ``h``:
    5-px dots with the digits underneath on a full panel, smaller and closer inside a safe area, and
    the digits given up before the dots get too small to read."""
    pairs_gap = (columns - 1) // 2
    for show in ((True, False) if labels else (False,)):
        for size in (5, 4, 3, 2):
            for inside, between in ((4, 4), (3, 3), (2, 3), (1, 2), (1, 1)):
                wide = columns * size + (columns - 1) * inside + pairs_gap * between
                high = 4 * size + 3 + (6 if show else 0)     # three 1-px gaps; a 1-px gap and 5-px digits
                if wide <= w and high <= h and (size >= 3 or not show):
                    return size, inside, between, show
    return 2, 1, 1, False


class Binary(Style):
    """Binary: columns of dots for each digit of the hours, minutes and seconds, with the digit
    underneath so it can be learned."""
    group = "Binary"
    fps = 4
    defaults = {"hours": "24", "show_digits": True, "hours_colour": "#FF5A5A", "minutes_colour": "#5ADC78",
                "seconds_colour": "#5AA0FF", "digit_colour": "#787882", "background": "#000000"}
    choices = {"hours": HOURS}
    basic = ("hours",)

    def render(self, now):
        s_ = self.settings
        pairs = [s_["hours_colour"], s_["minutes_colour"], s_["seconds_colour"]]
        colours = [tuple(int(p.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for p in pairs for _ in (0, 1)]
        bg = tuple(int(s_["background"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        digit_colour = tuple(int(s_["digit_colour"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h), bg)
        d = ImageDraw.Draw(img)
        small = self.ctx.fonts("tom-thumb")
        s = digits(now, self.settings["hours"], seconds=True)
        skip = self.settings["hours"] == "12" and s[0] == "0"   # 12-hour: no leading zero column
        size, inside, between, labels = layout(w, h, len(s), self.settings["show_digits"])
        pitch = size + inside
        wide = len(s) * size + (len(s) - 1) * inside + (len(s) - 1) // 2 * between
        high = 4 * size + 3 + (6 if labels else 0)
        left = (w - wide) // 2 - ((pitch + 1) // 2 if skip else 0)   # ...and the rest moves over to stay centred
        top = (h - high + 1) // 2
        for i, ch in enumerate(s):
            if i == 0 and skip:
                continue
            v = int(ch)
            x = left + i * pitch + (i // 2) * between
            for b in range(4):
                y = top + (3 - b) * (size + 1)
                d.rectangle([x, y, x + size - 1, y + size - 1],
                            fill=colours[i] if v >> b & 1 else tuple(b_ + (c - b_) // 7 for c, b_ in zip(colours[i], bg)))
            if labels:
                small.draw(img, x + (size - 3) // 2, top + high, ch, digit_colour)
        return img
