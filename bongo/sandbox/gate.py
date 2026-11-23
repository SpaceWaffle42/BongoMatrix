"""The gate: an imported module's only way to the internet. A child's relay (``jail.relay``) passes each web
request here over a local socket; the gate lets it through only to a site in that module's list (its
``bongo-module.json`` "network"), and never to the home network, whatever a name looks up to.

Shut in (``jail.can_shut_in``), each child runs as a user of its own, and the gate checks that user, so one
module can't borrow another's list.

One exception, for a module that asked for it and whose owner agreed ("abilities": "home_address"): a plain
connection to the ONE home-network address its owner typed into its settings (``set_home``, ``_home``). The
module never says where to: it asks for "home" and the gate dials the address it was given by the Matrix, after
checking what that address really is (``home_reason``): never this Matrix itself, never the internet.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger(__name__)

MOST_HEAD = 16 * 1024
MOST_AT_ONCE = 16           # connections per module (per child's user), counted as they arrive
MOST_ALL = 128              # connections for all modules together
TIMEOUT = 15                # s to say who it is and what it wants, and for any reply
TUNNELS = [ipaddress.ip_network(n) for n in ("::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")]


def host_ok(host: str, port: int, sites: list[str]) -> bool:
    """Whether ``host:port`` is in a module's list. "api.example.com" is that name only, "*.example.com" any
    name under it; the usual web ports (80, 443) unless the entry names its own ("example.com:8443")."""
    host = host.lower().rstrip(".")
    for site in sites:
        site = str(site).lower().strip()
        name, _, want = site.partition(":")
        ports = {int(want)} if want.isdigit() else {80, 443}
        if port not in ports:
            continue
        if name.startswith("*."):
            if host.endswith(name[1:]) and host != name[2:]:
                return True
        elif host == name:
            return True
    return False


def public(address: str) -> bool:
    """An address on the internet (not this Matrix, the home network, or anything else private)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in TUNNELS):      # IPv4 hidden inside IPv6 (NAT64, 6to4, Teredo...): never
            return False
    return ip.is_global and not ip.is_multicast


HOME_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
HOME_AT_ONCE = 4            # connections a module may have open to its home-network address
HOME_TRIES = 20             # ...and how many times it may try to connect in a minute
HOME_TIMEOUT = 5            # s to reach a device on the home network (at most: a module may ask for less)
# how each failure to reach a module's home-network address is told to it (bongo/sandbox/host.py turns these into
# the exceptions docs/MODULES.md lists)
HOME_FAILS = {"not_allowed": "403 Forbidden", "too_many": "429 Too Many Requests", "not_found": "404 Not Found", "refused": "502 Bad Gateway",
              "no_route": "503 Service Unavailable", "no_answer": "504 Gateway Timeout"}


def own_address(ip) -> bool:
    """Whether ``ip`` is one of this Matrix's own addresses, on any of its networks (the setup hotspot's too):
    only an address of its own can be bound to. Any doubt counts as its own."""
    import errno
    try:
        with socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((str(ip), 0))
        return True
    except OSError as error:   # not one of its addresses (or a kind of address it doesn't have at all)
        return error.errno not in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT)


def home_reason(address: str, own=own_address) -> str | None:
    """Why ``address`` can't be a module's home-network address (None: it can). Only a private address of the
    home network passes: not this Matrix (loopback, 0.0.0.0, any address of its own), not link-local
    (169.254.x.x, fe80::, where routers and cloud machines keep their own services), not multicast or
    broadcast (x.x.x.255 too), not IPv4 hidden inside IPv6, and not the internet (that is what "network" is for)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return "isn't an address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified:
        return "is this Matrix itself"
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in net for net in TUNNELS):
        return "is an IPv4 address inside an IPv6 one, which isn't allowed"
    if ip.is_link_local:
        return "is a link-local address, which isn't allowed"
    if ip.is_multicast or str(ip) == "255.255.255.255":
        return "is a broadcast address"
    if not any(ip in net for net in HOME_NETS):
        return "is on the internet, not your home network" if ip.is_global else "isn't a home-network address"
    if ip.version == 4 and ip.packed[3] == 255:
        # the last address of a home network (192.168.1.255, and 10.255.255.255 for the whole block) reaches every
        # device on it at once. Home networks are nearly always /24, so any x.x.x.255 counts as one
        return "is a broadcast address: it reaches every device on the network at once"
    if ip.version == 4 and ip.packed[3] == 0:
        # the first address of a home network (192.168.1.0) names the network itself, never a device on it
        return "is the address of a whole network, not of one device on it"
    if own(ip):
        return "is this Matrix itself"
    return None


def look_up(host: str, port: int, wait: float = HOME_TIMEOUT) -> list[str] | None:
    """Every address ``host`` looks up to, in the order the system gives them; None if it can't be found within
    ``wait`` seconds (a name server that doesn't answer mustn't hold the module up past its own timeout)."""
    found: list = []

    def ask():
        try:
            found.append([i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)])
        except Exception:   # (any failure to look it up is "can't be found", never an error out of this thread)
            found.append(None)
    asking = threading.Thread(target=ask, daemon=True, name="home-lookup")
    asking.start()
    asking.join(wait)
    return list(dict.fromkeys(found[0])) if found and found[0] else None


def home_targets(host: str, port: int, pinned: str | None = None, own=own_address,
                 wait: float = HOME_TIMEOUT) -> tuple[list[str], str]:
    """The addresses to dial, in turn, for a module's home-network address: ([addresses], "") or ([], why not).
    A name is looked up here, once, and only the addresses that passed are dialled, so it can't change in
    between. Of the addresses a name looks up to, only home-network ones count (a name often has an IPv6 and an
    IPv4 address, and a device may listen on only one of them: each is tried); ``pinned`` (the one that worked
    last time) goes first while the name still gives it."""
    found = look_up(host, port, wait)
    if found is None:
        return [], f"couldn't find {host} on the network"
    good = [a for a in found if home_reason(a, own) is None]
    if not good:
        return [], f"{host} {home_reason(found[0], own) if found else 'has no address'}"
    return ([pinned] + [a for a in good if a != pinned] if pinned in good else good), ""


def home_target(host: str, port: int, pinned: str | None = None, own=own_address) -> tuple[str | None, str]:
    """The first of ``home_targets``: (address, "") or (None, why not)."""
    addresses, why = home_targets(host, port, pinned, own)
    return (addresses[0] if addresses else None), why


def connect(host: str, port: int, *, check_public=True) -> socket.socket:
    """Connect to a name's first internet address (looked up once, so it can't change in between)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if check_public and not infos or check_public and not all(public(i[4][0]) for i in infos):
        raise PermissionError(f"{host} isn't on the internet")
    error = None
    for family, kind, proto, _, address in infos:
        s = socket.socket(family, kind, proto)
        s.settimeout(TIMEOUT)
        try:
            s.connect(address)
            s.settimeout(None)
            return s
        except OSError as e:
            error = e
            s.close()
    raise error or OSError(f"couldn't reach {host}")


class Gate:
    def __init__(self, path: Path, enforce_users: bool):
        self.path = Path(path)
        self.enforce_users = enforce_users
        self.modules: dict[str, dict] = {}          # id -> {"sites": [...], "uid": int}
        self.blocked: dict[str, list[str]] = {}     # id -> sites it tried and wasn't let through to (newest last)
        self.busy: dict = {}                       # who (a child's user) -> connections open
        self.total = 0
        self.lock = threading.Lock()
        self.server = None
        self.check_public = True                    # (the tests turn this off to use a local web server)
        self.homes: dict[str, dict] = {}            # id -> {"host", "port", "pin", "tries", "open", "said"}: its one
        self.check_home = True                      # home-network address (...and this off, a local test server)

    def allow(self, module_id: str, sites: list[str], uid: int | None) -> None:
        with self.lock:
            self.modules[module_id] = {"sites": list(sites), "uid": uid}

    def forget(self, module_id: str) -> None:
        with self.lock:
            self.modules.pop(module_id, None)

    def set_home(self, module_id: str, host: str | None, port: int | None = None) -> None:
        """The one home-network address ``module_id`` may connect to, as its owner typed it (None or "": none).
        Changed or taken away, any connection it has to the old one is closed."""
        with self.lock:
            old = self.homes.get(module_id)
            if host and old and (old["host"], old["port"]) == (host, port):
                return
            if host:
                self.homes[module_id] = {"host": host, "port": int(port), "pin": None, "tries": [], "open": set(), "said": ""}
            else:
                self.homes.pop(module_id, None)
        for s in list(old["open"]) if old else ():
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def home_said(self, module_id: str) -> dict | None:
        """For the dashboard: the address it last connected to, or why it wasn't let through."""
        with self.lock:
            home = self.homes.get(module_id)
            return {"pin": home["pin"], "said": home["said"]} if home else None

    def start(self) -> "Gate":
        if self.server:
            return self
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, 0o666)                 # any child can knock; it's let in by its user (below)
        server.listen(64)
        self.server = server
        threading.Thread(target=self._accept, daemon=True, name="module-gate").start()
        return self

    def _accept(self):
        import time
        while True:
            try:
                client, _ = self.server.accept()
            except OSError as error:                # (out of files, say): keep going, a little later
                log.warning("module gate: %s", error)
                time.sleep(0.5)
                continue
            try:
                who = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] if self.enforce_users else "-"
            except OSError:
                client.close()
                continue
            with self.lock:
                if self.total >= MOST_ALL or self.busy.get(who, 0) >= MOST_AT_ONCE:
                    client.close()                   # too many at once: turned away straight off
                    continue
                self.total += 1
                self.busy[who] = self.busy.get(who, 0) + 1
            threading.Thread(target=self._handle, args=(client, who), daemon=True).start()

    def _refuse(self, client, status: str, why: str):
        body = why.encode()
        try:
            client.sendall(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
                           f"Connection: close\r\n\r\n".encode() + body)
        except OSError:
            pass

    def _blocked(self, module_id: str, host: str):
        with self.lock:
            seen = self.blocked.setdefault(module_id, [])
            if host not in seen:
                log.warning("module %s tried to reach %s, which isn't in its list of sites", module_id, host)
                seen.append(host)
                del seen[:-10]

    @staticmethod
    def _recv(client, n, deadline):
        import time
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("too slow")
        client.settimeout(left)
        return client.recv(n)

    def _read_head(self, client, deadline) -> tuple[bytes, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            part = self._recv(client, 4096, deadline)
            if not part:
                raise ConnectionError("closed")
            buf += part
            if len(buf) > MOST_HEAD:
                raise ValueError("request too long")
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head + b"\r\n\r\n", rest

    def _handle(self, client, who):
        import time
        deadline = time.monotonic() + TIMEOUT        # to say who it is and what it wants, all told
        try:
            hello = b""
            while not hello.endswith(b"\n"):
                part = self._recv(client, 1, deadline)
                if not part or len(hello) > 80:
                    return
                hello += part
            word, _, module_id = hello.decode("ascii", "replace").strip().partition(" ")
            with self.lock:
                known = self.modules.get(module_id)
            if word != "BONGO" or not known:
                self._read_head(client, deadline)    # (read first, so the refusal isn't lost to a reset)
                return self._refuse(client, "403 Forbidden", "unknown module")
            if self.enforce_users and who != known["uid"]:
                self._read_head(client, deadline)
                return self._refuse(client, "403 Forbidden", "wrong module")
            self._forward(client, module_id, known["sites"], deadline)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            client.close()
            with self.lock:
                self.total -= 1
                self.busy[who] -= 1

    def _tls_hello(self, client, have: bytes, deadline) -> bytes:
        """The rest of the first TLS record (the ClientHello), up to 16 KB."""
        buf = have
        while len(buf) < 5:
            part = self._recv(client, 5 - len(buf), deadline)
            if not part:
                raise ConnectionError("closed")
            buf += part
        want = 5 + int.from_bytes(buf[3:5], "big")
        if want > 5 + 16384:
            raise ValueError("too long")
        while len(buf) < want:
            part = self._recv(client, want - len(buf), deadline)
            if not part:
                raise ConnectionError("closed")
            buf += part
        return buf[len(have):]

    def _forward(self, client, module_id, sites, deadline):
        head, rest = self._read_head(client, deadline)
        first, _, others = head.partition(b"\r\n")
        if b"\n" in first or b"\r" in first or b"\n" in others.replace(b"\r\n", b"") or b"\r" in others.replace(b"\r\n", b""):
            # a line break on its own inside a header: the gate reads one header there, some servers read two (a
            # second Host, a Transfer-Encoding), so what was checked isn't what the site would act on
            return self._refuse(client, "400 Bad Request", "a header has a bare line break in it")
        try:
            method, target, version = first.decode("ascii").split(" ")
        except ValueError:
            return self._refuse(client, "400 Bad Request", "not a web request")
        if method == "HOME":                         # its one home-network address (never named by the module)
            return self._home(client, module_id, rest, others)
        if method == "CONNECT":                      # https: a tunnel to host:port
            host, _, port = target.rpartition(":")
            host, port = host.strip("[]"), int(port) if port.isdigit() else 443
            send = None
        elif target.startswith("http://"):          # plain http: the request itself, sent on
            from urllib.parse import urlsplit
            try:
                url = urlsplit(target)
                host, port = url.hostname or "", url.port or 80
            except ValueError:
                return self._refuse(client, "400 Bad Request", "not a web address")
            for line in others.split(b"\r\n"):       # the request names the same site it asked for
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"host" and value.strip().decode("ascii", "replace").rsplit(":", 1)[0].strip("[]").lower() != host.lower():
                    self._blocked(module_id, value.strip().decode("ascii", "replace"))
                    return self._refuse(client, "403 Forbidden", "the Host header names a different site")
            # One request per connection: only this request's Host is checked, so a second one kept alive on the
            # same connection could name any other site on that server. It's sent with "Connection: close", and
            # nothing after its own body is passed on
            lines, length, sizes, named = [], 0, 0, False
            for line in others.split(b"\r\n"):
                name = line.partition(b":")[0].strip().lower()
                if name == b"transfer-encoding":
                    return self._refuse(client, "411 Length Required", "send a Content-Length (not chunked)")
                if name == b"content-length":
                    # plain digits, given once: the site must read the same body length as the gate does
                    value = line.partition(b":")[2].strip()
                    sizes += 1
                    if sizes > 1 or not value.isdigit() or len(value) > 15:
                        return self._refuse(client, "400 Bad Request", "one Content-Length, a whole number")
                    length = int(value)
                named = named or name == b"host"
                if line and name not in (b"connection", b"keep-alive", b"proxy-connection"):
                    lines.append(line)
            if not named:                            # the site is told which of its names was asked for
                where = f"[{host}]" if ":" in host else host
                lines.append(f"Host: {where}{'' if port == 80 else f':{port}'}".encode())
            path = url.path or "/"
            send = (f"{method} {path}{'?' + url.query if url.query else ''} {version}\r\n".encode()
                    + b"".join(line + b"\r\n" for line in lines) + b"Connection: close\r\n\r\n" + rest[:length])
            body_left = length - len(rest[:length])
        else:
            return self._refuse(client, "400 Bad Request", "not a web request")
        if not host_ok(host, port, sites):
            self._blocked(module_id, host)
            return self._refuse(client, "403 Forbidden", f"{host} isn't in this module's list of sites (bongo-module.json, \"network\")")
        try:
            out = connect(host, port, check_public=self.check_public)
        except PermissionError as error:
            self._blocked(module_id, host)
            return self._refuse(client, "403 Forbidden", str(error))
        except OSError as error:
            return self._refuse(client, "502 Bad Gateway", f"couldn't reach {host}: {error}")
        with out:
            if send is None:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                hello = rest + self._tls_hello(client, rest, deadline)
                name = server_name(hello)
                if name is None or name.lower().rstrip(".") != host.lower().rstrip("."):
                    self._blocked(module_id, name or f"{host} (no name given)")
                    return                           # a secure connection that names another site: dropped
                out.sendall(hello)
            else:
                out.sendall(send)
            client.settimeout(None)
            done = threading.Event()

            def back():
                _pipe(out, client)
                done.set()
            threading.Thread(target=back, daemon=True).start()
            _pipe(client, out, None if send is None else body_left)
            done.wait(TIMEOUT)

    def _home(self, client, module_id: str, rest: bytes, asked: bytes = b""):
        """A plain connection to the module's home-network address, if it has one and the address is what it should
        be. The bytes are passed on as they are: nothing in them is read, so nothing in them can redirect it.
        Each way it can fail has a status of its own (``HOME_FAILS``), so the module can tell them apart; the
        words are the same ones its card shows (``home_said``)."""
        import errno
        import time
        wait = HOME_TIMEOUT                              # "Wait: 3": the module's own timeout, if shorter
        for line in asked.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"wait":
                try:
                    wait = max(0.5, min(HOME_TIMEOUT, float(value.strip())))
                except ValueError:
                    pass
        with self.lock:
            home = self.homes.get(module_id)
            if home is not None:
                now = time.monotonic()
                home["tries"] = [t for t in home["tries"] if now - t < 60]
                if len(home["tries"]) <= HOME_TRIES:     # (one over is enough to know: the list never grows past that)
                    home["tries"].append(now)
                busy = len(home["tries"]) > HOME_TRIES or len(home["open"]) >= HOME_AT_ONCE
                host, port, pinned = home["host"], home["port"], home["pin"]
        if home is None:
            return self._refuse(client, HOME_FAILS["not_allowed"], "no home-network address is set in its settings")
        if busy:
            return self._refuse(client, HOME_FAILS["too_many"], "too many connections to its home-network address: try again in a minute")
        started = time.monotonic()                       # one ``wait`` for the lookup and every address together
        if self.check_home:
            addresses, why = home_targets(host, port, pinned, wait=wait)
        else:                                            # (the tests' own server, on this computer)
            found = look_up(host, port, wait)
            addresses, why = (found, "") if found else ([], f"couldn't find {host} on the network")
        if not addresses:
            with self.lock:
                new, home["said"] = home["said"] != why, why
            if new:
                log.warning("module %s wasn't let through to its home-network address: %s", module_id, why)
            return self._refuse(client, HOME_FAILS["not_found" if why.startswith("couldn't find") else "not_allowed"], why)
        out = error = None
        for address in addresses:                        # the addresses that were checked, nothing looked up again
            left = wait - (time.monotonic() - started)
            if left <= 0:
                error = error or TimeoutError()
                break
            try:
                out = socket.create_connection((address, port), timeout=left)
                break
            except OSError as failed:
                error = failed
        if out is None:
            if isinstance(error, TimeoutError) or error.errno == errno.ETIMEDOUT:
                kind, why = "no_answer", f"{host} didn't answer on port {port}"
            elif error.errno == errno.ECONNREFUSED:
                kind, why = "refused", f"{host} refused the connection on port {port}"
            elif error.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
                kind, why = "no_route", f"there's no way to {host} from this Matrix just now (no route)"
            else:
                kind, why = "no_answer", f"couldn't reach {host}: {error.strerror or error}"
            with self.lock:
                home["said"] = why
            return self._refuse(client, HOME_FAILS[kind], why)
        if address != pinned:
            log.info("module %s: its home-network address %s is %s%s", module_id, host, address, f" (was {pinned})" if pinned else "")
        with out:
            with self.lock:
                current = self.homes.get(module_id) is home          # (not changed in Settings while it was connecting)
                if current:
                    home["pin"], home["said"] = address, ""
                    home["open"].add(out)
            if not current:
                return self._refuse(client, "403 Forbidden", "its home-network address has just changed")
            try:
                out.settimeout(None)
                out.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                client.sendall(b"HTTP/1.1 200 Connected\r\n\r\n")
                if rest:
                    out.sendall(rest)
                client.settimeout(None)
                done = threading.Event()

                def back():
                    _pipe(out, client)
                    done.set()
                threading.Thread(target=back, daemon=True).start()
                _pipe(client, out)
                done.wait(2)                         # the module has hung up: a moment for the last of the answer, no more
            finally:
                with self.lock:
                    home["open"].discard(out)


def server_name(hello: bytes) -> str | None:
    """The site a TLS ClientHello asks for (its SNI), or None."""
    try:
        if hello[0] != 22 or hello[5] != 1:
            return None
        p = 9 + 2 + 32                               # record and handshake headers, version, random
        p += 1 + hello[p]                            # session id
        p += 2 + int.from_bytes(hello[p:p + 2], "big")   # cipher suites
        p += 1 + hello[p]                            # compression
        end = p + 2 + int.from_bytes(hello[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            kind, size = int.from_bytes(hello[p:p + 2], "big"), int.from_bytes(hello[p + 2:p + 4], "big")
            if kind == 0:                            # server_name: list length, type (0 = a name), length, name
                n = int.from_bytes(hello[p + 7:p + 9], "big")
                return hello[p + 9:p + 9 + n].decode("ascii")
            p += 4 + size
    except (IndexError, UnicodeDecodeError):
        return None
    return None


def _pipe(a, b, most=None):
    """Copy ``a`` to ``b`` until ``a`` ends; with ``most``, only that many bytes (the rest is read and dropped)."""
    try:
        while data := a.recv(65536):
            if most is not None:
                data, most = data[:most], most - len(data[:most])
            if data:
                b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


_gate = None
_gate_lock = threading.Lock()


def gate() -> Gate:
    """The Matrix's one gate, started the first time a module needs it."""
    global _gate
    from bongo.sandbox.jail import can_shut_in, run_dir

    with _gate_lock:
        if _gate is None:
            _gate = Gate(run_dir() / "gate.sock", enforce_users=can_shut_in()).start()
        return _gate
