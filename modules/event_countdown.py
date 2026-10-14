"""Local event countdown: a shared pixel renderer for preview and panel."""
import datetime as dt
import calendar
import math
import sys
import time
from functools import lru_cache
from bisect import bisect_left, bisect_right
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw
from edit_preview import _png_frame, CaptureMatrix
from frame_capture import BdfFont
from font_catalog import FONT_DIR, font_key
from holiday_calendar import pack_events
from modules import calendar_layouts, progress_styles


UTC = dt.timezone.utc
ICONS = {
    'calendar': (0x24, 0xff, 0x81, 0xff, 0x81, 0xa9, 0x81, 0xff),
    'birthday': (0x24, 0x24, 0x7e, 0x42, 0x7e, 0x42, 0x7e, 0xff),
    'anniversary': (0x66, 0xff, 0xff, 0x7e, 0x3c, 0x18, 0, 0),
    'launch': (0x18, 0x3c, 0x3c, 0x7e, 0x5a, 0x18, 0x24, 0x42),
    'holiday': (0x18, 0x3c, 0x18, 0x7e, 0x3c, 0xff, 0x18, 0x18),
    'opening': (0x7e, 0x42, 0x5e, 0x52, 0x56, 0x52, 0x7e, 0xff),
}
# Coloured 8x8 holiday art: one character per pixel, '.' is transparent.
_ART_COLOURS = {
    'g': (40, 170, 60), 'G': (20, 110, 40), 'y': (255, 215, 0), 'o': (255, 120, 20), 'O': (200, 80, 0),
    'b': (130, 80, 40), 'r': (230, 40, 50), 'p': (255, 105, 180), 'w': (255, 255, 255), 'c': (90, 200, 255),
    'B': (60, 110, 255), 'v': (170, 90, 255), 'k': (60, 60, 60), 'l': (180, 255, 120), 's': (200, 200, 210),
}
ART = {
    'birthday': ('..y.y.y.', '..w.w.w.', '.pppppp.', '.pwpwpp.', '.pppppp.', 'rrrrrrrr', 'wwwwwwww', 'rrrrrrrr'),
    'tree': ('...yy...', '...gg...', '..gggg..', '..grgg..', '.gggygg.', '.ggggrg.', 'gggggggg', '...bb...'),
    'pumpkin': ('....G...', '...G....', '.oooooo.', 'oOooooOo', 'oyoooyoo', 'oooooooo', 'oyyyyyoo', '.oooooo.'),
    'egg': ('...pp...', '..pppp..', '.cccccc.', '.yyyyyy.', 'pppppppp', 'cccccccc', '.yyyyyy.', '..pppp..'),
    'cross': ('...yy...', '...yy...', 'yyyyyyyy', 'yyyyyyyy', '...yy...', '...yy...', '...yy...', '...yy...'),
    'dove': ('........', '.ww.....', 'wwww..w.', '.wwwwww.', '..wwwwwo', '...www..', '..w.....', '........'),
    'star': ('...y....', '...y....', '..yyy...', 'yyyyyyy.', '.yyyyy..', '..yyy...', '.yy.yy..', 'yy...yy.'),
    'heart': ('.rr..rr.', 'rrrrrrrr', 'rrrrrrrr', 'rrrrrrrr', '.rrrrrr.', '..rrrr..', '...rr...', '........'),
    'firework': ('y..r..y.', '.y.r.y..', '..yry...', 'rrrwrrr.', '..yry...', '.y.r.y..', 'y..r..y.', '........'),
    'crescent': ('..yyy...', '.yy.....', 'yy......', 'yy....y.', 'yy...yyy', 'yy....y.', '.yy.....', '..yyy...'),
    'lantern': ('...yy...', '..rrrr..', '.rryyrr.', '.rryyrr.', '.rryyrr.', '..rrrr..', '...yy...', '...y....'),
    'star_of_david': ('...B....', '..B.B...', 'BBBBBBB.', '.B...B..', '.B...B..', 'BBBBBBB.', '..B.B...', '...B....'),
    'menorah': ('y.y.y.y.', 'y.y.y.y.', 'c.c.c.c.', 'cccccccc', '...cc...', '...cc...', '..cccc..', '.cccccc.'),
    'shofar': ('........', 'b.......', 'bb......', '.bb.....', '..bbb...', '...bbbb.', '....bbbb', '......bb'),
    'diya': ('...y....', '..yoy...', '...o....', '........', 'O......O', 'OOOOOOOO', '.OOOOOO.', '..OOOO..'),
    'holi': ('p...B...', '..y...g.', '.pp..BB.', 'ppp.BBB.', '.p.y.B..', '..yyy...', 'g.yyy.v.', 'gg...vv.'),
    'lotus': ('...p....', '..ppp...', '.p.p.p..', 'pp.p.pp.', 'pppppppp', '.pppppp.', 'gggggggg', '.gggggg.'),
    'khanda': ('...B....', '..BBB...', '.B.B.B..', 'B..B..B.', 'B..B..B.', '.B.B.B..', '..BBB...', '.BBBBB..'),
    'leaf': ('......gg', '....gggg', '...ggggg', '..gggglg', '.gggglgg', '.ggglgg.', '.glgg...', 'l.......'),
    # Everyday presets: haircut, appointment, school, trip.
    'scissors': ('ss....ss', '.ss..ss.', '..ssss..', '...ss...', '..r..r..', '.r.rr.r.', '.r.rr.r.', '..r..r..'),
    'clock': ('..wwww..', '.w..w.w.', 'w...w..w', 'w...www.', 'w......w', 'w......w', '.w....w.', '..wwww..'),
    'book': ('........', 'BBB.BBB.', 'BwB.BwBB', 'BwBBBwBB', 'BwBBBwBB', 'BBBBBBBB', '.bbbbbb.', '........'),
    'plane': ('...c....', '...cc...', 'c..cc...', 'cccccccc', 'c..cc...', '...cc...', '...c....', '........'),
    'ball': ('..wwww..', '.wwkkww.', 'wwkkkkww', 'wkwwwwkw', 'wkwwwwkw', 'wwkkkkww', '.wwkkww.', '..wwww..'),
    'tooth': ('.ww..ww.', 'wwwwwwww', 'wwwwwwww', 'wwwwwwww', '.wwwwww.', '.ww..ww.', '.ww..ww.', '.w....w.'),
}
def icon_catalogue():
    """Icon pixels for the dashboard calendar: 8 rows of 8 '#rrggbb' or None."""
    result = {name: [[None if key == '.' else '#%02x%02x%02x' % _ART_COLOURS[key] for key in row] for row in rows]
              for name, rows in ART.items()}
    for name, rows in ICONS.items():
        result.setdefault(name, [['#ffffff' if row & (0x80 >> x) else None for x in range(8)] for row in rows])
    return result


CONFETTI = ((255, 60, 60), (255, 210, 0), (60, 200, 255), (120, 255, 120), (255, 105, 180), (170, 90, 255))


def local_time(value, zone):
    value = dt.datetime.fromisoformat(value)
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def _year(value, year):
    return value.replace(year=year, day=min(value.day, calendar.monthrange(year, value.month)[1]))


def select_event(settings, *, now=None):
    """Select earliest still eligible occurrence; UTC arithmetic across DST.

    Naive local timestamps use the configured zone (first fold). All-day end
    dates are exclusive. Annual occurrences keep their local wall clock.
    """
    zone = ZoneInfo(settings['timezone'])
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    now = now.astimezone(UTC)
    local_year = now.astimezone(zone).year
    best = None
    # Holiday packs add all-day events for this and next year (dates calculated offline).
    events = [*settings['events'], *pack_events(settings, (local_year - 1, local_year, local_year + 1))]
    for order, item in enumerate(events):
        if not item['enabled']:
            continue
        original = local_time(item['start'], zone)
        original_end = local_time(item['end'], zone) if item['end'] else None
        years = range(max(original.year, local_year - 1), min(9999, max(original.year, local_year) + 2)) if item['annual'] else [original.year]
        for year in years:
            try:
                start = _year(original, year)
                if original_end is not None:
                    end = original_end
                    if item['annual'] and year != original.year:
                        # Anchor local-calendar duration (including leap spans),
                        # then restore the endpoint fold erased by addition.
                        duration = original_end - original
                        end = (start + duration).replace(fold=original_end.fold)
                        if (not item['all_day'] and duration <= dt.timedelta(0)
                                and end.astimezone(UTC) <= start.astimezone(UTC)):
                            # A cross-fold interval can have zero/negative wall
                            # duration. Without that fold in the projected year,
                            # retain its positive elapsed span instead.
                            elapsed = original_end.astimezone(UTC) - original.astimezone(UTC)
                            end = (start.astimezone(UTC) + elapsed).astimezone(zone)
                elif item['all_day']:
                    end = start + dt.timedelta(days=1)
                else:
                    end = start.astimezone(UTC) + dt.timedelta(minutes=item['now_minutes'])
                start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
            except (ValueError, OverflowError):
                # A valid stored span can exceed datetime's range on recurrence.
                # Yield instead of breaking rendering of other events.
                continue
            if end_utc <= start_utc or now >= end_utc:
                continue
            key = (start_utc, order)
            if best is None or key < best[0]:
                best = (key, {'event': item, 'start': start, 'end': end,
                              'seconds': max(0, math.ceil((start_utc - now).total_seconds()))})
            break
    return best[1] if best else None


def utc_now():
    return dt.datetime.now(UTC)


@lru_cache(maxsize=1)
def fonts():
    return {font_key(path): BdfFont(path) for path in sorted(FONT_DIR.glob('*.bdf'))}


def compact_countdown(seconds):
    if seconds <= 0:
        return 'NOW'
    if seconds >= 86400:
        return f'{seconds // 86400}d'
    if seconds >= 3600:
        return f'{seconds // 3600}h {seconds % 3600 // 60}m'
    if seconds >= 60:
        return f'{seconds // 60}m {seconds % 60}s'
    return f'{seconds}s'


@lru_cache(maxsize=4096)
def _glyph_bounds(glyph):
    points = [(glyph.x_offset + x, -glyph.y_offset - glyph.height + y)
              for y, row in enumerate(glyph.rows) for x in range(glyph.width)
              if row & (1 << (glyph.bits_per_row - 1 - x))]
    if not points:
        return None
    return (min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points) + 1, max(y for _, y in points) + 1)


@lru_cache(maxsize=32)
def _layout(font, text):
    # Cache advances, not a title-sized pixel cloud. Only visible glyphs rasterize.
    positions = []
    cursor = left = right = 0
    top, bottom = None, None
    min_bearing = max_extent = 0
    for character in text:
        positions.append(cursor)
        glyph = font.glyph(character)
        if glyph is None:
            continue
        bounds = _glyph_bounds(glyph)
        if bounds:
            x0, y0, x1, y1 = bounds
            left, right = min(left, cursor + x0), max(right, cursor + x1)
            top = y0 if top is None else min(top, y0)
            bottom = y1 if bottom is None else max(bottom, y1)
            min_bearing, max_extent = min(min_bearing, x0), max(max_extent, x1)
        cursor += glyph.advance
    return (max(cursor, right) - left, (bottom - top) if top is not None else 0,
            top or 0, left, tuple(positions), min_bearing, max_extent)


def _measure(name, text):
    return _layout(fonts()[name], text)[:3]


def _text(image, name, text, x, y, colour):
    font = fonts()[name]
    _, _, top, left, positions, min_bearing, max_extent = _layout(font, text)
    x -= left
    start = bisect_right(positions, -x - max_extent)
    end = bisect_left(positions, image.width - x - min_bearing)
    colour = tuple(bytes.fromhex(colour[1:]))
    for index in range(start, end):
        glyph = font.glyph(text[index])
        if glyph is None:
            continue
        for row_index, row in enumerate(glyph.rows):
            py = y - top - glyph.y_offset - glyph.height + row_index
            if not 0 <= py < image.height:
                continue
            for column in range(glyph.width):
                px = x + positions[index] + glyph.x_offset + column
                if 0 <= px < image.width and row & (1 << (glyph.bits_per_row - 1 - column)):
                    image.putpixel((px, py), colour)


def _art(draw, name, left, top, height):
    for y, row in enumerate(ART[name]):
        for x, key in enumerate(row):
            if key != '.' and top + y < height:
                draw.point((left + x, top + y), fill=_ART_COLOURS[key])


def _confetti(area, elapsed):
    """Falling multicoloured confetti over the card (birthday NOW only); deterministic."""
    width, height = area.size
    for index in range(28):
        seed = (index * 2654435761) & 0xffffffff
        speed = 10 + seed % 14
        x = (seed >> 8) % width + int(2 * math.sin(elapsed * 3 + index))
        y = int(((seed >> 16) % (height + 8)) + elapsed * speed) % (height + 8) - 4
        if 0 <= x < width and 0 <= y < height:
            area.putpixel((x, y), CONFETTI[index % len(CONFETTI)])
            if index % 3 == 0 and y + 1 < height:
                area.putpixel((x, y + 1), CONFETTI[index % len(CONFETTI)])


def render_frame(settings, selected, *, elapsed=0, initial_seconds=None, artwork=None):
    """Render BDF pixels once; both outputs use this exact clipped image."""
    inset = settings['insets']
    width, height = 64 - inset['left'] - inset['right'], 32 - inset['top'] - inset['bottom']
    image = Image.new('RGB', (64, 32), settings['background'])
    area = Image.new('RGB', (width, height), settings['background'])
    item, seconds = selected['event'], selected['seconds']
    accent = item['accent'] or settings['accent']
    if settings['layout'] in calendar_layouts.PRESETS:
        info = calendar_layouts.render(sys.modules[__name__], area, settings, selected, elapsed, accent, artwork)
        if seconds <= 0 and item.get('icon') == 'birthday':
            _confetti(area, elapsed)
        image.paste(area, (inset['left'], inset['top']))
        return image, info
    title_font = settings['title_font']
    title_width, title_height, _ = _measure(title_font, item['title'])
    measured_title_height = title_height
    title_height = min(title_height, max(1, height // 2 - 1))
    title_x = -int(elapsed / .05) if title_width > width and elapsed < (title_width + 4) * .05 else 0
    # Titles that fit use the chosen alignment; wider titles scroll from the left and rest at 0.
    title_offset = 0
    if title_width <= width:
        align = settings.get('title_align', 'center')
        title_offset = (width - title_width) // 2 if align == 'center' else width - title_width if align == 'right' else 0
    title = Image.new('RGB', (width, title_height), settings['background'])
    _text(title, title_font, item['title'], title_x + title_offset, 0, settings['text_colour'])
    area.paste(title, (0, 0))
    bar_height = settings['progress_height'] if settings['layout'] == 'progress' else 0
    text_y = title_height + 2
    text_height = max(1, height - text_y - bar_height - (1 if bar_height else 0))
    icon_width = 10 if settings['layout'] == 'icon' else 0
    text_width = width - icon_width
    label = compact_countdown(seconds)
    font = settings['countdown_font']
    if font == 'auto':
        fitting = [name for name in fonts() if _measure(name, label)[0] <= text_width and _measure(name, label)[1] <= text_height]
        font = max(fitting, key=lambda name: (_measure(name, label)[1], _measure(name, label)[0], name)) if fitting else min(fonts(), key=lambda name: (_measure(name, label)[1], _measure(name, label)[0], name))
    measured_width, measured_height, _ = _measure(font, label)
    text = Image.new('RGB', (text_width, text_height), settings['background'])
    _text(text, font, label, max(0, (text_width - measured_width) // 2), max(0, (text_height - measured_height) // 2), settings['text_colour'])
    area.paste(text, (icon_width, text_y))
    draw = ImageDraw.Draw(area)
    if icon_width and artwork is not None:
        area.paste(artwork, (0, text_y))
    elif icon_width and item.get('icon') in ART:
        _art(draw, item['icon'], 0, text_y, height)  # coloured pixel art (cake, tree, pumpkin, ...)
    elif icon_width:
        for y, row in enumerate(ICONS.get(item.get('icon'), ICONS['calendar'])):
            for x in range(8):
                if row & (0x80 >> x) and text_y + y < height:
                    draw.point((x, text_y + y), fill=accent)
    if bar_height:
        # Fixed 30-day window: stable across playlist invocations and restarts.
        # More distant events remain full; NOW is empty.
        horizon = 30 * 86400
        fill = min(width, max(0, math.ceil(width * seconds / horizon)))
        progress_styles.draw(draw, settings.get('progress_style', 'solid'), 0, height - bar_height,
                             fill, bar_height, accent, settings['background'], elapsed)
    if seconds <= 0 and item.get('icon') == 'birthday':
        _confetti(area, elapsed)
    image.paste(area, (inset['left'], inset['top']))
    return image, dict(title_width=title_width, title_scrolls=title_width > width, title_x=title_x,
                      title_height=measured_title_height, title_available_width=width,
                      title_available_height=title_height, title_overflow=measured_title_height > title_height,
                      countdown_width=measured_width, countdown_height=measured_height,
                      countdown_available_width=text_width, countdown_available_height=text_height,
                      countdown_font=font, countdown_overflow=measured_width > text_width or measured_height > text_height)


def image_frames(config, duration=15, stop_event=None, *, realtime=False, pinned_handles=None):
    settings = config['event_countdown']
    origin = utc_now()
    started = time.monotonic()
    elapsed, event_started, identity, initial = 0.0, 0.0, None, None
    deadline, pass_seconds, restored = duration, 0.0, True
    artwork = None
    while not (stop_event and stop_event.is_set()):
        if realtime:
            elapsed = time.monotonic() - started
        if elapsed >= deadline and restored:
            return
        selected = select_event(settings, now=utc_now() if realtime else origin + dt.timedelta(seconds=elapsed))
        if selected is None:
            return
        key = (selected['event']['id'], selected['start'])
        if key != identity:
            artwork = None
            reference = selected['event'].get('artwork')
            if settings['layout'] in ('icon', 'ring', 'hero') and reference:
                handle = (pinned_handles or {}).get(reference)
                if handle is None:
                    raise ValueError('countdown artwork capability is missing')
                # Decode just the first frame through the shared bounded decoder.
                # Never resolve a filename here: admission owns the pinned handle.
                decoded = CaptureMatrix().media_frames(
                    reference, 1, 1, pinned_handle=handle, stop_event=stop_event,
                    preserve_size=True, decode_timeout=5,
                )
                if stop_event and stop_event.is_set():
                    return
                if decoded:
                    artwork = decoded[0].copy()
                    artwork.thumbnail((8, 8), Image.Resampling.LANCZOS)
            identity, event_started, initial = key, elapsed, selected['seconds']
            title_width = _measure(settings['title_font'], selected['event']['title'])[0]
            available = calendar_layouts.title_column_width(
                settings['layout'], 64 - settings['insets']['left'] - settings['insets']['right'],
                32 - settings['insets']['top'] - settings['insets']['bottom'])
            pass_seconds = (title_width + 4) * .05 if title_width > available else 0.0
            deadline = max(duration, event_started + pass_seconds)
            restored = not pass_seconds
        event_elapsed = elapsed - event_started
        image, info = render_frame(settings, selected, elapsed=event_elapsed, initial_seconds=initial, artwork=artwork)
        restored = event_elapsed >= pass_seconds
        delay = max(0.0, min(.05, deadline - elapsed))
        yield image, delay, info
        if elapsed >= deadline:
            return
        elapsed += delay


def preview_frames(config, duration=15, stop_event=None, *, pinned_handles=None):
    for image, delay, info in image_frames(config, duration, stop_event, pinned_handles=pinned_handles):
        frame = _png_frame(image, delay * 1000)
        frame['countdown'] = info
        yield frame


def run(matrix, config, duration=15, stop_event=None, *, pinned_handles=None):
    rendered = False
    for image, delay, _ in image_frames(config, duration, stop_event, realtime=True, pinned_handles=pinned_handles):
        if stop_event and stop_event.is_set():
            break
        matrix.draw_image(image)
        matrix.swap()
        rendered = True
        if stop_event:
            if stop_event.wait(delay):
                break
        else:
            time.sleep(delay)
    return rendered
