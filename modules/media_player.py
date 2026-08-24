"""Shared interruptible playback for configured Matrix media."""

from pathlib import Path
import math
import threading
import time

from PIL import Image, ImageSequence


def gif_media_frames(
    path, loops=1, playback_rate=1.0, stop_event=None, max_duration=None,
    monotonic=None, image_open=None, image_iterator=None, frame_copy=None,
):
    """Yield the finite GIF timeline shared by physical and preview playback."""
    image_open = image_open or Image.open
    image_iterator = image_iterator or ImageSequence.Iterator
    frame_copy = frame_copy or (lambda frame: frame.copy())
    if (
        isinstance(playback_rate, bool)
        or not isinstance(playback_rate, (int, float))
        or not math.isfinite(playback_rate)
        or not 0.25 <= playback_rate <= 4.0
    ):
        raise ValueError("GIF playback rate must be between 0.25 and 4.0")
    playback_rate = float(playback_rate)
    started = monotonic() if monotonic is not None else 0.0
    playback_started = None
    preparation_elapsed = 0.0
    elapsed_delays = 0.0
    gif = image_open(path)
    completed = 0
    try:
        while loops == 0 or completed < loops:
            rendered_frame = False
            for frame in image_iterator(gif):
                rendered_frame = True
                if stop_event is not None and stop_event.is_set():
                    return
                elapsed = elapsed_delays
                if monotonic is not None:
                    now = monotonic()
                    if playback_started is None:
                        playback_started = now
                        preparation_elapsed = max(0.0, now - started)
                    elapsed = preparation_elapsed + max(
                        elapsed_delays, now - playback_started,
                    )
                if max_duration is not None and elapsed >= max_duration:
                    return
                candidate = frame_copy(frame)
                if monotonic is not None:
                    now = monotonic()
                    elapsed = preparation_elapsed + max(
                        elapsed_delays, now - playback_started,
                    )
                if max_duration is not None and elapsed >= max_duration:
                    close = getattr(candidate, "close", None)
                    if close is not None:
                        close()
                    return
                delay = max(
                    0.01, frame.info.get("duration", 100) / 1000.0 / playback_rate
                )
                if max_duration is not None:
                    delay = min(delay, max_duration - elapsed)
                yield candidate, delay
                elapsed_delays += delay
            if not rendered_frame:
                return
            completed += 1
    finally:
        gif.close()


def video_media_frames(
    frame_decoder, fps=20, loops=1, stop_event=None, max_duration=None,
    monotonic=None,
):
    """Yield one decoded video timeline repeatedly with deadline-clipped waits."""
    fps = max(1.0, min(60.0, float(fps)))
    frame_delay = 1.0 / fps
    started = monotonic() if monotonic is not None else 0.0
    elapsed_delays = 0.0
    completed = 0
    while loops == 0 or completed < loops:
        if stop_event is not None and stop_event.is_set():
            return
        decoded_frame = False
        decoder = iter(frame_decoder())
        try:
            for frame in decoder:
                decoded_frame = True
                if stop_event is not None and stop_event.is_set():
                    return
                elapsed = elapsed_delays
                if monotonic is not None:
                    elapsed = max(elapsed, monotonic() - started)
                if max_duration is not None and elapsed >= max_duration:
                    return
                delay = frame_delay
                if max_duration is not None:
                    delay = min(delay, max_duration - elapsed)
                yield frame, delay
                elapsed_delays += delay
        finally:
            close = getattr(decoder, "close", None)
            if close is not None:
                close()
        if not decoded_frame:
            return
        completed += 1


def play_media(matrix, filename, duration, stop_event=None, settings=None):
    if stop_event is not None and stop_event.is_set():
        return False
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

    from edit_preview import static_media_frames

    frame_iterator = static_media_frames(
        filename, duration, settings,
        render=lambda frame_filename, frame_geometry: matrix.show_image(
            frame_filename, delay=None, stop_event=stop_event, **frame_geometry,
        ),
        monotonic=time.monotonic,
    )
    try:
        _, delay_ms, _ = next(frame_iterator)
    except StopIteration:
        return False
    waiter = stop_event or threading.Event()
    waiter.wait(delay_ms / 1000)
    return True
