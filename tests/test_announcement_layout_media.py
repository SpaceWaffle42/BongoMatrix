"""Owner note on 5002 (2026-10-14): "All layouts apart from text layout should be
greyed out if there is no media selected, or should give the user a [reason]".
Real dashboard in Chromium (scenario in editor_usability_browser.cjs).
"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests import test_event_countdown_web as fixtures


@unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
class AnnouncementLayoutMediaTests(unittest.TestCase):
    def test_picture_layouts_greyed_with_reason_until_media_is_chosen(self):
        owner = fixtures.CountdownWebTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        fixture = owner.fixture
        self.addCleanup(fixture.app.extensions['rss_host'].close)
        Image.new('RGB', (8, 8), '#00ff00').save(fixture.media_dir / 'notice-art.png')
        source = Path(__file__).with_name('editor_usability_browser.cjs').read_text()
        with patch('rss_transport.SafeFeedClient.fetch', side_effect=ValueError('Offline UX fixture')):
            result = fixture.run_live_chromium_probe(source.replace('__SCENARIO__', 'announcement-media'), timeout=60)
        self.assertEqual(result['errors'], [])


if __name__ == '__main__':
    unittest.main()
