"""Pixel-exact BDF fonts drawn with Pillow (the same .bdf files the panel used in v2)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image

FONT_DIR = Path(os.environ.get("BONGO_FONTS", Path(__file__).resolve().parent.parent / "resources" / "fonts"))

# Fonts removed on 25 Nov 2026 because their licences aren't MIT or public domain, and the kept font that
# takes each one's place: the nearest in size, so a screen's layout hardly moves. A saved setting that
# names one is changed to its replacement when the config loads (config.tidy), and a font asked for by
# its old name (an imported module, say) gets the replacement too.
REPLACED = {
    "clR6x12": "6x12",     # the same 6 x 12 cell, with the same 7-row capitals
    "helvR12": "6x13",     # 9-row capitals, about as wide
    "texgyre-27": "10x20",  # the biggest font left
}


def renamed(value):
    """``value`` (settings, a playlist, a whole config sent by a page) with every removed font named in a font
    setting changed to its replacement: a page left open through the update still holds the old names."""
    if isinstance(value, dict):
        return {k: (REPLACED.get(v, v) if isinstance(v, str) and "font" in str(k) else renamed(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [renamed(v) for v in value]
    return value


class Glyphs(dict):
    """A font's glyphs by character code: (advance, x offset, rows above the baseline, mask). A glyph's
    mask is only made the first time it is asked for: the big fonts hold thousands of characters, and
    making them all took half a second of the panel's time on first use."""

    def __missing__(self, code):
        raise KeyError(code)

    def __getitem__(self, code):
        glyph = dict.__getitem__(self, code)
        if len(glyph) == 6:
            dwidth, x_off, top, w, h, rows = glyph
            mask = None
            if w and h:   # a BDF row is the pixels as hex, most significant bit first: Pillow's own "1" layout
                n = (w + 7) // 8 * 2
                mask = Image.frombytes("1", (w, h), bytes.fromhex("".join(row[:n].ljust(n, "0") for row in rows)))
            glyph = (dwidth, x_off, top, mask)
            dict.__setitem__(self, code, glyph)
        return glyph

    def get(self, code, default=None):
        return self[code] if code in self else default


class BdfFont:
    def __init__(self, path: Path):
        self.glyphs: dict[int, tuple[int, int, int, Image.Image | None]] = Glyphs()
        self.ascent = 0
        with open(path, encoding="latin-1") as f:
            lines = iter(f.read().splitlines())
        for line in lines:
            if line.startswith("STARTCHAR"):
                self._read_glyph(lines)
            elif line.startswith("FONT_ASCENT"):
                self.ascent = int(line.split()[1])

    def _read_glyph(self, lines):
        code = dwidth = 0
        bbx = (0, 0, 0, 0)
        for line in lines:
            key, _, rest = line.partition(" ")
            if key == "ENCODING":
                code = int(rest.split()[0])
            elif key == "DWIDTH":
                dwidth = int(rest.split()[0])
            elif key == "BBX":
                bbx = tuple(int(v) for v in rest.split())
            elif key == "BITMAP":
                w, h, x_off, y_off = bbx
                dict.__setitem__(self.glyphs, code, (dwidth, x_off, y_off + h, w, h, [next(lines) for _ in range(h)]))
            elif key == "ENDCHAR":
                return

    @property
    def cap(self) -> int:
        """Pixels from the baseline to the top of a capital (the 'H')."""
        glyph = self.glyphs.get(ord("H"))
        return glyph[2] if glyph else self.ascent

    @property
    def descent(self) -> int:
        """Pixels a descender (the 'g') drops below the baseline."""
        glyph = self.glyphs.get(ord("g"))
        return max(0, glyph[3].height - glyph[2]) if glyph and glyph[3] else 0

    def width(self, text: str) -> int:
        return sum(self.glyphs.get(ord(c), self.glyphs.get(ord("?"), (0,)))[0] for c in text)

    def draw(self, img: Image.Image, x: int, baseline: int, text: str, fill, outline=None) -> int:
        """Draw text with its baseline at ``baseline``; returns the advance width."""
        if outline is not None:
            for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
                self._draw(img, x + dx, baseline + dy, text, outline)
        return self._draw(img, x, baseline, text, fill)

    def _draw(self, img, x, baseline, text, fill):
        start = x
        for c in text:
            dwidth, x_off, top, mask = self.glyphs.get(ord(c), self.glyphs.get(ord("?"), (0, 0, 0, None)))
            if mask is not None:
                img.paste(fill, (x + x_off, baseline - top), mask)
            x += dwidth
        return x - start


class Fonts:
    def __init__(self, directory: Path = FONT_DIR):
        self.directory = Path(directory)

    def __call__(self, name: str) -> BdfFont:
        path = self.directory / f"{name}.bdf"
        if name in REPLACED and not path.is_file():
            path = self.directory / f"{REPLACED[name]}.bdf"
        return _load(path)


@lru_cache(maxsize=None)
def _load(path: Path) -> BdfFont:
    return BdfFont(path)


def names(directory: Path = FONT_DIR) -> list[str]:
    """Every font a setting may name, for the dashboard's font pickers."""
    return sorted(p.stem for p in Path(directory).glob("*.bdf"))


def table(font: BdfFont) -> dict:
    """A font as plain data for the Animator's text tool: the printable ASCII and Latin-1 characters it
    has, each as [advance, x offset, rows above the baseline, width, [a row's pixels as bits]]."""
    glyphs = {}
    for code in [*range(32, 127), *range(160, 256)]:
        if code not in font.glyphs:
            continue
        dwidth, x_off, top, mask = font.glyphs[code]
        rows = [] if mask is None else [int("".join("1" if mask.getpixel((x, y)) else "0" for x in range(mask.width)), 2)
                                        for y in range(mask.height)]
        glyphs[chr(code)] = [dwidth, x_off, top, mask.width if mask else 0, rows]
    return {"cap": font.cap, "descent": font.descent, "glyphs": glyphs}


def middle(font: BdfFont, height: int, lines: int = 1, gap: int = 1) -> int:
    """Baseline of the first line so ``lines`` lines of ``font`` sit centred in ``height``."""
    pitch = font.cap + font.descent + gap
    return (height - (lines * pitch - gap)) // 2 + font.cap


def align_x(alignment: str, text_width: int, canvas_width: int, margin: int = 2) -> int:
    if alignment == "left":
        return margin
    if alignment == "right":
        return max(0, canvas_width - text_width - margin)
    return max(0, (canvas_width - text_width) // 2)
