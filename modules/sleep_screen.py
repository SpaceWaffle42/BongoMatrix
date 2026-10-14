"""Night/sleep animation module."""

from modules.media_player import play_media


def run(
    matrix, config, duration=60, stop_event=None, *, pinned_handle=None,
    maximum_duration=None,
):
    if pinned_handle is None:
        raise ValueError("sleep media capability is missing")
    filename = config.get("media", {}).get("sleep", "sleep.gif")
    settings = config.get("media_layout", {}).get("sleep")
    if maximum_duration is not None and filename.lower().endswith((".gif", ".mp4")):
        # Scheduled nights pass the time until wake: loop seamlessly in one
        # playback instead of reopening the media (a visible freeze) each pass.
        return play_media(
            matrix, filename, maximum_duration, stop_event, settings=settings,
            pinned_handle=pinned_handle, gif_loops=0,
        )
    return play_media(
        matrix, filename, duration, stop_event, settings=settings,
        pinned_handle=pinned_handle, gif_loops=1,
        maximum_duration=maximum_duration,
    )


def panel(
    matrix, config, duration=60, stop_event=None, *, pinned_handle=None,
    maximum_duration=None,
):
    """Compatibility alias for the old module name."""
    return run(
        matrix, config, duration, stop_event, pinned_handle=pinned_handle,
        maximum_duration=maximum_duration,
    )
