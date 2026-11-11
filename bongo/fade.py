"""Brightness ramps: going to sleep and waking up glide between levels instead of jumping.

``Fader`` wraps a panel. ``ramp_to(level, seconds, curve)`` starts a glide from wherever the
brightness is now; every ``show()`` moves it along the curve. ``set_brightness`` (the
dashboard's override) still jumps straight there.
"""
from __future__ import annotations

import math
import time

# t (0..1 through the ramp) -> progress (0..1 of the way to the new level)
CURVES = {
    "smooth": lambda t: t * t * (3 - 2 * t),                  # gentle at both ends
    "linear": lambda t: t,                                    # constant speed
    "ease_in": lambda t: t * t,                               # slow start, quick finish
    "ease_out": lambda t: 1 - (1 - t) * (1 - t),              # quick start, slow finish
    "perceptual": None,                                       # even steps to the eye (see below)
}
DEFAULT_SECONDS = 5
DEFAULT_CURVE = "smooth"
MAX_SECONDS = 600


def level_at(start: float, end: float, t: float, curve: str = DEFAULT_CURVE) -> float:
    """Brightness ``t`` (0..1) of the way through a ramp from ``start`` to ``end``."""
    t = min(1.0, max(0.0, t))
    if curve == "perceptual":
        # eyes judge light on a log scale, so glide the logarithm: no sudden-looking last steps
        a, b = math.log(max(start, 1)), math.log(max(end, 1))
        return math.exp(a + (b - a) * t)
    shape = CURVES.get(curve) or CURVES[DEFAULT_CURVE]
    return start + (end - start) * shape(t)


class Fader:
    def __init__(self, panel, clock=time.monotonic):
        self.panel, self.clock = panel, clock
        self.level: float | None = None          # what the panel is showing now
        self._ramp: tuple[float, float, float, float, str] | None = None  # start, end, t0, seconds, curve

    def __getattr__(self, name):
        return getattr(self.panel, name)

    @property
    def ramping(self) -> bool:
        return self._ramp is not None

    def set_brightness(self, level: int) -> None:
        """Jump straight to ``level`` (dashboard override)."""
        self._ramp = None
        self._apply(level)

    def ramp_to(self, level: int, seconds: float = DEFAULT_SECONDS, curve: str = DEFAULT_CURVE) -> None:
        """Glide to ``level`` over ``seconds``. Asking again for the same target changes nothing."""
        target = self._ramp[1] if self._ramp else self.level
        if target is not None and round(target) == round(level):
            return
        if self.level is None or seconds <= 0:  # first light at start-up: no fade from nothing
            self.set_brightness(level)
            return
        self._ramp = (self.level, float(level), self.clock(), float(seconds), curve)

    def tick(self) -> None:
        if not self._ramp:
            return
        start, end, t0, seconds, curve = self._ramp
        t = (self.clock() - t0) / seconds
        if t >= 1:
            self._ramp = None
            self._apply(end)
        else:
            self._apply(level_at(start, end, t, curve))

    def show(self, image) -> None:
        self.tick()
        self.panel.show(image)

    def _apply(self, level: float) -> None:
        self.level = level
        whole = max(1, min(100, round(level)))
        if hasattr(self.panel, "set_brightness"):
            self.panel.set_brightness(whole)
