"""Port 80 redirect (owner 2026-10-15: names work without :5000)."""
import http.client
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import matrix_port80_redirect as redirect

ROOT = Path(__file__).resolve().parents[1]


class RedirectLocationTests(unittest.TestCase):
    def test_same_host_on_5000_keeps_path_and_query(self):
        self.assertEqual(redirect.redirect_location('billy-matrix.local', '/pair?from=ip', '192.168.1.7'),
                         'http://billy-matrix.local:5000/pair?from=ip')
        self.assertEqual(redirect.redirect_location('matrix.local:80', '/', '192.168.1.7'), 'http://matrix.local:5000/')
        self.assertEqual(redirect.redirect_location('192.168.1.7', '/api/status', 'x'), 'http://192.168.1.7:5000/api/status')

    def test_hostile_host_or_path_falls_back(self):
        for bad in (None, '', 'evil.com/x', 'a b', 'x@evil.com', 'host\r\nSet-Cookie: a=b', 'a' * 300, '[::1]'):
            with self.subTest(host=bad):
                self.assertEqual(redirect.redirect_location(bad, '/', '192.168.1.7'), 'http://192.168.1.7:5000/')
        for path in ('//evil.com/', 'http://evil.com/', '/a\r\nX: y', '/a\n', '/' + 'a' * 3000, ''):
            with self.subTest(path=path):
                self.assertEqual(redirect.redirect_location('matrix.local', path, 'x'), 'http://matrix.local:5000/')


class RedirectServerTests(unittest.TestCase):
    def setUp(self):
        self.server = redirect.Server(('127.0.0.1', 0), redirect.RedirectHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, host):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader('Host', host)
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_get_head_and_post_redirect_with_no_body(self):
        response, body = self.request('GET', '/pair?x=1', 'billy-matrix.local')
        self.assertEqual((response.status, response.getheader('Location'), body),
                         (301, 'http://billy-matrix.local:5000/pair?x=1', b''))
        response, _ = self.request('HEAD', '/', 'matrix.local')
        self.assertEqual((response.status, response.getheader('Location')), (301, 'http://matrix.local:5000/'))
        response, _ = self.request('POST', '/api/config', 'matrix.local')
        self.assertEqual((response.status, response.getheader('Location')), (308, 'http://matrix.local:5000/api/config'))
        response, _ = self.request('GET', '/', None)
        self.assertEqual(response.getheader('Location'), 'http://127.0.0.1:5000/')

    def test_idle_connections_are_capped_and_cut_at_deadline(self):
        port = self.server.server_address[1]
        idle = [socket.create_connection(('127.0.0.1', port)) for _ in range(redirect.MAX_CONNECTIONS + 8)]
        try:
            time.sleep(0.3)
            self.assertLessEqual(threading.active_count(), redirect.MAX_CONNECTIONS + 10)
            # A slow-drip client is closed by the wall-clock deadline, not kept alive by bytes.
            drip = idle[0]
            drip.settimeout(redirect.REQUEST_DEADLINE + 3)
            started = time.monotonic()
            try:
                while drip.recv(1024):
                    pass
            except OSError:
                pass
            self.assertLess(time.monotonic() - started, redirect.REQUEST_DEADLINE + 2)
        finally:
            for sock in idle:
                sock.close()
        time.sleep(redirect.REQUEST_DEADLINE + 1)
        response, _ = self.request('GET', '/', 'matrix.local')   # recovered after the flood
        self.assertEqual(response.status, 301)


class AddressTests(unittest.TestCase):
    def test_local_addresses_are_concrete_and_include_loopback(self):
        found = redirect.local_ipv4_addresses()
        self.assertIn('127.0.0.1', found)
        self.assertNotIn('0.0.0.0', found)

    def test_never_binds_hotspot_or_wildcard(self):
        self.assertEqual(redirect.wanted_addresses({'127.0.0.1', '10.42.0.1', '0.0.0.0', '192.168.1.7'}),
                         {'127.0.0.1', '192.168.1.7'})

    def test_portal_bind_coexists_and_listeners_follow_addresses(self):
        """Review MUST-FIX 1: the portal's specific-address bind always succeeds (deterministic)."""
        probe = socket.socket(); probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
        listeners = redirect.Listeners(port)
        self.addCleanup(lambda: [listeners.close(a) for a in list(listeners.servers)])
        baseline = threading.active_count()
        listeners.sync({'127.0.0.1', '10.42.0.1'})
        self.assertEqual(set(listeners.servers), {'127.0.0.1'})
        # Stand-in for the portal on another specific loopback address, same port, SO_REUSEADDR like werkzeug.
        portal = socket.socket(); portal.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        portal.bind(('127.0.0.2', port)); portal.listen(); portal.close()
        response = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        response.request('GET', '/x', headers={'Host': 'matrix.local'})
        self.assertEqual(response.getresponse().getheader('Location'), 'http://matrix.local:5000/x')
        response.close()
        for _ in range(5):                  # open/close cycles must not leak reaper threads
            listeners.sync(set())
            listeners.sync({'127.0.0.1'})
        listeners.sync(set())
        self.assertEqual(listeners.servers, {})
        time.sleep(0.8)
        self.assertLessEqual(threading.active_count(), baseline)


class UnitTests(unittest.TestCase):
    def test_unit_is_unprivileged_bounded_and_ordered_after_setup(self):
        unit = (ROOT / 'deploy/matrix-port80.service').read_text()
        for line in ('DynamicUser=yes', 'CapabilityBoundingSet=CAP_NET_BIND_SERVICE',
                     'AmbientCapabilities=CAP_NET_BIND_SERVICE', 'NoNewPrivileges=true', 'ProtectSystem=strict',
                     'After=network-online.target matrix-network-setup.service', 'TasksMax=32', 'MemoryMax=64M',
                     'RestrictAddressFamilies=AF_INET', 'StartLimitBurst=5',
                     'ExecStart=/usr/bin/python3 -I /usr/local/libexec/matrix-port80-redirect.py'):
            self.assertIn(line + '\n', unit)
        self.assertNotIn('User=root', unit)
        self.assertNotIn('Conflicts=', unit)
        self.assertLess(redirect.MAX_CONNECTIONS + 4, 32)


if __name__ == '__main__':
    unittest.main()
