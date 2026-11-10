"""Two looks of the same headlines: a scrolling ticker and a rotating headline card."""
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
        self.accent, self.text = rgb(s["accent"]), rgb(s["text_colour"])
        self.bg_top, self.bg_bottom = rgb(s["background_top"]), rgb(s["background_bottom"])
        self.tiny = self.ctx.fonts("tom-thumb")
        self.sizes = [self.ctx.fonts(n) for n in ("10x20", "9x18B", "7x13B", "6x10", "5x7", "tom-thumb")]
        t = np.linspace(0, 1, self.ctx.height)[:, None, None]
        ramp = np.array(self.bg_top) * (1 - t) + np.array(self.bg_bottom) * t
        self.backdrop = Image.fromarray(np.repeat(ramp, self.ctx.width, axis=1).round().astype("uint8"), "RGB")

    def render(self, now):
        items = data.latest(self.settings)
        return self.waiting(now) if items is None else self.draw(items, now)

    def waiting(self, now):
        img, font = self.backdrop.copy(), self.ctx.fonts("6x10")
        x = max(0, (self.ctx.width - font.width("NEWS...")) // 2)
        colour = mix(self.bg_top, self.accent, 0.45)
        base = middle(font, self.ctx.height)
        font.draw(img, x, base, "NEWS", colour)
        font.draw(img, x + font.width("NEWS"), base, "." * (int(now.timestamp() * 2) % 4), colour)
        return img


class Ticker(_News):
    """Source tag on top, the current headline scrolling right-to-left in the biggest font that fits."""

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        top = self.tiny.cap + 3  # below the tag pill
        font = next((f for f in self.sizes if f.cap + f.descent <= h - top), self.tiny)
        speed = max(1, self.settings["scroll_speed"])
        spans = [(w + font.width(i.title) + 8) / speed for i in items]  # seconds from entering to gone
        t = now.timestamp() % sum(spans)
        index = 0
        while t >= spans[index]:
            t -= spans[index]
            index += 1
        item = items[index]
        font.draw(img, w - round(t * speed), top + middle(font, h - top), item.title, self.text)
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

    def draw(self, items, now):
        img, w, h = self.backdrop.copy(), self.ctx.width, self.ctx.height
        item = items[int(now.timestamp() // max(1, self.settings["rotate_seconds"])) % len(items)]
        side = min(h, round(w * 0.42))
        img.paste(self.tile(item, side), (0, (h - side) // 2))
        x0 = side + 2
        room = w - x0 - 1
        stamp = age(now, item.published) or (item.source or "NEWS").upper()
        self.tiny.draw(img, x0, self.tiny.cap, fit(self.tiny, stamp, room), self.accent)
        top = self.tiny.cap + 2
        small = self.ctx.fonts("5x7")  # 5x7 unless a word would have to be broken; then tom-thumb
        font = small if all(small.width(word) <= room for word in item.title.split()) else self.tiny
        lines = self.wrap(font, item.title, room)
        pitch = font.cap + font.descent + 1
        height = len(lines) * pitch
        space = h - top
        offset = 0
        if height > space:  # crawl up: hold 1.5 s, move, hold at the end
            travel = height - space
            elapsed = now.timestamp() % max(1, self.settings["rotate_seconds"])
            offset = int(min(travel, max(0.0, (elapsed - 1.5) * self.settings["scroll_speed"])))
        text = Image.new("L", (room, space))
        for n, line in enumerate(lines):
            font.draw(text, 0, font.cap + n * pitch - offset, line, 255)
        img.paste(self.text, (x0, top, x0 + room, h), text)
        return img

    @staticmethod
    def wrap(font, text, width):
        lines = [""]
        for word in text.split():
            while font.width(word) > width:  # a word longer than the column: hard-break it
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
