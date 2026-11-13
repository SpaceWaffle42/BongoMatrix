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
- **joining**: the setup page asked to join a network. Setup closes, it tries, and only a network that
  works is kept; on failure setup reopens with the same name and password (phones rejoin by themselves)
  and the page says why.
- While setup is open, every name looked up on the hotspot answers with the Matrix (a dnsmasq rule for
  NetworkManager's shared connections), so a phone that joins pops up the setup page by itself.

Every call to NetworkManager goes through ``run`` (``nmcli``), so the tests drive the whole thing
with a fake NetworkManager and a fake clock.
"""
from __future__ import annotations

import logging
import re
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
SETUP_ADDRESS = "10.42.0.1"   # NetworkManager's address for itself on a shared (hotspot) connection
SETUP_SUBNET = "10.42.0.0/24"
CAPTIVE = "/etc/NetworkManager/dnsmasq-shared.d/bongo-setup.conf"
JOIN_WAIT = 50          # s NetworkManager may take to join a network from the setup page
JOIN_LIMIT = 3 * 60     # a join still going after this long is given up (setup reopens)
COUNTRIES = ("GB", "IE", "US", "CA", "AU", "NZ", "DE", "FR", "ES", "IT", "NL", "BE", "PT", "SE", "NO", "DK",
             "FI", "PL", "AT", "CH", "JP", "IN", "ZA", "AE", "SG")
WORDS = ("kite", "lamp", "moss", "pier", "reef", "sand", "tide", "wave", "palm", "star", "neon", "fern")


def nmcli(*args: str, timeout: float = 40) -> str:
    try:
        out = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"nmcli {args[0]} timed out") from None
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

    error: str = ""                    # why the last join from the setup page failed
    joining: str | None = None         # the network it's trying while state is "joining"
    scanning: bool = False             # setup's hotspot is briefly off while it looks around

    def public(self) -> dict:
        return {"state": self.state, "ssid": self.ssid, "wired": self.wired, "since": self.since,
                "hotspot": self.hotspot, "note": self.note, "bands": list(self.bands),
                "error": self.error, "joining": self.joining, "scanning": self.scanning}


def current_country(run=subprocess.run) -> str | None:
    """The Wi-Fi country already set (by Raspberry Pi Imager, raspi-config or this), or None."""
    import re
    for cmd in (["raspi-config", "nonint", "get_wifi_country"], ["iw", "reg", "get"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            out = run(cmd, check=True, capture_output=True, text=True, timeout=10).stdout or ""
        except (OSError, subprocess.SubprocessError):
            continue
        if cmd[0] == "raspi-config":   # just the code, or nothing
            codes = [out.strip()] if re.fullmatch(r"[A-Z]{2}", out.strip()) else []
        else:                          # only "country XX:" lines; never flag words such as NO-IR or AUTO-BW
            codes = re.findall(r"^country ([A-Z0-9]{2}):", out, re.M)
        codes = [c for c in codes if c.isalpha() and c not in ("00", "99")]
        if codes:
            return codes[0]
    return None


def apply_country(code: str, run=subprocess.run) -> bool:
    """Set the Wi-Fi country (the radio's legal channels; Raspberry Pi OS keeps Wi-Fi switched off
    until one is set). ``raspi-config`` also unblocks the radio; ``iw`` elsewhere. Root only."""
    from bongo.netname import can_manage_system
    code = (code or "").upper()
    if len(code) != 2 or not code.isalpha() or not can_manage_system():
        return False
    for cmd in (["raspi-config", "nonint", "do_wifi_country", code], ["iw", "reg", "set", code]):
        if shutil.which(cmd[0]):
            try:
                run(cmd, check=True, capture_output=True, timeout=30)
                return True
            except (OSError, subprocess.SubprocessError) as error:
                log.warning("couldn't set the Wi-Fi country to %s: %s", code, error)
    return False


def valid_join(ssid: str, password: str) -> str | None:
    """What's wrong with a network name and password from the setup page, or None."""
    if not ssid or len(ssid.encode()) > 32:
        return "A network name is 1 to 32 characters."
    if password and not (8 <= len(password) <= 63 or (len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password))):
        return "A Wi-Fi password is 8 to 63 characters."
    if any(ord(c) < 32 for c in ssid + password):
        return "That has a character a Wi-Fi name or password can't."
    return None


def plain_reason(error: str) -> str:
    """nmcli's error as something to tell a person."""
    low = error.lower()
    if "secrets were required" in low or "802-1x" in low or "password" in low or "psk" in low:
        return "the password wasn't accepted"
    if "no network with ssid" in low or "not found" in low or "could not be found" in low:
        return "the Matrix can't see that network from here"
    if "ip configuration" in low or "dhcp" in low:
        return "it joined, but the network didn't give it an address (the router may only allow devices it knows)"
    if re.search(r"\bsae\b|\bwpa3\b", low):
        return "the network's security type (probably WPA3 only) isn't one this Matrix's Wi-Fi can use"
    if "key-mgmt" in low:   # nmcli couldn't see the network's security when it made the profile
        return "the Matrix couldn't tell what security that network uses (try again, or check the name)"
    if "timeout" in low or "timed out" in low:
        return "the network didn't answer in time"
    return "the network wouldn't let it join"


def captive_rule(path: str = CAPTIVE) -> bool:
    """Every name looked up on the setup hotspot answers with the Matrix (phones then show the setup page)."""
    rule = f"# Bongo Matrix: while setup is open, every name answers with the Matrix\naddress=/#/{SETUP_ADDRESS}\n"
    try:
        from pathlib import Path
        target = Path(path)
        if not target.parent.is_dir():
            return False
        if not target.exists() or target.read_text() != rule:
            target.write_text(rule)
        return True
    except OSError:
        return False


class Manager:
    """The connection state machine. ``tick()`` every few seconds (``run`` does it on a thread)."""

    def __init__(self, suffix: str, run=nmcli, clock=time.monotonic, password=setup_password, settle: float = 2.0,
                 country=apply_country):
        self.suffix, self.run, self.clock, self.password, self.settle = suffix, run, clock, password, settle
        self.country = country
        self.status = Status(since=clock())
        self.last_nudge = self.last_check = self.last_used = -1e9
        self._lock = threading.RLock()
        self.wifi = None
        self.nearby: list[Network] = []   # the last scan before or during setup (the setup page's list)
        self._join_id = 0                  # which try is current: a stale one never touches the state
        self._setup_pw: str | None = None  # one password for a whole setup session

    # -- what NetworkManager knows -------------------------------------------------------------
    def devices(self) -> list[tuple[str, str, str]]:
        return [tuple(fields(ln)[:3]) for ln in self.run("-t", "-f", "DEVICE,TYPE,STATE", "device").splitlines() if ln]

    def wifi_uuids(self) -> set[str]:
        """The ids of the saved Wi-Fi connections (a try's new one is told apart from older ones by these)."""
        out = set()
        for ln in self.run("-t", "-f", "UUID,TYPE", "connection", "show").splitlines():
            uuid, kind = (fields(ln) + ["", ""])[:2]
            if kind in ("802-11-wireless", "wifi") and uuid:
                out.add(uuid)
        return out

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
            # one name can be several radios (2.4 + 5 GHz): the one it's joined to wins, then signal,
            # or a stronger idle radio hides the connection and setup would replace a working network
            if ssid not in seen or (n.active, n.signal) > (seen[ssid].active, seen[ssid].signal):
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
    def open_setup(self, why: str, keep_password: bool = False) -> None:
        with self._lock:
            try:
                self.nearby = self.scan(rescan=True)   # the radio can't look around once it's the hotspot
            except RuntimeError:
                pass
            if not (keep_password and self._setup_pw):
                self._setup_pw = self.password()
            ssid, pw = f"Matrix-Setup-{self.suffix.upper()}", self._setup_pw
            self.run("device", "wifi", "hotspot", "ifname", self.wifi or "wlan0", "con-name", HOTSPOT,
                     "ssid", ssid, "password", pw)
            self.status.hotspot = {"ssid": ssid, "password": pw}   # before the state: screens never see setup without it
            self._enter("setup", why)
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

    def reopen(self) -> bool:
        """Settings > Wi-Fi > Reopen setup: leave the network and open setup now (not mid-join)."""
        with self._lock:
            if self.status.state in ("setup", "joining"):
                return self.status.state == "setup"
            self.open_setup("asked for in Settings")
            return True

    def rescan(self) -> bool:
        """The setup page's Rescan: the hotspot goes off for a few seconds while the radio looks
        around, then comes back with the same name and password (phones rejoin by themselves)."""
        with self._lock:
            if self.status.state != "setup" or self.status.scanning:
                return False
            self.status.scanning = True
        threading.Thread(target=self._rescan, daemon=True, name="wifi-rescan").start()
        return True

    def _rescan(self) -> None:
        try:
            time.sleep(self.settle)            # the page's answer reaches the phone first
            with self._lock:
                if self.status.state != "setup":
                    return
                try:
                    self.run("connection", "down", HOTSPOT)
                except RuntimeError:
                    pass
            time.sleep(self.settle)            # the radio settles before it looks, so the list is new
            with self._lock:
                if self.status.state != "setup":   # a saved network came back meanwhile: stay on it
                    return
                self.open_setup("looked around again", keep_password=True)
        except RuntimeError as error:
            log.warning("rescan failed: %s", error)
        finally:
            self.status.scanning = False

    def join(self, ssid: str, password: str = "", hidden: bool = False, country: str = "") -> bool:
        """From the setup page: leave setup and try ``ssid``; keep it only if it works (on a thread,
        so the page gets its answer before the hotspot goes). Only while setup is open, one at a time."""
        with self._lock:
            if self.status.state != "setup" or self.status.scanning:
                return False
            self.status.error, self.status.joining = "", ssid
            self._enter("joining", f"trying {ssid}")
            self._join_id += 1
            ident = self._join_id
        threading.Thread(target=self._join, args=(ssid, password, hidden, country, ident), daemon=True, name="wifi-join").start()
        return True

    def _current(self, ident: int | None) -> bool:
        return self.status.state == "joining" and (ident is None or ident == self._join_id)

    def _join(self, ssid: str, password: str, hidden: bool, country: str = "", ident: int | None = None) -> bool:
        # no lock while NetworkManager tries (up to a minute): tick() leaves "joining" alone
        try:
            before = self.wifi_uuids()   # whatever was saved before this try stays saved, whatever its name
        except RuntimeError:
            before = None
        time.sleep(self.settle)   # let the page's answer reach the phone before the hotspot goes
        with self._lock:
            try:
                self.run("connection", "down", HOTSPOT)
            except RuntimeError:
                pass
            self.status.hotspot = None
        if country:
            self.country(country)
        args = ["--wait", str(JOIN_WAIT), "device", "wifi", "connect", ssid] + \
            (["password", password] if password else []) + (["hidden", "yes"] if hidden else []) + \
            ["ifname", self.wifi or "wlan0"]
        try:
            try:
                self.run("device", "wifi", "rescan", timeout=15)
            except RuntimeError:
                pass   # "scanning not allowed" right after the hotspot goes: connect scans anyway
            self.run(*args, timeout=JOIN_WAIT + 15)
        except RuntimeError as error:
            log.info("couldn't join %s: %s", ssid, error)
            if self._landed(ssid, ident):   # NetworkManager gave up waiting, but the join finished anyway (slow DHCP)
                log.info("…but it's on %s after all", ssid)
                return self._joined(ssid, ident)
            with self._lock:
                current = self._current(ident)
            if current and before is not None:   # only a network that works is kept (never one a newer try just saved)
                try:
                    for uuid in self.wifi_uuids() - before:   # the profile this try made, even if it's "Name 1"
                        self.run("connection", "delete", "uuid", uuid)
                except RuntimeError:
                    pass
            self._failed(ssid, f"Couldn't join {ssid}: {plain_reason(str(error))}.", ident)
            return False
        return self._joined(ssid, ident)

    def _landed(self, ssid: str, ident: int | None = None, looks: int = 5) -> bool:
        """Whether it's on ``ssid`` after all, looking a few times (``settle`` s apart) as an address arrives."""
        for k in range(looks):
            with self._lock:
                if not self._current(ident):   # given up on meanwhile: nothing to wait for
                    return False
            try:
                online, now, _wired = self.link()
            except RuntimeError:
                online, now = False, None
            if online and now == ssid:
                return True
            if k < looks - 1:
                time.sleep(self.settle)
        return False

    def _joined(self, ssid: str, ident: int | None) -> bool:
        with self._lock:
            if not self._current(ident):   # given up on meanwhile (the watchdog), maybe a newer try: leave it be
                return False
            self.status.joining = None
            self._enter("connected", f"on {ssid}")
            self.status.ssid, self._setup_pw = ssid, None
        log.info("joined %s from the setup page", ssid)
        return True

    def _failed(self, ssid: str, why: str, ident: int | None = None) -> None:
        with self._lock:
            if not self._current(ident):
                return
            self.status.error = why                       # first, so the screens never show setup without it
            try:
                self.open_setup(f"couldn't join {ssid}", keep_password=True)
            except RuntimeError as error:
                log.warning("couldn't reopen setup: %s", error)
                self._enter("connecting", "setup wouldn't reopen")
            self.status.joining = None

    # -- the state machine --------------------------------------------------------------------
    def _enter(self, state: str, note: str) -> None:
        if state == "connected":
            self.status.error = ""   # on a network now: an earlier "couldn't join" no longer applies
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
            if st.state == "joining":
                if now - st.since > JOIN_LIMIT:   # NetworkManager never answered: don't leave it offline
                    self._failed(st.joining or "the network", f"Couldn't join {st.joining}: the network didn't answer in time.")
                return self.status.state
            if st.state == "setup":
                if now - self.last_used >= SETUP_IDLE:
                    self.close_setup("nobody used setup for 15 minutes")
                elif now - self.last_check >= RECHECK:
                    self.last_check = now
                    try:
                        self.nearby = self.scan(rescan=True) or self.nearby
                        back = [n for n in self.nearby if n.saved]
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
