"""Announcements: scheduled notices that take turns on the panel (banner, card, flash).

Each notice stays until it has been read: a long one that scrolls or pages goes all the way through
(``times_to_scroll`` times) before the next comes on, even if that is longer than ``rotate_seconds``. A
screen starts on the notice after the one the last Announcements screen was showing."""
import datetime as dt
import math

import numpy as np
from PIL import Image, ImageFilter

from bongo.fonts import middle
from bongo.module import Module, Style
from bongo.scroll import HOLD, Scrolls

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _when(text, now):
    """ISO string -> datetime comparable with ``now`` (naive values adopt now's zone if it has one)."""
    d = dt.datetime.fromisoformat(text)
    return d if d.tzinfo or now.tzinfo is None else d.replace(tzinfo=now.tzinfo)


def _clock_ok(item, now):
    start, end = item.get("from"), item.get("to")
    if not (start or end):
        return True
    t = now.time().replace(tzinfo=None)
    a = dt.time.fromisoformat(start) if start else dt.time.min
    b = dt.time.fromisoformat(end) if end else None
    if b is None:
        return t >= a
    if a == b:
        return True  # an empty range is read as "all day"
    return a <= t < b if a < b else (t >= a or t < b)


def is_active(item, now):
    """Within [start, end), on one of ``days`` and inside [from, to) (overnight ranges allowed).
    For an overnight range the weekday is that of the day the window began."""
    if item.get("start") and now < _when(item["start"], now):
        return False
    if item.get("end") and now >= _when(item["end"], now):
        return False
    if not _clock_ok(item, now):
        return False
    days = item.get("days")
    if days:
        day = now
        a, b = item.get("from"), item.get("to")
        if a and b and a > b and now.time().replace(tzinfo=None) < dt.time.fromisoformat(b):
            day = now - dt.timedelta(days=1)
        return DAYS[day.weekday()] in [d.lower()[:3] for d in days]
    return True


def active(items, now):
    return [i for i in items if is_active(i, now)]


def wrap(text, font, width, lines=0):
    """Word-wrap into ``lines`` lines (2 = balanced split with the narrowest widest line; 0 = greedy)."""
    words = text.split() or [""]
    if lines == 1 or len(words) < 2:
        return [" ".join(words)]
    if lines == 2:
        cuts = [(" ".join(words[:c]), " ".join(words[c:])) for c in range(1, len(words))]
        return list(min(cuts, key=lambda p: max(map(font.width, p))))
    out = [words[0]]
    for w in words[1:]:
        if font.width(f"{out[-1]} {w}") <= width:
            out[-1] += f" {w}"
        else:
            out.append(w)
    return out


def layout(text, fonts, width, height, max_lines):
    """First (font, lines) whose wrapped text fits width x height, trying 1..max_lines lines; else None."""
    for n in range(1, max_lines + 1):  # n >= 3 means greedy wrap
        for f in fonts:
            lines = wrap(text, f, width, n if n < 3 else 0)
            if all(f.width(l) <= width for l in lines) and len(lines) * (f.cap + f.descent + 1) - 1 <= height:
                return f, lines
    return None


class Base(Scrolls):
    def setup(self):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        top, bot = np.array(rgb(s["background_top"]), float), np.array(rgb(s["background_bottom"]), float)
        rows = np.linspace(0, 1, h)[:, None, None]
        self.background = Image.fromarray(((1 - rows) * top + rows * bot).repeat(w, axis=1).astype(np.uint8), "RGB")
        self.ink, self.dark = rgb(s["text_colour"]), rgb(s["background_bottom"])
        self.F = {n: self.ctx.fonts(n) for n in ("10x20", "9x18B", "7x13B", "6x13B", "6x10", "5x7", "tom-thumb")}

    def fonts(self, *names):
        return [self.F[n] for n in names]

    def reads(self, item) -> float:
        """Seconds for this look to show ``item`` all the way through once (0: it fits, nothing moves)."""
        return 0.0

    def span(self, item) -> float:
        """A notice's turn: ``rotate_seconds``, or longer if it takes longer to read ``times_to_scroll`` times."""
        return max(self.settings["rotate_seconds"], 1, self.reads(item) * max(1, int(self.settings["times_to_scroll"])))

    def pick(self, now):
        """(item, seconds into its turn) for ``now``, or (None, 0). Notices take turns from when this screen
        came on, starting after the one the last screen was showing; the screen stays until it's read."""
        items = active(self.settings["items"], now)
        if not items:
            return None, 0.0
        return self.turns(("announcements",), items, now, self.span, name=lambda i: (i.get("title"), i.get("text")))

    def accent(self, item):
        return rgb(item.get("accent") or self.settings["accent"])

    def paint(self, items, img=None):
        """Draw (font, x, baseline, text, ink) items over the gradient with a soft dark halo behind them."""
        img = img or self.background.copy()
        mask = Image.new("L", img.size)
        for f, x, y, t, _ in items:
            f.draw(mask, x, y, t, 255)
        mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1)).point(lambda v: int(v * .75))
        img = Image.composite(Image.new("RGB", img.size, self.dark), img, mask)
        for f, x, y, t, ink in items:
            f.draw(img, x, y, t, ink)
        return img

    GAP = 16   # px between a scrolling line and its repeat

    def scroll(self, font, text, y, ink, t, speed=20):
        """A line: centred and still if it fits; otherwise from its first letter, held, then round at ``speed``."""
        w, tw = self.ctx.width, font.width(text)
        if tw <= w - 2:
            return [(font, (w - tw) // 2, y, text, ink)]
        off = int(max(0.0, t - HOLD) * speed) % (tw + self.GAP)
        return [(font, 1 - off, y, text, ink), (font, 1 - off + tw + self.GAP, y, text, ink)]

    def scrolls(self, font, text, speed) -> float:
        """Seconds for ``scroll`` to show ``text`` all the way through once (0: it fits)."""
        tw = font.width(text)
        return 0.0 if tw <= self.ctx.width - 2 else HOLD + (tw + self.GAP) / max(1, speed)

    def centred(self, font, lines, top, ink):
        w, pitch = self.ctx.width, font.cap + font.descent + 1
        return [(font, (w - font.width(l)) // 2, top + font.cap + i * pitch, l, ink) for i, l in enumerate(lines)]

    def idle(self, now):
        text, w, h = self.settings["idle_text"], self.ctx.width, self.ctx.height
        dim = tuple(int(c * .4) for c in self.ink)
        text = text or "NO NOTICES"
        fit = layout(text, self.fonts("5x7", "tom-thumb"), w - 2, h - 2, 3)
        if not fit:
            f = self.F["tom-thumb"]
            t = self.scrolled(("idle", text), now, self.scrolls(f, text, 20))
            return self.paint(self.scroll(f, text, middle(f, h), dim, t))
        f, lines = fit
        return self.paint(self.centred(f, lines, middle(f, h, len(lines)) - f.cap, dim))

    def render(self, now):
        item, t = self.pick(now)
        return self.idle(now) if item is None else self.draw(item, t, now)


class Banner(Base, Style):
    fps = 20
    defaults = {"scroll_speed": 20}

    def faces(self, item):
        """(title font, body font, title bar height) for this notice on this screen."""
        h, title = self.ctx.height, item.get("title", "")
        bold = self.F["6x10" if h >= 28 else "5x7"]
        bar = bold.cap + 3 if title else 0
        body = next((f for f in self.fonts("10x20", "9x18B", "7x13B", "6x10", "5x7", "tom-thumb")
                     if f.cap + f.descent + 2 <= h - bar), self.F["tom-thumb"])
        return bold, body, bar

    def reads(self, item):
        bold, body, _ = self.faces(item)
        title = item.get("title", "")
        return max(self.scrolls(body, item.get("text", ""), self.settings["scroll_speed"]), self.scrolls(bold, title, 14))

    def draw(self, item, t, now):
        w, h, title = self.ctx.width, self.ctx.height, item.get("title", "")
        acc = self.accent(item)
        bold, body, bar = self.faces(item)
        area = h - bar
        img = self.paint(self.scroll(body, item.get("text", ""), bar + middle(body, area), self.ink, t,
                                     self.settings["scroll_speed"]))
        if title:  # lit title on a dim tinted bar with an accent rule; drawn last so no halo dims it
            img.paste(tuple(c // 5 for c in acc), (0, 0, w, bar - 1))
            img.paste(acc, (0, bar - 1, w, bar))
            for f, x, y, text, _ in self.scroll(bold, title, 1 + bold.cap, acc, t, 14):
                f.draw(img, x, y, text, acc)
        return img


class Card(Base, Style):
    fps = 5

    def reads(self, item):
        w, h, title = self.ctx.width, self.ctx.height, item.get("title", "")
        small = self.F["5x7"]
        head = (small.cap + 3) if title else 0
        text, fonts, room = item.get("text", ""), self.fonts("6x10", "5x7", "tom-thumb"), h - head - 1
        pages = 0.0
        if not (layout(text, fonts, w - 2, room, 2) or layout(text, fonts, w - 2, room, 3)):
            font = self.F["tom-thumb"]
            rows = max(1, room // (font.cap + font.descent + 1))
            pages = 3.0 * -(-len(wrap(text, font, w - 2)) // rows)          # 3 s a page
        return max(pages, self.scrolls(small, title, 14))

    def draw(self, item, t, now):
        w, h, title = self.ctx.width, self.ctx.height, item.get("title", "")
        small, acc = self.F["5x7"], self.accent(item)
        head = (small.cap + 3) if title else 0
        text, fonts, room = item.get("text", ""), self.fonts("6x10", "5x7", "tom-thumb"), h - head - 1
        fit = layout(text, fonts, w - 2, room, 2) or layout(text, fonts, w - 2, room, 3)
        if fit:
            font, lines = fit
        else:  # last resort: tiny font, page through the lines every 3 s instead of clipping
            font = self.F["tom-thumb"]
            lines = wrap(text, font, w - 2)
            rows = max(1, room // (font.cap + font.descent + 1))
            page = int(t // 3) % -(-len(lines) // rows)
            lines = lines[page * rows:(page + 1) * rows]
        block = head + len(lines) * (font.cap + font.descent + 1) - 1
        top = max(0, (h - block) // 2)
        items = self.centred(font, lines, top + head, self.ink)
        if title:
            items += self.centred(small, [title], top, acc) if small.width(title) <= w - 2 else \
                self.scroll(small, title, top + small.cap, acc, t, 14)
        return self.paint(items)


class Flash(Base, Style):
    fps = 15

    def reads(self, item):
        w, h, title = self.ctx.width, self.ctx.height, item.get("title") or item.get("text", "")
        fonts = self.fonts("7x13B", "6x13B", "6x10", "5x7", "tom-thumb")
        return 0.0 if layout(title, fonts, w - 2, h - 2, 2) else self.scrolls(self.F["5x7"], title, 20)

    def draw(self, item, t, now):
        w, h, title = self.ctx.width, self.ctx.height, item.get("title") or item.get("text", "")
        k = 0.5 + 0.5 * math.sin(now.timestamp() * 2 * math.pi / 3)  # 3 s breathing cycle
        bg = tuple(int(c * (.15 + .3 * k)) for c in self.accent(item))
        img = Image.new("RGB", (w, h), bg)
        fonts = self.fonts("7x13B", "6x13B", "6x10", "5x7", "tom-thumb")
        fit = layout(title, fonts, w - 2, h - 2, 2)
        if fit:
            f, lines = fit
            items = self.centred(f, lines, middle(f, h, len(lines)) - f.cap, self.ink)
        else:
            f = self.F["5x7"]
            items = self.scroll(f, title, middle(f, h), self.ink, t, 20)
        for f, x, y, txt, ink in items:
            f.draw(img, x, y, txt, ink, outline=tuple(c // 3 for c in bg))
        return img


class Ribbon(Base, Style):
    """Overlay: the current notice crawls along a slim band over whatever module is playing.
    Fully transparent when nothing is scheduled, so it can stay switched on all day.
    It runs across the screens under it, so its turns go by the clock (not from when a screen came on);
    each notice's turn is long enough for it to crawl all the way across."""
    fps = 20
    defaults = {"scroll_speed": 18, "position": "bottom"}
    choices = {"position": ["bottom", "top"]}

    def reads(self, item):
        w, h = self.ctx.width, self.ctx.height
        font = self.F["5x7"] if h >= 24 else self.F["tom-thumb"]
        title, text = item.get("title", ""), item.get("text", "")
        total = (font.width(title.upper() + "  ") if title else 0) + font.width(text) + w // 2
        return (total + w) / max(1, self.settings["scroll_speed"])

    def ribbon_pick(self, now):
        items = active(self.settings["items"], now)
        if not items:
            return None, 0.0
        spans = [self.span(i) for i in items]
        t = now.timestamp() % sum(spans)
        for item, span in zip(items, spans):
            if t < span:
                return item, t
            t -= span
        return items[-1], 0.0

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        item, t = self.ribbon_pick(now)
        if item is None:
            return img
        font = self.F["5x7"] if h >= 24 else self.F["tom-thumb"]
        band = font.cap + 4
        y0 = h - band if self.settings["position"] == "bottom" else 0
        acc = self.accent(item)
        img.paste(self.dark + (240,), (0, y0, w, y0 + band))
        rule = y0 if self.settings["position"] == "bottom" else y0 + band - 1
        img.paste(acc + (255,), (0, rule, w, rule + 1))
        title, text = item.get("title", ""), item.get("text", "")
        parts = [(title.upper() + "  ", acc)] if title else []
        parts.append((text, self.ink))
        total = sum(font.width(p) for p, _ in parts) + w // 2
        x = w - int(t * self.settings["scroll_speed"]) % (total + w)
        base = y0 + (2 if self.settings["position"] == "bottom" else 1) + font.cap
        for part, ink in parts:
            font.draw(img, x, base, part, ink + (255,))
            x += font.width(part)
        return img


MODULE = Module(
    id="announcements", name="Announcements",
    description="Scheduled notices that rotate: Banner scrolls the text, Card fits it, Flash pulses a title.",
    styles={"banner": Banner, "card": Card, "flash": Flash},
    overlays={"ribbon": Ribbon},
    defaults=dict(
        items=[{"title": "Welcome", "text": "Notices you add on the dashboard appear here", "start": None,
                "end": None, "days": None, "from": None, "to": None, "accent": None}],
        rotate_seconds=8, times_to_scroll=1, idle_text="", text_colour="#FFF4DE", accent="#FFB547",
        background_top="#14161A", background_bottom="#08090A",
    ),
    basic=("items",),
    limits={"rotate_seconds": (1, 3600), "scroll_speed": (1, 200), "times_to_scroll": (1, 10)},
    item_fields={"items": [
        {"name": "title", "type": "text", "label": "Title", "placeholder": "Bin day"},
        {"name": "text", "type": "longtext", "label": "Message", "placeholder": "Green bin goes out tonight"},
        {"name": "start", "type": "date", "label": "Show from", "more": True},
        {"name": "end", "type": "date", "label": "Show until (not including)", "more": True},
        {"name": "days", "type": "days", "label": "Only on these days", "more": True},
        {"name": "from", "type": "time", "label": "From time", "more": True},
        {"name": "to", "type": "time", "label": "Until time", "more": True},
        {"name": "accent", "type": "colour", "label": "Own accent colour", "more": True},
    ]},
)
