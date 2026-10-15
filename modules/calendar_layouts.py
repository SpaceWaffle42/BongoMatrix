"""Decorative calendar layout presets (owner 2026-10-14: "more layout presets").

Each preset draws into the inset area using the shared BDF helpers from
modules.event_countdown and returns the same frame-info contract as the classic
layouts (title/countdown geometry), plus a few preset-specific keys.
"""
import math

from PIL import Image, ImageDraw

PRESETS = ('tearoff', 'flip', 'ring', 'hero', 'date')
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
WEEKDAYS = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')
RING_HORIZON = 30 * 86400


def _rgb(value):
    return tuple(bytes.fromhex(value[1:])) if isinstance(value, str) else tuple(value)


def _hex(rgb):
    return '#%02x%02x%02x' % tuple(rgb)


def _mix(a, b, amount):
    a, b = _rgb(a), _rgb(b)
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def _readable_on(rgb):
    r, g, b = _rgb(rgb)
    return '#000000' if (r * 299 + g * 587 + b * 114) / 1000 > 140 else '#ffffff'


def units(seconds):
    """Three flip tiles: the largest meaningful units, zero-padded."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return [(str(min(days, 999)), 'DAYS'), (f'{hours:02d}', 'HRS'), (f'{minutes:02d}', 'MIN')]
    return [(f'{hours:02d}', 'HRS'), (f'{minutes:02d}', 'MIN'), (f'{secs:02d}', 'SEC')]


def big_number(seconds):
    """Number + unit word for the tear-off/hero/date cards."""
    if seconds <= 0:
        return 'NOW', ''
    if seconds >= 86400:
        days = seconds // 86400
        return str(days), 'DAY' if days == 1 else 'DAYS'
    if seconds >= 3600:
        return str(seconds // 3600), 'HRS'
    if seconds >= 60:
        return str(seconds // 60), 'MIN'
    return str(seconds), 'SEC'


def title_band(ec, area, settings, item, x, y, width, height, elapsed, colour=None):
    """Title in a column/band: alignment when it fits, continuous right-to-left scroll when not."""
    font = settings['title_font']
    title_width, measured, _ = ec._measure(font, item['title'])
    available_height = max(1, min(measured, height))
    title_x = ec.scroll_x(title_width, width, elapsed)
    offset = 0
    if title_width <= width:
        align = settings.get('title_align', 'center')
        offset = (width - title_width) // 2 if align == 'center' else width - title_width if align == 'right' else 0
    band = Image.new('RGB', (max(1, width), available_height), area.getpixel((x, y)) if 0 <= x < area.width and 0 <= y < area.height else settings['background'])
    ec.draw_scrolling(band, font, item['title'], title_x + offset, 0, colour or settings['text_colour'], title_width, width)
    area.paste(band, (x, y))
    return dict(title_width=title_width, title_scrolls=title_width > width, title_x=title_x,
                title_height=measured, title_available_width=width,
                title_available_height=available_height, title_overflow=measured > available_height)


def fit_font(ec, text, width, height, prefer=None):
    """Largest installed font that fits (or the preferred one when it fits)."""
    if prefer and prefer != 'auto':
        w, h, _ = ec._measure(prefer, text)
        return prefer, w, h
    # Upright faces only: oblique (*O) fonts look broken next to the decorative frames.
    upright = [name for name in ec.fonts() if not name.endswith('O')]
    fitting = [name for name in upright if ec._measure(name, text)[0] <= width and ec._measure(name, text)[1] <= height]
    if fitting:
        name = max(fitting, key=lambda n: (ec._measure(n, text)[1], ec._measure(n, text)[0], n))
    else:
        name = min(upright, key=lambda n: (ec._measure(n, text)[1], ec._measure(n, text)[0], n))
    w, h, _ = ec._measure(name, text)
    return name, w, h


def centred_text(ec, area, font, text, x, y, width, height, colour):
    """Draw text centred in a box, keeping whatever is already painted behind it."""
    w, h, _ = ec._measure(font, text)
    mask = Image.new('RGB', (max(1, width), max(1, height)), (0, 0, 0))
    ec._text(mask, font, text, max(0, (width - w) // 2), max(0, (height - h) // 2), '#ffffff')
    ink = Image.new('RGB', mask.size, _rgb(colour))
    area.paste(ink, (x, y), mask.convert('L'))
    return w, h


def icon(ec, area, item, accent, x, y, scale=1, artwork=None):
    """8x8 icon (coloured art, mono glyph or library artwork) at 1x or 2x."""
    tile = Image.new('RGBA', (8, 8), (0, 0, 0, 0))
    if artwork is not None:
        tile.paste(artwork.convert('RGBA'), (0, 0))
    elif item.get('icon') in ec.ART:
        for row_index, row in enumerate(ec.ART[item['icon']]):
            for column, key in enumerate(row):
                if key != '.':
                    tile.putpixel((column, row_index), (*ec._ART_COLOURS[key], 255))
    else:
        for row_index, row in enumerate(ec.ICONS.get(item.get('icon'), ec.ICONS['calendar'])):
            for column in range(8):
                if row & (0x80 >> column):
                    tile.putpixel((column, row_index), (*_rgb(accent), 255))
    if scale != 1:
        tile = tile.resize((8 * scale, 8 * scale), Image.Resampling.NEAREST)
    area.paste(tile, (x, y), tile)


def countdown_info(font, width, height, available_width, available_height):
    return dict(countdown_width=width, countdown_height=height, countdown_available_width=available_width,
                countdown_available_height=available_height, countdown_font=font,
                countdown_overflow=width > available_width or height > available_height)


def _tearoff(ec, area, settings, selected, elapsed, accent, artwork):
    """A tear-off desk calendar page: coloured header with title, big number, unit."""
    width, height = area.size
    item, seconds = selected['event'], selected['seconds']
    draw = ImageDraw.Draw(area)
    paper = _mix(settings['background'], '#ffffff', .9)
    ink = _readable_on(paper)
    draw.rectangle((0, 0, width - 1, height - 1), fill=paper)
    header = min(9, max(6, height // 3))
    draw.rectangle((0, 0, width - 1, header - 1), fill=_rgb(accent))
    for x in range(3, width - 2, 8):  # binder rings
        draw.point((x, 0), fill=_mix(paper, '#000000', .5))
        draw.point((x + 1, 0), fill=_mix(paper, '#000000', .5))
    info = title_band(ec, area, settings, item, 1, max(1, (header - min(header - 1, ec._measure(settings['title_font'], item['title'])[1])) // 2),
                      width - 2, header - 1, elapsed, colour=_readable_on(accent))
    number, unit = big_number(seconds)
    unit_font = 'tom_thumb' if 'tom_thumb' in ec.fonts() else settings['title_font']
    unit_height = ec._measure(unit_font, unit)[1] + 1 if unit else 0
    body_y, body_height = header + 1, max(1, height - header - 2 - unit_height)
    font, w, h = fit_font(ec, number, width - 4, body_height, settings['countdown_font'])
    centred_text(ec, area, font, number, 2, body_y, width - 4, body_height, ink)
    if unit:
        centred_text(ec, area, unit_font, unit, 0, height - unit_height - 1, width, unit_height, _hex(_mix(paper, ink, .6)))
    # Turned-up page corner.
    for step in range(4):
        draw.line((width - 4 + step, height - 1, width - 1, height - 4 + step), fill=_mix(paper, '#000000', .25 + step * .05))
    info.update(countdown_info(font, w, h, width - 4, body_height))
    return info


def _flip(ec, area, settings, selected, elapsed, accent, artwork):
    """Split-flap clock: three tiles with labels; the hinge line flashes as a tile changes."""
    width, height = area.size
    item, seconds = selected['event'], selected['seconds']
    draw = ImageDraw.Draw(area)
    title_font = settings['title_font']
    title_height = min(ec._measure(title_font, item['title'])[1], max(1, height // 3))
    info = title_band(ec, area, settings, item, 0, 0, width, title_height, elapsed)
    tiles = units(seconds)
    label_font = 'tom_thumb' if 'tom_thumb' in ec.fonts() else title_font
    label_height = ec._measure(label_font, 'DAYS')[1] + 1
    gap = 2
    tile_width = (width - gap * 2) // 3
    top = title_height + 2
    tile_height = max(4, height - top - label_height - 1)
    face = _mix(settings['background'], '#ffffff', .18)
    digit_colour = settings['text_colour']
    largest = max((value for value, _ in tiles), key=len)
    font, w, h = fit_font(ec, largest, tile_width - 2, tile_height - 2, settings['countdown_font'])
    for index, (value, label) in enumerate(tiles):
        x = index * (tile_width + gap)
        draw.rectangle((x, top, x + tile_width - 1, top + tile_height - 1), fill=face)
        centred_text(ec, area, font, value, x + 1, top + 1, tile_width - 2, tile_height - 2, digit_colour)
        hinge = top + tile_height // 2
        draw.line((x, hinge, x + tile_width - 1, hinge), fill=_mix(face, settings['background'], .7))
        centred_text(ec, area, label_font, label, x, top + tile_height + 1, tile_width, label_height, _hex(_rgb(accent)))
    info.update(countdown_info(font, w, h, tile_width - 2, tile_height - 2))
    info['countdown_units'] = [label for _, label in tiles]
    return info


def _ring(ec, area, settings, selected, elapsed, accent, artwork):
    """Progress ring around the icon/number filling over the last 30 days; title beside it."""
    width, height = area.size
    item, seconds = selected['event'], selected['seconds']
    draw = ImageDraw.Draw(area)
    diameter = min(height, 30, width // 2)
    cx, cy = diameter // 2, height // 2
    fill = 1.0 if seconds <= 0 else max(0.0, min(1.0, 1 - seconds / RING_HORIZON))
    track = _mix(settings['background'], accent, .25)
    radius = diameter / 2 - 1
    steps = 96
    for step in range(steps):
        angle = -math.pi / 2 + 2 * math.pi * step / steps
        colour = _rgb(accent) if step / steps < fill else track
        for thickness in (0, 1):
            r = radius - thickness
            draw.point((round(cx + r * math.cos(angle)), round(cy + r * math.sin(angle))), fill=colour)
    number, unit = big_number(seconds)
    inner = max(4, int(diameter * .7071) - 2)  # inscribed square inside the 2px ring, 1px clear
    font, w, h = fit_font(ec, number, inner, inner - 4, settings['countdown_font'])
    centred_text(ec, area, font, number, cx - inner // 2, cy - inner // 2 - (2 if unit else 0), inner, inner - (2 if unit else 0), settings['text_colour'])
    unit_font = 'tom_thumb' if 'tom_thumb' in ec.fonts() else settings['title_font']
    if unit:
        uh = ec._measure(unit_font, unit)[1]
        centred_text(ec, area, unit_font, unit, cx - inner // 2, cy + inner // 2 - uh, inner, uh, _hex(_mix(settings['text_colour'], settings['background'], .35)))
    column_x = diameter + 2
    column_width = max(1, width - column_x)
    icon(ec, area, item, accent, column_x + max(0, (column_width - 8) // 2), 2, artwork=artwork)
    title_height = min(ec._measure(settings['title_font'], item['title'])[1], height - 12)
    info = title_band(ec, area, settings, item, column_x, 12, column_width, max(1, title_height), elapsed)
    info.update(countdown_info(font, w, h, inner, inner - 4))
    info['ring_fill'] = round(fill, 4)
    return info


def _hero(ec, area, settings, selected, elapsed, accent, artwork):
    """Double-size icon with twinkling sparkles, title and countdown stacked beside it."""
    width, height = area.size
    item, seconds = selected['event'], selected['seconds']
    draw = ImageDraw.Draw(area)
    glow = _mix(settings['background'], accent, .22)
    draw.rectangle((0, 0, 19, height - 1), fill=glow)
    icon_y = max(0, (height - 16) // 2)
    icon(ec, area, item, accent, 2, icon_y, scale=2, artwork=artwork)
    # Twinkles: four sparkle points that blink in turn (deterministic in elapsed time).
    phase = int(elapsed / .25)
    sparkles = ((0, max(0, icon_y - 1)), (18, max(0, icon_y + 2)), (1, min(height - 1, icon_y + 17)), (18, min(height - 1, icon_y + 14)))
    for index, (x, y) in enumerate(sparkles):
        if (phase + index) % 4 < 2 and x < width and y < height:
            draw.point((x, y), fill=_mix(accent, '#ffffff', .6))
    column_x = 21
    column_width = max(1, width - column_x)
    title_height = min(ec._measure(settings['title_font'], item['title'])[1], max(1, height // 2 - 1))
    info = title_band(ec, area, settings, item, column_x, 1, column_width, title_height, elapsed)
    label = ec.compact_countdown(seconds)
    top = title_height + 3
    available = max(1, height - top)
    font, w, h = fit_font(ec, label, column_width, available, settings['countdown_font'])
    centred_text(ec, area, font, label, column_x, top, column_width, available, _hex(_rgb(accent)))
    info.update(countdown_info(font, w, h, column_width, available))
    info['icon_size'] = 16
    return info


def _date(ec, area, settings, selected, elapsed, accent, artwork):
    """The event date as a little calendar tile, title and countdown beside it."""
    width, height = area.size
    item, seconds = selected['event'], selected['seconds']
    draw = ImageDraw.Draw(area)
    start = selected.get('start')
    day = str(start.day) if start else '?'
    month = MONTHS[start.month - 1] if start else ''
    weekday = WEEKDAYS[start.weekday()] if start else ''
    tile_width = min(24, width // 2)
    small = 'tom_thumb' if 'tom_thumb' in ec.fonts() else settings['title_font']
    band = ec._measure(small, 'DEC')[1] + 2
    paper = _mix(settings['background'], '#ffffff', .9)
    draw.rectangle((0, 0, tile_width - 1, height - 1), fill=paper)
    draw.rectangle((0, 0, tile_width - 1, band - 1), fill=_rgb(accent))
    centred_text(ec, area, small, month, 0, 1, tile_width, band - 2, _readable_on(accent))
    day_height = max(1, height - band - band)
    day_font, _, _ = fit_font(ec, day, tile_width - 2, day_height)
    centred_text(ec, area, day_font, day, 1, band, tile_width - 2, day_height, _readable_on(paper))
    centred_text(ec, area, small, weekday, 0, height - band + 1, tile_width, band - 2, _hex(_mix(paper, _readable_on(paper), .55)))
    column_x = tile_width + 2
    column_width = max(1, width - column_x)
    title_height = min(ec._measure(settings['title_font'], item['title'])[1], max(1, height // 2 - 1))
    info = title_band(ec, area, settings, item, column_x, 2, column_width, title_height, elapsed)
    label = ec.compact_countdown(seconds)
    top = title_height + 4
    available = max(1, height - top - 2)
    font, w, h = fit_font(ec, label, column_width, available, settings['countdown_font'])
    centred_text(ec, area, font, label, column_x, top, column_width, available, settings['text_colour'])
    info.update(countdown_info(font, w, h, column_width, available))
    info.update(date_day=day, date_month=month, date_weekday=weekday)
    return info


RENDERERS = dict(tearoff=_tearoff, flip=_flip, ring=_ring, hero=_hero, date=_date)


def title_column_width(layout, width, height):
    """Title space for scroll timing in image_frames (must match the renderers above)."""
    if layout == 'tearoff':
        return width - 2
    if layout == 'ring':
        return max(1, width - min(height, 30, width // 2) - 2)
    if layout == 'hero':
        return max(1, width - 21)
    if layout == 'date':
        return max(1, width - min(24, width // 2) - 2)
    return width


def render(ec, area, settings, selected, elapsed, accent, artwork):
    return RENDERERS[settings['layout']](ec, area, settings, selected, elapsed, accent, artwork)
