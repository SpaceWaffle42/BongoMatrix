"""Configurable splash-screen module."""

from modules.media_player import play_media


def run(matrix, config, duration=8, stop_event=None):
    filename = config.get("media", {}).get("splash", "squares_tb.gif")
    settings = config.get("media_layout", {}).get("splash")
    return play_media(matrix, filename, duration, stop_event, settings=settings)


def screen(matrix, config, duration=8, stop_event=None):
    """Compatibility alias for the old module name."""
    return run(matrix, config, duration, stop_event)
