"""Days: a big day count with a small label; under a day it becomes HH:MM (M:SS under an hour)."""
from .common import Base, time_left
from PIL import Image

from bongo.module import Style


NUMBERS = ("10x20", "9x18B", "9x15B", "7x13B", "6x10", "5x7")   # the number's fonts, biggest first


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
        picked = self.pick(now)
        if picked is None:
            return self.idle_frame(now)
        left, progress, over = picked
        if over:
            return self.done_frame(now)
        label = self.title
        if left >= 86400:
            n = self.days_left(now)
            text, unit = str(n), "DAY" if n == 1 else "DAYS"
        else:
            text, unit = time_left(left), "LEFT"
        w, h = self.ctx.width, self.ctx.height
        caption = f"{unit} TO {label}"
        if not self.fits(caption) and self.fits(f"TO {label}"):  # too long: alternate unit / label every 3s
            half = self.scrolled(("caption", label), now, 6.0)         # both halves, from the first, before it moves on
            caption = unit if int(half // 3) % 2 == 0 else f"TO {label}"
        # (a label too long even on its own: the whole caption scrolls, so it can be read through)
        font = self.small if self.small.width(caption) <= w - 2 or not self.fits(caption) else self.tiny
        big, at, under = self.big, h - 13, h - 4
        tw = big.width(text)
        x = (w - tw) // 2
        if h < 30:   # a short panel: the caption sits lower and the number takes the biggest font that clears the top
            font = font if h >= 22 else self.tiny
            under = h - (4 if h >= 28 else 2)
            # measured from the caption's usual font, so every event on this panel gets the same size of number
            at = under - (self.small.cap if h >= 22 else self.tiny.cap) - 2
            big = next((f for f in map(self.ctx.fonts, NUMBERS) if f.cap <= at and f.width(text) <= w - 2), self.small)
            if ":" in text and big.cap in OWN_COLON:
                return self.clock(now, big, text, at, font, caption, under, progress)
            left, _, right, _ = inked(big, text)
            tw = right - left + 1
            x = (w - tw) // 2 - left          # centred on its lit pixels: these fonts pad their glyphs unevenly
        icon_at = None
        if self.icon is not None and tw + 10 <= w - 2 and at - big.cap // 2 - 4 >= 1:  # the event's icon to the left of the number
            x += 5
            icon_at = (x - 10 + (inked(big, text)[0] if h < 30 else 0), at - big.cap // 2 - 4)
        items = [(big, x, at, text, self.ink)]
        items += self.label_items(font, caption, under, self.accent, now)
        return self.paint(items, progress, icon_at)

    def clock(self, now, big, text, at, font, caption, under, progress):
        """HH:MM in a middling font, whose own colon is a cross or hangs below the digits: the two
        halves with two square dots between them, two clear columns each side."""
        hours, minutes = text.split(":")
        (l1, top, r1, bottom), (l2, _, r2, _) = inked(big, hours), inked(big, minutes)
        w1, w2 = r1 - l1 + 1, r2 - l2 + 1
        x = (self.ctx.width - (w1 + 6 + w2)) // 2
        items = [(big, x - l1, at, hours, self.ink), (big, x + w1 + 6 - l2, at, minutes, self.ink)]
        img = self.paint(items + self.label_items(font, caption, under, self.accent, now), progress)
        tall = bottom - top + 1
        for y in (at + top + tall // 4, at + bottom - tall // 4 - 1):
            img.paste(self.ink, (x + w1 + 2, y, x + w1 + 4, y + 2))
        return img


OWN_COLON = (11, 8)   # the cap heights of 7x13B and 6x10


def inked(font, text):
    """(left, top, right, bottom) of the lit pixels of ``text`` drawn at x=0, rows counted from its baseline."""
    img = Image.new("L", (font.width(text) + 4, font.cap + 12))
    font.draw(img, 0, font.cap + 6, text, 255)
    box = img.getbbox() or (0, 6, font.width(text), font.cap + 6)
    return box[0], box[1] - font.cap - 6, box[2] - 1, box[3] - 1 - font.cap - 6
