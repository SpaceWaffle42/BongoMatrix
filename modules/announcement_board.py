"""Scheduled Announcement Board playback for the Matrix display."""

from __future__ import annotations

import datetime as dt
import hashlib
import time

from PIL import Image


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


def _draw_border(matrix, canvas, style):
    if style["border"] == "none":
        return
    if style["border"] not in {"solid", "inset"}:
        raise NotImplementedError("announcement border is not implemented")
    colour = matrix.colour_rgb(style["border_colour"])
    offset = 1 if style["border"] == "inset" else 0
    right = matrix.matrix.width - 1 - offset
    bottom = matrix.matrix.height - 1 - offset
    for x1, y1, x2, y2 in (
        (offset, offset, right, offset),
        (right, offset, right, bottom),
        (right, bottom, offset, bottom),
        (offset, bottom, offset, offset),
    ):
        matrix.draw_line(x1, y1, x2, y2, colour, canvas=canvas)


def _render_text_notice(matrix, canvas, announcement, x_offset=0):
    style = announcement["style"]
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    matrix.fill(background.red, background.green, background.blue, canvas=canvas)
    headline_font = matrix.font(style["font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground,
        announcement["headline"], style["alignment"], 11,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 11, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font("4x6")
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground,
            announcement["message"], style["alignment"], 23,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 23, foreground,
            announcement["message"], canvas=canvas,
        )
    _draw_border(matrix, canvas, style)


def _scroll_text_notice(matrix, canvas, announcement, stop_event):
    style = announcement["style"]
    if style["border"] != "none":
        raise NotImplementedError("scrolling announcement border is not implemented")
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    font = matrix.font(style["font"])
    text = announcement["headline"]
    if announcement["message"]:
        text += " - " + announcement["message"]
    timing = announcement["timing"]
    for _ in range(timing["loops"]):
        x = matrix.matrix.width
        while True:
            if stop_event is not None and stop_event.is_set():
                return canvas, True
            matrix.fill(background.red, background.green, background.blue, canvas=canvas)
            text_width = matrix.draw_text(font, x, 20, foreground, text, canvas=canvas)
            canvas = matrix.swap(canvas)
            if stop_event is not None:
                if stop_event.wait(timing["frame_delay"]):
                    return canvas, True
            else:
                time.sleep(timing["frame_delay"])
            if x <= -text_width:
                break
            x -= 1
    return canvas, False


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
        _draw_border(matrix, canvas, style)
        return
    image = media_frame
    if media_brightness != 1:
        image = _scaled_image(image, media_brightness)
    matrix.draw_image(image, 1 + x_offset, 1, width=14, height=14, canvas=canvas)
    headline_font = matrix.font(style["font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground, announcement["headline"],
        style["alignment"], 11, left=18,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 11, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font("4x6")
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground, announcement["message"],
            style["alignment"], 23, left=18,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 23, foreground,
            announcement["message"], canvas=canvas,
        )
    _draw_border(matrix, canvas, style)


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
            matrix.font("4x6"), 2, 18, foreground,
            "Media unavailable", canvas=canvas,
        )
        _draw_border(matrix, canvas, style)
        return
    image = media_frame
    if media_brightness != 1:
        image = _scaled_image(image, media_brightness)
    matrix.draw_image(
        image, x_offset, 0,
        width=matrix.matrix.width, height=matrix.matrix.height // 2,
        keep_aspect=False, canvas=canvas,
    )
    headline_font = matrix.font(style["font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground, announcement["headline"],
        style["alignment"], 24,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 24, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        message_font = matrix.font("4x6")
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground, announcement["message"],
            style["alignment"], 31,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 31, foreground,
            announcement["message"], canvas=canvas,
        )
    _draw_border(matrix, canvas, style)


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
            matrix.font("4x6"), 2, 18, foreground,
            "Media unavailable", canvas=canvas,
        )
        _draw_border(matrix, canvas, style)
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
        message_font = matrix.font("4x6")
        message_x = _aligned_text_x(
            matrix, canvas, message_font, foreground, announcement["message"],
            style["alignment"], 21,
        ) + x_offset
        matrix.draw_text(
            message_font, message_x, 21, foreground,
            announcement["message"], canvas=canvas,
        )
    headline_font = matrix.font(style["font"])
    headline_x = _aligned_text_x(
        matrix, canvas, headline_font, foreground, announcement["headline"],
        style["alignment"], 30,
    ) + x_offset
    matrix.draw_text(
        headline_font, headline_x, 30, foreground,
        announcement["headline"], canvas=canvas,
    )
    _draw_border(matrix, canvas, style)


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
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True
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
        canvas = matrix.swap(canvas)
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True
        else:
            time.sleep(delay)
    return canvas, False


def _render_slide(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    direction = announcement["transition"]["direction"]
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True
        remaining = 4 - step
        offset = (matrix.matrix.width * remaining) // 4
        if direction == "right":
            offset = -offset
        _render_static_layout(
            matrix, canvas, announcement, x_offset=offset, stop_event=stop_event,
            media_frame=media_frame,
        )
        canvas = matrix.swap(canvas)
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True
        else:
            time.sleep(delay)
    return canvas, False


def _render_wipe(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    direction = announcement["transition"]["direction"]
    background = matrix.colour_rgb(announcement["style"]["background"])
    width = matrix.matrix.width
    bottom = matrix.matrix.height - 1
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True
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
        canvas = matrix.swap(canvas)
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True
        else:
            time.sleep(delay)
    return canvas, False


def _render_pixel_dissolve(matrix, canvas, announcement, stop_event, media_frame=None):
    delay = announcement["transition"]["duration_seconds"] / 4
    background = matrix.colour_rgb(announcement["style"]["background"])
    order = dissolve_pixel_order(
        announcement["id"], matrix.matrix.width, matrix.matrix.height,
    )
    for step in range(1, 5):
        if stop_event is not None and stop_event.is_set():
            return canvas, True
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
        canvas = matrix.swap(canvas)
        if stop_event is not None:
            if stop_event.wait(delay):
                return canvas, True
        else:
            time.sleep(delay)
    return canvas, False


def _run_announcements(matrix, announcements, stop_event=None, eligibility=None):
    if stop_event is not None and stop_event.is_set():
        return False
    canvas = None
    rendered = False
    for announcement in announcements:
        if stop_event is not None and stop_event.is_set():
            break
        if eligibility is not None and not eligibility(announcement):
            continue
        if canvas is None:
            canvas = matrix.matrix.CreateFrameCanvas()
        rendered = True
        media_frame = None
        if announcement["layout"] != "text_notice":
            frames = matrix.media_frames(
                announcement["media"], 1, 1, stop_event=stop_event,
            )
            media_frame = frames[0] if frames else None
        if announcement["timing"]["mode"] == "scroll":
            if announcement["transition"]["type"] != "cut":
                raise NotImplementedError("scrolling announcement transition is not implemented")
            if announcement["layout"] != "text_notice":
                raise NotImplementedError("scrolling announcement layout is not implemented")
            canvas, interrupted = _scroll_text_notice(
                matrix, canvas, announcement, stop_event,
            )
            if interrupted:
                break
        else:
            if announcement["transition"]["type"] == "fade":
                canvas, interrupted = _render_fade(
                    matrix, canvas, announcement, stop_event, media_frame,
                )
                if interrupted:
                    break
            elif announcement["transition"]["type"] == "slide":
                canvas, interrupted = _render_slide(
                    matrix, canvas, announcement, stop_event, media_frame,
                )
                if interrupted:
                    break
            elif announcement["transition"]["type"] == "wipe":
                canvas, interrupted = _render_wipe(
                    matrix, canvas, announcement, stop_event, media_frame,
                )
                if interrupted:
                    break
            elif announcement["transition"]["type"] == "pixel_dissolve":
                canvas, interrupted = _render_pixel_dissolve(
                    matrix, canvas, announcement, stop_event, media_frame,
                )
                if interrupted:
                    break
            else:
                _render_static_layout(
                    matrix, canvas, announcement, stop_event=stop_event,
                    media_frame=media_frame,
                )
                canvas = matrix.swap(canvas)
            seconds = announcement["timing"]["duration_seconds"]
            if stop_event is not None:
                if stop_event.wait(seconds):
                    break
            else:
                time.sleep(seconds)
    return rendered


def run_preview(matrix, announcement, stop_event=None):
    """Render one validated draft regardless of enabled or schedule state."""
    return _run_announcements(matrix, [announcement], stop_event=stop_event)


def run(matrix, config, duration, stop_event=None, now_provider=dt.datetime.now):
    """Render each eligible announcement once in configured order, rechecking at start."""
    del duration
    announcements = config["announcement_board"]["announcements"]
    return _run_announcements(
        matrix,
        announcements,
        stop_event=stop_event,
        eligibility=lambda announcement: (
            announcement.get("enabled", True)
            and is_announcement_active(announcement, now=now_provider())
        ),
    )
