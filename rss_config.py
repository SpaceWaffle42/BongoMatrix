"""Validated RSS source and prepared-rendering settings."""
from dataclasses import dataclass
import json
import math
import re
from types import MappingProxyType
from font_catalog import available_font_names
from modules.scroll_timing import LEGACY_FRAME_DELAYS, MINIMUM_FRAME_DELAY, MAXIMUM_FRAME_DELAY

SPEED_DELAYS = MappingProxyType(dict(LEGACY_FRAME_DELAYS))

DEFAULTS = {
    # Owner 2026-10-13: each default verified live through SafeFeedClient + parse_feed before adding.
    'feeds': [
        'https://feeds.bbci.co.uk/news/rss.xml',
        'https://www.reddit.com/r/worldnews/.rss',
        'http://feeds.skynews.com/feeds/rss/world.xml',
        'http://www.espn.com/espn/rss/news',
        'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
        'https://www.theguardian.com/uk/rss',
        'https://www.allsides.com/rss/news',
        'https://news.google.com/rss/search?q=when:7d+site:reuters.com&ceid=US:en&hl=en-US&gl=US',
        'https://feedx.net/rss/ap.xml',
    ],
    'disabled_feeds': [],
    'refresh_seconds': 1800, 'count': 5, 'include': [], 'exclude': [],
    'headline_font': '6x9', 'summary_font': '5x8',
    'frame_delay': SPEED_DELAYS['normal'], 'headline_mode': 'once',
    'thumbnail_speed': 100.0,
    'layout': 'auto', 'headline_colour': '#eea62b',
    'summary_colour': '#ffffff', 'background_colour': '#000000',
}


def render_options(settings):
    return RenderOptions(**{key: settings[key] for key in (
        'headline_font', 'summary_font', 'frame_delay', 'headline_mode', 'thumbnail_speed',
        'layout', 'headline_colour', 'summary_colour', 'background_colour')})


def timing_request_limit(revision):
    """Bound the JSON envelope using the supported RSS schema, not provider defaults.

    checked_url permits 2048 Unicode characters, twice for 20 sources plus their
    disabled subset. filter_words permits two lists of 50 x 64 characters.
    An escaped non-BMP character takes 12 bytes; quotes/comma/space take four.
    Fonts come from the finite installed catalogue. Reserve 24 bytes per float,
    two digits for count, and the longest headline mode. Revision is authoritative.
    Default JSON spacing is covered; arbitrary whitespace is intentionally not.
    """
    envelope = len(json.dumps({'base_revision': revision, 'settings': DEFAULTS,
                              'settings_correlation': 'f' * 64}).encode('utf-8'))
    fonts = 2 * max(len(json.dumps(name)) for name in available_font_names())
    return envelope + 2 * 20 * (2048 * 12 + 4) + 2 * 50 * (64 * 12 + 4) + fonts + 2 * 24 + 2 + len('continuous')


def validate(settings):
    from rss_transport import checked_url
    from rss_data import filter_words
    if type(settings) is not dict or set(settings) != set(DEFAULTS):
        raise ValueError('RSS settings require the supported fields only')
    feeds = settings['feeds']
    if type(feeds) is not list or len(feeds) > 20:
        raise ValueError('RSS requires at most 20 feed URLs')
    for url in feeds:
        checked_url(url)
    if len(set(feeds)) != len(feeds):
        raise ValueError('RSS feed URLs must be unique')
    disabled = settings['disabled_feeds']
    if (type(disabled) is not list or len(disabled) > len(feeds)
            or any(type(url) is not str or url not in feeds for url in disabled)
            or len(set(disabled)) != len(disabled)):
        raise ValueError('Disabled RSS feeds must be a unique subset of feed URLs')
    if type(settings['refresh_seconds']) is not int or settings['refresh_seconds'] != 1800:
        raise ValueError('RSS refresh must be 1800 seconds')
    if type(settings['count']) is not int or not 1 <= settings['count'] <= 50:
        raise ValueError('RSS story count must be 1..50')
    for key in ('include', 'exclude'):
        if type(settings[key]) is not list:
            raise ValueError('RSS filters must be word lists')
        filter_words(settings[key])
    render_options(settings)
    return settings


@dataclass(frozen=True)
class RenderOptions:
    headline_font: str = '6x9'
    summary_font: str = '5x8'
    frame_delay: float = SPEED_DELAYS['normal']
    headline_mode: str = 'once'
    thumbnail_speed: float = 100.0
    layout: str = 'auto'
    headline_colour: str = '#eea62b'
    summary_colour: str = '#ffffff'
    background_colour: str = '#000000'

    def __post_init__(self):
        if self.layout not in ('auto', 'text'):
            raise ValueError('RSS layout must be auto or text')
        for colour in (self.headline_colour, self.summary_colour, self.background_colour):
            if not isinstance(colour, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', colour):
                raise ValueError('RSS colours must be #RRGGBB')
        installed = available_font_names()
        if self.headline_font not in installed or self.summary_font not in installed:
            raise ValueError('Choose an installed BDF font')
        if self.headline_mode not in ('once', 'continuous'):
            raise ValueError('Headline mode must be once or continuous')
        for value, low, high in ((self.frame_delay, MINIMUM_FRAME_DELAY, MAXIMUM_FRAME_DELAY),
                                 (self.thumbnail_speed, 10, 400)):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Render timing outside supported range')
