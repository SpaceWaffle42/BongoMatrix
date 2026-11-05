"""Prepared-only RSS pixels. No feed access, media decoding, or host registration.

Preparation loads installed BDFs outside playback; immutable glyph runs, not
unbounded text bitmaps, cross the render boundary. Coordinates use ink tops.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
from fractions import Fraction
from types import MappingProxyType
import time
from PIL import Image
from font_catalog import FONT_DIR, font_key
from frame_capture import BdfFont
from rss_config import RenderOptions


@dataclass(frozen=True)
class TextRun:
    glyphs: tuple
    width: int
    height: int
    left: int
    top: int


@lru_cache(maxsize=26)
def _font(name):
    return BdfFont(next(p for p in FONT_DIR.glob('*.bdf') if font_key(p) == name))


@lru_cache(maxsize=4096)
def _bounds(glyph):
    points = [(glyph.x_offset + x, -glyph.y_offset - glyph.height + y)
              for y, row in enumerate(glyph.rows) for x in range(glyph.width)
              if row & (1 << (glyph.bits_per_row - 1 - x))]
    if not points:
        return None
    return min(x for x, y in points), min(y for x, y in points), max(x for x, y in points) + 1, max(y for x, y in points) + 1


def _measure(font, text):
    glyphs = []
    cursor = left = right = 0
    top = bottom = None
    for c in text:
        glyph = font.glyph(c)
        if glyph is None:
            continue
        glyphs.append((cursor, glyph))
        bounds = _bounds(glyph)
        if bounds:
            x0, y0, x1, y1 = bounds
            left, right = min(left, cursor + x0), max(right, cursor + x1)
            top = y0 if top is None else min(top, y0)
            bottom = y1 if bottom is None else max(bottom, y1)
        cursor += glyph.advance
    return TextRun(tuple(glyphs), max(cursor, right) - left,
                   bottom - top if top is not None else 0, left, top or 0)


@lru_cache(maxsize=64)
def _line_box(name):
    """(ink top, ink height) of the font's printable ASCII: one baseline for every headline."""
    run = _measure(_font(name), ''.join(chr(c) for c in range(32, 127)))
    return run.top, run.height


PANEL = (64, 32)


def _rows(prepared_or_options, thumbnail, height=32):
    """(headline row height, summary area height). Text rows grow to the headline font (min 7px for the banner)."""
    hh = 16 if thumbnail else min(16, max(7, _line_box(prepared_or_options.headline_font)[1]))
    return hh, max(1, height - hh - 1)


def _headline_base(headline, options, thumbnail):
    """Row offset of the headline's ink: a shared baseline so the row never jumps between stories."""
    return 0 if thumbnail else headline.top - _line_box(options.headline_font)[0]


def _wrap(font, text, width=64):
    # Word wrapping with hard breaks for overlong tokens, bounded by 2000 chars.
    lines, current = [], ''
    for word in text.split():
        candidate = (current + ' ' + word).strip()
        if _measure(font, candidate).width <= width:
            current = candidate
            continue
        if current:
            lines.append(_measure(font, current))
            current = ''
        for character in word:
            if current and _measure(font, current + character).width > width:
                lines.append(_measure(font, current))
                current = ''
            current += character
    if current:
        lines.append(_measure(font, current))
    return tuple(lines)


@dataclass(frozen=True)
class PreparedThumbnail:
    """Already decoded/resized RGB bytes and source milliseconds. No I/O.

    At most 300 frames / 230400 pixel bytes per prepared thumbnail. The future
    secure decoder must enforce its own original dimensions/time/download caps.
    """
    frames: tuple[bytes, ...]
    durations_ms: tuple[int | Fraction, ...]

    def __post_init__(self):
        if (type(self.frames) is not tuple or type(self.durations_ms) is not tuple
                or not 1 <= len(self.frames) <= 300 or len(self.frames) != len(self.durations_ms)
                or any(type(frame) is not bytes or len(frame) != 16 * 16 * 3 for frame in self.frames)
                or any(type(delay) not in (int, Fraction) or not 1 <= delay <= 60000
                       or delay.numerator > 0xffffffff or delay.denominator > 0xffffffff
                       for delay in self.durations_ms)):
            raise ValueError('Expected bounded immutable RGB frames and exact 1..60000ms uint32 rational delays')


@dataclass(frozen=True)
class PreparedBanner:
    """Still site banner on the headline row: exact RGB bytes, no I/O at render time."""
    width: int
    height: int
    pixels: bytes

    def __post_init__(self):
        if (type(self.width) is not int or type(self.height) is not int
                or not 1 <= self.width <= 32 or not 1 <= self.height <= 7
                or type(self.pixels) is not bytes or len(self.pixels) != self.width * self.height * 3):
            raise ValueError('Expected a still banner at most 32x7 with exact RGB bytes')


@dataclass(frozen=True)
class PreparedStory:
    headline: TextRun
    lines: tuple[TextRun, ...]
    line_pitch: int
    options: RenderOptions
    warnings: object
    thumbnail: PreparedThumbnail | None = None
    banner: PreparedBanner | None = None
    # Canvas the story is laid out for (the Screen Safe Area inner size) and the
    # summary text, kept so playback can re-wrap it for a smaller canvas.
    size: tuple = PANEL
    summary: str = ''


def _check_size(size):
    if (type(size) is not tuple or len(size) != 2 or any(type(v) is not int for v in size)
            or not (1 <= size[0] <= 64 and 1 <= size[1] <= 32)):
        raise ValueError('RSS canvas size must be (1..64, 1..32)')
    return size


def _warnings(headline, lines, options, thumbnail, size):
    hh, sh = _rows(options, thumbnail, size[1])
    base = _headline_base(headline, options, thumbnail)
    return MappingProxyType({'headline_clipped': base < 0 or base + headline.height > hh,
                             'headline_height': headline.height, 'headline_available_height': hh,
                             'summary_clipped': any(line.width > size[0] or line.height > sh for line in lines),
                             'summary_max_height': max((line.height for line in lines), default=0),
                             'summary_available_height': sh})


def layout_for_size(prepared, size):
    """The same story laid out as if the panel were SIZE (the safe area's inner canvas).

    Only the canvas changes: the summary re-wraps at the inner width and rows use
    the inner height. Fonts, line pitch, thumbnail and banner keep their size;
    whatever does not fit is clipped like on a real smaller panel.
    """
    size = _check_size(tuple(size))
    if size == prepared.size:
        return prepared
    lines = _wrap(_font(prepared.options.summary_font), prepared.summary, size[0])
    return replace(prepared, lines=lines, size=size, warnings=_warnings(
        prepared.headline, lines, prepared.options, prepared.thumbnail, size))


def prepare_story(story, options=None, *, thumbnail=None, banner=None):
    options = options or RenderOptions()
    if thumbnail is not None and not isinstance(thumbnail, PreparedThumbnail):
        raise ValueError('Thumbnail must be prepared before rendering')
    if banner is not None and not isinstance(banner, PreparedBanner):
        raise ValueError('Banner must be prepared before rendering')
    if options.layout == 'text':
        thumbnail = banner = None
    if thumbnail is not None:
        banner = None  # A 16x16 thumbnail already occupies the headline corner.
    if not isinstance(story.title, str) or len(story.title) > 300 or not isinstance(story.summary, str) or len(story.summary) > 2000:
        raise ValueError('Prepared story exceeds normalized text bounds')
    headline = _measure(_font(options.headline_font), ' '.join(story.title.split()))
    summary_font = _font(options.summary_font)
    lines = _wrap(summary_font, story.summary)
    pitch = max(1, max(g.height for g in summary_font.glyphs.values()))
    warnings = _warnings(headline, lines, options, thumbnail, PANEL)
    return PreparedStory(headline, lines, pitch, options, warnings, thumbnail, banner, PANEL, story.summary)


def _draw(area, run, x, y, colour):
    for cursor, glyph in run.glyphs:
        gx = x + cursor - run.left + glyph.x_offset
        gy = y - run.top - glyph.y_offset - glyph.height
        if gx >= area.width or gx + glyph.width <= 0 or gy >= area.height or gy + glyph.height <= 0:
            continue
        for row_index, row in enumerate(glyph.rows):
            py = gy + row_index
            if 0 <= py < area.height:
                for col in range(glyph.width):
                    px = gx + col
                    if 0 <= px < area.width and row & (1 << (glyph.bits_per_row - 1 - col)):
                        area.putpixel((px, py), colour)


HEADLINE_GAP = 16  # Pixels between the end of a looping headline and its repeat.
READ_HOLD = 40  # Minimum ticks (2 s at normal speed) before the summary starts to move.
SUMMARY_GAP = 8  # Pixels between the end of a looping summary and its repeat (continuous mode).


def _headline_x(p):
    """Headline column: after a 16px thumbnail, or after the banner plus a 1px gap."""
    if p.thumbnail:
        return 17
    return p.banner.width + 1 if p.banner else 0


def render_frame(prepared, tick, *, thumbnail_index=0):
    background = prepared.options.background_colour
    width, height = prepared.size
    image = Image.new('RGB', (width, height), background)
    hh, sh = _rows(prepared.options, prepared.thumbnail, height)
    base = _headline_base(prepared.headline, prepared.options, prepared.thumbnail)
    tx = _headline_x(prepared)
    if prepared.thumbnail:
        image.paste(Image.frombytes('RGB', (16, 16), prepared.thumbnail.frames[thumbnail_index]), (0, 0))
    elif prepared.banner:
        b = prepared.banner
        image.paste(Image.frombytes('RGB', (b.width, b.height), b.pixels), (0, 0))
    title = Image.new('RGB', (max(1, width - tx), hh), background)
    loop, hold, scroll, _ = _phases(prepared)
    if prepared.options.headline_mode == 'continuous':
        return _render_continuous(prepared, tick, image, title, tx, hh, sh, loop, scroll, base)
    # Phase 1 (tick < hold): a too-wide headline loops once, seamlessly, with a
    # repeat following HEADLINE_GAP pixels behind; the summary's first lines wait
    # (at least READ_HOLD ticks so short stories stay readable).
    # Phase 2: the headline rests at its start while the summary scrolls up/off.
    x = -tick if tick < loop else 0
    colour = tuple(bytes.fromhex(prepared.options.headline_colour[1:]))
    _draw(title, prepared.headline, x, base, colour)
    if x:
        _draw(title, prepared.headline, x + loop, base, colour)
    image.paste(title, (tx, 0))
    summary = Image.new('RGB', (image.width, sh), background)
    sy = -max(0, tick - hold)
    for index, line in enumerate(prepared.lines):
        _draw(summary, line, 0, sy + index * prepared.line_pitch, tuple(bytes.fromhex(prepared.options.summary_colour[1:])))
    image.paste(summary, (0, hh + 1))
    return image, {'summary_y': sy, 'headline_x': x, 'summary_done': tick >= hold + scroll}


def _render_continuous(prepared, tick, image, title, tx, hh, sh, loop, cycle, base=0):
    """Headline and summary move together from the first frame.

    The headline loops across (seamless repeat HEADLINE_GAP behind); the summary
    is a seamless upward ribbon (repeat SUMMARY_GAP below) that keeps cycling
    until the headline is back at its start.
    """
    background = prepared.options.background_colour
    x = -(tick % loop) if loop else 0
    colour = tuple(bytes.fromhex(prepared.options.headline_colour[1:]))
    _draw(title, prepared.headline, x, base, colour)
    if x:
        _draw(title, prepared.headline, x + loop, base, colour)
    image.paste(title, (tx, 0))
    summary = Image.new('RGB', (image.width, sh), background)
    sy = -(tick % cycle) if cycle else 0
    summary_colour = tuple(bytes.fromhex(prepared.options.summary_colour[1:]))
    for offset in ((0, cycle) if cycle else (0,)):
        for index, line in enumerate(prepared.lines):
            _draw(summary, line, 0, sy + offset + index * prepared.line_pitch, summary_colour)
    image.paste(summary, (0, hh + 1))
    _, _, _, count = _phases(prepared)
    return image, {'summary_y': sy, 'headline_x': x, 'summary_done': tick >= count - 1}


def _phases(p):
    """(headline loop ticks, summary start tick, summary scroll ticks, total frames).

    'once' (default): headline loops once, then the summary scrolls up and off.
    'continuous': both move from the start; the summary cycles (scroll = one
    cycle incl. SUMMARY_GAP) until the headline loop and at least one summary
    cycle (and READ_HOLD) are complete.
    """
    width = p.size[0] - _headline_x(p)
    loop = p.headline.width + HEADLINE_GAP if p.headline.width > width else 0
    scroll = (len(p.lines) - 1) * p.line_pitch + p.lines[-1].height if p.lines else 0
    visible = p.headline.height or p.lines or p.thumbnail or p.banner
    if p.options.headline_mode == 'continuous':
        cycle = scroll + SUMMARY_GAP if p.lines else 0
        span = max(loop, cycle, READ_HOLD if p.lines else 0)
        return loop, 0, cycle, span + 1 if visible else 0
    hold = max(loop, READ_HOLD) if p.lines else loop
    count = hold + scroll + 1 if visible else 0
    return loop, hold, scroll, count


def _timing(p):
    loop, hold, scroll, count = _phases(p)
    return loop, hold + scroll, count


def _validated_pass(stories):
    if type(stories) is not tuple or len(stories) > 50 or any(not isinstance(p, PreparedStory) for p in stories):
        raise ValueError('Expected an immutable pass of at most 50 prepared stories')
    return stories


def _sized(stories, size):
    stories = _validated_pass(stories)
    return stories if size is None else tuple(layout_for_size(p, size) for p in stories)


def duration_seconds(stories, *, size=None):
    """One duration source: inclusive timeline submissions and their waits."""
    return sum(_timing(p)[2] * p.options.frame_delay for p in _sized(stories, size))


def image_frames(stories, stop_event=None, *, size=None):
    for p in _sized(stories, size):
        delay = Fraction(str(p.options.frame_delay))
        end = _timing(p)[2] * delay
        elapsed, index = Fraction(0), 0
        durations = tuple(Fraction(ms, 10) / Fraction(str(p.options.thumbnail_speed))
                          for ms in p.thumbnail.durations_ms) if p.thumbnail else ()
        animated = len(durations) > 1
        next_thumbnail = durations[0] if animated else end
        while elapsed < end:
            if stop_event is not None and stop_event.is_set():
                return
            tick = int(elapsed // delay)
            if animated and elapsed == next_thumbnail:
                index = (index + 1) % len(durations)
                next_thumbnail += durations[index]
            next_time = min((tick + 1) * delay, next_thumbnail, end)
            image, info = render_frame(p, tick, thumbnail_index=index)
            info.update(elapsed_seconds=float(elapsed), thumbnail_index=index)
            yield image, float(next_time - elapsed), info
            elapsed = next_time


def preview_frames(stories, stop_event=None, *, safe_area=None):
    """Lazy PNG stream of exactly the physical timeline, without sleeping.

    With a Screen Safe Area the story is laid out on the inner canvas, exactly
    as run() does on the panel, and placed inside the safe area.
    """
    from edit_preview import _png_frame
    size = safe_area.inner_size() if safe_area is not None else None
    for image, delay, info in image_frames(stories, stop_event, size=size):
        if stop_event is not None and stop_event.is_set():
            return
        if safe_area is not None and stories:
            image = safe_area.compose(image, stories[0].options.background_colour)
        frame = _png_frame(image, delay * 1000)
        frame['rss'] = info
        if stop_event is not None and stop_event.is_set():
            return
        yield frame


@dataclass(frozen=True)
class PlaybackResult:
    frames: int
    elapsed_seconds: float


def run(matrix, stories, stop_event=None, *, clock=time.monotonic, sleep=time.sleep):
    """Play all required frames; account for draw/swap time using monotonic time.

    Never skip a title/summary frame to catch up. A slow device may take longer
    than the calculated timeline; return actual elapsed time, not a fabricated
    sum of requested waits. The caller owns selection and preparation.
    """
    from safe_area import canvas_size
    size = canvas_size(matrix)
    started = deadline = clock()
    count = 0
    for image, delay, _ in image_frames(stories, stop_event, size=size):
        if stop_event is not None and stop_event.is_set():
            break
        matrix.draw_image(image)
        if stop_event is not None and stop_event.is_set():
            break
        matrix.swap()
        count += 1
        deadline += delay
        remaining = max(0.0, deadline - clock())
        if stop_event is not None:
            if stop_event.wait(remaining) or stop_event.is_set():
                break
        else:
            sleep(remaining)
    return PlaybackResult(count, max(0.0, clock() - started))
