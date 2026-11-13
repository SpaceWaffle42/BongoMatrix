"""Binary: each digit of the time as a column of four dots (binary-coded decimal)."""
from __future__ import annotations

from PIL import Image, ImageDraw

from bongo.module import Style

from .common import HOURS, digits, scaled

W, H = 64, 32


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
        img = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(img)
        small = self.ctx.fonts("tom-thumb")
        s = digits(now, self.settings["hours"], seconds=True)
        skip = self.settings["hours"] == "12" and s[0] == "0"   # 12-hour: no leading zero column
        shift = -5 if skip else 0                            # ...and the rest moves over to stay centred
        for i, ch in enumerate(s):
            if i == 0 and skip:
                continue
            v = int(ch)
            x = 3 + i * 9 + (i // 2) * 4 + shift
            for b in range(4):
                y = 20 - b * 6
                d.rectangle([x, y, x + 4, y + 4],
                            fill=colours[i] if v >> b & 1 else tuple(b_ + (c - b_) // 7 for c, b_ in zip(colours[i], bg)))
            if self.settings["show_digits"]:
                small.draw(img, x + 1, 31, ch, digit_colour)
        return scaled(img, self.ctx.width, self.ctx.height)
