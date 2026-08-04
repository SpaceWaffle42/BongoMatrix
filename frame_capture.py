"""Software mirror of submitted RGB Matrix frames for the web preview."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw


@dataclass(frozen=True)
class Glyph:
    width: int
    height: int
    x_offset: int
    y_offset: int
    advance: int
    rows: tuple[int, ...]
    bits_per_row: int


class BdfFont:
    """The BDF glyph data used by rgbmatrix.graphics.Font."""

    def __init__(self, path):
        self.path = Path(path)
        self.glyphs = {}
        self.default_codepoint = 0
        self._load()

    def _load(self):
        lines = self.path.read_text(encoding="ascii", errors="replace").splitlines()
        current = None
        bitmap = False
        rows = []
        for line in lines:
            if line.startswith("DEFAULT_CHAR "):
                self.default_codepoint = int(line.split()[1])
            elif line.startswith("STARTCHAR "):
                current = {}
                rows = []
                bitmap = False
            elif line == "ENDCHAR" and current is not None:
                width, height, x_offset, y_offset = current["bbx"]
                bits_per_row = max(8, ((width + 7) // 8) * 8)
                self.glyphs[current["encoding"]] = Glyph(
                    width=width,
                    height=height,
                    x_offset=x_offset,
                    y_offset=y_offset,
                    advance=current["advance"],
                    rows=tuple(rows),
                    bits_per_row=bits_per_row,
                )
                current = None
                bitmap = False
            elif current is None:
                continue
            elif line.startswith("ENCODING "):
                current["encoding"] = int(line.split()[1])
            elif line.startswith("DWIDTH "):
                current["advance"] = int(line.split()[1])
            elif line.startswith("BBX "):
                current["bbx"] = tuple(map(int, line.split()[1:5]))
            elif line == "BITMAP":
                bitmap = True
            elif bitmap:
                rows.append(int(line, 16))

    def glyph(self, character):
        return self.glyphs.get(ord(character), self.glyphs.get(self.default_codepoint))

    def text_pixels(self, x, baseline, text):
        """Return text advance and exact BDF pixel coordinates."""
        cursor = int(x)
        points = []
        for character in str(text):
            glyph = self.glyph(character)
            if glyph is None:
                continue
            left = cursor + glyph.x_offset
            top = int(baseline) - glyph.y_offset - glyph.height
            for row_index, row in enumerate(glyph.rows):
                for column in range(glyph.width):
                    if row & (1 << (glyph.bits_per_row - 1 - column)):
                        points.append((left + column, top + row_index))
            cursor += glyph.advance
        return cursor - int(x), points


class FrameCapture:
    """Mirror draw operations and publish exactly the frame sent at swap()."""

    def __init__(self, width, height):
        self.width = int(width)
        self.height = int(height)
        self._lock = threading.RLock()
        self._buffers = {}
        self._latest = Image.new("RGB", (self.width, self.height), "black")
        self._revision = 0

    def register(self, canvas):
        with self._lock:
            self._buffers[id(canvas)] = Image.new("RGB", (self.width, self.height), "black")

    def _buffer(self, canvas):
        key = id(canvas)
        image = self._buffers.get(key)
        if image is None:
            image = Image.new("RGB", (self.width, self.height), "black")
            self._buffers[key] = image
        return image

    @staticmethod
    def _rgb(colour):
        if isinstance(colour, tuple):
            return tuple(int(value) for value in colour[:3])
        return (int(colour.red), int(colour.green), int(colour.blue))

    def clear(self, canvas):
        self.fill(canvas, 0, 0, 0)

    def fill(self, canvas, red, green, blue):
        with self._lock:
            self._buffer(canvas).paste((int(red), int(green), int(blue)), (0, 0, self.width, self.height))

    def pixel(self, canvas, x, y, red, green, blue):
        x, y = int(x), int(y)
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        with self._lock:
            self._buffer(canvas).putpixel((x, y), (int(red), int(green), int(blue)))

    def set_image(self, canvas, image, x=0, y=0):
        with self._lock:
            self._buffer(canvas).paste(image.convert("RGB"), (int(x), int(y)))

    def line(self, canvas, x0, y0, x1, y1, colour, width=1):
        with self._lock:
            ImageDraw.Draw(self._buffer(canvas)).line(
                (int(x0), int(y0), int(x1), int(y1)),
                fill=self._rgb(colour),
                width=max(1, int(width)),
            )

    def circle(self, canvas, x, y, radius, colour, width=1, filled=False):
        box = (x - radius, y - radius, x + radius, y + radius)
        with self._lock:
            draw = ImageDraw.Draw(self._buffer(canvas))
            if filled:
                draw.ellipse(box, fill=self._rgb(colour))
            else:
                draw.ellipse(box, outline=self._rgb(colour), width=max(1, int(width)))

    def text(self, canvas, font, x, baseline, colour, text):
        rgb = self._rgb(colour)
        advance, points = font.text_pixels(x, baseline, text)
        with self._lock:
            image = self._buffer(canvas)
            for px, py in points:
                if 0 <= px < self.width and 0 <= py < self.height:
                    image.putpixel((px, py), rgb)
        return int(x) + advance

    def publish(self, canvas):
        with self._lock:
            key = id(canvas)
            self._latest = self._buffer(canvas).copy()
            self._buffers.pop(key, None)
            self._revision += 1
            return self._revision

    def png(self):
        with self._lock:
            output = io.BytesIO()
            self._latest.save(output, format="PNG", optimize=False)
            output.seek(0)
            return output

    @property
    def revision(self):
        with self._lock:
            return self._revision
