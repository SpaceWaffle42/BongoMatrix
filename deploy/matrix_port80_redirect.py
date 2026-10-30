"""Port 80 -> 5000 redirect so http://<name>.local/ works without typing :5000.

Owner request (2026-10-15): matrix.local / billy-matrix.local only answered on :5000.
The control panel keeps a single origin (port 5000: pairing cookies, canonical-host
probe), so port 80 only answers with a redirect to the same host on :5000, keeping
the path and query. It serves nothing else, reads no files and holds no state.

First-run setup portal: setup/first_run.py binds 10.42.0.1:80 while the setup hotspot
is up. A wildcard :80 listener would make that bind fail, so this server steps aside
(closes its listener) whenever 10.42.0.1 is a local address and comes back when the
hotspot is gone. If the portal ever loses a race it exits, matrix-network-setup restarts
it 5 s later, and by then this listener is closed.

Installed once (by hand, like the restart broker) as /usr/local/libexec/matrix-port80-redirect.py
and run by deploy/matrix-port80.service (DynamicUser, CAP_NET_BIND_SERVICE only). Stdlib only,
so it runs on the system python without the app venv; ship.py never touches it.
"""
from __future__ import annotations

import os
import re
import socket
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


def hotspot_active(address: str = HOTSPOT_ADDRESS) -> bool:
    """True when ``address`` is assigned locally (binding port 0 needs no privilege)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


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
        self._open: dict[socket.socket, float] = {}
        self._lock = threading.Lock()
        super().__init__(*args, **kwargs)
        threading.Thread(target=self._reap, daemon=True).start()

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
        while True:
            time.sleep(0.5)
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


def main() -> None:
    bind = os.environ.get('MATRIX_REDIRECT_BIND', '0.0.0.0')
    port = int(os.environ.get('MATRIX_REDIRECT_PORT', '80'))
    server = thread = None
    while True:
        if hotspot_active():
            if server is not None:          # setup portal needs 10.42.0.1:80
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                server = thread = None
        elif server is None:
            try:
                server = Server((bind, port), RedirectHandler)
            except OSError:
                server = None               # port busy (portal still closing): retry
            else:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True)
                thread.start()
        time.sleep(1)


if __name__ == '__main__':
    main()
