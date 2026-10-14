"""Owner 2026-10-14: "the calendar should have more layout presets. the current
ones are very boring." Five new presets share the existing renderer contract:
same frame info keys, insets, title alignment/scroll, birthday confetti.
"""
import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from config_store import ConfigStore, DEFAULT_CONFIG

NEW = ('tearoff', 'flip', 'ring', 'hero', 'date')
INFO_KEYS = {'title_width', 'title_scrolls', 'title_x', 'title_height', 'title_available_width',
             'title_available_height', 'title_overflow', 'countdown_width', 'countdown_height',
             'countdown_available_width', 'countdown_available_height', 'countdown_font', 'countdown_overflow'}
ZONE = dt.timezone.utc


def event(**changes):
    result = dict(id='party', title='Party', start='2030-12-25', end=None, all_day=True, annual=False,
                  enabled=True, now_minutes=15, accent=None, icon='birthday')
    result.update(changes)
    return result


def selected(seconds, **changes):
    return dict(event=event(**changes), seconds=seconds,
                start=dt.datetime(2030, 12, 25, tzinfo=ZONE), end=dt.datetime(2030, 12, 26, tzinfo=ZONE))


def settings(layout, **changes):
    result = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
    result.update(layout=layout, **changes)
    return result


def colours(image):
    return {image.getpixel((x, y)) for x in range(64) for y in range(32)}


class CalendarLayoutPresetTests(unittest.TestCase):
    def test_every_new_preset_renders_every_countdown_range(self):
        from modules import event_countdown as module
        for layout in NEW:
            for seconds in (0, 42, 3 * 3600 + 5, 86400 + 60, 3 * 86400 + 4 * 3600 + 5 * 60, 400 * 86400):
                with self.subTest(layout=layout, seconds=seconds):
                    image, info = module.render_frame(settings(layout), selected(seconds), elapsed=.3)
                    self.assertEqual(image.size, (64, 32))
                    self.assertEqual(set(info) & INFO_KEYS, INFO_KEYS)
                    self.assertGreaterEqual(len(colours(image)), 3, 'preset should be more than two-tone')
                    self.assertFalse(info['countdown_overflow'], info)

    def test_presets_look_different_from_each_other_and_from_classic(self):
        from modules import event_countdown as module
        frames = {layout: module.render_frame(settings(layout), selected(3 * 86400 + 3600))[0].tobytes()
                  for layout in ('large', 'icon', 'progress', *NEW)}
        self.assertEqual(len(set(frames.values())), len(frames))

    def test_insets_are_respected(self):
        from modules import event_countdown as module
        inset = dict(top=2, right=3, bottom=2, left=4)
        for layout in NEW:
            with self.subTest(layout=layout):
                image, _ = module.render_frame(settings(layout, insets=inset), selected(3 * 86400))
                border = [(x, y) for x in range(64) for y in range(32)
                          if x < 4 or x >= 61 or y < 2 or y >= 30]
                self.assertTrue(all(image.getpixel(point) == (0, 0, 0) for point in border), layout)

    def test_flip_clock_ticks_and_shows_three_units(self):
        from modules import event_countdown as module
        a, info = module.render_frame(settings('flip'), selected(3 * 86400 + 4 * 3600 + 5 * 60))
        b, _ = module.render_frame(settings('flip'), selected(3 * 86400 + 4 * 3600 + 4 * 60))
        self.assertNotEqual(a.tobytes(), b.tobytes(), 'minutes tile should change')
        self.assertEqual(info['countdown_units'], ['DAYS', 'HRS', 'MIN'])
        _, info = module.render_frame(settings('flip'), selected(4 * 3600 + 5 * 60 + 6))
        self.assertEqual(info['countdown_units'], ['HRS', 'MIN', 'SEC'])

    def test_ring_fills_as_the_event_approaches(self):
        from modules import event_countdown as module
        _, far = module.render_frame(settings('ring'), selected(40 * 86400))
        _, near = module.render_frame(settings('ring'), selected(3 * 86400))
        _, now = module.render_frame(settings('ring'), selected(0))
        self.assertEqual(far['ring_fill'], 0)
        self.assertGreater(near['ring_fill'], .85)
        self.assertEqual(now['ring_fill'], 1)

    def test_date_card_shows_the_occurrence_date(self):
        from modules import event_countdown as module
        _, info = module.render_frame(settings('date'), selected(3 * 86400))
        self.assertEqual((info['date_day'], info['date_month'], info['date_weekday']), ('25', 'DEC', 'WED'))

    def test_hero_icon_is_double_size_and_twinkles(self):
        from modules import event_countdown as module
        a, info = module.render_frame(settings('hero'), selected(3 * 86400), elapsed=0)
        b, _ = module.render_frame(settings('hero'), selected(3 * 86400), elapsed=.5)
        self.assertEqual(info['icon_size'], 16)
        self.assertNotEqual(a.tobytes(), b.tobytes(), 'twinkle should animate')
        art = Image.new('RGB', (8, 8), (255, 0, 0))
        image, _ = module.render_frame(settings('hero'), selected(3 * 86400), artwork=art)
        reds = [(x, y) for x in range(20) for y in range(32) if image.getpixel((x, y)) == (255, 0, 0)]
        self.assertEqual(len(reds), 256)

    def test_narrow_title_columns_scroll_for_the_right_time(self):
        from modules import event_countdown as module
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['event_countdown'].update(layout='ring', events=[event(title='Wedding', start='2030-12-25')])
        start = dt.datetime(2030, 12, 1, tzinfo=ZONE)
        from unittest.mock import patch
        with patch.object(module, 'utc_now', return_value=start):
            frames = list(module.image_frames(config, .1))
        info = frames[0][2]
        self.assertLess(info['title_available_width'], 40)
        self.assertTrue(info['title_scrolls'])
        self.assertAlmostEqual(sum(frame[1] for frame in frames), (info['title_width'] + 4) * .05)

    def test_title_alignment_applies_in_new_presets(self):
        from modules import event_countdown as module
        lefts = {}
        for align in ('left', 'right'):
            image, info = module.render_frame(settings('flip', title_align=align), selected(3 * 86400, title='Hi'))
            xs = [x for x in range(64) for y in range(info['title_available_height']) if image.getpixel((x, y)) == (255, 255, 255)]
            lefts[align] = min(xs)
        self.assertLess(lefts['left'], lefts['right'])

    def test_birthday_confetti_on_new_presets(self):
        from modules import event_countdown as module
        a, _ = module.render_frame(settings('tearoff'), selected(0), elapsed=0)
        b, _ = module.render_frame(settings('tearoff'), selected(0), elapsed=.4)
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_config_accepts_new_presets_and_rejects_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            for layout in NEW:
                store.update({'event_countdown': {'layout': layout}})
                self.assertEqual(store.get()['event_countdown']['layout'], layout)
            with self.assertRaises(ValueError):
                store.update({'event_countdown': {'layout': 'boring'}})


if __name__ == '__main__':
    unittest.main()
