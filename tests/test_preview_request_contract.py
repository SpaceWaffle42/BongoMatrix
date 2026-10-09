"""Own admission + public-route characterization; no production state used."""
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.preview_request_contract import RequestGate, TIMING_POSTS

class RequestContractTests(unittest.TestCase):
    def test_exact_observational_methods_paths_and_origins(self):
        gate = RequestGate('http://127.0.0.1:5001/')
        for path in TIMING_POSTS:
            self.assertTrue(gate.permits('POST', 'http://127.0.0.1:5001'+path))
        for path in ('/api/config', '/api/media', '/api/settings', '/api/start',
                     '/api/stop', '/api/restart', '/api/preview/sleep',
                     '/api/edit-preview/sleep', '/api/pair',
                     '/api/scroll-timing/clock', '/api/scroll-timing/date',
                     '/api/scroll-timing/weather/extra'):
            self.assertFalse(gate.permits('POST', 'http://127.0.0.1:5001'+path), path)
        for method in ('PUT', 'PATCH', 'DELETE'):
            for path in TIMING_POSTS:
                self.assertFalse(gate.permits(method, 'http://127.0.0.1:5001'+path))
        self.assertFalse(gate.permits('POST', 'http://elsewhere:5001/api/scroll-timing/weather'))

    def test_block_before_dispatch_and_explicit_one_shot_save(self):
        gate = RequestGate('http://127.0.0.1:5001/', product_journey=True)
        route = Mock()
        route.request.method = 'POST'
        route.request.url = 'http://127.0.0.1:5001/api/config'
        gate.dispatch(route)
        route.abort.assert_called_once_with('blockedbyclient')
        route.continue_.assert_not_called()
        gate.save_armed = True
        self.assertTrue(gate.permits('POST', route.request.url))
        self.assertFalse(gate.permits('POST', route.request.url))

    def test_public_timing_routes_preserve_saved_state_and_do_not_control_hardware(self):
        from tests.playwright_rectify_preview import build
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            app, cookie, store = build(state)
            client = app.test_client()
            client.set_cookie('session', cookie)
            def snapshot():
                return {str(p.relative_to(state)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in state.rglob('*') if p.is_file()}
            before = snapshot()
            saved = store.get()
            outcomes = {}
            try:
                # Block outbound network itself, never fabricate a successful response.
                with patch.object(socket, 'create_connection', side_effect=OSError('offline route proof')):
                    for path in sorted(TIMING_POSTS):
                        module = path.rsplit('/', 1)[-1]
                        payload = {'base_revision': saved['revision'], 'settings': saved[module]}
                        response = client.post(path, json=payload, headers={'X-Matrix-Client-Contract':'5', 'X-Matrix-Action':'paired-request'})
                        outcomes[module] = response.status_code
                        self.assertEqual(response.status_code, 200 if module == 'announcement_board' else 503)
                        self.assertEqual(store.get(), saved)
                        self.assertEqual(snapshot(), before)
                self.assertEqual(client.post('/api/scroll-timing/clock', json={}, headers={'X-Matrix-Action':'paired-request', 'X-Matrix-Client-Contract':'5'}).status_code, 404)
                self.assertEqual(client.post('/api/scroll-timing/date', json={}, headers={'X-Matrix-Action':'paired-request', 'X-Matrix-Client-Contract':'5'}).status_code, 404)
                # Controller is the injected hardware-disabled seam: no native commands.
                controller = app.extensions['offline_controller']
                self.assertEqual(controller.mock_calls, [])
                print('PUBLIC_ROUTE_PROOF', json.dumps(outcomes, sort_keys=True), 'saved_files_unchanged')
            finally:
                app.extensions['close_edit_preview_services']()
