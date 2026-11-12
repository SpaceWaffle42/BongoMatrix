"""Spotify: what's playing now, with its cover. Cover, Backdrop and Vinyl looks, and a Ticker overlay
that slides a slim strip up over whatever else is on. When you pause, the art goes grey; after a
while (or straight away when nothing is playing) the screen hands over to the next one.

Sign in once in Settings > Spotify (see account.py for how, and why it works that way)."""
from __future__ import annotations

import math
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from bongo.module import Module, Style

from . import account

GREY, DIM = (140, 140, 150), (45, 45, 50)


def accent(art: Image.Image | None) -> tuple:
    """The cover's liveliest colour, brightened: for the artist line and the progress bar."""
    if art is None:
        return (30, 215, 96)
    a = np.asarray(art.resize((8, 8))).reshape(-1, 3).astype(float)
    c = a[(a.max(1) - a.min(1) + a.max(1) * 0.3).argmax()]
    return tuple(int(v) for v in c / max(c.max(), 1) * 255)


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


class Base(Style):
    fps = 20
    SPEED = 14                                               # px a second long titles scroll

    def setup(self):
        self.small, self.mid = self.ctx.fonts("tom-thumb"), self.ctx.fonts("5x7")
        self.since = None                                    # when the music stopped (for handing over)
        self.started = None
        self.last_url = None
        self.changed = 0.0                                   # when the song changed (titles restart, covers slide)

    def reading(self, now):
        t = time.time()
        if self.started is None:
            self.started = t
        track, art = account.now_playing()
        if track and track.art_url != self.last_url:
            self.last_url, self.changed = track.art_url, t
        quiet = track is None or not track.playing
        self.since = (self.since or t) if quiet else None
        wait = 3 if track is None else self.settings["hand_over_after"]
        if not account.connected() or (quiet and t - self.since >= wait):
            self.finished = t - self.started > 3             # always on for a moment, so it doesn't flicker past
        return track, art, t

    def ticker(self, img, font, x0, x1, baseline, text, t, colour):
        """``text`` between x0 and x1, scrolling (after a pause) when it's too long."""
        w = font.width(text)
        layer = Image.new("RGB", (x1 - x0, img.height))
        if w <= x1 - x0:
            font.draw(layer, 0, baseline, text, colour)
        else:
            off = -int((max(0.0, t - self.changed - 1.2) * self.SPEED) % (w + 12))
            for k in (0, 1):
                font.draw(layer, off + k * (w + 12), baseline, text, colour)
        mask = Image.fromarray((np.asarray(layer).max(2) > 0).astype(np.uint8) * 255)
        img.paste(layer, (x0, 0), mask)

    def bar(self, d, x0, x1, y, frac, colour):
        d.line([x0, y, x1, y], fill=DIM)
        if frac > 0:
            d.line([x0, y, x0 + int((x1 - x0) * min(1.0, frac)), y], fill=colour)

    def pause_icon(self, d, x, y, paused, colour):
        if paused:
            d.rectangle([x, y, x, y + 4], fill=colour)
            d.rectangle([x + 2, y, x + 2, y + 4], fill=colour)
        else:
            for i in range(3):
                d.line([x + i, y + i, x + i, y + 4 - i], fill=colour)

    def message(self, text, colour=GREY):
        img = self.ctx.canvas()
        w, h = img.size
        lines = text.split("\n")
        y = (h - len(lines) * 7) // 2 + 6
        for line in lines:
            self.small.draw(img, (w - self.small.width(line)) // 2, y, line, colour)
            y += 7
        return img

    def render(self, now):
        track, art, t = self.reading(now)
        if not account.connected():
            return self.message("SPOTIFY\nSIGN IN IN\nSETTINGS", (30, 215, 96))
        if track is None:
            return self.message("NOTHING\nPLAYING")
        if art is None:
            art = Image.new("RGB", (64, 64), (40, 40, 46))
        if not track.playing:
            art = Image.fromarray((np.asarray(art.convert("L").convert("RGB")) * 0.45).astype(np.uint8))
        return self.draw(track, art, t)


class Cover(Base):
    """Cover: the album art on the left; title and artist (scrolling if long), the time and a
    progress bar in the art's colour on the right."""

    def draw(self, track, art, t):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas()
        size = min(h, w // 2)
        img.paste(art.resize((size, size), Image.LANCZOS), (0, (h - size) // 2))
        x0 = size + 2
        col = accent(art) if track.playing else GREY
        self.ticker(img, self.mid, x0, w, 8, track.title, t, (255, 255, 255) if track.playing else (150, 150, 150))
        self.ticker(img, self.small, x0, w, 16, track.artist, t, col)
        d = ImageDraw.Draw(img)
        if h >= 30:
            self.pause_icon(d, x0 + 1, 21, not track.playing, (200, 200, 200))
            self.small.draw(img, x0 + 7, 26, mmss(track.position(t)) if track.playing else "PAUSED", GREY)
            if self.settings["show_progress"]:
                self.bar(d, x0, w - 2, h - 2, track.position(t) / (track.duration_ms / 1000), col)
        return img


class Backdrop(Base):
    """Backdrop: the cover, blurred and dimmed, fills the panel behind a small sharp copy and the
    title and artist."""

    def draw(self, track, art, t):
        w, h = self.ctx.width, self.ctx.height
        drift = int(6 * math.sin(t / 3))
        big = art.resize((w, w), Image.LANCZOS)
        top = max(0, min(w - h, (w - h) // 2 + drift))
        bg = big.crop((0, top, w, top + h)).filter(ImageFilter.GaussianBlur(3))
        img = Image.fromarray((np.asarray(bg) * 0.35).astype(np.uint8))
        d = ImageDraw.Draw(img)
        thumb = min(22, h - 8)
        img.paste(art.resize((thumb, thumb), Image.LANCZOS), (3, 3))
        d.rectangle([2, 2, thumb + 3, thumb + 3], outline=(0, 0, 0))
        x0 = thumb + 6
        col = accent(art) if track.playing else GREY
        self.ticker(img, self.mid, x0, w, 10, track.title, t, (255, 255, 255))
        self.ticker(img, self.small, x0, w, 19, track.artist, t, col)
        if h >= 30:
            self.small.draw(img, x0, 26, mmss(track.position(t)) if track.playing else "PAUSED", (190, 180, 210))
            if self.settings["show_progress"]:
                self.bar(d, 3, w - 4, h - 3, track.position(t) / (track.duration_ms / 1000), col)
        return img


class Vinyl(Base):
    """Vinyl: a record spinning, with the cover as its label (it stops when you pause)."""

    def draw(self, track, art, t):
        w, h = self.ctx.width, self.ctx.height
        r = min(h, w // 2) / 2 - 0.5
        spin = (t - self.changed) * 3.5 if track.playing else 0.0
        size = int(2 * r + 1)
        a = np.zeros((size, size, 3)) + 8
        y, x = np.mgrid[0:size, 0:size]
        rr = np.hypot(x - r, y - r)
        ang = np.arctan2(y - r, x - r) - spin
        disc = rr <= r
        groove = 18 + 14 * (np.sin(rr * 2.6) > 0.3) + 160 * np.clip(np.cos(ang * 2) - 0.75, 0, 1)
        a[disc] = np.stack([groove] * 3, -1)[disc]
        ls = max(6, int(r * 0.9))
        label = np.asarray(art.resize((ls, ls), Image.LANCZOS).rotate(-math.degrees(spin), Image.BICUBIC), float)
        ly, lx = np.mgrid[0:ls, 0:ls]
        inside = np.hypot(lx - (ls - 1) / 2, ly - (ls - 1) / 2) <= (ls - 1) / 2
        o = int(r - (ls - 1) / 2)
        sub = a[o:o + ls, o:o + ls]
        sub[inside] = label[inside]
        c = int(r)
        a[c:c + 2, c:c + 2] = 8                              # the spindle hole
        img = self.ctx.canvas((8, 8, 10))
        img.paste(Image.fromarray(a.clip(0, 255).astype(np.uint8)), (0, (h - size) // 2))
        x0 = size + 2
        col = accent(art) if track.playing else GREY
        self.ticker(img, self.mid, x0, w, 9, track.title, t, (255, 255, 255))
        self.ticker(img, self.small, x0, w, 17, track.artist, t, col)
        if h >= 30:
            self.small.draw(img, x0, 25, mmss(track.position(t)) if track.playing else "PAUSED", GREY)
            if self.settings["show_progress"]:
                self.bar(ImageDraw.Draw(img), x0, w - 2, h - 3, track.position(t) / (track.duration_ms / 1000), col)
        return img


class Ticker(Base):
    """Overlay: while music plays, a slim strip slides up over whatever else is on, with a tiny
    cover, the song and the artist; it slides away when the music stops."""
    defaults = {"position": "bottom"}
    choices = {"position": ["bottom", "top"]}

    def setup(self):
        super().setup()
        self.shown = 0.0                                     # 0 hidden .. 1 fully up

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        track, art, t = self.reading(now)
        self.finished = False                                # an overlay never ends an entry
        want = 1.0 if track and track.playing else 0.0
        self.shown += max(-0.12, min(0.12, want - self.shown))
        if self.shown <= 0 or track is None:
            return img
        band = 9
        k = 1 - (1 - self.shown) ** 2
        top = h - int(band * k) if self.settings["position"] == "bottom" else int(band * k) - band
        strip = Image.new("RGB", (w, band))
        if art is not None:
            strip.paste(art.resize((band - 2, band - 2), Image.LANCZOS), (1, 1))
        self.ticker(strip, self.small, band, w - 1, 6, f"{track.title} - {track.artist}", t, (255, 255, 255))
        sd = ImageDraw.Draw(strip)
        self.bar(sd, 0, w - 1, 0 if self.settings["position"] == "bottom" else band - 1,
                 track.position(t) / (track.duration_ms / 1000), accent(art))
        img.paste(strip.convert("RGBA"), (0, top))
        return img


def check(settings: dict) -> None:
    if not 0 <= float(settings["hand_over_after"]) <= 3600:
        raise ValueError("hand_over_after: seconds, 0 to 3600")


MODULE = Module(
    id="spotify",
    name="Spotify",
    description="What's playing on Spotify: the cover, title and artist. Hands over to the next screen "
                "when nothing is playing. Sign in once in Settings > Spotify.",
    styles={"cover": Cover, "backdrop": Backdrop, "vinyl": Vinyl},
    overlays={"ticker": Ticker},
    defaults={"hand_over_after": 60, "show_progress": True},
    basic=("hand_over_after",),
    check=check,
)
