"""The base of every Ambient style: a small simulation stepped at a steady rate by real time.

A style sets ``rate`` (steps a second at speed 1), and writes ``init`` (make the world),
``step`` (move it on by one step) and ``draw`` (an H x W x 3 float array, 0-255). Drawing
fills the whole canvas, whatever size the safe area is.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from bongo.module import Style

MAX_CATCH_UP = 6   # steps one frame may take to catch up after a stall (a slow frame never snowballs)


class Sim(Style):
    fps = 20
    rate = 20.0
    defaults = {"speed": 1.0}
    basic = ("speed",)

    def setup(self):
        self.W, self.H = self.ctx.width, self.ctx.height
        self.r = np.random.default_rng(self.ctx.rng.getrandbits(32))
        self.xs, self.ys = np.meshgrid(np.arange(self.W, dtype=float), np.arange(self.H, dtype=float))
        self.started = None
        self.done = 0
        self.init()

    def init(self) -> None:
        pass

    def step(self) -> None:
        pass

    def draw(self) -> np.ndarray:
        raise NotImplementedError

    def render(self, now):
        if self.started is None:
            self.started = now
            self.step()
            self.done = 1
        else:
            due = int((now - self.started).total_seconds() * self.rate * float(self.settings["speed"])) + 1
            if due - self.done > MAX_CATCH_UP:
                self.done = due - MAX_CATCH_UP
            while self.done < due:
                self.step()
                self.done += 1
        return Image.fromarray(np.clip(self.draw(), 0, 255).astype(np.uint8), "RGB")


def hsv(h, s, v) -> np.ndarray:
    """HSV (each 0-1, arrays or numbers) to RGB 0-1, stacked on a last axis."""
    h, s, v = np.broadcast_arrays(np.asarray(h, float) % 1 * 6, np.asarray(s, float), np.asarray(v, float))
    i = np.floor(h).astype(int) % 6
    f = h - np.floor(h)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], -1)


def blob(xs, ys, x, y, r2) -> np.ndarray:
    """A soft round glow centred on (x, y), 1 at the centre."""
    return np.exp(-((xs - x) ** 2 + (ys - y) ** 2) / r2)


def gradient(h: int, top, bottom) -> np.ndarray:
    """An H x 1 x 3 sky from ``top`` to ``bottom``."""
    k = np.linspace(0, 1, h)[:, None, None]
    return np.array(top, float) * (1 - k) + np.array(bottom, float) * k
