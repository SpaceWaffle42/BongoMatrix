"""Capture-only Matrix adapter and shared preview frame seams."""

from __future__ import annotations

import math

from frame_capture import FrameCapture


class _Canvas:
    pass


class CaptureMatrix:
    """Matrix primitive adapter backed by the production framebuffer mirror."""

    def __init__(self, width=64, height=32):
        self.width = int(width)
        self.height = int(height)
        self.canvas = _Canvas()
        self._capture = FrameCapture(self.width, self.height)
        self._capture.register(self.canvas)

    def clear(self, canvas=None):
        self._capture.clear(canvas or self.canvas)

    def fill(self, red, green, blue, canvas=None):
        self._capture.fill(canvas or self.canvas, red, green, blue)

    def pixel(self, x, y, red, green, blue, canvas=None):
        self._capture.pixel(canvas or self.canvas, x, y, red, green, blue)

    def draw_line(self, x0, y0, x1, y1, colour, width=1, canvas=None):
        self._capture.line(canvas or self.canvas, x0, y0, x1, y1, colour, width=width)

    def draw_circle(self, x, y, radius, colour, width=1, filled=False, canvas=None):
        self._capture.circle(
            canvas or self.canvas, x, y, radius, colour, width=width, filled=filled,
        )

    def draw_image(
        self, image, x=0, y=0, width=None, height=None, keep_aspect=True,
        rotation=0, canvas=None,
    ):
        prepared = image.convert("RGB")
        if width is not None and height is not None:
            if keep_aspect:
                prepared = prepared.copy()
                prepared.thumbnail((width, height))
            else:
                prepared = prepared.resize((width, height))
        if rotation:
            prepared = prepared.rotate(-rotation, expand=False)
        self._capture.set_image(canvas or self.canvas, prepared, x, y)
        return prepared

    def draw_text(self, font, x, y, colour, text, canvas=None, rotation=0):
        canvas = canvas or self.canvas
        if not rotation % 360:
            return self._capture.text(canvas, font, x, y, colour, text)

        advance, points = font.text_pixels(x, y, text)
        if not points:
            return int(x) + advance
        centre_x = (min(px for px, _ in points) + max(px for px, _ in points)) / 2
        centre_y = (min(py for _, py in points) + max(py for _, py in points)) / 2
        angle = math.radians(rotation)
        cosine, sine = math.cos(angle), math.sin(angle)
        for px, py in points:
            dx, dy = px - centre_x, py - centre_y
            self._capture.pixel(
                canvas,
                round(centre_x + dx * cosine - dy * sine),
                round(centre_y + dx * sine + dy * cosine),
                colour.red,
                colour.green,
                colour.blue,
            )
        return int(x) + advance

    def snapshot(self):
        """Return an isolated copy without changing publication or revision state."""
        return self._capture.snapshot(self.canvas)


def static_media_frames(filename, duration, settings=None):
    """Yield the physical still-image operation and its complete hold delay."""
    geometry = {}
    if settings:
        geometry = {
            "x": settings["x"],
            "y": settings["y"],
            "width": settings["width"],
            "height": settings["height"],
            "keep_aspect": settings["keep_aspect"],
        }
    yield filename, max(0.0, float(duration)) * 1000, geometry


def preview_frames(iterator):
    """Consume the same resumable frame iterator as physical wrappers."""
    yield from iterator
