"""Opt-in RSS rendering contract; not registered in saved config or the UI."""
from dataclasses import dataclass
import math
from types import MappingProxyType
from font_catalog import available_font_names
from modules.scroll_timing import LEGACY_FRAME_DELAYS, MINIMUM_FRAME_DELAY, MAXIMUM_FRAME_DELAY

SPEED_DELAYS = MappingProxyType(dict(LEGACY_FRAME_DELAYS))

DEFAULTS = {
    'feeds': ['https://feeds.bbci.co.uk/news/rss.xml'],
    'refresh_seconds': 1800, 'count': 5, 'include': [], 'exclude': [],
    'headline_font': '5x7', 'summary_font': '4x6',
    'frame_delay': SPEED_DELAYS['normal'], 'headline_mode': 'once',
    'thumbnail_speed': 100.0,
}


def render_options(settings):
    return RenderOptions(**{key: settings[key] for key in (
        'headline_font', 'summary_font', 'frame_delay', 'headline_mode', 'thumbnail_speed')})


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
    headline_font: str = '5x7'
    summary_font: str = '4x6'
    frame_delay: float = SPEED_DELAYS['normal']
    headline_mode: str = 'once'
    thumbnail_speed: float = 100.0

    def __post_init__(self):
        installed = available_font_names()
        if self.headline_font not in installed or self.summary_font not in installed:
            raise ValueError('Choose an installed BDF font')
        if self.headline_mode not in ('once', 'continuous'):
            raise ValueError('Headline mode must be once or continuous')
        for value, low, high in ((self.frame_delay, MINIMUM_FRAME_DELAY, MAXIMUM_FRAME_DELAY),
                                 (self.thumbnail_speed, 10, 400)):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Render timing outside supported range')
