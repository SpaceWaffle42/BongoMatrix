"""Clock: a ticking DD:HH:MM:SS (HH:MM:SS under a day) with the label above."""
from bongo.module import Style

from .common import Base, cap


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
        w, h = self.ctx.width, self.ctx.height

        def wide(f, n):
            return f.width("00") * n + 3 * (n - 1)
        # the usual font, else a smaller one that fits the safe area; with days, the seconds go first
        font = next((f for f in (self.mid if d else self.big, self.ctx.fonts("5x7")) if wide(f, len(parts)) <= w), None)
        if font is None:
            font = next((f for f in (self.mid, self.ctx.fonts("5x7"), self.small) if wide(f, len(parts) - 1) <= w), self.small)
            parts = parts[:-1]
        baseline = h - max(2, 9 - (32 - h) // 2)             # 9 rows clear below on a full panel, fewer in a short one
        if h >= 20 and baseline - cap(font) >= 9:            # room for the label above
            items = self.line(self.small, self.title, 8, self.accent, now)
        else:
            items, baseline = [], (h + cap(font)) // 2
        items += self.groups(font, parts, baseline, now)
        return self.paint(items, progress)
