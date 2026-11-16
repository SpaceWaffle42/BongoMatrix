"""Safe area: the part of the panel the frame doesn't hide.

Modules always draw on a canvas the size of the safe area (``ctx.width`` x ``ctx.height``).
The core places that canvas on the panel and leaves the hidden border dark, so no module
ever needs to know about the frame.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw


@dataclass(frozen=True)
class SafeArea:
    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0
    panel_width: int = 64
    panel_height: int = 32

    MIN_SIZE = (32, 16)  # smallest visible area modules are designed and tested for

    def __post_init__(self):
        if min(self.top, self.right, self.bottom, self.left) < 0:
            raise ValueError("safe area margins can't be negative")
        if self.width < self.MIN_SIZE[0] or self.height < self.MIN_SIZE[1]:
            raise ValueError(f"safe area {self.width}x{self.height} is smaller than {self.MIN_SIZE}")

    @classmethod
    def from_config(cls, config: dict) -> "SafeArea":
        margins = config.get("safe_area") or {}
        panel = config["panel"]
        # the whole display: panels chained side by side, and parallel chains stacked (as HardwarePanel drives them)
        return cls(**{k: int(margins.get(k, 0)) for k in ("top", "right", "bottom", "left")},
                   panel_width=panel["cols"] * int(panel.get("chain_length") or 1),
                   panel_height=panel["rows"] * int(panel.get("parallel") or 1))

    @property
    def width(self) -> int:
        return self.panel_width - self.left - self.right

    @property
    def height(self) -> int:
        return self.panel_height - self.top - self.bottom

    def place(self, frame: Image.Image, border=(0, 0, 0)) -> Image.Image:
        """Put a module's safe-area frame onto a full panel image."""
        if frame.size == (self.panel_width, self.panel_height) and not any(self.margins):
            return frame
        panel = Image.new("RGB", (self.panel_width, self.panel_height), border)
        panel.paste(frame.crop((0, 0, self.width, self.height)), (self.left, self.top))
        return panel

    @property
    def margins(self) -> tuple[int, int, int, int]:
        return self.top, self.right, self.bottom, self.left

    def calibration(self, font=None, step: int = 0) -> Image.Image:
        """Full-panel test card: dim grid in the hidden border, bright outline and corner
        ticks on the safe edge, margins written in the middle. ``step`` blinks the outline."""
        w, h = self.panel_width, self.panel_height
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        for x in range(0, w, 2):  # hidden border: checkerboard so you can see what the frame covers
            for y in range(0, h, 2):
                if not (self.left <= x < self.left + self.width and self.top <= y < self.top + self.height):
                    d.point((x + (y // 2) % 2, y), fill=(110, 26, 26))
        x0, y0, x1, y1 = self.left, self.top, self.left + self.width - 1, self.top + self.height - 1
        edge = (255, 181, 71) if step % 2 == 0 else (255, 120, 40)
        d.rectangle([x0, y0, x1, y1], outline=edge)
        for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            d.line([(cx, cy), (cx + 4 * dx, cy)], fill=(255, 244, 222))
            d.line([(cx, cy), (cx, cy + 4 * dy)], fill=(255, 244, 222))
        if font is not None:
            text = f"T{self.top} R{self.right} B{self.bottom} L{self.left}"
            tw = font.width(text)
            font.draw(img, x0 + (self.width - tw) // 2, y0 + self.height // 2 + 3, text, (255, 244, 222))
        return img
