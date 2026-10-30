"""Port 80 -> 5000 redirect so http://<name>.local/ works without typing :5000.

Owner request (2026-10-15): matrix.local / billy-matrix.local only answered on :5000.
The control panel keeps a single origin (port 5000: pairing cookies, canonical-host
probe), so port 80 only answers with a redirect to the same host on :5000, keeping
the path and query. It serves nothing else, reads no files and holds no state.

Installed once (by hand, like the restart broker) as /usr/local/libexec/matrix-port80-redirect.py
and run by deploy/matrix-port80.service (DynamicUser, CAP_NET_BIND_SERVICE only). Stdlib only,
so it runs on the system python without the app venv; ship.py never touches it.
"""
from __future__ import annotations

import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_PORT = int(os.environ.get('MATRIX_PORT', '5000'))
# A DNS name or IPv4 address, optionally :port; or a bracketed IPv6 literal.
_HOST = re.compile(r'^(?:(?P<name>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}))|(?P<v6>\[[0-9A-Fa-f:.%]{2,60}\]))(?::\d{1,5})?$')
_PATH = re.compile(r'^/[\x21-\x7e]{0,2047}$')


def redirect_location(host_header: str | None, path: str, fallback_host: str, port: int = TARGET_PORT) -> str:
    """Absolute URL on ``port`` for the same host; bad input falls back safely."""
    host = fallback_host
    match = _HOST.match((host_header or '').strip())
    if match:
        host = match.group('name') or match.group('v6')
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    if not _PATH.match(path or '') or path.startswith('//'):
        path = '/'
    return f'http://{host}:{port}{path}'


class RedirectHandler(BaseHTTPRequestHandler):
    server_version = 'Matrix'
    sys_version = ''
    timeout = 10

    def _redirect(self) -> None:
        location = redirect_location(self.headers.get('Host'), self.path, self.connection.getsockname()[0])
        self.send_response(308 if self.command not in ('GET', 'HEAD') else 301)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _redirect

    do_HEAD = _redirect

    def log_message(self, format: str, *args) -> None:  # journal stays quiet
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16


def main() -> None:
    Server((os.environ.get('MATRIX_REDIRECT_BIND', '0.0.0.0'), int(os.environ.get('MATRIX_REDIRECT_PORT', '80'))),
           RedirectHandler).serve_forever()


if __name__ == '__main__':
    main()
