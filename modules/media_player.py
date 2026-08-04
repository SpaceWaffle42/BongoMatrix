"""Shared interruptible playback for configured Matrix media."""

from pathlib import Path
import threading


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
        return matrix.show_gif(
            filename,
            **geometry,
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

    shown = matrix.show_image(filename, delay=None, **geometry)
    if not shown:
        return False
    waiter = stop_event or threading.Event()
    waiter.wait(max(0, float(duration)))
    return True
