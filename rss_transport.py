"""Bounded direct public HTTP transport. No proxies, cookies, or ambient auth.

Resolver/connector injection is a trusted application/test seam, never feed config.
DNS workers are daemonized and globally bounded: stuck libc DNS cannot grow threads.
"""
import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urljoin


class FeedFetchError(ValueError):
    pass


_DNS_SLOTS = threading.BoundedSemaphore(4)


def _resolve(host, port):
    # Query A and AAAA separately. Some LAN resolvers drop glibc's parallel
    # AF_UNSPEC A+AAAA request, stalling until libc's 5 s retry. Every returned
    # address still passes the caller's public-destination checks.
    addresses, failure = [], None
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            rows = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except OSError as error:
            failure = error
            continue
        addresses.extend(row[4][0] for row in rows if row[4][0] not in addresses)
    if not addresses:
        raise failure or OSError('DNS unavailable')
    return addresses


def _bounded_resolve(resolver, host, port, timeout):
    deadline = time.monotonic() + timeout
    slots = _DNS_SLOTS
    if not slots.acquire(timeout=timeout):
        raise FeedFetchError('DNS busy')
    result = queue.Queue(maxsize=1)
    def run():
        try:
            result.put((True, resolver(host, port)))
        except Exception:
            result.put((False, None))
        finally:
            slots.release()
    threading.Thread(target=run, daemon=True).start()
    try:
        ok, addresses = result.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise FeedFetchError('DNS timed out') from None
    if not ok or not addresses:
        raise FeedFetchError('DNS unavailable')
    return addresses


def _connect(ip, port, timeout):
    # Numeric socket.connect, not create_connection: no second DNS resolution.
    sock = socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((ip, port))
        return sock
    except BaseException:
        sock.close()
        raise


def checked_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FeedFetchError('Invalid URL')
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii')
        if '%' in host or '\\' in host:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except (ValueError, UnicodeError):
        raise FeedFetchError('Invalid HTTP URL') from None
    return parsed, host, port


class SafeFeedClient:
    def __init__(self, *, resolver=_resolve, connector=_connect, max_bytes=2 * 1024 * 1024,
                 connect_timeout=3., read_timeout=3., total_timeout=10., max_redirects=3):
        self.resolver, self.connector = resolver, connector
        self.max_bytes = max_bytes
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.total_timeout, self.max_redirects = total_timeout, max_redirects

    def fetch(self, url, *, stop_event=None):
        deadline = time.monotonic() + self.total_timeout
        def remaining(cap):
            if stop_event is not None and stop_event.is_set():
                raise FeedFetchError('Fetch cancelled')
            value = min(cap, deadline - time.monotonic())
            if value <= 0:
                raise FeedFetchError('Fetch timed out')
            return value
        try:
            for hop in range(self.max_redirects + 1):
                parsed, host, port = checked_url(url)
                addresses = _bounded_resolve(self.resolver, host, port, remaining(self.connect_timeout))
                for address in addresses:
                    ip = ipaddress.ip_address(address)
                    if not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, 'ipv4_mapped', None) or getattr(ip, 'sixtofour', None) or getattr(ip, 'teredo', None):
                        raise FeedFetchError('Destination is not public')
                conn = http.client.HTTPConnection(host, port, timeout=remaining(self.connect_timeout))
                sock = self.connector(str(ipaddress.ip_address(addresses[0])), port, remaining(self.connect_timeout))
                holder = [sock]
                def abort(sock_holder=holder):
                    try:
                        sock_holder[0].shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                timer = None
                finished = threading.Event()
                def watch():
                    while not finished.wait(.01):
                        if time.monotonic() >= deadline or (stop_event is not None and stop_event.is_set()):
                            abort()
                            return
                try:
                    timer = threading.Thread(target=watch)
                    timer.daemon = True
                    timer.start()
                    if parsed.scheme == 'https':
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                        holder[0] = sock
                        sock.settimeout(remaining(self.connect_timeout))
                        sock.do_handshake()
                    conn.sock = sock
                    sock.settimeout(remaining(self.read_timeout))
                    target = parsed.path or '/'
                    if parsed.query:
                        target += '?' + parsed.query
                    conn.request('GET', target, headers={'Accept-Encoding': 'identity', 'User-Agent': 'MatrixRSS/1', 'Connection': 'close'})
                    response = conn.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.getheader('Location')
                        if not location or hop == self.max_redirects:
                            raise FeedFetchError('Redirect limit or missing location')
                        url = urljoin(url, location)
                        continue
                    if response.status != 200 or response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                        raise FeedFetchError('Unsupported feed response')
                    length = response.getheader('Content-Length')
                    if length and (int(length) < 0 or int(length) > self.max_bytes):
                        raise FeedFetchError('Feed response too large')
                    body = bytearray()
                    while True:
                        if response.isclosed():
                            break
                        sock.settimeout(remaining(self.read_timeout))
                        chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                        remaining(self.total_timeout)
                        if not chunk:
                            break
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise FeedFetchError('Feed response too large')
                    if length and len(body) != int(length):
                        raise FeedFetchError('Incomplete feed response')
                    return bytes(body)
                finally:
                    if timer is not None:
                        finished.set()
                        timer.join()
                    conn.close()
                    holder[0].close()
        except FeedFetchError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise FeedFetchError('Feed transport failed') from None