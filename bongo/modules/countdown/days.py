"""Days: a big day count with a small label; under a day it becomes HH:MM."""
from .common import Base, remaining
from bongo.module import Style


class Days(Base, Style):
    fps = 5

    def load_fonts(self):
        f = self.ctx.fonts
        self.big, self.small = f("10x20"), f("5x7")

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
        caption = f"{unit} TO {label}"
        if self.small.width(caption) > self.ctx.width - 2:  # too long: alternate unit / label every 3s
            caption = unit if int(now.timestamp() // 3) % 2 == 0 else f"TO {label}"
        w, h = self.ctx.width, self.ctx.height
        items = [(self.big, (w - self.big.width(text)) // 2, h - 13, text, self.ink)]
        items += self.line(self.small, caption, h - 4, self.accent, now)
        return self.paint(items, progress)

