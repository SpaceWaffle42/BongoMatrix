"""Staying connected: the Matrix keeps to its saved Wi-Fi, and opens setup only as a last resort.

Raspberry Pi OS runs NetworkManager, which already reconnects to saved networks by itself; this
adds what it doesn't do (see the Wi-Fi proposal, "Staying connected"):

- **connected** -> the link drops -> **connecting**: keep nudging the strongest saved network in range.
- **connecting** for ``SETUP_AFTER`` (5 min), or nothing saved at all -> **setup**: a WPA2 hotspot
  ``Matrix-Setup-xxxx`` with a fresh password each time (the setup page and panel screens use it).
- **setup**: every ``RECHECK`` (2 min) look for a saved network; back to **connecting** as soon as one
  is in range, or after ``SETUP_IDLE`` (15 min) with nobody using setup. So a power cut, a slow router
  or a shop's unattended panel never leaves it stuck in setup.
- A cable (Ethernet) counts as connected: setup never opens while one is plugged in.

Every call to NetworkManager goes through ``run`` (``nmcli``), so the tests drive the whole thing
with a fake NetworkManager and a fake clock.
"""
from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("bongo.net")

SETUP_AFTER = 5 * 60     # trying saved networks this long before setup opens
SETUP_IDLE = 15 * 60     # setup closes after this long with nobody using it
RECHECK = 2 * 60         # while setup is open: look for a saved network this often
NUDGE = 60               # while connecting: ask NetworkManager to try the strongest saved network
TICK = 5
HOTSPOT = "bongo-setup"  # NetworkManager's name for the setup hotspot connection
WORDS = ("kite", "lamp", "moss", "pier", "reef", "sand", "tide", "wave", "palm", "star", "neon", "fern")


def nmcli(*args: str, timeout: float = 40) -> str:
    out = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"nmcli {args[0]} failed")
    return out.stdout


def fields(line: str) -> list[str]:
    """One line of ``nmcli -t`` output: ``:``-separated, with ``\\:`` for a colon inside a field."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def setup_password(rng=secrets) -> str:
    """Easy to read off a panel and type on a phone: ``kite-4729`` (WPA2 wants at least 8)."""
    return f"{rng.choice(WORDS)}-{rng.randbelow(9000) + 1000}"


@dataclass
class Network:
    ssid: str
    signal: int = 0
    secure: bool = True
    saved: bool = False
    active: bool = False


@dataclass
class Status:
    state: str = "starting"            # starting | connected | connecting | setup
    ssid: str | None = None            # the Wi-Fi it's on (None on a cable or off)
    wired: bool = False
    since: float = 0.0
    hotspot: dict | None = None        # {"ssid", "password"} while setup is open
    note: str = ""
    bands: tuple = ("2.4",)            # what the Wi-Fi chip can do ("5" too on a Pi 3B+/4/5)
    history: list = field(default_factory=list)

    def public(self) -> dict:
        return {"state": self.state, "ssid": self.ssid, "wired": self.wired, "since": self.since,
                "hotspot": self.hotspot, "note": self.note, "bands": list(self.bands)}


class Manager:
    """The connection state machine. ``tick()`` every few seconds (``run`` does it on a thread)."""

    def __init__(self, suffix: str, run=nmcli, clock=time.monotonic, password=setup_password):
        self.suffix, self.run, self.clock, self.password = suffix, run, clock, password
        self.status = Status(since=clock())
        self.last_nudge = self.last_check = self.last_used = -1e9
        self._lock = threading.RLock()
        self.wifi = None

    # -- what NetworkManager knows -------------------------------------------------------------
    def devices(self) -> list[tuple[str, str, str]]:
        return [tuple(fields(ln)[:3]) for ln in self.run("-t", "-f", "DEVICE,TYPE,STATE", "device").splitlines() if ln]

    def saved(self) -> list[str]:
        """Saved Wi-Fi networks (connection names; NetworkManager names them after the network)."""
        out = []
        for ln in self.run("-t", "-f", "NAME,TYPE", "connection", "show").splitlines():
            name, kind = (fields(ln) + ["", ""])[:2]
            if kind in ("802-11-wireless", "wifi") and name != HOTSPOT:
                out.append(name)
        return out

    def scan(self, rescan: bool = False) -> list[Network]:
        """Networks in range, strongest first, one entry per name."""
        args = ["-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        if rescan:
            args += ["--rescan", "yes"]
        saved, seen = set(self.saved()), {}
        for ln in self.run(*args).splitlines():
            active, ssid, signal, security = (fields(ln) + ["", "", "", ""])[:4]
            if not ssid:
                continue   # hidden networks are typed in by name on the setup page
            n = Network(ssid, int(signal or 0), security not in ("", "--"), ssid in saved, active == "yes")
            if ssid not in seen or n.signal > seen[ssid].signal:
                seen[ssid] = n
        return sorted(seen.values(), key=lambda n: (-n.active, -n.signal))

    def link(self) -> tuple[bool, str | None, bool]:
        """(online, Wi-Fi name, wired) from the devices' states."""
        online, ssid, wired = False, None, False
        for dev, kind, state in self.devices():
            if kind == "wifi":
                self.wifi = self.wifi or dev
            if state != "connected":
                continue
            if kind == "ethernet":
                online = wired = True
            elif kind == "wifi":
                active = [n for n in self.scan() if n.active]
                if active and active[0].ssid:
                    online, ssid = True, active[0].ssid
        return online, ssid, wired

    def bands(self) -> tuple:
        """Which bands the Wi-Fi chip supports: a Pi Zero (2) W only does 2.4 GHz (setup page hint)."""
        try:
            caps = self.run("-t", "-f", "WIFI-PROPERTIES", "device", "show", self.wifi or "wlan0")
        except RuntimeError:
            return ("2.4",)
        return ("2.4", "5") if "5GHZ:YES" in caps.upper().replace(" ", "") else ("2.4",)

    # -- the setup hotspot ------------------------------------------------------------------------
    def open_setup(self, why: str) -> None:
        with self._lock:
            ssid, pw = f"Matrix-Setup-{self.suffix.upper()}", self.password()
            self.run("device", "wifi", "hotspot", "ifname", self.wifi or "wlan0", "con-name", HOTSPOT,
                     "ssid", ssid, "password", pw)
            self._enter("setup", why)
            self.status.hotspot = {"ssid": ssid, "password": pw}
            self.status.bands = self.bands()
            self.last_check = self.last_used = self.clock()
            log.info("setup opened (%s): join %s", why, ssid)

    def close_setup(self, why: str) -> None:
        with self._lock:
            try:
                self.run("connection", "down", HOTSPOT)
            except RuntimeError:
                pass   # already down
            try:
                self.run("connection", "delete", HOTSPOT)
            except RuntimeError:
                pass
            self.status.hotspot = None
            self._enter("connecting", why)
            self.last_nudge = -1e9
            log.info("setup closed (%s)", why)

    def used(self) -> None:
        """The setup page was used: keep setup open (``SETUP_IDLE`` counts from now)."""
        self.last_used = self.clock()

    def reopen(self) -> None:
        """Settings > Wi-Fi > Reopen setup: leave the network and open setup now."""
        with self._lock:
            if self.status.state != "setup":
                self.open_setup("asked for in Settings")

    # -- the state machine --------------------------------------------------------------------
    def _enter(self, state: str, note: str) -> None:
        if self.status.state != state:
            self.status.history = (self.status.history + [(round(self.clock()), state, note)])[-20:]
            self.status.state, self.status.since = state, self.clock()
        self.status.note = note

    def nudge(self) -> None:
        """Ask NetworkManager to bring up the strongest saved network in range (it may be waiting)."""
        self.last_nudge = self.clock()
        for n in self.scan(rescan=True):
            if n.saved:
                try:
                    self.run("connection", "up", n.ssid, timeout=40)
                    return
                except RuntimeError as error:
                    log.info("couldn't join %s: %s", n.ssid, error)

    def tick(self) -> str:
        with self._lock:
            now, st = self.clock(), self.status
            if st.state == "setup":
                if now - self.last_used >= SETUP_IDLE:
                    self.close_setup("nobody used setup for 15 minutes")
                elif now - self.last_check >= RECHECK:
                    self.last_check = now
                    try:
                        back = [n for n in self.scan(rescan=True) if n.saved]
                    except RuntimeError:
                        back = []
                    if back:
                        self.close_setup(f"{back[0].ssid} is back in range")
                return self.status.state
            online, ssid, wired = self.link()
            st.ssid, st.wired = ssid, wired
            if online:
                self._enter("connected", "on a cable" if wired else f"on {ssid}")
                return st.state
            if st.state in ("starting", "connected"):
                self._enter("connecting", "looking for a saved network")
            if not self.saved() or now - st.since >= SETUP_AFTER:
                self.open_setup("no saved network yet" if not self.saved() else "no saved network for 5 minutes")
            elif now - self.last_nudge >= NUDGE:
                self.nudge()
            return self.status.state

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:   # NetworkManager hiccups must never stop the player
                log.exception("network check failed")
            stop.wait(TICK)


def available() -> bool:
    """Only on a machine where NetworkManager is in charge and we may drive it (the Pi, as root)."""
    from bongo.netname import can_manage_system
    if not (can_manage_system() and shutil.which("nmcli")):
        return False
    try:
        return "running" in nmcli("-t", "-f", "RUNNING", "general", timeout=10)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False
