"""Three looks of the same reading: ticker, overview and forecast."""
from __future__ import annotations

from bongo.fonts import align_x
from bongo.module import Style

from . import data
from .look import icon, label, sky, temp_text


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class _Weather(Style):
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

    def marquee(self, img, font, text, baseline, now, colour, speed=None):
        speed = self.settings["scroll_speed"] if speed is None else speed
        period = font.width(text) + 24
        x = self.ctx.width - int(now.timestamp() * speed) % period
        while x < self.ctx.width:
            font.draw(img, x, baseline, text, colour)
            x += period


class Ticker(_Weather):
    """v2 Weather: icon and temperature on top, everything else scrolls underneath."""
    defaults = {"scroll_speed": 22, "temperature_font": "6x13B"}
    choices = {"temperature_font": ["6x13B", "7x13B", "6x10", "5x7"]}

    def draw(self, r, now):
        img = self.backdrop(r.code, r.is_day)
        self.place_icon(img, r.code, r.is_day, 16, 1, 1, now)
        self.temp_font.draw(img, 20, 11, temp_text(r.temp), self.TEXT)
        self.tiny.draw(img, 20, 18, self.settings["location"].upper(), self.DIM)
        unit = "km/h" if self.settings["wind_unit"] == "kmh" else self.settings["wind_unit"]
        ticker = (f"{label(r.code, r.is_day)}  ·  Feels {temp_text(r.feels)}  ·  "
                  f"H {temp_text(r.high)} L {temp_text(r.low)}  ·  Wind {round(r.wind)} {unit}  ·  "
                  f"Humidity {r.humidity}%")
        self.marquee(img, self.small, ticker.replace("·", "-"), self.ctx.height - 3, now, self.ACCENT)
        return img


class Overview(_Weather):
    """Current conditions at a glance: big icon, big temperature, condition, high/low."""
    defaults = {"scroll_speed": 18, "temperature_font": "10x20"}
    choices = {"temperature_font": ["10x20", "9x18B", "7x13B", "6x13B"]}

    def draw(self, r, now):
        img = self.backdrop(r.code, r.is_day)
        self.place_icon(img, r.code, r.is_day, 20, 0, 0, now)
        temp = temp_text(r.temp)
        self.temp_font.draw(img, align_x("right", self.temp_font.width(temp), self.ctx.width, 1), 15, temp, self.TEXT)
        h = self.ctx.height
        name = label(r.code, r.is_day).upper()
        font = next((f for f in (self.small, self.tiny) if f.width(name) <= self.ctx.width - 2), None)
        if font:
            font.draw(img, 1, h - 7, name, self.ACCENT)
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
            x = -(int(now.timestamp() * speed) % period)  # seamless: a repeat follows 16 px behind
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
        elif int(now.timestamp() // 4) % 2 == 0:
            tiny.draw(img, 1, baseline, high_low, self.DIM)
        elif tiny.width(town) <= w - 2:
            tiny.draw(img, align_x("right", tiny.width(town), w, 1), baseline, town, self.DIM)
        else:
            self.marquee(img, tiny, town, baseline, now, self.DIM)


class Forecast(_Weather):
    """v2 OpenWeather Forecast: the next few hours as columns of time, icon and temperature."""
    defaults = {"columns": 4, "step_hours": 3, "temperature_font": "5x7"}
    choices = {"temperature_font": ["5x7", "tom-thumb"]}

    def draw(self, r, now):
        step = self.settings["step_hours"]
        cols = max(1, min(self.settings["columns"], self.ctx.width // 15))  # keep columns readable
        img = self.backdrop(r.code, r.is_day)
        here = now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
        upcoming = [h for h in r.hours if h.time >= here][::step][:cols]
        width = self.ctx.width // max(1, cols)
        for i, hour in enumerate(upcoming):
            x0 = i * width
            if i:
                for y in range(4, self.ctx.height - 4):
                    img.putpixel((x0, y), tuple(c // 3 for c in img.getpixel((x0, y))))
            stamp = "NOW" if i == 0 else hour.time.strftime("%H")
            self.tiny.draw(img, x0 + (width - self.tiny.width(stamp)) // 2 + 1, 6, stamp, self.DIM)
            self.place_icon(img, hour.code, hour.is_day, 11, x0 + (width - 11) // 2 + 1, 8, now)
            t = temp_text(hour.temp)
            self.temp_font.draw(img, x0 + (width - self.temp_font.width(t)) // 2 + 1, self.ctx.height - 4, t, self.TEXT)
        return img
