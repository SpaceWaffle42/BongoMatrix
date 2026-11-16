"""Text: your own words on the panel. ``scroll`` is a marquee, ``static`` stays put."""
import math
import re

from bongo.fonts import align_x, names
from bongo.module import Module, Style
from bongo.scroll import HOLD, Scrolls

FALLBACK = ("6x10", "5x7", "tom-thumb")  # tried in order when static text is too wide


def pitch(font, gap: int = 1) -> int:
    """Line spacing: cap height of 'H' plus descent of 'g' plus a gap."""
    g = font.glyphs[ord("g")]
    return font.glyphs[ord("H")][2] + (max(0, g[3].height - g[2]) if g[3] else 0) + gap


def middle(font, height: int, lines: int = 1, gap: int = 1) -> int:
    """Baseline of the first line so ``lines`` lines sit centred in ``height``."""
    return (height - (lines * pitch(font, gap) - gap)) // 2 + font.glyphs[ord("H")][2]


# typed or pasted characters some fonts lack (tom-thumb has no dashes or curly quotes), and their plain
# stand-ins, so they don't show as "?"
PLAIN = {"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
         "\u2026": "...", "\u00a0": " "}


def readable(font, text: str) -> str:
    """``text`` as ``font`` can draw it on one line: line breaks become spaces, and a dash or curly quote
    the font hasn't got becomes its plain one."""
    text = re.sub(r"\s*[\r\n]+\s*", " ", text).replace("\t", " ")
    return "".join(PLAIN.get(c, c) if ord(c) not in font.glyphs else c for c in text)


def colour(value: str) -> tuple:
    h = str(value).lstrip("#")
    if len(h) != 6:
        raise ValueError(f"text: bad colour {value!r}, expected #RRGGBB")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class _Base(Style):
    fps = 30

    def setup(self):
        s = self.settings
        self.ink, self.bg = colour(s["colour"]), colour(s["background"])
        if s["background_style"] not in ("solid", "gradient"):
            raise ValueError(f"text: background_style must be solid or gradient, not {s['background_style']!r}")
        if s["align"] not in ("left", "center", "right"):
            raise ValueError(f"text: align must be left, center or right, not {s['align']!r}")
        self.font = self.ctx.fonts(s["font"])

    def baseline(self, font):
        """The configured ``y``, or vertically centred when ``y`` is None (the default)."""
        y = self.settings["y"]
        return middle(font, self.ctx.height) if y is None else y

    @property
    def text(self):
        t = str(self.settings["text"])
        return t.upper() if self.settings["uppercase"] else t

    def background(self):
        img = self.ctx.canvas(self.bg)
        if self.settings["background_style"] == "gradient":
            w, h = self.ctx.width, self.ctx.height
            for y in range(h):  # vertical blend to a darker shade of the background
                k = 1 - 0.55 * y / max(1, h - 1)
                img.paste(tuple(int(c * k) for c in self.bg), (0, y, w, y + 1))
        return img


class Scroll(Scrolls, _Base):
    """A marquee. It starts from the first letter when the screen comes on, and the screen stays until the
    text has gone all the way along ``times_to_scroll`` times (longer than its seconds if need be)."""
    defaults = dict(speed=24, gap=24, times_to_scroll=1)

    def render(self, now):
        s = self.settings
        return self.marquee(now, self.font, s["speed"], s["gap"], s["times_to_scroll"], self.baseline(self.font))

    def marquee(self, now, font, speed, gap, times, baseline):
        img, text = self.background(), readable(font, self.text)
        period = font.width(text) + max(0, int(gap))
        if period <= 0:
            return img
        # how far the marquee has travelled since this screen first drew it: held at its first letter, then along
        need = HOLD + period / speed * max(1, int(times))
        offset = math.floor(max(0.0, self.scrolled(("text", text), now, need) - HOLD) * speed + 1e-6) % period
        x = -offset
        while x < self.ctx.width:
            font.draw(img, x, baseline, text, self.ink)
            x += period
        return img


class Static(Scrolls, _Base):
    def _split(self, font, text):
        """Best two-line split at spaces (smallest widest line), or None if neither half fits."""
        words, best = text.split(), None
        for i in range(1, len(words)):
            lines = (" ".join(words[:i]), " ".join(words[i:]))
            widest = max(font.width(l) for l in lines)
            if widest <= self.ctx.width and (best is None or widest < best[0]):
                best = (widest, lines)
        return best and best[1]

    def _layout(self, text):
        """First option that fits: (font, lines, is_fallback). Falls back to the smallest font, which may not
        fit (``render`` scrolls it then)."""
        w, f = self.ctx.width, self.ctx.fonts
        own = f(self.settings["font"])
        if own.width(readable(own, text)) <= w:
            return own, [readable(own, text)], False
        for name in FALLBACK[:2]:
            if f(name).width(readable(f(name), text)) <= w:
                return f(name), [readable(f(name), text)], True
        for name in FALLBACK[:2]:
            if (lines := self._split(f(name), readable(f(name), text))):
                return f(name), list(lines), True
        tiny = f(FALLBACK[2])
        text = readable(tiny, text)
        return tiny, list(self._split(tiny, text) or [text]) if tiny.width(text) > w else [text], True

    def render(self, now):
        img, text, s = self.background(), self.text, self.settings
        font, lines, fallback = self._layout(text)
        if any(font.width(line) > self.ctx.width for line in lines):   # too long even small and on two lines: it goes
            return Scroll.marquee(self, now, self.font, 24, 24, 1, self.baseline(self.font))   # along once, in its own font
        w, h = self.ctx.width, self.ctx.height
        first = middle(font, h, len(lines))
        step = pitch(font)
        for i, line in enumerate(lines):
            tw = font.width(line)
            x = align_x(s["align"], tw, w)
            y = first + i * step if fallback else self.baseline(font)
            font.draw(img, x, y, line, self.ink)
        return img


MODULE = Module(
    id="text", name="Text",
    description="Your own text: a scrolling marquee or a still line.",
    styles={"scroll": Scroll, "static": Static},
    defaults=dict(
        text="Hello from Bongo Matrix", font="6x13B", colour="#FFF4DE", background="#0B0C0E",
        background_style="solid", y=None, align="center", uppercase=False,
    ),
    choices={"font": names(), "background_style": ["solid", "gradient"], "align": ["left", "center", "right"]},
    basic=("text", "colour", "align"),
    limits={"speed": (1, 200), "gap": (0, 256), "y": (-64, 256), "times_to_scroll": (1, 10)},
)
