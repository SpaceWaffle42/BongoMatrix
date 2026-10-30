"""Shared interruptible playback for configured Matrix media."""

from pathlib import Path
import math
import sys
import threading
import time

from PIL import Image, ImageSequence


_PILLOW_WORK_SLOTS = threading.BoundedSemaphore(4)


class PillowWorkCancelled(Exception):
    """Internal control flow for cancelled or expired Pillow work."""


def _close_media_value(value):
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _close_media_preserving_active_error(value, label):
    active_error = sys.exception()
    try:
        _close_media_value(value)
    except BaseException:
        if active_error is None:
            raise
        try:
            active_error.add_note(f"suppressed {label} cleanup failure")
        except BaseException:
            pass


def _interruptible_pillow(
    work, stop_event, deadline, monotonic, *, abandoned_owner=None,
    close_result=_close_media_value,
):
    """Transfer a bounded Pillow result or abandon it for worker cleanup."""
    def cancelled():
        return ((stop_event is not None and stop_event.is_set()) or
                (deadline is not None and monotonic() >= deadline))

    while not _PILLOW_WORK_SLOTS.acquire(timeout=0.01):
        if cancelled():
            _close_media_value(abandoned_owner)
            raise PillowWorkCancelled()
    if cancelled():
        _PILLOW_WORK_SLOTS.release()
        _close_media_value(abandoned_owner)
        raise PillowWorkCancelled()

    done = threading.Event()
    ownership_lock = threading.Lock()
    state = {"abandoned": False, "outcome": None}

    def cleanup(result, owner):
        try:
            if result is not None:
                close_result(result)
        finally:
            if owner is not None:
                _close_media_preserving_active_error(owner, "Pillow owner")

    def worker():
        result_to_close = None
        owner_to_close = None
        try:
            try:
                outcome = (True, work())
            except BaseException as error:
                outcome = (False, error)
            with ownership_lock:
                if state["abandoned"]:
                    if outcome[0]:
                        result_to_close = outcome[1]
                    owner_to_close = abandoned_owner
                else:
                    state["outcome"] = outcome
            cleanup(result_to_close, owner_to_close)
        finally:
            done.set()
            _PILLOW_WORK_SLOTS.release()

    thread = threading.Thread(
        target=worker, name="matrix-pillow", daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        _PILLOW_WORK_SLOTS.release()
        raise

    def abandon():
        result_to_close = None
        owner_to_close = None
        with ownership_lock:
            state["abandoned"] = True
            outcome = state["outcome"]
            state["outcome"] = None
            if outcome is not None:
                owner_to_close = abandoned_owner
                if outcome[0]:
                    result_to_close = outcome[1]
        cleanup(result_to_close, owner_to_close)
        raise PillowWorkCancelled()

    while not done.wait(0.01):
        if cancelled():
            abandon()
    if cancelled():
        abandon()
    with ownership_lock:
        outcome = state["outcome"]
        state["outcome"] = None
    succeeded, value = outcome
    if not succeeded:
        raise value
    return value


class _GifSource:
    def __init__(self, gif):
        self.gif = gif
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            self.gif.close()


def _repair_gif_background_disposal(gif):
    """Resolve opaque disposal-2 background colours through the global palette."""
    if (
        getattr(gif, "disposal_method", 0) != 2
        or getattr(gif, "dispose", None) is None
        or getattr(gif, "dispose_extent", None) is None
        or gif.info.get("transparency") is not None
        or getattr(gif, "_frame_transparency", None) is not None
        or gif.mode not in {"RGB", "RGBA"}
    ):
        return
    palette = getattr(gif, "global_palette", None)
    palette_bytes = getattr(palette, "palette", None)
    background = gif.info.get("background", 0)
    offset = background * 3
    if palette_bytes is None or offset + 3 > len(palette_bytes):
        return
    colour = tuple(palette_bytes[offset:offset + 3])
    if gif.mode == "RGBA":
        colour += (255,)
    x0, y0, x1, y1 = gif.dispose_extent
    gif.dispose = Image.new(gif.mode, (x1 - x0, y1 - y0), colour).im


def gif_composited_frames(gif, image_iterator=None):
    """Iterate GIF frames with logical-background disposal composited correctly."""
    iterator = iter((image_iterator or ImageSequence.Iterator)(gif))
    for frame in iterator:
        _repair_gif_background_disposal(gif)
        yield frame


def gif_media_frames(
    path, loops=1, playback_rate=1.0, stop_event=None, max_duration=None,
    monotonic=None, image_open=None, image_iterator=None, frame_copy=None,
    *, bounded_pillow=True, completion=None,
):
    """Yield the finite GIF timeline shared by physical and preview playback."""
    image_open = image_open or Image.open
    image_iterator = image_iterator or gif_composited_frames
    frame_copy = frame_copy or (lambda frame: frame.copy())
    if (
        isinstance(playback_rate, bool)
        or not isinstance(playback_rate, (int, float))
        or not math.isfinite(playback_rate)
        or not 0.1 <= playback_rate <= 4.0
    ):
        raise ValueError("GIF playback rate must be between 0.1 and 4.0")
    playback_rate = float(playback_rate)
    started = monotonic() if monotonic is not None else 0.0
    deadline = (
        started + max_duration
        if monotonic is not None and max_duration is not None else None
    )
    playback_started = None
    preparation_elapsed = 0.0
    elapsed_delays = 0.0
    if completion is not None:
        completion["complete"] = False

    def cancelled():
        return ((stop_event is not None and stop_event.is_set()) or
                (deadline is not None and monotonic() >= deadline))

    def run_pillow(work, *, owner=None, close_result=_close_media_value):
        if bounded_pillow:
            return _interruptible_pillow(
                work, stop_event, deadline, monotonic or time.monotonic,
                abandoned_owner=owner, close_result=close_result,
            )
        if cancelled():
            _close_media_value(owner)
            raise PillowWorkCancelled()
        result = work()
        if cancelled():
            try:
                close_result(result)
            finally:
                _close_media_preserving_active_error(owner, "Pillow owner")
            raise PillowWorkCancelled()
        return result

    source = None
    try:
        source = run_pillow(lambda: _GifSource(image_open(path)))
        completed = 0
        while loops == 0 or completed < loops:
            rendered_frame = False
            iterator = None
            while True:
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

                owned_source = source
                frame_state = {"frame": None}

                def copy_next_frame():
                    nonlocal iterator
                    if iterator is None:
                        iterator = iter(image_iterator(owned_source.gif))
                    frame = next(iterator)
                    frame_state["frame"] = frame
                    return frame_copy(frame)

                def close_abandoned_candidate(candidate):
                    if candidate is not frame_state["frame"]:
                        _close_media_value(candidate)

                try:
                    candidate = run_pillow(
                        copy_next_frame, owner=owned_source,
                        close_result=close_abandoned_candidate,
                    )
                except StopIteration:
                    break
                except PillowWorkCancelled:
                    source = None
                    return
                rendered_frame = True
                if monotonic is not None:
                    now = monotonic()
                    elapsed = preparation_elapsed + max(
                        elapsed_delays, now - playback_started,
                    )
                if max_duration is not None and elapsed >= max_duration:
                    _close_media_value(candidate)
                    return
                delay = max(
                    0.01,
                    frame_state["frame"].info.get("duration", 100)
                    / 1000.0 / playback_rate,
                )
                if max_duration is not None:
                    delay = min(delay, max_duration - elapsed)
                yield candidate, delay
                elapsed_delays += delay
            if not rendered_frame:
                return
            completed += 1
        if completion is not None:
            completion["complete"] = True
    except PillowWorkCancelled:
        source = None
        return
    finally:
        if source is not None:
            _close_media_preserving_active_error(source, "GIF source")


def video_media_frames(
    frame_decoder, fps=20, loops=1, stop_event=None, max_duration=None,
    monotonic=None, playback_rate=1.0,
):
    """Yield one decoded video timeline repeatedly with deadline-clipped waits."""
    fps = max(1.0, min(60.0, float(fps)))
    if (
        isinstance(playback_rate, bool)
        or not isinstance(playback_rate, (int, float))
        or not math.isfinite(playback_rate)
        or not 0.1 <= playback_rate <= 4.0
    ):
        raise ValueError("video playback rate must be between 0.1 and 4.0")
    playback_rate = float(playback_rate)
    frame_delay = 1.0 / fps / playback_rate
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


def play_media(
    matrix, filename, duration, stop_event=None, settings=None, *, pinned_handle=None,
    gif_loops=0, maximum_duration=None, require_complete=False,
):
    if pinned_handle is None:
        raise ValueError("media playback capability is missing")
    if stop_event is not None and stop_event.is_set():
        return False
    from safe_area import fit_media_geometry

    settings = fit_media_geometry(matrix, settings)
    geometry = {}
    if settings:
        geometry = {
            "x": settings["x"],
            "y": settings["y"],
            "width": settings["width"],
            "height": settings["height"],
            "keep_aspect": settings["keep_aspect"],
        }
    playback = {}
    if settings and "playback_speed_percent" in settings:
        playback["playback_rate"] = settings["playback_speed_percent"] / 100.0
    pinning = {"pinned_handle": pinned_handle}
    suffix = Path(filename).suffix.lower()
    if suffix == ".gif":
        return matrix.show_gif(
            filename,
            **geometry,
            **playback,
            loops=gif_loops,
            stop_event=stop_event,
            max_duration=duration if gif_loops == 0 else maximum_duration,
            require_complete=require_complete,
            **pinning,
        )
    if suffix == ".mp4":
        return matrix.show_video(
            filename,
            **geometry,
            **({"fps_override": settings["video_fps"]} if settings else {}),
            **playback,
            loops=0,
            stop_event=stop_event,
            max_duration=duration,
            **pinning,
        )

    from edit_preview import static_media_frames

    frame_iterator = static_media_frames(
        filename, duration, settings,
        render=lambda frame_filename, frame_geometry: matrix.show_image(
            frame_filename, delay=None, stop_event=stop_event,
            **pinning, **frame_geometry,
        ),
        monotonic=time.monotonic,
    )
    try:
        _, delay_ms, _ = next(frame_iterator)
    except StopIteration:
        return False
    waiter = stop_event or threading.Event()
    interrupted = waiter.wait(delay_ms / 1000)
    return not (require_complete and interrupted)
