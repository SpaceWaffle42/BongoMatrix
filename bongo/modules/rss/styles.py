"""Four looks of the same stories. Headlines are drawn in ``headline_colour`` and story text in
``summary_colour`` everywhere, so the two never blur together."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from bongo.fonts import middle
from bongo.module import Style
from bongo.scroll import HOLD, Scrolls

from . import data


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def wrap(font, text, width):
    """Lines no wider than ``width``; a word longer than the line is broken with a hyphen ("Environ-" / "ment")."""
    lines = [""]
    for word in text.split():
        while font.width(word) > width:
            cut = max(1, len(word) - 1)   # (at least a letter a line, however narrow: it always finishes)
            while cut > 1 and font.width(word[:cut] + "-") > width:
                cut -= 1
            if len(word) - cut < 3 and cut - (3 - (len(word) - cut)) >= 3:
                cut -= 3 - (len(word) - cut)   # no one- or two-letter tail ("residen-" / "ts"): "resid-" / "ents"
            if lines[-1]:
                lines.append("")
            lines[-1], word = (word[:cut] + "-" if font.width(word[:cut] + "-") <= width else word[:cut]), word[cut:]
            lines.append("")
        trial = f"{lines[-1]} {word}".strip()
        if font.width(trial) <= width:
            lines[-1] = trial
        else:
            lines.append(word)
    return [l for l in lines if l]


def first_sentence(text: str, limit: int = 160) -> str:
    """The opening of the story, for one-line styles."""
    for stop in (". ", "! ", "? "):
        if stop in text[:limit]:
            return text[:text.index(stop, 0, limit) + 1]
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


READ = 0.07      # s to read a letter at the "slow" text speed: how long a line of story text stays before the next comes up
LINE = 1.2       # s each new line stays at least (at "slow")
MOVE = 0.4       # s for the text to move up a line
# text_speed (Story and Picture): how quickly the story text moves up, apart from the headline's scroll_speed:
# 1 (slowest) to 100 (fastest), with named points along the way. Each line's time to be read is READ's and
# LINE's times its pace ("slow": the pace the text had when its headline fitted); between the named points
# the pace changes evenly (each step the same share faster). The first lines always get HOLD at least.
TEXT_SPEEDS = {"slow": 25, "normal": 50, "fast": 75, "faster": 100}     # the named points (the dashboard's marks)
PACES = ((1, 2.0), (25, 1.0), (50, 0.7), (75, 0.5), (100, 0.35))      # (text_speed, pace) to interpolate between


def pace(speed) -> float:
    """How much longer (over 1) or shorter (under 1) than the "slow" pace each line of text stays at ``speed``."""
    speed = TEXT_SPEEDS.get(speed, speed) if isinstance(speed, str) else speed   # (a name saved before it was a number)
    try:
        speed = min(100.0, max(1.0, float(speed)))
    except (TypeError, ValueError):
        speed = TEXT_SPEEDS["normal"]
    for (a, pa), (b, pb) in zip(PACES, PACES[1:]):
        if speed <= b:
            return pa * (pb / pa) ** ((speed - a) / (b - a))
    return PACES[-1][1]


def pace_of(settings) -> float:
    return pace(settings.get("text_speed", TEXT_SPEEDS["normal"]))


def crawl(lines, rows, start=0.0, pace=1.0):
    """When text taller than its window moves up, a line at a time: [(when the move to line k+1 starts)],
    and when the last line has had its time. The first ``rows`` lines are given time to be read first,
    then each line that comes up its own (``READ`` a letter, ``LINE`` at least), times ``pace``."""
    first = start + max(HOLD, sum(len(l) for l in lines[:rows]) * READ * pace)
    moves, t = [], first
    for line in lines[rows:]:
        moves.append(t)
        t += MOVE + max(LINE, len(line) * READ) * pace
    return moves, t


def climbed(moves, t, pitch) -> int:
    """Pixels the text has moved up at ``t`` (each move eased in and out)."""
    done = sum(1 for m in moves if m + MOVE <= t)
    going = next((m for m in moves if m <= t < m + MOVE), None)
    part = 0.0
    if going is not None:
        p = (t - going) / MOVE
        part = p * p * (3 - 2 * p)
    return round((done + part) * pitch)


def glided(moves, t, pitch) -> int:
    """Pixels the text has moved up at ``t`` when it glides steadily instead of a line at a time: it leaves
    when the first move would and is all the way up when the last would be, so it takes the same time."""
    if not moves:
        return 0
    start, end = moves[0], moves[-1] + MOVE
    return round(len(moves) * pitch * min(1.0, max(0.0, (t - start) / (end - start))))


def age(now, published) -> str:
    if published is None:
        return ""
    minutes = int((now.timestamp() - published.timestamp()) // 60)
    if minutes < 1:
        return "NOW"
    return f"{minutes}M" if minutes < 60 else f"{minutes // 60}H" if minutes < 1440 else f"{minutes // 1440}D"


class _News(Scrolls, Style):
    fps = 20

    def setup(self):
        s = self.settings
        self.accent, self.text, self.body = rgb(s["accent"]), rgb(s["headline_colour"]), rgb(s["summary_colour"])
        self.bg_top, self.bg_bottom = rgb(s["background_top"]), rgb(s["background_bottom"])
        self.tiny = self.ctx.fonts("tom-thumb")
        self.sizes = [self.ctx.fonts(n) for n in ("10x20", "9x18B", "7x13B", "6x10", "5x7", "tom-thumb")]
        t = np.linspace(0, 1, self.ctx.height)[:, None, None]
        ramp = np.array(self.bg_top) * (1 - t) + np.array(self.bg_bottom) * t
        self.backdrop = Image.fromarray(np.repeat(ramp, self.ctx.width, axis=1).round().astype("uint8"), "RGB")

    def render(self, now):
        items = data.latest(self.settings)
        if items is not None:
            self.since = None
            return self.draw(items, now)
        trouble = data.trouble(self.settings)
        if trouble:   # nothing to show and nothing coming: say why, briefly, and let the next screen on
            self.since = getattr(self, "since", None) or now
            self.finished = (now - self.since).total_seconds() >= self.TROUBLE_SECONDS
        return self.waiting(now, trouble)

    TROUBLE_SECONDS = 5
    # what to say, and the font to say it in, biggest first. The small fonts' W reads as an H ("NEHS"),
    # so where they're needed the wording has no W in it. After the two-line wordings come one-line ones, for a
    # canvas too low for two lines; on one too small even for those, nothing is written (a clean frame, never
    # cut-off letters).
    SAYS = {"none": ((("ADD A", "NEWS FEED"), "6x10"), (("ADD A", "FEED"), "5x7"), (("ADD A", "FEED"), "tom-thumb"),
                     (("ADD A FEED",), "tom-thumb"), (("NO FEED",), "tom-thumb")),
            "offline": ((("NEWS", "OFFLINE"), "6x10"), (("FEED", "OFFLINE"), "5x7"), (("FEED", "OFFLINE"), "tom-thumb"),
                        (("OFFLINE",), "5x7"), (("OFFLINE",), "tom-thumb")),
            # the feeds have stories, and their word filters leave none of them
            "filtered": ((("FILTERS", "HIDE ALL"), "6x10"), (("FILTERS", "HIDE ALL"), "5x7"), (("FILTERS", "HIDE ALL"), "tom-thumb"),
                         (("ALL", "HIDDEN"), "tom-thumb"), (("ALL HIDDEN",), "5x7"), (("ALL HIDDEN",), "tom-thumb"),
                         (("HIDDEN",), "5x7"), (("HIDDEN",), "tom-thumb"))}
    LOADING = (("NEWS", "6x10"), ("FEED", "5x7"), ("FEED", "tom-thumb"))   # the same rule while it's fetching

    def summary(self, item) -> str:
        return item.summary if self.settings["show_summary"] else ""

    def pick(self, items, now, seconds):
        """(item, seconds into it): stories take turns, each for ``seconds(item)``. A screen starts on the
        story after the one the last news screen was showing, and stays until the one it's on is read."""
        return self.turns(("news", tuple(data.filters.entries(self.settings["feeds"]))), items, now, seconds, name=lambda i: i.title)

    def lines(self, font, text, width):
        """Wrapped lines, cached: spans are worked out every frame."""
        cache = self.__dict__.setdefault("_wrapped", {})
        key = (id(font), text, width)
        if key not in cache:
            if len(cache) > 256:
                cache.clear()
            cache[key] = wrap(font, text, width)
        return cache[key]

    def waiting(self, now, trouble=None):
        """No stories yet: NEWS... while they're fetched; what to do about it when they can't be."""
        if trouble:
            img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
            # the first wording whose font leaves two clear columns each side and a clear row above and below
            lines, font = next(((lines, f) for lines, name in self.SAYS[trouble] for f in (self.ctx.fonts(name),)
                                if max(f.width(line) for line in lines) <= w - 3 and len(lines) * (f.cap + 3) - 1 <= h),
                               ((), self.tiny))                 # (none fits: say nothing)
            base = middle(font, h, lines=2, gap=3 - font.descent) if len(lines) == 2 else (h - font.cap) // 2 + font.cap   # (capitals: no tails to leave room for)
            for i, line in enumerate(lines):
                font.draw(img, (w - font.width(line) + 1) // 2, base + i * (font.cap + 3), line,   # (+1: a glyph ends in a blank column)
                          self.accent if i == 0 else self.text)
            return img
        # still fetching: the word, centred and bright enough to read across a room, over three dots that
        # fill in turn. (It was "NEWS..." in one font: dim, pushed about by its own dots, cut off at 32 wide.)
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        word, font = next(((word, f) for word, name in self.LOADING for f in (self.ctx.fonts(name),)
                           if f.width(word) <= w - 3 and f.cap + 6 <= h), (self.LOADING[-1][0], self.tiny))
        base = (h - (font.cap + 5)) // 2 + font.cap
        font.draw(img, (w - font.width(word) + 1) // 2, base, word, mix(self.bg_top, self.accent, 0.9))
        lit = int(now.timestamp() * 2) % 4
        for i in range(3):
            x, y = w // 2 - 5 + i * 4, base + 3
            img.paste(self.accent if i < lit else mix(self.bg_top, self.accent, 0.3), (x, y, x + 2, y + 2))
        return img


class Ticker(_News):
    """Source tag on top, the current headline scrolling right-to-left in the biggest font that fits."""

    GAP = "  -  "

    def parts(self, item):
        tail = first_sentence(self.summary(item))
        return item.title, (self.GAP + tail) if tail else ""

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        top = self.tiny.cap + 3  # below the tag pill
        font = next((f for f in self.sizes if f.cap + f.descent <= h - top), self.tiny)
        speed = max(1, self.settings["scroll_speed"])

        def seconds(i):  # from its first letter, held, until its last has gone off on the left
            head, tail = self.parts(i)
            return HOLD + (font.width(head) + font.width(tail) + 2) / speed
        item, t = self.pick(items, now, seconds)
        head, tail = self.parts(item)
        x, base = 1 - round(max(0.0, t - HOLD) * speed), top + middle(font, h - top)
        font.draw(img, x, base, head, self.text)
        if tail:
            font.draw(img, x + font.width(head), base, tail, self.body)
        tag = (item.source or "NEWS").upper()
        room = min(self.tiny.width(tag), w - 4)                  # a source too long for the pill scrolls inside it
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, room + 2, self.tiny.cap + 1], fill=mix(self.bg_top, self.accent, 0.22))
        self.line(img, self.tiny, 2, self.tiny.cap, room, tag, self.accent, now, key=("tag", item.title))
        return img


class Headline(_News):
    """One headline at a time, wrapped to fit, under a tiny source and age line."""
    defaults = {"scroll_speed": 16}

    def layout(self, text, width, height):
        """(font, lines, gap) for the largest font where the wrapped text fits, else None."""
        for font in self.sizes[3:]:
            if font.width(text) <= width:
                lines = [text]
            else:
                lines = self.balanced(font, text, width) or (self.greedy(font, text, width, 3) if font is self.tiny else None)
            for gap in (1, 0):
                if lines and len(lines) * (font.cap + font.descent) + (len(lines) - 1) * gap <= height:
                    return font, lines, gap
        return None

    @staticmethod
    def balanced(font, text, width):
        words, best = text.split(), None
        for i in range(1, len(words)):
            pair = [" ".join(words[:i]), " ".join(words[i:])]
            size = max(font.width(p) for p in pair)
            if size <= width and (best is None or size < best[0]):
                best = size, pair
        return best[1] if best else None

    @staticmethod
    def greedy(font, text, width, limit):
        lines = [""]
        for word in text.split():
            trial = f"{lines[-1]} {word}".strip()
            if font.width(trial) <= width:
                lines[-1] = trial
            elif lines[-1]:
                lines.append(word)
            else:
                return None
        return lines if len(lines) <= limit and all(font.width(l) <= width for l in lines) else None

    def card_seconds(self, item):
        """A story's turn: ``rotate_seconds``, or longer when its headline has to scroll across on one line."""
        w, h = self.ctx.width, self.ctx.height
        top = self.tiny.cap + 3
        if self.layout(item.title, w - 2, h - top):
            return self.settings["rotate_seconds"]
        font = next((f for f in self.sizes[3:] if f.cap + f.descent <= h - top), self.tiny)
        return max(self.settings["rotate_seconds"], HOLD + (font.width(item.title) + 16) / max(1, self.settings["scroll_speed"]))

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        item, t = self.pick(items, now, self.card_seconds)
        stamp = age(now, item.published)
        edge = self.tiny.width(stamp)
        self.line(img, self.tiny, 1, self.tiny.cap, w - edge - 5, (item.source or "NEWS").upper(),
                  mix(self.bg_top, self.accent, 0.75), now, key=("source", item.title))
        self.tiny.draw(img, w - edge - 1, self.tiny.cap, stamp, mix(self.bg_top, self.text, 0.45))
        top = self.tiny.cap + 3
        fitted = self.layout(item.title, w - 2, h - top)
        if fitted:
            font, lines, gap = fitted
            base = top + middle(font, h - top, len(lines), gap)
            for n, line in enumerate(lines):
                font.draw(img, 1, base + n * (font.cap + font.descent + gap), line, self.text)
        else:  # too little room to wrap: scroll it on one line instead
            font = next((f for f in self.sizes[3:] if f.cap + f.descent <= h - top), self.tiny)
            period = font.width(item.title) + 16                       # from its first letter, held, then round
            x = 1 - int(max(0.0, t - HOLD) * self.settings["scroll_speed"]) % period
            for k in (0, 1):
                font.draw(img, x + k * period, top + middle(font, h - top), item.title, self.text)
        return img


class Picture(_News):
    """The story's thumbnail on the left; its age and headline on the right, crawling upwards
    when it's longer than the space. Stories without a picture get a tinted tile instead."""
    defaults = {"rotate_seconds": 10, "text_speed": 50}   # (its text moves a line at a time: no scroll speed)

    def tile(self, item, side):
        thumb = data.image(item.image)
        if thumb is not None:
            k = max(side / thumb.width, side / thumb.height)
            size = (max(side, round(thumb.width * k)), max(side, round(thumb.height * k)))
            big = thumb.resize(size, Image.LANCZOS)
            x, y = (size[0] - side) // 2, (size[1] - side) // 2
            return big.crop((x, y, x + side, y + side))
        img = Image.new("RGB", (side, side), mix(self.bg_bottom, self.accent, 0.18))
        letter = (item.source or "N")[:1].upper()
        font = next((f for f in self.sizes if f.width(letter) <= side - 4 and f.cap <= side - 4), self.tiny)
        font.draw(img, (side - font.width(letter)) // 2, middle(font, side), letter, mix(self.bg_bottom, self.accent, 0.7))
        return img

    def column(self, item, room):
        """(font, [(line, colour)]) for the text column: headline lines, a gap, story lines."""
        small = self.ctx.fonts("5x7")  # 5x7 unless a word would have to be broken; then tom-thumb
        text = f"{item.title} {self.summary(item)}"
        font = small if all(small.width(word) <= room for word in text.split()) else self.tiny
        rows = [(l, self.text) for l in self.lines(font, item.title, room)]
        if self.summary(item):
            rows += [("", None)] + [(l, self.body) for l in self.lines(font, self.summary(item), room)]
        return font, rows

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        side = min(h, round(w * 0.42))
        room, space = w - side - 3, h - self.tiny.cap - 2

        def plan(i):   # (font, rows, pitch, rows that fit, moves, when it's all been read)
            font, rows = self.column(i, room)
            pitch = font.cap + font.descent + 1
            fit = max(1, space // pitch)
            pace = pace_of(self.settings)
            moves, read = (crawl([line for line, _ in rows], fit, pace=pace) if len(rows) > fit
                           else ([], max(HOLD, sum(len(l) for l, _ in rows) * READ * pace)))
            return font, rows, pitch, fit, moves, read

        def seconds(i):  # a long story stays up until its last line has been read
            return max(self.settings["rotate_seconds"], plan(i)[5] + HOLD)
        item, elapsed = self.pick(items, now, seconds)
        img.paste(self.tile(item, side), (0, (h - side) // 2))
        x0 = side + 2
        room = w - x0 - 1
        stamp = age(now, item.published) or (item.source or "NEWS").upper()
        self.line(img, self.tiny, x0, self.tiny.cap, room, stamp, self.accent, now, key=("stamp", item.title))
        top = h - space
        font, rows, pitch, fit, moves, _ = plan(item)
        offset = climbed(moves, elapsed, pitch)
        area = img.crop((x0, top, x0 + room, top + fit * pitch))   # whole lines only: none half cut off at rest
        for n, (line, colour) in enumerate(rows):
            if line:
                font.draw(area, 0, font.cap + n * pitch - offset, line, colour)
        img.paste(area, (x0, top))
        return img


class Story(Headline):
    """v2's layout: the headline on a row of its own, and the story text below it in its own colour.
    Both start together, each at its own speed: the headline, if it's too wide, slides across once (held at
    its first letter first) at ``scroll_speed``; the text moves up a line at a time at ``text_speed``, each
    line given time to be read. The story stays up until both are done."""
    # stop_at_each_line: the text moves up a line and stops while it's read (off: it glides up steadily,
    # taking the same time overall, and stops once the last lines are up)
    defaults = {"scroll_speed": 12, "text_speed": 50, "stop_at_each_line": True}
    basic = ("text_speed", "stop_at_each_line")   # (shown with the main settings, not under More)
    GAP = 16    # pixels between a sliding headline and its repeat

    def setup(self):
        super().setup()
        self.head = self.ctx.fonts("6x10") if self.ctx.height >= 20 else self.ctx.fonts("5x7")
        self.small = self.ctx.fonts("5x7") if self.ctx.width >= 40 else self.tiny
        self.row = self.head.cap + self.head.descent + 1
        self.pitch = self.small.cap + self.small.descent + 1
        self.fit = max(1, (self.ctx.height - self.row - 1) // self.pitch)   # whole lines of text below the headline

    def slide(self, item) -> float:
        """Seconds for a too-wide headline to go across once (0: it fits)."""
        wide = self.head.width(item.title) > self.ctx.width - 2
        return (self.head.width(item.title) + self.GAP) / max(1, self.settings["scroll_speed"]) if wide else 0.0

    def plan(self, item):
        """(text lines, moves, when the last line has been read)."""
        text = self.lines(self.small, self.summary(item), self.ctx.width - 2)
        pace = pace_of(self.settings)   # (its own: a long headline sliding across doesn't slow the text down)
        if len(text) <= self.fit:
            return text, [], max(HOLD, sum(len(l) for l in text) * READ * pace)
        return (text, *crawl(text, self.fit, pace=pace))

    def seconds(self, item):
        slide = self.slide(item)
        return max(self.settings["rotate_seconds"], (HOLD + slide + 1.0) if slide else 0.0, self.plan(item)[2] + HOLD)

    def draw(self, items, now):
        if not any(self.summary(i) for i in items):  # nothing but headlines: the card shows them best
            return super().draw(items, now)
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        item, t = self.pick(items, now, self.seconds)
        speed = max(1, self.settings["scroll_speed"])
        slide = self.slide(item)
        # headline row: held at its first letter, slides across once, seamlessly, then rests at its start
        head = img.crop((0, 0, w, self.row))
        x = 1 - (round((t - HOLD) * speed) if HOLD < t < HOLD + slide else 0)
        self.head.draw(head, x, self.head.cap, item.title, self.text)
        if x < 1:
            self.head.draw(head, x + self.head.width(item.title) + self.GAP, self.head.cap, item.title, self.text)
        img.paste(head, (0, 0))
        ImageDraw.Draw(img).line([(0, self.row - 1), (w - 1, self.row - 1)], fill=mix(self.bg_top, self.accent, 0.35))
        # story text, at the same time: a line at a time, whole lines only
        text, moves, _ = self.plan(item)
        if not text:
            return img
        top = self.row + 1
        area = img.crop((0, top, w, min(h, top + self.fit * self.pitch)))
        offset = climbed(moves, t, self.pitch) if self.settings["stop_at_each_line"] else glided(moves, t, self.pitch)
        for n, line in enumerate(text):
            self.small.draw(area, 1, self.small.cap + n * self.pitch - offset, line, self.body)
        img.paste(area, (0, top))
        return img
