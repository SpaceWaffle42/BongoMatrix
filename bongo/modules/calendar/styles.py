"""Calendar styles. Everything is drawn from ``now`` alone, so frames are reproducible."""
from __future__ import annotations

import calendar as cal
import datetime as dt
import math

import numpy as np
from PIL import Image, ImageDraw

from bongo import holidays, icons
from bongo.module import Style


def rgb(value: str) -> tuple:
    h = str(value).lstrip("#")
    if len(h) != 6:
        raise ValueError(f"calendar: bad colour {value!r}, expected #RRGGBB")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


PAST = 0.45    # a day or letter that has gone by: dimmer, but still read at low brightness
FAINT = 0.3    # outside this month
BLOCK = 0.25   # a plain square for a day or week not in play


def scale(colour, k):
    return tuple(int(c * k) for c in colour)


class _Base(Style):
    def setup(self):
        s = self.settings
        self.ink, self.accent = rgb(s["text_colour"]), rgb(s["accent"])
        self.event_ink, self.bg = rgb(s["event_colour"]), rgb(s["background"])
        self.tiny, self.small = self.ctx.fonts("tom-thumb"), self.ctx.fonts("5x7")
        self.dates = []  # (spec or one-off date, title, is_own_event, icon name or None)
        for hid in s["holidays"]:
            self.dates.append((hid, holidays.label(hid), False, holidays.RULES[hid].icon))
        for event in s["events"]:                             # checked by the module (countdown/events.py)
            if event.get("holiday"):
                spec = event["holiday"]
            elif event.get("repeat") == "yearly":
                day = dt.date.fromisoformat(str(event["date"])[:10])
                spec = f"{day:%m-%d}"
            else:
                spec = dt.date.fromisoformat(str(event["date"])[:10])
            mark = event.get("icon") if event.get("icon") in icons.ICONS else None
            if mark is None and event.get("holiday") in holidays.RULES and event.get("icon") != "none":
                mark = holidays.RULES[event["holiday"]].icon
            self.dates.append((spec, str(event["title"]).strip(), True, mark))

    def occurrence(self, spec, after: dt.date) -> dt.date | None:
        if isinstance(spec, dt.date):
            return spec if spec >= after else None
        return holidays.next_date(spec, after)

    def upcoming(self, today: dt.date, count: int):
        found = [(d, title, own, mark) for spec, title, own, mark in self.dates if (d := self.occurrence(spec, today))]
        return sorted(found, key=lambda x: (x[0], not x[2]))[:count]

    def marked(self, year: int, month: int) -> set[int]:
        """Days of this month with a holiday or event."""
        first = dt.date(year, month, 1)
        days = set()
        for spec, *_ in self.dates:
            d = self.occurrence(spec, first)
            if d and (d.year, d.month) == (year, month):
                days.add(d.day)
        return days

    def fit(self, font, text, width):
        """Trim ``text`` until it fits ``width`` px."""
        while text and font.width(text) > width:
            text = text[:-1]
        return text.rstrip()


def digits_width(font, text: str) -> int:
    """Width of ``text`` drawn by ``tight``: each digit trimmed to its ink, 1 px apart."""
    return sum(_ink(font, ch)[2] - _ink(font, ch)[0] for ch in text) + len(text) - 1


def _ink(font, ch):
    m = Image.new("L", (font.width(ch) + 2, 20))
    font.draw(m, 0, 15, ch, 255)
    return m.getbbox() or (0, 0, font.width(ch), 0)


def tight(img, font, x, baseline, text, ink) -> int:
    """Numbers drawn digit by digit, trimmed to their ink with a 1 px gap (so "11" isn't "1 1")."""
    for ch in text:
        box = _ink(font, ch)
        font.draw(img, x - box[0], baseline, ch, ink)
        x += box[2] - box[0] + 1
    return x


def when(days: int) -> str:
    return "TODAY" if days == 0 else "TOMORROW" if days == 1 else f"{days} DAYS"


class Page(_Base):
    """A desk-calendar page: weekday, big date and month on the left; what's next on the right."""
    fps = 12

    def marquee(self, img, x, baseline, width, text, ink, now):
        """Draw ``text`` in a ``width``-px window, scrolling it (pausing at the start) if it's too long."""
        tw = self.tiny.width(text)
        if tw <= width:
            self.tiny.draw(img, x, baseline, text, ink)
            return
        travel = tw - width + 4
        cycle = travel / 10 + 4  # 10 px/s, then a 2 s hold at each end
        t = now.timestamp() % cycle
        offset = int(min(travel, max(0.0, (t - 2) * 10)))
        mask = Image.new("L", (width, img.height))
        self.tiny.draw(mask, -offset, baseline, text, 255)
        img.paste(ink, (x, 0, x + width, img.height), mask)
    @staticmethod
    def lit_rows(font, text):
        """(first, last) lit row of ``text``, counted from the row its baseline is drawn on (so first is negative)."""
        scratch = Image.new("L", (font.width(text) + 2, font.cap + 16))
        font.draw(scratch, 0, font.cap + 8, text, 255)
        box = scratch.getbbox() or (0, 8, 1, font.cap + 8)
        return box[1] - font.cap - 8, box[3] - 1 - font.cap - 8

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        today = now.date()
        big = next(f for f in map(self.ctx.fonts, ("10x20", "6x13B", "5x7"))
                   if self.small.cap * 2 + f.cap + 4 <= h or f.cap <= 7)
        weekday, day, month = f"{today:%a}".upper(), str(today.day), f"{today:%b}".upper()
        lines = [(self.small, weekday, self.accent), (big, day, self.ink)]
        if self.small.cap * 2 + big.cap + 4 <= h:
            lines.append((self.small, month, scale(self.ink, 0.6)))
        left = max(f.width(t) for f, t, _ in lines)
        # stacked by their lit rows, not their fonts' nominal heights: the big digits have blank rows above
        # them, which used to push the weekday onto the panel's top row and the month onto its last
        spans = [self.lit_rows(f, t) for f, t, _ in lines]
        block = sum(bottom - top + 1 for top, bottom in spans) + 2 * (len(lines) - 1)
        y = (h - block) // 2
        for (font, text, ink), (top, bottom) in zip(lines, spans):
            font.draw(img, 1 + (left - font.width(text)) // 2, y - top, text, ink)
            y += bottom - top + 1 + 2
        x0 = left + 5
        room = w - x0 - 1
        if room < 16:
            return img
        ImageDraw.Draw(img).line([(left + 2, 2), (left + 2, h - 3)], fill=scale(self.ink, 0.18))
        pitch = self.tiny.cap + 2
        count = max(1, (h + 4) // (pitch * 2 + 2))  # n items take n*(2*pitch+2) - 4 rows
        items = self.upcoming(today, count)
        if not items:
            self.tiny.draw(img, x0, (h + self.tiny.cap) // 2, self.fit(self.tiny, "NOTHING SOON", room), scale(self.ink, 0.5))
            return img
        block = len(items) * pitch * 2 + (len(items) - 1) * 2 - 2
        y = (h - block) // 2 + self.tiny.cap
        with_icons = self.settings["icons"] and room >= 30 and any(mark for *_, mark in items)
        tx, troom = (x0 + 10, room - 10) if with_icons else (x0, room)
        for d, title, own, mark in items:
            if with_icons and mark:  # centred on the item's two lines
                tile = icons.icon(mark)
                img.paste(tile, (x0, y - self.tiny.cap + (pitch + self.tiny.cap - 8) // 2), tile)
            self.marquee(img, tx, y, troom, title.upper(), self.event_ink if own else self.ink, now)
            self.tiny.draw(img, tx, y + pitch, self.fit(self.tiny, when((d - today).days), troom), self.accent)
            y += pitch * 2 + 2
        return img


class Month(_Base):
    """The month as a grid of lit blocks: past days dim, today glowing, holidays and events coloured."""
    fps = 8

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        today = now.date()
        first = 0 if self.settings["week_start"] == "monday" else 6
        weeks = cal.Calendar(first).monthdayscalendar(today.year, today.month)
        name = f"{today:%B}".upper()
        right = f"{today:%a} {today.day}".upper()
        if self.tiny.width(name) + self.tiny.width(right) + 4 > w:
            name = f"{today:%b}".upper()
        top = self.tiny.cap + 1
        self.tiny.draw(img, 1, top, name, self.ink)
        self.tiny.draw(img, w - 1 - self.tiny.width(right), top, right, self.accent)
        grid_top = top + 2
        cw, ch = w // 7, max(2, (h - grid_top) // len(weeks))
        x_off = (w - cw * 7) // 2 + 1
        y_off = grid_top + (h - grid_top - ch * len(weeks)) // 2
        marked = self.marked(today.year, today.month)
        pulse = 0.75 + 0.25 * math.sin(now.timestamp() * 3)
        weekend = {5, 6} if first == 0 else {0, 6}  # column indexes of Sat/Sun
        draw = ImageDraw.Draw(img)
        for r, week in enumerate(weeks):
            for c, day in enumerate(week):
                if not day:
                    continue
                if day == today.day:
                    ink = scale(self.accent, pulse)
                elif day in marked:
                    ink = scale(self.event_ink, PAST if day < today.day else 0.9)
                elif day < today.day:
                    ink = scale(self.ink, BLOCK)
                else:
                    ink = scale(self.ink, 0.34 if c in weekend else 0.5)
                x, y = x_off + c * cw, y_off + r * ch
                draw.rectangle([x, y, x + cw - 2, y + ch - 2], fill=ink)
        return img


class Progress(_Base):
    """Year, month, week and day as bars with percentages."""
    fps = 2
    defaults = dict(bars=["year", "month", "week", "day"])
    choices = {"bars": ["year", "month", "week", "day"]}
    basic = ("bars",)

    def fractions(self, now):
        start_day = dt.datetime.combine(now.date(), dt.time(), tzinfo=now.tzinfo)
        day_secs = (now - start_day).total_seconds()
        year_start = start_day.replace(month=1, day=1)
        year_len = 366 if cal.isleap(now.year) else 365
        month_len = cal.monthrange(now.year, now.month)[1]
        shift = 0 if self.settings["week_start"] == "monday" else 1
        weekday = (now.weekday() + shift) % 7
        return {
            "year": ((start_day - year_start).days * 86400 + day_secs) / (year_len * 86400),
            "month": ((now.day - 1) * 86400 + day_secs) / (month_len * 86400),
            "week": (weekday * 86400 + day_secs) / (7 * 86400),
            "day": day_secs / 86400,
        }

    def labels(self, now):
        return {"year": str(now.year), "month": f"{now:%b}".upper(),
                "week": f"WK{now.isocalendar()[1]}", "day": f"{now:%a}".upper()}

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        wanted = [b for b in ("year", "month", "week", "day") if b in self.settings["bars"]]
        pitch_min = self.tiny.cap + 2
        while wanted and len(wanted) * pitch_min > h + 2:
            wanted.remove("week" if "week" in wanted else wanted[-1])
        if not wanted:
            return img
        pitch = min(9, (h + 2) // len(wanted))
        frac, names = self.fractions(now), self.labels(now)
        label_w = max(self.tiny.width(names[b]) for b in wanted)
        pct_w = self.tiny.width("100%")
        x0, x1 = label_w + 3, w - pct_w - 3
        y = (h - (len(wanted) * pitch - (pitch - self.tiny.cap))) // 2 + self.tiny.cap
        draw = ImageDraw.Draw(img)
        for bar in wanted:
            f = min(1.0, max(0.0, frac[bar]))
            self.tiny.draw(img, 1, y, names[bar], self.ink)
            pct = f"{int(f * 100)}%"
            self.tiny.draw(img, w - 1 - self.tiny.width(pct), y, pct, self.accent)
            if x1 - x0 >= 4:
                top = y - self.tiny.cap + 1
                draw.rectangle([x0, top, x1, top + 2], fill=scale(self.accent, 0.16))
                filled = f * (x1 - x0 + 1)
                whole = int(filled)
                if whole:
                    draw.rectangle([x0, top, x0 + whole - 1, top + 2], fill=self.accent)
                if whole <= x1 - x0 and filled > whole:  # anti-aliased end pixel
                    k = 0.16 + 0.84 * (filled - whole)
                    draw.rectangle([x0 + whole, top, x0 + whole, top + 2], fill=scale(self.accent, k))
            y += pitch
        return img


# -- Today: three more ways to show the date ------------------------------------------------------

class Desk(_Base):
    """Desk calendar: a ring-bound page with the month on a coloured band and the big date."""
    fps = 2

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        d = ImageDraw.Draw(img)
        today = now.date()
        pw = min(w - 2, 40)
        x0 = (w - pw) // 2
        band = max(7, h // 4)
        d.rectangle([x0, 2, x0 + pw - 1, h - 1], fill=scale(self.ink, 0.3))          # the paper, gently lit
        d.rectangle([x0, 2, x0 + pw - 1, 2 + band], fill=scale(self.accent, 0.8))    # the month band
        for k in range(4):                                                           # binder rings
            rx = x0 + 5 + k * (pw - 10) // 3
            d.rectangle([rx - 1, 0, rx, 2], fill=scale(self.ink, 0.6))
        month = f"{today:%B}".upper()
        if self.tiny.width(month) > pw - 6:
            month = f"{today:%b}".upper()
        self.tiny.draw(img, x0 + (pw - self.tiny.width(month)) // 2, 2 + band - 1, month, self.bg)
        big = next((f for f in map(self.ctx.fonts, ("10x20", "6x13B", "5x7")) if f.cap + 9 <= h - band - 3), self.small)
        day = str(today.day)
        base = 2 + band + 2 + big.cap
        tight(img, big, x0 + (pw - digits_width(big, day)) // 2, base, day, self.ink)
        weekday = f"{today:%A}".upper()
        if self.tiny.width(weekday) > pw - 2:
            weekday = f"{today:%a}".upper()
        if base + 2 + self.tiny.cap <= h - 1:
            self.tiny.draw(img, x0 + (pw - self.tiny.width(weekday)) // 2, base + 2 + self.tiny.cap, weekday,
                           self.accent)
        return img


class DateLine(_Base):
    """Date in words: the weekday over the date, written out, with today's holiday or your event
    (or the next one) underneath."""
    fps = 12

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        today = now.date()
        f610 = self.ctx.fonts("6x10")
        weekday = f"{today:%A}".upper()
        if f610.width(weekday) > w - 2:
            weekday = f"{today:%a}".upper()
        date = f"{today.day} {today:%B}".upper()
        if self.small.width(date) > w - 2:
            date = f"{today.day} {today:%b}".upper()
        items = self.upcoming(today, 1)
        # baselines of the weekday, the date and the line underneath: spread out with a rule on a full
        # panel, closed up in a shorter safe area, and just the two lines, centred, where three won't fit
        if items and h >= 30:
            first, second, third, rule = 9, 19, 30, 22
        elif items and h >= 24:
            first, second, third, rule = 8, 16, h, None
        else:
            items, third, rule = [], None, None
            first, second = (9, 19) if h >= 30 else ((h - 16) // 2 + 8, (h - 16) // 2 + 16)
        f610.draw(img, (w - f610.width(weekday)) // 2, first, weekday, self.accent)
        self.small.draw(img, (w - self.small.width(date)) // 2, second, date, self.ink)
        if items:
            d, title, own, _ = items[0]
            days = (d - today).days
            text = title.upper() if days == 0 else f"{title.upper()} - {when(days)}"
            if rule:
                ImageDraw.Draw(img).line([(8, rule), (w - 9, rule)], fill=scale(self.ink, 0.15))
            ink = self.event_ink if own else scale(self.ink, 0.7)
            if self.tiny.width(text) <= w - 2:
                self.tiny.draw(img, (w - self.tiny.width(text)) // 2, third, text, ink)
            else:
                Page.marquee(self, img, 0, third, w, text, ink, now)
        return img


class Week(_Base):
    """This week: seven columns with the day letter and date, today lit up, a dot under days with
    something on; the next date along the bottom."""
    fps = 10

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        d = ImageDraw.Draw(img)
        today = now.date()
        shift = 0 if self.settings["week_start"] == "monday" else 1
        start = today - dt.timedelta(days=(today.weekday() + shift) % 7)
        days = 7 if w >= 56 else 5                            # a small canvas shows today and two either side
        if days == 5:
            start = today - dt.timedelta(days=2)
        cw = w // days
        x_off = (w - cw * days) // 2
        marked = {}
        for spec, title, own, _ in self.dates:
            day = self.occurrence(spec, start)
            if day and 0 <= (day - start).days < days:
                marked[(day - start).days] = own or marked.get((day - start).days, False)
        for k in range(days):
            day = start + dt.timedelta(days=k)
            x = x_off + k * cw
            is_today = day == today
            if is_today:
                d.rectangle([x, 1, x + cw - 2, 15], fill=scale(self.accent, 0.75))
            ink = self.bg if is_today else (scale(self.ink, PAST) if day < today else self.ink)
            letter = f"{day:%a}"[0]
            self.tiny.draw(img, x + (cw - 1 - self.tiny.width(letter)) // 2, 7, letter, ink)
            num = str(day.day)
            tight(img, self.tiny, x + (cw - 1 - digits_width(self.tiny, num)) // 2, 13, num, ink)
            if k in marked and h >= 20:
                colour = self.event_ink if marked[k] else scale(self.ink, 0.6)
                d.rectangle([x + (cw - 1) // 2 - 1, 17, x + (cw - 1) // 2, 18], fill=colour)
        items = self.upcoming(today + dt.timedelta(days=1), 1)
        if items and h >= 30:
            dd, title, own, _ = items[0]
            text = f"{title.upper()} {dd:%a %d}".upper()
            ink = self.event_ink if own else scale(self.ink, 0.7)
            if self.tiny.width(text) <= w - 2:
                self.tiny.draw(img, (w - self.tiny.width(text)) // 2, 29, text, ink)
            else:
                Page.marquee(self, img, 0, 29, w, text, ink, now)
        return img


# -- Month: three more ---------------------------------------------------------------------------

class Numbers(_Base):
    """Numbers: the month name over a calendar grid of day numbers for the weeks around today,
    today's number lit and underlined, your dates coloured."""
    fps = 2

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        if w < 56 or h < 26:                                  # too small for seven columns of numbers
            return Dots.render(self, now)
        img = self.ctx.canvas(self.bg)
        d = ImageDraw.Draw(img)
        today = now.date()
        first = 0 if self.settings["week_start"] == "monday" else 6
        shift = 0 if first == 0 else 1
        monday = today - dt.timedelta(days=(today.weekday() + shift) % 7)
        rows = max(1, min(4, (h - 7) // 6))                   # last week, this week and what's next
        start = monday - dt.timedelta(days=7 if rows >= 3 else 0)
        marked = set()
        for spec, *_ in self.dates:
            day = self.occurrence(spec, start)
            if day and (day - start).days < rows * 7:
                marked.add(day)
        title = f"{today:%B %Y}".upper() if self.tiny.width(f"{today:%B %Y}") <= w - 2 else f"{today:%b}".upper()
        self.tiny.draw(img, (w - self.tiny.width(title)) // 2, 6, title, self.accent)
        cw = w // 7
        x_off = (w - cw * 7) // 2 + 1
        top = 7 + (h - 7 - rows * 6) // 2 + 6
        for r in range(rows):
            for c in range(7):
                day = start + dt.timedelta(days=r * 7 + c)
                text = str(day.day)
                tw = digits_width(self.tiny, text)
                x = x_off + c * cw + (cw - 1 - tw) // 2
                y = top + r * 6
                if day == today:                              # a soft orange backing, the number bright on it
                    d.rectangle([x - 1, y - self.tiny.cap, x + tw, y], fill=scale(self.accent, 0.35))
                    ink = (255, 255, 255)
                elif day in marked:
                    ink = self.event_ink if day > today else scale(self.event_ink, 0.45)
                elif day.month != today.month:
                    ink = scale(self.ink, FAINT)
                else:
                    ink = scale(self.ink, PAST if day < today else 0.85)
                tight(img, self.tiny, x, y, text, ink)
        return img


class Dots(_Base):
    """Dots: the month name and big date on the left, the month as a grid of dots on the right."""
    fps = 8

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        d = ImageDraw.Draw(img)
        today = now.date()
        first = 0 if self.settings["week_start"] == "monday" else 6
        weeks = cal.Calendar(first).monthdayscalendar(today.year, today.month)
        marked = self.marked(today.year, today.month)
        pitch = 4 if len(weeks) * 4 <= h else 3            # 2x2 dots on a small canvas
        gw, gh = 7 * pitch - 1, len(weeks) * pitch - 1
        gx = w - gw - 2
        gy = (h - gh) // 2
        pulse = 0.7 + 0.3 * math.sin(now.timestamp() * 3)
        for r, week in enumerate(weeks):
            for c, day in enumerate(week):
                if not day:
                    continue
                x, y = gx + c * pitch, gy + r * pitch
                if day == today.day:
                    ink = scale(self.accent, pulse)
                elif day in marked:
                    ink = self.event_ink if day > today.day else scale(self.event_ink, PAST)
                else:
                    ink = scale(self.ink, BLOCK if day < today.day else 0.5)
                d.rectangle([x, y, x + pitch - 2, y + pitch - 2], fill=ink)
        left = gx - 2
        month = f"{today:%b}".upper()
        big = self.ctx.fonts("10x20") if left >= 22 and h >= 28 else self.small
        day = str(today.day)
        top = (h - (self.small.cap + 4 + big.cap)) // 2 + self.small.cap
        self.small.draw(img, (left - self.small.width(month)) // 2, top, month, self.accent)
        big.draw(img, (left - big.width(day)) // 2, top + 4 + big.cap, day, self.ink)
        return img


class Year(_Base):
    """Year: all twelve months as tiny grids, a pixel a day: gone days dim, today blinking, your
    dates and holidays lit."""
    fps = 4

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        today = now.date()
        first = 0 if self.settings["week_start"] == "monday" else 6
        cols, mw, mh = 6, 7, 6
        months = range(1, 13) if h >= 24 else range(1 + (today.month - 1) // 6 * 6, 7 + (today.month - 1) // 6 * 6)
        gapx = max(1, (w - cols * mw) // (cols + 1))
        rows = len(months) // cols
        gapy = max(1, (h - rows * (mh + 4)) // (rows + 1))
        px = img.load()
        marked = {m: self.marked(today.year, m) for m in range(1, 13)}
        blink = int(now.timestamp() * 2) % 2 == 0
        for k, m in enumerate(months):
            col, row = k % cols, k // cols
            ox = gapx + col * (mw + gapx)
            oy = gapy + row * (mh + 4 + gapy) + 5
            self.tiny.draw(img, ox, oy - 1, f"{dt.date(2000, m, 1):%b}"[0], self.accent if m == today.month else scale(self.ink, PAST))
            for r, week in enumerate(cal.Calendar(first).monthdayscalendar(today.year, m)):
                for c, day in enumerate(week):
                    if not day or r >= mh:
                        continue
                    date = dt.date(today.year, m, day)
                    x, y = ox + c, oy + r
                    if not (0 <= x < w and 0 <= y < h):
                        continue
                    if date == today:
                        px[x, y] = self.accent if blink else self.bg
                    elif day in marked[m]:
                        px[x, y] = scale(self.event_ink, 0.5 if date < today else 1.0)
                    else:
                        px[x, y] = scale(self.ink, BLOCK if date < today else 0.45)
        return img


# -- Progress: three more ------------------------------------------------------------------------

class Rings(Progress):
    """Rings: year, month, week and day as rings filling round, the percentages beside them."""
    fps = 2
    COLOURS = ((255, 181, 71), (90, 200, 250), (120, 220, 140), (232, 121, 183))

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        S = 4
        size = h - 2                                          # a pixel clear of the edges
        big = Image.new("RGB", (size * S, size * S), self.bg)
        d = ImageDraw.Draw(big)
        frac, names = self.fractions(now), self.labels(now)
        bars = [b for b in ("year", "month", "week", "day") if b in self.settings["bars"]] or ["year"]
        c = size * S / 2
        bars = bars[:3] if size < 24 else bars                # three rings on a small canvas, each big enough to read
        step = 14 if size >= 24 else 10
        for k, bar in enumerate(bars):
            r = c - 4 - k * step
            if r < 8:
                break
            colour = self.COLOURS[k % 4]
            box = [c - r, c - r, c + r, c + r]
            d.arc(box, 0, 360, fill=scale(colour, 0.18), width=9)
            f = min(1.0, max(0.0, frac[bar]))
            if f > 0:
                d.arc(box, -90, -90 + 360 * f, fill=colour, width=9)
        img = self.ctx.canvas(self.bg)
        ring = big.resize((size, size), Image.LANCZOS)
        a = np.asarray(ring, float)
        lit = a.max(-1, keepdims=True)                        # no dim speckle: faint pixels off, the rest at least 30
        a = np.where(lit < 18, 0, a * np.maximum(1, 30 / np.maximum(lit, 1)))
        ring = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
        mask = Image.fromarray(((a.max(-1) > 0) * 255).astype(np.uint8))   # unlit dots stay as they were
        legend = 30 if w - size - 3 >= 30 else 14
        x0 = (w - size - 2 - legend) // 2
        img.paste(ring, (x0, 1), mask)
        pitch = 7 if h >= 28 else 6
        y = (h - len(bars) * pitch) // 2 + self.tiny.cap + 1
        for k, bar in enumerate(bars):
            pct = f"{int(min(1.0, frac[bar]) * 100)}%"
            lx = x0 + size + 2
            if legend >= 30:
                self.tiny.draw(img, lx, y, names[bar], self.COLOURS[k % 4])
                self.tiny.draw(img, lx + legend - self.tiny.width(pct), y, pct, self.ink)
            else:
                self.tiny.draw(img, lx, y, pct, self.COLOURS[k % 4])
            y += pitch
        return img


class Weeks(Progress):
    """Weeks: the year as 52 squares, one a week, lit up to this one, which fills as it goes; the
    day of the year underneath."""
    fps = 2

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        d = ImageDraw.Draw(img)
        frac = self.fractions(now)["year"]
        cols, rows = 13, 4
        pitch = max(3, min((w - 2) // cols, (h - 9) // rows))
        gx = (w - cols * pitch + 1) // 2
        block = rows * pitch - 1 + 3 + self.tiny.cap
        gy = (h - block) // 2
        weeks_done = frac * 52
        pulse = 0.75 + 0.25 * math.sin(now.timestamp() * 3)
        for k in range(52):
            x, y = gx + (k % cols) * pitch, gy + (k // cols) * pitch
            if k < int(weeks_done):
                ink = scale(self.accent, 0.6)                 # gone
            elif k == int(weeks_done):
                ink = scale(self.ink, pulse)                  # this week
            else:
                ink = scale(self.ink, BLOCK)
            d.rectangle([x, y, x + pitch - 2, y + pitch - 2], fill=ink)
        year_len = 366 if cal.isleap(now.year) else 365
        text = f"DAY {now.timetuple().tm_yday} OF {year_len}"
        if self.tiny.width(text) > w - 2:
            text = f"{now.timetuple().tm_yday}/{year_len}"
        self.tiny.draw(img, (w - self.tiny.width(text)) // 2, gy + rows * pitch + 2 + self.tiny.cap, text, self.ink)
        return img


class Percent(Progress):
    """Percent: one big number at a time, how much of the year (then month, week, day) has gone,
    with a thin bar beneath."""
    fps = 4
    HOLD = 5          # seconds each one shows

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas(self.bg)
        bars = [b for b in ("year", "month", "week", "day") if b in self.settings["bars"]] or ["year"]
        bar = bars[int(now.timestamp() // self.HOLD) % len(bars)]
        f = min(1.0, max(0.0, self.fractions(now)[bar]))
        big = self.ctx.fonts("10x20" if h >= 30 else "6x13B")
        base = 17 if h >= 30 else big.cap + 1
        num = f"{int(f * 100)}"
        pct_w = self.small.width("%")
        x = (w - big.width(num) - pct_w - 1) // 2
        big.draw(img, x, base, num, self.ink)
        self.small.draw(img, x + big.width(num) + 1, base, "%", self.accent)
        words = {"year": f"OF {now.year} GONE", "month": f"OF {now:%b} GONE".upper(),
                 "week": "OF WEEK GONE", "day": "OF TODAY GONE"}[bar]
        if self.tiny.width(words) > w - 2:
            words = {"year": str(now.year), "month": f"{now:%b}".upper(), "week": "WK", "day": "DAY"}[bar]
        self.tiny.draw(img, (w - self.tiny.width(words)) // 2, base + 8, words, scale(self.ink, 0.7))
        if h < 30:
            return img
        d = ImageDraw.Draw(img)
        d.rectangle([2, 29, w - 3, 30], fill=scale(self.accent, 0.18))
        filled = int(f * (w - 4))
        if filled:
            d.rectangle([2, 29, 1 + filled, 30], fill=self.accent)
        return img
