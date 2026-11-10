"""The module contract. Every display module is a folder under bongo/modules/
that exposes one ``MODULE``. A module owns one or more *styles*: different looks
of the same idea (Lava Lamp: classic, metro). Nothing outside a module's folder
knows anything about it.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from PIL import Image

from bongo.fonts import Fonts


@dataclass
class Context:
    """What the core hands every style: the canvas size and shared services."""
    width: int = 64
    height: int = 32
    tz: ZoneInfo | None = None
    rng: random.Random = field(default_factory=random.Random)
    fonts: Fonts = field(default_factory=Fonts)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def canvas(self, colour=(0, 0, 0)) -> Image.Image:
        return Image.new("RGB", (self.width, self.height), colour)


class Style:
    """One look of a module. Subclass it, set ``defaults``, implement ``render``."""
    defaults: dict = {}
    choices: dict = {}  # setting name -> allowed values, for settings with a fixed set
    fps: int = 30

    def __init__(self, ctx: Context, settings: dict):
        self.ctx = ctx
        self.settings = settings
        self.setup()

    def setup(self) -> None:
        """Optional one-off preparation (load fonts, pick a palette...)."""

    def render(self, now: dt.datetime) -> Image.Image:
        raise NotImplementedError


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    styles: dict[str, type[Style]]
    defaults: dict = field(default_factory=dict)
    description: str = ""
    choices: dict = field(default_factory=dict)

    @property
    def default_style(self) -> str:
        return next(iter(self.styles))

    def settings_for(self, style: str | None = None, overrides: dict | None = None) -> dict:
        """Module defaults <- style defaults <- user overrides. Unknown keys, wrong types and
        values outside a setting's ``choices`` are errors, so typos surface early."""
        from bongo.schema import check

        style_cls = self._style(style)
        merged = {**self.defaults, **style_cls.defaults}
        unknown = set(overrides or {}) - set(merged)
        if unknown:
            raise ValueError(f"{self.id}: unknown setting(s) {sorted(unknown)}")
        choices = {**self.choices, **style_cls.choices}
        for name, value in (overrides or {}).items():
            try:
                check(name, merged[name], value, choices.get(name))
            except ValueError as error:
                raise ValueError(f"{self.id}: {error}") from None
        merged.update(overrides or {})
        return merged

    def create(self, style: str | None = None, settings: dict | None = None,
               ctx: Context | None = None) -> Style:
        return self._style(style)(ctx or Context(), self.settings_for(style, settings))

    def _style(self, style: str | None) -> type[Style]:
        name = style or self.default_style
        if name not in self.styles:
            raise ValueError(f"{self.id}: no style {name!r} (have {sorted(self.styles)})")
        return self.styles[name]
