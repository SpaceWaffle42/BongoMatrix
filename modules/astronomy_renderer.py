"""One offline prepared-card pixel/timing path for panel and PNG preview.

The Moon is stylized, not a sky-oriented or north-up map. Preparation performs
font I/O; playback performs no astronomy, catalogue access or font loading.
"""
import datetime as dt
import math
import re
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from PIL import Image
from font_catalog import FONT_DIR, font_key, available_font_names
from frame_capture import BdfFont
from astronomy import AstronomyEvent, event_eligible, _utc

UTC = dt.timezone.utc
SPRITES = FONT_DIR.parent / 'astronomy' / 'sprites'
MOON_FRAMES = 32  # Aseprite sheet frames: index i covers phase angle i*360/32.
VIEWS = ('simple', 'advanced')
PANEL = (64, 32)


@lru_cache(maxsize=4)
def _sheet(name):
    with Image.open(SPRITES / name) as image:
        return image.convert('RGBA')


def moon_sprite(size, phase_degrees):
    """One immutable RGBA frame from the Aseprite-exported phase sheet."""
    index = round((phase_degrees % 360) / 360 * MOON_FRAMES) % MOON_FRAMES
    return _sheet(f'moon_{size}.png').crop((index * size, 0, (index + 1) * size, size))


def event_icon(kind):
    index = {'full_moon': 0, 'new_moon': 1}.get(kind, 2)
    return _sheet('event_icons.png').crop((index * 8, 0, index * 8 + 8, 8))


def _tint(sprite, colour):
    """Scale the neutral sprite toward the configured moon colour, keep alpha."""
    if colour == '#D9D9D9':
        return sprite
    r, g, b = bytes.fromhex(colour[1:])
    channels = sprite.split()
    scaled = [c.point(lambda v, k=k: min(255, v * k // 217)) for c, k in zip(channels[:3], (r, g, b))]
    return Image.merge('RGBA', (*scaled, channels[3]))


def minute_label(value, pattern='%H:%M'):
    # Round the instant, never wall time: arithmetic in a DST fold is ambiguous.
    rounded = value.astimezone(UTC) + dt.timedelta(seconds=30)
    return rounded.replace(second=0, microsecond=0).astimezone(value.tzinfo).strftime(pattern)


ROLES = ('title', 'date', 'description')


def _check_role_colours(options):
    for role in ROLES:
        value = getattr(options, role + '_colour')
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', value)):
            raise ValueError('RGB hex colours required')


def _role_rgb(options, role):
    return tuple(bytes.fromhex((getattr(options, role + '_colour') or options.text_colour)[1:]))


@dataclass(frozen=True)
class MoonOptions:
    title_font: str = '5x7'
    detail_font: str = '4x6'
    hold_seconds: float = 10
    frame_delay: float = .05
    background: str = '#000000'
    text_colour: str = '#FFFFFF'
    moon_colour: str = '#D9D9D9'
    view: str = 'simple'
    # Per-role colours; None follows text_colour (callers predating them are unchanged).
    title_colour: str | None = None
    date_colour: str | None = None
    description_colour: str | None = None

    def __post_init__(self):
        _check_role_colours(self)
        if self.view not in VIEWS:
            raise ValueError('Moon view must be simple or advanced')
        for value, low, high in ((self.hold_seconds, 1, 300), (self.frame_delay, .01, 1)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Invalid bounded card timing')
        if any(name not in available_font_names() for name in (self.title_font, self.detail_font)):
            raise ValueError('Installed fonts required')
        if any(not isinstance(c, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', c)
               for c in (self.background, self.text_colour, self.moon_colour)):
            raise ValueError('RGB hex colours required')


@dataclass(frozen=True)
class PreparedMoon:
    labels: tuple
    lines: tuple
    phase_degrees: float
    options: MoonOptions
    clipping: tuple
    refresh_at: dt.datetime | None = None
    sprite: Image.Image | None = None
    size: tuple = PANEL  # Canvas the card is laid out for (the safe area's inner size).


@dataclass(frozen=True)
class EventOptions:
    title_font: str = '5x7'
    detail_font: str = '4x6'
    countdown_font: str = 'auto'
    hold_seconds: float = 8
    frame_delay: float = .05
    background: str = '#000000'
    text_colour: str = '#FFFFFF'
    accent: str = '#B388FF'
    title_colour: str | None = None
    date_colour: str | None = None
    description_colour: str | None = None

    def __post_init__(self):
        _check_role_colours(self)
        MoonOptions(self.title_font, self.detail_font, self.hold_seconds,
                    self.frame_delay, self.background, self.text_colour, self.accent)
        if self.countdown_font != 'auto' and self.countdown_font not in available_font_names():
            raise ValueError('Installed countdown font required')


@dataclass(frozen=True)
class PreparedEvent:
    event: AstronomyEvent
    prepared_at: dt.datetime
    labels: tuple
    lines: tuple
    window_lines: tuple
    countdown_fonts: tuple
    options: EventOptions
    clipping: tuple
    size: tuple = PANEL


def event_countdown(event, now):
    """Count down to the next local observing window; NOW only inside one.

    The exact global phase instant is a separate labelled date/time, not an
    invented phase duration. Event Countdown's seconds-based helper differs.
    """
    now = _utc(now)
    window = next((w for w in event.windows if w.end > now), None)
    if window is None:
        return None
    seconds = max(0, math.ceil((window.start - now).total_seconds()))
    if not seconds:
        return 'NOW'
    if seconds >= 86400:
        return f'{seconds // 86400}d {seconds % 86400 // 3600}h'
    if seconds >= 3600:
        return f'{seconds // 3600}h {seconds % 3600 // 60}m'
    return f'{max(1, seconds // 60)}m'


@lru_cache(maxsize=32)
def _font(name):
    return BdfFont(next(p for p in FONT_DIR.glob('*.bdf') if font_key(p) == name))


def _line(text, name, x, y, width, height):
    advance, pixels = _font(name).text_pixels(0, 0, text)
    return _pixel_line(advance, pixels, x, y, width, height)


def _pixel_line(advance, pixels, x, y, width, height):
    left = min((p[0] for p in pixels), default=0)
    top = min((p[1] for p in pixels), default=0)
    right = max((p[0] for p in pixels), default=-1) + 1
    return (tuple((px - min(0, left), py - top) for px, py in pixels),
            max(advance, right) - min(0, left), x, y, width, height)


def _countdown_line(fonts, text, width=64):
    # The font choice never depends on the canvas: a smaller canvas clips, it does not step down.
    candidates = []
    for name, glyphs in fonts:
        glyphs = dict(glyphs)
        advance, pixels = 0, []
        for character in text:
            size, points = glyphs[character]
            pixels.extend((px + advance, py) for px, py in points)
            advance += size
        line = _pixel_line(advance, pixels, 0, 25, width, 7)
        height = max((py for px, py in line[0]), default=-1) + 1
        candidates.append((height, line[1], name, line))
    fitting = [c for c in candidates if c[0] <= 7 and c[1] <= 64]
    selected = max(fitting) if fitting else min(candidates)
    return selected[3], selected[2], selected[0] > 7 or selected[1] > width


def prepare_event(event, now, options=None):
    from zoneinfo import ZoneInfo
    now = _utc(now)
    options = options or EventOptions()
    zone = ZoneInfo(event.location.timezone)
    windows = tuple(f'Partly visible {minute_label(w.start.astimezone(zone), "%H:%M %Z")}'
                    f'-{minute_label(w.end.astimezone(zone), "%H:%M %Z")}' for w in event.windows)
    labels = (event.title, minute_label(event.time.astimezone(zone), '%d %b %H:%M %Z'), windows[0])
    lines = (_line(labels[0], options.title_font, 10, 0, 54, 7),
             _line(labels[1], options.detail_font, 0, 9, 64, 6),
             _line(labels[2], options.detail_font, 0, 17, 64, 6))
    window_lines = tuple(_line(text, options.detail_font, 0, 17, 64, 6) for text in windows)
    names = available_font_names() if options.countdown_font == 'auto' else (options.countdown_font,)
    fonts = tuple((name, tuple((c, (advance, tuple(points)))
                    for c in '0123456789dhm NOW'
                    for advance, points in (_font(name).text_pixels(0, 0, c),))) for name in names)
    _, _, clipped = _countdown_line(fonts, event_countdown(event, now) or 'NOW')
    clipping = tuple(any(py >= line[5] for px, py in line[0]) for line in lines) + (clipped,)
    return PreparedEvent(event, now.astimezone(UTC), labels, lines, window_lines, fonts, options, clipping)


def _simple_lines(phase_name, options):
    """Two centred words beside the large moon; largest configured font that fits."""
    words = tuple(phase_name.split(' ', 1)) if ' ' in phase_name else (phase_name, '')
    for name in (options.title_font, options.detail_font, 'tom_thumb'):
        measured = [_line(word, name, 0, 0, 33, 16) for word in words]
        if all(line[1] <= 33 for line in measured):
            break
    heights = [max((py for _, py in line[0]), default=-1) + 1 for line in measured]
    return words, _place_simple(tuple((points, width, 0, 0, 33, height)
                                      for (points, width, *_), height in zip(measured, heights)), PANEL)


def _place_simple(lines, size):
    """Centre the phase words in the column right of the 28 px moon, on a SIZE canvas."""
    column = size[0] - 31
    gap = 2
    placed, y = [], (size[1] - sum(line[5] for line in lines) - gap) // 2
    for points, width, _, _, _, height in lines:
        placed.append((points, width, 31 + max(0, (column - width) // 2), y, column, height))
        y += height + gap
    return tuple(placed)


def _check_size(size):
    if (type(size) is not tuple or len(size) != 2 or any(type(v) is not int for v in size)
            or not (1 <= size[0] <= 64 and 1 <= size[1] <= 32)):
        raise ValueError('Astronomy canvas size must be (1..64, 1..32)')
    return size


def layout_for_size(card, size):
    """The same card laid out as if the panel were SIZE (the safe area's inner canvas).

    Lines keep their font, position and glyph pixels; lines that reach the right
    edge now end at the inner width (and scroll if they no longer fit). The moon,
    icons and fonts are never resized: what does not fit is clipped.
    """
    size = _check_size(tuple(size))
    if size == card.size:
        return card
    if isinstance(card, PreparedMoon) and card.options.view == 'simple':
        lines = _place_simple(card.lines, size)
        return replace(card, lines=lines, size=size, clipping=tuple(line[1] > line[4] for line in lines))
    grow = size[0] - card.size[0]
    def widen(lines):  # Every non-simple line runs to the right edge of the canvas.
        return tuple((points, measured, x, y, width + grow, height)
                     for points, measured, x, y, width, height in lines)
    if isinstance(card, PreparedEvent):
        return replace(card, lines=widen(card.lines), window_lines=widen(card.window_lines), size=size)
    return replace(card, lines=widen(card.lines), size=size)


def prepare_moon(moon, options=None):
    options = options or MoonOptions()
    if options.view == 'simple':
        labels, lines = _simple_lines(moon.phase_name, options)
        clipping = tuple(line[1] > line[4] for line in lines)
        sprite = _tint(moon_sprite(28, moon.phase_degrees), options.moon_colour)
        return PreparedMoon(labels, lines, moon.phase_degrees, options, clipping, moon.next_phase.time, sprite)
    if moon.horizon_state == 'always_above':
        horizon = 'Always above horizon'
    elif moon.horizon_state == 'always_below':
        horizon = 'Always below horizon'
    else:
        rises = ','.join(minute_label(t) for t in moon.rises) or '--'
        sets = ','.join(minute_label(t) for t in moon.sets) or '--'
        horizon = f'R {rises}  S {sets}'
    # Roles: phase name = title; illumination and rise/set = description; next phase = date.
    labels = (moon.phase_name, f'{moon.illumination * 100:.0f}% lit', horizon,
              f'Next {moon.next_phase.name} {minute_label(moon.next_phase.time, "%d %b %H:%M")}')
    lines = tuple(_line(text, *geometry) for text, geometry in zip(labels, (
        (options.title_font, 17, 0, 47, 7), (options.detail_font, 17, 9, 47, 6),
        (options.detail_font, 0, 18, 64, 6), (options.detail_font, 0, 26, 64, 6))))
    clipping = tuple(any(py >= line[5] for px, py in line[0]) for line in lines)
    sprite = _tint(moon_sprite(16, moon.phase_degrees), options.moon_colour)
    return PreparedMoon(labels, lines, moon.phase_degrees, options, clipping, moon.next_phase.time, sprite)


def prepare_pass(engine, now, location, *, current_moon=True, lunar_events=False,
                 horizon_days=90, event_limit=5, options=None, event_options=None, stop_event=None):
    """Recalculate outside playback; host owns errors and empty-cycle idling."""
    from astronomy import Cancelled, check_cancel, select_events
    if not current_moon and not lunar_events:
        return ()
    try:
        check_cancel(stop_event)
        if lunar_events:
            select_events((), now, location, horizon_days=horizon_days, limit=event_limit)
        cards = []
        if current_moon:
            moon = engine.moon(now, location, stop_event=stop_event)
            check_cancel(stop_event)
            cards.append(prepare_moon(moon, options))
        if lunar_events:
            events = engine.lunar_events(now, location, horizon_days=horizon_days, stop_event=stop_event)
            for event in select_events(events, now, location, horizon_days=horizon_days, limit=event_limit):
                check_cancel(stop_event)
                cards.append(prepare_event(event, now, event_options))
        check_cancel(stop_event)
        return tuple(cards)
    except Cancelled:
        return ()


def _scroll_ticks(card):
    lines = card.lines + (card.window_lines if isinstance(card, PreparedEvent) else ())
    return max((line[1] + 1 for line in lines if line[1] > line[4]), default=0)


def render_frame(card, tick=0, *, now=None):
    image = Image.new('RGB', card.size, card.options.background)
    if isinstance(card, PreparedEvent):
        now = now or card.prepared_at
        countdown = event_countdown(card.event, now)
        if countdown is None:
            return image
        index = next(i for i, w in enumerate(card.event.windows) if w.end > now.astimezone(UTC))
        counter, _, _ = _countdown_line(card.countdown_fonts, countdown, card.size[0])
        lines = card.lines[:2] + (card.window_lines[index], counter)
        roles = EVENT_ROLES
        # Aseprite 8x8 event icons (tinted with the accent); unknown types use a star.
        image.paste(_tint(event_icon(card.event.kind), card.options.accent), (0, 0), event_icon(card.event.kind))
    else:
        lines = card.lines
        roles = ('title',) * len(lines) if card.options.view == 'simple' else ADVANCED_MOON_ROLES
        # Aseprite-drawn phase sprite: 28 px simple view, 16 px advanced view.
        # Fixed waxing-right schematic, not a sky-oriented render.
        top = 2 if card.options.view == 'simple' else 0
        image.paste(card.sprite, (top, top), card.sprite)
    for (points, measured, x, y, width, height), role in zip(lines, roles, strict=True):
        colour = _role_rgb(card.options, role)
        offset = -tick if measured > width and tick <= measured else 0
        for px, py in points:
            px += offset
            if 0 <= px < width and 0 <= py < height and y + py < image.height:
                image.putpixel((x + px, y + py), colour)
    return image


# Event card: title, date line, visibility window (description), countdown (date).
EVENT_ROLES = ('title', 'date', 'description', 'date')
ADVANCED_MOON_ROLES = ('title', 'description', 'description', 'date')


def _pass(cards, size=None):
    if type(cards) is not tuple or len(cards) > 51 or any(not isinstance(c, (PreparedMoon, PreparedEvent)) for c in cards):
        raise ValueError('Expected immutable bounded prepared cards')
    return cards if size is None else tuple(layout_for_size(card, size) for card in cards)


def duration_seconds(cards, *, now=None, size=None):
    """Exact shared virtual timeline; slow hardware/cancellation can differ."""
    return sum(delay for _, _, delay, _ in _frame_specs(_pass(cards, size), now=now))


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def _frame_specs(cards, stop_event=None, *, now=None, utc_clock=None):
    cards = _pass(cards)
    origin = _utc(now or next((c.prepared_at for c in cards if isinstance(c, PreparedEvent)), dt.datetime.now(UTC)))
    elapsed = 0
    for card_index, card in enumerate(cards):
        ticks = _scroll_ticks(card)
        for tick in range(ticks + 1):
            remaining = card.options.frame_delay if tick < ticks else card.options.hold_seconds
            while remaining > 0.000001:
                if _stopped(stop_event):
                    return
                moment = _utc(utc_clock() if utc_clock else origin + dt.timedelta(seconds=elapsed))
                info = {'tick': tick, 'card_index': card_index}
                delay = remaining
                if isinstance(card, PreparedEvent):
                    if not event_eligible(card.event, moment):
                        break
                    index = next(i for i, w in enumerate(card.event.windows) if w.end > moment)
                    window = card.event.windows[index]
                    until_start = (window.start - moment).total_seconds()
                    if until_start > 0:
                        # Match the seconds-free label's actual next change.
                        quantum = 3600 if until_start >= 86400 else 60
                        threshold = max(0, math.ceil(until_start) // quantum * quantum - 1)
                        change = until_start - threshold
                    else:
                        change = (window.end - moment).total_seconds()
                    delay = min(delay, max(0.000001, change))
                    label = event_countdown(card.event, moment)
                    info = dict(info, countdown=label, window_index=index)
                if _stopped(stop_event):
                    return
                yield card, moment, delay, info
                elapsed += delay
                remaining -= delay


def image_frames(cards, stop_event=None, *, now=None, utc_clock=None, size=None):
    for card, moment, delay, info in _frame_specs(_pass(cards, size), stop_event, now=now, utc_clock=utc_clock):
        if _stopped(stop_event):
            return
        if isinstance(card, PreparedEvent):
            image = render_frame(card, info['tick'], now=moment)
            _, font, clipping = _countdown_line(card.countdown_fonts, info['countdown'], card.size[0])
            info = dict(info, countdown_font=font, countdown_clipped=clipping)
        else:
            image = render_frame(card, info['tick'])
        if _stopped(stop_event):
            return
        yield image, delay, info


def preview_frames(cards, stop_event=None, *, now=None, safe_area=None):
    """With a Screen Safe Area cards are laid out on the inner canvas, exactly as run() does."""
    from edit_preview import _png_frame
    size = safe_area.inner_size() if safe_area is not None else None
    for image, delay, info in image_frames(cards, stop_event, now=now, size=size):
        if _stopped(stop_event):
            return
        if safe_area is not None:
            image = safe_area.compose(image, cards[info['card_index']].options.background)
        frame = _png_frame(image, delay * 1000)
        frame['astronomy'] = info
        if _stopped(stop_event):
            return
        yield frame


def run(matrix, cards, stop_event=None, *, clock=time.monotonic, sleep=time.sleep, utc_clock=None):
    from safe_area import canvas_size
    utc_clock = utc_clock or (lambda: dt.datetime.now(UTC))
    size = canvas_size(matrix)
    deadline, count = clock(), 0
    for image, delay, info in image_frames(cards, stop_event, utc_clock=utc_clock, size=size):
        if _stopped(stop_event):
            break
        card = cards[info['card_index']]
        def still_eligible():
            if not isinstance(card, PreparedEvent):
                return True
            now = utc_clock().astimezone(UTC)
            index = next((i for i, w in enumerate(card.event.windows) if w.end > now), None)
            return index == info['window_index'] and event_countdown(card.event, now) == info['countdown']
        if not still_eligible():
            continue
        if size != PANEL:
            # Smaller (safe-area) canvas: the card background also covers the margin, as in the preview.
            matrix.fill(*Image.new('RGB', (1, 1), card.options.background).getpixel((0, 0)))
        matrix.draw_image(image)
        if _stopped(stop_event):
            break
        if not still_eligible():
            continue
        matrix.swap()
        count += 1
        deadline += delay
        remaining = max(0, deadline - clock())
        if stop_event is not None:
            if stop_event.wait(remaining) or stop_event.is_set():
                break
        else:
            sleep(remaining)
    return count
