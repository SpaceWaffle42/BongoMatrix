"""QR code: a real, scannable code (one LED per module, a lit quiet zone round it) with a label and
the address scrolling underneath. A link, guest Wi-Fi, a number to call or text, two codes side
by side, a bigger code on its own, or a code that swaps with a large message. The ``corner``
overlay puts a small code in a corner over whatever is playing.

Codes that wouldn't fit on the panel are refused when the settings are saved, with how much
shorter to make them.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from bongo.module import Module, Style

from . import codes

AMBER, WHITE, RULE = (255, 181, 71), (255, 255, 255), (40, 40, 46)


def _limit_for(settings: dict) -> list[tuple[str, int]]:
    """(payload, largest version) for each code the entry's style shows (told apart by their settings)."""
    if "network" in settings:
        return [(codes.wifi(settings["network"], settings["password"], settings["security"]), codes.MAX_VERSION)]
    if "number" in settings:
        return [(_phone(settings), codes.MAX_VERSION)]
    if "second_url" in settings:
        return [(codes.link(settings["url"]), 2), (codes.link(settings["second_url"]), 2)]
    if "corner" in settings:
        return [(codes.link(settings["url"]), 2)]   # the corner code stays small: short links only
    return [(codes.link(settings["url"]), codes.MAX_VERSION)]


def _phone(s):
    return codes.call(s["number"]) if s["action"] == "call" else codes.text(s["number"], s["message"])


def check(settings: dict) -> None:
    if not str(settings.get("url", "")).strip():
        raise ValueError("url is empty")
    if "network" in settings and not settings["network"].strip():
        raise ValueError("network name is empty")
    if "number" in settings and len(codes.number(settings["number"])) < 3:
        raise ValueError(f"{settings['number']!r} doesn't look like a phone number")
    for payload, limit in _limit_for(settings):
        problem = codes.too_long(payload, limit)
        if problem:
            raise ValueError(problem)


class Base(Style):
    fps = 20
    SPEED = 20        # px a second the line along the bottom scrolls: one pixel a frame, no judder

    def setup(self):
        self.small = self.ctx.fonts("tom-thumb")
        self.f57 = self.ctx.fonts("5x7")
        self.started = None

    def seconds(self, now):
        if self.started is None:
            self.started = now
        return (now - self.started).total_seconds()

    def ticker(self, img, x0, x1, baseline, text, t, colour=AMBER, gap=16):
        """``text`` centred between x0 and x1, or scrolling if it's too long."""
        w = self.small.width(text)
        strip = Image.new("RGB", (x1 - x0, 7))
        if w <= x1 - x0:
            self.small.draw(strip, (x1 - x0 - w) // 2, 6, text, colour)
        else:
            ms = max(0, round(t * 1000) - 1000)              # whole milliseconds: an even pixel a frame
            off = -(ms * self.SPEED // 1000 % (w + gap))
            for k in (0, 1):
                self.small.draw(strip, off + k * (w + gap), 6, text, colour)
        img.paste(strip, (x0, baseline - 6), strip.convert("L").point(lambda v: 255 if v else 0))   # just the letters

    def words(self, img, x0, x1, top, label, colour=WHITE):
        for i, word in enumerate(label.upper().split()[:3]):
            self.small.draw(img, x0 + (x1 - x0 - self.small.width(word)) // 2, top + i * 6, word, colour)

    def coded(self, now, payload, label, scroll, t=None):
        """The usual layout, picked by the code's size: a small code at the top with the label
        beside it and the line along the bottom, or a bigger one full height on the left."""
        w, h = self.ctx.width, self.ctx.height
        t = self.seconds(now) if t is None else t
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        small_code = codes.fitted(payload, h - 7)
        if small_code is not None and small_code.width <= 25 and h >= 30:
            b = small_code
            x = (w - b.width) // 2
            img.paste(b, (x, 0))
            mid = b.height // 2
            lines = len(label.split()[:3])
            self.words(img, 0, x, mid - lines * 3 + 5, label)       # centred on the code
            ax = x + b.width + 4 + (1 if int(t * 2) % 2 else 0)        # an arrow nudging at the code
            for k in range(4):
                d.line([ax + k, mid - k, ax + k, mid + k], fill=AMBER)
            self.ticker(img, 0, w, h - 1, scroll, t)                    # two dark rows under the code
            return img
        b = codes.fitted(payload, h)
        if b is None:
            return self.too_small(img)
        img.paste(b, (0, (h - b.height) // 2))
        x0 = b.width + 2
        if w - x0 >= 16:
            lines = len(label.split()[:3])
            self.words(img, x0, w, (h - 8) // 2 - lines * 3 + 5, label)
            d.line([x0, h - 8, w - 1, h - 8], fill=RULE)
            self.ticker(img, x0, w, h - 1, scroll, t)
        return img

    def too_small(self, img):
        self.small.draw(img, 1, 6, "QR CODE", AMBER)
        self.small.draw(img, 1, 12, "NEEDS A", WHITE)
        self.small.draw(img, 1, 18, "BIGGER", WHITE)
        self.small.draw(img, 1, 24, "SCREEN", WHITE)
        return img


class Standard(Base):
    """Standard: your link as a code, with a label and the address scrolling underneath. A short
    link sits at the top; a longer one fills the height on the left."""
    defaults = {"scroll": ""}
    basic = ("scroll",)

    def render(self, now):
        s = self.settings
        return self.coded(now, codes.link(s["url"]), s["label"], s["scroll"] or codes.shown(s["url"]))


class WifiGuests(Base):
    """Wi-Fi for guests: scanning joins your guest network with no typing; the name and password
    scroll underneath for anyone who can't scan."""
    defaults = {"network": "Guest", "password": "", "security": "WPA", "label": "SCAN FOR WIFI"}
    choices = {"security": ["WPA", "WEP", "none"]}
    basic = ("network", "password", "label")

    def render(self, now):
        s = self.settings
        scroll = s["network"] + (f"  /  {s['password']}" if s["security"] != "none" else "")
        return self.coded(now, codes.wifi(s["network"], s["password"], s["security"]), s["label"], scroll)


class CallOrText(Base):
    """Call or text: scanning opens the phone's dialler (or a text message, typed in ready) with
    your number."""
    defaults = {"number": "07700 900123", "action": "call", "message": "", "label": "SCAN TO CALL"}
    choices = {"action": ["call", "text"]}
    basic = ("number", "action", "label")

    def render(self, now):
        s = self.settings
        scroll = s["number"]
        return self.coded(now, _phone(s), s["label"], scroll)


class TwoCodes(Base):
    """Two codes: two links side by side (say booking and Instagram), each with its own short
    label underneath."""
    defaults = {"second_url": "instagram.com", "second_label": "INSTA"}
    basic = ("second_url", "second_label")

    def render(self, now):
        s, w, h = self.settings, self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h))
        a = codes.fitted(codes.link(s["url"]), h - 5)
        b = codes.fitted(codes.link(s["second_url"]), h - 5)
        if a is None or b is None or a.width + b.width + 4 > w:
            return self.too_small(img)
        gap = (w - a.width - b.width) // 3
        xa, xb = gap, w - gap - b.width
        img.paste(a, (xa, 0))
        img.paste(b, (xb, 0))
        for x, code, text in ((xa, a, s["label"]), (xb, b, s["second_label"])):   # capitals only: 5 rows, clear of the codes
            word = text.upper()
            while word and self.small.width(word) > code.width:
                word = word[:-1]
            self.small.draw(img, x + (code.width - self.small.width(word) + 2) // 2, h, word, AMBER)   # rows 27-31
        return img


class CodeOnly(Base):
    """Code only: no words, so the code can be bigger and hold a longer link (about 50
    characters). Small lights run up the edges so it doesn't look frozen."""
    defaults = {"lights": True}

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h))
        b = codes.fitted(codes.link(self.settings["url"]), h)
        if b is None:
            return self.too_small(img)
        x = (w - b.width) // 2
        img.paste(b, (x, (h - b.height) // 2))
        if self.settings["lights"] and x >= 3:
            k = int(self.seconds(now) * 5) % 16
            for y in range(h):
                if (y + k) % 16 < 3:
                    img.putpixel((1, y), AMBER)
                    img.putpixel((w - 2, h - 1 - y), AMBER)
        return img


class CodeAndMessage(Standard):
    """Code and message: swaps every few seconds between the code and a big message, so it
    reads from across the room and scans up close."""
    defaults = {"scroll": "", "message": "BOOK ONLINE", "swap": 4}
    basic = ("message",)

    def render(self, now):
        t = self.seconds(now)
        swap = max(1, self.settings["swap"])
        if int(t // swap) % 2 == 0:                          # the line along the bottom starts again each time
            s = self.settings
            return self.coded(now, codes.link(s["url"]), s["label"], s["scroll"] or codes.shown(s["url"]), t % swap)
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h))
        big = self.ctx.fonts("6x13B")
        spaced = lambda f, text: f.width(text) + (len(text) - 1 if f is big else 0)   # big letters get a gap
        font = big if all(spaced(big, word.upper()) <= w - 2 for word in self.settings["message"].split()) else self.f57
        lines, line = [], ""
        for word in self.settings["message"].upper().split():
            if line and spaced(font, line + " " + word) > w - 2:
                lines.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        lines.append(line)
        lines = lines[:2] if font is big else lines[:3]
        pitch = font.cap + 4
        top = (h - (pitch * len(lines) - 4)) // 2 + font.cap
        for i, text in enumerate(lines):
            x = (w - spaced(font, text)) // 2
            for ch in text:                                  # letter by letter, a pixel apart
                x += font.draw(img, x, top + i * pitch, ch, WHITE if i == 0 else AMBER) + (1 if font is big else 0)
        return img


class Corner(Base):
    """Overlay: a small code for a short link in a corner, over whatever is playing."""
    defaults = {"corner": "bottom_right"}
    choices = {"corner": ["bottom_right", "bottom_left", "top_right", "top_left"]}

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        b = codes.block(codes.link(self.settings["url"]), 2)
        if b.height + 2 > h or b.width + 2 > w:
            return img
        corner = self.settings["corner"]
        x = w - b.width if corner.endswith("right") else 0
        y = h - b.height if corner.startswith("bottom") else 0
        x = max(1, x - 1) if corner.endswith("right") else min(w - b.width - 1, x + 1)
        y = max(1, y - 1) if corner.startswith("bottom") else min(h - b.height - 1, y + 1)
        img.paste((0, 0, 0, 120), (0, 0, w, h))                 # the screen behind dims a little, so cameras find the code
        img.paste((0, 0, 0, 255), (x - 1, y - 1, x + b.width + 1, y + b.height + 1))   # a black moat round it
        img.paste(b.convert("RGBA"), (x, y))
        return img


MODULE = Module(
    id="qr",
    name="QR code",
    description="A scannable code with its address scrolling underneath: a link, guest Wi-Fi, a number to "
                "call or text, two links side by side, or a code that swaps with a big message.",
    defaults={"url": "example.com", "label": "SCAN ME"},
    basic=("url", "label"),
    styles={"standard": Standard, "wifi_guests": WifiGuests, "call_or_text": CallOrText,
            "two_codes": TwoCodes, "code_only": CodeOnly, "code_and_message": CodeAndMessage},
    overlays={"corner": Corner},
    check=check,
)
