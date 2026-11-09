"""Owning-main integration: shipped grouped cards, actual Save and readback."""
import copy
import io
import json
import os
from pathlib import Path
import unittest
from PIL import Image
from tests import test_webapp
from config_store import DEFAULT_CONFIG, validate_config
from display_controller import scheduled_playlist_items


class GroupedScreenIntegrationTests(unittest.TestCase):
    @staticmethod
    def configure_live_frame(fixture):
        def frame_png():
            frame = io.BytesIO()
            with Image.new('RGB', (64, 32), 'black') as image:
                image.save(frame, format='PNG')
            frame.seek(0)
            return frame, 1
        fixture.controller.frame_png.side_effect = frame_png

    def test_saved_nonfirst_legacy_pool_shipped_save_preview_and_reload(self):
        # The first case is the auditor's exact revision-17 -> 18 counterexample.
        for order, enabled, capacity in (("nonfirst", True, False), ("first", True, False),
                                         ("nonfirst", False, False), ("ordinary", True, False),
                                         ("nonfirst", True, True)):
            with self.subTest(order=order, enabled=enabled, capacity=capacity):
                fixture = test_webapp.WebAppTests()
                fixture.setUp()
                try:
                    fixture.store.path.write_text(json.dumps(test_webapp.test_config_store.ConfigStoreTests.startup_predecessor()))
                    fixture.pair_browser()
                    fixture.pairing.verify.return_value = True
                    self.configure_live_frame(fixture)
                    with Image.new('RGB', (64, 32), 'red') as image:
                        image.save(fixture.media_dir / 'grouped_upload.gif')
                    rows = copy.deepcopy(fixture.store.get()['playlist'])
                    original = rows[0]
                    legacy, modern = 'built-in/loading_squares.gif', 'built-in/splash_loading_squares.gif'
                    sources = [modern, legacy] if order == 'nonfirst' else [legacy, modern]
                    if order == 'ordinary':
                        sources = [modern, 'grouped_upload.gif']
                    original.update(source=sources[0], sources=sources, enabled=enabled)
                    original['layout']['x'] = 3
                    if capacity:
                        while len(rows) < 256:
                            rows.append({'module': 'weather', 'duration': 15, 'enabled': False})
                    initial = fixture.paired_request('post', '/api/config', json={'playlist': rows},
                        headers={'If-Match': '"17"', 'X-Matrix-Client-Contract': '5'})
                    self.assertEqual(initial.status_code, 200, initial.get_data(as_text=True))
                    self.assertEqual(initial.headers['ETag'], '"18"')
                    before = fixture.store.path.read_bytes()
                    persisted = fixture.store.get()
                    probe = Path(__file__).with_name('grouped_screen_legacy_browser.js').read_text()
                    report = fixture.run_live_chromium_probe(probe, timeout=60)
                    if report['saved']['revision'] == 18:
                        self.assertEqual(fixture.store.path.read_bytes(), before)
                    self.assertEqual(report['errors'], [])
                    self.assertEqual(report['preview']['status'], 200, report['preview'])
                    self.assertEqual(report['message'], 'Configuration saved and display reloaded.')
                    self.assertEqual(report['requests'][0]['match'], '"18"')
                    draft = report['draft']
                    self.assertEqual(report['requests'][0]['body'], draft)
                    self.assertEqual(report['repeated'], draft, 'catalog normalization must be idempotent')
                    self.assertEqual(report['saved']['revision'], 19)
                    self.assertEqual(report['saved']['playlist'], draft['playlist'])
                    self.assertEqual(report['reloaded'], draft)
                    self.assertEqual(fixture.store.get(), report['saved'])
                    self.assertEqual(draft['screen_enabled'], persisted['screen_enabled'])
                    self.assertEqual([r for r in draft['playlist'] if r['module'] != 'splash'],
                                     [r for r in persisted['playlist'] if r['module'] != 'splash'])
                    split = draft['playlist'][:len(sources) if not capacity else 1]
                    self.assertEqual([s for r in split for s in r['sources']], sources)
                    retained = next(r for r in draft['playlist'] if r.get('id') == original['id'])
                    self.assertEqual(retained['sources'], sources if capacity else [sources[0] if order == 'ordinary' else legacy])
                    for row in split:
                        self.assertEqual(row['layout'], original['layout'])
                        self.assertEqual(row['enabled'], enabled)
                        self.assertEqual(row['duration'], original['duration'])
                    if capacity:
                        self.assertEqual(draft['playlist'], persisted['playlist'])
                    else:
                        self.assertEqual(len({r['id'] for r in split}), len(sources))
                    self.assertEqual(draft['playlist'][1 if capacity else len(sources):],
                                     persisted['playlist'][1:])
                finally:
                    fixture.tearDown()

    def test_contract_five_additions_share_save_and_preview_validation(self):
        fixture = test_webapp.WebAppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.pair_browser()
        current = fixture.store.get()
        changes = {'playlist': copy.deepcopy(current['playlist'])}
        for role in ('splash', 'sleep'):
            row = copy.deepcopy(next(r for r in changes['playlist'] if r['module'] == role))
            row['id'] = role + '_added'
            row['layout']['x'] = 7
            changes['playlist'].append(row)
        candidate = fixture.store.validate_candidate(changes, client_contract=5)
        self.assertEqual(candidate['playlist'], changes['playlist'])
        response = fixture.paired_request('get', '/api/config', headers={'X-Matrix-Client-Contract': '5'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Matrix-Current-Client-Contract'], '5')
        self.assertEqual(response.headers['X-Matrix-Minimum-Client-Contract'], '3')
        for contract in ('3', '4'):
            rejected = fixture.post_config(json=changes, headers={'X-Matrix-Client-Contract': contract})
            self.assertEqual(rejected.status_code, 400)
        preview = fixture.paired_request('post', '/api/edit-preview/splash',
            headers={'X-Matrix-Client-Contract': '5'}, json={'config': changes,
            'generation': 1, 'preview_instance': 'c' * 32, 'selected_media_item_id': 'splash_added'})
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertEqual(fixture.store.get(), current)
        saved = fixture.post_config(json=changes, headers={'X-Matrix-Client-Contract': '5'})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(fixture.store.get()['playlist'], candidate['playlist'])

    def test_contract_five_cannot_destroy_current_ids_or_admit_invalid_rows(self):
        fixture = test_webapp.WebAppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.pair_browser()
        current = fixture.store.get()
        splash = next(r for r in current['playlist'] if r['module'] == 'splash')
        for defect in ('omit', 'replace', 'duplicate', 'invalid-source', 'capacity'):
            with self.subTest(defect=defect):
                playlist = copy.deepcopy(current['playlist'])
                row = next(r for r in playlist if r['module'] == 'splash')
                if defect == 'omit':
                    playlist.remove(row)
                    row['id'] = 'splash_replacement'
                    playlist.append(row)
                elif defect == 'replace':
                    row['id'] = 'splash_replacement'
                elif defect == 'duplicate':
                    playlist.append(copy.deepcopy(row))
                elif defect == 'invalid-source':
                    row['source'] = '../invalid.gif'
                    row['sources'] = [row['source']]
                else:
                    for index in range(257):
                        added = copy.deepcopy(splash)
                        added['id'] = 'splash_capacity_' + str(index)
                        playlist.append(added)
                before = fixture.store.path.read_bytes()
                for endpoint in ('save', 'preview'):
                    if endpoint == 'save':
                        response = fixture.post_config(json={'playlist': playlist}, headers={'X-Matrix-Client-Contract': '5'})
                    else:
                        response = fixture.paired_request('post', '/api/edit-preview/splash',
                            headers={'X-Matrix-Client-Contract': '5'}, json={'config': {'playlist': playlist},
                            'generation': 1, 'preview_instance': 'd' * 32})
                    self.assertEqual(response.status_code, 400, (defect, endpoint, response.get_data(as_text=True)))
                    self.assertEqual(fixture.store.path.read_bytes(), before)
        fixture.controller.reload.assert_not_called()

    def test_whole_screen_off_preserves_rows_and_prevents_scheduling(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        original = copy.deepcopy(config['playlist'])
        config['screen_enabled'] = {'splash': False, 'sleep': False}
        validate_config(config)
        self.assertEqual(config['playlist'], original)
        self.assertFalse(any(row['module'] == 'splash' for row in scheduled_playlist_items(config, active=True)))
        self.assertEqual(scheduled_playlist_items(config, active=False), [])

    def test_grouped_compatibility_focus_capacity_and_removal(self):
        for capacity in (False, True):
            with self.subTest(capacity=capacity):
                fixture = test_webapp.WebAppTests()
                fixture.setUp()
                try:
                    fixture.pairing.verify.return_value = True
                    self.configure_live_frame(fixture)
                    predecessor = fixture.store.get()
                    predecessor['custom_modules'] = [test_webapp.test_config_store.ConfigStoreTests.advanced_module()]
                    predecessor['playlist'].append({'module': predecessor['custom_modules'][0]['id'], 'duration': 12, 'enabled': True})
                    splash = next(r for r in predecessor['playlist'] if r['module'] == 'splash')
                    splash['sources'].append('built-in/loading_squares.gif')
                    splash['layout']['x'] = 3
                    duplicate = copy.deepcopy(splash)
                    duplicate.update(id='splash_duplicate', sources=[splash['source']], enabled=False)
                    unavailable = copy.deepcopy(duplicate)
                    unavailable.update(id='splash_unavailable', source='missing.gif', sources=['missing.gif'])
                    predecessor['playlist'][1:1] = [duplicate, unavailable]
                    if capacity:
                        while len(predecessor['playlist']) < 256:
                            predecessor['playlist'].append({'module': 'weather', 'duration': 15, 'enabled': False})
                    fixture.store._write(predecessor)
                    with Image.new('RGB', (64, 32), 'red') as image:
                        image.save(fixture.media_dir / 'removable.gif')
                        for index in range(24):
                            image.save(fixture.media_dir / f'{"a" if index < 4 else "z"}_overflow_{index}.gif')
                    probe = Path(__file__).with_name('grouped_screen_compat_browser.js').read_text()
                    report = fixture.run_live_chromium_probe(probe, timeout=60)
                    print('GROUPED_COMPATIBILITY ' + json.dumps(report, sort_keys=True))
                    self.assertEqual(report['errors'], [])
                    self.assertFalse((fixture.media_dir / 'removable.gif').exists())
                    self.assertEqual(fixture.store.get(), predecessor)
                finally:
                    fixture.tearDown()

    def test_grouped_cards_real_save_readback_and_reload(self):
        self.assertTrue(os.access(os.environ.get('MATRIX_CHROMIUM', ''), os.X_OK), 'actual browser required')
        fixture = test_webapp.WebAppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.pairing.verify.return_value = True
        with Image.new('RGB', (64, 32), 'red') as image:
            image.save(fixture.media_dir / 'grouped_upload.gif')
        def frame_png():
            frame = io.BytesIO()
            with Image.new('RGB', (64, 32), 'black') as image:
                image.save(frame, format='PNG')
            frame.seek(0)
            return frame, 1
        fixture.controller.frame_png.side_effect = frame_png
        probe = Path(__file__).with_name('grouped_screen_browser.js').read_text()
        report = fixture.run_live_chromium_probe(probe, timeout=60)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['saved']['playlist'], report['draft']['playlist'])
        self.assertEqual(report['saved']['screen_enabled'], {'splash': False, 'sleep': True})
        self.assertEqual(fixture.store.get(), report['saved'])
        self.assertEqual(fixture.controller.reload.call_count, 1)
        self.assertEqual(report['reloaded']['playlist'], report['draft']['playlist'])
        self.assertEqual(report['reloaded']['screen_enabled'], report['draft']['screen_enabled'])
        self.assertEqual(report['viewport'], 390)

    def test_screen_media_edit_dialog_for_uploads_on_insecure_http(self):
        fixture = test_webapp.WebAppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.pairing.verify.return_value = True
        with Image.new('RGB', (64, 32), 'blue') as image:
            image.save(fixture.media_dir / 'editor_upload.gif')
        def frame_png():
            frame = io.BytesIO()
            with Image.new('RGB', (64, 32), 'black') as image:
                image.save(frame, format='PNG')
            frame.seek(0)
            return frame, 1
        fixture.controller.frame_png.side_effect = frame_png
        probe = Path(__file__).with_name('screen_media_editor_browser.js').read_text()
        report = fixture.run_live_chromium_probe(probe, timeout=60)
        self.assertEqual(report['errors'], [])
        self.assertTrue(report['random_uuid_absent'])
        self.assertEqual(sorted(report['roles']), ['sleep', 'splash'])
        self.assertTrue(report['phone_dialog_fits'])
        self.assertFalse(report['phone_overflow'])
        # Owner decision 2: presets and stage use the Safe Area's inner canvas.
        self.assertEqual(report['safe_area_editor'], {'fill': [52, 20], 'canvas': '52x20'})

    def test_dashboard_modules_share_visual_contract(self):
        fixture = test_webapp.WebAppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.pairing.verify.return_value = True
        for name, colour in (('consistency_a.gif', 'blue'), ('consistency_b.png', 'green')):
            with Image.new('RGB', (64, 32), colour) as image:
                image.save(fixture.media_dir / name)
        def frame_png():
            frame = io.BytesIO()
            with Image.new('RGB', (64, 32), 'black') as image:
                image.save(frame, format='PNG')
            frame.seek(0)
            return frame, 1
        fixture.controller.frame_png.side_effect = frame_png
        probe = Path(__file__).with_name('ui_consistency_browser.js').read_text()
        report = fixture.run_live_chromium_probe(probe, timeout=90)
        self.assertEqual(report['errors'], [])
        self.assertEqual(sorted(report['viewports']), ['1440', '390'])
