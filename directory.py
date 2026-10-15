"""Roadmap 1.10d/e: displays announce themselves; matrix.local is a directory.

Every display publishes a ``_matrix._tcp`` service through the device's
avahi-daemon (``avahi-publish``) and keeps trying to hold the ``matrix.local``
address record; whichever display holds it answers browsers that ask for
``http://matrix.local:5000`` with a read-only list of all displays.  If the
holder disappears another display's next attempt succeeds within seconds.

Peers are found with ``avahi-browse`` and confirmed by fetching their public,
read-only ``/api/directory/self`` summary.  Nothing here can change settings.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import re
import socket
import subprocess
import threading
import time

LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_matrix._tcp"
DIRECTORY_HOST = "matrix.local"
DIRECTORY_CACHE_SECONDS = 5
BROWSE_TIMEOUT_SECONDS = 4
PEER_TIMEOUT_SECONDS = 1.5  # whole fetch: connect + headers + body, not per socket read
PEER_TOTAL_DEADLINE_SECONDS = 3.0  # one directory build waits at most this long for peers
PEER_RESPONSE_LIMIT = 4096
PEER_FETCH_WORKERS = 8  # shared pool: the most peer-fetch threads that can ever exist
PEER_FETCH_LIMIT = 32  # peers fetched per build; more announcements are ignored
STALE_DROP_SECONDS = 600  # quiet displays are listed greyed, then dropped
HOLD_RETRY_SECONDS = (6, 10)  # matrix.local takeover attempt interval
_JITTER = secrets.SystemRandom()
SUPERVISE_SECONDS = 2
_TXT_VALUE = re.compile(r"^[A-Za-z0-9._+-]{1,63}$")


def is_directory_host(host):
    """True for requests addressed to matrix.local (any port, trailing dot)."""
    name = (host or "").strip().lower()
    if name.startswith("["):
        return False
    name = name.rsplit(":", 1)[0] if ":" in name else name
    return name.rstrip(".") == DIRECTORY_HOST


def _clean(value, limit=63):
    return value if isinstance(value, str) and _TXT_VALUE.fullmatch(value) and len(value) <= limit else None


def _unescape(value):
    """avahi-browse -p escapes bytes as \\DDD (decimal)."""
    return re.sub(r"\\(\d{3})", lambda match: chr(int(match.group(1))), value)


def _lan_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.version != 4 or not address.is_private or address.is_loopback:
        return None
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return None
    return str(address)


def parse_browse(output):
    """Parse ``avahi-browse -rpt`` output into {id: peer} for resolved IPv4 rows."""
    peers = {}
    for line in output.splitlines():
        fields = line.split(";")
        if len(fields) < 10 or fields[0] != "=" or fields[2] != "IPv4":
            continue
        if fields[4] != SERVICE_TYPE:
            continue
        address = _lan_ipv4(fields[7])
        try:
            port = int(fields[8])
        except ValueError:
            continue
        if address is None or not 1 <= port <= 65535:
            continue
        txt = {}
        for item in re.findall(r'"((?:[^"\\]|\\.)*)"', ";".join(fields[9:])):
            key, _, value = _unescape(item).partition("=")
            txt[key] = value
        device_id = _clean(txt.get("id")) or _clean(_unescape(fields[3]))
        if device_id is None:
            continue
        peers[device_id] = {
            "id": device_id,
            "name": _clean(txt.get("name")) or device_id,
            "version": _clean(txt.get("v"), 40),
            "address": address,
            "port": port,
        }
    return peers


def _version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


PEER_HEADER_LIMIT = 4096  # status line + headers; a real Matrix answer needs ~200 bytes


class _PeerAnswerError(Exception):
    pass


def _recv_until_deadline(sock, deadline, limit):
    """All bytes until the peer closes, or raise past ``deadline`` or ``limit``.

    Every recv is given only the time left, so no phase of the exchange
    (connect, status line, headers, body) can outlast the deadline.
    """
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _PeerAnswerError("deadline")
        sock.settimeout(remaining)
        chunk = sock.recv(min(1024, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data += chunk
        if len(data) > limit:
            raise _PeerAnswerError("too large")


def _http_get(address, port, path, timeout, connect=socket.create_connection):
    """Minimal HTTP/1.0 GET with one total deadline. Returns the 200 body or None.

    Deliberately not urllib/http.client: those bound each socket operation, not
    the exchange, so a dripping peer could hold a thread for minutes. No
    redirects (any non-200 is a failure), no proxies, no chunked encoding.
    """
    deadline = time.monotonic() + timeout
    try:
        with connect((address, int(port)), timeout=timeout) as sock:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            sock.sendall(
                f"GET {path} HTTP/1.0\r\nHost: {address}:{int(port)}\r\n"
                "Accept: application/json\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            raw = _recv_until_deadline(sock, deadline, PEER_HEADER_LIMIT + PEER_RESPONSE_LIMIT)
    except (OSError, ValueError, _PeerAnswerError):
        return None
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator or len(head) > PEER_HEADER_LIMIT:
        return None
    lines = head.split(b"\r\n")
    status = lines[0].split(None, 2)
    if len(status) < 2 or not status[0].startswith(b"HTTP/1.") or status[1] != b"200":
        return None
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        name, value = name.strip().lower(), value.strip().lower()
        if name == b"transfer-encoding" and value != b"identity":
            return None
        if name == b"content-length":
            if not value.isdigit() or int(value) != len(body):
                return None
    return body


def fetch_peer(address, port, timeout=PEER_TIMEOUT_SECONDS, connect=socket.create_connection):
    """GET a peer's public summary; None unless it is a well-formed Matrix answer.

    The whole exchange is bounded by ``timeout``; redirects are never followed.
    """
    body = _http_get(address, port, "/api/directory/self", timeout, connect=connect)
    if body is None:
        return None
    if len(body) > PEER_RESPONSE_LIMIT:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("service") != "matrix":
        return None
    device_id = _clean(data.get("id"))
    if device_id is None:
        return None
    playing = data.get("playing")
    return {
        "id": device_id,
        "name": _clean(data.get("name")) or device_id,
        "version": _clean(data.get("version"), 40),
        "playing": playing[:60] if isinstance(playing, str) else None,
    }


def playing_label(module):
    if not isinstance(module, str) or not module:
        return None
    if module == "address_notice":
        return "Address notice"
    kind, _, rest = module.partition(":")
    if rest:  # media:<file>, device-draft:<name>: owner content stays private
        return {"media": "Media", "device-draft": "Preview"}.get(kind, "Display")
    return module.replace("_", " ").strip().capitalize()[:60] or None


class AvahiProcess:
    """Keeps one ``avahi-publish`` child running for the current argv."""

    def __init__(self, popen=subprocess.Popen):
        self._popen = popen
        self._process = None
        self._argv = None
        self.established = False
        self._lock = threading.Lock()

    def ensure(self, argv):
        """Start/restart for ``argv``; return True if a child is running."""
        with self._lock:
            alive = self._process is not None and self._process.poll() is None
            if alive and argv == self._argv:
                return True
            self._stop_locked()
            if argv is None:
                return False
            try:
                process = self._popen(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True,
                )
            except OSError:
                LOGGER.warning("Could not start %s", argv[0])
                return False
            self._process, self._argv, self.established = process, argv, False
        threading.Thread(target=self._read, args=(process,), daemon=True).start()
        return True

    def _read(self, process):
        try:
            for line in process.stdout:
                if line.startswith("Established under name"):
                    with self._lock:
                        if process is self._process:
                            self.established = True
        except (OSError, ValueError):
            pass
        with self._lock:
            if process is self._process:
                self.established = False

    def running(self):
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        process, self._process, self._argv = self._process, None, None
        self.established = False
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


class _PeerPool:
    """Fixed set of daemon worker threads for peer fetches.

    ThreadPoolExecutor workers are joined at interpreter exit, so one stuck
    fetch would delay service shutdown; daemon workers never do. The pool never
    grows past ``workers`` threads, and queued work is dropped when it is full.
    """

    def __init__(self, workers, prefix, backlog=64):
        import queue

        self._queue = queue.Queue(maxsize=backlog)
        self._closed = threading.Event()
        self.threads = [threading.Thread(target=self._work, name=f"{prefix}-{index}", daemon=True)
                        for index in range(workers)]
        for thread in self.threads:
            thread.start()

    def _work(self):
        while not self._closed.is_set():
            item = self._queue.get()
            if item is None:
                return
            future, fn, args = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as error:  # reported through the future
                future.set_exception(error)

    def submit(self, fn, *args):
        import queue
        from concurrent.futures import Future

        future = Future()
        if self._closed.is_set():
            future.cancel()
            return future
        try:
            self._queue.put_nowait((future, fn, args))
        except queue.Full:
            future.cancel()
        return future

    def shutdown(self):
        """Cancel queued work and wake every idle worker so it exits."""
        import queue

        self._closed.set()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()
        for _ in self.threads:  # one sentinel per worker; busy workers exit after their fetch
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break


class Directory:
    """Announce this display, try to hold matrix.local, list all displays."""

    def __init__(self, *, version, port, device_name, status, address,
                 run=subprocess.run, popen=subprocess.Popen, fetch=fetch_peer,
                 clock=time.monotonic, wall=time.time):
        self.version = version
        self.port = int(port)
        self._device_name = device_name
        self._status = status
        self._address = address
        self._run = run
        self._fetch = fetch
        self._clock = clock
        self._wall = wall
        self.service = AvahiProcess(popen)
        self.holder = AvahiProcess(popen)
        self._hold_next = 0.0
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = None
        self._building = False
        self._pool = _PeerPool(PEER_FETCH_WORKERS, "matrix-directory-peer")
        self._known = {}
        self._shutdown = threading.Event()
        self._thread = None

    # -- this display ---------------------------------------------------
    def _identity(self):
        default = getattr(self._device_name, "default", None)
        published = self._device_name.current() if self._device_name else None
        name = published[: -len(".local")] if published and published.endswith(".local") else None
        return default, name or default

    def self_summary(self):
        device_id, name = self._identity()
        try:
            playing = playing_label((self._status() or {}).get("current_module"))
        except Exception:
            playing = None
        return {
            "service": "matrix", "id": device_id, "name": name,
            "version": self.version, "playing": playing,
        }

    # -- publishing -----------------------------------------------------
    def service_argv(self):
        device_id, name = self._identity()
        if not device_id:
            return None
        return [
            "avahi-publish", "-s", device_id, SERVICE_TYPE, str(self.port),
            f"id={device_id}", f"name={name}", f"v={self.version}",
        ]

    def holder_argv(self):
        address = _lan_ipv4(self._address() or "")
        if address is None:
            return None
        return ["avahi-publish", "-a", "-R", DIRECTORY_HOST, address]

    def supervise_once(self):
        self.service.ensure(self.service_argv())
        argv = self.holder_argv()
        if self.holder.running() and self.holder._argv == argv:
            return
        now = self._clock()
        if argv is None:
            self.holder.stop()
        elif now >= self._hold_next:
            # Jitter only (not security): SystemRandom keeps SAST free of weak-PRNG findings.
            self._hold_next = now + _JITTER.uniform(*HOLD_RETRY_SECONDS)
            self.holder.ensure(argv)

    def holds_directory(self):
        return self.holder.running() and self.holder.established

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(target=self._loop, name="matrix-directory", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._shutdown.is_set():
            try:
                self.supervise_once()
            except Exception:
                LOGGER.exception("Matrix directory publisher failed")
            self._shutdown.wait(SUPERVISE_SECONDS)

    def stop(self):
        self._shutdown.set()
        self.service.stop()
        self.holder.stop()
        self._pool.shutdown()

    # -- listing --------------------------------------------------------
    def _browse(self):
        try:
            result = self._run(
                ["avahi-browse", "-r", "-p", "-t", "-k", SERVICE_TYPE],
                capture_output=True, text=True, timeout=BROWSE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        return parse_browse(result.stdout or "")

    def listing(self):
        """Single-flight: one caller does network I/O; others get the last listing.

        Network I/O (avahi-browse, peer fetches) never runs under ``self._lock``,
        so a slow LAN host cannot pile up request threads behind it.
        """
        with self._lock:
            now = self._clock()
            if self._cache is not None and now - self._cache_at < DIRECTORY_CACHE_SECONDS:
                return self._cache
            if self._building:
                if self._cache is not None:
                    return self._cache
                return self._assemble(now, {})
            self._building = True
        try:
            answers = self._gather()
            with self._lock:
                now = self._clock()
                listing = self._assemble(now, answers)
                self._cache, self._cache_at = listing, now
                return listing
        finally:
            with self._lock:
                self._building = False

    def _gather(self):
        """Browse and fetch peers, bounded by PEER_TOTAL_DEADLINE_SECONDS overall."""
        me_id, _ = self._identity()
        peers = {key: peer for key, peer in self._browse().items() if key != me_id}
        if not peers:
            return {}
        # One shared, bounded pool for the life of the directory: at most
        # PEER_FETCH_WORKERS fetch threads ever exist, and each fetch ends by its
        # own deadline, so hostile peers cannot grow the thread count.
        futures = {key: self._pool.submit(self._fetch, peer["address"], peer["port"])
                   for key, peer in list(peers.items())[:PEER_FETCH_LIMIT]}
        deadline = time.monotonic() + PEER_TOTAL_DEADLINE_SECONDS
        answers = {}
        for key, future in futures.items():
            try:
                answer = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except BaseException:  # timeout, cancelled or failed: this peer is unconfirmed
                future.cancel()
                continue
            answers[key] = (peers[key], answer)
        return answers

    def _assemble(self, now, answers):
        me = self.self_summary()
        address = _lan_ipv4(self._address() or "") or None
        for key, (peer, answer) in answers.items():
            if key == me["id"]:
                continue
            if answer is None or answer["id"] != key:
                continue
            self._known[key] = {
                "id": key, "name": answer["name"], "version": answer["version"],
                "playing": answer["playing"], "address": peer["address"],
                "port": peer["port"], "seen": now,
            }
        for key in [key for key, entry in self._known.items()
                    if now - entry["seen"] > STALE_DROP_SECONDS or key == me["id"]]:
            del self._known[key]
        displays = [{
            **me, "address": address, "port": self.port, "seen_seconds": 0,
            "online": True, "serving": True,
        }] if me["id"] else []
        for entry in sorted(self._known.values(), key=lambda item: item["name"].lower()):
            age = max(0, int(now - entry["seen"]))
            displays.append({
                "id": entry["id"], "name": entry["name"], "version": entry["version"],
                "playing": entry["playing"], "address": entry["address"],
                "port": entry["port"], "seen_seconds": age,
                "online": entry["seen"] == now, "serving": False,
            })
        online_versions = [item["version"] for item in displays if item["online"] and item["version"]]
        newest = max(online_versions, key=_version_key, default=None)
        names = {}
        for item in displays:
            names.setdefault(item["name"].lower(), []).append(item)
        for item in displays:
            item["older_version"] = bool(
                newest and item["version"] and _version_key(item["version"]) < _version_key(newest)
            )
            item["duplicate_name"] = len(names[item["name"].lower()]) > 1
        return {
            "served_by": me["name"],
            "holds_matrix_local": self.holds_directory(),
            "generated_at": int(self._wall()),
            "displays": displays,
        }
