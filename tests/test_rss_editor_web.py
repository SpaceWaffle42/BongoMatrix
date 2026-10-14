"""RSS editor acceptance: real dashboard, Chromium and isolated public-feed transport."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_event_countdown_web as fixtures

class RSSEditorWebTests(unittest.TestCase):
    def setUp(self):
        owner = fixtures.CountdownWebTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        self.fixture = owner.fixture
        self.addCleanup(self.fixture.app.extensions['rss_host'].close)

    @unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
    def test_native_rss_editor(self):
        feed = b'<rss><channel><title>Fixture news &lt;img src=x onerror=alert(1)&gt;</title><item><title>Headline</title><description>Overview</description><enclosure url="https://example.com/unavailable.png" type="image/png" /></item></channel></rss>'
        def fetch(client, url):
            if url not in ('https://example.com/rss', 'https://feeds.bbci.co.uk/news/rss.xml'):
                raise ValueError('Fixture URL not allowed')
            return feed
        with patch('rss_transport.SafeFeedClient.fetch', fetch), patch('rss_thumbnails.ThumbnailPreparer.prepare', side_effect=ValueError('Unavailable fixture image')):
            result = self.fixture.run_live_chromium_probe(Path(__file__).with_name('rss_editor_browser.cjs').read_text(), timeout=60)
        self.assertEqual(result['errors'], [])
        from rss_data import parse_feed
        from rss_config import RenderOptions
        from modules.rss_renderer import prepare_story, image_frames
        story = parse_feed(feed, 'https://example.com/rss').stories[0]
        prepared = prepare_story(story, RenderOptions(frame_delay=.025, headline_mode='once',
            layout='text', headline_colour='#12ab34', summary_colour='#abcdef', background_colour='#010203'))
        image, _, _ = list(image_frames((prepared,)))[-1]
        self.assertEqual(result['finalPixels'], list(image.convert('RGBA').tobytes()))
        self.assertEqual(self.fixture.store.get()['rss']['count'], 3)

    def test_owner_defaults_and_no_redundant_animation_speed_control(self):
        from rss_config import DEFAULTS, RenderOptions
        self.assertEqual(DEFAULTS['headline_colour'], '#eea62b')
        self.assertEqual(RenderOptions().headline_colour, '#eea62b')
        # Owner 2026-10-12: default summary font 5x8 (backend, options and editor reset agree).
        self.assertEqual(DEFAULTS['summary_font'], '5x8')
        self.assertEqual(RenderOptions().summary_font, '5x8')
        js = (Path(__file__).parents[1] / 'app/templates/rss_editor.js').read_text()
        self.assertIn("summary_font:'5x8'", js)
        self.assertNotIn("summary_font:'4x6'", js)
        # Owner 2026-10-12: default headline font 6x9.
        self.assertEqual(DEFAULTS['headline_font'], '6x9')
        self.assertEqual(RenderOptions().headline_font, '6x9')
        self.assertIn("headline_font:'6x9'", js)
        self.assertNotIn("headline_font:'5x7'", js)
        # Site banners are still images: an animation speed control does nothing.
        self.assertNotIn("rssControl(appearanceFields,'thumbnail_speed'", js)
        self.assertNotIn('Thumbnail animation speed', js)
        # Two scrolling modes (owner): headline-first (default) or summary loops while headline scrolls.
        self.assertIn("rssControl(fields,'headline_mode','Scrolling'", js)
        self.assertNotIn("rssControl(fields,'thumbnail_speed'", js)
        self.assertIn('headline_mode', DEFAULTS)
        self.assertIn("headline_colour:'#eea62b'", js)
        self.assertNotIn("headline_colour:'#ffffff'", js)
        # Saved field stays accepted so existing configs load unchanged.
        self.assertIn('thumbnail_speed', DEFAULTS)

    @unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
    def test_stale_error_and_bounded_empty_preview(self):
        import time
        host = self.fixture.app.extensions['rss_host']
        urls = ['https://feeds.bbci.co.uk/news/rss.xml', 'https://example.com/error']
        config = self.fixture.store.update({'rss': {'feeds': urls}})
        host.sync(config)
        now = [time.time() - 3600]
        host.service.clock = lambda: now[0]
        feed = b'<rss><channel><title>Cached fixture</title><item><title>Headline</title></item></channel></rss>'
        def fetch(client, url):
            if url != urls[0]:
                raise ValueError('Unavailable fixture')
            return feed
        with patch('rss_transport.SafeFeedClient.fetch', fetch):
            host.service.refresh_due()
        now[0] += 1801
        with patch('rss_transport.SafeFeedClient.fetch', side_effect=ValueError('Unavailable fixture')):
            host.service.refresh_due()
            self.assertEqual([s.status for s in host.service.snapshots()], ['stale', 'error'])
            result = self.fixture.run_live_chromium_probe(Path(__file__).with_name('rss_status_browser.cjs').read_text(), timeout=40)
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.fixture.store.get()['rss']['include'], [])
