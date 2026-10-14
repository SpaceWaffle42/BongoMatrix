"""Playlist adapter: prepared snapshot only, never fetch/decode during playback."""
from modules import rss_renderer


def run(matrix, config, duration=None, stop_event=None, *, prepared=None):
    if prepared is None or not prepared.items or (stop_event is not None and stop_event.is_set()):
        return False
    return rss_renderer.run(matrix, prepared.stories, stop_event).frames > 0
