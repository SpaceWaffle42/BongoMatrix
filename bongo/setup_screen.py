"""The panel while Wi-Fi setup is open (bongo/net.py): how to join the Matrix's setup hotspot.

Two views take turns, ``VIEW_SECONDS`` each:

- **code**: a Wi-Fi QR code (``WIFI:T:WPA;S:…;P:…;;``): a phone camera joins the hotspot in one tap,
  and the setup page pops up by itself. Beside it, ``SCAN TO JOIN``.
- **words**: ``JOIN WI-FI`` / the hotspot's name / ``PASSWORD`` / the password, for typing it in
  (on a short panel, two views: ``WI-FI`` and the name, ``PASSWORD`` and the password).
- **page**: ``SETUP PAGE`` / ``10.42.0.1``, for a phone that doesn't open the setup page by itself.

While it tries a network from the setup page: ``TRYING`` and the network's name. After a failed try
the words view says ``NOT JOINED, SEE PHONE`` first (the setup page says why).

Like the address screen, it lays out on the frame it is given (already the safe area), and a line too
long for its space holds, then scrolls a pixel at a time.
"""
from __future__ import annotations

import time

from PIL import Image

from bongo import net, netname

VIEW_SECONDS = 10   # long enough for the hotspot's name to scroll past once on 64 px
BLUE, WARM, WHITE, RED = netname.NAME_COLOUR, netname.IP_COLOUR, (236, 238, 241), (255, 138, 146)


def wifi_payload(ssid: str, password: str) -> str:
    """The text phones read from a Wi-Fi QR code (special characters escaped)."""
    def esc(s: str) -> str:
        return "".join("\\" + c if c in '\\;,:"' else c for c in s)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


class SetupScreen:
    """A Live overlay: shows while ``status()`` (net.Manager.status) is in setup or joining."""

    def __init__(self, fonts, status, clock=time.monotonic):
        self.fonts, self.status, self.clock = fonts, status, clock
        self._key = None          # what's being shown; a change restarts the views and the scrolling
        self._since = 0.0
        self._qr: Image.Image | None = None

    def _state(self):
        s = self.status()
        if s is None or s.state not in ("setup", "joining"):
            return None
        if s.state == "joining":
            return ("joining", s.joining or "", "", "")
        hotspot = s.hotspot   # read once: setup may close meanwhile
        if not hotspot:
            return None
        return ("setup", hotspot["ssid"], hotspot["password"], s.error or "")

    def overlay(self, frame: Image.Image) -> Image.Image:
        key = self._state()
        if key is None:
            self._key = None
            return frame
        now = self.clock()
        if key != self._key:
            self._key, self._since = key, now
            self._qr = netname.qr_image(wifi_payload(key[1], key[2])) if key[0] == "setup" else None
        w, h = frame.size
        img = Image.new("RGB", (w, h), netname.BG)
        t = now - self._since
        if key[0] == "joining":
            dots = "." * (1 + int(t * 2) % 3)
            self._lines(img, 0, w, [("TRYING" + dots, BLUE, "TRYING..."), (key[1], WHITE, None)], t)
            return img
        short = 7 * 3 + 5 > h   # four lines don't fit (32x16): the words in two views
        views = (["error"] if key[3] else []) + (["code"] if self._qr is not None and self._qr.height <= h else []) + \
            (["wifi", "password"] if short else ["words"]) + ["page"]
        k = int(t // VIEW_SECONDS) % len(views)
        view, tv = views[k], t % VIEW_SECONDS
        if view == "error":
            self._lines(img, 0, w, [("NOT JOINED", RED, None), ("SEE PHONE", WHITE, None)], tv)
        elif view == "code":
            qr = self._qr
            beside = w - (qr.width + 4)
            if beside >= 20:
                img.paste(qr, (1, (h - qr.height) // 2))
                self._lines(img, qr.width + 3, beside + 1, [("SCAN", BLUE, None), ("TO", BLUE, None), ("JOIN", BLUE, None)], tv)
            else:
                img.paste(qr, ((w - qr.width) // 2, (h - qr.height) // 2))
        elif view == "wifi":
            self._lines(img, 0, w, [("WI-FI", BLUE, None), (key[1], WHITE, None)], tv)
        elif view == "password":
            self._lines(img, 0, w, [("PASSWORD", BLUE, None), (key[2], WARM, None)], tv)
        elif view == "page":
            self._lines(img, 0, w, [("SETUP PAGE", BLUE, None), (net.SETUP_ADDRESS, WARM, None)], tv)
        else:
            self._lines(img, 0, w, [("JOIN WI-FI", BLUE, None), (key[1], WHITE, None),
                                    ("PASSWORD", BLUE, None), (key[2], WARM, None)], tv)   # as typed: case matters
        return img

    def _lines(self, img: Image.Image, x: int, room: int, lines, t: float) -> None:
        """Centred lines in tom-thumb; any too long hold, then scroll together. ``lines`` are
        (text, colour, widest form) — the widest form keeps a line from twitching as it changes."""
        tiny = self.fonts("tom-thumb")
        widths = [tiny.width(widest or text) for text, _, widest in lines]
        period = max(widths) + netname.GAP
        off = int(max(0.0, t - netname.HOLD) * netname.SPEED + 0.25) % period
        pitch = 7 if len(lines) > 2 else 9
        y = (img.height - (pitch * (len(lines) - 1) + 5)) // 2   # capitals and digits: 5 rows
        for (text, colour, widest), width in zip(lines, widths):
            line = Image.new("RGB", (room, 8), netname.BG)
            if width <= room:
                tiny.draw(line, (room - width) // 2, 6, text, colour)
            else:
                for start in (-off, period - off):
                    tiny.draw(line, start, 6, text, colour)
            img.paste(line, (x, y - 1))
            y += pitch
