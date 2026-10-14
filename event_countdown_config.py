"""Strict local countdown schema; no credentials or provider state in config."""
import copy
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from font_catalog import available_font_names
from media_resolver import MediaResolver, MediaResolutionError
from holiday_calendar import PACKS, HOLIDAYS, YEARS, HOLIDAY_ICONS

DEFAULTS = {
    'timezone': 'UTC', 'events': [], 'layout': 'large',
    'background': '#000000', 'text_colour': '#FFFFFF', 'accent': '#7170FF',
    'title_font': '5x7', 'countdown_font': 'auto', 'progress_height': 2,
    # Owner 2026-10-14: titles centred by default; left/right in Advanced.
    'title_align': 'center',
    'insets': dict(top=0, right=0, bottom=0, left=0),
    # Holiday packs (all off by default), per-year date edits and hidden holidays.
    'packs': {pack: False for pack in PACKS}, 'holiday_dates': {}, 'hidden_holidays': [],
}
# Additive fields: missing on older saves, refilled from DEFAULTS on read.
PROJECTED_FIELDS = ('packs', 'holiday_dates', 'hidden_holidays', 'title_align')
TITLE_ALIGNS = ('center', 'left', 'right')
# Classic three plus the decorative presets in modules/calendar_layouts.py.
LAYOUTS = ('large', 'icon', 'progress', 'tearoff', 'flip', 'ring', 'hero', 'date')
ICONS = ('calendar', 'birthday', 'anniversary', 'launch', 'holiday', 'opening', 'scissors', 'clock', 'book', 'plane', 'ball', 'tooth', *HOLIDAY_ICONS)
MAX_HOLIDAY_EDITS = 400


def project(settings):
    """Fill additive pack fields (older saves, or new packs added later)."""
    if isinstance(settings, dict):
        for key in PROJECTED_FIELDS:
            settings.setdefault(key, copy.deepcopy(DEFAULTS[key]))
        if isinstance(settings['packs'], dict):
            for pack in PACKS:
                settings['packs'].setdefault(pack, False)
    return settings


def validate(settings):
    def require(condition, message):
        if not condition:
            raise ValueError('event_countdown: ' + message)
    def colour(value):
        return isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value)
    def integer(value, lo, hi):
        return type(value) is int and lo <= value <= hi
    require(isinstance(settings, dict) and set(settings) == set(DEFAULTS), 'invalid settings fields')
    try:
        zone = ZoneInfo(settings['timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError('event_countdown: invalid IANA timezone') from exc
    require(settings['layout'] in LAYOUTS, 'invalid layout')
    require(settings['title_align'] in TITLE_ALIGNS, 'title alignment must be center, left or right')
    for key in ('background', 'text_colour', 'accent'):
        require(colour(settings[key]), 'invalid colour')
    fonts = available_font_names()
    require(settings['title_font'] in fonts, 'invalid title font')
    require(settings['countdown_font'] in ('auto', *fonts), 'invalid countdown font')
    require(integer(settings['progress_height'], 1, 4), 'progress height must be 1..4')
    insets = settings['insets']
    require(isinstance(insets, dict) and set(insets) == {'top', 'right', 'bottom', 'left'}, 'invalid insets')
    require(all(integer(value, 0, 8) for value in insets.values()), 'insets must be 0..8')
    packs = settings['packs']
    require(isinstance(packs, dict) and set(packs) == set(PACKS), 'invalid holiday packs')
    require(all(type(value) is bool for value in packs.values()), 'holiday pack switches must be booleans')
    edits = settings['holiday_dates']
    require(isinstance(edits, dict) and set(edits) <= set(HOLIDAYS), 'unknown holiday in edited dates')
    count = 0
    for years in edits.values():
        require(isinstance(years, dict) and years, 'invalid edited holiday dates')
        for year, value in years.items():
            count += 1
            require(isinstance(year, str) and re.fullmatch(r'\d{4}', year) and int(year) in YEARS, 'edited year must be 1950..2150')
            require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), 'edited date must be YYYY-MM-DD')
            try:
                parsed = dt.date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError('event_countdown: invalid edited date') from exc
            # Within a day or two of its year (Hebrew/Islamic dates can straddle New Year).
            require(abs((parsed - dt.date(int(year), 7, 2)).days) <= 200, 'edited date must fall in its year')
    require(count <= MAX_HOLIDAY_EDITS, 'too many edited holiday dates')
    hidden = settings['hidden_holidays']
    require(isinstance(hidden, list) and all(isinstance(key, str) and key in HOLIDAYS for key in hidden)
            and len(set(hidden)) == len(hidden), 'invalid hidden holidays')
    require(isinstance(settings['events'], list), 'events must be a list')
    ids = set()
    for item in settings['events']:
        require(isinstance(item, dict) and set(item) - {'artwork'} == {'id', 'title', 'start', 'end', 'all_day', 'annual', 'enabled', 'now_minutes', 'accent', 'icon'}, 'invalid event fields')
        if item.get('artwork') is not None:
            try:
                parts = MediaResolver._normalize(item['artwork'])
                require('/'.join(parts) == item['artwork'], 'invalid artwork reference')
            except MediaResolutionError as exc:
                raise ValueError('event_countdown: invalid artwork reference') from exc
        require(isinstance(item['id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item['id']), 'invalid event id')
        require(item['id'] not in ids, 'duplicate event id')
        ids.add(item['id'])
        require(isinstance(item['title'], str) and item['title'].strip() and item['title'].isprintable(), 'title must be printable and nonempty')
        require(all(type(item[key]) is bool for key in ('enabled', 'annual', 'all_day')), 'event flags must be booleans')
        require(integer(item['now_minutes'], 1, 10080), 'NOW minutes must be 1..10080')
        require(item['accent'] is None or colour(item['accent']), 'invalid event accent')
        require(item['icon'] in ICONS, 'invalid icon')
        times = []
        for key in ('start', 'end'):
            value = item[key]
            if key == 'end' and value is None:
                continue
            require(isinstance(value, str), 'invalid date/time')
            pattern = r'\d{4}-\d{2}-\d{2}' if item['all_day'] else r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?'
            require(re.fullmatch(pattern, value), 'invalid date/time format')
            try:
                parsed = dt.datetime.fromisoformat(value)
                require(1 <= parsed.year <= 9997, 'year out of range')
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=zone)
                    require(parsed.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) == parsed.replace(tzinfo=None), 'nonexistent local time (DST gap)')
                times.append(parsed.astimezone(dt.timezone.utc))
            except (ValueError, OverflowError) as exc:
                raise ValueError('event_countdown: invalid date/time: ' + str(exc)) from exc
        require(len(times) == 1 or times[1] > times[0], 'end must be after start (exclusive)')
