"""The Bongo Matrix logo, as pictures for the dashboard's pages.

The logo is made of the splash wall's blocks (3 x 3, dark spot in the middle). There are two: the normal one,
"Round wall", and a simple one, "Stack", for small sizes. Each also comes built from blocks of one colour,
as a badge that tells one Matrix from another on the hub. All of them are Animator projects in
``resources/animator`` (``logo``, ``logo_simple``, ``logo_blue``, ``logo_simple_blue``, ...), loaded as sprites.
"""
from __future__ import annotations

import io
import zlib

from PIL import Image

from bongo import sprites

STYLES = {"normal": "logo", "simple": "logo_simple"}
COLOURS = ("blue", "red", "tan", "pink", "yellow", "green")
SIZES = (16, 32, 48, 64, 96, 128, 180, 192, 256, 512)
TILE = (11, 12, 14, 255)   # the dashboard's page colour, behind a home-screen icon
_made: dict[tuple, tuple[object, bytes]] = {}


def badge_colour(chosen, ident: str) -> str:
    """A Matrix's badge colour: the one picked in Settings, else one that follows from its own id (so each
    keeps its colour; with six colours two Matrices can match, and Settings is where to change one)."""
    return chosen if chosen in COLOURS else COLOURS[zlib.crc32(ident.encode()) % len(COLOURS)]


def png(style: str = "normal", colour: str | None = None, size: int = 32, tile: bool = False) -> bytes:
    """The logo as a PNG about ``size`` pixels across, every block a whole number of pixels. ``colour``: the
    one-colour badge. ``tile``: on a dark square with a margin, for a phone's home screen."""
    name = STYLES.get(style, "logo") + (f"_{colour}" if colour in COLOURS else "")
    size = min(SIZES, key=lambda v: abs(v - int(size)))   # one of a few sizes, so only a few pictures are ever kept
    sprite = sprites.load(name, own=False)   # never a project someone saved as "logo": this is on open pages
    key = (name, size, tile)
    if key not in _made or _made[key][0] is not sprite:   # drawn once (again if the file changes)
        mark = sprite.frame(0)
        scale = max(1, (size * (3 if tile else 4)) // (max(mark.size) * 4))   # a tile keeps a quarter of its width as margin
        mark = mark.resize((mark.width * scale, mark.height * scale), Image.Resampling.NEAREST)
        if tile:
            img = Image.new("RGBA", (size, size), TILE)
            img.alpha_composite(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
        else:
            img = mark
        out = io.BytesIO()
        img.save(out, "PNG")
        _made[key] = (sprite, out.getvalue())
    return _made[key][1]


def svg(style: str = "normal") -> str:
    """The logo as an SVG, a square per pixel: ``bongo/web/static/logo.svg`` is this, for a copy of the pages
    with no Matrix behind it (the ?demo dashboard). A test keeps the file in step with the drawing."""
    mark = sprites.load(STYLES.get(style, "logo"), own=False).frame(0)
    paths: dict[str, str] = {}
    for y in range(mark.height):
        for x in range(mark.width):
            r, g, b, a = mark.getpixel((x, y))
            if a:
                paths[f"#{r:02x}{g:02x}{b:02x}"] = paths.get(f"#{r:02x}{g:02x}{b:02x}", "") + f"M{x} {y}h1v1h-1z"
    body = "".join(f'<path fill="{c}" d="{d}"/>' for c, d in sorted(paths.items()))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {mark.width} {mark.height}" '
            f'shape-rendering="crispEdges">{body}</svg>\n')
