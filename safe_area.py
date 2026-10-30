"""System-wide Screen Safe Area (Priority 1.11).

A display sits in a frame that may hide its outer pixels. The safe area is the
space, in whole physical pixels, kept clear from each panel edge. Background
colours (Matrix.fill/clear) still cover the whole panel; every other drawing
operation is translated by (left, top) and clipped to the inner rectangle.
Full-screen media follows the ``media_inside`` toggle.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

EDGES = ("top", "right", "bottom", "left")
MAX_EDGE = 6
KEYS = frozenset((*EDGES, "media_inside"))
PANEL_WIDTH = 64
PANEL_HEIGHT = 32

#: Fresh installs (DEFAULT_CONFIG) start with no safe area.
NEW_DISPLAY_DEFAULT = {"top": 0, "right": 0, "bottom": 0, "left": 0, "media_inside": True}
#: Saved configurations from before 1.11 read as 2 px so calendars that had the
#: fixed 0.3.75 frame border look unchanged.
EXISTING_DISPLAY_DEFAULT = {"top": 2, "right": 2, "bottom": 2, "left": 2, "media_inside": True}


def validate(value):
    """Reject anything but the exact {top,right,bottom,left: 0..6, media_inside: bool}."""
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("safe_area must contain exactly top, right, bottom, left and media_inside")
    for edge in EDGES:
        amount = value[edge]
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= MAX_EDGE:
            raise ValueError(f"safe_area {edge} must be a whole number of pixels from 0 to {MAX_EDGE}")
    if not isinstance(value["media_inside"], bool):
        raise ValueError("safe_area media_inside must be true or false")
    return value


@dataclass(frozen=True)
class SafeArea:
    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0
    media_inside: bool = True

    @classmethod
    def from_mapping(cls, value):
        validate(value)
        return cls(**{key: value[key] for key in KEYS})

    @classmethod
    def from_config(cls, config):
        """Read a validated config; configs without the key draw edge to edge."""
        device = config.get("device") if isinstance(config, dict) else None
        value = device.get("safe_area") if isinstance(device, dict) else None
        return ZERO if value is None else cls.from_mapping(value)

    @property
    def is_zero(self):
        return not (self.top or self.right or self.bottom or self.left)

    def as_dict(self):
        return {key: getattr(self, key) for key in (*EDGES, "media_inside")}

    def viewport(self, width=PANEL_WIDTH, height=PANEL_HEIGHT, *, media=False):
        """Return (x, y, width, height) of the drawable rectangle, or None for the full panel."""
        if self.is_zero or (media and not self.media_inside):
            return None
        return (
            self.left, self.top,
            max(1, int(width) - self.left - self.right),
            max(1, int(height) - self.top - self.bottom),
        )

    def inner_size(self, width=PANEL_WIDTH, height=PANEL_HEIGHT, *, media=False):
        viewport = self.viewport(width, height, media=media)
        return (int(width), int(height)) if viewport is None else viewport[2:]

    def compose(self, inner, background, width=PANEL_WIDTH, height=PANEL_HEIGHT):
        """Place an inner-sized render on a full panel filled with ``background``."""
        viewport = self.viewport(width, height)
        if viewport is None:
            return inner.convert("RGB")
        panel = Image.new("RGB", (int(width), int(height)), background)
        x, y, inner_width, inner_height = viewport
        panel.paste(inner.convert("RGB").crop((0, 0, inner_width, inner_height)), (x, y))
        return panel


ZERO = SafeArea()


def clip_image(image, x, y, viewport):
    """Crop an image drawn at inner (x, y) to the viewport; return (image, panel_x, panel_y) or None."""
    vx, vy, vw, vh = viewport
    x, y = int(x), int(y)
    left, top = max(0, x), max(0, y)
    right, bottom = min(vw, x + image.width), min(vh, y + image.height)
    if right <= left or bottom <= top:
        return None
    cropped = image.crop((left - x, top - y, right - x, bottom - y))
    return cropped, left + vx, top + vy


MARKER_COLOUR = (255, 214, 0)


def marker_colour(tick):
    """Pulsing yellow: alternate full and dimmed brightness every tick (4 per second)."""
    level = 1.0 if tick % 2 == 0 else 0.45
    return tuple(int(channel * level) for channel in MARKER_COLOUR)


def marker_points(viewport, width=PANEL_WIDTH, height=PANEL_HEIGHT, *, phase=0):
    """Dotted outline on the inner rectangle's edge pixels (every other pixel)."""
    if viewport is None:
        viewport = (0, 0, int(width), int(height))
    x0, y0, w, h = viewport
    x1, y1 = x0 + w - 1, y0 + h - 1
    perimeter = []
    perimeter.extend((x, y0) for x in range(x0, x1 + 1))
    perimeter.extend((x1, y) for y in range(y0 + 1, y1 + 1))
    perimeter.extend((x, y1) for x in range(x1 - 1, x0 - 1, -1))
    perimeter.extend((x0, y) for y in range(y1 - 1, y0, -1))
    return [point for index, point in enumerate(perimeter) if (index + phase) % 2 == 0]


def fit_media_geometry(matrix, settings):
    """Scale panel-space media geometry into the active full-screen-media safe area.

    Saved splash/sleep layouts and custom media screens store absolute panel
    geometry (usually x0 y0 w64 h32). With ``media_inside`` on, the safe-area
    scope already translates drawing by (left, top), so this only rescales from
    the panel size to the inner size: x' = x*iw/pw, w' = (x+w)*iw/pw - x'.
    Anything else (no scope, toggle off, plain mocks) returns ``settings`` as is.
    """
    if not settings:
        return settings
    safe_area = getattr(matrix, "safe_area", None)
    state = getattr(matrix, "__dict__", {})
    if (not isinstance(safe_area, SafeArea) or not state.get("_safe_area_media", False)
            or safe_area.is_zero or not safe_area.media_inside):
        return settings
    inner = matrix.matrix
    inner_width, inner_height = int(inner.width), int(inner.height)
    panel_width = inner_width + safe_area.left + safe_area.right
    panel_height = inner_height + safe_area.top + safe_area.bottom
    scaled = dict(settings)
    for position, size, inner_size, panel_size in (
        ("x", "width", inner_width, panel_width),
        ("y", "height", inner_height, panel_height),
    ):
        start = settings.get(position)
        extent = settings.get(size)
        numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
        if numeric(start):
            scaled[position] = round(start * inner_size / panel_size)
        if numeric(extent):
            origin = start if numeric(start) else 0
            end = round((origin + extent) * inner_size / panel_size)
            scaled[size] = max(1, end - round(origin * inner_size / panel_size))
    return scaled
