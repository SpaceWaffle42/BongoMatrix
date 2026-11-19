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

API = 2   # what a module can count on from this Matrix. 2: ``Background``, ``Module.background``, ``home_connect``
#           and ``Context.module_settings`` (docs/MODULES.md, "Background modules, controls and the home network")


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
    calendar: dict | None = None  # the Calendar screens' colour coding, set in Settings > Calendars (config.calendar_look)
    preview: bool = False  # True: a picture for the dashboard (possibly of a made-up moment), not the screen on the panel.
    #                        A module that keeps something going between frames must not let a preview move it on.
    module_settings: dict = field(default_factory=dict)  # an imported module's own settings (its card in Settings), if it has any

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def canvas(self, colour=(0, 0, 0)) -> Image.Image:
        return Image.new("RGB", (self.width, self.height), colour)


class Style:
    """One look of a module. Subclass it, set ``defaults``, implement ``render``."""
    defaults: dict = {}
    choices: dict = {}  # setting name -> allowed values, for settings with a fixed set
    limits: dict = {}  # setting name -> (lowest, highest) for a number setting; anything outside is refused
    swatches: dict = {}  # setting name -> {choice: [colours]}, so the dashboard shows each choice's colours
    basic: tuple = ()  # this style's settings shown up front in the dashboard; the rest sit under Advanced
    secrets: tuple = ()  # this style's settings that are secrets (a password, a token): never put in a backup (bongo/backup.py)
    group: str | None = None  # heading the dashboard files this style under (a Clock's "Digital", "Word"...)
    finished: bool = False  # set True in render to hand over to the next entry before its time is up
    busy: bool = False      # True: in the middle of something worth finishing (a line still scrolling); the
    #                         player keeps the screen up past its time until it isn't (at most player.OVERRUN)
    hand_over_at: float | None = None  # when the player means to hand over (a timestamp on ctx.now's clock): set by the
    #                            player, so a style knows not to start something new it would then have to finish
    stay: bool = False      # True while something is live (a timer running, a live event): the player keeps the screen
    #                         on past its time for as long as it is (at most player.STAY_MOST in any STAY_WINDOW, never
    #                         in the sleep hours; Next still moves on). hand_over_at is None meanwhile: no end in sight
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

    def act(self, name: str) -> None:
        """Someone pressed one of the module's buttons (``Module.actions``) while this screen is on the panel.
        Called between frames: note what was asked and return at once; ``render`` shows it."""


class Panel:
    """What a background task may ask of the panel: ``Background.panel``. Each call only notes what is wanted;
    the Matrix does it when ``tick`` returns, and only if the module's bongo-module.json asked for that control
    ("abilities": "controls") and its owner agreed at import. Anything refused (not asked for, too often, a
    value that isn't right) is in ``refused`` at the next tick: [{"do": "sleep", "why": "..."}]."""
    MOST = 8   # asks per tick; any more are dropped

    def __init__(self):
        self.status: dict | None = None   # "status": {"asleep", "paused", "brightness", "brightness_by_hand", "screen", "module", "style"}
        self.refused: list = []
        self._asked: list = []

    def _ask(self, do: str, **more) -> None:
        if len(self._asked) < self.MOST:
            self._asked.append({"do": do, **more})

    def sleep(self, minutes: float | None = None) -> None:
        """"sleep": Sleep now. ``minutes`` left out: until the sleep hours next start or end (a week, if there are none)."""
        self._ask("sleep", **({} if minutes is None else {"minutes": minutes}))

    def wake(self) -> None:
        """"sleep": Wake now."""
        self._ask("wake")

    def brightness(self, value: int | None) -> None:
        """"brightness": 1 to 100, as if set by hand; None: back to the Matrix's own."""
        self._ask("brightness", value=value)

    def pause(self) -> None:
        """"playback"."""
        self._ask("pause")

    def resume(self) -> None:
        """"playback"."""
        self._ask("resume")

    def next(self) -> None:
        """"playback": on to the next screen."""
        self._ask("next")

    def message(self, text: str, title: str = "", seconds: float = 10) -> None:
        """"message": a few words on the panel (up to 120 characters, a title up to 40) for 3 to 30 seconds."""
        self._ask("message", text=text, title=title, seconds=seconds)


class Background:
    """An imported module's work that carries on while none of its screens is on the panel: pass the class as
    ``Module(background=...)`` and ask for it in bongo-module.json ("abilities": {"background": true}).
    It runs in the module's own process, started when the module is switched on and stopped when it is switched
    off or removed. ``tick`` is called every ``every`` seconds and must come back quickly: one that takes over
    5 seconds is taken for a hang, and the module's process is stopped and started again a little later."""
    every: float = 5          # seconds between ticks (1 to 3600)

    def __init__(self, settings: dict, panel: Panel):
        self.settings = settings      # the module's own settings (its card in Settings), secrets included
        self.panel = panel
        self._status = None

    def start(self) -> None:
        """Once, before the first tick."""

    def tick(self, now: dt.datetime) -> None:
        """Every ``every`` seconds. Do a little and return."""

    def settings_changed(self) -> None:
        """``self.settings`` has just changed (someone saved the module's card)."""

    def stop(self) -> None:
        """The module is being switched off, removed or updated, or the Matrix is stopping: say goodbye (close a
        connection, tell the far end you are going) in at most 2 seconds. Past that the process is stopped
        anyway. Not called after a crash or a hang. The panel controls can't be used from here."""

    def report(self, text: str, ok: bool = True) -> None:
        """The module's one-line status in the dashboard ("Connected", or what's wrong in a few words: up to 80
        characters). ``ok`` False shows it with the amber dot. "" takes it away."""
        self._status = {"text": str(text), "ok": bool(ok)}


def home_connect(timeout: float = 5.0):
    """A connected TCP socket to the one home-network address this module's owner typed into its settings
    (bongo-module.json: "abilities": {"home_address": {"ports": [...]}}). There is nothing to pass: the Matrix
    dials the address and port in the settings, and nothing else on the home network can be reached. Raises
    OSError (PermissionError when it isn't allowed, with the reason) if it can't. For TLS, wrap the socket."""
    from bongo.sandbox import host
    return host.home_connect(timeout)


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    styles: dict[str, type[Style]]
    defaults: dict = field(default_factory=dict)
    description: str = ""
    choices: dict = field(default_factory=dict)
    # ``limits={"blob_count": (1, 24)}``: the lowest and highest a number setting may be. A number that
    # could stall or crash the panel needs one; the dashboard's slider and number box keep to it too.
    limits: dict = field(default_factory=dict)
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
    # ``verify_parts(settings) -> list``: the things in the settings that ``verify`` checks, one by one (News: its
    # feeds' addresses; they must be hashable). With it, ``verify(settings, parts)`` is called only with the parts
    # that no screen of this module in the saved config already has, and not at all when there are none: a part
    # accepted once isn't checked again, whatever else about the screen changes. Without it, ``verify(settings)``
    # runs whenever anything in a screen's settings is new.
    verify_parts: Callable[[dict], list] | None = None
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
    # ``word_filters={"feeds": "/api/rss/filtered"}``: each entry of that list setting may carry word filters: it is
    # then saved as {"url": the entry, "only": "words, ...", "never": "words, ..."} in place of the plain entry (both
    # forms are valid; the module's ``check`` and its own code read either). The dashboard shows the two boxes under
    # each entry and asks that route (POST {name: the list}) how many get through.
    word_filters: dict = field(default_factory=dict)
    # ``choice_names={"holidays": {"labels": {"eid_al_fitr": "Eid al-Fitr"}, "groups": {"eid_al_fitr": "Islamic"}}}``:
    # how the dashboard words a choice setting's values, and the headings it groups them under.
    choice_names: dict = field(default_factory=dict)
    # ``marks={"text_speed": [[25, "Slow"], [50, "Normal"]]}``: named points along a number setting's slider in
    # the dashboard (it can still be set anywhere between its limits; the slider snaps to a mark it's dragged near).
    marks: dict = field(default_factory=dict)
    # ``renames={"old_module": {"old_style": "new_style", None: "style for entries without one"}}``:
    # entries saved for a module that has since moved into this one (config.tidy rewrites them).
    renames: dict = field(default_factory=dict)
    # ``help``: a step-by-step guide behind a Help button, for a module that needs something set up
    # away from the Matrix (an account, a developer app, a web address). See ``bongo.schema.HELP_KEYS``:
    # {"title": ..., "intro": ..., "steps": [...], "tips": [...], "links": [{"label", "url" | "route"}],
    # "required": True if it shows nothing until this is done}.
    # Text inside `backticks` is shown as something to copy, with a Copy button.
    help: dict | None = None
    # ``extend(settings, ctx)``: settings filled in from outside the entry as a screen starts (the
    # Calendar adds the showings of your linked calendars, bongo.calendars). Returns new settings.
    extend: Callable[[dict, Context], dict] | None = None
    # ``actions={"feed": "Feed"}``: buttons the dashboard shows while one of this module's screens is on the
    # panel (name -> the words on the button). Pressing one calls that screen's ``Style.act(name)``. Always
    # optional extras: a screen must be complete with nobody pressing anything. See ``bongo.schema.actions``.
    actions: dict = field(default_factory=dict)
    # ``notes={"step_hours": "Rounded to the forecast's 3-hour steps."}``: a short line the dashboard shows
    # under a setting, for one whose effect isn't what its name alone suggests.
    notes: dict = field(default_factory=dict)
    # ``trouble()``: what stops every screen of this module showing anything right now, in a few words
    # ("Add your OpenWeather API key"), or None. The dashboard shows it on those screens, beside the
    # Help guide; the screens themselves hand over at once meanwhile.
    trouble: Callable[[], str | None] | None = None
    # ``secrets=("token",)``: shared settings that are secrets (a password, a token). They are never put in a
    # backup (bongo/backup.py); a style lists its own in ``Style.secrets``.
    secrets: tuple = ()
    # ``needs(settings)``: what a screen with these settings needs that is kept outside its settings and that
    # this Matrix doesn't have (a key, a sign-in, a link), with where to add it, as the end of the sentence
    # "3 Weather screens need ...": "an OpenWeather key: skipped until you add one in Settings › Weather".
    # None: nothing. Asked when a backup is restored, since none of those are ever in one.
    needs: Callable[[dict], str | None] | None = None
    # ``background``: a ``Background`` class, for an imported module that keeps working while none of its
    # screens is on the panel (it may then have no styles at all). See docs/MODULES.md.
    background: type | None = None

    @property
    def default_style(self) -> str | None:
        return next(iter(self.styles), None)   # (None: a module with no screens, only a background task)

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
        limits = {**self.limits, **style_cls.limits}
        for name, value in (overrides or {}).items():
            try:
                check(name, merged[name], value, choices.get(name), limits.get(name))
            except ValueError as error:
                raise ValueError(f"{self.id}: {error}") from None
            if name in self.item_fields and isinstance(value, list) and not all(isinstance(v, dict) for v in value):
                raise ValueError(f"{self.id}: {name}: every entry must be an object")
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
        settings = self.settings_for(style, self.secreted(self.homed(settings, ctx.home)), overlay)
        cls = self._style(style, overlay)
        if self.secrets or getattr(cls, "secrets", ()):   # what it is handed is never to be in the log (bongo/standins.py)
            from bongo import standins
            standins.hush(settings.get(n) for n in (*self.secrets, *cls.secrets))
        return cls(ctx, self.extend(settings, ctx) if self.extend else settings)

    def homed(self, settings: dict | None, home: dict | None) -> dict | None:
        """``settings`` with the Matrix's location (``home``) in each place setting, unless this screen
        has its own (``own_location``), or no location is set (then its own settings, or the defaults)."""
        if not self.places or not home or (settings or {}).get("own_location"):
            return settings
        out = dict(settings or {})
        for name, (lat, lon) in self.places.items():
            out[name], out[lat], out[lon] = home["name"], home["latitude"], home["longitude"]
        return out

    def secreted(self, settings: dict | None) -> dict | None:
        """``settings`` with this module's shared secrets filled in. A secret every screen of the module shares
        (``secrets`` on the Module: an API key) is kept once for the module, in a private file and never with a
        screen (bongo/private.py); one that is a look's own (``Style.secrets``) is already among its screen's
        settings (bongo/standins.py). Either way a screen finds it in ``self.settings``."""
        if not self.secrets:
            return settings
        from bongo import private

        kept = private.secrets_of(self)
        return {**(settings or {}), **kept} if kept else settings

    def _style(self, style: str | None, overlay: bool = False) -> type[Style]:
        kinds = self.overlays if overlay else self.styles
        name = style or next(iter(kinds), None)
        if name not in kinds:
            kind = "overlay" if overlay else "style"
            raise ValueError(f"{self.id}: no {kind} {name!r} (have {sorted(kinds)})")
        return kinds[name]
