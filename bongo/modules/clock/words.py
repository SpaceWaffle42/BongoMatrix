"""Word: the time in words, to the nearest five minutes ("twenty five past seven")."""
from __future__ import annotations

import datetime as dt

from PIL import Image

from bongo.module import Style

from .common import scaled

W, H = 64, 32
HOUR_WORDS = ["TWELVE", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN", "ELEVEN"]
MINUTE_WORDS = {5: "FIVE", 10: "TEN", 15: "QUARTER", 20: "TWENTY", 25: "TWENTY FIVE", 30: "HALF"}


def phrase(now: dt.datetime) -> tuple[str, str, str]:
    """(minutes, "PAST"/"TO"/"", hour): "TWENTY FIVE", "PAST", "SEVEN"; on the hour ("", "", "SEVEN")
    and the minutes word is "O'CLOCK"... see lines(). Rounded to the nearest five minutes."""
    m = (now.minute * 60 + now.second + 150) // 300 * 5   # nearest 5 minutes
    hour = now.hour + (1 if m == 60 else 0)
    m %= 60
    if m == 0:
        return "", "OCLOCK", HOUR_WORDS[hour % 12]
    if m <= 30:
        return MINUTE_WORDS[m], "PAST", HOUR_WORDS[hour % 12]
    return MINUTE_WORDS[60 - m], "TO", HOUR_WORDS[(hour + 1) % 12]


# A 16 x 6 letter grid (tom-thumb, 4 px a letter) holding every word the phrases need
GRID = ["ITSQUARTERTWENTY",
        "FIVEHALFTENXPAST",
        "TOONETWOTHREESIX",
        "FOURFIVESEVENTEN",
        "EIGHTNINEELEVENX",
        "TWELVEAMOCLOCKPM"]
# where each word is: (row, column) of its first letter
PLACES = {"ITS": (0, 0), "QUARTER": (0, 3), "TWENTY": (0, 10), "FIVE_M": (1, 0), "HALF": (1, 4),
          "TEN_M": (1, 8), "PAST": (1, 12), "TO": (2, 0), "ONE": (2, 2), "TWO": (2, 5), "THREE": (2, 8),
          "SIX": (2, 13), "FOUR": (3, 0), "FIVE": (3, 4), "SEVEN": (3, 8), "TEN": (3, 13), "EIGHT": (4, 0),
          "NINE": (4, 5), "ELEVEN": (4, 9), "TWELVE": (5, 0), "OCLOCK": (5, 8)}


def lit_cells(now: dt.datetime) -> set:
    minutes, link, hour = phrase(now)
    words = ["ITS"] + [w + "_M" if w in ("FIVE", "TEN") else w for w in minutes.split()] + [link, hour]
    cells = set()
    for word in words:
        if not word:
            continue
        r, c = PLACES[word]
        cells.update((r, c + k) for k in range(len(word.split("_")[0])))
    return cells


class WordGrid(Style):
    """Word grid: a grid of letters where the words for the time light up."""
    group = "Word"
    fps = 2
    defaults = {"colour": "#FFD28C", "unlit_colour": "#221E1C", "background": "#000000"}
    basic = ("colour",)

    def render(self, now):
        img = Image.new("RGB", (W, H), _rgb(self.settings["background"]))
        font = self.ctx.fonts("tom-thumb")
        lit = lit_cells(now)
        on = _rgb(self.settings["colour"])
        off = _rgb(self.settings["unlit_colour"])
        for r, row in enumerate(GRID):
            for c, ch in enumerate(row):
                font.draw(img, c * 4, 6 + r * 5, ch, on if (r, c) in lit else off)
        return scaled(img, self.ctx.width, self.ctx.height)


def lines(now: dt.datetime) -> list[str]:
    minutes, link, hour = phrase(now)
    if link == "OCLOCK":
        return [hour, "O'CLOCK"]
    return [minutes, link, hour]


class WordLines(Style):
    """Word lines: the time in words on up to three lines, typed in when it changes."""
    group = "Word"
    fps = 20
    defaults = {"minutes_colour": "#78C8FF", "link_colour": "#C8C8D2", "hour_colour": "#FFFFFF", "background": "#000000"}
    TYPE = 0.1    # seconds a letter

    def render(self, now):
        s = self.settings
        colours = [_rgb(s["minutes_colour"]), _rgb(s["link_colour"]), _rgb(s["hour_colour"])]
        img = Image.new("RGB", (W, H), _rgb(s["background"]))
        small, f57 = self.ctx.fonts("tom-thumb"), self.ctx.fonts("5x7")
        words = lines(now)
        # when did these words start showing? (the phrase changes at x:x2:30, x:x7:30)
        since_ms = ((now.minute * 60 + now.second + 150) % 300) * 1000 + now.microsecond // 1000
        before = lines(now - dt.timedelta(milliseconds=since_ms + 1000))
        typed = since_ms // round(self.TYPE * 1000)       # whole letters, every 100 ms exactly
        pitch = 10 if len(words) == 3 else 12
        top = (H - pitch * len(words)) // 2 + 8
        done = 0
        for i, line in enumerate(words):
            font = f57 if f57.width(line) <= W - 2 else small
            if len(before) == len(words) and before[i] == line:
                show = line                                  # an unchanged line stays put; only new words type in
            else:
                show = line[:max(0, min(len(line), int(typed - done)))]
                done += len(line) + 2
            x = (W - font.width(line)) // 2
            font.draw(img, x, top + i * pitch, show, colours[i if len(words) == 3 else (0, 2)[i]])
        return scaled(img, self.ctx.width, self.ctx.height)


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
