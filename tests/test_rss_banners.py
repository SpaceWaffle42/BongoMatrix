import unittest
from unittest.mock import patch

from modules.rss_renderer import PreparedBanner

W, H = 15, 7


def pixels(banner):
    data = banner.pixels
    return [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]


class SiteBannerTests(unittest.TestCase):
    def test_bbc_is_red_rectangle_with_white_bbc_text(self):
        from rss_banners import site_banner
        for url in ('https://feeds.bbci.co.uk/news/rss.xml', 'https://www.bbc.co.uk/sport/rss.xml', 'https://bbc.com/x'):
            banner = site_banner(url, 'BBC News')
            self.assertIsInstance(banner, PreparedBanner)
            self.assertEqual((banner.width, banner.height), (W, H))
            self.assertGreater(banner.width, banner.height, 'banner must be a wide rectangle')
            colours = set(pixels(banner))
            self.assertEqual(colours, {(0xBB, 0x19, 0x19), (0xFF, 0xFF, 0xFF)})
        from rss_banners import banner_text
        self.assertEqual(banner_text('https://feeds.bbci.co.uk/news/rss.xml', 'anything'), 'BBC')

    def test_text_is_centred_and_inside_banner(self):
        from rss_banners import site_banner
        grid = pixels(site_banner('https://feeds.bbci.co.uk/news/rss.xml', ''))
        white = [(i % W, i // W) for i, c in enumerate(grid) if c == (255, 255, 255)]
        xs, ys = [x for x, _ in white], [y for _, y in white]
        self.assertLessEqual(abs(min(xs) - (W - 1 - max(xs))), 1)
        self.assertLessEqual(abs(min(ys) - (H - 1 - max(ys))), 1)

    def test_banner_sits_on_headline_row_and_summary_keeps_full_width(self):
        from rss_banners import site_banner
        from rss_data import Story
        from modules.rss_renderer import prepare_story, render_frame
        story = Story(('k',), 'Headline text', 'A summary line', '', None)
        p = prepare_story(story, banner=site_banner('https://feeds.bbci.co.uk/news/rss.xml', ''))
        self.assertEqual(p.warnings['headline_available_height'], 9)  # 6x9 default row
        self.assertEqual(p.warnings['summary_available_height'], 22)
        image, _ = render_frame(p, 0)
        self.assertEqual(image.getpixel((0, 0)), (0xBB, 0x19, 0x19))
        self.assertEqual(image.getpixel((W - 1, H - 1)), (0xBB, 0x19, 0x19))
        # Nothing of the banner below its 7px tile: rows 7-8 of the banner columns are background,
        # and the summary area (row 10+) matches the text-only layout.
        self.assertIsNone(image.crop((0, H, W, 9)).getbbox())
        self.assertEqual(image.crop((0, 10, 64, 32)).tobytes(), render_frame(prepare_story(story), 0)[0].crop((0, 10, 64, 32)).tobytes())
        # Summary starts at column 0 (full width), as in the text-only layout.
        text_only = render_frame(prepare_story(story), 24)[0]
        banner_frame = render_frame(p, 24)[0]
        self.assertEqual(banner_frame.crop((0, 10, 64, 32)).tobytes(), text_only.crop((0, 10, 64, 32)).tobytes())

    def test_lookalike_hosts_do_not_get_bbc_branding(self):
        from rss_banners import banner_text
        self.assertNotEqual(banner_text('https://notbbc.co.uk/rss', 'Not BBC'), 'BBC')
        self.assertNotEqual(banner_text('https://bbc.co.uk.evil.example/rss', 'Fake'), 'BBC')

    def test_unknown_site_uses_title_initials_and_stable_colour(self):
        from rss_banners import banner_text, site_banner
        self.assertEqual(banner_text('https://example.com/feed', 'Hacker News Daily'), 'HND')
        self.assertEqual(banner_text('https://example.com/feed', ''), 'EXA')
        self.assertEqual(banner_text('https://example.com/feed', '  '), 'EXA')
        first = site_banner('https://example.com/feed', 'Hacker News')
        self.assertEqual(first, site_banner('https://example.com/other', 'Hacker News'))
        self.assertEqual(len(set(pixels(first))), 2)

    def test_service_uses_site_banner_and_never_downloads_story_images(self):
        from rss_service import PreparedFeedService
        class Client:
            def fetch(self, url):
                return (b'<rss><channel><title>BBC News</title><item><guid>one</guid><title>one</title>'
                        b'<enclosure type="image/png" url="https://example.com/a.png"/></item></channel></rss>')
        service = PreparedFeedService(('https://feeds.bbci.co.uk/news/rss.xml',), client=Client(), count=1)
        with patch('rss_thumbnails.ThumbnailPreparer.prepare', side_effect=AssertionError('story image download')):
            service.refresh_due()
        from rss_banners import site_banner
        story = service.peek().stories[0]
        self.assertEqual(story.banner, site_banner('https://feeds.bbci.co.uk/news/rss.xml', 'BBC News'))
        self.assertIsNone(story.thumbnail)
        self.assertEqual(story.warnings['headline_available_height'], 9)  # 6x9 default row
        service.close()

    def test_text_layout_still_has_no_banner(self):
        from rss_config import RenderOptions
        from rss_service import PreparedFeedService
        class Client:
            def fetch(self, url):
                return b'<rss><channel><title>T</title><item><guid>one</guid><title>one</title></item></channel></rss>'
        service = PreparedFeedService(('https://example.com/feed',), client=Client(), options=RenderOptions(layout='text'))
        service.refresh_due()
        self.assertIsNone(service.peek().stories[0].banner)
        service.close()


if __name__ == '__main__':
    unittest.main()
