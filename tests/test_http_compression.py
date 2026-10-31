import gzip
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from flask import Flask, Response, jsonify, stream_with_context

import http_compression
from config_store import ConfigStore
from pairing import PairedDeviceStore
import webapp

BIG_TEXT = ("<p>matrix dashboard payload</p>\n" * 200).encode()
GZIP = {"Accept-Encoding": "gzip"}


def build_app(static_dir):
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    http_compression.register_http_compression(app)

    @app.get("/html")
    def html():
        return Response(BIG_TEXT, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/vary")
    def vary():
        return Response(BIG_TEXT, mimetype="text/html", headers={"Vary": "Cookie"})

    @app.get("/has-vary")
    def has_vary():
        return Response(BIG_TEXT, mimetype="text/html",
                        headers={"Vary": "accept-encoding, Cookie"})

    @app.get("/json")
    def json_big():
        return jsonify({"items": ["x" * 40] * 100})

    @app.get("/json-small")
    def json_small():
        return jsonify({"ok": True})

    @app.get("/exact")
    def exact():
        return Response(b"a" * 1024, mimetype="text/css")

    @app.get("/below")
    def below():
        return Response(b"a" * 1023, mimetype="text/css")

    @app.get("/png")
    def png():
        return Response(b"\x89PNG" + b"\0" * 5000, mimetype="image/png")

    @app.get("/gif")
    def gif():
        return Response(b"GIF89a" + b"\0" * 5000, mimetype="image/gif")

    @app.get("/mp4")
    def mp4():
        return Response(b"\0" * 5000, mimetype="video/mp4")

    @app.get("/svg")
    def svg():
        return Response(b"<svg>" + b" " * 3000 + b"</svg>", mimetype="image/svg+xml")

    @app.get("/js")
    def js():
        return Response(b"var a=1;" * 400, mimetype="application/javascript")

    @app.get("/encoded")
    def encoded():
        return Response(gzip.compress(BIG_TEXT), mimetype="text/html",
                        headers={"Content-Encoding": "gzip"})

    @app.get("/stream")
    def stream():
        return Response(stream_with_context(iter([BIG_TEXT, BIG_TEXT])), mimetype="text/html")

    @app.get("/partial")
    def partial():
        return Response(BIG_TEXT[:2000], status=206, mimetype="text/html",
                        headers={"Content-Range": "bytes 0-1999/9999"})

    @app.get("/notfound-big")
    def notfound_big():
        return Response(BIG_TEXT, status=404, mimetype="text/html")

    @app.get("/no-transform")
    def no_transform():
        return Response(BIG_TEXT, mimetype="text/html",
                        headers={"Cache-Control": "no-transform"})

    @app.get("/etag-api")
    def etag_api():
        return Response(BIG_TEXT, mimetype="application/json", headers={"ETag": '"17"'})

    return app


class CompressionRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = build_app(Path(self.temp.name))
        self.client = self.app.test_client()

    def assert_gzipped(self, response, original):
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        body = response.get_data()
        self.assertEqual(int(response.headers["Content-Length"]), len(body))
        self.assertLess(len(body), len(original))
        self.assertEqual(gzip.decompress(body), original)

    def assert_untouched(self, response, original):
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.get_data(), original)

    def test_text_types_are_gzipped_and_round_trip(self):
        cases = {
            "/html": BIG_TEXT,
            "/js": b"var a=1;" * 400,
            "/svg": b"<svg>" + b" " * 3000 + b"</svg>",
        }
        for path, original in cases.items():
            with self.subTest(path=path):
                self.assert_gzipped(self.client.get(path, headers=GZIP), original)
        response = self.client.get("/json", headers=GZIP)
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(
            gzip.decompress(response.get_data()),
            self.client.get("/json").get_data(),
        )

    def test_css_at_exact_threshold_is_gzipped_but_one_byte_less_is_not(self):
        self.assert_gzipped(self.client.get("/exact", headers=GZIP), b"a" * 1024)
        self.assert_untouched(self.client.get("/below", headers=GZIP), b"a" * 1023)

    def test_no_compression_without_gzip_accept_encoding(self):
        for headers in ({}, {"Accept-Encoding": "identity"}, {"Accept-Encoding": "br"},
                        {"Accept-Encoding": "gzip;q=0"}, {"Accept-Encoding": "gzip; q=0.0, br"}):
            with self.subTest(headers=headers):
                self.assert_untouched(self.client.get("/html", headers=headers), BIG_TEXT)

    def test_accept_encoding_variants_that_allow_gzip(self):
        for value in ("gzip, deflate, br", "GZIP", "deflate, gzip;q=0.5", "br;q=1.0, gzip;q=0.1"):
            with self.subTest(value=value):
                self.assert_gzipped(
                    self.client.get("/html", headers={"Accept-Encoding": value}), BIG_TEXT)

    def test_media_and_binary_types_are_never_compressed(self):
        for path in ("/png", "/gif", "/mp4"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=GZIP)
                self.assertIsNone(response.headers.get("Content-Encoding"))
                self.assertGreater(len(response.get_data()), 1024)
                self.assertNotIn("Accept-Encoding", response.headers.get("Vary", ""))

    def test_small_json_status_poll_is_left_alone(self):
        response = self.client.get("/json-small", headers=GZIP)
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.get_json(), {"ok": True})
        self.assertEqual(int(response.headers["Content-Length"]), len(response.get_data()))

    def test_already_encoded_streamed_partial_and_error_responses_are_untouched(self):
        encoded = self.client.get("/encoded", headers=GZIP)
        self.assertEqual(encoded.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(encoded.get_data()), BIG_TEXT)  # not double-compressed
        self.assert_untouched(self.client.get("/stream", headers=GZIP), BIG_TEXT * 2)
        partial = self.client.get("/partial", headers=GZIP)
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.headers["Content-Range"], "bytes 0-1999/9999")
        self.assert_untouched(partial, BIG_TEXT[:2000])
        missing = self.client.get("/notfound-big", headers=GZIP)
        self.assertEqual(missing.status_code, 404)
        self.assert_untouched(missing, BIG_TEXT)

    def test_range_requests_are_never_compressed(self):
        response = self.client.get("/html", headers={**GZIP, "Range": "bytes=0-99"})
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.get_data(), BIG_TEXT)

    def test_no_transform_is_respected(self):
        self.assert_untouched(self.client.get("/no-transform", headers=GZIP), BIG_TEXT)

    def test_vary_is_appended_not_clobbered(self):
        response = self.client.get("/vary", headers=GZIP)
        values = [part.strip() for part in response.headers["Vary"].split(",")]
        self.assertEqual(values, ["Cookie", "Accept-Encoding"])
        plain = self.client.get("/vary")  # the identity variant must also say it varies
        self.assertIn("Accept-Encoding", plain.headers["Vary"])
        self.assertIn("Cookie", plain.headers["Vary"])
        again = self.client.get("/html", headers=GZIP)
        self.assertEqual(again.headers["Vary"], "Accept-Encoding")

    def test_vary_is_not_duplicated_when_already_present(self):
        response = self.client.get("/has-vary", headers=GZIP)
        self.assertEqual(response.headers["Vary"], "accept-encoding, Cookie")

    def test_cache_control_and_dynamic_etag_are_preserved(self):
        response = self.client.get("/html", headers=GZIP)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        etag = self.client.get("/etag-api", headers=GZIP)
        self.assertEqual(etag.headers["Content-Encoding"], "gzip")
        self.assertEqual(etag.headers["ETag"], '"17"')  # revision tag stays strong for If-Match

    def test_head_requests_report_gzip_headers_without_a_body(self):
        get = self.client.get("/html", headers=GZIP)
        head = self.client.head("/html", headers=GZIP)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["Content-Encoding"], "gzip")
        self.assertEqual(head.headers["Content-Length"], get.headers["Content-Length"])
        self.assertEqual(head.get_data(), b"")

    def test_gzip_output_is_deterministic(self):
        first = self.client.get("/html", headers=GZIP).get_data()
        second = self.client.get("/html", headers=GZIP).get_data()
        self.assertEqual(first, second)


class StaticCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.static = Path(self.temp.name)
        self.script = self.static / "app.js"
        self.body = b"function matrix(){return 1;}\n" * 300
        self.script.write_bytes(self.body)
        (self.static / "pixel.png").write_bytes(b"\x89PNG" + b"\0" * 4000)
        (self.static / "tiny.css").write_text("a{}")
        self.app = build_app(self.static)
        self.client = self.app.test_client()
        self.cache = self.app.extensions["http_compression_static_cache"]

    def test_static_text_is_gzipped_with_validators_and_round_trips(self):
        response = self.client.get("/static/app.js", headers=GZIP)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", response.headers["Vary"])
        self.assertEqual(int(response.headers["Content-Length"]), len(response.get_data()))
        self.assertEqual(gzip.decompress(response.get_data()), self.body)
        self.assertTrue(response.headers["ETag"].startswith('W/"'))
        self.assertIn("Last-Modified", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_static_without_gzip_is_the_original_file(self):
        response = self.client.get("/static/app.js")
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.get_data(), self.body)
        self.assertFalse(response.headers["ETag"].startswith("W/"))

    def test_static_png_and_tiny_files_are_not_compressed(self):
        for name in ("pixel.png", "tiny.css"):
            with self.subTest(name=name):
                response = self.client.get(f"/static/{name}", headers=GZIP)
                self.assertIsNone(response.headers.get("Content-Encoding"))
                self.assertEqual(response.get_data(), (self.static / name).read_bytes())

    def test_static_revalidation_returns_304_for_both_encodings(self):
        gz = self.client.get("/static/app.js", headers=GZIP)
        plain = self.client.get("/static/app.js")
        for first, headers in ((gz, GZIP), (plain, {})):
            with self.subTest(headers=headers):
                conditional = self.client.get(
                    "/static/app.js", headers={**headers, "If-None-Match": first.headers["ETag"]})
                self.assertEqual(conditional.status_code, 304)
                self.assertEqual(conditional.get_data(), b"")
                self.assertIsNone(conditional.headers.get("Content-Encoding"))
        cross = self.client.get(
            "/static/app.js", headers={**GZIP, "If-None-Match": plain.headers["ETag"]})
        self.assertEqual(cross.status_code, 304)
        by_date = self.client.get("/static/app.js", headers={
            **GZIP, "If-Modified-Since": gz.headers["Last-Modified"]})
        self.assertEqual(by_date.status_code, 304)

    def test_static_head_and_range_are_safe(self):
        head = self.client.head("/static/app.js", headers=GZIP)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.get_data(), b"")
        full = self.client.get("/static/app.js", headers=GZIP)
        self.assertEqual(head.headers["Content-Length"], full.headers["Content-Length"])
        ranged = self.client.get("/static/app.js", headers={**GZIP, "Range": "bytes=0-9"})
        self.assertEqual(ranged.status_code, 206)
        self.assertIsNone(ranged.headers.get("Content-Encoding"))
        self.assertEqual(ranged.get_data(), self.body[:10])

    def test_missing_static_file_still_404s(self):
        self.assertEqual(self.client.get("/static/nope.js", headers=GZIP).status_code, 404)
        self.assertEqual(self.client.get("/static/../app.js", headers=GZIP).status_code, 404)

    def test_static_compressed_bytes_are_reused_across_requests(self):
        original = http_compression.gzip.compress
        calls = []

        def counting(data, *args, **kwargs):
            calls.append(len(data))
            return original(data, *args, **kwargs)

        http_compression.gzip.compress = counting
        self.addCleanup(setattr, http_compression.gzip, "compress", original)
        first = self.client.get("/static/app.js", headers=GZIP).get_data()
        second = self.client.get("/static/app.js", headers=GZIP).get_data()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_static_cache_is_invalidated_when_file_changes(self):
        first = self.client.get("/static/app.js", headers=GZIP)
        new_body = b"function changed(){return 2;}\n" * 300
        self.script.write_bytes(new_body)
        stat = self.script.stat()
        os.utime(self.script, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        second = self.client.get("/static/app.js", headers=GZIP)
        self.assertEqual(gzip.decompress(second.get_data()), new_body)
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_static_cache_is_bounded(self):
        for index in range(http_compression.STATIC_CACHE_MAX_ENTRIES + 10):
            path = self.static / f"file{index}.js"
            path.write_bytes(f"var v{index}=1;\n".encode() * 200)
            self.assertEqual(
                self.client.get(f"/static/file{index}.js", headers=GZIP).status_code, 200)
        self.assertLessEqual(len(self.cache), http_compression.STATIC_CACHE_MAX_ENTRIES)


class MatrixAppCompressionTests(unittest.TestCase):
    """The real create_app: dashboard, API and static behaviour."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o755)
        media = root / "media"
        media.mkdir()
        media.chmod(0o755)
        self.controller = Mock()
        self.pairing = Mock()
        self.pairing.active_pin.return_value = None
        self.pairing.has_testing_pin.return_value = False
        self.pairing.verify.return_value = True
        self.app = webapp.create_app(
            self.controller, ConfigStore(root / "config.json"), self.pairing,
            secret_key="test", session_generation="generation-one", media_dir=media,
            device_store=PairedDeviceStore(root / "paired-devices.json"),
        )
        self.client = self.app.test_client()

    def pair(self):
        self.client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})

    def test_dashboard_is_gzipped_but_stays_uncacheable(self):
        self.pair()
        plain = self.client.get("/")
        compressed = self.client.get("/", headers=GZIP)
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(compressed.headers["Content-Encoding"], "gzip")
        self.assertEqual(compressed.headers["Cache-Control"], "no-store")
        self.assertNotIn("ETag", compressed.headers)
        self.assertIn("Cookie", compressed.headers["Vary"])
        self.assertIn("Accept-Encoding", compressed.headers["Vary"])
        self.assertEqual(int(compressed.headers["Content-Length"]), len(compressed.get_data()))
        self.assertEqual(gzip.decompress(compressed.get_data()), plain.get_data())
        self.assertLess(len(compressed.get_data()) * 3, len(plain.get_data()))

    def test_api_json_is_gzipped_with_unchanged_revision_etag(self):
        self.pair()
        plain = self.client.get("/api/config")
        compressed = self.client.get("/api/config", headers=GZIP)
        self.assertEqual(compressed.headers["Content-Encoding"], "gzip")
        self.assertEqual(compressed.headers["ETag"], plain.headers["ETag"])
        self.assertEqual(gzip.decompress(compressed.get_data()), plain.get_data())
        self.assertEqual(
            compressed.headers["X-Matrix-Current-Client-Contract"],
            plain.headers["X-Matrix-Current-Client-Contract"],
        )

    def test_small_status_poll_is_not_compressed(self):
        self.pair()
        status = self.client.get("/api/status", headers=GZIP)
        self.assertEqual(status.status_code, 200)
        self.assertIsNone(status.headers.get("Content-Encoding"))

    def test_pair_page_still_pairs_when_the_browser_accepts_gzip(self):
        page = self.client.get("/pair", headers=GZIP)
        body = gzip.decompress(page.get_data()) if page.headers.get("Content-Encoding") else page.get_data()
        self.assertIn(b'name="pin"', body)
        paired = self.client.post(
            "/pair", data={"pin": "123456", "device_name": "Test browser"}, headers=GZIP)
        self.assertEqual(paired.status_code, 302)
        self.assertEqual(paired.headers["Location"], "/")
        self.assertIn("session=", paired.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/", headers=GZIP).status_code, 200)

    def test_display_frame_png_is_never_compressed(self):
        self.pair()
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 4096
        self.controller.frame_png.return_value = (io.BytesIO(png), "r1")
        response = self.client.get("/api/display-frame", headers=GZIP)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.get_data(), png)

    def test_real_static_assets_are_gzipped_cached_and_revalidate_to_304(self):
        response = self.client.get("/static/custom_canvas.js", headers=GZIP)
        raw = Path(webapp.__file__).parent.joinpath("app/static/custom_canvas.js").read_bytes()
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(response.get_data()), raw)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertIn("Last-Modified", response.headers)
        again = self.client.get(
            "/static/custom_canvas.js",
            headers={**GZIP, "If-None-Match": response.headers["ETag"]})
        self.assertEqual(again.status_code, 304)

    def test_session_cookie_and_csrf_behaviour_are_unchanged(self):
        unpaired = self.client.post("/api/config", json={}, headers=GZIP)
        self.assertEqual(unpaired.status_code, 401)
        self.pair()
        missing_header = self.client.post("/api/config", json={}, headers=GZIP)
        self.assertEqual(missing_header.status_code, 403)


if __name__ == "__main__":
    unittest.main()
