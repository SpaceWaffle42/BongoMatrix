"""Configurable splash-screen module."""

from modules.media_player import play_media


class _PinnedMediaMatrix:
    """Inject one already-resolved handle without changing media-player timing."""

    def __init__(self, matrix, pinned_handle):
        self._matrix = matrix
        self._pinned_handle = pinned_handle

    def __getattr__(self, name):
        return getattr(self._matrix, name)

    def show_gif(self, filename, **options):
        return self._matrix.show_gif(
            filename, pinned_handle=self._pinned_handle, **options,
        )

    def show_image(self, filename, **options):
        return self._matrix.show_image(
            filename, pinned_handle=self._pinned_handle, **options,
        )


def run(matrix, config, duration=8, stop_event=None, *, pinned_handle=None):
    filename = config.get("media", {}).get("splash", "squares_tb.gif")
    settings = config.get("media_layout", {}).get("splash")
    if pinned_handle is not None:
        matrix = _PinnedMediaMatrix(matrix, pinned_handle)
    return play_media(matrix, filename, duration, stop_event, settings=settings)


def screen(matrix, config, duration=8, stop_event=None, *, pinned_handle=None):
    """Compatibility alias for the old module name."""
    return run(
        matrix, config, duration, stop_event, pinned_handle=pinned_handle,
    )
