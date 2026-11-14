"""Digital: big numbers, a split-flap flip clock, and red seven-segment LEDs."""
from __future__ import annotations

import datetime as dt

import numpy as np
from PIL import Image, ImageDraw

from bongo.module import Style

from .common import HOURS, colon_on, digits, glow, hm, ink_box, tall

W, H = 64, 32


class Big(Style):
    """Big numbers: large hours and minutes with a blinking colon, the date underneath."""
    group = "Digital"
    fps = 4
    defaults = {"hours": "24", "colour": "#FFB547", "show_date": True, "date_matches_colour": True,
                "date_colour": "#BF8735", "background": "#000000", "font": "10x20"}
    choices = {"hours": HOURS, "font": ["10x20", "9x18B"]}
    basic = ("hours", "colour")

    SMALLER = ("9x18B", "7x13B", "6x10", "5x7")   # tried in turn where the chosen font is too big for a safe area

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h), _rgb(self.settings["background"]))
        d = ImageDraw.Draw(img)
        small = self.ctx.fonts("5x7")
        colour = _rgb(self.settings["colour"])
        hours, minutes = hm(now, self.settings["hours"]).split(":")
        colon_w = 6                                          # a 2 px colon with 2 px each side
        for name in (self.settings["font"], *self.SMALLER):   # the chosen font, or the biggest that fits
            big = self.ctx.fonts(name)
            wh, wm = big.width(hours), big.width(minutes)
            box, end = ink_box(big, hours), ink_box(big, minutes)   # centre the ink, not the letter boxes
            ink_w = wh - box[0] + colon_w + end[2]
            _, top, _, bottom = ink_box(big, "0123456789")
            height = bottom - top
            if ink_w <= w and height <= h:
                break
        x = (w - ink_w) // 2 - box[0]
        # a full panel has the digits 4 rows down and the date's baseline 3 from the bottom; a safe area
        # closes both margins up, and where the date would touch the digits it's the time alone, centred
        y_top, date_line = max(0, 4 - (H - h + 1) // 2), h - 1 - max(0, 2 - (H - h) // 4)
        date = now.strftime("%a %d %b").upper()
        if small.width(date) > w:
            small = self.ctx.fonts("tom-thumb")
        dated = self.settings["show_date"] and y_top + height + 1 <= date_line - small.cap and small.width(date) <= w
        if not dated:
            y_top = (h - height) // 2
        baseline = y_top + (30 - top)
        big.draw(img, x, baseline, hours, colour)
        big.draw(img, x + wh + colon_w, baseline, minutes, colour)
        if colon_on(now):
            cx = x + wh + colon_w // 2 - 1
            mid = y_top + height // 2
            for yy in (mid - 3, mid + 2):                    # two dots centred on the digits
                d.rectangle([cx, yy, cx + 1, yy + 1], fill=colour)
        if dated:
            shade = tuple(c * 3 // 4 for c in colour) if self.settings["date_matches_colour"] else _rgb(self.settings["date_colour"])
            small.draw(img, (w - small.width(date)) // 2, date_line, date, shade)   # a dimmer shade of the time, or your own
        return img


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

    DIGITS = (("9x18B", 2), ("6x13B", 2), ("9x18B", 1), ("6x10", 1), ("5x7", 1), ("tom-thumb", 1))   # font, times taller

    def setup(self):
        """The cards as big as the canvas allows: 13 x 27 on a full panel, narrower and shorter inside
        a safe area, with the biggest digits that fit them."""
        self.cards = {}
        w, h = self.ctx.width, self.ctx.height
        self.mid = self.MID if w >= 4 * self.CARD + 2 * self.GAP + self.MID else 4
        self.wide = max(5, min(self.CARD, (w - 2 * self.GAP - self.mid) // 4))
        self.half = max(4, min(self.HALF, (h - 1) // 2))
        for name, scale in self.DIGITS:
            sizes = [tall(self.ctx.fonts(name), ch, scale).size for ch in "0123456789"]
            if max(g[0] for g in sizes) <= self.wide - 2 and max(g[1] for g in sizes) <= 2 * self.half - 2:
                break
        self.digit = (name, scale)

    def card(self, ch):
        """A card: top half, a 1 px hinge, bottom half (27 rows); the digit split across the hinge."""
        if ch not in self.cards:
            face = Image.new("RGB", (self.wide, 2 * self.half), self.FACE)
            if ch != " ":
                g = tall(self.ctx.fonts(self.digit[0]), ch, self.digit[1])
                face.paste(self.INK, ((self.wide - g.width) // 2, (2 * self.half - g.height) // 2), g)
            for x in (0, self.wide - 1):                     # rounded corners
                for y in (0, 2 * self.half - 1):
                    face.putpixel((x, y), self.BG)
            self.cards[ch] = (face.crop((0, 0, self.wide, self.half)), face.crop((0, self.half, self.wide, 2 * self.half)))
        return self.cards[ch]

    def shown(self, now):
        s = digits(now, self.settings["hours"])
        return (" " + s[1:]) if self.settings["hours"] == "12" and s[0] == "0" else s   # 12-hour: a blank first card

    def render(self, now):
        W, H, CARD, HALF, MID = self.ctx.width, self.ctx.height, self.wide, self.half, self.mid
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        new = self.shown(now)
        old = self.shown(now - dt.timedelta(minutes=1))
        into = now.second + now.microsecond / 1e6
        width = 4 * CARD + 2 * self.GAP + MID
        x0 = (W - width) // 2
        y = (H - (2 * HALF + 1)) // 2
        for i in range(4):
            x = x0 + i * (CARD + self.GAP) + (MID - self.GAP if i >= 2 else 0)
            (nt, nb), (ot, ob) = self.card(new[i]), self.card(old[i])
            p = into / self.FLIP if now.second == 0 and into < self.FLIP and new[i] != old[i] else 1.0
            top, bottom = y, y + HALF + 1
            if p >= 1:
                img.paste(nt, (x, top))
                img.paste(nb, (x, bottom))
                continue
            img.paste(nt, (x, top))                          # the new top waits behind the falling flap
            img.paste(ob, (x, bottom))                       # the old bottom shows until it's covered
            if p < 0.5:                                      # the old top folds down towards the hinge
                h = max(1, round(HALF * (1 - p * 2)))
                flap = ot.resize((CARD, h)).point(lambda v: int(v * (0.8 + 0.2 * (1 - p * 2))))
                img.paste(flap, (x, top + HALF - h))
            else:                                            # the new bottom swings down into place
                h = max(1, round(HALF * (p - 0.5) * 2))
                flap = nb.resize((CARD, h)).point(lambda v: int(v * (0.8 + 0.2 * (p - 0.5) * 2)))
                img.paste(flap, (x, bottom))
        cx = x0 + 2 * CARD + self.GAP + (MID - 2) // 2
        mid, apart = y + HALF, max(2, round(4 * HALF / self.HALF))
        for yy in (mid - apart, mid + apart - 1):            # the colon, centred on the cards
            d.rectangle([cx, yy, cx + 1, yy + 1], fill=(200, 198, 188))
        return img


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

    # digit width, colon zone, space before the seconds (0: no seconds), tried in turn until the row fits
    FITS = ((8, 6, 3), (7, 6, 2), (8, 6, 0), (7, 6, 0), (7, 4, 0), (6, 4, 0), (5, 2, 0))

    def render(self, now):
        W, H = self.ctx.width, self.ctx.height
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
        DH = min(self.DH, H - 2 if H < self.DH + 2 else H)   # a safe area: shorter digits, then closer, then no seconds
        SH = min(self.SH, DH // 2 + 1)
        for DW, COLON, SEC_GAP in self.FITS:
            width = sum(DW if c else COLON for c in big) + self.GAP * (len(big) - 3) \
                + (SEC_GAP + 2 * self.SW + 2 if SEC_GAP else 0)
            if width <= W:
                break
        x = (W - width) // 2
        y = (H - DH) // 2
        dot = round(6 * DH / self.DH)
        for k, ch in enumerate(big):
            if ch is None:
                c = on if colon_on(now) else off
                cx = x + (COLON - 2) // 2
                d.rectangle([cx, y + dot, cx + 1, y + dot + 1], fill=c)
                d.rectangle([cx, y + DH - dot - 2, cx + 1, y + DH - dot - 1], fill=c)
                x += COLON
                continue
            digit(x, y, ch, DW, DH, 2)
            x += DW + (self.GAP if k + 1 < len(big) and big[k + 1] is not None else 0)
        x += SEC_GAP
        sy = y + DH - SH
        for k in range(2 if SEC_GAP else 0):
            digit(x + k * (self.SW + 2), sy, s[4 + k], self.SW, SH, 1)
        out = glow(img, 1.2, 0.6)
        small = self.ctx.fonts("tom-thumb")
        if SEC_GAP and sy - 2 - small.cap >= 0:
            small.draw(out, x + (2 * self.SW + 2 - small.width("SEC")) // 2, sy - 2, "SEC", tuple(v // 2 for v in on))
        bg = _rgb(self.settings["background"])
        if any(bg):   # the lit LEDs (and their glow) over the background: it shows through only where they're dark
            led = np.asarray(out, float)
            lit = led.max(axis=2, keepdims=True) / 255
            out = Image.fromarray(np.clip(led + np.array(bg, float) * (1 - lit), 0, 255).astype(np.uint8))
        return out


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
