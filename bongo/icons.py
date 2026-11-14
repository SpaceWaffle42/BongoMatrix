"""8x8 pixel icons for holidays and your own events (Countdown, Calendar). Each is an Animator drawing,
``resources/animator/icon_<name>.json``: open one in the Animator (Open > Built-in icons), change it and
save it under the same name, and the panel shows yours. Colours are chosen to read on an LED panel, where
black is just "off": bright, saturated, and the dark tones only as detail inside a lit shape (eyes, ball
patches, a poppy's centre), never against the background.

Reviewed 11 Nov 2026 for recognisability, LED readability and cultural accuracy. Notes from that:
a nine-branch hanukkiah can't be drawn in 8 columns (a wrong count is worse than none), so Hanukkah
has a dreidel; the Islamic holidays have a plain crescent (the crescent-and-star is a cultural, not
religious, emblem, and a small star reads as a cross); sacred emblems (Khanda, Om, Ik Onkar) are
not attempted at this size."""
from __future__ import annotations

from PIL import Image

from bongo import sprites

SIZE = 8

# every icon, in the order they were added (the dashboard sorts them by name)
ICONS = (
    # celebrations
    "heart", "star", "fireworks", "cake", "gift", "party", "balloon", "bell", "ring", "music",
    # home, travel, life
    "plane", "car", "house", "bin", "school", "baby", "paw", "ball", "work", "beach", "game", "book", "pin",
    "clock", "sun", "snowflake", "flower", "flag", "tie",
    # holidays
    "tree", "pumpkin", "egg", "cross", "dove", "palm", "shamrock", "bonfire", "poppy", "turkey", "pancake",
    "mask", "wine", "wheat", "apple", "candle", "scroll", "crescent", "lantern", "kite", "moon", "colours",
    "rakhi", "flute", "lamp", "bow", "willow", "boat", "mooncake", "dreidel", "fanous", "night", "lulav",
    "frond", "ash_cross", "red_egg", "matzah", "nishan", "trishul", "modak", "dandiya", "hongbao", "hardhat",
)

# words people may know an icon by, for the dashboard
LABELS = {"party": "Party popper", "school": "Graduation", "work": "Work", "game": "Games", "pin": "Place",
          "bin": "Bin day", "colours": "Holi colours", "lamp": "Diya lamp", "bow": "Bow and arrow",
          "rakhi": "Rakhi", "willow": "Willow", "mooncake": "Mooncake", "dreidel": "Dreidel",
          "fanous": "Ramadan lantern", "night": "Night sky", "lulav": "Lulav and etrog", "frond": "Palm frond",
          "ash_cross": "Ash cross", "red_egg": "Red egg", "matzah": "Matzah", "nishan": "Nishan Sahib",
          "trishul": "Trishul", "modak": "Modak", "dandiya": "Dandiya sticks", "hongbao": "Red envelope",
          "hardhat": "Hard hat", "baby": "Baby bottle", "boat": "Dragon boat"}

EVENT_ICONS = ("cake", "gift", "heart", "star", "party", "balloon", "ring", "music", "plane", "car", "house",
               "bin", "school", "baby", "paw", "ball", "work", "beach", "game", "book", "pin", "clock", "bell",
               "sun", "snowflake", "flower", "tree", "pumpkin", "egg", "fireworks")


def names() -> list[str]:
    return list(ICONS)


def label(name: str) -> str:
    return LABELS.get(name, name.replace("_", " ").capitalize())


def icon(name: str) -> Image.Image:
    """The 8x8 RGBA image for ``name`` (KeyError if there's no such icon). Yours, if you've saved a drawing
    called ``icon_<name>`` from the Animator that is 8 x 8 (its first frame); otherwise the built-in one. The picture is
    shared: paste it, don't draw on it."""
    if name not in ICONS:
        raise KeyError(name)
    tile = sprites.load(f"icon_{name}").frame(0)
    # the screens leave an 8 x 8 space, so any other size would spill over the text; and an empty first frame
    # would be an icon that has quietly gone missing
    if tile.size != (SIZE, SIZE) or not tile.getbbox():
        tile = sprites.load(f"icon_{name}", own=False).frame(0)
    return tile
