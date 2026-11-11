"""The module contract. Every display module is a folder under bongo/modules/
that exposes one ``MODULE``. A module owns one or more *styles*: different looks
of the same idea (Lava Lamp: classic, metro). Nothing outside a module's folder
knows anything about it.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Callable
from zoneinfo import ZoneInfo

from PIL import Image

from bongo.fonts import Fonts
from bongo.pets import Pet


@dataclass
class Context:
    """What the core hands every style: the canvas size and shared services."""
    width: int = 64
    height: int = 32
    tz: ZoneInfo | None = None
    rng: random.Random = field(default_factory=random.Random)
    fonts: Fonts = field(default_factory=Fonts)
    pet: Pet = field(default_factory=Pet)  # the Matrix's one pet (bongo/pets.py)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def canvas(self, colour=(0, 0, 0)) -> Image.Image:
        return Image.new("RGB", (self.width, self.height), colour)


class Style:
    """One look of a module. Subclass it, set ``defaults``, implement ``render``."""
    defaults: dict = {}
    choices: dict = {}  # setting name -> allowed values, for settings with a fixed set
    swatches: dict = {}  # setting name -> {choice: [colours]}, so the dashboard shows each choice's colours
    basic: tuple = ()  # this style's settings shown up front in the dashboard; the rest sit under Advanced
    group: str | None = None  # heading the dashboard files this style under (a Clock's "Digital", "Word"...)
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
    # Overlays are styles drawn *over* whatever is playing (see bongo/overlays.py). Their
    # render returns an RGBA frame where transparent pixels let the module underneath show.
    overlays: dict[str, type[Style]] = field(default_factory=dict)
    # Optional extra checks on the merged settings. ``check`` is quick and runs on every
    # validation (raise ValueError). ``verify`` may use the network and runs only when the
    # dashboard saves new settings; it returns a list of problems (empty = fine).
    check: Callable[[dict], None] | None = None
    # ``migrate`` updates one entry's saved settings from an older Bongo, in place, before unknown
    # settings are dropped (``config.tidy``); returns notes for the log.
    migrate: Callable[[dict], list[str]] | None = None
    verify: Callable[[dict], list[str]] | None = None
    # Dashboard hints. ``basic``: the few settings most people change, shown up front; the rest
    # go under "Advanced settings". Leave it empty to show everything. ``item_fields``: for a
    # list-of-entries setting, the fields of one entry, so it gets a form instead of raw JSON:
    # {"items": [{"name": "title", "type": "text", "label": "Title"}, ...]}. Field types: text,
    # longtext, date, time, days, colour; ``"more": True`` tucks a field under the entry's "More".
    basic: tuple = ()
    item_fields: dict = field(default_factory=dict)
    # ``places={"location": ("latitude", "longitude")}``: the dashboard turns that setting into a place
    # search; picking a result fills in the name and both coordinates together.
    places: dict = field(default_factory=dict)
    # ``suggestions={"feeds": [{"name": "BBC Top Stories", "value": "https://...", "group": "BBC"}]}``:
    # ready-made values for a list setting, offered as one-tap "Quick add" picks in the dashboard.
    suggestions: dict = field(default_factory=dict)
    # ``choice_names={"holidays": {"labels": {"eid_al_fitr": "Eid al-Fitr"}, "groups": {"eid_al_fitr": "Islamic"}}}``:
    # how the dashboard words a choice setting's values, and the headings it groups them under.
    choice_names: dict = field(default_factory=dict)

    @property
    def default_style(self) -> str:
        return next(iter(self.styles))

    def settings_for(self, style: str | None = None, overrides: dict | None = None,
                     overlay: bool = False) -> dict:
        """Module defaults <- style defaults <- user overrides. Unknown keys, wrong types and
        values outside a setting's ``choices`` are errors, so typos surface early."""
        from bongo.schema import check

        style_cls = self._style(style, overlay)
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
        if self.check:
            try:
                self.check(merged)
            except ValueError as error:
                raise ValueError(f"{self.id}: {error}") from None
        return merged

    def create(self, style: str | None = None, settings: dict | None = None,
               ctx: Context | None = None, overlay: bool = False) -> Style:
        return self._style(style, overlay)(ctx or Context(), self.settings_for(style, settings, overlay))

    def _style(self, style: str | None, overlay: bool = False) -> type[Style]:
        kinds = self.overlays if overlay else self.styles
        name = style or next(iter(kinds), None)
        if name not in kinds:
            kind = "overlay" if overlay else "style"
            raise ValueError(f"{self.id}: no {kind} {name!r} (have {sorted(kinds)})")
        return kinds[name]
