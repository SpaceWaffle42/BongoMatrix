"""8x8 pixel icons for holidays and your own events (Countdown, Calendar), drawn here as text so
they need no image files. One letter per pixel; '.' is transparent. Colours are chosen to read on
an LED panel, where black is just "off": bright, saturated, and the dark tones (k, N, R, B) only as
detail inside a lit shape (eyes, ball patches, a poppy's centre), never against the background.

Reviewed 11 Nov 2026 for recognisability, LED readability and cultural accuracy. Notes from that:
a nine-branch hanukkiah can't be drawn in 8 columns (a wrong count is worse than none), so Hanukkah
has a dreidel; the Islamic holidays have a plain crescent (the crescent-and-star is a cultural, not
religious, emblem, and a small star reads as a cross); sacred emblems (Khanda, Om, Ik Onkar) are
not attempted at this size."""
from __future__ import annotations

from functools import lru_cache

from PIL import Image

PALETTE = {
    "r": (235, 45, 45), "R": (150, 20, 35), "o": (255, 140, 30), "y": (255, 214, 50), "Y": (255, 244, 180),
    "g": (70, 200, 80), "G": (35, 145, 65), "b": (60, 135, 255), "B": (35, 65, 170), "c": (100, 225, 245),
    "p": (255, 110, 175), "P": (165, 85, 230), "w": (245, 240, 228), "s": (150, 155, 170), "n": (170, 105, 50),
    "N": (110, 65, 30), "t": (225, 175, 115), "k": (55, 55, 65),
}

ICONS = {
    # celebrations
    "heart": ("........", ".rr..rr.", "rprrrrrr", "rrrrrrrr", ".rrrrrr.", "..rrrr..", "...rr...", "........"),
    "star": ("...yy...", "...yy...", "..yyyy..", "yyyyyyyy", ".yyyyyy.", "..yyyy..", ".yy..yy.", "yy....yy"),
    "fireworks": ("p..y..c.", ".p.y.c..", "..pyc...", "yyy.yyy.", "..cyp...", ".c.y.p..", "c..y..p.", "........"),
    "cake": ("..y..y..", "..o..o..", ".pwppwp.", ".pppppp.", "wwwwwwww", "rrrrrrrr", "wwwwwwww", "nnnnnnnn"),
    "gift": ("..r..r..", "...rr...", "gggrrggg", "gggrrggg", "gggrrggg", "rrrrrrrr", "gggrrggg", "gggrrggg"),
    "party": ("y.....p.", ".....c..", "..y.....", "o.....b.", "oy......", "ooy.....", "ooooy...", "oooooy.."),
    "balloon": ("..rrr...", ".rrwrr..", ".rrrrr..", ".rrrrr..", "..rrr...", "...r....", "....r...", "...r...."),
    "bell": ("...yy...", "..yyyy..", "..yyyy..", ".yyyyyy.", ".yyyyyy.", "yyyyyyyy", "...oo...", "........"),
    "ring": ("..cwc...", "...c....", ".yyyyy..", "yy...yy.", "y.....y.", "yy...yy.", ".yyyyy..", "........"),
    "music": ("...bbbbb", "...b...b", "...b...b", "...b...b", ".bbb.bbb", "bbbbbbbb", "bbb..bbb", "........"),
    # home, travel, life
    "plane": ("........", "c.......", "cc......", "ccccccc.", "cccccccc", "...cc...", "..cc....", "........"),
    "car": ("........", "..rrrr..", ".rccrcc.", "rrrrrrry", "rrrrrrrr", "rssrrssr", ".ss..ss.", "........"),
    "house": ("...rr...", "..rrrr..", ".rrrrrr.", "rrrrrrrr", ".wwwwww.", ".wcwwnw.", ".wwwwnw.", ".wwwwnw."),
    "bin": ("..gggg..", "gggggggg", ".gGgGgG.", ".gGgGgG.", ".gGgGgG.", ".gGgGgG.", ".gggggg.", "........"),
    "school": ("........", "..PPPP..", "PPPPPPPP", "..PPPP.y", "..BBBB.y", "..BBBB.y", "......y.", "........"),
    "baby": ("...tt...", "...tt...", "..bbbb..", "..wwww..", "..wbww..", "..wwww..", "..wbww..", "..wwww.."),
    "paw": (".tt..tt.", ".tt..tt.", "t......t", "t..tt..t", "..tttt..", ".tttttt.", ".tttttt.", "..tttt.."),
    "ball": ("..wwww..", ".wwkkww.", "wwwkkwww", "wkwwwwkw", "kkwwwwkk", "wwwkkwww", ".wwkkww.", "..wwww.."),
    "work": ("........", "..nnnn..", "..n..n..", "tttttttt", "nnnyynnn", "nnnnnnnn", "nnnnnnnn", "........"),
    "beach": ("..rwwr..", ".rrwwrr.", "rrrwwrrr", "...n....", "...n....", "...n....", "bbbnyyyy", "yyyyyyyy"),
    "game": ("........", ".ssssss.", "sswsssps", "swwwsspp", "sswssbss", "ssssssss", "ss....ss", "........"),
    "book": (".bbbbbb.", "bbbbbbbw", "byyyybbw", "bbbbbbbw", "bbbbbbbw", "bbbbbbbw", "bbbbbbbw", ".wwwwwww"),
    "pin": ("..rrrr..", ".rrrrrr.", ".rrwwrr.", ".rrwwrr.", "..rrrr..", "...rr...", "...r....", "........"),
    "clock": ("..wwww..", ".w....w.", "w..y...w", "w..y...w", "w..yyy.w", "w......w", ".w....w.", "..wwww.."),
    "sun": ("y..y..y.", ".y.y.y..", "..yyy...", "yyyoyyy.", "..yyy...", ".y.y.y..", "y..y..y.", "........"),
    "snowflake": ("...c....", ".c.c.c..", "..ccc...", "ccccccc.", "..ccc...", ".c.c.c..", "...c....", "........"),
    "flower": (".pp.pp..", "ppppppp.", ".ppypp..", "ppppppp.", ".pp.pp..", "...g....", "gg.g.gg.", "..ggg..."),
    "flag": ("sbwbrrrr", "sbbbwwww", "sbwbrrrr", "swwwwwww", "srrrrrrr", "s.......", "s.......", "s......."),
    "tie": (".w....w.", "..wrrw..", "...rr...", "...rr...", "..rbrr..", "..rrbr..", "..rrrr..", "...rr..."),
    # holidays
    "tree": ("...y....", "...g....", "..ggg...", ".ggrgg..", "..ggg...", ".gyggg..", "gggggrg.", "...n...."),
    "pumpkin": ("...g....", "...n....", ".oooooo.", "oyooooyo", "oooyyooo", "oyyyyyyo", ".oooooo.", "........"),
    "egg": ("...pp...", "..pppp..", ".pyyyyp.", ".pppppp.", "pcpcpcpp", "pppppppp", ".pppppp.", "..pppp.."),
    "cross": ("...yy...", "...yy...", ".yyyyyy.", ".yyyyyy.", "...yy...", "...yy...", "...yy...", "...yy..."),
    "dove": ("........", "....w...", "....ww..", ".w..www.", "owwwwwww", "..wwwww.", "..g.ww..", ".gg....."),
    "palm": ("..g.g...", ".gggggg.", "ggg.ggg.", "g..n..g.", "...n....", "...n....", "...n....", "..nnn..."),
    "shamrock": ("...gg...", "..gggg..", "..gggg..", "gg.gg.gg", "gggggggg", "ggg..ggg", ".g..g.g.", "....g..."),
    "bonfire": ("...y....", "..yoy...", ".yoroy..", ".orrro..", "..oro...", "n.nnn.n.", ".nnnnn..", "n.....n."),
    "poppy": ("........", ".rr.rr..", "rrrrrrr.", "rrrkrrr.", "rrrrrrr.", ".rrrrr..", "...g....", "...g...."),
    "turkey": ("..rrrr..", ".roooor.", "royttyor", "ronrrnor", "rnnnnnnr", ".nnnnnn.", "..nnnn..", "..y..y.."),
    "pancake": ("...yy...", ".nnnnnn.", "tnttttnt", "tttttttt", ".NNNNNN.", "tttttttt", ".NNNNNN.", "tttttttt"),
    "mask": ("........", "P......P", "PPP..PPP", "P..PP..P", "PPPPPPPP", ".PP..PP.", "........", "........"),
    "wine": ("........", ".rrrrrr.", ".rrrrrr.", "..rrrr..", "...ss...", "...ss...", "...ss...", "..ssss.."),
    "wheat": ("...y....", "..yyy...", ".y.y.y..", "..yyy...", ".y.y.y..", "..yyy...", "...g....", "...g...."),
    "apple": ("...n....", "...ng...", ".rrrrr..", "rrwrrrr.", "rrrrrrr.", "rrrrrrr.", ".rrrrr..", "..r.r..."),
    "candle": ("...y....", "..yoy...", "...k....", "..www...", "..www...", "..www...", "..www...", ".sssss.."),
    "scroll": ("o......o", "nYYYYYYn", "nYnnYnYn", "nYYYYYYn", "nYnYnnYn", "nYYYYYYn", "n......n", "o......o"),
    "crescent": ("..yyy...", ".yy.....", "yy......", "yy......", "yy......", ".yy.....", "..yyy...", "........"),
    "lantern": ("...y....", "..rrrr..", ".rryyrr.", ".rryyrr.", ".rryyrr.", "..rrrr..", "...y....", "...y...."),
    "kite": ("...p....", "..ppp...", ".ppyyp..", "ppyyyyp.", ".ppyyp..", "..ppp...", "...p.b..", "....b..b"),
    "moon": ("..wwww..", ".www....", "www.....", "www.....", "www.....", ".www....", "..wwww..", "........"),
    "colours": (".pp.yy..", "ppp.yyy.", "ppp.yyy.", "........", "ggg.bbb.", "ggg.bbb.", ".gg.bb..", "........"),
    "rakhi": ("........", "...rr...", "..ryyr..", "yyyrryyy", "..ryyr..", "...rr...", "........", "........"),
    "flute": (".....gg.", "....gbcg", ".....gg.", "......g.", "otttttto", "tntntntt", "........", "........"),
    "lamp": ("....y...", "...yyy..", "...yoy..", "....o...", "ooYYYYoo", ".oooooo.", "..oooo..", "........"),
    "bow": (".nn.....", ".w.n....", ".w..n.y.", "rwyyyyyy", ".w..n.y.", ".w.n....", ".nn.....", "........"),
    "willow": ("..gggg..", ".gggggg.", "g.gnng.g", "g.gnng.g", "g.gnng.g", "g..nn..g", "...nn...", "..nnnn.."),
    "boat": ("........", "rr......", "rwr....r", ".r.y.y.r", ".rrrrrrr", "..yyyyy.", ".b.b.b.b", "bbbbbbbb"),
    "mooncake": ("........", ".nnnnnn.", "nttttttn", "ntyttytn", "nttyyttn", "ntyttytn", "nttttttn", ".nnnnnn."),
    "dreidel": ("...w....", "...w....", ".bbbbbb.", ".bwbwbwb", ".bwbwbwb", ".bwwwwwb", "..bbbbb.", "....b..."),
    "fanous": ("...y....", "..yyy...", "yyyyyyy.", ".yoyoy..", ".yoyoy..", ".yoyoy..", "yyyyyyy.", "..yyy..."),
    "night": ("..yyy..Y", ".yy.....", "yy...Y..", "yy......", "yy.....Y", ".yy.....", "..yyy.Y.", "........"),
    "lulav": ("..g.....", ".Gg.....", ".Gg.....", ".Gg.....", "GGgg.yy.", ".Ggg.yyy", "..n.yyyy", "..n..yy."),
    "frond": (".....g.g", "....g.g.", "...g.g.g", "..g.g.g.", ".g.g.g..", "..G.g...", ".G.g....", "G......."),
    "ash_cross": ("...ss...", "...ss...", "ssssssss", "ssssssss", "...ss...", "...ss...", "...ss...", "...ss..."),
    "red_egg": ("...rr...", "..rrrr..", ".rwrrrr.", ".rrrrrr.", "rrrrrrrr", "rrrrrrRr", ".rrrrRr.", "..RRRR.."),
    "matzah": ("........", "tttttttt", "tNtNtNtN", "tttttttt", "NtNtNtNt", "tttttttt", "tNtNtNtN", "tttttttt"),
    "nishan": ("s.......", "soooo...", "sooooooo", "soooo...", "s.......", "s.......", "s.......", "sss....."),
    "trishul": ("w..w..w.", "w..w..w.", ".wwwww..", "...w....", "..nnn...", "...n....", "..nnn...", "...w...."),
    "modak": ("...Y....", "...Y....", "..YtY...", ".YtYtY..", ".YtYtY..", "YtYtYtY.", "YYYYYYY.", ".ggggg.."),
    "dandiya": ("o......o", ".r....g.", "..y..y..", "...rg...", "...gr...", "..y..y..", ".g....r.", "o......o"),
    "hongbao": (".rrrrrr.", ".yrrrry.", ".ryrryr.", ".rryyrr.", ".rryyrr.", ".rrrrrr.", ".rrrrrr.", ".rrrrrr."),
    "hardhat": ("........", "........", "...yy...", ".yyyyyy.", ".yyoyyy.", ".yyoyyy.", "yyyyyyyy", "........"),
}

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


@lru_cache(maxsize=None)
def icon(name: str) -> Image.Image:
    """The 8x8 RGBA image for ``name`` (KeyError if there's no such icon)."""
    rows = ICONS[name]
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != ".":
                px[x, y] = PALETTE[ch] + (255,)
    return img
