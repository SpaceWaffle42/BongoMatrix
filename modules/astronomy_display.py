"""Prepared-only astronomy playlist adapter; host owns all numerical and font work."""
from modules import astronomy_renderer


def run(matrix, config, duration=None, stop_event=None, *, prepared=None):
    if prepared is None or not prepared.cards or (stop_event is not None and stop_event.is_set()):
        return False
    return astronomy_renderer.run(matrix, prepared.cards, stop_event) > 0
