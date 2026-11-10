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

# Classic: (outside, inside, line) — carried over unchanged from v2.
CLASSIC = {
    "fire_burst": ("black", "red", "yellow"),
    "ember_glow": ("black", "orange", "red"),
    "royal_fizz": ("black", "magenta", "purple"),
    "ocean_wave": ("black", "cyan", "blue"),
    "miami_vice": ("cyan", "magenta", "black"),
    "sun_drop": ("black", "yellow", "orange"),
    "sunset_punch": ("black", "orange", "magenta"),
    "candy_cyan": ("magenta", "cyan", "black"),
    "vapor_pink_cyan": ("retro_deep_purple", "vapor_pink", "vapor_cyan"),
    "miami_sunset": ("retro_slate", "neon_coral", "vapor_mint"),
    "midnight_grid": ("black", "vapor_lilac", "neon_blue"),
    "electric_lagoon": ("teal", "vapor_mint", "neon_yellow"),
    "neon_arcade": ("black", "vapor_pink", "vapor_cyan"),
    "sunset_dream": ("retro_purple", "vapor_cream", "neon_yellow"),
    "aqua_glow": ("navy", "vapor_mint", "vapor_cyan"),
    "laser_grid": ("black", "neon_purple", "cyan"),
    "cotton_candy": ("black", "vapor_pink", "vapor_cream"),
    "midnight_soda": ("navy", "neon_coral", "vapor_mint"),
    "twilight_wave": ("retro_deep_purple", "neon_blue", "vapor_mint"),
    "neon_dream": ("black", "neon_purple", "vapor_cream"),
    "pastel_breeze": ("retro_slate", "vapor_pink", "vapor_cyan"),
    "mint_cloud": ("black", "vapor_mint", "vapor_cream"),
    "lilac_milk": ("black", "vapor_lilac", "vapor_cream"),
    "petal_night": ("retro_deep_purple", "vapor_pink", "vapor_lilac"),
    "teal_mist": ("teal", "vapor_cyan", "vapor_mint"),
    "sugar_fizz": ("black", "vapor_pink", "vapor_cream"),
    "mint_soda": ("black", "vapor_mint", "vapor_cyan"),
    "lilac_fizz": ("black", "vapor_lilac", "vapor_pink"),
    "cyan_haze": ("black", "vapor_cyan", "vapor_lilac"),
    "teal_cotton": ("teal", "vapor_pink", "vapor_cyan"),
    "starlight_cream": ("black", "vapor_pink", "vapor_cream"),
    "navy_mist": ("navy", "vapor_mint", "vapor_lilac"),
    "seafoam_glow": ("black", "vapor_cyan", "vapor_mint"),
    "dusk_cotton": ("retro_deep_purple", "vapor_cream", "vapor_cyan"),
    "cyan_candy": ("cyan", "vapor_pink", "vapor_cream"),
    "cyan_dream": ("cyan", "vapor_mint", "vapor_lilac"),
    "magenta_mist": ("magenta", "vapor_cream", "vapor_mint"),
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


def rgb(value: str) -> tuple[int, int, int]:
    value = NAMED.get(value, value).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
