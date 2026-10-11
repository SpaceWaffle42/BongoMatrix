"""Bounded offline-only settings; weather coordinates never imply a timezone."""
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from font_catalog import available_font_names

DEFAULTS = {
    'current_moon': True, 'lunar_events': False,
    'location_source': 'weather', 'latitude': 0.0, 'longitude': 0.0,
    'location_label': 'Astronomy override', 'timezone': '',
    'horizon_days': 90, 'event_limit': 5, 'moon_hold_seconds': 10,
    'event_hold_seconds': 8, 'frame_delay': .05,
    'title_font': '5x7', 'detail_font': '4x6', 'countdown_font': 'auto',
    'background': '#000000', 'text_colour': '#FFFFFF',
    'moon_colour': '#D9D9D9', 'accent': '#B388FF',
}


def validate(settings):
    def require(ok, message):
        if not ok:
            raise ValueError('astronomy: ' + message)
    require(isinstance(settings, dict) and set(settings) == set(DEFAULTS), 'invalid settings fields')
    require(all(type(settings[k]) is bool for k in ('current_moon', 'lunar_events')), 'invalid content flags')
    require(settings['location_source'] in ('weather', 'override'), 'invalid location source')
    for key, bound in (('latitude', 90), ('longitude', 180)):
        value = settings[key]
        require(type(value) in (int, float) and math.isfinite(value) and abs(value) <= bound, 'invalid coordinates')
    label = settings['location_label']
    require(isinstance(label, str) and bool(label.strip()) and label.isprintable() and len(label) <= 200, 'invalid place label')
    zone = settings['timezone']
    require(isinstance(zone, str) and len(zone) <= 100, 'invalid IANA timezone')
    if zone:
        try:
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError('astronomy: invalid IANA timezone') from exc
    for key, low, high in (('horizon_days', 1, 366), ('event_limit', 1, 50)):
        require(type(settings[key]) is int and low <= settings[key] <= high, 'invalid ' + key)
    for key, low, high in (('moon_hold_seconds', 1, 300), ('event_hold_seconds', 1, 300), ('frame_delay', .01, 1)):
        value = settings[key]
        require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high, 'invalid ' + key)
    fonts = available_font_names()
    for key in ('title_font', 'detail_font', 'countdown_font'):
        require(settings[key] in (('auto', *fonts) if key == 'countdown_font' else fonts), 'installed font required')
    for key in ('background', 'text_colour', 'moon_colour', 'accent'):
        require(isinstance(settings[key], str) and re.fullmatch(r'#[0-9a-fA-F]{6}', settings[key]), 'invalid colour')


def location_values(config):
    """Return immutable arguments, or no location until timezone is explicit."""
    settings = config['astronomy']
    validate(settings)
    if not settings['timezone']:
        return None
    if settings['location_source'] == 'weather':
        weather = config['weather']
        return (weather['latitude'], weather['longitude'], settings['timezone'],
                weather['location'], 'Weather coordinates; explicit astronomy timezone')
    return (settings['latitude'], settings['longitude'], settings['timezone'],
            settings['location_label'], 'Explicit astronomy coordinate override')
