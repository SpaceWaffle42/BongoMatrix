"""Grouped editor UX on the real dashboard with native Chromium input."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import available_timezones
from tests import test_event_countdown_web as fixtures


@unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
class EditorUsabilityTests(unittest.TestCase):
    def setUp(self):
        owner = fixtures.CountdownWebTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        self.fixture = owner.fixture
        self.addCleanup(self.fixture.app.extensions['rss_host'].close)

    def probe(self, scenario):
        source = Path(__file__).with_name('editor_usability_browser.cjs').read_text()
        with patch('rss_transport.SafeFeedClient.fetch', side_effect=ValueError('Offline UX fixture')):
            result = self.fixture.run_live_chromium_probe(source.replace('__SCENARIO__', scenario), timeout=60)
        self.assertEqual(result['errors'], [])
        return result

    def test_grouped_sections_and_conditional_location(self):
        self.probe('groups')

    def test_authoritative_timezone_select_save_reload(self):
        self.fixture.store.update({'astronomy': {'timezone': 'Etc/UTC'},
                                   'event_countdown': {'timezone': 'Etc/UTC'}})
        # A valid saved alias/custom server zone need not be enumerated by the catalogue.
        with patch('webapp.available_timezones', return_value=available_timezones() - {'Etc/UTC'}):
            result = self.probe('zones')
        self.assertEqual(set(result['zones']), available_timezones() | {'UTC'})

    def test_editors_open_simple_with_remembered_advanced_toggle(self):
        self.probe('simple')

    def test_opening_one_editor_closes_the_others(self):
        self.probe('accordion')

    def test_contextual_help_native_input_and_reflow(self):
        self.probe('help')
