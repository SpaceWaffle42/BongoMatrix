"""Shared runtime helpers for scrolling Matrix modules."""

LEGACY_FRAME_DELAYS = {
    "slow": 0.1,
    "normal": 0.05,
    "fast": 0.025,
}


def frame_delay_seconds(value, default=0.05):
    """Return numeric frame-delay seconds, accepting legacy speed presets."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return LEGACY_FRAME_DELAYS.get(value, default)
