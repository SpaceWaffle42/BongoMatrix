"""Three looks of the same reading: ticker, overview and forecast. Each opens on OpenWeather's credit (credit.py)."""
from __future__ import annotations

import time

from PIL import Image

from bongo.fonts import align_x
from bongo.module import Style
from bongo.scroll import HOLD, Scrolls

from . import data
from .credit import Credit
from .look import icon, icon_size, label, sky, temp_text


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class _Weather(Scrolls, Style):
    fps = 20

    def setup(self):
        s = self.settings
        self.big = self.ctx.fonts("10x20")
        self.mid = self.ctx.fonts("6x13B")
        self.small = self.ctx.fonts("5x7")
        self.tiny = self.ctx.fonts("tom-thumb")
        self.temp_font = self.ctx.fonts(s["temperature_font"])
        self.TEXT, self.ACCENT, self.DIM = rgb(s["text_colour"]), rgb(s["accent"]), rgb(s["detail_colour"])
        self.began = self.shown_at = self.held = self.since = self.last = None   # (see render)
        self.with_credit, self.loaded, self._credit = True, False, None
        if not self.ctx.preview:
            data.latest(s)               # a fetch starts now if one is due (or a fresh try of a key refused before)
            if data.trouble():           # no key, or OpenWeather refuses it: skipped before a frame is drawn
                self.finished = True

    def backdrop(self, code, is_day):
        """The weather's own sky (the default), or a plain colour."""
        if self.settings["sky_background"]:
            return sky(code, is_day, self.ctx.width, self.ctx.height)
        return self.ctx.canvas(rgb(self.settings["background"]))

    clock = staticmethod(time.monotonic)   # the credit's own clock: a wall clock put right (or back) mustn't move it
    FADE = 0.4                             # s the credit takes to fade into the weather
    WEATHER_FOR = 5.0                      # s of weather a screen needs after its credit; less time than that: the weather straight away
    OFFLINE_SECONDS = 5

    def render(self, now):
        """The credit (also the loading screen) for one full pass and while the weather is fetched, then
        the weather, faded in. With no API key, or one OpenWeather refuses, the screen hands over at once
        (in a preview: a card saying so)."""
        trouble = data.trouble()
        t = self.clock()
        if trouble:
            if self.ctx.preview:
                return self.needs_key(trouble)
            self.finished = True               # (refused while the credit was up: it hands over on the credit, not the card)
            return self.loading(t - self.began) if self.began is not None else self.needs_key(trouble)
        reading = data.latest(self.settings)
        if self.began is None:
            self.began = t
            left = None if self.hand_over_at is None else self.hand_over_at - now.timestamp()
            self.with_credit = not self.ctx.preview and (left is None or left >= self.credit.minimum + self.WEATHER_FOR - 0.01)
        T, before, self.last = t - self.began, self.last, t - self.began
        if self.shown_at is None:
            if reading is None:
                if data.failing(self.settings):   # nothing to show and no way to get it: say so, briefly, and let the next screen on
                    self.since = self.since or t
                    self.finished = t - self.since >= self.OFFLINE_SECONDS
                    return self.waiting(now, True)
                self.since = None
                return self.loading(T)
            hold = self.credit.handover(before, T) if self.with_credit else T
            if hold is None:
                return self.loading(T)
            self.shown_at = t
            self.held = hold if self.loaded else None
        if reading is None:          # (hours old since: nothing to show as the weather now)
            return self.loading(T)
        img = self.draw(reading, now)
        k = (t - self.shown_at) / self.FADE
        if self.held is not None and k < 1:   # the credit fades out as the weather comes in; its bar and cloud carry on
            img = Image.blend(self.credit.frame(T, hold=self.held), img, max(0.0, k))
        return img

    def loading(self, T):
        self.loaded = True
        return self.credit.frame(T)

    @property
    def credit(self):
        if self._credit is None:
            self._credit = Credit(self)
        return self._credit

    def needs_key(self, trouble):
        """A card for the dashboard's preview: what's missing (the panel skips the screen instead)."""
        w, h = self.ctx.width, self.ctx.height
        img = self.backdrop(3, True)
        says = ([["WEATHER", "NEEDS AN", "API KEY"], ["NEEDS", "API KEY"], ["NO KEY"]] if trouble == data.NO_KEY else
                [["API KEY", "NOT", "WORKING"], ["KEY NOT", "WORKING"], ["NO KEY"]])
        for lines in says:
            for font in (self.small, self.tiny):
                tall = len(lines) * font.cap + 2 * (len(lines) - 1)
                if tall <= h - 2 and all(font.width(x) <= w - 1 for x in lines):
                    y = (h - tall) // 2
                    for n, text in enumerate(lines):
                        y += font.cap
                        colour = self.DIM if (text == "WEATHER") else self.ACCENT
                        font.draw(img, max(1, (w - font.width(text) + 1) // 2), y, text, colour)
                        y += 2
                    return img
        return img

    def waiting(self, now, offline=False):
        """OFFLINE, with a cloud above it where there's room: the weather can't be fetched."""
        w, h = self.ctx.width, self.ctx.height
        img = self.backdrop(3, True)
        font = self.small if self.small.width("OFFLINE") <= w else self.ctx.fonts("tom-thumb")
        top, baseline = max(0, 3 - (32 - h + 1) // 2), h - 5     # the cloud 3 rows down, the word 5 up, on a full panel
        if top + 14 + 1 <= baseline - font.cap:
            cloud = icon(3, True, 14, now.timestamp())
            img.paste(cloud, ((w - 14) // 2, top), cloud)
        else:                                                   # no room for both: the word alone, centred
            baseline = (h + font.cap) // 2
        font.draw(img, (w - font.width("OFFLINE") + 1) // 2, baseline, "OFFLINE", self.ACCENT)
        return img

    def place_icon(self, img, reading_code, is_day, size, x, y, now):
        art = icon(reading_code, is_day, size, now.timestamp())
        img.paste(art, (x, y), art)

    def fit(self, font, rows, room=None):
        """``font``, or the biggest of the temperature fonts that fits in ``rows`` rows (and ``room`` columns
        for a temperature like 12°): text gets smaller only once the icon can't."""
        names = [font] + [self.ctx.fonts(n) for n in ("10x20", "9x18B", "7x13B", "6x13B", "6x10", "5x7", "tom-thumb")]
        sample = "-12°"
        for f in names:
            if f.cap <= rows and (room is None or f.width(sample) <= room) and f.cap <= font.cap:
                return f
        return self.tiny

    def marquee(self, img, font, text, baseline, now, colour, speed=None):
        """Across the whole width: starts at its first letter, holds, then goes round once before the screen moves on."""
        speed = self.settings["scroll_speed"] if speed is None else speed
        self.line(img, font, 1, baseline, self.ctx.width - 2, text, colour, now, speed=max(1, speed), gap=24)


class Ticker(_Weather):
    """v2 Weather: icon and temperature on top, everything else scrolls underneath."""
    defaults = {"scroll_speed": 22, "temperature_font": "6x13B"}
    choices = {"temperature_font": ["6x13B", "7x13B", "6x10", "5x7"]}

    def draw(self, r, now):
        """On a smaller screen (a safe area) the icon shrinks first, so it never runs into the ticker; then
        the location is left out, then the temperature takes a smaller font."""
        img = self.backdrop(r.code, r.is_day)
        lane = self.ctx.height - 3                          # the ticker's baseline
        room = lane - self.small.cap - 1                    # rows above the ticker, with one free row before it
        size = icon_size(room - 1, 16)                      # from row 1 down
        x = 1
        if size:
            self.place_icon(img, r.code, r.is_day, size, 1, 1, now)
            x = size + 4
        font = self.fit(self.temp_font, room, self.ctx.width - x)
        top = min(11, room)
        font.draw(img, x, top, temp_text(r.temp), self.TEXT)
        place = min(18, room)
        if place - self.tiny.cap >= top + 1:                # under the temperature, a free row between
            self.line(img, self.tiny, x, place, self.ctx.width - x - 1, self.settings["location"].upper(), self.DIM, now)
        unit = "km/h" if self.settings["wind_unit"] == "kmh" else self.settings["wind_unit"]
        ticker = (f"{label(r.code, r.is_day)}  ·  Feels {temp_text(r.feels)}  ·  "
                  f"H {temp_text(r.high)} L {temp_text(r.low)}  ·  Wind {round(r.wind)} {unit}  ·  "
                  f"Humidity {r.humidity}%")
        self.marquee(img, self.small, ticker.replace("·", "-"), lane, now, self.ACCENT)
        return img


class Overview(_Weather):
    """Current conditions at a glance: big icon, big temperature, condition, high/low."""
    defaults = {"scroll_speed": 18, "temperature_font": "10x20"}
    choices = {"temperature_font": ["10x20", "9x18B", "7x13B", "6x13B"]}

    def draw(self, r, now):
        """On a smaller screen (a safe area) the icon shrinks first, down to its smallest drawing, so it never
        runs into the words under it or the temperature beside it; only then does the text give way (the
        condition line goes, then the temperature takes a smaller font)."""
        img = self.backdrop(r.code, r.is_day)
        w, h = self.ctx.width, self.ctx.height
        temp = temp_text(r.temp)
        name = label(r.code, r.is_day).upper()
        under = h - 7 - self.small.cap                      # the condition line's top row
        widest = self.temp_font.width("-12°")
        gap = 0 if w >= 64 and h >= 32 else 1               # the full panel keeps its look: the big cloud rests on the line
        size = icon_size(under - gap, min(20, w - widest - 2))
        show_name = size is not None
        if size is None:                                    # no room for the icon over the condition: drop the line
            under = h - 1 - self.tiny.cap                   # the high/low row's top row
            size = icon_size(under - 1, min(20, w - widest - 2))
        baseline = min(15, under)
        font = self.fit(self.temp_font, baseline + 1, w - (size or 0) - 2)
        temp_w = font.width(temp)
        if size:
            self.place_icon(img, r.code, r.is_day, size, 0, 0, now)
        font.draw(img, align_x("right", temp_w, w, 1), baseline, temp, self.TEXT)
        if show_name:
            line = next((f for f in (self.small, self.tiny) if f.width(name) <= w - 2), None)
            if line:
                line.draw(img, 1, h - 7, name, self.ACCENT)
            else:
                self.marquee(img, self.small, name, h - 7, now, self.ACCENT)
        self.bottom_line(img, f"H{round(r.high)} L{round(r.low)}", h - 1, now)
        return img

    LANE = 20   # px (five letters): the narrowest a scrolling location may be beside high/low before they take turns

    def bottom_line(self, img, high_low, baseline, now):
        """High/low on the left, the location on the right, never drawn over each other.
        With ``scroll_location`` (default) a location too long for its space scrolls there as a
        ticker; without it the town alone is shown. Where there isn't room for both (a small
        screen, or a wide high/low such as H-12 L-24), they take turns."""
        w, tiny = self.ctx.width, self.tiny
        place = self.settings["location"].strip().upper()
        town = place if self.settings["scroll_location"] else place.split(",")[0].strip()  # "CHELTENHAM, ENGLAND, GB" -> CHELTENHAM
        left = tiny.width(high_low) + 4
        if not town and tiny.width(high_low) > w - 2:  # no town, and high/low too wide even alone: it scrolls
            self.line(img, tiny, 1, baseline, w - 2, high_low, self.DIM, now, speed=max(1, self.settings["scroll_speed"]), gap=24)
            return
        if not town or left + tiny.width(town) <= w - (1 if self.settings["scroll_location"] else 2):
            tiny.draw(img, 1, baseline, high_low, self.DIM)
            if town:
                tiny.draw(img, align_x("right", tiny.width(town), w, 1), baseline, town, self.DIM)
            return
        if self.settings["scroll_location"] and left + 3 + self.LANE <= w:
            top = baseline - tiny.cap
            tiny.draw(img, 1, baseline, high_low, self.DIM)
            left += 3  # a gap and a thin divider, so high/low and the ticker read as two things
            for y in range(top, baseline + 1):
                img.putpixel((left - 3, y), (70, 80, 96))
            lane = img.crop((left, top, w, baseline + tiny.descent + 1))  # scroll only in the space to the right
            period, speed = tiny.width(town) + 16, max(1, self.settings["scroll_speed"])
            t = self.scrolled(("place", baseline, town), now, HOLD + period / speed)
            x = -(int(max(0.0, t - HOLD) * speed) % period)  # held at its start, then seamless: a repeat follows 16 px behind
            while x < lane.width:
                tiny.draw(lane, x, tiny.cap, town, self.DIM)
                x += period
            img.paste(lane, (left, top))
            return
        # taking turns: each for 4 s, or as long as it takes to scroll through if it's wider than the screen
        speed = max(1, self.settings["scroll_speed"])

        def reads(text):
            return 4.0 if tiny.width(text) <= w - 2 else HOLD + (tiny.width(text) + 24) / speed

        t = self.scrolled(("turns", town), now, reads(high_low) + reads(town))
        turn, t = divmod(t, reads(high_low) + reads(town))
        text, right = (high_low, False) if t < reads(high_low) else (town, True)
        if tiny.width(text) <= w - 2:
            tiny.draw(img, align_x("right", tiny.width(text), w, 1) if right else 1, baseline, text, self.DIM)
        else:
            self.line(img, tiny, 1, baseline, w - 2, text, self.DIM, now, speed=speed, gap=24, key=(text, turn))


class Forecast(_Weather):
    """v2 OpenWeather Forecast: now and the next few 3-hour slots as columns of time, icon and temperature."""
    defaults = {"columns": 4, "step_hours": 3, "temperature_font": "5x7"}
    choices = {"temperature_font": ["5x7", "tom-thumb"]}

    def rows(self):
        """Where the time, icon and temperature go: as on a full panel if they fit; on a shorter screen (a safe
        area) the gaps close up first, and the icons are left out only if even then they'd touch the text."""
        h, cap = self.ctx.height, self.temp_font.cap
        for stamp_at, icon_at, temp_at in ((6, 8, h - 4), (5, 6, h - 1)):
            size = icon_size(temp_at - cap - icon_at - 1, 11)
            if size:
                return stamp_at, icon_at, temp_at, size
        return 5, 6, h - 1, None

    def columns(self, r):
        """How many columns: as set, as many as stay readable (15 px each)."""
        return max(1, min(self.settings["columns"], self.ctx.width // 15))

    def stamps(self, hours, width):
        """The times over the columns: 15, 18... Where the place's clock is off the hour (India: 20:30) they
        say so (8:30, 20:30) if every one fits its column with a pixel clear each side; if not, the hour
        alone (20), like the rest, rather than fewer columns."""
        whole = [h.time.strftime("%H") for h in hours]
        if all(h.time.minute == 0 for h in hours):
            return whole
        exact = [f"{h.time.hour}:{h.time.minute:02d}" for h in hours]
        return exact if all(self.tiny.width(t) - 1 <= width - 3 for t in exact) else whole

    def upcoming(self, r, now):
        """Now, then the forecast every ``step_hours``: OpenWeather's is every 3 hours, so the step is rounded
        to whole 3-hour slots (1 to 4 hours: each slot; 6: every other one...). Which slots are still to come
        goes by the place's own clock, not the Matrix's (a screen with its own place elsewhere)."""
        every = max(1, round(self.settings["step_hours"] / 3))
        here = r.here() or now.replace(tzinfo=None)
        later = [h for h in r.hours if h.time > here]
        return ([data.Hour(here, r.temp, r.code, r.is_day)] + later[every - 1::every])[:self.columns(r)]

    def draw(self, r, now):
        cols = self.columns(r)
        img = self.backdrop(r.code, r.is_day)
        upcoming = self.upcoming(r, now)
        width = self.ctx.width // max(1, cols)
        stamp_at, icon_at, temp_at, size = self.rows()
        font, temps = self.column_temps([h.temp for h in upcoming], width - 1)
        stamps = self.stamps(upcoming[1:], width)
        for i, hour in enumerate(upcoming):
            x0 = i * width
            if i:
                for y in range(4, self.ctx.height - 4):
                    img.putpixel((x0, y), tuple(c // 3 for c in img.getpixel((x0, y))))
            stamp = "NOW" if i == 0 else stamps[i - 1]
            self.tiny.draw(img, x0 + (width - self.tiny.width(stamp)) // 2 + 1, stamp_at, stamp, self.DIM)
            if size:
                self.place_icon(img, hour.code, hour.is_day, size, x0 + (width - size) // 2 + 1, icon_at, now)
            t = temps[i]
            if font.width(t) <= width - 1:
                font.draw(img, x0 + (width - font.width(t)) // 2 + 1, temp_at, t, self.TEXT)
            else:   # wider than its column even in the smallest font: it scrolls there rather than run into the next
                self.line(img, font, x0 + 1, temp_at, width - 1, t, self.TEXT, now, key=("temp", i, t))
        return img

    def column_temps(self, values, room):
        """The font and words for the row of temperatures, all alike: as chosen if every one fits its column
        (``room`` px), else without the degree sign (-22 not -22°), else in the smallest font. Without the
        sign, which reads as a break, bare numbers keep 2 px more to themselves, so neighbouring columns
        don't run together (-44-53 or 107110)."""
        for font, sign in ((self.temp_font, True), (self.temp_font, False), (self.tiny, False)):
            temps = [temp_text(v) if sign else temp_text(v).rstrip("°") for v in values]
            if all(font.width(t) <= (room if sign else room - 2) for t in temps):
                return font, temps
        return font, temps
