"""Discover the BDF fonts available to the Matrix renderer."""

from pathlib import Path

from frame_capture import BdfFont


FONT_DIR = Path(__file__).resolve().parent / "resources" / "fonts"


def font_key(path):
    """Return the renderer key for a BDF path."""
    return Path(path).stem.replace("-", "_")


def available_font_names(font_dir=FONT_DIR):
    """Return every BDF font key in deterministic order."""
    return tuple(font_key(path) for path in sorted(Path(font_dir).glob("*.bdf")))


def text_width(font_name, text, font_dir=FONT_DIR):
    """Measure text using the same BDF glyph advances as the renderer."""
    paths = {font_key(path): path for path in Path(font_dir).glob("*.bdf")}
    try:
        font = BdfFont(paths[font_name])
    except KeyError as exc:
        raise KeyError(f"Unknown installed font: {font_name}") from exc
    advance, _ = font.text_pixels(0, 0, text)
    return advance


def font_advance_catalog(font_dir=FONT_DIR):
    """Return compact Unicode glyph-advance ranges for every installed font."""
    catalog = {}
    for path in sorted(Path(font_dir).glob("*.bdf")):
        font = BdfFont(path)
        default = font.glyphs.get(font.default_codepoint)
        ranges = []
        for codepoint, glyph in sorted(font.glyphs.items()):
            if not 0 <= codepoint <= 0x10FFFF:
                continue
            if (
                ranges
                and codepoint == ranges[-1][1] + 1
                and glyph.advance == ranges[-1][2]
            ):
                ranges[-1][1] = codepoint
            else:
                ranges.append([codepoint, codepoint, glyph.advance])
        catalog[font_key(path)] = {
            "default": default.advance if default is not None else 0,
            "ranges": ranges,
        }
    return catalog
