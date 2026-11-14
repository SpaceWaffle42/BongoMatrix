"""Four looks of the same stories. Headlines are drawn in ``headline_colour`` and story text in
``summary_colour`` everywhere, so the two never blur together."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from bongo.fonts import middle
from bongo.module import Style

from . import data


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def fit(font, text: str, width: int) -> str:
    while text and font.width(text) > width:
        text = text[:-1]
    return text


def wrap(font, text, width):
    """Lines no wider than ``width``; a word longer than the line is hard-broken."""
    lines = [""]
    for word in text.split():
        while font.width(word) > width:
            cut = len(word)
            while cut > 1 and font.width(word[:cut]) > width:
                cut -= 1
            if lines[-1]:
                lines.append("")
            lines[-1], word = word[:cut], word[cut:]
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


def age(now, published) -> str:
    if published is None:
        return ""
    minutes = int((now.timestamp() - published.timestamp()) // 60)
    if minutes < 1:
        return "NOW"
    return f"{minutes}M" if minutes < 60 else f"{minutes // 60}H" if minutes < 1440 else f"{minutes // 1440}D"


class _News(Style):
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
    # so where they're needed the wording has no W in it.
    SAYS = {"none": ((("ADD A", "NEWS FEED"), "6x10"), (("ADD A", "FEED"), "5x7"), (("ADD A", "FEED"), "tom-thumb")),
            "offline": ((("NEWS", "OFFLINE"), "6x10"), (("FEED", "OFFLINE"), "5x7"), (("FEED", "OFFLINE"), "tom-thumb"))}
    LOADING = (("NEWS", "6x10"), ("FEED", "5x7"), ("FEED", "tom-thumb"))   # the same rule while it's fetching

    def summary(self, item) -> str:
        return item.summary if self.settings["show_summary"] else ""

    @staticmethod
    def pick(items, now, seconds):
        """(item, seconds into it): stories take turns, each for ``seconds(item)``."""
        spans = [seconds(i) for i in items]
        t = now.timestamp() % sum(spans)
        for item, span in zip(items, spans):
            if t < span:
                return item, t
            t -= span
        return items[-1], 0.0

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
                                if max(f.width(line) for line in lines) <= w - 3 and 2 * f.cap + 5 <= h),
                               (self.SAYS[trouble][-1][0], self.tiny))
            base = middle(font, h, lines=2, gap=3 - font.descent)
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

    def line(self, font, item):
        tail = first_sentence(self.summary(item))
        return item.title, (self.GAP + tail) if tail else ""

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        top = self.tiny.cap + 3  # below the tag pill
        font = next((f for f in self.sizes if f.cap + f.descent <= h - top), self.tiny)
        speed = max(1, self.settings["scroll_speed"])

        def seconds(i):  # from entering on the right to gone on the left
            head, tail = self.line(font, i)
            return (w + font.width(head) + font.width(tail) + 8) / speed
        item, t = self.pick(items, now, seconds)
        head, tail = self.line(font, item)
        x, base = w - round(t * speed), top + middle(font, h - top)
        font.draw(img, x, base, head, self.text)
        if tail:
            font.draw(img, x + font.width(head), base, tail, self.body)
        tag = fit(self.tiny, (item.source or "NEWS").upper(), w - 3)
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, self.tiny.width(tag) + 2, self.tiny.cap + 1], fill=mix(self.bg_top, self.accent, 0.22))
        self.tiny.draw(img, 2, self.tiny.cap, tag, self.accent)
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

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        item = items[int(now.timestamp() // max(1, self.settings["rotate_seconds"])) % len(items)]
        stamp = age(now, item.published)
        edge = self.tiny.width(stamp)
        self.tiny.draw(img, 1, self.tiny.cap, fit(self.tiny, (item.source or "NEWS").upper(), w - edge - 5),
                       mix(self.bg_top, self.accent, 0.75))
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
            period = font.width(item.title) + w
            x = w - int(now.timestamp() * self.settings["scroll_speed"]) % period
            font.draw(img, x, top + middle(font, h - top), item.title, self.text)
        return img


class Picture(_News):
    """The story's thumbnail on the left; its age and headline on the right, crawling upwards
    when it's longer than the space. Stories without a picture get a tinted tile instead."""
    defaults = {"rotate_seconds": 10, "scroll_speed": 6}

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

    HOLD = 1.5  # seconds still at the start and at the end of a crawl

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
        speed = max(1, self.settings["scroll_speed"])

        def seconds(i):  # long stories stay up until they've crawled all the way
            font, rows = self.column(i, room)
            travel = max(0, len(rows) * (font.cap + font.descent + 1) - space)
            return max(self.settings["rotate_seconds"], travel / speed + 2 * self.HOLD) if travel else self.settings["rotate_seconds"]
        item, elapsed = self.pick(items, now, seconds)
        img.paste(self.tile(item, side), (0, (h - side) // 2))
        x0 = side + 2
        room = w - x0 - 1
        stamp = age(now, item.published) or (item.source or "NEWS").upper()
        self.tiny.draw(img, x0, self.tiny.cap, fit(self.tiny, stamp, room), self.accent)
        top = h - space
        font, rows = self.column(item, room)
        pitch = font.cap + font.descent + 1
        travel = max(0, len(rows) * pitch - space)
        offset = int(min(travel, max(0.0, (elapsed - self.HOLD) * speed))) if travel else 0
        area = img.crop((x0, top, x0 + room, h))  # draw clipped to the column
        for n, (line, colour) in enumerate(rows):
            if line:
                font.draw(area, 0, font.cap + n * pitch - offset, line, colour)
        img.paste(area, (x0, top))
        return img


class Story(Headline):
    """v2's layout: the headline on a row of its own (sliding across once if it's too wide), and the
    story text below it in its own colour, crawling up once the headline has been read."""
    defaults = {"scroll_speed": 12}
    HOLD = 2.0  # seconds before the text starts to move, and at its end
    GAP = 16    # pixels between a sliding headline and its repeat

    def setup(self):
        super().setup()
        self.head = self.ctx.fonts("6x10") if self.ctx.height >= 20 else self.ctx.fonts("5x7")
        self.small = self.ctx.fonts("5x7") if self.ctx.width >= 40 else self.tiny
        self.row = self.head.cap + self.head.descent + 1

    def timing(self, item):
        """(headline slide seconds, text travel in pixels)."""
        w, speed = self.ctx.width, max(1, self.settings["scroll_speed"])
        wide = self.head.width(item.title) > w - 2
        slide = (self.head.width(item.title) + self.GAP) / speed if wide else 0.0
        text = self.lines(self.small, self.summary(item), w - 2)
        pitch = self.small.cap + self.small.descent + 1
        return slide, max(0, len(text) * pitch - (self.ctx.height - self.row - 1))

    def seconds(self, item):
        slide, travel = self.timing(item)
        speed = max(1, self.settings["scroll_speed"])
        return max(self.settings["rotate_seconds"], max(slide, self.HOLD) + travel / speed + self.HOLD)

    def draw(self, items, now):
        if not any(self.summary(i) for i in items):  # nothing but headlines: the card shows them best
            return super().draw(items, now)
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        item, t = self.pick(items, now, self.seconds)
        speed = max(1, self.settings["scroll_speed"])
        slide, travel = self.timing(item)
        # headline row: slides across once, seamlessly, then rests at its start
        head = img.crop((0, 0, w, self.row))
        x = 1 - (round(t * speed) if t < slide else 0)
        self.head.draw(head, x, self.head.cap, item.title, self.text)
        if x < 1:
            self.head.draw(head, x + self.head.width(item.title) + self.GAP, self.head.cap, item.title, self.text)
        img.paste(head, (0, 0))
        ImageDraw.Draw(img).line([(0, self.row - 1), (w - 1, self.row - 1)], fill=mix(self.bg_top, self.accent, 0.35))
        # story text: waits until the headline has been read, then crawls up
        text = self.summary(item)
        if not text:
            return img
        area = img.crop((0, self.row + 1, w, h))
        pitch = self.small.cap + self.small.descent + 1
        start = max(slide, self.HOLD)
        offset = int(min(travel, max(0.0, (t - start) * speed)))
        for n, line in enumerate(self.lines(self.small, text, w - 2)):
            self.small.draw(area, 1, self.small.cap + n * pitch - offset, line, self.body)
        img.paste(area, (0, self.row + 1))
        return img
