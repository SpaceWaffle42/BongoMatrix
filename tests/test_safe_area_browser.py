"""1.11 Screen Safe Area: Device settings in real Chromium (legacy config reads 2 px)."""
import json
import os
import unittest
from pathlib import Path

from tests import test_event_countdown_web as fixtures


@unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
class SafeAreaBrowserTests(unittest.TestCase):
    def setUp(self):
        owner = fixtures.CountdownWebTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        self.fixture = owner.fixture
        # A display saved before 1.11 has no safe_area key and reads as 2 px.
        path = self.fixture.store.path
        saved = json.loads(path.read_text())
        saved['device'].pop('safe_area', None)
        path.write_text(json.dumps(saved))

    def test_device_settings_edit_marker_save_and_reload(self):
        path = self.fixture.store.path
        self.assertEqual(self.fixture.store.get()['device']['safe_area'],
                         {'top': 2, 'right': 2, 'bottom': 2, 'left': 2, 'media_inside': True})
        probe = Path(__file__).with_name('safe_area_browser.cjs').read_text()
        # Spy on the file between the two saves: the unrelated first save must not write the key.
        original_update = self.fixture.store.update
        files_after_save = []

        def recording_update(*args, **kwargs):
            result = original_update(*args, **kwargs)
            files_after_save.append(json.loads(path.read_text())['device'])
            return result

        self.fixture.store.update = recording_update
        result = self.fixture.run_live_chromium_probe(probe, timeout=90)
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['markerWhileEditing'])
        # An unrelated save neither sends nor persists the projected 2 px value (rollback safety).
        self.assertEqual(result['unrelatedSave'], {'name': 'Hall', 'sentSafeArea': False})
        # Invalid edges are flagged on the input and block saving with a clear message.
        self.assertEqual([a['value'] for a in result['invalidAttempts']], ['', '9', '2.7', '-1'])
        for attempt in result['invalidAttempts']:
            with self.subTest(value=attempt['value']):
                self.assertEqual(attempt['ariaInvalid'], 'true')
                self.assertFalse(attempt['valid'])
                self.assertIn('0 to 6', attempt['validationMessage'])
                self.assertTrue(attempt['error'])
                self.assertIn('Screen Safe Area', attempt['message'])
                self.assertIn('Left', attempt['message'])
                self.assertEqual(attempt['posted'], 0)
        self.assertEqual(result['fixedInput'], {'ariaInvalid': 'false', 'valid': True})
        self.assertTrue(result['changedSentSafeArea'])
        expected = {'top': 2, 'right': 2, 'bottom': 2, 'left': 4, 'media_inside': True}
        self.assertEqual(result['saved'], expected)
        self.assertEqual(result['reloaded'], '2,2,2,4')
        self.assertEqual(self.fixture.store.get()['device']['safe_area'], expected)
        self.assertEqual(len(files_after_save), 2)
        self.assertEqual(files_after_save[0]['name'], 'Hall')
        self.assertNotIn('safe_area', files_after_save[0])
        self.assertEqual(files_after_save[1]['safe_area'], expected)


if __name__ == '__main__':
    unittest.main()
