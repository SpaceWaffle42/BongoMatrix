"""Word: the time in words, to the nearest five minutes ("twenty five past seven")."""
from __future__ import annotations

import datetime as dt

from PIL import Image

from bongo.module import Style

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
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h), _rgb(self.settings["background"]))
        on = _rgb(self.settings["colour"])
        if w < GRID_W or h < GRID_H:   # the grid has no room to spare: a smaller safe area gets just its lit words
            words = fitted(lines(now), w, h, self.ctx.fonts("tom-thumb"))
            draw_lines(img, self.ctx.fonts, words, words, [on] * len(words))
            return img
        font = self.ctx.fonts("tom-thumb")
        lit = lit_cells(now)
        off = _rgb(self.settings["unlit_colour"])
        x0, y0 = (w - GRID_W) // 2, (h - GRID_H) // 2
        for r, row in enumerate(GRID):
            for c, ch in enumerate(row):
                font.draw(img, x0 + c * 4, y0 + 5 + r * 5, ch, on if (r, c) in lit else off)
        return img


GRID_W, GRID_H = 16 * 4 - 1, 6 * 5   # the letters' own size: 3 x 5 each, a column between, rows touching


def lines(now: dt.datetime) -> list[str]:
    minutes, link, hour = phrase(now)
    if link == "OCLOCK":
        return [hour, "O'CLOCK"]
    return [minutes, link, hour]


SHORT = {"FIVE": "5", "TEN": "10", "QUARTER": "15", "TWENTY": "20", "TWENTY FIVE": "25", "HALF": "30"}


def fitted(words: list[str], w: int, h: int, small) -> list[str]:
    """``words`` as they fit ``w`` x ``h``: where three lines of the small font have no room (a very small
    safe area), two lines with the minutes as a number: "25 TO" / "SEVEN"."""
    if len(words) == 3 and (h < 18 or small.width(words[0]) > w - 2):
        return [f"{SHORT[words[0]]} {words[1]}", words[2]]
    return words


def draw_lines(img, fonts, words, shown, colours) -> None:
    """Two or three centred lines, spaced to fit the canvas, all in one font: 5x7 letters where every
    line has the room, else the 3x5 font. ``shown``: how much of each line is typed so far."""
    w, h = img.size
    pitch = min(10 if len(words) == 3 else 12, h // len(words))
    big = fonts("5x7")
    font = big if pitch >= 8 and all(big.width(line) <= w - 2 for line in words) else fonts("tom-thumb")
    for i, (line, show, colour) in enumerate(zip(words, shown, colours)):
        baseline = (h - pitch * len(words)) // 2 + i * pitch + font.cap + min(2, (pitch - font.cap + 1) // 2)
        font.draw(img, (w - font.width(line)) // 2, baseline, show, colour)


class WordLines(Style):
    """Word lines: the time in words on up to three lines, typed in when it changes."""
    group = "Word"
    fps = 20
    defaults = {"minutes_colour": "#78C8FF", "link_colour": "#C8C8D2", "hour_colour": "#FFFFFF", "background": "#000000"}
    TYPE = 0.1    # seconds a letter

    def render(self, now):
        s = self.settings
        colours = [_rgb(s["minutes_colour"]), _rgb(s["link_colour"]), _rgb(s["hour_colour"])]
        img = Image.new("RGB", (self.ctx.width, self.ctx.height), _rgb(s["background"]))
        small = self.ctx.fonts("tom-thumb")
        words = fitted(lines(now), img.width, img.height, small)
        # when did these words start showing? (the phrase changes at x:x2:30, x:x7:30)
        since_ms = ((now.minute * 60 + now.second + 150) % 300) * 1000 + now.microsecond // 1000
        before = fitted(lines(now - dt.timedelta(milliseconds=since_ms + 1000)), img.width, img.height, small)
        typed = since_ms // round(self.TYPE * 1000)       # whole letters, every 100 ms exactly
        done, shown = 0, []
        for i, line in enumerate(words):
            if len(before) == len(words) and before[i] == line:
                shown.append(line)                           # an unchanged line stays put; only new words type in
            else:
                shown.append(line[:max(0, min(len(line), int(typed - done)))])
                done += len(line) + 2
        draw_lines(img, self.ctx.fonts, words, shown, colours if len(words) == 3 else [colours[0], colours[2]])
        return img


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
