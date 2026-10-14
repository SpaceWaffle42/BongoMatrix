"""Roadmap 1.10d/e: displays announce each other; matrix.local is a directory."""

import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import directory
from config_store import ConfigStore
from directory import Directory, is_directory_host, parse_browse, playing_label
from pairing import PairedDeviceStore
from secret_store import SecretStore
from webapp import create_app

BROWSE = "\n".join([
    "+;wlan0;IPv4;matrix-71c2;_matrix._tcp;local",
    '=;wlan0;IPv6;matrix-71c2;_matrix._tcp;local;matrix-71c2.local;fe80::1;5000;"v=0.3.71-preview" "name=matrix-71c2" "id=matrix-71c2"',
    '=;wlan0;IPv4;matrix-71c2;_matrix._tcp;local;matrix-71c2.local;192.168.1.23;5000;"v=0.3.71-preview" "name=matrix-71c2" "id=matrix-71c2"',
    '=;wlan0;IPv4;matrix-0be4;_matrix._tcp;local;office.local;192.168.1.41;5000;"v=0.3.69-preview" "name=office" "id=matrix-0be4"',
    '=;lo;IPv4;matrix-7d03;_matrix._tcp;local;matrix-7d03.local;127.0.0.1;5000;"id=matrix-7d03"',
    '=;wlan0;IPv4;matrix-7d03;_matrix._tcp;local;matrix-7d03.local;192.168.1.5;5000;"id=matrix-7d03" "name=matrix-7d03"',
    '=;wlan0;IPv4;evil;_matrix._tcp;local;evil.local;8.8.8.8;5000;"id=evil"',
    '=;wlan0;IPv4;bad\\059name;_matrix._tcp;local;x.local;192.168.1.9;5000;"id=<script>"',
    '=;wlan0;IPv4;other;_http._tcp;local;x.local;192.168.1.10;80;',
])


class ParseTests(unittest.TestCase):
    def test_parses_resolved_lan_ipv4_matrix_services_only(self):
        peers = parse_browse(BROWSE)
        self.assertEqual(sorted(peers), ["matrix-0be4", "matrix-71c2", "matrix-7d03"])
        self.assertEqual(peers["matrix-0be4"], {
            "id": "matrix-0be4", "name": "office", "version": "0.3.69-preview",
            "address": "192.168.1.41", "port": 5000,
        })
        self.assertEqual(peers["matrix-7d03"]["address"], "192.168.1.5", "loopback row ignored")

    def test_directory_host_detection(self):
        for host in ("matrix.local", "matrix.local:5000", "MATRIX.LOCAL:5000", "matrix.local."):
            self.assertTrue(is_directory_host(host), host)
        for host in ("matrix-7d03.local:5000", "192.168.1.5:5000", "kitchen.local",
                     "matrix.localhost", "[::1]:5000", "", None):
            self.assertFalse(is_directory_host(host), host)

    def test_playing_label(self):
        self.assertEqual(playing_label("clock_lava"), "Clock lava")
        # Review r2 finding 3: filenames and draft names are never published.
        self.assertEqual(playing_label("device-draft:pet_cat"), "Preview")
        self.assertEqual(playing_label("media:Holiday photo of kids.png"), "Media")
        self.assertEqual(playing_label("other:secret"), "Display")
        self.assertIsNone(playing_label(None))

    def test_trailing_newline_is_not_a_valid_value(self):  # review r2 finding 7
        self.assertIsNone(directory._clean("abc\n"))
        from device_name import name_problem
        self.assertIsNotNone(name_problem("kitchen\n"))


def fake_name(default="matrix-7d03", current="kitchen.local"):
    name = Mock()
    name.default = default
    name.current.return_value = current
    return name


class FakeProcess:
    def __init__(self, argv, lines=("Established under name 'x'\n",), exits=False):
        self.argv = argv
        self.stdout = iter(lines)
        self._exit = 1 if exits else None
        self.terminated = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = -15

    def wait(self, timeout=None):
        return self._exit

    def kill(self):
        self._exit = -9


class DirectoryTests(unittest.TestCase):
    def make(self, browse=BROWSE, answers=None, clock=None, popen=None):
        answers = answers if answers is not None else {
            "192.168.1.23": {"id": "matrix-71c2", "name": "matrix-71c2",
                             "version": "0.3.71-preview", "playing": "Clock lava"},
            "192.168.1.41": {"id": "matrix-0be4", "name": "office",
                             "version": "0.3.69-preview", "playing": None},
        }
        self.clock = clock or [1000.0]
        self.started = []

        def default_popen(argv, **kwargs):
            process = FakeProcess(argv)
            self.started.append(process)
            return process

        return Directory(
            version="0.3.71-preview", port=5000, device_name=fake_name(),
            status=lambda: {"current_module": "clock_lava"},
            address=lambda: "192.168.1.5",
            run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, browse, ""),
            popen=popen or default_popen,
            fetch=lambda address, port: answers.get(address),
            clock=lambda: self.clock[0], wall=lambda: 1_790_000_000,
        )

    def test_self_summary_is_public_and_minimal(self):
        self.assertEqual(self.make().self_summary(), {
            "service": "matrix", "id": "matrix-7d03", "name": "kitchen",
            "version": "0.3.71-preview", "playing": "Clock lava",
        })

    def test_listing_puts_self_first_and_flags_older_versions(self):
        listing = self.make().listing()
        names = [item["name"] for item in listing["displays"]]
        self.assertEqual(names, ["kitchen", "matrix-71c2", "office"])
        me, peer, office = listing["displays"]
        self.assertTrue(me["serving"])
        self.assertEqual(me["address"], "192.168.1.5")
        self.assertFalse(peer["serving"])
        self.assertTrue(office["older_version"])
        self.assertFalse(peer["older_version"])
        self.assertTrue(all(item["online"] for item in listing["displays"]))
        self.assertEqual(listing["served_by"], "kitchen")

    def test_unconfirmed_peer_is_not_listed(self):
        listing = self.make(answers={}).listing()
        self.assertEqual([item["id"] for item in listing["displays"]], ["matrix-7d03"])

    def test_peer_answering_with_other_identity_is_not_listed(self):
        listing = self.make(answers={
            "192.168.1.23": {"id": "matrix-ffff", "name": "x", "version": None, "playing": None},
        }).listing()
        self.assertNotIn("matrix-71c2", [item["id"] for item in listing["displays"]])

    def test_quiet_peer_is_greyed_then_dropped_after_ten_minutes(self):
        directory_ = self.make()
        directory_.listing()
        directory_._run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        self.clock[0] += 120
        greyed = {item["id"]: item for item in directory_.listing()["displays"]}
        self.assertFalse(greyed["matrix-71c2"]["online"])
        self.assertEqual(greyed["matrix-71c2"]["seen_seconds"], 120)
        self.assertTrue(greyed["matrix-0be4"]["older_version"], "compared with online newest")
        self.clock[0] += 481
        self.assertEqual([item["id"] for item in directory_.listing()["displays"]], ["matrix-7d03"])

    def test_listing_is_cached_briefly(self):
        directory_ = self.make()
        first = directory_.listing()
        directory_._run = Mock(side_effect=AssertionError("browsed again"))
        self.clock[0] += 2
        self.assertIs(directory_.listing(), first)

    def test_duplicate_names_are_flagged(self):
        listing = self.make(answers={
            "192.168.1.23": {"id": "matrix-71c2", "name": "kitchen", "version": "0.3.71-preview", "playing": None},
        }).listing()
        flags = {item["id"]: item["duplicate_name"] for item in listing["displays"]}
        self.assertEqual(flags, {"matrix-7d03": True, "matrix-71c2": True})

    def test_browse_failure_still_lists_self(self):
        directory_ = self.make()
        directory_._run = Mock(side_effect=FileNotFoundError("avahi-browse"))
        self.assertEqual(len(directory_.listing()["displays"]), 1)

    def test_publishes_service_and_tries_to_hold_matrix_local(self):
        directory_ = self.make()
        directory_.supervise_once()
        argvs = [process.argv for process in self.started]
        self.assertIn([
            "avahi-publish", "-s", "matrix-7d03", "_matrix._tcp", "5000",
            "id=matrix-7d03", "name=kitchen", "v=0.3.71-preview",
        ], argvs)
        self.assertIn(["avahi-publish", "-a", "-R", "matrix.local", "192.168.1.5"], argvs)
        directory_.supervise_once()
        self.assertEqual(len(self.started), 2, "running children are kept")

    def test_lost_matrix_local_is_retried_after_delay(self):
        attempts = []

        def popen(argv, **kwargs):
            process = FakeProcess(argv, lines=(), exits="-a" in argv)
            attempts.append(argv)
            return process

        directory_ = self.make(popen=popen)
        directory_.supervise_once()
        holds = lambda: sum(1 for argv in attempts if "-a" in argv)
        self.assertEqual(holds(), 1)
        directory_.supervise_once()
        self.assertEqual(holds(), 1, "waits before retrying")
        self.clock[0] += 11
        directory_.supervise_once()
        self.assertEqual(holds(), 2, "takes over within seconds")
        self.assertFalse(directory_.holds_directory())

    def test_rename_republishes_service(self):
        directory_ = self.make()
        directory_.supervise_once()
        directory_._device_name.current.return_value = "office2.local"
        directory_.supervise_once()
        old = [p for p in self.started if "name=kitchen" in p.argv][0]
        self.assertTrue(old.terminated)
        self.assertTrue(any("name=office2" in p.argv for p in self.started))

    def test_no_address_stops_holding(self):
        directory_ = self.make()
        directory_.supervise_once()
        directory_._address = lambda: "IP unavailable"
        directory_.supervise_once()
        holder = [p for p in self.started if "-a" in p.argv][0]
        self.assertTrue(holder.terminated)


class FakeSocket:
    def __init__(self, reply):
        self.reply = reply
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        chunk, self.reply = self.reply[:size], self.reply[size:]
        return chunk


def http_reply(body, status=b"200 OK", headers=b""):
    return (b"HTTP/1.0 " + status + b"\r\nContent-Type: application/json\r\n" + headers
            + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


class FetchPeerTests(unittest.TestCase):
    def fetch(self, reply, **kwargs):
        self.calls = []

        def connect(address, timeout):
            self.calls.append((address, timeout))
            self.sock = FakeSocket(reply)
            return self.sock
        return directory.fetch_peer("192.168.1.23", 5000, connect=connect, **kwargs)

    def test_accepts_matrix_summary(self):
        peer = self.fetch(http_reply(
            b'{"service":"matrix","id":"matrix-71c2","name":"x","version":"0.3.71-preview","playing":"Clock"}'))
        self.assertEqual(peer["id"], "matrix-71c2")
        self.assertEqual(self.calls, [(("192.168.1.23", 5000), 1.5)])
        self.assertTrue(self.sock.sent.startswith(b"GET /api/directory/self HTTP/1.0\r\n"))

    def test_rejects_non_matrix_large_or_bad_answers(self):
        for body in (b'{"service":"other","id":"a"}', b"x" * 5000, b"not json",
                     b'{"service":"matrix","id":"<b>"}'):
            self.assertIsNone(self.fetch(http_reply(body)), body[:20])
        good = b'{"service":"matrix","id":"matrix-71c2"}'
        for reply in (http_reply(good, status=b"302 Found", headers=b"Location: http://127.0.0.1/\r\n"),
                      http_reply(good, headers=b"Transfer-Encoding: chunked\r\n"),
                      http_reply(good).replace(b"Content-Length: 39", b"Content-Length: 99"),
                      b"HTTP/1.0 200 OK\r\n" + b"X: " + b"a" * 9000 + b"\r\n\r\n" + good,
                      b"garbage"):
            self.assertIsNone(self.fetch(reply), reply[:30])

        def refused(address, timeout):
            raise OSError("refused")
        self.assertIsNone(directory.fetch_peer("192.168.1.23", 5000, connect=refused))


class LivePeerServer:
    """A real HTTP peer on loopback; ``behaviour`` decides each answer."""

    def __init__(self, behaviour):
        import http.server
        import socketserver

        self.hits = []
        server_self = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server_self.hits.append(self.path)
                behaviour(self)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FetchPeerLiveTests(unittest.TestCase):
    """1.10 review r2 findings 1-2, against a real socket (no opener mock)."""

    def serve(self, behaviour):
        server = LivePeerServer(behaviour)
        self.addCleanup(server.close)
        return server

    def test_redirects_are_never_followed(self):
        internal = self.serve(lambda h: (h.send_response(200), h.end_headers(),
                                         h.wfile.write(b'{"service":"matrix","id":"matrix-0001"}')))

        def redirect(handler):
            handler.send_response(302)
            handler.send_header("Location", f"http://127.0.0.1:{internal.port}/secret-internal")
            handler.end_headers()
        peer = self.serve(redirect)
        self.assertIsNone(directory.fetch_peer("127.0.0.1", peer.port))
        self.assertEqual(peer.hits, ["/api/directory/self"])
        self.assertEqual(internal.hits, [])

    def raw_drip(self, prefix, drip_byte=b"a", interval=0.1, count=200):
        """A raw TCP peer that sends ``prefix`` then drips forever (no HTTP framing)."""
        import socket as socket_module

        listener = socket_module.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def serve():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(prefix)
                    for _ in range(count):
                        if stop.wait(interval):
                            return
                        conn.sendall(drip_byte)
                except OSError:
                    return
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return listener.getsockname()[1]

    def assert_cut_off(self, port, timeout=0.5):
        started = time.monotonic()
        self.assertIsNone(directory.fetch_peer("127.0.0.1", port, timeout=timeout))
        self.assertLess(time.monotonic() - started, timeout + 0.5)

    def test_header_drip_is_cut_off_at_the_total_deadline(self):  # r3 MUST-FIX 1
        self.assert_cut_off(self.raw_drip(b"HTTP/1.0 200 OK\r\nX-A: "))

    def test_status_line_drip_is_cut_off(self):
        self.assert_cut_off(self.raw_drip(b"", drip_byte=b"H"))

    def test_chunked_size_line_drip_is_cut_off(self):
        self.assert_cut_off(self.raw_drip(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", drip_byte=b"f"))

    def test_slow_drip_peer_is_cut_off_at_the_total_deadline(self):
        def drip(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "4000")
            handler.end_headers()
            for _ in range(60):
                try:
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                except OSError:
                    return
                time.sleep(0.2)
        peer = self.serve(drip)
        started = time.monotonic()
        self.assertIsNone(directory.fetch_peer("127.0.0.1", peer.port, timeout=0.5))
        self.assertLess(time.monotonic() - started, 1.5)


class DirectoryNonBlockingTests(unittest.TestCase):
    """Finding 2: a slow peer must not wedge /api/directory or hold the lock."""

    def test_slow_peer_does_not_block_listing_past_the_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_fetch(address, port):
            release.wait(30)
            return None
        directory_service = directory.Directory(
            device_name=fake_name(), version="0.3.73-preview", port=5000,
            status=lambda: {"current_module": "clock_lava"}, address=lambda: "192.168.1.5",
            run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, BROWSE, ""),
            popen=lambda argv, **kwargs: FakeProcess(argv), fetch=slow_fetch,
        )
        results, threads = [], []
        for _ in range(3):
            thread = threading.Thread(target=lambda: results.append(directory_service.listing()))
            threads.append(thread)
            thread.start()
        started = time.monotonic()
        for thread in threads:
            thread.join(directory.PEER_TOTAL_DEADLINE_SECONDS + 2)
        self.assertEqual(len(results), 3, "listing() calls were still blocked")
        self.assertLess(time.monotonic() - started, directory.PEER_TOTAL_DEADLINE_SECONDS + 2)
        for listing in results:
            self.assertEqual(listing["displays"][0]["serving"], True)

    def test_repeated_builds_against_hung_peers_do_not_grow_threads(self):  # r3 MUST-FIX 1
        release = threading.Event()
        self.addCleanup(release.set)

        def hung_fetch(address, port):
            release.wait(30)
            return None
        clock = [1000.0]
        service = directory.Directory(
            device_name=fake_name(), version="0.3.73-preview", port=5000,
            status=lambda: {"current_module": "clock_lava"}, address=lambda: "192.168.1.5",
            run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, BROWSE, ""),
            popen=lambda argv, **kwargs: FakeProcess(argv), fetch=hung_fetch,
            clock=lambda: clock[0],
        )
        self.addCleanup(service.stop)
        with patch.object(directory, "PEER_TOTAL_DEADLINE_SECONDS", 0.05):
            for _ in range(6):
                clock[0] += directory.DIRECTORY_CACHE_SECONDS + 1
                service.listing()
        peer_threads = [t for t in threading.enumerate() if t.name.startswith("matrix-directory-peer")]
        self.assertLessEqual(len(peer_threads), directory.PEER_FETCH_WORKERS)


class DirectoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o755)
        media = root / "media"
        shutil.copytree(Path(__file__).parents[1] / "resources" / "media" / "built-in",
                        media / "built-in")
        self.directory = Mock()
        self.directory.listing.return_value = {"served_by": "kitchen", "displays": [{"id": "matrix-7d03"}]}
        self.directory.self_summary.return_value = {"service": "matrix", "id": "matrix-7d03"}
        self.controller = Mock()
        self.pairing = Mock()
        self.pairing.active_pin.return_value = None
        self.pairing.has_testing_pin.return_value = False
        self.app = create_app(
            self.controller, ConfigStore(root / "config.json"), self.pairing,
            secret_key="test", session_generation="g", media_dir=media,
            device_store=PairedDeviceStore(root / "paired-devices.json"),
            openweather_secrets=SecretStore(root / "secrets" / "k"),
            directory=self.directory,
        )
        self.client = self.app.test_client()

    def get(self, path, host="matrix.local:5000", **kwargs):
        return self.client.get(path, headers={"Host": host}, **kwargs)

    def test_matrix_local_root_is_directory_page_without_pin(self):
        response = self.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Matrix displays on this network", body)
        self.assertNotIn("PIN", body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("/api/directory", body)

    def test_matrix_local_listing_api(self):
        response = self.get("/api/directory")
        self.assertEqual(response.get_json()["served_by"], "kitchen")

    def test_matrix_local_never_exposes_dashboard_or_controls(self):
        for path in ("/pair", "/api/status", "/api/config", "/api/display-frame",
                     "/api/display-name", "/static/custom_canvas.js"):
            self.assertEqual(self.get(path).status_code, 404, path)
        for method in ("post", "put", "delete"):
            response = getattr(self.client, method)("/api/config", headers={"Host": "matrix.local"})
            self.assertEqual(response.status_code, 405)
        response = self.client.post("/pair", headers={"Host": "matrix.local:5000"},
                                    data={"pin": "123456", "device_name": "x"})
        self.assertEqual(response.status_code, 405)
        self.pairing.verify.assert_not_called()

    def test_display_address_still_serves_dashboard(self):
        response = self.get("/", host="192.168.1.5:5000")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pair", response.headers["Location"])

    def test_self_summary_is_public_on_any_host(self):
        self.assertEqual(self.get("/api/directory/self", host="192.168.1.5:5000").get_json()["id"],
                         "matrix-7d03")
        self.assertEqual(self.get("/api/directory/self").get_json()["id"], "matrix-7d03")


if __name__ == "__main__":
    unittest.main()
