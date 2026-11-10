"""Metaball field shared by both styles: strength = sum(r^2 / distance^2)."""
import numpy as np


def field(xs: np.ndarray, ys: np.ndarray, blobs) -> np.ndarray:
    """``xs``/``ys`` are coordinate grids; ``blobs`` is an iterable of (x, y, r)."""
    total = np.zeros(np.broadcast(xs, ys).shape)
    for bx, by, r in blobs:
        total += r * r / ((xs - bx) ** 2 + (ys - by) ** 2 + 0.01)
    return total
