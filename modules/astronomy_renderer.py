"""One offline prepared-card pixel/timing path for panel and PNG preview.

The Moon is stylized, not a sky-oriented or north-up map. Preparation performs
font I/O; playback performs no astronomy, catalogue access or font loading.
"""
import datetime as dt
import math
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from PIL import Image
from font_catalog import FONT_DIR, font_key, available_font_names
from frame_capture import BdfFont

UTC = dt.timezone.utc


def minute_label(value, pattern='%H:%M'):
    # Round the instant, never wall time: arithmetic in a DST fold is ambiguous.
    rounded = value.astimezone(UTC) + dt.timedelta(seconds=30)
    return rounded.replace(second=0, microsecond=0).astimezone(value.tzinfo).strftime(pattern)


@dataclass(frozen=True)
class MoonOptions:
    title_font: str = '5x7'
    detail_font: str = '4x6'
    hold_seconds: float = 10
    frame_delay: float = .05
    background: str = '#000000'
    text_colour: str = '#FFFFFF'
    moon_colour: str = '#D9D9D9'

    def __post_init__(self):
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


@lru_cache(maxsize=32)
def _font(name):
    return BdfFont(next(p for p in FONT_DIR.glob('*.bdf') if font_key(p) == name))


def _line(text, name, x, y, width, height):
    advance, pixels = _font(name).text_pixels(0, 0, text)
    left = min((p[0] for p in pixels), default=0)
    top = min((p[1] for p in pixels), default=0)
    right = max((p[0] for p in pixels), default=-1) + 1
    return (tuple((px - min(0, left), py - top) for px, py in pixels),
            max(advance, right) - min(0, left), x, y, width, height)


def prepare_moon(moon, options=None):
    options = options or MoonOptions()
    if moon.horizon_state == 'always_above':
        horizon = 'Always above horizon'
    elif moon.horizon_state == 'always_below':
        horizon = 'Always below horizon'
    else:
        rises = ','.join(minute_label(t) for t in moon.rises) or '--'
        sets = ','.join(minute_label(t) for t in moon.sets) or '--'
        horizon = f'R {rises}  S {sets}'
    labels = (moon.phase_name, f'{moon.illumination * 100:.0f}% lit', horizon,
              f'Next {moon.next_phase.name} {minute_label(moon.next_phase.time, "%d %b %H:%M")}')
    lines = tuple(_line(text, *geometry) for text, geometry in zip(labels, (
        (options.title_font, 17, 0, 47, 7), (options.detail_font, 17, 9, 47, 6),
        (options.detail_font, 0, 18, 64, 6), (options.detail_font, 0, 26, 64, 6))))
    clipping = tuple(any(py >= line[5] for px, py in line[0]) for line in lines)
    return PreparedMoon(labels, lines, moon.phase_degrees, options, clipping)


def prepare_pass(engine, now, location, *, current_moon=True, options=None, stop_event=None):
    """Recalculate outside playback; host owns errors and empty-cycle idling."""
    from astronomy import Cancelled, check_cancel
    if not current_moon:
        return ()
    try:
        check_cancel(stop_event)
        moon = engine.moon(now, location, stop_event=stop_event)
        check_cancel(stop_event)
        card = prepare_moon(moon, options)
        check_cancel(stop_event)
        return (card,)
    except Cancelled:
        return ()


def _scroll_ticks(card):
    return max((line[1] + 1 for line in card.lines if line[1] > line[4]), default=0)


def render_frame(card, tick=0):
    image = Image.new('RGB', (64, 32), card.options.background)
    moon_colour = tuple(bytes.fromhex(card.options.moon_colour[1:]))
    text_colour = tuple(bytes.fromhex(card.options.text_colour[1:]))
    # Orthographic sphere shading with a fixed schematic waxing-right view.
    angle = math.radians(card.phase_degrees)
    sx, sz = math.sin(angle), -math.cos(angle)
    for y in range(16):
        for x in range(16):
            nx, ny = (x - 7.5) / 7.5, (y - 7.5) / 7.5
            radius = nx * nx + ny * ny
            if radius <= 1:
                lit = nx * sx + math.sqrt(1 - radius) * sz > 0
                image.putpixel((x, y), moon_colour if lit else (25, 25, 25))
    for points, measured, x, y, width, height in card.lines:
        offset = -tick if measured > width and tick <= measured else 0
        for px, py in points:
            px += offset
            if 0 <= px < width and 0 <= py < height:
                image.putpixel((x + px, y + py), text_colour)
    return image


def _pass(cards):
    if type(cards) is not tuple or len(cards) > 51 or any(not isinstance(c, PreparedMoon) for c in cards):
        raise ValueError('Expected immutable bounded prepared cards')
    return cards


def duration_seconds(cards):
    return sum(_scroll_ticks(c) * c.options.frame_delay + c.options.hold_seconds for c in _pass(cards))


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def image_frames(cards, stop_event=None):
    for card in _pass(cards):
        ticks = _scroll_ticks(card)
        for tick in range(ticks + 1):
            if _stopped(stop_event):
                return
            image = render_frame(card, tick)
            if _stopped(stop_event):
                return
            yield image, card.options.frame_delay if tick < ticks else card.options.hold_seconds, {'tick': tick}


def preview_frames(cards, stop_event=None):
    from edit_preview import _png_frame
    for image, delay, info in image_frames(cards, stop_event):
        if _stopped(stop_event):
            return
        frame = _png_frame(image, delay * 1000)
        frame['astronomy'] = info
        if _stopped(stop_event):
            return
        yield frame


def run(matrix, cards, stop_event=None, *, clock=time.monotonic, sleep=time.sleep):
    deadline, count = clock(), 0
    for image, delay, _ in image_frames(cards, stop_event):
        if _stopped(stop_event):
            break
        matrix.draw_image(image)
        if _stopped(stop_event):
            break
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
