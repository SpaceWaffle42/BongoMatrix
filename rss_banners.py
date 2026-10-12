"""Site banners for RSS stories: the publisher's colour with its name over it.

Pure computation (no network, no files beyond the bundled BDF font). Replaces
per-story image downloads: every story from a site shows the same small
rectangle (15x7) at the start of the headline row; the summary keeps full width.
Known publishers match on exact host or a dot-delimited suffix, never substring.
"""
from functools import lru_cache
from hashlib import sha256
import re
from urllib.parse import urlsplit

from modules.rss_renderer import PreparedBanner, _font

WIDTH, HEIGHT = 15, 7  # Fits 3 tom_thumb letters (4px advance) with a 1px margin.
FONT = 'tom_thumb'
WHITE = (255, 255, 255)
# (host suffixes, banner text, background RGB). Colours from each brand's masthead.
KNOWN_SITES = (
    (('bbc.co.uk', 'bbci.co.uk', 'bbc.com'), 'BBC', (0xBB, 0x19, 0x19)),
    (('sky.com', 'skynews.com'), 'SKY', (0xC8, 0x10, 0x2E)),
    (('theguardian.com', 'guardian.co.uk'), 'G', (0x05, 0x29, 0x62)),
    (('reuters.com',), 'R', (0xFA, 0x64, 0x00)),
    (('npr.org',), 'NPR', (0xD6, 0x2E, 0x1F)),
    (('nytimes.com',), 'NYT', (0x12, 0x12, 0x12)),
    (('cnn.com',), 'CNN', (0xCC, 0x00, 0x00)),
    (('aljazeera.com',), 'AJ', (0xD2, 0x8A, 0x00)),
)
# Dark, saturated fallbacks so white text always reads on the panel.
FALLBACK_COLOURS = ((0x1E, 0x4B, 0x8C), (0x6A, 0x1B, 0x9A), (0x00, 0x69, 0x5C), (0x8E, 0x24, 0x24),
                    (0x2E, 0x5E, 0x1E), (0x8A, 0x4B, 0x08), (0x37, 0x47, 0x4F), (0xAD, 0x14, 0x57))


def _host(url):
    try:
        return (urlsplit(url).hostname or '').rstrip('.').lower()
    except ValueError:
        return ''


def _known(url):
    host = _host(url)
    for suffixes, text, colour in KNOWN_SITES:
        if any(host == s or host.endswith('.' + s) for s in suffixes):
            return text, colour
    return None


def banner_text(url, title):
    known = _known(url)
    if known:
        return known[0]
    words = re.findall(r'[A-Za-z0-9]+', title or '')
    if words:
        return ''.join(word[0] for word in words[:3]).upper()
    labels = [part for part in _host(url).split('.') if part not in ('www', 'feeds', 'rss', 'news')]
    return (labels[0] if labels else 'RSS')[:3].upper()


def _colour(url, text):
    known = _known(url)
    if known:
        return known[1]
    return FALLBACK_COLOURS[sha256(text.encode()).digest()[0] % len(FALLBACK_COLOURS)]


@lru_cache(maxsize=256)
def _tile(text, colour):
    font = _font(FONT)
    _, points = font.text_pixels(0, 0, text)
    dx = dy = 0
    if points:
        xs, ys = [x for x, _ in points], [y for _, y in points]
        dx = (WIDTH - (max(xs) - min(xs) + 1)) // 2 - min(xs)
        dy = (HEIGHT - (max(ys) - min(ys) + 1)) // 2 - min(ys)
    pixels = bytearray(bytes(colour) * WIDTH * HEIGHT)
    for x, y in points:
        x, y = x + dx, y + dy
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            pixels[(y * WIDTH + x) * 3:(y * WIDTH + x) * 3 + 3] = bytes(WHITE)
    return PreparedBanner(WIDTH, HEIGHT, bytes(pixels))


def site_banner(url, title=''):
    """15x7 still banner for a feed URL; known publishers get their own branding."""
    text = banner_text(url, title)
    return _tile(text, _colour(url, text))
