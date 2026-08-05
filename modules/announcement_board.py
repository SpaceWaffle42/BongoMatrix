"""Scheduled Announcement Board playback for the Matrix display."""

from __future__ import annotations

import datetime as dt


def _scheduled_interval(schedule):
    start_date = dt.date.fromisoformat(schedule["start_date"])
    end_date = dt.date.fromisoformat(schedule["end_date"])
    start_time = dt.time.fromisoformat(schedule["start_time"])
    end_time = dt.time.fromisoformat(schedule["end_time"])
    return start_date, end_date, start_time, end_time


def is_announcement_active(announcement, *, now):
    """Return whether a validated announcement is active at local ``now``."""
    start_date, end_date, start_time, end_time = _scheduled_interval(
        announcement["schedule"]
    )
    if start_time < end_time:
        return start_date <= now.date() <= end_date and start_time <= now.time() < end_time

    owning_date = now.date() if now.time() >= start_time else now.date() - dt.timedelta(days=1)
    if not start_date <= owning_date <= end_date:
        return False
    interval_start = dt.datetime.combine(owning_date, start_time)
    interval_end = dt.datetime.combine(owning_date + dt.timedelta(days=1), end_time)
    return interval_start <= now < interval_end


def active_announcements(announcements, *, now):
    """Filter active announcements without changing their configured order."""
    return [
        announcement
        for announcement in announcements
        if is_announcement_active(announcement, now=now)
    ]


def _render_text_notice(matrix, canvas, announcement):
    style = announcement["style"]
    if style["alignment"] != "left" or style["border"] != "none":
        raise NotImplementedError("announcement alignment and border are not implemented")
    background = matrix.colour_rgb(style["background"])
    foreground = matrix.colour_rgb(style["foreground"])
    matrix.fill(background.red, background.green, background.blue, canvas=canvas)
    matrix.draw_text(
        matrix.font(style["font"]), 2, 11, foreground,
        announcement["headline"], canvas=canvas,
    )
    if announcement["message"]:
        matrix.draw_text(
            matrix.font("4x6"), 2, 23, foreground,
            announcement["message"], canvas=canvas,
        )


def run(matrix, config, duration, stop_event=None, now_provider=dt.datetime.now):
    """Render each active announcement once in its configured order."""
    del duration
    if stop_event is not None and stop_event.is_set():
        return False
    active = active_announcements(
        config["announcement_board"]["announcements"],
        now=now_provider(),
    )
    if not active:
        return False
    canvas = matrix.matrix.CreateFrameCanvas()
    for announcement in active:
        if stop_event is not None and stop_event.is_set():
            break
        if announcement["layout"] != "text_notice":
            raise NotImplementedError("announcement layout is not implemented")
        _render_text_notice(matrix, canvas, announcement)
        canvas = matrix.swap(canvas)
        seconds = announcement["timing"]["duration_seconds"]
        if stop_event is not None:
            if stop_event.wait(seconds):
                break
        else:
            import time
            time.sleep(seconds)
    return True
