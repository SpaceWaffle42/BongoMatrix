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
    home: dict | None = None  # the Matrix's location, set in Settings: {"name", "latitude", "longitude"}

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
    finished: bool = False  # set True in render to hand over to the next entry before its time is up
    once: bool = False      # set by the player: play through once, then hand over (splash, boot screen)
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
    # search; picking a result fills in the name and both coordinates together. Such a module also has
    # ``own_location=False`` in its defaults: until a screen is given its own place, it uses the
    # Matrix's location from Settings (``Context.home``), see ``homed``.
    places: dict = field(default_factory=dict)
    # ``suggestions={"feeds": [{"name": "BBC Top Stories", "value": "https://...", "group": "BBC"}]}``:
    # ready-made values for a list setting, offered as one-tap "Quick add" picks in the dashboard.
    suggestions: dict = field(default_factory=dict)
    # ``choice_names={"holidays": {"labels": {"eid_al_fitr": "Eid al-Fitr"}, "groups": {"eid_al_fitr": "Islamic"}}}``:
    # how the dashboard words a choice setting's values, and the headings it groups them under.
    choice_names: dict = field(default_factory=dict)
    # ``renames={"old_module": {"old_style": "new_style", None: "style for entries without one"}}``:
    # entries saved for a module that has since moved into this one (config.tidy rewrites them).
    renames: dict = field(default_factory=dict)
    # ``help``: a step-by-step guide behind a Help button, for a module that needs something set up
    # away from the Matrix (an account, a developer app, a web address). See ``bongo.schema.HELP_KEYS``:
    # {"title": ..., "intro": ..., "steps": [...], "tips": [...], "links": [{"label", "url" | "route"}],
    # "required": True if it shows nothing until this is done}.
    # Text inside `backticks` is shown as something to copy, with a Copy button.
    help: dict | None = None

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
        ctx = ctx or Context()
        return self._style(style, overlay)(ctx, self.settings_for(style, self.homed(settings, ctx.home), overlay))

    def homed(self, settings: dict | None, home: dict | None) -> dict | None:
        """``settings`` with the Matrix's location (``home``) in each place setting, unless this screen
        has its own (``own_location``), or no location is set (then its own settings, or the defaults)."""
        if not self.places or not home or (settings or {}).get("own_location"):
            return settings
        out = dict(settings or {})
        for name, (lat, lon) in self.places.items():
            out[name], out[lat], out[lon] = home["name"], home["latitude"], home["longitude"]
        return out

    def _style(self, style: str | None, overlay: bool = False) -> type[Style]:
        kinds = self.overlays if overlay else self.styles
        name = style or next(iter(kinds), None)
        if name not in kinds:
            kind = "overlay" if overlay else "style"
            raise ValueError(f"{self.id}: no {kind} {name!r} (have {sorted(kinds)})")
        return kinds[name]
