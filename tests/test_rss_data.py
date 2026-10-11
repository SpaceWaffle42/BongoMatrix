import importlib
import importlib.util
import unittest


class ParseTests(unittest.TestCase):
    def test_safe_rss_atom_normalization_and_limits(self):
        self.assertIsNotNone(importlib.util.find_spec('rss_data'), 'normalized feed boundary absent')
        api = importlib.import_module('rss_data')
        rss = b'''<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><title>News</title>
        <item><guid> stable </guid><title><![CDATA[<b>World</b> &amp; news]]></title>
        <link>https://EXAMPLE.com:443/story#top</link><description><![CDATA[Hello <script>evil()</script><img src="x">world]]></description>
        <pubDate>Sun, 11 Oct 2026 12:00:00 GMT</pubDate><category>Science</category>
        <media:thumbnail url="https://example.com/photo.png"/></item></channel></rss>'''
        feed = api.parse_feed(rss, 'https://example.com/feed')
        self.assertEqual(feed.title, 'News')
        story = feed.stories[0]
        self.assertEqual(story.title, 'World & news')
        self.assertEqual(story.summary, 'Hello world')
        self.assertEqual(story.link, 'https://example.com/story')
        self.assertEqual(story.categories, ('Science',))
        self.assertEqual(story.media, ('https://example.com/photo.png',))
        self.assertIn('id:stable', story.keys)
        self.assertIsNotNone(story.published)
        atom = api.parse_feed(b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
          <entry><id>tag:example,2026:1</id><title>Atom story</title><updated>2026-10-11T12:00:00Z</updated>
          <link href="/article"/><category term="Space"/><summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Hi <b>there</b></div></summary></entry></feed>''', 'https://example.com/feed')
        self.assertEqual(atom.stories[0].summary, 'Hi there')
        self.assertEqual(atom.stories[0].link, 'https://example.com/article')
        self.assertEqual(atom.stories[0].categories, ('Space',))
        for bad in (b'bad', b'<html/>', b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel><title>&x;</title></channel></rss>', b'x' * (api.MAX_FEED_BYTES + 1)):
            with self.assertRaises(ValueError):
                api.parse_feed(bad, 'https://example.com/feed')
        many = '<rss><channel>' + ('<item><title>' + 'x' * 1000 + '</title></item>') * 205 + '</channel></rss>'
        limited = api.parse_feed(many.encode(), 'https://example.com/feed')
        self.assertEqual(len(limited.stories), 200)
        self.assertEqual(len(limited.stories[0].title), 300)
        self.assertTrue(limited.warnings)

    def test_filter_dedup_fairness_and_rotation_reset(self):
        import rss_data as api
        self.assertTrue(hasattr(api, 'Rotation'), 'selection boundary absent')
        def story(key, title, date, **kw):
            return api.Story((key,), title, '', '', date, **kw)
        a = story('a', 'Cat news', 1)
        b = story('b', 'Catapult', 2, categories=('CAT',))
        c = story('c', 'Cat forbidden', 3)
        d = story('d', 'Catapult', 4)
        e = story('e', 'cat newest', 5)
        feeds = ((a, c, d, e), (a, b))
        rotation = api.Rotation()
        first = rotation.select(feeds, count=2, include=('cat',), exclude=('forbidden',), material=('feeds',))
        self.assertEqual(first, (a, b))
        self.assertEqual(rotation.select(feeds, count=2, include=('cat',), exclude=('forbidden',), material=('feeds',)), (e,))
        self.assertEqual(rotation.select(feeds, count=2, include=('cat',), exclude=('forbidden',), material=('feeds',)), (e, b))
        # Case/order-only filter edits are not material changes; actual changes reset.
        self.assertEqual(rotation.select(feeds, count=1, include=('CAT',), exclude=('forbidden',), material=('feeds',)), (e,))
        self.assertEqual(rotation.select(feeds, count=1, include=('news',), material=('feeds',)), (a,))
        self.assertEqual(api.Rotation().select(feeds, count=1), (a,))
        # Aliases dedup across canonical link even if IDs differ.
        alias = api.Story(('other-id', 'a'), 'alias', '', '', 10)
        self.assertEqual(api.Rotation().select(((a,), (alias, b)), count=5), (a, b))

    def test_xml_structure_budget_and_absent_media_url(self):
        from rss_data import parse_feed
        deep = b'<rss><channel><item><description>' + b'<div>' * 100 + b'text' + b'</div>' * 100 + b'</description></item></channel></rss>'
        with self.assertRaisesRegex(ValueError, 'structure'):
            parse_feed(deep, 'https://example.com/feed')
        feed = parse_feed(b'<rss xmlns:m="http://search.yahoo.com/mrss/"><channel><item><title>Headline</title><m:thumbnail/></item></channel></rss>', 'https://example.com/feed')
        self.assertEqual(feed.stories[0].media, ())