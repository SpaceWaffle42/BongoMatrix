"""Tearoff: a desk-calendar page with the target month on the header and the day count in ink."""
from bongo.module import Style

from .common import Base, cap, fit_font, mix

# LEDs show light, so the page is a dim warm glow and the ink is bright (dark ink reads as holes).
PAGE, INK, HOLE = (52, 42, 30), (255, 244, 222), (8, 6, 4)


class Tearoff(Base, Style):
    fps = 5

    def load_fonts(self):
        self.small = self.ctx.fonts("tom-thumb")

    def render(self, now):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        picked = self.pick(now)
        if picked is None:
            return self.idle_frame(now)
        left, progress, over = picked
        if over:
            return self.done_frame(now)
        if left >= 86400:
            n = left // 86400
            text, unit = str(n), "DAY" if n == 1 else "DAYS"
        else:
            text, unit = f"{left // 3600:02d}:{left % 3600 // 60:02d}", "LEFT"
        img = self.paint([], progress)
        pw = min(w - 6, 40)
        x0, y0, y1 = (w - pw) // 2, 0, h - 2  # page spans y0..y1 inclusive, just above the bar
        x1 = x0 + pw - 1
        hh = 9 if h >= 24 else 7  # header height
        img.paste(PAGE, (x0, y0, x1 + 1, y1 + 1))
        img.paste(mix(self.dark, self.accent, 0.3), (x0, y0, x1 + 1, y0 + hh - 1))
        img.paste(self.accent, (x0, y0 + hh - 1, x1 + 1, y0 + hh))  # tear line
        month = self.target.strftime("%b").upper()
        self.small.draw(img, x0 + (pw - self.small.width(month)) // 2, y0 + hh - 3, month, self.accent)
        if hh >= 9:  # binding holes
            for hx in range(x0 + 4, x1 - 2, max(5, (pw - 8) // 4)):
                img.paste(HOLE, (hx, y0 + 1, hx + 2, y0 + 2))
        body = y1 - y0 - hh + 1  # rows of cream under the header
        with_unit = body >= 18
        font = fit_font(self.ctx.fonts, ("9x18B", "7x13B", "6x10", "5x7", "tom-thumb"), text, pw - 4,
                        body - (10 if with_unit else 3))
        top = y0 + hh + (body - (cap(font) + (6 if with_unit else 0))) // 2
        font.draw(img, x0 + (pw - font.width(text)) // 2, top + cap(font), text, INK)
        if with_unit:
            self.small.draw(img, x0 + (pw - self.small.width(unit)) // 2, top + cap(font) + 6, unit, self.accent)
        return img
