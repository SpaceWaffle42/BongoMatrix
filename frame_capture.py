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
        # Screen Safe Area: (x, y, width, height) of the drawable rectangle, or
        # None for the full panel. fill/clear always cover the whole panel.
        self.viewport = None

    def _box(self):
        viewport = self.viewport
        if viewport is None:
            return 0, 0, self.width, self.height
        return viewport

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
        ox, oy, width, height = self._box()
        if not (0 <= x < width and 0 <= y < height):
            return
        with self._lock:
            self._buffer(canvas).putpixel((x + ox, y + oy), (int(red), int(green), int(blue)))

    def set_image(self, canvas, image, x=0, y=0):
        image = image.convert("RGB")
        viewport = self.viewport
        if viewport is not None:
            from safe_area import clip_image

            clipped = clip_image(image, x, y, viewport)
            if clipped is None:
                return
            image, x, y = clipped
        with self._lock:
            self._buffer(canvas).paste(image, (int(x), int(y)))

    def _inner_draw(self, canvas, draw):
        """Run a PIL draw on the inner rectangle so it is translated and clipped."""
        with self._lock:
            buffer = self._buffer(canvas)
            viewport = self.viewport
            if viewport is None:
                draw(ImageDraw.Draw(buffer))
                return
            x, y, width, height = viewport
            inner = buffer.crop((x, y, x + width, y + height))
            draw(ImageDraw.Draw(inner))
            buffer.paste(inner, (x, y))

    def line(self, canvas, x0, y0, x1, y1, colour, width=1):
        self._inner_draw(canvas, lambda draw: draw.line(
            (int(x0), int(y0), int(x1), int(y1)),
            fill=self._rgb(colour),
            width=max(1, int(width)),
        ))

    def circle(self, canvas, x, y, radius, colour, width=1, filled=False):
        box = (x - radius, y - radius, x + radius, y + radius)

        def draw_circle(draw):
            if filled:
                draw.ellipse(box, fill=self._rgb(colour))
            else:
                draw.ellipse(box, outline=self._rgb(colour), width=max(1, int(width)))

        self._inner_draw(canvas, draw_circle)

    def text(self, canvas, font, x, baseline, colour, text):
        rgb = self._rgb(colour)
        advance, points = font.text_pixels(x, baseline, text)
        ox, oy, width, height = self._box()
        with self._lock:
            image = self._buffer(canvas)
            for px, py in points:
                if 0 <= px < width and 0 <= py < height:
                    image.putpixel((px + ox, py + oy), rgb)
        return advance

    def publish(self, canvas):
        with self._lock:
            key = id(canvas)
            self._latest = self._buffer(canvas).copy()
            self._buffers.pop(key, None)
            self._revision += 1
            return self._revision

    def raw_snapshot(self, canvas=None):
        """Return immutable packed RGB bytes for a buffer or latest publication."""
        with self._lock:
            image = self._latest if canvas is None else self._buffer(canvas)
            return image.tobytes("raw", "RGB")

    def latest_rgb(self):
        with self._lock:
            return self._latest.tobytes("raw", "RGB"), self._revision

    def snapshot(self, canvas):
        """Return an isolated copy of a draw buffer without publishing it."""
        with self._lock:
            return self._buffer(canvas).copy()

    def png(self):
        return self.png_with_revision()[0]

    def png_with_revision(self):
        """Encode the latest publication and return it with its own revision."""
        with self._lock:
            output = io.BytesIO()
            self._latest.save(output, format="PNG", optimize=False)
            output.seek(0)
            return output, self._revision

    @property
    def revision(self):
        with self._lock:
            return self._revision
