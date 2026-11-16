"""Ring: an anti-aliased progress ring over the final 30 days, with the count beside it."""
from PIL import Image, ImageDraw

from bongo.module import Style

from .common import Base, cap, fit_font, mix, time_items, time_left

WINDOW = 30 * 86400
K = 4  # supersampling factor


class Ring(Base, Style):
    fps = 5

    def load_fonts(self):
        self.small = self.ctx.fonts("tom-thumb")

    def ring(self, d, thick, frac):
        """(track mask, arc mask), each drawn at K x and box-downsampled to d x d."""
        masks = []
        for sweep in (360, 360 * frac):
            m = Image.new("L", (d * K, d * K))
            if sweep > 0:
                ImageDraw.Draw(m).arc([0, 0, d * K - 1, d * K - 1], -90, -90 + sweep, fill=255, width=thick * K)
            masks.append(m.resize((d, d), Image.BOX))
        return masks

    def render(self, now):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        picked = self.pick(now)
        if picked is None:
            return self.idle_frame(now)
        left, progress, over = picked
        if over:
            return self.done_frame(now)
        if left >= 86400:
            n = self.days_left(now)
            text, unit = str(n), "DAY" if n == 1 else "DAYS"
        else:
            text, unit = time_left(left), "LEFT"
        frac = min(1.0, left / WINDOW)
        label_h = 7 if h >= 22 else 0
        span = d = max(8, min(h - 3 - label_h, int(w * .42)))   # (rows the ring and the count share)
        tx = 2 + d + 4
        avail = w - tx - 2
        has_unit = h >= 22
        fonts = ("9x18B", "7x13B", "6x10", "5x7", "tom-thumb")
        font = fit_font(self.ctx.fonts, fonts, text, avail, d - (6 if has_unit else 0))
        if font.width(text) > avail:
            # the count doesn't fit beside the ring: the ring shrinks to make room, and goes if it would
            # be smaller than 8 px (it used to run into "00:02" and push it off the edge)
            d = w - 2 - font.width(text) - 4 - 2
            if d < 8:                                         # no ring: the count as big as the panel takes
                font = fit_font(self.ctx.fonts, fonts, text, w - 4, span - (6 if has_unit else 0))
            tx = 2 + d + 4 if d >= 8 else 0
            avail = w - tx - 2 if d >= 8 else w
        block = cap(font) + (6 if has_unit else 0)
        top = 1 + (span - block) // 2
        items = time_items(font, tx + (avail - font.width(text)) // 2, top + cap(font), text, self.ink)
        if has_unit:
            items.append((self.small, tx + (avail - self.small.width(unit)) // 2, top + block, unit, self.accent))
        if label_h:
            items += self.label_items(self.small, self.title, h - 3, self.accent, now)
        img = self.paint(items, progress)
        if d < 8:
            return img
        track, arc = self.ring(d, 3 if d >= 18 else 2, frac)
        at = (2, 1 + (span - d) // 2)
        img.paste(mix(self.dark, self.accent, .22), at, track)
        img.paste(self.accent, at, arc)
        return img
