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
        self.assertEqual(self.fixture.store.get()['device']['safe_area'],
                         {'top': 2, 'right': 2, 'bottom': 2, 'left': 2, 'media_inside': True})
        probe = Path(__file__).with_name('safe_area_browser.cjs').read_text()
        result = self.fixture.run_live_chromium_probe(probe, timeout=90)
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['markerWhileEditing'])
        expected = {'top': 2, 'right': 2, 'bottom': 2, 'left': 4, 'media_inside': True}
        self.assertEqual(result['saved'], expected)
        self.assertEqual(result['reloaded'], '2,2,2,4')
        self.assertEqual(self.fixture.store.get()['device']['safe_area'], expected)


if __name__ == '__main__':
    unittest.main()
