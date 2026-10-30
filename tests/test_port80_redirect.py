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


class HotspotTests(unittest.TestCase):
    def test_hotspot_probe(self):
        self.assertTrue(redirect.hotspot_active('127.0.0.1'))
        self.assertFalse(redirect.hotspot_active('192.0.2.123'))  # TEST-NET, never local

    def test_listener_closes_while_setup_hotspot_is_up(self):
        """The portal's 10.42.0.1:80 bind must not meet a wildcard listener (review MUST-FIX 1)."""
        state = {'hotspot': False}
        probe = socket.socket(); probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
        env = {'MATRIX_REDIRECT_BIND': '127.0.0.1', 'MATRIX_REDIRECT_PORT': str(port)}
        with patch.dict('os.environ', env), patch.object(redirect, 'hotspot_active', lambda: state['hotspot']):
            threading.Thread(target=redirect.main, daemon=True).start()

            def listening():
                try:
                    socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
                    return True
                except OSError:
                    return False

            def wait(expected):
                for _ in range(40):
                    if listening() is expected:
                        return True
                    time.sleep(0.1)
                return False

            self.assertTrue(wait(True), 'redirect should listen with no hotspot')
            state['hotspot'] = True
            self.assertTrue(wait(False), 'redirect must free port 80 while the setup hotspot is up')
            portal = socket.socket(); portal.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            portal.bind(('127.0.0.1', port)); portal.listen(); portal.close()   # the portal's bind works
            state['hotspot'] = False
            self.assertTrue(wait(True), 'redirect should return after setup')


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
