"""Digital: big numbers, a split-flap flip clock, and red seven-segment LEDs."""
from __future__ import annotations

import datetime as dt

import numpy as np
from PIL import Image, ImageDraw

from bongo.module import Style

from .common import HOURS, colon_on, digits, glow, hm, ink_box, scaled, tall

W, H = 64, 32


class Big(Style):
    """Big numbers: large hours and minutes with a blinking colon, the date underneath."""
    group = "Digital"
    fps = 4
    defaults = {"hours": "24", "colour": "#FFB547", "show_date": True, "date_matches_colour": True,
                "date_colour": "#BF8735", "background": "#000000", "font": "10x20"}
    choices = {"hours": HOURS, "font": ["10x20", "9x18B"]}
    basic = ("hours", "colour")

    def render(self, now):
        img = Image.new("RGB", (W, H), _rgb(self.settings["background"]))
        d = ImageDraw.Draw(img)
        big, small = self.ctx.fonts(self.settings["font"]), self.ctx.fonts("5x7")
        colour = _rgb(self.settings["colour"])
        hours, minutes = hm(now, self.settings["hours"]).split(":")
        colon_w = 6                                          # a 2 px colon with 2 px each side
        wh, wm = big.width(hours), big.width(minutes)
        box, end = ink_box(big, hours), ink_box(big, minutes)   # centre the ink, not the letter boxes
        ink_w = wh - box[0] + colon_w + end[2]
        x = (W - ink_w) // 2 - box[0]
        _, top, _, bottom = ink_box(big, "0123456789")
        height = bottom - top
        y_top = 4 if self.settings["show_date"] else (H - height) // 2
        baseline = y_top + (30 - top)
        big.draw(img, x, baseline, hours, colour)
        big.draw(img, x + wh + colon_w, baseline, minutes, colour)
        if colon_on(now):
            cx = x + wh + colon_w // 2 - 1
            mid = y_top + height // 2
            for yy in (mid - 3, mid + 2):                    # two dots centred on the digits
                d.rectangle([cx, yy, cx + 1, yy + 1], fill=colour)
        if self.settings["show_date"]:
            date = now.strftime("%a %d %b").upper()
            dated = tuple(c * 3 // 4 for c in colour) if self.settings["date_matches_colour"] else _rgb(self.settings["date_colour"])
            small.draw(img, (W - small.width(date)) // 2, 29, date, dated)   # a dimmer shade of the time, or your own
        return scaled(img, self.ctx.width, self.ctx.height)


class Flip(Style):
    """Flip clock: split-flap cards; when the minute changes the old top half falls and the new
    bottom half lands."""
    group = "Digital"
    fps = 20
    defaults = {"hours": "24"}
    choices = {"hours": HOURS}
    basic = ("hours",)
    CARD, HALF, GAP, MID, FLIP = 13, 13, 2, 6, 0.6   # card width, half height, gaps, seconds a flip takes
    FACE, BG, INK = (30, 30, 36), (6, 6, 8), (238, 236, 226)

    def setup(self):
        self.cards = {}

    def card(self, ch):
        """A card: top half, a 1 px hinge, bottom half (27 rows); the digit split across the hinge."""
        if ch not in self.cards:
            face = Image.new("RGB", (self.CARD, 2 * self.HALF), self.FACE)
            if ch != " ":
                g = tall(self.ctx.fonts("9x18B"), ch)
                face.paste(self.INK, ((self.CARD - g.width) // 2, (2 * self.HALF - g.height) // 2), g)
            for x in (0, self.CARD - 1):                     # rounded corners
                for y in (0, 2 * self.HALF - 1):
                    face.putpixel((x, y), self.BG)
            self.cards[ch] = (face.crop((0, 0, self.CARD, self.HALF)), face.crop((0, self.HALF, self.CARD, 2 * self.HALF)))
        return self.cards[ch]

    def shown(self, now):
        s = digits(now, self.settings["hours"])
        return (" " + s[1:]) if self.settings["hours"] == "12" and s[0] == "0" else s   # 12-hour: a blank first card

    def render(self, now):
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        new = self.shown(now)
        old = self.shown(now - dt.timedelta(minutes=1))
        into = now.second + now.microsecond / 1e6
        width = 4 * self.CARD + 2 * self.GAP + self.MID
        x0 = (W - width) // 2
        y = (H - (2 * self.HALF + 1)) // 2
        for i in range(4):
            x = x0 + i * (self.CARD + self.GAP) + (self.MID - self.GAP if i >= 2 else 0)
            (nt, nb), (ot, ob) = self.card(new[i]), self.card(old[i])
            p = into / self.FLIP if now.second == 0 and into < self.FLIP and new[i] != old[i] else 1.0
            top, bottom = y, y + self.HALF + 1
            if p >= 1:
                img.paste(nt, (x, top))
                img.paste(nb, (x, bottom))
                continue
            img.paste(nt, (x, top))                          # the new top waits behind the falling flap
            img.paste(ob, (x, bottom))                       # the old bottom shows until it's covered
            if p < 0.5:                                      # the old top folds down towards the hinge
                h = max(1, round(self.HALF * (1 - p * 2)))
                flap = ot.resize((self.CARD, h)).point(lambda v: int(v * (0.8 + 0.2 * (1 - p * 2))))
                img.paste(flap, (x, top + self.HALF - h))
            else:                                            # the new bottom swings down into place
                h = max(1, round(self.HALF * (p - 0.5) * 2))
                flap = nb.resize((self.CARD, h)).point(lambda v: int(v * (0.8 + 0.2 * (p - 0.5) * 2)))
                img.paste(flap, (x, bottom))
        cx = x0 + 2 * self.CARD + self.GAP + (self.MID - 2) // 2
        mid = y + self.HALF
        for yy in (mid - 4, mid + 3):                        # the colon, centred on the cards
            d.rectangle([cx, yy, cx + 1, yy + 1], fill=(200, 198, 188))
        return scaled(img, self.ctx.width, self.ctx.height)


SEGMENTS = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
            "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg"}


class Seven(Style):
    """Seven-segment: red LED digits with the unlit segments faintly showing, small seconds."""
    group = "Digital"
    fps = 4
    defaults = {"hours": "24", "colour": "#FF281E", "background": "#000000"}
    choices = {"hours": HOURS}
    basic = ("hours", "colour")
    DW, DH, GAP, COLON, SEC_GAP = 8, 22, 2, 6, 3     # big digit size, gaps, colon zone, space before seconds
    SW, SH = 6, 12                                  # seconds digit size

    def render(self, now):
        img = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(img)
        on = _rgb(self.settings["colour"])
        off = tuple(max(3, c // 12) for c in on)

        def digit(x, y, ch, w, h, th):
            segs = {"a": (x + 1, y, x + w - 2, y + th - 1), "g": (x + 1, y + h // 2 - 1, x + w - 2, y + h // 2 + th - 2),
                    "d": (x + 1, y + h - th, x + w - 2, y + h - 1), "f": (x, y + 1, x + th - 1, y + h // 2 - 1),
                    "b": (x + w - th, y + 1, x + w - 1, y + h // 2 - 1), "e": (x, y + h // 2 + 1, x + th - 1, y + h - 2),
                    "c": (x + w - th, y + h // 2 + 1, x + w - 1, y + h - 2)}
            for seg, box in segs.items():
                d.rectangle(box, fill=on if ch != " " and seg in SEGMENTS[ch] else off)

        s = digits(now, self.settings["hours"], seconds=True)
        hours = s[1] if self.settings["hours"] == "12" and s[0] == "0" else s[:2]   # 12-hour: no leading zero
        big = [*hours, None, s[2], s[3]]
        width = sum(self.DW if c else self.COLON for c in big) + self.GAP * (len(big) - 3) + self.SEC_GAP + 2 * self.SW + 2
        x = (W - width) // 2
        y = (H - self.DH) // 2
        for k, ch in enumerate(big):
            if ch is None:
                c = on if colon_on(now) else off
                cx = x + (self.COLON - 2) // 2
                d.rectangle([cx, y + 6, cx + 1, y + 7], fill=c)
                d.rectangle([cx, y + self.DH - 8, cx + 1, y + self.DH - 7], fill=c)
                x += self.COLON
                continue
            digit(x, y, ch, self.DW, self.DH, 2)
            x += self.DW + (self.GAP if k + 1 < len(big) and big[k + 1] is not None else 0)
        x += self.SEC_GAP
        sy = y + self.DH - self.SH
        for k in range(2):
            digit(x + k * (self.SW + 2), sy, s[4 + k], self.SW, self.SH, 1)
        out = glow(img, 1.2, 0.6)
        small = self.ctx.fonts("tom-thumb")
        small.draw(out, x + (2 * self.SW + 2 - small.width("SEC")) // 2, sy - 2, "SEC", tuple(v // 2 for v in on))
        bg = _rgb(self.settings["background"])
        if any(bg):   # the lit LEDs (and their glow) over the background: it shows through only where they're dark
            led = np.asarray(out, float)
            lit = led.max(axis=2, keepdims=True) / 255
            out = Image.fromarray(np.clip(led + np.array(bg, float) * (1 - lit), 0, 255).astype(np.uint8))
        return scaled(out, self.ctx.width, self.ctx.height)


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
