"""Clock: a ticking DD:HH:MM:SS (HH:MM:SS under a day) with the label above."""
from bongo.module import Style

from .common import Base


class Clock(Base, Style):
    fps = 5

    def load_fonts(self):
        f = self.ctx.fonts
        self.big, self.mid, self.small = f("7x13B"), f("6x10"), f("tom-thumb")

    def groups(self, font, parts, y, now):
        """Two-digit groups with hand-drawn colon dots so DD:HH:MM:SS fits 64px."""
        gw, sep = font.width("00"), 3
        x = (self.ctx.width - (gw * len(parts) + sep * (len(parts) - 1))) // 2
        items = []
        for i, p in enumerate(parts):
            items.append((font, x, y, f"{p:02d}", self.ink))
            x += gw
            if i < len(parts) - 1:
                items.append((self.small, x, y - 2, ":", self.accent))
                x += sep
        return items

    def render(self, now):
        s = self.settings
        picked = self.pick(now)
        if picked is None:
            return self.idle_frame(now)
        left, progress, over = picked
        if over:
            return self.done_frame(now)
        d, r = divmod(left, 86400)
        parts = [r // 3600, r % 3600 // 60, r % 60]
        parts = [d] + parts if d else parts
        font = self.mid if d else self.big
        items = self.line(self.small, self.title, 8, self.accent, now)
        items += self.groups(font, parts, self.ctx.height - 9, now)
        return self.paint(items, progress)
