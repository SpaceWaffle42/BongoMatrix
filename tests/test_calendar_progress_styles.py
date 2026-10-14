"""Owner 2026-10-14: "for the calender progress bar add a variety of animations
for the bar that the user can choose from. ensure solid is one option."

progress_style picks how the bar fill is drawn. 'solid' is the default and is
pixel-identical to the existing bar; every style fills exactly the same columns
(the fill length still tells the time) and animated styles move over time.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from config_store import ConfigStore, DEFAULT_CONFIG

STYLES = ('solid', 'gradient', 'shimmer', 'pulse', 'stripes', 'segments', 'dots', 'rainbow')
ANIMATED = ('shimmer', 'pulse', 'stripes', 'rainbow')


def event(**changes):
    result = dict(id='launch', title='Hi', start='2030-01-01T12:00', end=None, all_day=False,
                  annual=False, enabled=True, now_minutes=15, accent=None, icon='calendar')
    result.update(changes)
    return result


def bar_rows(image, height=2):
    return [[image.getpixel((x, y)) for x in range(64)] for y in range(32 - height, 32)]


def lit_columns(image, height=2):
    return {x for row in bar_rows(image, height) for x, pixel in enumerate(row) if pixel != (0, 0, 0)}


class CalendarProgressStyleTests(unittest.TestCase):
    def render(self, seconds=10 * 86400, elapsed=0.0, **changes):
        from modules import event_countdown as module
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings.update(layout='progress', **changes)
        return module.render_frame(settings, dict(event=event(), seconds=seconds), elapsed=elapsed)[0]

    def test_solid_is_default_and_matches_the_original_bar(self):
        self.assertEqual(DEFAULT_CONFIG['event_countdown']['progress_style'], 'solid')
        image = self.render()
        accent = tuple(bytes.fromhex(DEFAULT_CONFIG['event_countdown']['accent'][1:]))
        fill = -(-64 * 10 // 30)  # ceil(64 * 10 days / 30 days)
        for row in bar_rows(image):
            self.assertEqual(row[:fill], [accent] * fill)
            self.assertEqual(row[fill:], [(0, 0, 0)] * (64 - fill))

    def test_every_style_renders_inside_the_same_fill(self):
        solid = lit_columns(self.render())
        frames = {}
        for style in STYLES:
            with self.subTest(style=style):
                image = self.render(progress_style=style, elapsed=.35)
                lit = lit_columns(image)
                self.assertTrue(lit, 'bar must show something')
                self.assertTrue(lit <= solid, f'{style} draws outside the fill')
                self.assertGreaterEqual(max(lit), max(solid) - 3, f'{style} hides the fill end')
                frames[style] = image.tobytes()
        self.assertEqual(len(set(frames.values())), len(STYLES), 'styles must look different')

    def test_animated_styles_move_and_static_ones_do_not(self):
        for style in STYLES:
            with self.subTest(style=style):
                a = bar_rows(self.render(progress_style=style, elapsed=0))
                b = bar_rows(self.render(progress_style=style, elapsed=.4))
                if style in ANIMATED:
                    self.assertNotEqual(a, b)
                else:
                    self.assertEqual(a, b)

    def test_empty_bar_at_now_for_every_style(self):
        for style in STYLES:
            with self.subTest(style=style):
                self.assertEqual(lit_columns(self.render(seconds=0, progress_style=style, elapsed=.2)), set())

    def test_styles_respect_insets_and_thickness(self):
        from modules import event_countdown as module
        for style in STYLES:
            settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
            settings.update(layout='progress', progress_style=style, progress_height=4,
                            insets=dict(top=1, right=3, bottom=2, left=4))
            image, _ = module.render_frame(settings, dict(event=event(), seconds=29 * 86400), elapsed=.3)
            outside = [(x, y) for x in range(64) for y in range(32) if x < 4 or x >= 61 or y >= 30]
            self.assertTrue(all(image.getpixel(p) == (0, 0, 0) for p in outside), style)
            rows = {y for y in range(26, 30) if any(image.getpixel((x, y)) != (0, 0, 0) for x in range(4, 61))}
            self.assertTrue(rows, style)

    def test_saved_style_validates_and_older_saves_read_as_solid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            store = ConfigStore(path)
            store.update({'event_countdown': {'progress_style': 'rainbow'}})
            self.assertEqual(ConfigStore(path).get()['event_countdown']['progress_style'], 'rainbow')
            with self.assertRaises(ValueError):
                store.update({'event_countdown': {'progress_style': 'sparkly'}})
            raw = json.loads(path.read_text())
            raw['event_countdown'].pop('progress_style', None)
            raw['event_countdown']['layout'] = 'progress'
            path.write_text(json.dumps(raw))
            settings = ConfigStore(path).get()['event_countdown']
            self.assertEqual((settings['progress_style'], settings['layout']), ('solid', 'progress'))

    def test_editor_offers_the_styles_in_simple_mode_for_the_bar_style(self):
        js = (Path(__file__).resolve().parents[1] / 'app/templates/event_countdown_editor.js').read_text()
        self.assertIn("'progress_style','Bar animation'", js)
        for style in STYLES:
            self.assertIn(f"'{style}'", js)


if __name__ == '__main__':
    unittest.main()
