"""Port 80 redirect (owner 2026-10-15: names work without :5000)."""
import http.client
import threading
import unittest
from pathlib import Path

from deploy import matrix_port80_redirect as redirect

ROOT = Path(__file__).resolve().parents[1]


class RedirectLocationTests(unittest.TestCase):
    def test_same_host_on_5000_keeps_path_and_query(self):
        self.assertEqual(redirect.redirect_location('billy-matrix.local', '/pair?from=ip', '192.168.1.7'),
                         'http://billy-matrix.local:5000/pair?from=ip')
        self.assertEqual(redirect.redirect_location('matrix.local:80', '/', '192.168.1.7'),
                         'http://matrix.local:5000/')
        self.assertEqual(redirect.redirect_location('192.168.1.7', '/api/status', 'x'),
                         'http://192.168.1.7:5000/api/status')
        self.assertEqual(redirect.redirect_location('[fe80::1]:80', '/', 'x'), 'http://[fe80::1]:5000/')

    def test_hostile_host_or_path_falls_back(self):
        for bad in (None, '', 'evil.com/x', 'a b', 'x@evil.com', 'host\r\nSet-Cookie: a=b', 'a' * 300):
            with self.subTest(host=bad):
                self.assertEqual(redirect.redirect_location(bad, '/', '192.168.1.7'), 'http://192.168.1.7:5000/')
        for path in ('//evil.com/', 'http://evil.com/', '/a\r\nX: y', '/' + 'a' * 3000, ''):
            with self.subTest(path=path):
                self.assertEqual(redirect.redirect_location('matrix.local', path, 'x'), 'http://matrix.local:5000/')
        self.assertEqual(redirect.redirect_location(None, '/', 'fe80::1'), 'http://[fe80::1]:5000/')


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

    def test_get_and_post_redirect_with_no_body(self):
        response, body = self.request('GET', '/pair?x=1', 'billy-matrix.local')
        self.assertEqual((response.status, response.getheader('Location'), body),
                         (301, 'http://billy-matrix.local:5000/pair?x=1', b''))
        response, _ = self.request('POST', '/api/config', 'matrix.local')
        self.assertEqual((response.status, response.getheader('Location')), (308, 'http://matrix.local:5000/api/config'))
        response, _ = self.request('GET', '/', None)
        self.assertEqual(response.getheader('Location'), 'http://127.0.0.1:5000/')


class UnitTests(unittest.TestCase):
    def test_unit_is_unprivileged_and_runs_installed_copy(self):
        unit = (ROOT / 'deploy/matrix-port80.service').read_text()
        for line in ('DynamicUser=yes', 'CapabilityBoundingSet=CAP_NET_BIND_SERVICE',
                     'AmbientCapabilities=CAP_NET_BIND_SERVICE', 'NoNewPrivileges=true', 'ProtectSystem=strict',
                     'ExecStart=/usr/bin/python3 -I /usr/local/libexec/matrix-port80-redirect.py'):
            self.assertIn(line + '\n', unit)
        self.assertNotIn('User=root', unit)


if __name__ == '__main__':
    unittest.main()
