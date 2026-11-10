"""Overlays: things drawn over whatever module is playing, on a timer.

    "overlays": [{"module": "announcements", "overlay": "ribbon", "every": 300, "seconds": 30}]

An overlay is a style listed in a module's ``overlays``; its render returns an RGBA frame
the size of the safe area, transparent where the module underneath should show. A rule is
on for ``seconds`` out of every ``every`` seconds (``every`` 0 = always), shifted by
``offset``, so two overlays can take turns. Like modules, it is a pure function of the clock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from PIL import Image

log = logging.getLogger(__name__)


@dataclass
class Rule:
    module: str
    overlay: str | None = None
    every: float = 0
    seconds: float = 30
    offset: float = 0
    settings: dict = field(default_factory=dict)
    enabled: bool = True

    def on(self, now) -> bool:
        if not self.enabled:
            return False
        if self.every <= 0:
            return True
        return (now.timestamp() - self.offset) % self.every < self.seconds


class Overlays:
    def __init__(self, rules: list[Rule], modules: dict, ctx_factory):
        self.rules, self.modules, self.ctx_factory = rules, modules, ctx_factory
        self._styles: dict[int, object] = {}

    @classmethod
    def from_config(cls, config: dict, modules: dict, ctx_factory) -> "Overlays":
        return cls([Rule(**r) for r in config.get("overlays", [])], modules, ctx_factory)

    def style(self, i: int, rule: Rule):
        if i not in self._styles:  # built on first use, then kept so it can animate
            self._styles[i] = self.modules[rule.module].create(rule.overlay, rule.settings, self.ctx_factory(),
                                                               overlay=True)
        return self._styles[i]

    def __call__(self, frame: Image.Image, now) -> Image.Image:
        for i, rule in enumerate(self.rules):
            if not rule.on(now):
                continue
            try:
                layer = self.style(i, rule).render(now)
            except Exception:  # a broken overlay is dropped, never the panel
                log.exception("overlay %s/%s failed; turning it off", rule.module, rule.overlay)
                rule.enabled = False
                continue
            if layer.size == frame.size:
                frame = Image.alpha_composite(frame.convert("RGBA"), layer.convert("RGBA")).convert("RGB")
        return frame

    def __bool__(self) -> bool:
        return any(r.enabled for r in self.rules)
