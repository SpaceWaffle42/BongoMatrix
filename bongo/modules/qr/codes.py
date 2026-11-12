"""What goes in a code, and the code itself as a picture: one LED per module, lit quiet zone."""
from __future__ import annotations

from functools import lru_cache

import segno
from PIL import Image

LIGHT, DARK = (235, 235, 235), (0, 0, 0)
MAX_VERSION = 3            # 29 modules: with a 1-module quiet zone it fills 31 of the 32 rows


def link(url: str) -> str:
    """A web address as a phone will open it: ``fadeco.uk/book`` gets ``https://`` in front."""
    url = url.strip()
    return url if "://" in url else "https://" + url


def shown(url: str) -> str:
    """The address as people read it, without ``https://``."""
    return url.strip().split("://", 1)[-1]


def _escape(text: str) -> str:
    return "".join("\\" + c if c in '\\;,:"' else c for c in text)


def wifi(network: str, password: str, security: str = "WPA") -> str:
    """Joins a Wi-Fi network when scanned (the format Android and iPhone cameras read)."""
    if security == "none":
        return f"WIFI:T:nopass;S:{_escape(network)};;"
    return f"WIFI:T:{security};S:{_escape(network)};P:{_escape(password)};;"


def number(digits: str) -> str:
    return "".join(c for c in digits if c.isdigit() or c == "+")


def call(digits: str) -> str:
    return "tel:" + number(digits)


def text(digits: str, message: str = "") -> str:
    """Opens a text message to ``digits``, with ``message`` typed in ready."""
    return f"SMSTO:{number(digits)}:{message}"


def version(payload: str) -> int:
    """The smallest QR version (1-40) that holds ``payload`` at the lowest error correction."""
    return segno.make_qr(payload, error="l", boost_error=False).version


@lru_cache(maxsize=32)
def block(payload: str, quiet: int = 2) -> Image.Image:
    """The code as an RGB image: one pixel per module, dark on a lit quiet zone ``quiet`` wide."""
    q = segno.make_qr(payload, error="l", boost_error=True)
    n = len(q.matrix) + 2 * quiet
    img = Image.new("RGB", (n, n), LIGHT)
    px = img.load()
    for y, row in enumerate(q.matrix):
        for x, dark in enumerate(row):
            if dark:
                px[x + quiet, y + quiet] = DARK
    return img


def fitted(payload: str, height: int) -> Image.Image | None:
    """The code with as wide a quiet zone (2, else 1) as fits ``height``; None if it can't fit."""
    for quiet in (2, 1):
        b = block(payload, quiet)
        if b.height <= height:
            return b
    return None


def too_long(payload: str, limit: int = MAX_VERSION) -> str | None:
    """None if ``payload`` fits in a code of version ``limit``; else a message saying by how much."""
    if version(payload) <= limit:
        return None
    capacity = {1: 17, 2: 32, 3: 53, 4: 78}[limit]
    over = len(payload.encode()) - capacity
    return (f"too long to fit as a scannable code on the panel ({len(payload.encode())} characters, about "
            f"{capacity} fit): make it {max(1, over)} characters shorter, for example with a shorter link")
