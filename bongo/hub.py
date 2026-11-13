"""The hub: every Matrix on a network listed on one page at ``http://matrix.local`` (read-only).

There is no extra box: each Matrix can be the hub, and they agree which one is.

- Each Matrix announces itself as ``_bongo._tcp`` (bongo/netname.py), with its id, version and the
  time it started.
- Each one browses for the others (mDNS, ``zeroconf``) every ``CHECK_SECONDS``. The **longest-running**
  one is the hub (lowest id breaks a tie). The hub also answers at ``matrix.local``; if it goes away,
  the next longest-running one takes over within about a minute.
- The hub asks every Matrix for its public card (``/api/public``: name, what's playing, version,
  warnings) and serves the hub page with one card each, a live picture and an **Open dashboard** link.

Read-only on purpose: the hub can't change anything, and each Matrix still pairs on its own. The
cards show nothing private (no settings, feeds or messages): anyone on the same Wi-Fi can see them.

The mDNS side is behind small callables (``browse``, ``alias``), so the tests drive the election and
the cards with fake peers and no network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import threading
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("bongo.hub")

SERVICE = "_bongo._tcp.local."
ALIAS = "matrix.local."
CHECK_SECONDS = 20          # browse and re-elect this often
CARD_SECONDS = 5            # the hub refreshes every card this often
GONE_SECONDS = 90           # a Matrix not heard from this long drops off the page
ALIVE_SECONDS = 30          # a would-be hub must have answered this recently to count (mDNS caches go stale)
CARD_BYTES = 64 * 1024      # a card is small; never read more than this from another device
FETCH_AT_ONCE = 8           # cards asked for at the same time


@dataclass
class Peer:
    """One Matrix as the network sees it."""
    id: str
    name: str
    address: str
    port: int
    version: str = ""
    started: float = 0.0     # wall-clock start time: the longest-running one is the hub

    @property
    def url(self) -> str:
        return f"http://{self.address}:{self.port}/"


def elect(peers: list[Peer], alive=None) -> Peer | None:
    """The hub: the longest-running Matrix, lowest id breaking a tie. Every Matrix works this out
    the same way from the same announcements, so they agree without talking. ``alive(peer)``: skip
    one that has stopped answering (an unplugged Matrix's mDNS record can linger for an hour)."""
    for p in sorted((p for p in peers if p.started > 0), key=lambda p: (p.started, p.id)):
        if alive is None or alive(p):
            return p
    return None


def version_key(v: str) -> tuple:
    parts = []
    for bit in str(v).replace("-", ".").split("."):
        parts.append((0, int(bit)) if bit.isascii() and bit.isdigit() else (1, bit))
    return tuple(parts)


def clean_card(raw) -> dict:
    """Only the fields the hub shows, each forced to its type: a card comes from another device."""
    if not isinstance(raw, dict):
        raise ValueError("not a card")
    warnings = raw.get("warnings")
    return {"name": str(raw.get("name") or "")[:40],
            "version": str(raw.get("version") or "")[:20],
            "playing": str(raw.get("playing") or "")[:60] or None,
            "paused": bool(raw.get("paused")),
            "sleeping": bool(raw.get("sleeping")),
            "picture": raw.get("picture") is not False,   # False: its owner turned the hub picture off
            "warnings": [str(w)[:80] for w in warnings[:5]] if isinstance(warnings, list) else []}


def reachable(address: str, mine: str | None) -> bool:
    """Only another device on this Matrix's own network (or this Matrix itself) is asked for a card."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4 or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        return False
    if ip.is_loopback:
        return False   # never this machine's own local services: check() keeps this Matrix by its id
    if address == mine:
        return True
    if not mine:
        return False
    return ip in ipaddress.ip_network(f"{mine}/24", strict=False)


def label(module: str, style: str | None) -> str:
    """``clock``, ``nixie_seconds`` -> ``Clock · Nixie seconds``."""
    def tidy(s):
        return s.replace("_", " ").replace("-", " ").strip().capitalize()
    return f"{tidy(module)} · {tidy(style)}" if style else tidy(module)


class OldVersion(Exception):
    """A Matrix from before the hub: it has no card to give."""


def fetch_card(peer: Peer, timeout: float = 2.0) -> dict:
    """A peer's card, given up on after ``timeout`` * 1.5 in all. Every read waits only for what is left of
    that, so a peer trickling its answer a byte at a time can't hold up the hub (or its shutdown), and no
    thread is left behind. A redirect is never followed."""
    deadline = time.monotonic() + timeout * 1.5

    def left() -> float:
        rest = deadline - time.monotonic()
        if rest <= 0:
            raise TimeoutError(f"{peer.address} took too long")
        return rest
    data = b""
    with socket.create_connection((peer.address, int(peer.port)), timeout=min(timeout, left())) as sock:
        sock.sendall(f"GET /api/public HTTP/1.0\r\nHost: {peer.address}\r\nAccept: application/json\r\n\r\n".encode())
        while len(data) <= CARD_BYTES + 8192:
            sock.settimeout(left())
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
    if code == 404:
        raise OldVersion()
    if code != 200:
        raise ValueError(f"HTTP {code or '?'}")
    return clean_card(json.loads(body[:CARD_BYTES].decode()))


@dataclass
class Card:
    peer: Peer
    card: dict = field(default_factory=dict)
    seen: float = 0.0           # when it last answered (time.time())
    error: str = ""

    def public(self, now: float, newest: str, hub_id: str | None) -> dict:
        behind = bool(newest and self.peer.version and version_key(self.peer.version) < version_key(newest))
        card = dict(self.card)
        if self.error:   # not answering (or too old): nothing from its last card is current but its name
            card = {"name": card.get("name")}
        # the card can't override who and where it is: those come from mDNS
        return {**card, **asdict(self.peer), "name": card.get("name") or self.peer.name, "url": self.peer.url,
                "seen_ago": round(now - self.seen) if self.seen else None,
                "behind": behind, "hub": self.peer.id == hub_id, "error": self.error}


class Hub:
    """Browses for Matrices, works out which is the hub, and (when it's this one) keeps the cards."""

    def __init__(self, me: Peer, browse=None, alias=None, fetch=fetch_card, clock=time.time, whoami=None):
        self.me, self.browse, self.alias, self.fetch, self.clock = me, browse, alias, fetch, clock
        self.whoami = whoami        # () -> Peer: this Matrix now (its name and address can change)
        self.peers: dict[str, Peer] = {me.id: me}
        self.cards: dict[str, Card] = {}
        self.leader: Peer | None = me
        self.aliased = False
        self.answered: dict[str, float] = {}   # peer id -> when it last answered (time.time())
        self._lock = threading.Lock()

    @property
    def is_hub(self) -> bool:
        return self.leader is not None and self.leader.id == self.me.id

    def check(self) -> Peer | None:
        """One browse and election; claims or lets go of ``matrix.local``."""
        found = list(self.browse()) if self.browse else []
        if self.whoami:
            self.me = self.whoami()
        mine = self.me.address
        found = [p for p in found if p.id == self.me.id or reachable(p.address, mine)]   # loopback: only itself
        with self._lock:
            self.peers = {p.id: p for p in found if p.id != self.me.id}
            self.peers[self.me.id] = self.me
            peers = list(self.peers.values())
        leader = elect(peers, alive=self._alive)
        with self._lock:
            self.leader = leader
        if self.alias:
            want = self.is_hub
            where = self.me.address if want else None
            if want != self.aliased or (want and where != getattr(self, "_aliased_at", None)):
                try:
                    if self.aliased and want:
                        self.alias(False)            # the address changed: claim it again with the new one
                    self.alias(want)
                    self.aliased, self._aliased_at = want, where
                    log.info("%s matrix.local", "answering at" if want else "no longer answering at")
                except Exception as error:   # another Matrix still holds it: try again next time
                    log.info("couldn't %s matrix.local: %s", "claim" if want else "release", error)
        return self.leader

    def _alive(self, p: Peer) -> bool:
        """Has ``p`` answered lately? Asks it (cheaply) if not: only the would-be hubs ahead of this one
        get asked, so a stale mDNS record of an unplugged Matrix can't hold the hub."""
        if p.id == self.me.id:
            return True
        now = self.clock()
        if now - self.answered.get(p.id, -1e9) < ALIVE_SECONDS:
            return True
        try:
            self.fetch(p)
        except OldVersion:
            return False   # an old build answers but has no card: it can't be the hub
        except Exception:
            return False
        self.answered[p.id] = now
        return True

    def refresh(self) -> None:
        """The hub only: ask every Matrix for its card."""
        if not self.is_hub:
            return
        now = self.clock()
        with self._lock:
            peers = list(self.peers.values())
        results: dict[str, tuple] = {}

        def ask(p: Peer) -> None:
            try:
                results[p.id] = ("ok", self.fetch(p))
            except OldVersion:
                results[p.id] = ("old", None)
            except Exception as error:
                results[p.id] = ("gone", error)
        # every Matrix at once (a few at a time), so slow ones cost one deadline, not one each;
        # fetch_card's own hard deadline means every thread ends
        for i in range(0, len(peers), FETCH_AT_ONCE):
            batch = [threading.Thread(target=ask, args=(p,), daemon=True, name="hub-card") for p in peers[i:i + FETCH_AT_ONCE]]
            for th in batch:
                th.start()
            for th in batch:
                th.join(10)
        for p in peers:
            card = self.cards.setdefault(p.id, Card(p))
            card.peer = p
            kind, value = results.get(p.id, ("gone", "no answer"))
            if kind == "ok":
                card.card, card.seen, card.error = value, now, ""
                self.answered[p.id] = now
            elif kind == "old":
                card.card, card.seen, card.error = {}, now, "Update it to see more"
                self.answered[p.id] = now
            else:
                card.error = "Not answering"
                log.debug("hub: %s didn't answer: %s", p.name, value)
        for pid in [pid for pid, c in self.cards.items() if pid not in self.peers and now - c.seen > GONE_SECONDS]:
            del self.cards[pid]

    def listing(self) -> dict:
        now = self.clock()
        with self._lock:
            cards = list(self.cards.values())
            leader = self.leader
        newest = max((c.peer.version for c in cards if c.peer.version), key=version_key, default="")
        rows = sorted((c.public(now, newest, leader.id if leader else None) for c in cards),
                      key=lambda r: (r["name"] or "").lower())
        return {"hub": self.is_hub, "leader": asdict(leader) if leader else None, "newest": newest, "matrices": rows}

    def run(self, stop: threading.Event) -> None:
        last_check = -1e9
        while not stop.is_set():
            try:
                if time.monotonic() - last_check >= CHECK_SECONDS:
                    last_check = time.monotonic()
                    self.check()
                self.refresh()
            except Exception:   # never let the hub stop the player
                log.exception("hub check failed")
            stop.wait(CARD_SECONDS)


# -- mDNS (zeroconf) ---------------------------------------------------------------------------
class Zeroconf:
    """The real mDNS side: browse ``_bongo._tcp`` and, on the hub, answer for ``matrix.local``."""

    def __init__(self, get_address, port: int, ident: str = ""):
        import atexit

        from zeroconf import IPVersion, ServiceBrowser, Zeroconf as Zc
        self.zc = Zc(ip_version=IPVersion.V4Only)
        atexit.register(self.close)   # goodbyes on the way out, so a restart doesn't leave a stale hub
        self.ident = ident
        self.get_address, self.port = get_address, port
        self.found: dict[str, Peer] = {}
        self._alias_info = None

        hub = self

        class Listener:
            def add_service(self, zc, kind, name):
                hub._resolve(name)

            def update_service(self, zc, kind, name):
                hub._resolve(name)

            def remove_service(self, zc, kind, name):
                hub.found.pop(name, None)
        self.browser = ServiceBrowser(self.zc, SERVICE, Listener())

    def _resolve(self, name: str) -> None:
        info = self.zc.get_service_info(SERVICE, name, timeout=3000)
        if info is None:
            return
        txt = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
        from zeroconf import IPVersion
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if not addresses or "id" not in txt:
            return
        try:
            started = float(txt.get("started", "0") or 0)
        except ValueError:
            started = 0.0
        self.found[name] = Peer(txt["id"], name.split("." + SERVICE)[0], addresses[0], info.port,
                                txt.get("version", ""), started)

    def browse(self) -> list[Peer]:
        return list(self.found.values())

    def alias(self, on: bool) -> None:
        import socket

        from zeroconf import ServiceInfo
        if on:
            address = self.get_address()
            if not address:
                raise RuntimeError("no address yet")
            self._forget_stale_alias()
            # short-lived records: if this Matrix vanishes, phones and the next hub forget it within a minute;
            # its id in the record makes a second claim a conflict that zeroconf refuses
            self._alias_info = ServiceInfo("_http._tcp.local.", "Matrix hub._http._tcp.local.",
                                           addresses=[socket.inet_aton(address)], port=self.port,
                                           server=ALIAS, properties={"path": "/hub", "id": self.ident},
                                           host_ttl=30, other_ttl=30)
            self.zc.register_service(self._alias_info)   # probes first: refuses if another holds the name
        elif self._alias_info is not None:
            self.zc.unregister_service(self._alias_info)
            self._alias_info = None

    def _forget_stale_alias(self) -> None:
        """Drop remembered matrix.local records before claiming it. zeroconf keeps them ~19 minutes
        whatever lifetime was sent, so after a power cut nobody could claim it; this only runs once
        every Matrix ahead of this one has stopped answering, so they are stale. A live holder still
        answers the claim's probes, and the claim is refused as it should be."""
        cache, name = self.zc.cache, "Matrix hub._http._tcp.local."

        async def drop():
            stale = [r for r in cache.async_entries_with_name("_http._tcp.local.") if getattr(r, "alias", None) == name]
            stale += list(cache.async_entries_with_name(name)) + list(cache.async_entries_with_name(ALIAS))
            cache.async_remove_records(stale)
        try:
            asyncio.run_coroutine_threadsafe(drop(), self.zc.loop).result(5)
        except Exception as error:   # an older zeroconf: claim anyway; at worst it waits for the cache
            log.debug("couldn't clear old matrix.local records: %s", error)

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if self._alias_info is not None:
                self.zc.unregister_service(self._alias_info)
                self._alias_info = None
            self.zc.close()
        except Exception:   # already closed, or the network is gone: nothing to say goodbye on
            pass


def available() -> bool:
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    from bongo.netname import can_manage_system
    return can_manage_system()
