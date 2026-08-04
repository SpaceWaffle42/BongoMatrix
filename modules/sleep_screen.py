"""Night/sleep animation module."""

from modules.media_player import play_media


def run(matrix, config, duration=60, stop_event=None):
    filename = config.get("media", {}).get("sleep", "sleep.gif")
    settings = config.get("media_layout", {}).get("sleep")
    return play_media(matrix, filename, duration, stop_event, settings=settings)


def panel(matrix, config, duration=60, stop_event=None):
    """Compatibility alias for the old module name."""
    return run(matrix, config, duration, stop_event)
