"""Prepared-only RSS pixels. No feed access, media decoding, or host registration.

Preparation loads installed BDFs outside playback; immutable glyph runs, not
unbounded text bitmaps, cross the render boundary. Coordinates use ink tops.
"""
from dataclasses import dataclass
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


def _wrap(font, text):
    # Word wrapping with hard breaks for overlong tokens, bounded by 2000 chars.
    lines, current = [], ''
    for word in text.split():
        candidate = (current + ' ' + word).strip()
        if _measure(font, candidate).width <= 64:
            current = candidate
            continue
        if current:
            lines.append(_measure(font, current))
            current = ''
        for character in word:
            if current and _measure(font, current + character).width > 64:
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
class PreparedStory:
    headline: TextRun
    lines: tuple[TextRun, ...]
    line_pitch: int
    options: RenderOptions
    warnings: object
    thumbnail: PreparedThumbnail | None = None


def prepare_story(story, options=None, *, thumbnail=None):
    options = options or RenderOptions()
    if thumbnail is not None and not isinstance(thumbnail, PreparedThumbnail):
        raise ValueError('Thumbnail must be prepared before rendering')
    if options.layout == 'text':
        thumbnail = None
    if not isinstance(story.title, str) or len(story.title) > 300 or not isinstance(story.summary, str) or len(story.summary) > 2000:
        raise ValueError('Prepared story exceeds normalized text bounds')
    headline = _measure(_font(options.headline_font), ' '.join(story.title.split()))
    summary_font = _font(options.summary_font)
    lines = _wrap(summary_font, story.summary)
    pitch = max(1, max(g.height for g in summary_font.glyphs.values()))
    hh, sh = (16, 15) if thumbnail else (7, 24)
    warnings = MappingProxyType({'headline_clipped': headline.height > hh,
                                'headline_height': headline.height, 'headline_available_height': hh,
                                'summary_clipped': any(line.width > 64 or line.height > sh for line in lines),
                                'summary_max_height': max((line.height for line in lines), default=0),
                                'summary_available_height': sh})
    return PreparedStory(headline, lines, pitch, options, warnings, thumbnail)


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


def render_frame(prepared, tick, *, thumbnail_index=0):
    background = prepared.options.background_colour
    image = Image.new('RGB', (64, 32), background)
    tx, hh, sh = (17, 16, 15) if prepared.thumbnail else (0, 7, 24)
    if prepared.thumbnail:
        image.paste(Image.frombytes('RGB', (16, 16), prepared.thumbnail.frames[thumbnail_index]), (0, 0))
    title = Image.new('RGB', (64 - tx, hh), background)
    title_end, summary_end, _ = _timing(prepared)
    x = 0
    if title_end and (tick < title_end or prepared.options.headline_mode == 'continuous'):
        x = -(tick % title_end)
    _draw(title, prepared.headline, x, 0, tuple(bytes.fromhex(prepared.options.headline_colour[1:])))
    image.paste(title, (tx, 0))
    summary = Image.new('RGB', (64, sh), background)
    sy = sh - tick
    for index, line in enumerate(prepared.lines):
        _draw(summary, line, 0, sy + index * prepared.line_pitch, tuple(bytes.fromhex(prepared.options.summary_colour[1:])))
    image.paste(summary, (0, hh + 1))
    return image, {'summary_y': sy, 'headline_x': x, 'summary_done': tick >= summary_end}


def _timing(p):
    width, height = (47, 15) if p.thumbnail else (64, 24)
    title_end = p.headline.width + 1 if p.headline.width > width else 0
    summary_end = height + (len(p.lines) - 1) * p.line_pitch + p.lines[-1].height if p.lines else 0
    count = max(title_end, summary_end) + 1 if p.headline.height or p.lines or p.thumbnail else 0
    return title_end, summary_end, count


def _validated_pass(stories):
    if type(stories) is not tuple or len(stories) > 50 or any(not isinstance(p, PreparedStory) for p in stories):
        raise ValueError('Expected an immutable pass of at most 50 prepared stories')
    return stories


def duration_seconds(stories):
    """One duration source: inclusive timeline submissions and their waits."""
    return sum(_timing(p)[2] * p.options.frame_delay for p in _validated_pass(stories))


def image_frames(stories, stop_event=None):
    for p in _validated_pass(stories):
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


def preview_frames(stories, stop_event=None):
    """Lazy PNG stream of exactly the physical timeline, without sleeping."""
    from edit_preview import _png_frame
    for image, delay, info in image_frames(stories, stop_event):
        if stop_event is not None and stop_event.is_set():
            return
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
    started = deadline = clock()
    count = 0
    for image, delay, _ in image_frames(stories, stop_event):
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
