"""The gate: an imported module's only way to the internet. A child's relay (``jail.relay``) passes each web
request here over a local socket; the gate lets it through only to a site in that module's list (its
``bongo-module.json`` "network"), and never to the home network, whatever a name looks up to.

Shut in (``jail.can_shut_in``), each child runs as a user of its own, and the gate checks that user, so one
module can't borrow another's list.
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

    def allow(self, module_id: str, sites: list[str], uid: int | None) -> None:
        with self.lock:
            self.modules[module_id] = {"sites": list(sites), "uid": uid}

    def forget(self, module_id: str) -> None:
        with self.lock:
            self.modules.pop(module_id, None)

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
        try:
            method, target, version = first.decode("ascii").split(" ")
        except ValueError:
            return self._refuse(client, "400 Bad Request", "not a web request")
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
