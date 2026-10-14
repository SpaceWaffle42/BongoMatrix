"""Owner 2026-10-14: "the now timer should be seconds not minutes".

Each timed event's NOW window ("Show NOW for") is entered and stored in
seconds (now_seconds, 1..604800). Saved events from earlier builds keep their
meaning: now_minutes is read as minutes x 60.
"""
import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import event_countdown_config
from config_store import ConfigStore, DEFAULT_CONFIG
from modules import event_countdown


def event(**changes):
    result = dict(id='launch', title='Launch', start='2030-01-01T12:00', end=None,
                  all_day=False, annual=False, enabled=True, now_seconds=30,
                  accent=None, icon='calendar')
    result.update(changes)
    return result


UTC = dt.timezone.utc


class NowSecondsTests(unittest.TestCase):
    def settings(self, item):
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings.update(timezone='UTC', events=[item])
        return settings

    def test_now_window_is_measured_in_seconds(self):
        start = dt.datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
        settings = self.settings(event(now_seconds=30))
        shown = event_countdown.select_event(settings, now=start + dt.timedelta(seconds=29))
        self.assertIsNotNone(shown)
        self.assertEqual(event_countdown.compact_countdown(shown['seconds']), 'NOW')
        self.assertIsNone(event_countdown.select_event(settings, now=start + dt.timedelta(seconds=30)))

    def test_saved_minutes_are_read_as_seconds(self):
        legacy = event()
        legacy.pop('now_seconds')
        legacy['now_minutes'] = 15
        settings = self.settings(legacy)
        event_countdown_config.project(settings)
        self.assertEqual(settings['events'][0]['now_seconds'], 900)
        self.assertNotIn('now_minutes', settings['events'][0])

    def test_store_saves_seconds_and_upgrades_old_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            store.update({'event_countdown': {'events': [event(now_seconds=45)]}})
            self.assertEqual(store.get()['event_countdown']['events'][0]['now_seconds'], 45)
            # An editor page still open from the previous build sends minutes.
            old = event()
            old.pop('now_seconds')
            old['now_minutes'] = 2
            store.update({'event_countdown': {'events': [old]}})
            saved = store.get()['event_countdown']['events'][0]
            self.assertEqual(saved['now_seconds'], 120)
            self.assertNotIn('now_minutes', saved)
            self.assertNotIn('now_minutes', store.path.read_text())

    def test_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            before = store.path.read_bytes()
            for bad in (0, 604801, True, 1.5, '30'):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store.update({'event_countdown': {'events': [event(now_seconds=bad)]}})
                self.assertEqual(store.path.read_bytes(), before)
            both = event(now_minutes=1)
            with self.assertRaises(ValueError):
                store.update({'event_countdown': {'events': [both]}})
            store.update({'event_countdown': {'events': [event(now_seconds=604800)]}})

    def test_editor_asks_for_seconds(self):
        source = (Path(__file__).parents[1] / 'app/templates/event_countdown_editor.js').read_text()
        self.assertIn("'now_seconds','Show “NOW” for (seconds)'", source)
        self.assertIn('now_seconds:900', source)
        self.assertNotIn('now_minutes', source)


if __name__ == '__main__':
    unittest.main()
