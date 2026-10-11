import importlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rss_data import parse_feed


class CacheTests(unittest.TestCase):
    def test_atomic_bounded_lru_normalized_cache_and_corruption(self):
        self.assertIsNotNone(importlib.util.find_spec('rss_cache'), 'normalized disk cache absent')
        api = importlib.import_module('rss_cache')
        feed = parse_feed(b'<rss><channel><title>News</title><item><title>Hello</title></item></channel></rss>', 'https://example.com/')
        with tempfile.TemporaryDirectory() as directory:
            cache = api.DiskFeedCache(directory, max_bytes=2000, max_entries=2)
            self.assertTrue(cache.put('a', feed, 100.))
            self.assertTrue(cache.put('b', feed, 101.))
            self.assertEqual(cache.get('a'), (feed, 100.))
            self.assertTrue(cache.put('c', feed, 102.))
            self.assertIsNone(cache.get('b'))
            self.assertEqual(cache.get('a'), (feed, 100.))
            with patch.object(api.os, 'replace', side_effect=OSError('disk fail')):
                with self.assertRaises(OSError):
                    cache.put('a', feed, 200.)
            self.assertEqual(cache.get('a'), (feed, 100.))
            self.assertFalse(list(Path(directory).glob('*.tmp')))
            files = list(Path(directory).glob('*.json'))
            self.assertLessEqual(sum(p.stat().st_size for p in files), 2000)
            self.assertEqual(api.DiskFeedCache(directory, max_bytes=2000, max_entries=2).get('a'), (feed, 100.))
            for path in files:
                path.write_text('{"feed": "malformed"}')
            self.assertIsNone(cache.get('a'))
            with self.assertRaises(ValueError):
                api.DiskFeedCache(directory, max_bytes=50_000_001)