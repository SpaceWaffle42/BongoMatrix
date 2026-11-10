"""Days: a big day count with a small label; under a day it becomes HH:MM."""
from .common import Base, remaining
from bongo.module import Style


class Days(Base, Style):
    fps = 5

    def load_fonts(self):
        f = self.ctx.fonts
        self.big, self.small, self.tiny = f("10x20"), f("5x7"), f("tom-thumb")

    def fits(self, text):
        """Fits on one line in 5x7, or at least in tom-thumb, before falling back to scrolling."""
        return min(self.small.width(text), self.tiny.width(text)) <= self.ctx.width - 2

    def render(self, now):
        s = self.settings
        left, progress, over = remaining(s, now)
        if over:
            return self.done_frame(now)
        label = s["label"].upper()
        if left >= 86400:
            n = left // 86400
            text, unit = str(n), "DAY" if n == 1 else "DAYS"
        else:
            text, unit = f"{left // 3600:02d}:{left % 3600 // 60:02d}", "LEFT"
        w, h = self.ctx.width, self.ctx.height
        caption = f"{unit} TO {label}"
        if not self.fits(caption):  # too long: alternate unit / label every 3s
            caption = unit if int(now.timestamp() // 3) % 2 == 0 else f"TO {label}"
        font = self.small if self.small.width(caption) <= w - 2 or not self.fits(caption) else self.tiny
        items = [(self.big, (w - self.big.width(text)) // 2, h - 13, text, self.ink)]
        items += self.line(font, caption, h - 4, self.accent, now)
        return self.paint(items, progress)
