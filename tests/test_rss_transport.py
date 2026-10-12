import importlib
import importlib.util
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


class TransportTests(unittest.TestCase):
    def test_pinned_public_fetch_and_security_limits(self):
        self.assertIsNotNone(importlib.util.find_spec('rss_transport'), 'safe feed transport absent')
        api = importlib.import_module('rss_transport')
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self):
                seen.append((self.path, self.headers['Host']))
                self.send_response(302 if self.path in ('/private', '/loop') else 200)
                if self.path == '/private':
                    self.send_header('Location', 'http://127.0.0.1/secret')
                if self.path == '/loop':
                    self.send_header('Location', '/loop')
                self.end_headers()
                if self.path == '/slow':
                    time.sleep(.3)
                try:
                    self.wfile.write(b'<rss/>' if self.path != '/big' else b'x' * 200)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        calls = []
        def resolve(host, port):
            calls.append(host)
            return ['93.184.216.34'] if host == 'feed.example' else [host]
        def connect(ip, port, timeout):
            self.assertEqual(ip, '93.184.216.34')
            return socket.create_connection(server.server_address, timeout)
        client = api.SafeFeedClient(resolver=resolve, connector=connect, max_bytes=100,
                                    total_timeout=.15, read_timeout=.1, max_redirects=1)
        try:
            with patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:1'}):
                self.assertEqual(client.fetch('http://feed.example/news'), b'<rss/>')
            self.assertEqual(seen, [('/news', 'feed.example')])
            self.assertEqual(calls, ['feed.example'])
            for url in ('file:///etc/passwd', 'http://user:pw@feed.example/',
                        'http://127.0.0.1/', 'http://169.254.169.254/',
                        'http://[::1]/', 'http://feed.example/private',
                        'http://feed.example/loop', 'http://feed.example/big',
                        'http://feed.example/slow'):
                with self.subTest(url=url), self.assertRaises(api.FeedFetchError):
                    client.fetch(url)
            mixed = api.SafeFeedClient(resolver=lambda *_: ['93.184.216.34', '10.0.0.1'], connector=connect)
            with self.assertRaises(api.FeedFetchError):
                mixed.fetch('http://feed.example/')
            self.assertFalse(any(path == '/secret' for path, _ in seen))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_default_resolver_queries_each_family_separately(self):
        # Some LAN resolvers drop glibc's parallel A+AAAA query, so AF_UNSPEC
        # stalls for libc's 5 s retry and exceeds the DNS deadline.
        import rss_transport as api
        families = []
        def getaddrinfo(host, port, family=0, type=0, *rest):
            families.append(family)
            if family == socket.AF_UNSPEC:
                time.sleep(5)
            if family == socket.AF_INET6:
                raise socket.gaierror(socket.EAI_NONAME, 'no AAAA')
            return [(socket.AF_INET, type, 6, '', ('93.184.216.34', port))]
        with patch.object(api.socket, 'getaddrinfo', getaddrinfo):
            started = time.monotonic()
            self.assertEqual(api._resolve('feed.example', 443), ['93.184.216.34'])
            self.assertLess(time.monotonic() - started, 1)
        self.assertNotIn(socket.AF_UNSPEC, families)
        self.assertEqual(sorted(families), sorted([socket.AF_INET, socket.AF_INET6]))
        def both_fail(*args, **kwargs):
            raise socket.gaierror(socket.EAI_NONAME, 'missing')
        with patch.object(api.socket, 'getaddrinfo', both_fail), self.assertRaises(OSError):
            api._resolve('missing.example', 443)

    def test_dns_slot_wait_is_part_of_total_deadline(self):
        import rss_transport as api
        class Slots:
            def acquire(self, timeout):
                time.sleep(.12)
                return True
            def release(self):
                pass
        def resolve(*args):
            time.sleep(.15)
            return ['93.184.216.34']
        with patch.object(api, '_DNS_SLOTS', Slots()):
            started = time.monotonic()
            with self.assertRaises(api.FeedFetchError):
                api.SafeFeedClient(resolver=resolve, total_timeout=.18).fetch('http://feed.example/')
            self.assertLess(time.monotonic() - started, .24)

    def test_deadline_at_connect_completion_closes_socket(self):
        import rss_transport as api
        created = []
        def connector(*args):
            sock = socket.socket()
            created.append(sock)
            time.sleep(.03)
            return sock
        try:
            with self.assertRaises(api.FeedFetchError):
                api.SafeFeedClient(resolver=lambda *_: ['93.184.216.34'], connector=connector,
                                   total_timeout=.01).fetch('http://feed.example/')
            self.assertEqual(created[0].fileno(), -1)
        finally:
            for sock in created:
                sock.close()

    def test_real_tls_host_verification_and_slow_drip_deadline(self):
        import rss_transport as api
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=feed.example', '-addext', 'subjectAltName=DNS:feed.example',
                            '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
            hosts, names = [], []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass
                def do_GET(self):
                    hosts.append(self.headers['Host'])
                    self.send_response(200)
                    self.end_headers()
                    try:
                        if self.path == '/drip':
                            for _ in range(20):
                                self.wfile.write(b'x')
                                self.wfile.flush()
                                time.sleep(.03)
                        else:
                            self.wfile.write(b'<rss/>')
                    except (OSError, ssl.SSLError):
                        pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            context.set_servername_callback(lambda sock, name, ctx: names.append(name))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            trusted = ssl.create_default_context(cafile=str(cert))
            client = api.SafeFeedClient(resolver=lambda *_: ['93.184.216.34'],
                connector=lambda ip, port, timeout: socket.create_connection(server.server_address, timeout),
                total_timeout=.3, read_timeout=.15)
            try:
                # Untrusted self-signed cert fails with the production default trust store.
                with self.assertRaises(api.FeedFetchError):
                    client.fetch('https://feed.example/')
                with patch.object(api.ssl, 'create_default_context', return_value=trusted):
                    self.assertEqual(client.fetch('https://feed.example/'), b'<rss/>')
                    # Explicit default port is a valid Host authority.
                    self.assertEqual(hosts, ['feed.example:443'])
                    self.assertIn('feed.example', names)
                    with self.assertRaises(api.FeedFetchError):
                        client.fetch('https://wrong.example/')
                    started = time.monotonic()
                    with self.assertRaises(api.FeedFetchError):
                        client.fetch('https://feed.example/drip')
                    self.assertLess(time.monotonic() - started, .5)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()