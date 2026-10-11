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
        prepared = prepare_story(story, RenderOptions(frame_delay=.025, headline_mode='continuous',
            layout='text', headline_colour='#12ab34', summary_colour='#abcdef', background_colour='#010203'))
        image, _, _ = list(image_frames((prepared,)))[-1]
        self.assertEqual(result['finalPixels'], list(image.convert('RGBA').tobytes()))
        self.assertEqual(self.fixture.store.get()['rss']['count'], 3)

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
