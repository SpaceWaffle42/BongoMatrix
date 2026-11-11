"""Calendar styles. Everything is drawn from ``now`` alone, so frames are reproducible."""
from __future__ import annotations

import calendar as cal
import datetime as dt
import math

from PIL import Image, ImageDraw

from bongo import holidays, icons
from bongo.module import Style


def rgb(value: str) -> tuple:
    h = str(value).lstrip("#")
    if len(h) != 6:
        raise ValueError(f"calendar: bad colour {value!r}, expected #RRGGBB")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


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
        for i, event in enumerate(s["events"]):
            if not isinstance(event, dict) or not event.get("date") or not str(event.get("title", "")).strip():
                raise ValueError(f"calendar: events[{i}] needs a date and a title")
            when = str(event["date"]).strip()
            try:
                spec = dt.date.fromisoformat(when) if len(when) == 10 and when[4] == "-" else when
                if isinstance(spec, str):
                    holidays.next_date(spec, dt.date(2000, 1, 1))  # validates the spec now, not at 3 a.m.
            except ValueError:
                raise ValueError(f"calendar: events[{i}] has a date I can't read: {when!r} "
                                 "(use YYYY-MM-DD, MM-DD for every year, or a rule like last-mon-05)") from None
            mark = event.get("icon") if event.get("icon") in icons.ICONS else None
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
        block = sum(f.cap for f, _, _ in lines) + 2 * (len(lines) - 1)
        y = (h - block) // 2
        for font, text, ink in lines:
            y += font.cap
            font.draw(img, 1 + (left - font.width(text)) // 2, y, text, ink)
            y += 2
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
                    ink = scale(self.event_ink, 0.35 if day < today.day else 0.9)
                elif day < today.day:
                    ink = scale(self.ink, 0.12)
                else:
                    ink = scale(self.ink, 0.28 if c in weekend else 0.42)
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
