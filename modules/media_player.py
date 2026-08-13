"""Shared interruptible playback for configured Matrix media."""

from pathlib import Path
import threading

from edit_preview import static_media_frames


def play_media(matrix, filename, duration, stop_event=None, settings=None):
    geometry = {}
    if settings:
        geometry = {
            "x": settings["x"],
            "y": settings["y"],
            "width": settings["width"],
            "height": settings["height"],
            "keep_aspect": settings["keep_aspect"],
        }
    suffix = Path(filename).suffix.lower()
    if suffix == ".gif":
        playback = {}
        if settings and "gif_speed_percent" in settings:
            playback["playback_rate"] = settings["gif_speed_percent"] / 100.0
        return matrix.show_gif(
            filename,
            **geometry,
            **playback,
            loops=0,
            stop_event=stop_event,
            max_duration=duration,
        )
    if suffix == ".mp4":
        return matrix.show_video(
            filename,
            **geometry,
            **({"fps_override": settings["video_fps"]} if settings else {}),
            loops=0,
            stop_event=stop_event,
            max_duration=duration,
        )

    frame_iterator = static_media_frames(filename, duration, settings)
    frame_filename, delay_ms, geometry = next(frame_iterator)
    shown = matrix.show_image(frame_filename, delay=None, **geometry)
    if not shown:
        return False
    waiter = stop_event or threading.Event()
    waiter.wait(delay_ms / 1000)
    return True
