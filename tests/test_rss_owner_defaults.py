"""Owner 2026-10-14 feedback on 5002.

- "the Default presets are wrong! they need to be in the drop down not already
  added!" -> a fresh config has only BBC Top Stories; the eight verified feeds
  are offered in the Default presets dropdown.
- "the default RSS should be summary loops while headline scrolls. the summary
  must loop atleast once if the headline finishes before summary" -> default
  headline_mode is 'continuous'; a short headline never ends the story before
  one full summary cycle.
"""
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIED = (
    'https://www.reddit.com/r/worldnews/.rss',
    'http://feeds.skynews.com/feeds/rss/world.xml',
    'http://www.espn.com/espn/rss/news',
    'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'https://www.theguardian.com/uk/rss',
    'https://www.allsides.com/rss/news',
    'https://news.google.com/rss/search?q=when:7d+site:reuters.com&ceid=US:en&hl=en-US&gl=US',
    'https://feedx.net/rss/ap.xml',
)


class RssOwnerDefaultsTests(unittest.TestCase):
    def test_fresh_config_has_only_bbc_and_presets_offer_the_verified_feeds(self):
        from config_store import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigStore(Path(directory) / 'config.json').get()
        self.assertEqual(config['rss']['feeds'], ['https://feeds.bbci.co.uk/news/rss.xml'])
        presets = json.loads((ROOT / 'rss_presets.json').read_text())
        urls = [feed['url'] for feed in presets['feeds']]
        for url in VERIFIED:
            self.assertIn(url, urls)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(feed.get('group') for feed in presets['feeds']))

    def test_saved_feed_lists_are_untouched(self):
        from config_store import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            store.update({'rss': {'feeds': ['https://feeds.bbci.co.uk/news/rss.xml', *VERIFIED]}})
            self.assertEqual(ConfigStore(store.path).get()['rss']['feeds'][1:], list(VERIFIED))

    def test_default_mode_is_summary_loops_while_headline_scrolls(self):
        from config_store import ConfigStore
        from rss_config import DEFAULTS, RenderOptions
        self.assertEqual(DEFAULTS['headline_mode'], 'continuous')
        self.assertEqual(RenderOptions().headline_mode, 'continuous')
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            self.assertEqual(store.get()['rss']['headline_mode'], 'continuous')
            store.update({'rss': {'headline_mode': 'once'}})
            self.assertEqual(ConfigStore(store.path).get()['rss']['headline_mode'], 'once')

    def test_summary_completes_at_least_one_loop_when_headline_finishes_first(self):
        from modules import rss_renderer as r
        from rss_config import RenderOptions
        from rss_data import Story
        # Long-enough headline that scrolls, summary much longer than the headline loop.
        story = r.prepare_story(Story(('a',), 'Short headline that scrolls', 'word ' * 120, '', None), RenderOptions())
        loop = story.headline.width + r.HEADLINE_GAP
        cycle = (len(story.lines) - 1) * story.line_pitch + story.lines[-1].height + r.SUMMARY_GAP
        self.assertLess(loop, cycle)
        frames = list(r.image_frames((story,)))
        self.assertGreaterEqual(len(frames), cycle + 1)
        # Back at the summary's start after the full loop.
        self.assertEqual(frames[cycle][2]['summary_y'], 0)
        self.assertTrue(frames[-1][2]['summary_done'])
        self.assertFalse(any(info['summary_done'] for _, _, info in frames[:cycle]))

    def test_editor_dropdown_groups_and_draft_defaults(self):
        js = (ROOT / 'app/templates/rss_editor.js').read_text()
        self.assertIn("feed.group", js)
        self.assertIn("headline_mode:'continuous'", js)
        self.assertNotIn("headline_mode:'once'", js)


if __name__ == '__main__':
    unittest.main()
