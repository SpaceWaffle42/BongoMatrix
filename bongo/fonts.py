"""Pixel-exact BDF fonts drawn with Pillow (the same .bdf files the panel used in v2)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image

FONT_DIR = Path(os.environ.get("BONGO_FONTS", Path(__file__).resolve().parent.parent / "resources" / "fonts"))


class BdfFont:
    def __init__(self, path: Path):
        self.glyphs: dict[int, tuple[int, int, int, Image.Image | None]] = {}
        self.ascent = 0
        with open(path, encoding="latin-1") as f:
            lines = iter(f.read().splitlines())
        for line in lines:
            key, _, rest = line.partition(" ")
            if key == "FONT_ASCENT":
                self.ascent = int(rest)
            elif key == "STARTCHAR":
                self._read_glyph(lines)

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
                rows = [next(lines) for _ in range(h)]
                mask = None
                if w and h:
                    mask = Image.new("1", (w, h))
                    for y, row in enumerate(rows):
                        bits, width = int(row, 16), len(row) * 4
                        for x in range(w):
                            if bits >> (width - 1 - x) & 1:
                                mask.putpixel((x, y), 1)
                self.glyphs[code] = (dwidth, x_off, y_off + h, mask)
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
        return _load(self.directory / f"{name}.bdf")


@lru_cache(maxsize=None)
def _load(path: Path) -> BdfFont:
    return BdfFont(path)


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
