"""Colour sets for both Lava Lamp styles."""

NAMED = {
    "black": "#000000", "red": "#ff0000", "yellow": "#ffff00", "orange": "#ffa500",
    "magenta": "#ff00ff", "purple": "#7851a9", "cyan": "#00ffff", "blue": "#0000ff",
    "navy": "#000080", "teal": "#008080", "vapor_pink": "#ff71ce", "vapor_cyan": "#01cdfe",
    "vapor_mint": "#05ffa1", "vapor_lilac": "#b967ff", "vapor_cream": "#fffb96",
    "neon_purple": "#8a00c4", "neon_blue": "#00bfff", "neon_yellow": "#ffb300",
    "neon_coral": "#ff5c5c", "retro_purple": "#b252a1", "retro_deep_purple": "#2c0452",
    "retro_slate": "#455f65",
}

# Classic: (background, wax, highlight). Redesigned from v2's 37, which had near-copies (three were
# identical): every pair here differs clearly at a glance; tests/test_lava_lamp.py keeps it that way.
CLASSIC = {
    # warm
    "fire_burst":      ("#000000", "#FF1A00", "#FFD400"),
    "ember_glow":      ("#1A0600", "#FF7A00", "#FFE7B0"),
    "sun_drop":        ("#000000", "#FFE100", "#FF5A00"),
    "blood_moon":      ("#0A0000", "#8E0010", "#FF6A5A"),
    "sunset_punch":    ("#2B0F3A", "#FF6F3C", "#B04DFF"),
    "terracotta":      ("#0A0A0A", "#C2410C", "#5FE1F0"),
    # greens
    "lime_fizz":       ("#000000", "#A8FF00", "#FF4FA0"),
    "matrix":          ("#000000", "#00C838", "#B6FFB0"),
    "toxic":           ("#200030", "#39FF14", "#FFFFFF"),
    "electric_lagoon": ("#003030", "#FFB300", "#00FF9C"),
    "mint_cloud":      ("#0D2B26", "#3FE0A8", "#FF9E6E"),
    # blues
    "ocean_wave":      ("#00121F", "#00A8FF", "#9CF6FF"),
    "deep_sea":        ("#000000", "#1F4BFF", "#FFFFFF"),
    "aqua_glow":       ("#00323C", "#00B3A6", "#FFE14D"),
    "arctic":          ("#0B2E4A", "#BFEFFF", "#2E7DFF"),
    "midnight_soda":   ("#00004A", "#FF3B3B", "#FFFFFF"),
    # purples and pinks
    "laser_grid":      ("#000000", "#7A00FF", "#FF2BD6"),
    "royal_fizz":      ("#12002A", "#C04DFF", "#FFD000"),
    "lilac_milk":      ("#2A1650", "#B48CFF", "#FFE9F6"),
    "neon_dream":      ("#000000", "#FF00C8", "#FFFF66"),
    "cotton_candy":    ("#3B1030", "#FF7EC2", "#FFF59E"),
    "vapor_pink_cyan": ("#2C0452", "#FF71CE", "#01CDFE"),
    "twilight_wave":   ("#16102E", "#4F7BFF", "#FFA94D"),
    # bright backgrounds
    "miami_vice":      ("#00D8F0", "#FF2D95", "#101010"),
    "candy_cyan":      ("#FF2D95", "#00E5FF", "#FFFFFF"),
    "sunny_side":      ("#FFC400", "#7A1FA0", "#FFFFFF"),
    # neutral
    "moonlight":       ("#000000", "#8C97A6", "#FFFFFF"),
}

# Metro: smooth gradients. bg top/bottom, halo glow, wax edge -> mid -> hot core, text, date accent.
METRO = {
    "magma": dict(bg_top="#2A0A06", bg_bottom="#0E0201", glow="#4A0E06", edge="#B3261E",
                  mid="#FF5A1F", core="#FFB547", text="#FFF4DE", accent="#FFD9A8"),
    "ocean": dict(bg_top="#0A1A30", bg_bottom="#02060E", glow="#0C2A4E", edge="#1558B0",
                  mid="#1E88E5", core="#8FDCFF", text="#EEF8FF", accent="#9AD8FF"),
    "aurora": dict(bg_top="#0A2216", bg_bottom="#020A06", glow="#0F3320", edge="#1A7A45",
                   mid="#2ECC71", core="#D2FF9A", text="#F2FFF5", accent="#B7F5C8"),
    "synthwave": dict(bg_top="#2C0452", bg_bottom="#0A0216", glow="#3E0A6E", edge="#8A00C4",
                      mid="#FF71CE", core="#FFFB96", text="#FFF6FF", accent="#FFB3E6"),
    "mono": dict(bg_top="#161616", bg_bottom="#040404", glow="#242424", edge="#5A5A5A",
                 mid="#A0A0A0", core="#FFFFFF", text="#FFFFFF", accent="#BDBDBD"),
}


def hex_of(value: str) -> str:
    """#RRGGBB for a named or hex colour."""
    return NAMED.get(value, value).upper()


# What the dashboard shows for each palette, in the same order as a custom palette:
# background, wax, highlight.
CLASSIC_SWATCHES = {name: [hex_of(c) for c in colours] for name, colours in CLASSIC.items()}
METRO_SWATCHES = {name: [p["bg_top"], p["mid"], p["core"]] for name, p in METRO.items()}


def rgb(value: str) -> tuple[int, int, int]:
    value = NAMED.get(value, value).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


# -- custom palettes ------------------------------------------------------------------------
# Like v2: a custom palette is just three colours, shared by both styles.
#   {"name": "Bedroom", "background": "#000000", "wax": "#ff0000", "highlight": "#ffff00", "enabled": true}
# Classic uses them directly (outside, inside, line). Metro derives its gradients from them.
CUSTOM_KEYS = ("background", "wax", "highlight")


def _mix(a, b, k):
    a, b = rgb(a), rgb(b)
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * k) for x, y in zip(a, b))


def metro_from(background: str, wax: str, highlight: str) -> dict:
    """A full Metro palette worked out from three colours."""
    return dict(
        bg_top=_mix(background, wax, 0.12), bg_bottom=_mix(background, "#000000", 0.6),
        glow=_mix(background, wax, 0.3), edge=_mix(wax, "#000000", 0.35), mid=wax,
        core=highlight, text=_mix(highlight, "#FFFFFF", 0.85), accent=_mix(highlight, "#FFFFFF", 0.4),
    )


def check_custom(palettes) -> None:
    """Raise ValueError with a readable message unless ``palettes`` is a list of good custom palettes."""
    if not isinstance(palettes, list):
        raise ValueError("custom_palettes must be a list")
    names = set()
    for i, p in enumerate(palettes):
        where = f"custom palette {i + 1}"
        if not isinstance(p, dict):
            raise ValueError(f"{where}: must be an object with a name and three colours")
        unknown = set(p) - {"name", "enabled", *CUSTOM_KEYS}
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        name = p.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 24:
            raise ValueError(f"{where}: give it a name (up to 24 characters)")
        if name.strip().lower() in names:
            raise ValueError(f"{where}: another palette is already called {name!r}")
        names.add(name.strip().lower())
        for key in CUSTOM_KEYS:
            value = p.get(key)
            if not (isinstance(value, str) and len(value) == 7 and value[0] == "#"
                    and all(c in "0123456789abcdefABCDEF" for c in value[1:])):
                raise ValueError(f"{where} ({name}): {key} must be a colour like #FF8800")
        if not isinstance(p.get("enabled", True), bool):
            raise ValueError(f"{where} ({name}): enabled must be true or false")


def enabled_custom(palettes) -> list[dict]:
    return [p for p in palettes if p.get("enabled", True)]
