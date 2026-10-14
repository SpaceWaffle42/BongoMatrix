"""Bounded offline-only settings.

A blank astronomy timezone follows the Weather location's timezone; choosing
one here overrides it.
"""
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
    'title_colour': '#FFFFFF', 'date_colour': '#FFFFFF', 'description_colour': '#FFFFFF',
    'moon_colour': '#D9D9D9', 'accent': '#B388FF',
    'view': 'simple',
}
VIEWS = ('simple', 'advanced')
# Additive fields reconstructed on read for saves made before they existed.
PROJECTED_FIELDS = ('view',)
# Per-role text colours. A save without them keeps its look: each inherits
# that save's text_colour (see project_role_colours).
ROLE_COLOURS = ('title_colour', 'date_colour', 'description_colour')


def project_role_colours(settings):
    """Fill missing role colours from text_colour in place (old saves keep their look)."""
    if isinstance(settings, dict):
        inherited = settings.get('text_colour', DEFAULTS['text_colour'])
        for key in ROLE_COLOURS:
            settings.setdefault(key, inherited)
    return settings


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
    for key in ('background', 'text_colour', 'moon_colour', 'accent', *ROLE_COLOURS):
        require(isinstance(settings[key], str) and re.fullmatch(r'#[0-9a-fA-F]{6}', settings[key]), 'invalid colour')
    require(settings['view'] in VIEWS, 'invalid view')


def location_values(config):
    """Return immutable arguments, or None when no timezone is known.

    Timezone: the astronomy setting if chosen, else the Weather location's.
    """
    settings = config['astronomy']
    validate(settings)
    weather = config['weather']
    zone, zone_source = settings['timezone'], 'explicit astronomy timezone'
    if not zone:
        zone, zone_source = weather.get('timezone') or '', 'Weather timezone'
    if not zone:
        return None
    if settings['location_source'] == 'weather':
        return (weather['latitude'], weather['longitude'], zone,
                weather['location'], 'Weather coordinates; ' + zone_source)
    return (settings['latitude'], settings['longitude'], zone,
            settings['location_label'], 'Explicit astronomy coordinate override; ' + zone_source)
