"""The loading screen, which is also OpenWeather's credit: their name, and a code that opens their website.

OpenWeather's free plan asks for the credit wherever its weather is shown, so it comes up every time a
Weather screen does, for at least one full pass, and stays while the weather is being fetched.

Where the code fits beside the name, it's one screen. Where it doesn't, the screen flicks: the name,
then the code with what it's for beside it, or (no room beside it) the name, then what the code is for,
then the code alone. Too small for a code at all: the name alone. Each screen pushes the next in from
the right; the code wipes in top to bottom once it has landed; a thin bar slides to and fro along the
bottom row all the while, so it's clear something is happening.

Everything that moves on its own (the bar, the cloud, the shine on the words) follows one clock, the
seconds since the screen came on (``T``); only the slides and the wipe follow the screen they're on.

OpenWeather's own icon (``resources/media/attribution/openweather.png``, as they supply it) sits above the
name, still: 12 rows tall on a 64x32 panel, 16 on one 48 rows or more, 10 where only that fits. It is never
redrawn, only scaled down, with every LED fully on or off. Without the file (or no room for it beside the code) a cloud takes its place.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from bongo.modules.qr.codes import block

from .look import icon

SITE = "HTTPS://OPENWEATHERMAP.ORG"   # (in capitals: a smaller code, and any phone opens it all the same)
LOGO = Path(__file__).resolve().parents[3] / "resources" / "media" / "attribution" / "openweather.png"
LIGHT = (200, 200, 200)               # the code's light parts: a little under full white, easier for a phone off LEDs
SLIDE, WIPE = 0.35, 0.4               # s for one screen to push the next in; for the code to wipe in
ONE = 3.0                             # s the one-screen credit stays up at least (with the code: once it is all in)
NAME, TELL, SCAN = 2.0, 1.5, 3.5      # s each flicking screen stays once it has landed


def code(rows: int, cols: int) -> Image.Image | None:
    """The code as big as fits ``rows`` x ``cols`` in whole pixels a module (quiet zone 2, else 1), or None."""
    for quiet in (2, 1):
        b = block(SITE, quiet)
        if b.height <= rows and b.width <= cols:
            k = max(1, min(rows // b.height, cols // b.width))
            b = b.resize((b.width * k, b.height * k), Image.NEAREST)
            b.paste(LIGHT, mask=b.convert("L").point(lambda v: 255 if v else 0))
            return b
    return None


ORANGE = (0xEB, 0x6E, 0x4B)           # the icon's own colour


@lru_cache(maxsize=8)
def _logo(path: str, stamp: float, rows: int) -> Image.Image:
    art = Image.open(path).convert("RGBA")
    width = max(1, round(art.width * rows / art.height))
    shape = art.resize((width, rows), Image.BOX).getchannel("A").point(lambda v: 255 if v >= 118 else 0)
    out = Image.new("RGBA", (width, rows), ORANGE + (255,))   # every LED fully on or off, in its own orange
    out.putalpha(shape)
    return out


def logo(rows: int, cols: int) -> Image.Image | None:
    """OpenWeather's own icon, as supplied, ``rows`` tall; None when there's no file, or it would be wider
    than ``cols`` (left out rather than squashed)."""
    try:
        art = _logo(str(LOGO), LOGO.stat().st_mtime, rows)
    except (OSError, ValueError):
        return None
    return art if art.width <= cols else None


class Credit:
    def __init__(self, style):
        self.s = style
        self.w, self.h = style.ctx.width, style.ctx.height
        self.sky = style.backdrop(3, True)
        self.steps, self.bar = self.plan()
        self.boxes = {}   # what each screen drew where (checked by the tests): step -> [(what, x0, y0, x1, y1)]

    # -- the screens -----------------------------------------------------------------------------
    def fits(self, lines, room, art=0):
        tall = (art + 2 if art else 0) + sum(f.cap for f, _, _ in lines) + 2 * (len(lines) - 1)
        return tall <= self.h - 4 and all(f.width(t) <= room - 1 for f, t, _ in lines)

    def name_lines(self, room):
        """(picture height, lines) for the name, the biggest that fits ``room`` columns."""
        s, h = self.s, self.h
        name = lambda f: [(f, "OPEN", s.ACCENT), (f, "WEATHER", s.ACCENT)]
        by = [(s.tiny, "DATA BY", s.DIM)]
        art = 16 if h >= 48 else 12   # (the sizes OpenWeather's icon comes out clean at)
        small = [(10, name(s.tiny))] if logo(10, room - 2) else []   # (their icon only: the cloud has no 10 px drawing)
        for size, lines in ([(art, by + name(s.small))] * (h >= 48) + [(art, by + name(s.tiny)), (art, name(s.tiny))]
                            + small + [(0, name(s.tiny)), (0, [(s.tiny, "OPENWEATHER", s.ACCENT)])]):
            if self.fits(lines, room, size):
                return size, lines
        return 0, name(s.tiny)   # (nothing smaller: the smallest screens are checked to fit this)

    def plan(self):
        """[(seconds, draw)], and the bar's columns (x0, x1)."""
        w, h, s = self.w, self.h, self.s
        q = code(h - 2, w - 32)
        if q is not None:                                           # the code beside the name: one screen
            x, y = w - q.width - 1, (h - q.height) // 2
            art, lines = self.name_lines(x)
            def one(img, T, local, i):
                self.words(img, i, lines, 0, x, T, art)
                self.code(img, i, q, x, y, local)
            return [(ONE + WIPE, one)], (2, x - 3)                # (the whole code up for ONE s, after its wipe)
        art, lines = self.name_lines(w)
        def name(img, T, local, i):
            self.words(img, i, lines, 0, w, T, art)
        q = code(h, w - 22)
        label = None
        if q is not None:
            room = w - q.width - 1
            label = next((lines for lines in ([(s.tiny, "SCAN FOR", s.DIM), (s.tiny, "OPEN", s.ACCENT), (s.tiny, "WEATHER", s.ACCENT)],
                                               [(s.tiny, "SCAN", s.DIM), (s.tiny, "TO", s.DIM), (s.tiny, "VISIT", s.ACCENT)])
                          if self.fits(lines, room)), None)
        if label is not None:                                       # the name, then the code with what it's for
            x, y = w - q.width, (h - q.height) // 2
            def scan(img, T, local, i):
                self.words(img, i, label, 0, x, T)
                self.code(img, i, q, x, y, local)
            right = x - 3 if y + q.height > h - 2 else w - 3       # (a code down to the bottom row: the bar stays left of it)
            return [(NAME, name), (SCAN, scan)], (2, right)
        q = code(h - 2, w - 2)
        if q is None:                                               # too small for a code: the name is the credit
            return [(ONE, name)], (2, w - 3)
        tell_lines = next((lines for lines in ([(s.tiny, "SCAN", s.DIM), (s.tiny, "THE CODE", s.DIM), (s.tiny, "TO VISIT", s.ACCENT)],
                                               [(s.tiny, "SCAN THE", s.DIM), (s.tiny, "CODE TO", s.DIM), (s.tiny, "VISIT", s.ACCENT)],
                                               [(s.tiny, "SCAN", s.DIM), (s.tiny, "CODE TO", s.DIM), (s.tiny, "VISIT", s.ACCENT)])
                           if self.fits(lines, w)), None)
        if tell_lines is None:
            return [(ONE, name)], (2, w - 3)
        x, y = (w - q.width) // 2, (h - q.height) // 2           # the code alone, in the middle
        def tell(img, T, local, i):
            self.words(img, i, tell_lines, 0, w, T)
        def alone(img, T, local, i):
            self.code(img, i, q, x, y, local)
        right = x - 3 if y + q.height > h - 2 else w - 3
        tell.leads_on = True                                        # (the weather never comes on straight after it)
        return [(NAME, name), (TELL, tell), (SCAN, alone)], (2, right)

    def note(self, i, what, box):
        self.boxes.setdefault(i, {})[what] = box

    def words(self, img, i, lines, x0, room, T, art=0):
        """The lines in the middle of ``room`` columns from ``x0``, under the logo or the cloud (``art`` rows
        tall), which drifts to and fro and bobs; a soft highlight sweeps across the accent words every 2.5 s."""
        s = self.s
        tall = (art + 2 if art else 0) + sum(f.cap for f, _, _ in lines) + 2 * (len(lines) - 1)
        y = (self.h - 2 - tall) // 2
        if art:
            pic = logo(art, room - 2)
            if pic is not None:                                     # theirs, still: it's their mark
                at = (x0 + (room - pic.width) // 2, y + (art - pic.height) // 2)
                img.paste(pic, at, pic)
                self.note(i, "logo", (at[0], at[1], at[0] + pic.width - 1, at[1] + pic.height - 1))
            else:
                cloud = icon(3, True, art, T)
                dx = round(2 * math.sin(2 * math.pi * T / 4))     # a slow drift to and fro
                dy = round(0.6 * math.sin(2 * math.pi * T / 2))   # and a gentle bob
                at = (x0 + (room - art) // 2 + dx, y + dy)
                img.paste(cloud, at, cloud)
                self.note(i, "cloud", (at[0], at[1], at[0] + art - 1, at[1] + art - 1))
            y += art + 2
        band = (T % 2.5) / 1.2 * (room + 12) - 6                   # the highlight's column (off the words for a while)
        for n, (f, text, colour) in enumerate(lines):
            y += f.cap
            x = x0 + max(1, (room - f.width(text) + 1) // 2)
            f.draw(img, x, y, text, colour)
            self.note(i, f"text{n}", (x, y - f.cap, x + f.width(text) - 2, y))
            if colour == s.ACCENT and band - 4 < x0 + room and band + 4 > x0:
                self.shine(img, f, x, y, text, colour, x0 + band)
            y += 2

    @staticmethod
    def shine(img, font, x, y, text, colour, band):
        top = y - font.cap
        mask = Image.new("L", (font.width(text), font.cap + 1))
        font.draw(mask, 0, font.cap, text, 255)
        lo, hi = max(0, int(band) - 4 - x), min(mask.width, int(band) + 5 - x)
        px = mask.load()
        for mx in range(lo, hi):
            k = max(0.0, 1 - abs(mx + x - band) / 4) * 0.7
            lit = tuple(round(c + (255 - c) * k) for c in colour)
            for my in range(mask.height):
                if px[mx, my] and 0 <= mx + x < img.width and 0 <= my + top < img.height:
                    img.putpixel((mx + x, my + top), lit)

    def code(self, img, i, q, x, y, local):
        """The code, wiping in over its first ``WIPE`` s once it has landed (``local`` None: still sliding in)."""
        self.note(i, "code", (x, y, x + q.width - 1, y + q.height - 1))
        if local is None:
            return
        rows = q.height if local >= WIPE else round(q.height * local / WIPE)
        if rows:
            img.paste(q.crop((0, 0, q.width, rows)), (x, y))
        if rows < q.height:
            img.paste((120, 120, 120), (x, y + rows, x + q.width, y + rows + 1))   # the wipe's edge

    def draw_bar(self, img, T):
        """Fetching: a short bright piece sliding to and fro along the bottom row, over a dim track."""
        x0, x1 = self.bar
        n = x1 - x0 + 1
        if n < 6:
            return
        colour = self.s.ACCENT
        y = self.h - 1
        img.paste(tuple(c // 4 for c in colour), (x0, y, x1 + 1, y + 1))
        seg = max(3, n // 5)
        period = max(1.6, 2 * (n - seg) / 24)                      # about 24 px a second, however long the track
        p = (T % period) / period
        p = 2 * p if p < 0.5 else 2 - 2 * p                        # there and back
        p = p * p * (3 - 2 * p)                                    # eased at the ends
        start = x0 + round(p * (n - seg))
        img.paste(colour, (start, y, start + seg, y + 1))

    # -- time --------------------------------------------------------------------------------------
    @property
    def cycle(self) -> float:
        return self.steps[0][0] if len(self.steps) == 1 else sum(d for d, _ in self.steps) + SLIDE * len(self.steps)

    @property
    def minimum(self) -> float:
        """One full pass: up to the end of the first showing of the code (or of the one screen)."""
        return self.steps[0][0] if len(self.steps) == 1 else self.cycle - SLIDE

    def where(self, at: float):
        """(step, seconds since it landed) at ``at``, or (step, -progress) while it slides in."""
        if len(self.steps) == 1:
            return 0, at
        u = at % self.cycle
        for i, (d, _) in enumerate(self.steps):
            if u < d:
                return i, u
            u -= d
            if u < SLIDE:
                return (i + 1) % len(self.steps), -(u / SLIDE)
            u -= SLIDE
        return 0, 0.0

    def ends(self):
        """When each screen ends (its slide out starts), in seconds into one cycle."""
        at, out = 0.0, []
        for d, _ in self.steps:
            at += d
            out.append(at)
            at += SLIDE
        return out

    def handover(self, before: float | None, T: float) -> float | None:
        """Whether the weather may come on at ``T`` (the frame before was at ``before``): after one full pass,
        and only as a screen ends, never part-way through a slide. Returns the moment to hold the screens
        at while the weather fades in (the end of the screen just shown), or None: not yet."""
        T += 1e-6                                                   # (a frame due exactly then counts)
        if T < self.minimum:
            return None
        if len(self.steps) == 1:
            return T
        before = T - 0.1 if before is None else before + 1e-6
        for n in range(int(before // self.cycle), int(T // self.cycle) + 1):
            for i, end in enumerate(self.ends()):
                if getattr(self.steps[i][1], "leads_on", False):   # (never just after what the code is for: the code comes next)
                    continue
                at = n * self.cycle + end
                if self.minimum - 1e-6 <= at and before < at <= T:   # a screen ended since the last frame: hand over now
                    return at - 1e-6
        return None

    def frame(self, T: float, hold: float | None = None) -> Image.Image:
        """The credit ``T`` s after it came on. ``hold``: keep the screens where they were then (the bar,
        cloud and shine carry on)."""
        i, local = self.where(T if hold is None else hold)
        if local >= 0:
            img = self.sky.copy()
            self.steps[i][1](img, T, local, i)
        else:                                                       # the one before pushed out by this one
            k = -local
            k = k * k * (3 - 2 * k)
            before = self.sky.copy()
            j = (i - 1) % len(self.steps)
            self.steps[j][1](before, T, self.steps[j][0], j)
            after = self.sky.copy()
            self.steps[i][1](after, T, None, i)                       # (the code wipes in once it has landed)
            off = round(k * self.w)
            img = self.sky.copy()
            img.paste(before.crop((off, 0, self.w, self.h)), (0, 0))
            img.paste(after.crop((0, 0, off, self.h)), (self.w - off, 0))
        self.draw_bar(img, T)
        return img
