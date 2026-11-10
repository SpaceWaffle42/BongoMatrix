"""Shared drawing helpers for the Sky styles: colours, gradients, anti-aliased glows."""
from __future__ import annotations

import datetime as dt

import numpy as np
from PIL import Image


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def local(now: dt.datetime) -> dt.datetime:
    """Aware version of ``now``: naive times are UTC."""
    return now.replace(tzinfo=dt.timezone.utc) if now.tzinfo is None else now


def gradient(w: int, h: int, top, bottom) -> np.ndarray:
    f = np.linspace(0, 1, h)[:, None, None]
    row = np.array(top, float) * (1 - f) + np.array(bottom, float) * f
    return np.repeat(row, w, axis=1)


def add_blob(arr: np.ndarray, cx: float, cy: float, radius: float, colour, glow: float = 0.0, power: float = 0.5):
    """Add an anti-aliased disc (analytic coverage) with a soft halo of ``glow`` px onto a float array."""
    h, w, _ = arr.shape
    ys, xs = np.mgrid[0:h, 0:w]
    d = np.hypot(xs + 0.5 - cx, ys + 0.5 - cy)
    cov = np.clip(radius + 0.5 - d, 0, 1)[..., None]
    if glow > 0:
        halo = (power * np.exp(-(np.maximum(d - radius, 0) / glow) ** 2))[..., None]
        arr += halo * np.array(colour, float)
    arr[:] = arr * (1 - cov) + np.array(colour, float) * cov


def to_image(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(arr + 0.5, 0, 255).astype("uint8"), "RGB")
