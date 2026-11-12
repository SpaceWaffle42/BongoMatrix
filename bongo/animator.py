"""The dashboard's Animator: frames drawn in the browser become a GIF in the media library.

A frame is the panel's pixels as a list of ``"#rrggbb"`` (or None for the background), row by
row, plus how long it shows. The GIF is drawn at the panel's size, one pixel per LED.
"""
from __future__ import annotations

import io
import re

from PIL import Image

from bongo import media

MAX_FRAMES = 300
HEX = re.compile(r"#[0-9a-fA-F]{6}")
PREVIEW = "uploaded/.animator/preview.gif"   # "Show on Matrix" without saving: hidden from the library


def colour(value, background):
    if value is None:
        return background
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError(f"colours look like #ff8800, not {str(value)[:20]!r}")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def gif(frames: list, width: int, height: int, background: str = "#000000", loop: str = "loop") -> bytes:
    """The frames as an animated GIF (``loop``: loop, once or pingpong)."""
    if not isinstance(frames, list) or not frames:
        raise ValueError("an animation needs at least one frame")
    if len(frames) > MAX_FRAMES:
        raise ValueError(f"up to {MAX_FRAMES} frames")
    bg = colour(background, (0, 0, 0))
    images, times = [], []
    for k, f in enumerate(frames):
        px = f.get("px") if isinstance(f, dict) else None
        if not isinstance(px, list) or len(px) != width * height:
            raise ValueError(f"frame {k + 1} isn't {width}x{height}")
        img = Image.new("RGB", (width, height))
        img.putdata([colour(v, bg) for v in px])
        images.append(img)
        times.append(max(20, min(10_000, int(f.get("ms", 100)))))
    if loop == "pingpong" and len(images) > 2:
        images += images[-2:0:-1]
        times += times[-2:0:-1]
    out = io.BytesIO()
    extra = {} if loop == "once" else {"loop": 0}
    images[0].save(out, "GIF", save_all=True, append_images=images[1:], duration=times, optimize=False,
                   disposal=1, **extra)
    return out.getvalue()


def save(name: str, data: bytes) -> str:
    """Into the media library as ``uploaded/<name>.gif`` (never over another file)."""
    stem = media.clean_name(f"{name or 'animation'}.gif")
    return media.save_upload(stem, io.BytesIO(data))


def save_preview(data: bytes) -> str:
    path = media.UPLOADED / ".animator" / "preview.gif"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return PREVIEW
