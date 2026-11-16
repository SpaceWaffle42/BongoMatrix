"""Flip: three flip-clock tiles (DAYS/HRS/MIN, or HRS/MIN/SEC under a day) that flip when a digit changes
(two on a panel too small for three)."""
from PIL import Image, ImageDraw

from bongo.module import Style

from .common import Base, cap, ink, mix, seconds

FLIP = 0.25  # seconds a flip takes


class Flip(Base, Style):
    fps = 20

    def load_fonts(self):
        self.small = self.ctx.fonts("tom-thumb")

    def tile(self, text, font, w, h, letter=None):
        """One card: rounded, a touch lighter than the background, a hairline across the middle and
        the digits over it. (The hairline used to be drawn last and cut a row out of every digit.)
        ``letter``: the unit (D, H, M or S) small in the bottom corner, under the digits, for a card with
        no label beneath it."""
        card = mix(self.dark, self.ink, 0.2)
        img = Image.new("RGB", (w, h), card)
        ImageDraw.Draw(img).line([(0, h // 2), (w, h // 2)], fill=mix(self.dark, card, 0.1))
        first, last = ink(font, text)
        if font.width(text) + 2 <= w:                         # (as it always was, where the spacing fits)
            x = (w - font.width(text)) // 2
        else:                                                 # else centred on the digits' ink
            x = (w - (last - first)) // 2 - first
        if letter is None:
            font.draw(img, x, (h + cap(font)) // 2, text, self.ink)
            return img
        block = cap(font) + 1 + cap(self.small)
        top = (h - block) // 2
        font.draw(img, x, top + cap(font), text, self.ink)
        lf, ll = ink(self.small, letter)
        self.small.draw(img, w - 1 - ll, top + block, letter, mix(self.dark, self.accent, 0.8))
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
        exact = seconds(now, self.target)
        since = (-exact) % 1  # seconds since the displayed second last changed
        days = left >= 86400

        def units(n):
            d, r = divmod(n, 86400)
            return [d, r // 3600, r % 3600 // 60, r % 60]

        now_units, was_units = units(left), units(left + 1)
        labels = h >= 22
        gap, margin = (2, 2) if w >= 48 else (1, 1)
        ty, th = 1, h - 3 - (7 if labels else 0)
        first = 0 if days else 1                              # DAYS HRS MIN, or HRS MIN SEC under a day
        fit = self.layout(now_units[first:first + 3], w - 2 * margin, gap, th - 4)
        corner = False
        if fit is None:
            # two cards: the two biggest units that aren't 0, so the last minute counts its seconds
            # (it used to show "00 00"); with no labels under them, each card's unit in its corner
            first = next((i for i, v in enumerate(now_units[:2]) if v), 2)
            corner = not labels
            fit = self.layout(now_units[first:first + 2], w - 2 * margin, gap,
                              th - 2 - (1 + cap(self.small) if corner else 2), cards=(2,))
        font, widths = fit or (self.small, [w - 2 * margin])
        n = len(widths)
        new, old = now_units[first:first + n], was_units[first:first + n]
        names = ("DAYS", "HRS", "MIN", "SEC")[first:first + n]
        img = self.paint([], progress)
        x = (w - (sum(widths) + gap * (n - 1))) // 2
        for v, o, name, tw in zip(new, old, names, widths):
            mask = Image.new("L", (tw, th))
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, tw - 1, th - 1], radius=2 if th > 12 else 1, fill=255)
            letter = name[0] if corner else None
            tile = self.tile(f"{v:02d}", font, tw, th, letter)
            if v != o and since < FLIP:
                tile = self.flipped(self.tile(f"{o:02d}", font, tw, th, letter), tile, since / FLIP)
            img.paste(tile, (x, ty), mask)
            if labels:                                        # shortened where a card is narrower than its name
                name = next((t for t in (name, name[:3], name[0]) if self.small.width(t) - 1 <= tw), name[0])
                self.small.draw(img, x + (tw - self.small.width(name)) // 2, ty + th + 6, name, self.accent)
            x += tw + gap
        return img

    def layout(self, values, room, gap, max_cap, cards=(3,)):
        """(font, card widths) for the cards, each with a clear pixel either side of its digits, or None.
        Biggest font first: the cards the same width, else the first (days) card wider, so a hundred days
        or more doesn't spill off it. Measured by the fonts' spacing first (as it always was), then by
        the digits' ink alone, which is a pixel narrower and lets three cards fit 32 px."""
        fonts = [f for f in map(self.ctx.fonts, ("9x18B", "7x13B", "6x10", "5x7", "tom-thumb")) if cap(f) <= max_cap]
        fonts = fonts or [self.ctx.fonts("tom-thumb")]
        # (the tiny font only by its ink: by its spacing it won over a bigger font that fits by ink)
        passes = ((lambda f, t: f.width(t), [f for f in fonts if f is not self.small] or fonts),
                  (lambda f, t: ink(f, t)[1] - ink(f, t)[0], fonts))
        for size, choice in passes:
            for n in cards:
                space = room - gap * (n - 1)
                for f in choice:
                    need = [size(f, f"{v:02d}") + 2 for v in values[:n]]
                    if max(need) <= space // n:
                        return f, [space // n] * n
                    rest = (space - need[0]) // (n - 1)
                    if rest >= max(need[1:]):
                        return f, [space - rest * (n - 1)] + [rest] * (n - 1)
        return None
