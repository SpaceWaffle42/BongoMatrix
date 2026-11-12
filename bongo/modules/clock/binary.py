"""Binary: each digit of the time as a column of four dots (binary-coded decimal)."""
from __future__ import annotations

from PIL import Image, ImageDraw

from bongo.module import Style

from .common import HOURS, digits, scaled

W, H = 64, 32
COLOURS = [(255, 90, 90), (255, 90, 90), (90, 220, 120), (90, 220, 120), (90, 160, 255), (90, 160, 255)]


class Binary(Style):
    """Binary: columns of dots for each digit of the hours, minutes and seconds, with the digit
    underneath so it can be learned."""
    group = "Binary"
    fps = 4
    defaults = {"hours": "24", "show_digits": True}
    choices = {"hours": HOURS}
    basic = ("hours",)

    def render(self, now):
        img = Image.new("RGB", (W, H))
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
                            fill=COLOURS[i] if v >> b & 1 else tuple(c // 7 for c in COLOURS[i]))
            if self.settings["show_digits"]:
                small.draw(img, x + 1, 31, ch, (120, 120, 130))
        return scaled(img, self.ctx.width, self.ctx.height)
