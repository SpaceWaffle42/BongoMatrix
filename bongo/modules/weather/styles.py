"""Three looks of the same reading: ticker, overview and forecast."""
from __future__ import annotations

from bongo.fonts import align_x
from bongo.module import Style

from . import data
from .look import ACCENT, DIM, TEXT, icon, label, sky, temp_text


class _Weather(Style):
    fps = 20

    def setup(self):
        self.big = self.ctx.fonts("10x20")
        self.mid = self.ctx.fonts("6x13B")
        self.small = self.ctx.fonts("5x7")
        self.tiny = self.ctx.fonts("tom-thumb")

    def render(self, now):
        reading = data.latest(self.settings)
        if reading is None:
            return self.waiting(now)
        return self.draw(reading, now)

    def waiting(self, now):
        img = sky(3, True, self.ctx.width, self.ctx.height)
        cloud = icon(3, True, 14, now.timestamp())
        img.paste(cloud, (25, 3), cloud)
        dots = "." * (int(now.timestamp() * 2) % 4)
        text = f"WEATHER{dots}"
        self.small.draw(img, 15, 27, text, DIM)
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
    defaults = {"scroll_speed": 22}

    def draw(self, r, now):
        img = sky(r.code, r.is_day, self.ctx.width, self.ctx.height)
        self.place_icon(img, r.code, r.is_day, 16, 1, 1, now)
        self.mid.draw(img, 20, 11, temp_text(r.temp), TEXT)
        self.tiny.draw(img, 20, 18, self.settings["location"].upper(), DIM)
        unit = "km/h" if self.settings["wind_unit"] == "kmh" else self.settings["wind_unit"]
        ticker = (f"{label(r.code, r.is_day)}  ·  Feels {temp_text(r.feels)}  ·  "
                  f"H {temp_text(r.high)} L {temp_text(r.low)}  ·  Wind {round(r.wind)} {unit}  ·  "
                  f"Humidity {r.humidity}%")
        self.marquee(img, self.small, ticker.replace("·", "-"), self.ctx.height - 3, now, ACCENT)
        return img


class Overview(_Weather):
    """Current conditions at a glance: big icon, big temperature, condition, high/low."""
    defaults = {"scroll_speed": 18}

    def draw(self, r, now):
        img = sky(r.code, r.is_day, self.ctx.width, self.ctx.height)
        self.place_icon(img, r.code, r.is_day, 20, 0, 0, now)
        temp = temp_text(r.temp)
        self.big.draw(img, align_x("right", self.big.width(temp), self.ctx.width, 1), 15, temp, TEXT)
        h = self.ctx.height
        name = label(r.code, r.is_day).upper()
        font = next((f for f in (self.small, self.tiny) if f.width(name) <= self.ctx.width - 2), None)
        if font:
            font.draw(img, 1, h - 7, name, ACCENT)
        else:
            self.marquee(img, self.small, name, h - 7, now, ACCENT)
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
            tiny.draw(img, 1, baseline, high_low, DIM)
            if tiny.width(place) <= w - left - 1:
                tiny.draw(img, align_x("right", tiny.width(place), w, 1), baseline, place, DIM)
                return
            top = baseline - tiny.cap
            left += 3  # a gap and a thin divider, so high/low and the ticker read as two things
            for y in range(top, baseline + 1):
                img.putpixel((left - 3, y), (70, 80, 96))
            lane = img.crop((left, top, w, baseline + tiny.descent + 1))  # scroll only in the space to the right
            period, speed = tiny.width(place) + 16, max(1, self.settings["scroll_speed"])
            x = -(int(now.timestamp() * speed) % period)  # seamless: a repeat follows 16 px behind
            while x < lane.width:
                tiny.draw(lane, x, tiny.cap, place, DIM)
                x += period
            img.paste(lane, (left, top))
            return
        town = place.split(",")[0].strip()  # "CHELTENHAM, ENGLAND, GB" -> CHELTENHAM
        if not town or tiny.width(high_low) + 4 + tiny.width(town) <= w - 2:
            tiny.draw(img, 1, baseline, high_low, DIM)
            if town:
                tiny.draw(img, align_x("right", tiny.width(town), w, 1), baseline, town, DIM)
        elif int(now.timestamp() // 4) % 2 == 0:
            tiny.draw(img, 1, baseline, high_low, DIM)
        elif tiny.width(town) <= w - 2:
            tiny.draw(img, align_x("right", tiny.width(town), w, 1), baseline, town, DIM)
        else:
            self.marquee(img, tiny, town, baseline, now, DIM)


class Forecast(_Weather):
    """v2 OpenWeather Forecast: the next few hours as columns of time, icon and temperature."""
    defaults = {"columns": 4, "step_hours": 3}

    def draw(self, r, now):
        step = self.settings["step_hours"]
        cols = max(1, min(self.settings["columns"], self.ctx.width // 15))  # keep columns readable
        img = sky(r.code, r.is_day, self.ctx.width, self.ctx.height)
        here = now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
        upcoming = [h for h in r.hours if h.time >= here][::step][:cols]
        width = self.ctx.width // max(1, cols)
        for i, hour in enumerate(upcoming):
            x0 = i * width
            if i:
                for y in range(4, self.ctx.height - 4):
                    img.putpixel((x0, y), tuple(c // 3 for c in img.getpixel((x0, y))))
            stamp = "NOW" if i == 0 else hour.time.strftime("%H")
            self.tiny.draw(img, x0 + (width - self.tiny.width(stamp)) // 2 + 1, 6, stamp, DIM)
            self.place_icon(img, hour.code, hour.is_day, 11, x0 + (width - 11) // 2 + 1, 8, now)
            t = temp_text(hour.temp)
            self.small.draw(img, x0 + (width - self.small.width(t)) // 2 + 1, self.ctx.height - 4, t, TEXT)
        return img
