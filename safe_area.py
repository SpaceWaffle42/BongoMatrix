"""System-wide Screen Safe Area (Priority 1.11).

A display sits in a frame that may hide its outer pixels. The safe area is the
space, in whole physical pixels, kept clear from each panel edge. Every drawing
operation is translated by (left, top) and clipped to the inner rectangle. The
margin ring outside it is always black, as if the panel were physically smaller
(owner decision 2026-10-31): fills, backgrounds and borders never light it. The
one exception is full-screen media with the ``media_inside`` toggle off, which
has no viewport and may cover the whole panel.
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
        """Place an inner-sized render on a full panel whose margin stays black.

        ``background`` is kept for callers but never paints the margin ring.
        """
        del background
        viewport = self.viewport(width, height)
        if viewport is None:
            return inner.convert("RGB")
        panel = Image.new("RGB", (int(width), int(height)), "black")
        x, y, inner_width, inner_height = viewport
        panel.paste(inner.convert("RGB").crop((0, 0, inner_width, inner_height)), (x, y))
        return panel


ZERO = SafeArea()


def canvas_size(matrix):
    """(width, height) a module draws on: the driver's inner safe-area size, else the panel.

    Both matrix_framework.Matrix and edit_preview.CaptureMatrix report it via
    ``matrix.matrix.width/height``; plain test doubles fall back to 64x32.
    """
    driver = getattr(matrix, "matrix", None)
    width, height = getattr(driver, "width", None), getattr(driver, "height", None)
    if (type(width) is int and type(height) is int
            and 0 < width <= PANEL_WIDTH and 0 < height <= PANEL_HEIGHT):
        return width, height
    return PANEL_WIDTH, PANEL_HEIGHT


def margin_boxes(viewport, width=PANEL_WIDTH, height=PANEL_HEIGHT):
    """(left, top, right, bottom) panel boxes of the margin ring around ``viewport``."""
    if viewport is None:
        return ()
    x, y, inner_width, inner_height = viewport
    width, height = int(width), int(height)
    boxes = (
        (0, 0, width, y),
        (0, y + inner_height, width, height),
        (0, y, x, y + inner_height),
        (x + inner_width, y, width, y + inner_height),
    )
    return tuple(box for box in boxes if box[2] > box[0] and box[3] > box[1])


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


FITS = ("contain", "cover", "stretch", "manual")


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _thumbnail_size(source, box):
    """The size Pillow's ``Image.thumbnail(box)`` produces (never enlarges)."""
    import math

    width, height = source
    box_width, box_height = box
    if box_width >= width and box_height >= height:
        return width, height
    aspect = width / height

    def round_aspect(number, key):
        return max(min(math.floor(number), math.ceil(number), key=key), 1)

    if box_width / box_height >= aspect:
        box_width = round_aspect(box_height * aspect, key=lambda n: abs(aspect - n / box_height))
    else:
        box_height = round_aspect(
            box_width / aspect, key=lambda n: 0 if n == 0 else abs(aspect - box_width / n),
        )
    return box_width, box_height


def place_media(source_size, fit, layout, canvas_size):
    """Where one image goes on the canvas: ``((draw_w, draw_h), (draw_x, draw_y))``.

    The single media placement rule for the physical Matrix and the Edit Preview
    (owner decision 2, 2026-10-31). ``canvas_size`` is the drawing canvas: the inner
    size under a safe area, so the panel behaves as if it were really that small.
    Saved x/y/width/height are pixels on that canvas, never rescaled; anything past
    its edge is clipped by the caller's viewport.

    ``fit`` contain/cover/stretch size against the canvas; manual uses the layout as
    authored. With no ``fit``: width+height is a box (``keep_aspect`` fits inside it the
    way ``Image.thumbnail`` does, otherwise exact), one side scales proportionally, and
    no size at all is a 1:1 blit.
    """
    source_width, source_height = (int(value) for value in source_size)
    canvas_width, canvas_height = (int(value) for value in canvas_size)
    layout = layout or {}
    x, y = layout.get("x", 0), layout.get("y", 0)
    width, height = layout.get("width"), layout.get("height")
    if fit is None:
        if width is not None and height is not None:
            if layout.get("keep_aspect", True):
                size = _thumbnail_size((source_width, source_height), (int(width), int(height)))
            else:
                size = (int(width), int(height))
        elif width is not None:
            size = (int(width), int(source_height * (width / source_width)))
        elif height is not None:
            size = (int(source_width * (height / source_height)), int(height))
        else:
            size = (source_width, source_height)
        return size, (int(x), int(y))
    if fit == "contain":
        scale = min(1, canvas_width / source_width, canvas_height / source_height)
        size = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
    elif fit == "cover":
        scale = max(canvas_width / source_width, canvas_height / source_height)
        size = (max(canvas_width, round(source_width * scale)),
                max(canvas_height, round(source_height * scale)))
    elif fit == "stretch":
        return (canvas_width, canvas_height), (0, 0)
    elif fit == "manual":
        if not all(_whole(value) for value in (x, y, width, height)) or width < 1 or height < 1:
            raise ValueError("manual media fit requires integer geometry")
        return (width, height), (x, y)
    else:
        raise ValueError(f"unknown media fit: {fit}")
    return size, ((canvas_width - size[0]) // 2, (canvas_height - size[1]) // 2)


def render_media(image, fit, layout, canvas_size):
    """Resample ``image`` once per :func:`place_media`; returns ``(image, x, y)``."""
    image = image.convert("RGB")
    size, (x, y) = place_media(image.size, fit, layout, canvas_size)
    if image.size != size:
        layout = layout or {}
        if (fit is None and layout.get("width") is not None and layout.get("height") is not None
                and layout.get("keep_aspect", True)):
            # Same pixels as before: Image.thumbnail (reducing_gap draft + LANCZOS).
            image = image.copy()
            image.thumbnail((int(layout["width"]), int(layout["height"])), Image.Resampling.LANCZOS)
        else:
            image = image.resize(size, Image.Resampling.LANCZOS)
    return image, x, y
