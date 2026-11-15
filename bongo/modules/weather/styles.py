"""Three looks of the same reading: ticker, overview and forecast."""
from __future__ import annotations

from bongo.fonts import align_x
from bongo.module import Style
from bongo.scroll import HOLD, Scrolls

from . import data
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

    def backdrop(self, code, is_day):
        """The weather's own sky (the default), or a plain colour."""
        if self.settings["sky_background"]:
            return sky(code, is_day, self.ctx.width, self.ctx.height)
        return self.ctx.canvas(rgb(self.settings["background"]))

    def render(self, now):
        reading = data.latest(self.settings)
        if reading is None:
            offline = data.failing(self.settings)
            if offline:   # nothing to show and no way to get it: say so, briefly, and let the next screen on
                self.since = getattr(self, "since", None) or now
                self.finished = (now - self.since).total_seconds() >= self.OFFLINE_SECONDS
            return self.waiting(now, offline)
        self.since = None
        return self.draw(reading, now)

    OFFLINE_SECONDS = 5

    def waiting(self, now, offline=False):
        """No reading yet: a cloud and WEATHER... while it's being fetched, OFFLINE when it can't be."""
        w, h = self.ctx.width, self.ctx.height
        img = self.backdrop(3, True)
        font = self.small if self.small.width("WEATHER...") <= w else self.ctx.fonts("tom-thumb")
        top, baseline = max(0, 3 - (32 - h + 1) // 2), h - 5     # the cloud 3 rows down, the word 5 up, on a full panel
        if top + 14 + 1 <= baseline - font.cap:
            cloud = icon(3, True, 14, now.timestamp())
            img.paste(cloud, ((w - 14) // 2, top), cloud)
        else:                                                   # no room for both: the word alone, centred
            baseline = (h + font.cap) // 2
        if offline:
            font.draw(img, (w - font.width("OFFLINE") + 1) // 2, baseline, "OFFLINE", self.ACCENT)
            return img
        dots = "." * (int(now.timestamp() * 2) % 4)
        font.draw(img, (w - font.width("WEATHER") + 1) // 2, baseline, f"WEATHER{dots}", self.DIM)
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

    def bottom_line(self, img, high_low, baseline, now):
        """High/low on the left, the location on the right, never drawn over each other.
        With ``scroll_location`` (default) a location too long for its space scrolls there as a
        ticker; without it the town alone is shown, taking turns with high/low if need be."""
        w, tiny = self.ctx.width, self.tiny
        place = self.settings["location"].strip().upper()
        if self.settings["scroll_location"] and place:
            left = tiny.width(high_low) + 4
            tiny.draw(img, 1, baseline, high_low, self.DIM)
            if tiny.width(place) <= w - left - 1:
                tiny.draw(img, align_x("right", tiny.width(place), w, 1), baseline, place, self.DIM)
                return
            top = baseline - tiny.cap
            left += 3  # a gap and a thin divider, so high/low and the ticker read as two things
            for y in range(top, baseline + 1):
                img.putpixel((left - 3, y), (70, 80, 96))
            lane = img.crop((left, top, w, baseline + tiny.descent + 1))  # scroll only in the space to the right
            period, speed = tiny.width(place) + 16, max(1, self.settings["scroll_speed"])
            t = self.scrolled(("place", baseline, place), now, HOLD + period / speed)
            x = -(int(max(0.0, t - HOLD) * speed) % period)  # held at its start, then seamless: a repeat follows 16 px behind
            while x < lane.width:
                tiny.draw(lane, x, tiny.cap, place, self.DIM)
                x += period
            img.paste(lane, (left, top))
            return
        town = place.split(",")[0].strip()  # "CHELTENHAM, ENGLAND, GB" -> CHELTENHAM
        if not town or tiny.width(high_low) + 4 + tiny.width(town) <= w - 2:
            tiny.draw(img, 1, baseline, high_low, self.DIM)
            if town:
                tiny.draw(img, align_x("right", tiny.width(town), w, 1), baseline, town, self.DIM)
        else:   # taking turns: high/low for 4 s, then the town for 4 s or as long as it takes to scroll through
            speed = max(1, self.settings["scroll_speed"])
            reads = 4.0 if tiny.width(town) <= w - 2 else HOLD + (tiny.width(town) + 24) / speed
            t = self.scrolled(("turns", town), now, 4.0 + reads)
            turn, t = divmod(t, 4.0 + reads)
            if t < 4.0:
                tiny.draw(img, 1, baseline, high_low, self.DIM)
            elif tiny.width(town) <= w - 2:
                tiny.draw(img, align_x("right", tiny.width(town), w, 1), baseline, town, self.DIM)
            else:
                self.line(img, tiny, 1, baseline, w - 2, town, self.DIM, now, speed=speed, gap=24, key=("town", turn))


class Forecast(_Weather):
    """v2 OpenWeather Forecast: the next few hours as columns of time, icon and temperature."""
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

    def draw(self, r, now):
        step = self.settings["step_hours"]
        cols = max(1, min(self.settings["columns"], self.ctx.width // 15))  # keep columns readable
        img = self.backdrop(r.code, r.is_day)
        here = now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
        upcoming = [h for h in r.hours if h.time >= here][::step][:cols]
        width = self.ctx.width // max(1, cols)
        stamp_at, icon_at, temp_at, size = self.rows()
        for i, hour in enumerate(upcoming):
            x0 = i * width
            if i:
                for y in range(4, self.ctx.height - 4):
                    img.putpixel((x0, y), tuple(c // 3 for c in img.getpixel((x0, y))))
            stamp = "NOW" if i == 0 else hour.time.strftime("%H")
            self.tiny.draw(img, x0 + (width - self.tiny.width(stamp)) // 2 + 1, stamp_at, stamp, self.DIM)
            if size:
                self.place_icon(img, hour.code, hour.is_day, size, x0 + (width - size) // 2 + 1, icon_at, now)
            t = temp_text(hour.temp)
            self.temp_font.draw(img, x0 + (width - self.temp_font.width(t)) // 2 + 1, temp_at, t, self.TEXT)
        return img
