"""The Matrix on the network: its ``<name>.local`` name, its address on the panel, plain http on port 80.

- The name in Settings becomes the Pi's host name (``Kitchen`` -> ``kitchen.local``; avahi, already on
  Raspberry Pi OS, answers for it). Unnamed, it is ``matrix-7f3a`` from the end of its hardware address.
- It is announced as a ``_bongo._tcp`` service, so the hub (later) and phones can find it.
- When it joins a network, its address changes or its name changes, the panel shows the name, the
  address and a QR code that opens the dashboard, for ``SHOW_SECONDS``.
- Port 80 answers with a redirect to the dashboard, so ``http://kitchen.local`` works with no port.

Everything that touches the system (host name, avahi, port 80) quietly does nothing where it can't
(not root, not Linux, no avahi), so the same code runs in CI and on a laptop.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

log = logging.getLogger("bongo.net")

SHOW_SECONDS = 10
CHECK_SECONDS = 10
DEFAULT_NAME = "Bongo Matrix"   # the name a Matrix has before you give it one
AVAHI = Path("/etc/avahi/services")


def mac_suffix(root: Path = Path("/sys/class/net")) -> str:
    """The last four hex digits of the Wi-Fi (else wired) hardware address: a stable short id."""
    for nic in ("wlan0", "eth0", "end0"):
        try:
            mac = (root / nic / "address").read_text().strip().replace(":", "")
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-fA-F]{12}", mac) and mac != "000000000000":
            return mac[-4:].lower()
    return "0000"


def host_name(name: str | None, suffix: str) -> str:
    """``Shop window`` -> ``shop-window``; no name (or the default one) -> ``matrix-7f3a``."""
    if not name or name.strip() == DEFAULT_NAME:
        return f"matrix-{suffix}"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    if slug == "matrix":
        return f"matrix-{suffix}"   # matrix.local is the hub's (bongo/hub.py)
    return slug or f"matrix-{suffix}"


def address() -> str | None:
    """The address other devices on the network reach this one on (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") or ip == "0.0.0.0" else ip
    except OSError:
        return None
    finally:
        s.close()


def can_manage_system() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0 and not os.environ.get("BONGO_OFFLINE")


def apply_host_name(host: str, run=subprocess.run, hosts: Path = Path("/etc/hosts")) -> bool:
    """Make ``host`` the system's name (so avahi answers for ``host.local``). Root on Linux only."""
    if not can_manage_system() or not shutil.which("hostnamectl") or socket.gethostname() == host:
        return False
    try:
        run(["hostnamectl", "set-hostname", host], check=True, capture_output=True, timeout=20)
        text = hosts.read_text() if hosts.exists() else ""
        lines = [ln for ln in text.splitlines() if not ln.startswith("127.0.1.1")] + [f"127.0.1.1\t{host}"]
        hosts.write_text("\n".join(lines) + "\n")
        log.info("network name is now %s.local", host)
        return True
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("couldn't set the network name to %s: %s", host, error)
        return False


def announce(name: str, port: int, version: str, ident: str, folder: Path = AVAHI, started: float = 0.0) -> bool:
    """A ``_bongo._tcp`` service file for avahi (it notices new files by itself). ``started`` (when
    this Matrix started) lets the Matrices agree which is the hub (bongo/hub.py)."""
    if not folder.is_dir() or not os.access(folder, os.W_OK):
        return False
    xml = (f'<?xml version="1.0" standalone="no"?>\n<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
           f'<service-group>\n  <name>{escape(name)}</name>\n  <service>\n    <type>_bongo._tcp</type>\n'
           f'    <port>{int(port)}</port>\n    <txt-record>id={escape(ident)}</txt-record>\n'
           f'    <txt-record>version={escape(version)}</txt-record>\n'
           + (f'    <txt-record>started={started:.0f}</txt-record>\n' if started else '')
           + '  </service>\n</service-group>\n')
    path = folder / "bongo.service"
    if not path.exists() or path.read_text() != xml:
        path.write_text(xml)
    return True


# -- port 80 ---------------------------------------------------------------------------------
def redirect_server(port: int, target: int, host: str = "0.0.0.0", override=None) -> ThreadingHTTPServer | None:
    """Answer plain ``http://name.local`` with a redirect to the dashboard's port. None if port
    ``port`` can't be had (not root, or something else uses it): the dashboard still works on its own port.
    ``override()`` may return a URL to send every request to instead (the setup page while Wi-Fi setup
    is open: a phone checking for a sign-in page then opens it by itself)."""
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's naming)
            elsewhere = override() if override else None
            if elsewhere:
                self.send_response(302)   # temporary: after setup the same names mean the internet again
                self.send_header("Location", elsewhere)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            where = (self.headers.get("Host") or "").rsplit(":", 1)[0] or address() or "localhost"
            self.send_response(308)
            self.send_header("Location", f"http://{where}:{target}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_HEAD = do_POST = do_PUT = do_DELETE = do_GET

        def log_message(self, *args):  # quiet: the dashboard logs what matters
            pass
    try:
        server = ThreadingHTTPServer((host, port), Redirect)
    except OSError as error:
        log.info("plain http on port %s not available (%s); use :%s", port, error.strerror or error, target)
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="port80").start()
    return server


# -- the address screen ------------------------------------------------------------------------
BG = (7, 9, 12)                 # the pairing screen's background
NAME_COLOUR, IP_COLOUR = (143, 180, 255), (255, 227, 179)
SPEED = 10                      # px/s a line too long for its space scrolls: 1 px every other frame at 20 fps
HOLD = 1.0                      # s the start of a long line stands still before it moves
GAP = 12                        # px between the end of a scrolling line and its next pass


def readable(font, text: str) -> str:
    """``text`` upper-cased for the panel font, accents dropped (É -> E); ``?`` for what it can't write."""
    import unicodedata
    out = []
    for c in text.upper():
        plain = "".join(ch for ch in unicodedata.normalize("NFKD", c) if not unicodedata.combining(ch))
        if plain and all(ord(ch) in font.glyphs for ch in plain):
            c = plain        # the font's accented capitals squeeze into 5 rows: plain letters read better
        elif ord(c) not in font.glyphs:
            c = "?"
        out.append(c)
    return "".join(out)


def ip_width(font, text: str) -> int:
    """Width of an address with its dots drawn 2 px wide (the font's ``.`` takes a 4 px cell)."""
    return sum(2 if c == "." else font.width(c) for c in text)


def draw_ip(font, img: Image.Image, x: int, baseline: int, text: str, colour) -> None:
    for c in text:
        if c == ".":
            if 0 <= x < img.width:
                img.putpixel((x, baseline - 1), colour)
            x += 2
        else:
            font.draw(img, x, baseline, c, colour)
            x += font.width(c)


def wrap_ip(font, ip: str, room: int) -> list[str]:
    """One line if it fits, else two, split at the dot nearest the middle (the dot ends line one)."""
    if ip_width(font, ip) <= room:
        return [ip]
    best = None
    for i, c in enumerate(ip):
        if c == ".":
            a, b = ip[:i + 1], ip[i + 1:]
            worst = max(ip_width(font, a), ip_width(font, b))
            if best is None or worst < best[0]:
                best = (worst, [a, b])
    return best[1] if best and best[0] <= room else [ip]


class AddressScreen:
    """Shows the name, address and a QR code for the dashboard after the address or name changes.

    The frame it is given is already the safe area (the player places it on the panel afterwards),
    so it lays out on ``frame.size``. The code is there for the whole ``SHOW_SECONDS``; the words sit
    beside it (an address wraps onto two lines if it must). On a panel too narrow for both, the
    code shows alone. A line still too long scrolls, all such lines together, after ``HOLD``."""

    MIN_TEXT = 14   # px of text room worth having beside the code

    def __init__(self, fonts, clock=time.monotonic):
        self.fonts, self.clock = fonts, clock
        # (name, ip, url, code, shown, until) in one tuple: show() swaps it whole, overlay() reads it once
        self._state: tuple = ("", None, "", None, 0.0, 0.0)

    @property
    def name(self) -> str:
        return self._state[0]

    @property
    def showing(self) -> bool:
        return self._showing(self._state)

    def _showing(self, state) -> bool:
        return self.clock() < state[5] and bool(state[1])

    def show(self, name: str, ip: str | None, url: str, host: str = "") -> None:
        tiny = self.fonts("tom-thumb")
        label = readable(tiny, name)
        if label.count("?") * 2 > len(label) and host:
            label = host.upper()      # a name the panel can't write (emoji, CJK): its network name instead
        codes = (qr_image(url), qr_image(compact_url(url))) if ip else (None, None)
        now = self.clock()
        self._state = (label, ip, url, codes, now, now + SHOW_SECONDS)   # one assignment: never half new

    def _code(self, codes, w: int, h: int) -> tuple[Image.Image | None, bool]:
        """(the code, scaled as large as fits; whether text fits beside it)."""
        if codes is None or codes[0] is None:
            return None, True
        qr = codes[0] if codes[0].height <= h else codes[1]   # the smaller upper-case form only when needed
        if qr.height > h or qr.width > w:
            return None, True
        beside = qr.width + 3 + self.MIN_TEXT <= w
        room = w - 33 if beside else w                          # keep ~30 px for words beside it
        scale = max(1, min(h // qr.height, room // qr.width))
        if scale > 1:
            qr = qr.resize((qr.width * scale, qr.height * scale), Image.NEAREST)
        return qr, beside

    def overlay(self, frame: Image.Image) -> Image.Image:
        state = self._state
        if not self._showing(state):
            return frame
        name, ip, _url, codes, shown, _until = state
        w, h = frame.size
        img = Image.new("RGB", (w, h), BG)
        qr, beside = self._code(codes, w, h)
        if qr is not None and not beside:   # too narrow for words too: the code alone, centred
            img.paste(qr, ((w - qr.width) // 2, (h - qr.height) // 2))
            return img
        tiny = self.fonts("tom-thumb")
        x = 0
        if qr is not None:
            img.paste(qr, (1, (h - qr.height) // 2))
            x = qr.width + 3
        room = w - x
        ips = wrap_ip(tiny, ip or "", room)
        if len(ips) > 1 and 7 * 2 + 6 > h:   # no room for three lines (32x16): one, scrolling if it must
            ips = [ip or ""]
        lines = [(name, NAME_COLOUR, False)] + [(t, IP_COLOUR, True) for t in ips]
        widths = [ip_width(tiny, t) if is_ip else tiny.width(t) for t, _, is_ip in lines]
        period = max(widths) + GAP                         # every scrolling line moves as one
        t = max(0.0, self.clock() - shown - HOLD)
        off = int(t * SPEED + 0.25) % period               # +0.25: frame-time jitter never lands on a step edge
        pitch = 9 if len(lines) == 2 else 7
        y = (h - (pitch * (len(lines) - 1) + 5)) // 2      # top of the first line (capitals and digits: 5 rows)
        for (text, colour, is_ip), width in zip(lines, widths):
            line = Image.new("RGB", (room, 8), BG)
            for start in ([(room - width) // 2] if width <= room else [-off, period - off]):
                if is_ip:
                    draw_ip(tiny, line, start, 6, text, colour)
                else:
                    tiny.draw(line, start, 6, text, colour)
            img.paste(line, (x, y - 1))
            y += pitch
        return img


def compact_url(url: str) -> str:
    """``http://10.0.0.42:5000/`` -> ``HTTP://10.0.0.42:5000``: QR's upper-case mode packs it into a
    smaller code (23 px, not 27). Used only where the usual one won't fit (a big safe-area margin)."""
    return url.upper().rstrip("/") if re.fullmatch(r"http://[0-9.]+(:\d+)?/?", url) else url


def qr_image(url: str) -> Image.Image:
    """The dashboard's link as a QR code, one pixel a module, white with a 1-module quiet zone."""
    import segno
    code = segno.make(url, error="l", micro=False)
    rows = list(code.matrix_iter(scale=1, border=1))
    img = Image.new("RGB", (len(rows[0]), len(rows)))
    img.putdata([(0, 0, 0) if dark else (255, 255, 255) for row in rows for dark in row])
    return img


def chain(*overlays):
    """Several Live overlays as one; the first that changes the frame wins (pairing over address)."""
    live = [o for o in overlays if o]

    def draw(frame):
        for o in live:
            out = o(frame)
            if out is not frame:
                return out
        return frame
    return draw


class Watch:
    """Every ``CHECK_SECONDS``: has the address or the name changed? Then set the host name,
    re-announce, and show the address screen. Runs on its own thread from ``bongo run``."""

    def __init__(self, screen: AddressScreen, read_config, port: int, plain_http: bool, version: str,
                 suffix: str | None = None, get_address=address, set_host=apply_host_name, publish=announce,
                 started: float | None = None):
        self.screen, self.read_config, self.port, self.plain_http, self.version = screen, read_config, port, plain_http, version
        self.suffix = suffix or mac_suffix()
        self.get_address, self.set_host, self.publish = get_address, set_host, publish
        self.seen: tuple | None = None
        self.started = time.time() if started is None else started

    def url(self, ip: str) -> str:
        return f"http://{ip}/" if self.plain_http else f"http://{ip}:{self.port}/"

    def check(self) -> bool:
        """One look; True if the screen was put up."""
        config = self.read_config()
        name = (config.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME
        host, ip = host_name(name, self.suffix), self.get_address()
        if (host, name, ip) == self.seen:
            return False
        if self.seen is None or self.seen[0] != host:
            self.set_host(host)
        if self.seen is None or self.seen[1] != name:
            self.publish(name, self.port, self.version, self.suffix, started=self.started)
        self.seen = (host, name, ip)
        if ip:
            self.screen.show(name, ip, self.url(ip), host)
            log.info("on the network as %s.local, %s", host, ip)
        return bool(ip)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check()
            except Exception:   # never let the network check stop the player
                log.exception("network check failed")
            stop.wait(CHECK_SECONDS)
