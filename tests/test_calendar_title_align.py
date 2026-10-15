"""Owner 2026-10-14: "calendar titles should be center aligned by default. but add
alignment to the advanced options." Short titles sit centre/left/right; titles
wider than the panel still scroll once from the left edge, then rest at x=0.
"""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from config_store import ConfigStore, DEFAULT_CONFIG


def event(**changes):
    result = dict(id='launch', title='Hi', start='2030-01-01T12:00', end=None, all_day=False,
                  annual=False, enabled=True, now_seconds=900, accent=None, icon='calendar')
    result.update(changes)
    return result


def title_columns(image, title_height):
    xs = [x for x in range(64) for y in range(title_height) if image.getpixel((x, y)) != (0, 0, 0)]
    return min(xs), max(xs)


class CalendarTitleAlignTests(unittest.TestCase):
    def setUp(self):
        # These check layout geometry on the full panel; the 2px frame border
        # (FRAME_MARGIN, owner 2026-10-15) is covered in test_calendar_frame_margin.
        from modules import event_countdown as module
        margin = patch.object(module, 'FRAME_MARGIN', 0)
        margin.start()
        self.addCleanup(margin.stop)

    def render(self, **changes):
        from modules import event_countdown as module
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings.update(changes)
        return module.render_frame(settings, dict(event=event(), seconds=3600 * 30))

    def test_default_is_centre(self):
        self.assertEqual(DEFAULT_CONFIG['event_countdown']['title_align'], 'center')
        image, info = self.render()
        left, right = title_columns(image, info['title_available_height'])
        self.assertLessEqual(abs(left - (63 - right)), 1, (left, right))
        self.assertGreater(left, 10)

    def test_left_and_right_are_selectable(self):
        image, info = self.render(title_align='left')
        self.assertEqual(title_columns(image, info['title_available_height'])[0], 0)
        image, info = self.render(title_align='right', insets=dict(top=0, right=3, bottom=0, left=0))
        right = title_columns(image, info['title_available_height'])[1]
        self.assertGreaterEqual(right, 58)
        self.assertLessEqual(right, 60)

    def test_long_titles_still_scroll_from_the_left_whatever_the_alignment(self):
        from modules import event_countdown as module
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        selected = dict(event=event(title='A long event title that must scroll'), seconds=3600)
        _, start = module.render_frame(settings, selected, elapsed=0)
        _, moving = module.render_frame(settings, selected, elapsed=.1)
        self.assertEqual((start['title_x'], start['title_scrolls']), (0, True))
        self.assertLess(moving['title_x'], 0)

    def test_saved_alignment_validates_and_older_saves_read_as_centre(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            store = ConfigStore(path)
            store.update({'event_countdown': {'title_align': 'right'}})
            self.assertEqual(ConfigStore(path).get()['event_countdown']['title_align'], 'right')
            with self.assertRaises(ValueError):
                store.update({'event_countdown': {'title_align': 'middle'}})
            raw = json.loads(path.read_text())
            raw.setdefault('event_countdown', {}).pop('title_align', None)
            raw['event_countdown']['layout'] = 'icon'  # an older save with other calendar settings
            path.write_text(json.dumps(raw))
            settings = ConfigStore(path).get()['event_countdown']
            self.assertEqual((settings['title_align'], settings['layout']), ('center', 'icon'))


if __name__ == '__main__':
    unittest.main()
