"""Offline public validation + real raster seams; fake hardware transport only.
No sockets, GPIO, production state, browser acceptance or incident attribution.
"""
import base64
import copy
import importlib.util
import io
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from tests import test_webapp as fixtures
from device_preview import DevicePreviewRequest
from display_controller import DisplayController, playlist_module_config
from frame_capture import FrameCapture
from matrix_framework import Matrix

W = Path(__file__).resolve().parents[1]
DRIVER = W / 'deploy/feature_release_browser.mjs'
spec = importlib.util.spec_from_file_location('pixel_caller', DRIVER.with_name('feature_release_acceptance.py'))
caller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caller)


class Canvas:
    width = 64
    height = 32

    def __init__(self):
        self.image = Image.new('RGB', (64, 32))

    def Clear(self):
        self.image.paste((0, 0, 0), (0, 0, 64, 32))

    def SetImage(self, image, x, y):
        self.image.paste(image, (x, y))


class FakeHardware:
    width = 64
    height = 32

    def __init__(self):
        self.frames = []

    def SwapOnVSync(self, canvas):
        self.frames.append(canvas.image.convert('RGBA').tobytes())
        return Canvas()


def native_matrix():
    matrix = Matrix.__new__(Matrix)
    matrix.matrix = FakeHardware()
    matrix.canvas = Canvas()
    matrix._frame_capture = FrameCapture(64, 32)
    matrix._frame_capture.register(matrix.canvas)
    matrix._publication_guard = None
    matrix._physical_swap_observer = None
    return matrix


def pattern(path, phase_offset=0):
    frames = []
    for phase in range(2):
        phase += phase_offset
        image = Image.new('RGB', (64, 32))
        image.putdata([(255 if (x // 3 + y // 2 + phase) % 2 else 0,
                        (x * 17 + phase * 51) % 256, (y * 23 + x * 7) % 256)
                       for y in range(32) for x in range(64)])
        frames.append(image)
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=[20, 30], loop=0, optimize=False)
    for image in frames:
        image.close()


class ReleasePixelPaths(unittest.TestCase):
    def fixture(self, role, selected_first=False, geometry=None):
        f = fixtures.WebAppTests()
        f.setUp()
        def cleanup():
            f.app.extensions['close_edit_preview_services']()
            f.controller.device_preview_manager.close()
            f.doCleanups()
            f.tearDown()
        self.addCleanup(cleanup)
        f.physical_draft_fixture()
        pattern(f.media_dir / 'public-pattern.gif')
        pattern(f.media_dir / 'public-earlier.gif', 1)
        rows = copy.deepcopy(f.store.get()['playlist'])
        first = next(r for r in rows if r['module'] == role)
        first.update(enabled=False, source='public-earlier.gif', sources=['public-earlier.gif'])
        first['layout'].update(x=0, y=0, width=64, height=32, keep_aspect=False)
        selected = copy.deepcopy(first)
        selected.update(id=role + '_selected', enabled=True,
                        source='public-pattern.gif', sources=['public-pattern.gif'])
        selected['layout'].update(x=7, **(geometry or {}))
        rows.insert(rows.index(first) if selected_first else len(rows), selected)
        f.store.update({'playlist': rows}, client_contract=5)
        saved = f.store.get()
        draft = {k: copy.deepcopy(v) for k, v in saved.items()
                 if k not in ('revision', 'schema_version')}
        return f, draft, next(r for r in draft['playlist'] if r.get('id') == selected['id'])

    def projection(self, draft, role, selected):
        line = next(line.strip() for line in DRIVER.read_text().splitlines()
                    if line.strip().startswith('const native=structuredClone(draft);'))
        code = "const assert=require('node:assert/strict');const {draft,role,selected}=JSON.parse(process.argv[1]);" + line
        code += 'console.log(JSON.stringify({native,draft}));'
        proc = subprocess.run(['node', '-e', code, json.dumps(dict(
            draft=draft, role=role, selected=selected))], capture_output=True,
            text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['draft'], draft, 'serialized projection must not mutate UI state')
        return result['native']

    def check_path(self, role, selected_first=False, geometry=None):
        f, draft, selected = self.fixture(role, selected_first, geometry)
        before = copy.deepcopy(draft)
        saved = f.store.path.read_bytes()
        native = self.projection(draft, role, selected)
        self.assertCountEqual([r.get('id') for r in native['playlist']], [r.get('id') for r in draft['playlist']])
        self.assertEqual([r for r in native['playlist'] if r['module'] != role],
                         [r for r in draft['playlist'] if r['module'] != role])
        self.assertEqual(native['custom_modules'], draft['custom_modules'])
        ordered = copy.deepcopy(draft['playlist'])
        first = next(i for i, r in enumerate(ordered) if r['module'] == role)
        chosen = next(r for r in ordered if r.get('id') == selected['id'])
        ordered.remove(chosen)
        ordered.insert(first, chosen)
        for row in ordered:
            if row['module'] == role:
                row['enabled'] = row['id'] == selected['id']
        self.assertEqual(native['playlist'], ordered, 'only selected-first move and role enabled flags')
        self.assertEqual(native['screen_enabled'], dict(draft['screen_enabled'], **{role: True}))
        control = dict(generation=1, preview_instance='e' * 32)
        headers = {'X-Matrix-Client-Contract': '5'}
        inspector = f.paired_request('POST', '/api/edit-preview/' + role,
            json=dict(config=draft, selected_media_item_id=selected['id'], **control), headers=headers)
        self.assertEqual(inspector.status_code, 200, inspector.get_json())
        frames = inspector.get_json()['frames']
        expected = []
        for frame in frames:
            with Image.open(io.BytesIO(base64.b64decode(frame['png']))) as image:
                self.assertEqual(image.size, (64, 32))
                rgba = image.convert('RGBA').tobytes()
            self.assertEqual(caller.png_rgba(frame['png']), rgba)
            expected.append(rgba)
        admission = f.paired_request('POST', '/api/device-preview/' + role,
            json=dict(config=native, **control), headers=headers)
        self.assertEqual(admission.status_code, 202, admission.get_json())
        active = f.controller.device_preview_manager.active_lease()
        admitted = active.request
        matrix = native_matrix()
        # Bound only this synchronous offline render, not product admission semantics.
        request = DevicePreviewRequest(admitted.owner, admitted.generation, admitted.config,
                                       timeout_seconds=1, pinned_media=admitted.pinned_media)
        rendered = DisplayController.render_device_draft(None, matrix, request, stop_event=threading.Event())
        self.assertTrue(rendered)
        self.assertEqual(len(expected), 2)
        self.assertEqual(len(matrix.matrix.frames), 2)
        png, revision = matrix.latest_frame_png()
        self.assertGreater(revision, 0)
        self.assertEqual(caller.png_rgba(base64.b64encode(png.getvalue()).decode()), matrix.matrix.frames[-1])
        with patch.object(f.controller.device_preview_manager, '_clock', return_value=active.last_heartbeat_at + 5):
            self.assertEqual(f.paired_request('POST', '/api/device-preview/heartbeat', json=control).status_code, 202)
        foreign = dict(control, preview_instance='f' * 32)
        self.assertEqual(f.paired_request('POST', '/api/device-preview/heartbeat', json=foreign).status_code, 409)
        self.assertEqual(f.paired_request('POST', '/api/device-preview/cancel', json=control).status_code, 202)
        self.assertEqual(draft, before)
        self.assertEqual(f.store.path.read_bytes(), saved)
        effective = playlist_module_config(admitted.config['candidate'], role)['media_layout'][role]
        mismatch = sum(a != b for a, b in zip(expected[0][::4], matrix.matrix.frames[0][::4]))
        print('PIXEL_PATH ' + json.dumps(dict(role=role, selected_first=selected_first,
              geometry=geometry, effective_x=effective['x'], revision=revision,
              timeline_equal=expected == matrix.matrix.frames, red_channel_mismatch=mismatch)))
        self.assertEqual(expected, matrix.matrix.frames, 'exact entire decoded RGBA timeline, selected row x=7')
        self.assertEqual(effective['x'], 7)

    def test_resized_selected_first(self):
        for role in ('splash', 'sleep'):
            with self.subTest(role=role):
                self.check_path(role, True, dict(width=31, height=17, keep_aspect=False))

    def test_thumbnail_selected_first(self):
        self.check_path('sleep', True, dict(width=31, height=17, keep_aspect=True))

    def test_selected_later_splash(self):
        self.check_path('splash')

    def test_selected_later_sleep(self):
        self.check_path('sleep')

    def test_selected_first_splash_control(self):
        self.check_path('splash', True)

    def test_selected_first_sleep_control(self):
        self.check_path('sleep', True)


if __name__ == '__main__':
    unittest.main()
