"""Configurable splash-screen module."""

from modules.media_player import play_media


def run(
    matrix, config, duration=8, stop_event=None, *, pinned_handle=None,
    maximum_duration=None,
):
    if pinned_handle is None:
        raise ValueError("splash media capability is missing")
    filename = config.get("media", {}).get("splash", "squares_tb.gif")
    settings = config.get("media_layout", {}).get("splash")
    return play_media(
        matrix, filename, duration, stop_event, settings=settings,
        pinned_handle=pinned_handle, gif_loops=1,
        maximum_duration=maximum_duration,
        require_complete=True,
    )


def screen(
    matrix, config, duration=8, stop_event=None, *, pinned_handle=None,
    maximum_duration=None,
):
    """Compatibility alias for the old module name."""
    return run(
        matrix, config, duration, stop_event, pinned_handle=pinned_handle,
        maximum_duration=maximum_duration,
    )
