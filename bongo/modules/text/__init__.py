"""Text: your own words on the panel. ``scroll`` is a marquee, ``static`` stays put."""
import math

from bongo.fonts import align_x
from bongo.module import Module, Style

FALLBACK = ("6x10", "5x7", "tom-thumb")  # tried in order when static text is too wide


def pitch(font, gap: int = 1) -> int:
    """Line spacing: cap height of 'H' plus descent of 'g' plus a gap."""
    g = font.glyphs[ord("g")]
    return font.glyphs[ord("H")][2] + (max(0, g[3].height - g[2]) if g[3] else 0) + gap


def middle(font, height: int, lines: int = 1, gap: int = 1) -> int:
    """Baseline of the first line so ``lines`` lines sit centred in ``height``."""
    return (height - (lines * pitch(font, gap) - gap)) // 2 + font.glyphs[ord("H")][2]


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


class Scroll(_Base):
    defaults = dict(speed=24, gap=24)

    def render(self, now):
        img, text, s = self.background(), self.text, self.settings
        period = self.font.width(text) + max(0, int(s["gap"]))
        if period <= 0:
            return img
        # Pure function of the clock: how far the marquee has travelled since the epoch.
        offset = math.floor(now.timestamp() * s["speed"] + 1e-6) % period
        x = -offset
        while x < self.ctx.width:
            self.font.draw(img, x, self.baseline(self.font), text, self.ink)
            x += period
        return img


class Static(_Base):
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
        """First option that fits: (font, lines, is_fallback). Falls back to a clipped smallest font."""
        w, f = self.ctx.width, self.ctx.fonts
        if f(self.settings["font"]).width(text) <= w:
            return f(self.settings["font"]), [text], False
        for name in FALLBACK[:2]:
            if f(name).width(text) <= w:
                return f(name), [text], True
        for name in FALLBACK[:2]:
            if (lines := self._split(f(name), text)):
                return f(name), list(lines), True
        tiny = f(FALLBACK[2])
        return tiny, list(self._split(tiny, text) or [text]) if tiny.width(text) > w else [text], True

    def render(self, now):
        img, text, s = self.background(), self.text, self.settings
        font, lines, fallback = self._layout(text)
        w, h = self.ctx.width, self.ctx.height
        first = middle(font, h, len(lines))
        step = pitch(font)
        for i, line in enumerate(lines):
            tw = font.width(line)
            # If it still overflows, align the overflow so it clips at the edge(s) cleanly.
            x = align_x(s["align"], tw, w) if tw <= w else {"left": 0, "right": w - tw}.get(s["align"], (w - tw) // 2)
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
)
