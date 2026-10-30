"""Port 80 -> 5000 redirect so http://<name>.local/ works without typing :5000.

Owner request (2026-10-15): matrix.local / billy-matrix.local only answered on :5000.
The control panel keeps a single origin (port 5000: pairing cookies, canonical-host
probe), so port 80 only answers with a redirect to the same host on :5000, keeping
the path and query. It serves nothing else, reads no files and holds no state.

First-run setup portal: setup/first_run.py binds 10.42.0.1:80 while the setup hotspot
is up. This server never binds the wildcard address or 10.42.0.1: it listens on each
concrete local IPv4 address (re-checked every second) except the hotspot address, and two
different specific addresses on the same port never collide. So the portal can always bind.

Installed once (by hand, like the restart broker) as /usr/local/libexec/matrix-port80-redirect.py
and run by deploy/matrix-port80.service (DynamicUser, CAP_NET_BIND_SERVICE only). Stdlib only,
so it runs on the system python without the app venv; ship.py never touches it.
"""
from __future__ import annotations

import os
import re
import fcntl
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_PORT = int(os.environ.get('MATRIX_PORT', '5000'))
HOTSPOT_ADDRESS = '10.42.0.1'
MAX_CONNECTIONS = 16          # thread cap; the unit's TasksMax leaves headroom above it
REQUEST_DEADLINE = 3.0        # whole-connection wall-clock limit (slow-drip clients included)
_HOST = re.compile(r'(?:(?P<name>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})))(?::\d{1,5})?')
_PATH = re.compile(r'/[\x21-\x7e]{0,2047}')


def redirect_location(host_header: str | None, path: str, fallback_host: str, port: int = TARGET_PORT) -> str:
    """Absolute URL on ``port`` for the same host; bad input falls back safely."""
    host = fallback_host
    match = _HOST.fullmatch((host_header or '').strip())
    if match:
        host = match.group('name')
    if not _PATH.fullmatch(path or '') or path.startswith('//'):
        path = '/'
    return f'http://{host}:{port}{path}'


def local_ipv4_addresses() -> set[str]:
    """Concrete IPv4 addresses on up interfaces (SIOCGIFADDR per /sys/class/net entry)."""
    found = {'127.0.0.1'}
    try:
        names = os.listdir('/sys/class/net')
    except OSError:
        return found
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            try:
                packed = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack('256s', name[:15].encode()))  # SIOCGIFADDR
            except OSError:
                continue
            found.add(socket.inet_ntoa(packed[20:24]))
    finally:
        probe.close()
    return found


def wanted_addresses(addresses: set[str]) -> set[str]:
    """Never the hotspot address (the setup portal owns 10.42.0.1:80) or the wildcard."""
    return {address for address in addresses if address not in (HOTSPOT_ADDRESS, '0.0.0.0')}


class RedirectHandler(BaseHTTPRequestHandler):
    server_version = 'Matrix'
    sys_version = ''
    timeout = 2

    def _redirect(self) -> None:
        location = redirect_location(self.headers.get('Host'), self.path, self.connection.getsockname()[0])
        self.send_response(308 if self.command not in ('GET', 'HEAD') else 301)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _redirect

    def log_message(self, format: str, *args) -> None:  # journal stays quiet
        return


class Server(ThreadingHTTPServer):
    """Bounded: at most MAX_CONNECTIONS handlers; each connection closed after REQUEST_DEADLINE."""
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._open: dict = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        super().__init__(*args, **kwargs)
        threading.Thread(target=self._reap, daemon=True).start()

    def server_close(self):
        self._stopped.set()          # ends the reaper thread with the server
        super().server_close()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)   # full: drop instead of queueing threads
            return
        with self._lock:
            self._open[request] = time.monotonic() + REQUEST_DEADLINE
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(request)
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request):
        with self._lock:
            if self._open.pop(request, None) is None:
                return
        self._slots.release()

    def _reap(self):
        while not self._stopped.wait(0.5):
            now = time.monotonic()
            with self._lock:
                late = [sock for sock, deadline in self._open.items() if deadline <= now]
            for sock in late:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle_error(self, request, client_address):  # no tracebacks in the journal
        return


class Listeners:
    """One bounded Server per concrete address; follows address changes."""

    def __init__(self, port: int):
        self.port = port
        self.servers: dict[str, tuple[Server, threading.Thread]] = {}

    def sync(self, addresses: set[str]) -> None:
        wanted = wanted_addresses(addresses)
        for address in list(self.servers):
            if address not in wanted:
                self.close(address)
        for address in wanted - set(self.servers):
            try:
                server = Server((address, self.port), RedirectHandler)
            except OSError:
                continue                    # busy or address gone: retry next second
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True)
            thread.start()
            self.servers[address] = (server, thread)

    def close(self, address: str) -> None:
        server, thread = self.servers.pop(address)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    listeners = Listeners(int(os.environ.get('MATRIX_REDIRECT_PORT', '80')))
    while True:
        listeners.sync(local_ipv4_addresses())
        time.sleep(1)


if __name__ == '__main__':
    main()
