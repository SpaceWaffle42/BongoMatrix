"""Clock: a ticking DD:HH:MM:SS (HH:MM:SS under a day) with the label above."""
from bongo.module import Style

from .common import Base, cap, ink


def wide(font, parts) -> int:
    """Width of the groups drawn by ``Clock.groups``: the digits as they are, 3 px for each colon."""
    return sum(font.width(f"{p:02d}") for p in parts) + 3 * (len(parts) - 1)


def lit(font, parts) -> int:
    """``wide`` less the blank columns before the first digit and after the last: what must fit the
    panel with a clear pixel either side (00:02:00 ran edge to edge at 48 px)."""
    first, last = f"{parts[0]:02d}", f"{parts[-1]:02d}"
    return wide(font, parts) - ink(font, first)[0] - (font.width(last) - ink(font, last)[1])


class Clock(Base, Style):
    fps = 5

    def load_fonts(self):
        f = self.ctx.fonts
        self.big, self.mid, self.small = f("7x13B"), f("6x10"), f("tom-thumb")

    def groups(self, font, parts, y, now):
        """Two-digit groups with hand-drawn colon dots so DD:HH:MM:SS fits 64px. Each group is as wide
        as its own digits: a hundred days or more is three, and used to run into the hours."""
        sep, w = 3, self.ctx.width
        x = (w - wide(font, parts)) // 2
        if wide(font, parts) > w - 2:                         # tight: centred on the ink, not flush left
            lead, trail = ink(font, f"{parts[0]:02d}")[0], font.width(f"{parts[-1]:02d}") - ink(font, f"{parts[-1]:02d}")[1]
            x = (w - (wide(font, parts) - lead - trail)) // 2 - lead
        items = []
        for i, p in enumerate(parts):
            items.append((font, x, y, f"{p:02d}", self.ink))
            x += font.width(f"{p:02d}")
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
        # the usual font, else a smaller one that fits the safe area; with days, the seconds go first
        font = next((f for f in (self.mid if d else self.big, self.ctx.fonts("5x7")) if lit(f, parts) + 2 <= w), None)
        if font is None:
            # under an hour the hours go rather than the seconds (the last minute read 00:00)
            parts = parts[1:] if not d and parts[0] == 0 else parts[:-1]
            font = next((f for f in (self.mid, self.ctx.fonts("5x7"), self.small) if lit(f, parts) + 2 <= w), None)
        if font is None:                                      # hundreds of days on a tiny panel: days and hours
            font, parts = self.small, parts[:-1]
        baseline = h - max(2, 9 - (32 - h) // 2)             # 9 rows clear below on a full panel, fewer in a short one
        if h >= 20 and baseline - cap(font) >= 9:            # room for the label above
            items = self.label_items(self.small, self.title, 8, self.accent, now)
        else:
            items, baseline = [], (h + cap(font)) // 2
        items += self.groups(font, parts, baseline, now)
        return self.paint(items, progress)
