"""Local HTTP + real Chromium acceptance using the existing dashboard fixture."""
import os
import io
import base64
from unittest.mock import patch
import unittest
from pathlib import Path
from PIL import Image
from tests import test_webapp as fixtures
from tests.test_event_countdown import event


class CountdownWebTests(unittest.TestCase):
    def test_saved_artwork_delete_requires_explicit_clearance(self):
        self.fixture.pair_browser()
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                path = self.fixture.media_dir / 'referenced.png'
                Image.new('RGB', (8, 8), 'red').save(path)
                self.fixture.store.update({'event_countdown': {'events': [
                    event(artwork=path.name, enabled=enabled)]}})
                before, image = self.fixture.store.path.read_bytes(), path.read_bytes()
                response = self.fixture.paired_request('delete', '/api/media/' + path.name)
                self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
                self.assertIn('countdown', response.get_json()['error'].lower())
                self.assertEqual(self.fixture.store.path.read_bytes(), before)
                self.assertEqual(path.read_bytes(), image)
                self.fixture.store.update({'event_countdown': {'events': [
                    event(artwork=None, enabled=enabled)]}})
                cleared = self.fixture.store.path.read_bytes()
                response = self.fixture.paired_request('delete', '/api/media/' + path.name)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(path.exists())
                self.assertEqual(self.fixture.store.path.read_bytes(), cleared)

    def test_preview_artwork_cancel_closes_admitted_handles_without_saving(self):
        from media_resolver import MediaResolver
        self.fixture.pair_browser()
        Image.new('RGB', (8, 8), '#ff00ff').save(self.fixture.media_dir / 'art.png')
        before = self.fixture.store.path.read_bytes()
        handles = []
        resolve = MediaResolver.resolve
        def record(resolver, reference):
            handle = resolve(resolver, reference)
            handles.append(handle)
            return handle
        with patch.object(MediaResolver, 'resolve', record):
            response = self.fixture.paired_request('post', '/api/edit-preview/event_countdown', json={
                'config': {'event_countdown': {'layout': 'icon', 'events': [event(artwork='art.png')]}},
                'generation': 1, 'preview_instance': 'a' * 32})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        with Image.open(io.BytesIO(base64.b64decode(body['frames'][0]['png']))) as image:
            self.assertEqual(image.getpixel((0, 9)), (255, 0, 255))
        cancelled = self.fixture.paired_request('post', '/api/edit-preview/cancel', json={
            'session': body['session'], 'preview_instance': 'a' * 32})
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(handles)
        self.assertTrue(all(handle.closed for handle in handles))
        self.assertEqual(self.fixture.store.path.read_bytes(), before)

    def test_missing_deleted_and_hostile_artwork_fail_closed(self):
        self.fixture.pair_browser()
        before = self.fixture.store.path.read_bytes()
        outside = self.fixture.media_dir.parent / 'outside.png'
        Image.new('RGB', (8, 8), 'red').save(outside)
        (self.fixture.media_dir / 'link.png').symlink_to(outside)
        Image.new('RGB', (8, 8), 'red').save(self.fixture.media_dir / 'deleted.png')
        (self.fixture.media_dir / 'deleted.png').unlink()
        for reference in ('missing.png', 'deleted.png', 'link.png', '../outside.png', str(outside),
                          'https://example.org/art.png', 'built-in/../outside.png', './art.png', 3, ''):
            with self.subTest(reference=reference):
                response = self.fixture.paired_request('post', '/api/edit-preview/event_countdown', json={
                    'config': {'event_countdown': {'layout': 'icon', 'events': [event(artwork=reference)]}},
                    'generation': 1, 'preview_instance': 'b' * 32})
                # Admission failures use the existing generic unavailable contract;
                # invalid schema references are rejected before admission.
                expected = 503 if reference in ('missing.png', 'deleted.png', 'link.png') else 400
                self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        self.assertEqual(self.fixture.store.path.read_bytes(), before)

    def setUp(self):
        self.fixture = fixtures.WebAppTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.pairing.verify.return_value = True
        self.fixture.controller.status.return_value = {'current_module': None, 'running': False}
        frame = io.BytesIO()
        Image.new('RGB', (64, 32)).save(frame, format='PNG')
        self.fixture.controller.frame_png.side_effect = lambda: (io.BytesIO(frame.getvalue()), 0)

    @unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
    def test_browser_local_crud_save_reload(self):
        Image.new('RGB', (8, 8), '#ff00ff').save(self.fixture.media_dir / 'countdown-art.png')
        probe = Path(__file__).with_name('event_countdown_browser.cjs').read_text()
        result = self.fixture.run_live_chromium_probe(probe, timeout=60)
        self.assertEqual(result['title'], 'Browser launch')
        self.assertEqual(result['events'], 1)
        self.assertEqual(result['layout'], 'icon')
        self.assertEqual(result['artwork'], 'countdown-art.png')
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.fixture.store.get()['event_countdown']['events'][0]['title'], 'Browser launch')
