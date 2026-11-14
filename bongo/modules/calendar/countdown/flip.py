"""Flip: three flip-clock tiles (DAYS/HRS/MIN, or HRS/MIN/SEC under a day) that flip when a digit changes."""
from PIL import Image, ImageDraw

from bongo.module import Style

from .common import Base, cap, fit_font, mix

FLIP = 0.25  # seconds a flip takes


class Flip(Base, Style):
    fps = 20

    def load_fonts(self):
        self.small = self.ctx.fonts("tom-thumb")

    def tile(self, text, font, w, h):
        """One card: rounded, a touch lighter than the background, a hairline across the middle and
        the digits over it. (The hairline used to be drawn last and cut a row out of every digit.)"""
        card = mix(self.dark, self.ink, 0.2)
        img = Image.new("RGB", (w, h), card)
        ImageDraw.Draw(img).line([(0, h // 2), (w, h // 2)], fill=mix(self.dark, card, 0.1))
        font.draw(img, (w - font.width(text)) // 2, (h + cap(font)) // 2, text, self.ink)
        return img

    def flipped(self, old, new, p):
        """Mid-flip tile: old top flap squashes away (p<.5), then the new bottom flap opens."""
        w, h = new.size
        half = h // 2
        out = new.copy()
        out.paste(old.crop((0, half, w, h)), (0, half))
        if p < .5:
            sq = max(0, round(half * (1 - 2 * p)))
            if sq:
                out.paste(old.crop((0, 0, w, half)).resize((w, sq)), (0, half - sq))
        else:
            sq = max(1, round((h - half) * (2 * p - 1)))
            out.paste(new.crop((0, half, w, h)).resize((w, sq)), (0, half))
        return out

    def render(self, now):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        picked = self.pick(now)
        if picked is None:
            return self.idle_frame(now)
        left, progress, over = picked
        if over:
            return self.done_frame(now)
        exact = (self.target - now).total_seconds()
        since = (-exact) % 1  # seconds since the displayed second last changed
        days = left >= 86400

        def vals(n):
            d, r = divmod(n, 86400)
            hms = [r // 3600, r % 3600 // 60, r % 60]
            return [d] + hms[:2] if days else hms

        new, old = vals(left), vals(left + 1)
        names = ("DAYS", "HRS", "MIN") if days else ("HRS", "MIN", "SEC")
        labels = h >= 22
        gap, margin = (2, 2) if w >= 48 else (1, 1)
        tw = (w - 2 * margin - 2 * gap) // 3
        ty, th = 1, h - 3 - (7 if labels else 0)
        font = fit_font(self.ctx.fonts, ("9x18B", "7x13B", "6x10", "5x7", "tom-thumb"),
                        "0" * max(2, len(str(new[0]))), tw - 2, th - 4)
        img = self.paint([], progress)
        x = (w - (3 * tw + 2 * gap)) // 2
        mask = Image.new("L", (tw, th))
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, tw - 1, th - 1], radius=2 if th > 12 else 1, fill=255)
        for v, o, name in zip(new, old, names):
            tile = self.tile(f"{v:02d}", font, tw, th)
            if v != o and since < FLIP:
                tile = self.flipped(self.tile(f"{o:02d}", font, tw, th), tile, since / FLIP)
            img.paste(tile, (x, ty), mask)
            if labels:
                self.small.draw(img, x + (tw - self.small.width(name)) // 2, ty + th + 6, name, self.accent)
            x += tw + gap
        return img
