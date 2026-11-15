"""Ring: an anti-aliased progress ring over the final 30 days, with the count beside it."""
from PIL import Image, ImageDraw

from bongo.module import Style

from .common import Base, cap, fit_font, mix

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
            text, unit = f"{left // 3600:02d}:{left % 3600 // 60:02d}", "LEFT"
        frac = min(1.0, left / WINDOW)
        label_h = 7 if h >= 22 else 0
        d = max(8, min(h - 3 - label_h, int(w * .42)))
        tx = 2 + d + 4
        avail = w - tx - 2
        has_unit = h >= 22
        font = fit_font(self.ctx.fonts, ("9x18B", "7x13B", "6x10", "5x7", "tom-thumb"), text, avail,
                        d - (6 if has_unit else 0))
        block = cap(font) + (6 if has_unit else 0)
        top = 1 + (d - block) // 2
        items = [(font, tx + (avail - font.width(text)) // 2, top + cap(font), text, self.ink)]
        if has_unit:
            items.append((self.small, tx + (avail - self.small.width(unit)) // 2, top + block, unit, self.accent))
        if label_h:
            items += self.label_items(self.small, self.title, h - 3, self.accent, now)
        img = self.paint(items, progress)
        track, arc = self.ring(d, 3 if d >= 18 else 2, frac)
        img.paste(mix(self.dark, self.accent, .22), (2, 1), track)
        img.paste(self.accent, (2, 1), arc)
        return img
