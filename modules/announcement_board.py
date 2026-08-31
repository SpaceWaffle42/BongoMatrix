"""Scheduled Announcement Board playback for the Matrix display."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from PIL import Image

from font_catalog import text_width
from modules.frame_appearance import draw_frame_border
from modules.scroll_timing import duration_milliseconds, format_duration_seconds, scroll_frames


ANNOUNCEMENT_MEDIA_DECODE_TIMEOUT = 5


def _scheduled_interval(schedule):
    start_date = dt.date.fromisoformat(schedule["start_date"])
    end_date = dt.date.fromisoformat(schedule["end_date"])
    start_time = dt.time.fromisoformat(schedule["start_time"])
    end_time = dt.time.fromisoformat(schedule["end_time"])
    return start_date, end_date, start_time, end_time


def is_announcement_active(announcement, *, now):
    """Return whether a validated announcement is active at local ``now``."""
    schedule = announcement["schedule"]
    start_date, end_date, start_time, end_time = _scheduled_interval(schedule)
    if start_time < end_time:
        return (
            start_date <= now.date() <= end_date
            and now.weekday() in schedule.get("days", range(7))
            and start_time <= now.time() < end_time
        )

    owning_date = now.date() if now.time() >= start_time else now.date() - dt.timedelta(days=1)
    if not start_date <= owning_date <= end_date:
        return False
    if owning_date.weekday() not in schedule.get("days", range(7)):
        return False
    interval_start = dt.datetime.combine(owning_date, start_time)
    interval_end = dt.datetime.combine(owning_date + dt.timedelta(days=1), end_time)
    return interval_start <= now < interval_end


def active_announcements(announcements, *, now):
    """Filter active announcements without changing their configured order."""
    return [
        announcement
        for announcement in announcements
        if announcement.get("enabled", True)
        and is_announcement_active(announcement, now=now)
    ]


def _next_schedule_boundary(announcements, now):
    candidates = []
    for announcement in announcements:
        if not announcement.get("enabled", True):
            continue
        schedule = announcement["schedule"]
        start_date, end_date, start_time, end_time = _scheduled_interval(schedule)
        for offset in range(-1, 3):
            owning_date = now.date() + dt.timedelta(days=offset)
            if not start_date <= owning_date <= end_date:
                continue
            if owning_date.weekday() not in schedule.get("days", range(7)):
                continue
            start = dt.datetime.combine(owning_date, start_time)
            end_date_for_interval = owning_date if start_time < end_time else owning_date + dt.timedelta(days=1)
            end = dt.datetime.combine(end_date_for_interval, end_time)
            candidates.extend(candidate for candidate in (start, end) if candidate > now)
    return min(candidates) if candidates else None


def announcement_timing_plan(announcements, *, now, text_width_provider=text_width, matrix_width=64):
    """Return the fixed-clock aggregate runtime for eligible announcements."""
    eligible = active_announcements(announcements, now=now)
    item_milliseconds = []
    for announcement in eligible:
        timing = announcement["timing"]
        if timing["mode"] == "static":
            transition = announcement.get("transition", {"type": "cut"})
            transition_seconds = (
                transition.get("duration_seconds", 0)
                if transition.get("type") != "cut"
                else 0
            )
            milliseconds = (
                Decimal(str(timing["duration_seconds"]))
                + Decimal(str(transition_seconds))
            ) * 1000
            item_milliseconds.append(int(
                milliseconds.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            ))
            continue
        runs = _scroll_text_runs(announcement)
        frames = scroll_frames(
            sum(text_width_provider(font_name, text) for font_name, text in runs),
            matrix_width=matrix_width,
        )
        item_milliseconds.append(duration_milliseconds(frames, timing["loops"], timing["frame_delay"]))
    next_minute = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    boundary = _next_schedule_boundary(announcements, now)
    refresh_at = min(next_minute, boundary) if boundary else next_minute
    if not item_milliseconds:
        return {
            "status": "unavailable", "milliseconds": None, "display": "Unavailable",
            "item_milliseconds": [], "refresh_at": refresh_at,
        }
    milliseconds = sum(item_milliseconds)
    return {
        "status": "fresh", "milliseconds": milliseconds,
        "display": format_duration_seconds(milliseconds),
        "item_milliseconds": item_milliseconds, "refresh_at": refresh_at,
    }


def dissolve_pixel_order(announcement_id, width, height):
    """Return a stable ID-specific permutation of panel coordinates."""
    coordinates = [(x, y) for y in range(height) for x in range(width)]
    return sorted(
        coordinates,
        key=lambda point: (
            hashlib.sha256(
                f"{announcement_id}:{point[0]}:{point[1]}".encode("utf-8")
            ).digest(),
            point[1],
            point[0],
        ),
    )


def _aligned_text_x(
    matrix, canvas, font, colour, text, alignment, baseline, *, left=2, right_margin=2,
):
    if alignment == "left":
        return left
    if alignment not in {"center", "right"}:
        raise NotImplementedError("announcement text alignment is not implemented")
    width = matrix.draw_text(font, -10000, baseline, colour, text, canvas=canvas)
    right = matrix.matrix.width - right_margin
    if alignment == "center":
        return left + (right - left - width) // 2
    return right - width


def _draw_border(matrix, canvas, style, x_offset=0):
    draw_frame_border(
        matrix, canvas, style["border"], style["border_colour"], x_offset=x_offset,
    )


def _scroll_text_runs(announcement):
    """Return adjacent font/text runs for one scrolling announcement."""
    style = announcement["style"]
    headline = announcement["headline"]
    if not announcement.get("message"):
        return [(style["headline_font"], headline)]
    return [
        (style["headline_font"], headline + " - "),
        (style["message_font"], announcement["message"]),
    ]


def _render_text_notice(matrix, canvas, announcement, x_offset=0):
    style = announcement["style"]
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    matrix.fill(background.red, background.green, background.blue, canvas=canvas)
    headline_font = matrix.font(style["headline_font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground,
        announcement["headline"], style["alignment"], 11,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 11, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font(style["message_font"])
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground,
            announcement["message"], style["alignment"], 23,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 23, foreground,
            announcement["message"], canvas=canvas,
        )
    _draw_border(matrix, canvas, style, x_offset)


def _scroll_text_notice(matrix, canvas, announcement, stop_event):
    style = announcement["style"]
    if style["border"] != "none":
        raise NotImplementedError("scrolling announcement border is not implemented")
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    runs = [(matrix.font(font_name), text) for font_name, text in _scroll_text_runs(announcement)]
    timing = announcement["timing"]
    submitted = False
    for _ in range(timing["loops"]):
        x = matrix.matrix.width
        while True:
            if stop_event is not None and stop_event.is_set():
                return canvas, True, submitted
            matrix.fill(background.red, background.green, background.blue, canvas=canvas)
            text_width = 0
            for font, text in runs:
                text_width += matrix.draw_text(
                    font, x + text_width, 20, foreground, text, canvas=canvas,
                )
            if stop_event is not None and stop_event.is_set():
                return canvas, True, submitted
            canvas = matrix.swap(canvas)
            submitted = True
            if stop_event is not None:
                if stop_event.wait(timing["frame_delay"]):
                    return canvas, True, submitted
            else:
                time.sleep(timing["frame_delay"])
            if x <= -text_width:
                break
            x -= 1
    return canvas, False, submitted


def _render_icon_notice(
    matrix, canvas, announcement, media_brightness=1, x_offset=0, stop_event=None,
    media_frame=None,
):
    style = announcement["style"]
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    matrix.fill(background.red, background.green, background.blue, canvas=canvas)
    if media_frame is None:
        fallback_font = matrix.font("4x6")
        fallback_x = _aligned_text_x(
            matrix, canvas, fallback_font, foreground, "Media unavailable",
            style["alignment"], 18,
        ) + x_offset
        matrix.draw_text(
            fallback_font, fallback_x, 18, foreground,
            "Media unavailable", canvas=canvas,
        )
        _draw_border(matrix, canvas, style, x_offset)
        return
    image = media_frame
    if media_brightness != 1:
        image = _scaled_image(image, media_brightness)
    matrix.draw_image(image, 1 + x_offset, 1, width=14, height=14, canvas=canvas)
    headline_font = matrix.font(style["headline_font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground, announcement["headline"],
        style["alignment"], 11, left=18,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 11, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font(style["message_font"])
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground, announcement["message"],
            style["alignment"], 23, left=18,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 23, foreground,
            announcement["message"], canvas=canvas,
        )
    _draw_border(matrix, canvas, style, x_offset)


def _render_image_banner(
    matrix, canvas, announcement, media_brightness=1, x_offset=0, stop_event=None,
    media_frame=None,
):
    style = announcement["style"]
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    matrix.fill(background.red, background.green, background.blue, canvas=canvas)
    if media_frame is None:
        matrix.draw_text(
            matrix.font("4x6"), 2 + x_offset, 18, foreground,
            "Media unavailable", canvas=canvas,
        )
        _draw_border(matrix, canvas, style, x_offset)
        return
    image = media_frame
    if media_brightness != 1:
        image = _scaled_image(image, media_brightness)
    matrix.draw_image(
        image, x_offset, 0,
        width=matrix.matrix.width, height=matrix.matrix.height // 2,
        keep_aspect=False, canvas=canvas,
    )
    headline_font = matrix.font(style["headline_font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground, announcement["headline"],
        style["alignment"], 24,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 24, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font(style["message_font"])
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground, announcement["message"],
            style["alignment"], 31,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 31, foreground,
            announcement["message"], canvas=canvas,
        )
    _draw_border(matrix, canvas, style, x_offset)


def _render_full_image(
    matrix, canvas, announcement, media_brightness=1, x_offset=0, stop_event=None,
    media_frame=None,
):
    style = announcement["style"]
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    matrix.fill(background.red, background.green, background.blue, canvas=canvas)
    if media_frame is None:
        matrix.draw_text(
            matrix.font("4x6"), 2 + x_offset, 18, foreground,
            "Media unavailable", canvas=canvas,
        )
        _draw_border(matrix, canvas, style, x_offset)
        return
    image = media_frame
    if media_brightness != 1:
        image = _scaled_image(image, media_brightness)
    matrix.draw_image(
        image, x_offset, 0,
        width=matrix.matrix.width, height=matrix.matrix.height,
        keep_aspect=False, canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font(style["message_font"])
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground, announcement["message"],
            style["alignment"], 21,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 21, foreground,
            announcement["message"], canvas=canvas,
        )
    headline_font = matrix.font(style["headline_font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground, announcement["headline"],
        style["alignment"], 30,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 30, foreground,
        announcement["headline"], canvas=canvas,
    )
    _draw_border(matrix, canvas, style, x_offset)


def _render_static_layout(
    matrix, canvas, announcement, media_brightness=1, x_offset=0, stop_event=None,
    media_frame=None,
):
    if announcement["layout"] == "text_notice":
        _render_text_notice(matrix, canvas, announcement, x_offset=x_offset)
    elif announcement["layout"] == "icon_notice":
        _render_icon_notice(
            matrix, canvas, announcement, media_brightness, x_offset, stop_event,
            media_frame,
        )
    elif announcement["layout"] == "image_banner":
        _render_image_banner(
            matrix, canvas, announcement, media_brightness, x_offset, stop_event,
            media_frame,
        )
    elif announcement["layout"] == "full_image_notice":
        _render_full_image(
            matrix, canvas, announcement, media_brightness, x_offset, stop_event,
            media_frame,
        )
    else:
        raise NotImplementedError("announcement layout is not implemented")


def _scaled_colour(value, factor):
    channels = [round(int(value[index:index + 2], 16) * factor) for index in (1, 3, 5)]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _scaled_image(image, factor):
    rgb = image.convert("RGB").point(lambda channel: round(channel * factor))
    if "A" not in image.getbands():
        return rgb
    red, green, blue = rgb.split()
    return Image.merge("RGBA", (red, green, blue, image.getchannel("A")))


def _render_fade(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    submitted = False
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        factor = step / 4
        faded = {
            **announcement,
            "style": {
                **announcement["style"],
                "foreground": _scaled_colour(
                    announcement["style"]["foreground"], factor,
                ),
                "background": _scaled_colour(
                    announcement["style"]["background"], factor,
                ),
                "border_colour": _scaled_colour(
                    announcement["style"]["border_colour"], factor,
                ),
            },
        }
        _render_static_layout(
            matrix, canvas, faded, media_brightness=factor, stop_event=stop_event,
            media_frame=media_frame,
        )
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        canvas = matrix.swap(canvas)
        submitted = True
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True, submitted
        else:
            time.sleep(delay)
    return canvas, False, submitted


def static_announcement_frames(matrix, announcement, stop_event=None):
    """Yield rendered fade frames and one final static hold frame for text notices."""
    transition_delay_ms = announcement["transition"]["duration_seconds"] * 1000 / 4
    canvas = None
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return
        factor = step / 4
        faded = {
            **announcement,
            "style": {
                **announcement["style"],
                "foreground": _scaled_colour(announcement["style"]["foreground"], factor),
                "background": _scaled_colour(announcement["style"]["background"], factor),
                "border_colour": _scaled_colour(
                    announcement["style"]["border_colour"], factor,
                ),
            },
        }
        canvas = matrix.matrix.CreateFrameCanvas()
        _render_static_layout(matrix, canvas, faded, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            return
        yield canvas, transition_delay_ms

    # The completed transition canvas is submitted once more as the sole hold
    # frame; its hold delay represents the configured static duration.
    yield canvas, announcement["timing"]["duration_seconds"] * 1000


def _render_static_announcement_frames(matrix, announcement, stop_event):
    submitted = False
    canvas = None
    for canvas, delay_ms in static_announcement_frames(matrix, announcement, stop_event):
        canvas = matrix.swap(canvas)
        submitted = True
        delay = delay_ms / 1000
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True, submitted
        else:
            time.sleep(delay)
    interrupted = stop_event is not None and stop_event.is_set()
    return canvas, interrupted, submitted


def _static_transition_frames(matrix, announcement, stop_event, media_frame):
    """Yield the four existing transition draws without submitting or waiting."""
    transition = announcement["transition"]
    transition_type = transition["type"]
    delay_ms = transition["duration_seconds"] * 1000 / 4
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return
        canvas = matrix.matrix.CreateFrameCanvas()
        if transition_type == "fade":
            factor = step / 4
            faded = {
                **announcement,
                "style": {
                    **announcement["style"],
                    "foreground": _scaled_colour(
                        announcement["style"]["foreground"], factor,
                    ),
                    "background": _scaled_colour(
                        announcement["style"]["background"], factor,
                    ),
                    "border_colour": _scaled_colour(
                        announcement["style"]["border_colour"], factor,
                    ),
                },
            }
            _render_static_layout(
                matrix, canvas, faded, media_brightness=factor,
                stop_event=stop_event, media_frame=media_frame,
            )
        elif transition_type == "slide":
            remaining = 4 - step
            offset = (matrix.matrix.width * remaining) // 4
            if transition["direction"] == "right":
                offset = -offset
            _render_static_layout(
                matrix, canvas, announcement, x_offset=offset,
                stop_event=stop_event, media_frame=media_frame,
            )
        elif transition_type == "wipe":
            _render_static_layout(
                matrix, canvas, announcement, stop_event=stop_event,
                media_frame=media_frame,
            )
            width = matrix.matrix.width
            reveal_width = (width * step) // 4
            hidden_columns = (
                range(reveal_width, width)
                if transition["direction"] == "left"
                else range(0, width - reveal_width)
            )
            background = matrix.colour_rgb(announcement["style"]["background"])
            for x in hidden_columns:
                matrix.draw_line(
                    x, 0, x, matrix.matrix.height - 1, background, canvas=canvas,
                )
        else:
            _render_static_layout(
                matrix, canvas, announcement, stop_event=stop_event,
                media_frame=media_frame,
            )
            order = dissolve_pixel_order(
                announcement["id"], matrix.matrix.width, matrix.matrix.height,
            )
            revealed = (len(order) * step) // 4
            background = matrix.colour_rgb(announcement["style"]["background"])
            for x, y in order[revealed:]:
                matrix.pixel(
                    x, y, background.red, background.green, background.blue,
                    canvas=canvas,
                )
        if stop_event is not None and stop_event.is_set():
            return
        yield canvas, delay_ms


def announcement_frames(
    matrix, announcements, duration=None, stop_event=None, eligibility=None,
    monotonic=None, *, pinned_handles=None,
):
    """Yield one physical announcement invocation as canvases and delays.

    A positive ``duration`` is an invocation deadline.  The final delay is
    clipped after rendering, so drawing time and waits cannot overrun it.
    """
    monotonic = monotonic or time.monotonic
    deadline = None
    if duration is not None and float(duration) > 0:
        deadline = monotonic() + float(duration)

    def clipped(delay_ms):
        if deadline is None:
            return delay_ms
        return min(delay_ms, max(0.0, (deadline - monotonic()) * 1000))

    for announcement in announcements:
        if stop_event is not None and stop_event.is_set():
            return
        if eligibility is not None and not eligibility(announcement):
            continue
        media_frame = None
        media_frames = []
        animated = False
        media_fps = 20
        if announcement["layout"] != "text_notice":
            try:
                hold_seconds = announcement["timing"].get("duration_seconds", 1)
                animated = Path(announcement["media"] or "").suffix.lower() in {".gif", ".mp4"}
                media_options = {
                    "stop_event": stop_event,
                    "preserve_size": True,
                    "decode_timeout": ANNOUNCEMENT_MEDIA_DECODE_TIMEOUT,
                }
                if animated:
                    media_options["playback_rate"] = (
                        announcement.get("playback_speed_percent", 100.0) / 100.0
                    )
                pinned_handle = (pinned_handles or {}).get(announcement["media"])
                if pinned_handle is not None:
                    media_options["pinned_handle"] = pinned_handle
                frames = matrix.media_frames(
                    announcement["media"],
                    min(120, max(1, math.ceil(hold_seconds * media_fps))) if animated else 1,
                    media_fps if animated else 1,
                    **media_options,
                )
            except (OSError, ValueError):
                frames = []
            if stop_event is not None and stop_event.is_set():
                return
            media_frames = frames
            media_frame = frames[0] if frames else None

        if announcement["timing"]["mode"] == "scroll":
            style = announcement["style"]
            foreground = matrix.colour_rgb(style["foreground"])
            background = matrix.colour_rgb(style["background"])
            runs = [
                (font_name, matrix.font(font_name), text)
                for font_name, text in _scroll_text_runs(announcement)
            ]
            width = None
            for _ in range(announcement["timing"]["loops"]):
                x = matrix.matrix.width
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return
                    canvas = matrix.matrix.CreateFrameCanvas()
                    matrix.fill(
                        background.red, background.green, background.blue,
                        canvas=canvas,
                    )
                    drawn_width = 0
                    measured_width = 0
                    for font_name, font, text in runs:
                        run_width = matrix.draw_text(
                            font, x + drawn_width, 20, foreground, text, canvas=canvas,
                        )
                        drawn_width += run_width
                        measured_width += (
                            text_width(font_name, text)
                            if hasattr(font, "text_pixels")
                            else run_width
                        )
                    if width is None:
                        width = measured_width
                    if stop_event is not None and stop_event.is_set():
                        return
                    delay_ms = clipped(announcement["timing"]["frame_delay"] * 1000)
                    if delay_ms <= 0:
                        return
                    yield canvas, delay_ms
                    if x <= -width:
                        break
                    x -= 1
            continue

        transition_type = announcement["transition"]["type"]
        if transition_type == "cut":
            canvas = matrix.matrix.CreateFrameCanvas()
            _render_static_layout(
                matrix, canvas, announcement, stop_event=stop_event,
                media_frame=media_frame,
            )
        else:
            canvas = None
            for canvas, delay_ms in _static_transition_frames(
                matrix, announcement, stop_event, media_frame,
            ):
                delay_ms = clipped(delay_ms)
                if delay_ms <= 0:
                    return
                yield canvas, delay_ms
            if canvas is None:
                return
        if stop_event is not None and stop_event.is_set():
            return
        hold_ms = clipped(announcement["timing"]["duration_seconds"] * 1000)
        if hold_ms <= 0:
            return
        if not animated or not media_frames:
            yield canvas, hold_ms
            continue
        remaining_ms = hold_ms
        index = 0
        while remaining_ms > 0:
            media_frame = media_frames[index % len(media_frames)]
            if index:
                canvas = matrix.matrix.CreateFrameCanvas()
                _render_static_layout(
                    matrix, canvas, announcement, stop_event=stop_event,
                    media_frame=media_frame,
                )
            if stop_event is not None and stop_event.is_set():
                return
            frame_delay_ms = min(1000 / media_fps, remaining_ms)
            yield canvas, frame_delay_ms
            remaining_ms -= frame_delay_ms
            index += 1


def _render_slide(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    direction = announcement["transition"]["direction"]
    submitted = False
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        remaining = 4 - step
        offset = (matrix.matrix.width * remaining) // 4
        if direction == "right":
            offset = -offset
        _render_static_layout(
            matrix, canvas, announcement, x_offset=offset, stop_event=stop_event,
            media_frame=media_frame,
        )
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        canvas = matrix.swap(canvas)
        submitted = True
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True, submitted
        else:
            time.sleep(delay)
    return canvas, False, submitted


def _render_wipe(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    direction = announcement["transition"]["direction"]
    background = matrix.colour_rgb(announcement["style"]["background"])
    width = matrix.matrix.width
    bottom = matrix.matrix.height - 1
    submitted = False
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        _render_static_layout(
            matrix, canvas, announcement, stop_event=stop_event,
            media_frame=media_frame,
        )
        reveal_width = (width * step) // 4
        hidden_columns = (
            range(reveal_width, width)
            if direction == "left"
            else range(0, width - reveal_width)
        )
        for x in hidden_columns:
            matrix.draw_line(x, 0, x, bottom, background, canvas=canvas)
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        canvas = matrix.swap(canvas)
        submitted = True
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True, submitted
        else:
            time.sleep(delay)
    return canvas, False, submitted


def _render_pixel_dissolve(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    background = matrix.colour_rgb(announcement["style"]["background"])
    order = dissolve_pixel_order(
        announcement["id"], matrix.matrix.width, matrix.matrix.height,
    )
    submitted = False
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        _render_static_layout(
            matrix, canvas, announcement, stop_event=stop_event,
            media_frame=media_frame,
        )
        revealed = (len(order) * step) // 4
        for x, y in order[revealed:]:
            matrix.pixel(
                x, y, background.red, background.green, background.blue,
                canvas=canvas,
            )
        if stop_event is not None and stop_event.is_set():
            return canvas, True, submitted
        canvas = matrix.swap(canvas)
        submitted = True
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True, submitted
        else:
            time.sleep(delay)
    return canvas, False, submitted


def _run_announcements(
    matrix, announcements, duration=None, stop_event=None, eligibility=None,
    *, pinned_handles=None,
):
    if stop_event is not None and stop_event.is_set():
        return False
    rendered = False
    for canvas, delay_ms in announcement_frames(
        matrix, announcements, duration, stop_event, eligibility,
        pinned_handles=pinned_handles,
    ):
        if stop_event is not None and stop_event.is_set():
            break
        matrix.swap(canvas)
        rendered = True
        seconds = delay_ms / 1000
        if stop_event is not None:
            if stop_event.wait(seconds):
                break
        else:
            time.sleep(seconds)
    return rendered


def run_preview(matrix, announcement, stop_event=None, *, pinned_handles=None):
    """Render one validated draft regardless of enabled or schedule state."""
    return _run_announcements(
        matrix, [announcement], stop_event=stop_event,
        pinned_handles=pinned_handles,
    )


def run(
    matrix, config, duration, stop_event=None, now_provider=dt.datetime.now,
    *, pinned_handles=None,
):
    """Render each eligible announcement once in configured order, rechecking at start."""
    announcements = config["announcement_board"]["announcements"]
    return _run_announcements(
        matrix,
        announcements,
        duration=duration,
        stop_event=stop_event,
        pinned_handles=pinned_handles,
        eligibility=lambda announcement: (
            announcement.get("enabled", True)
            and is_announcement_active(announcement, now=now_provider())
        ),
    )
