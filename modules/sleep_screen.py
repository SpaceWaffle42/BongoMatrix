"""Night/sleep animation module."""

from modules.media_player import play_media
from modules.splash_screen import _PinnedMediaMatrix


def run(matrix, config, duration=60, stop_event=None, *, pinned_handle=None):
    filename = config.get("media", {}).get("sleep", "sleep.gif")
    settings = config.get("media_layout", {}).get("sleep")
    if pinned_handle is not None:
        matrix = _PinnedMediaMatrix(matrix, pinned_handle)
    return play_media(matrix, filename, duration, stop_event, settings=settings)


def panel(matrix, config, duration=60, stop_event=None, *, pinned_handle=None):
    """Compatibility alias for the old module name."""
    return run(
        matrix, config, duration, stop_event, pinned_handle=pinned_handle,
    )
