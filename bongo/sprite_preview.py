"""See a sprite where it's used: the real screens that draw it, with a drawing standing in for the saved one.

The Animator sends the drawing as it is now (unsaved). For each screen that draws a sprite of that name this
renders a few seconds of it twice, with the built-in drawing and with the one sent, and returns both as small
GIFs. Nothing is saved and nothing global changes: the stand-in lives in ``sprites.overriding`` (a context
variable, so only the code running in this request sees it), and every screen is a new instance, never one of
the dashboard's cached previews. Kept cheap for a Raspberry Pi: at most two screens, ten frames a second,
three seconds each, and a time budget after which it stops and says so.
"""
from __future__ import annotations

import base64
import datetime as dt
import re
import time

from bongo import animator, icons, sprites
from bongo.module import Context
from bongo.modules.weather import data as weather_data

FPS = 10                 # the pictures are GIFs, which keep hundredths of a second: 10 a second is exact
SECONDS = 3.0            # the most of the animation that is shown
MAX_SCREENS = 2
SLOT_WAIT = 6.0          # seconds a second request waits for the first to finish before it is told to try again
BUDGET = 4.0             # seconds, after which no more is drawn (what has been drawn is returned)

WEATHER = re.compile(r"weather_(clear|partly|cloudy|fog|rain|snow|storm)(_night)?_(\d+)")
CODES = {"clear": 0, "partly": 2, "cloudy": 3, "fog": 45, "rain": 63, "snow": 73, "storm": 95}   # a WMO code each (look.condition)
ICON_SCREENS = ("countdown_days", "page", "countdown_clock", "countdown_flip", "countdown_ring", "countdown_tearoff")
WEATHER_SCREENS = ("overview", "forecast", "ticker")
TREE = re.compile(r"tree_(apple|oak|robin|firefly|butterfly_(?:brimstone|orange_tip|common_blue|peacock|admiral))")   # the Seasonal tree clock's drawings
TREE_VISITOR = {"robin": "robin", "firefly": "fireflies"}
TREE_MOMENT = {None: (7, 15, 12), "robin": (1, 15, 12), "butterfly": (5, 20, 14), "fireflies": (7, 5, 23)}   # month, day, hour


class Unknown(LookupError):
    """There is no built-in drawing of that name."""


def drawing(data, name: str) -> sprites.Sprite:
    """The sprite in a project as the Animator sends it (checked all the way through; ValueError if it is wrong).
    Only the frames that cover the seconds shown are drawn into pictures: the rest can't be seen."""
    project = animator.check_project({**data, "name": name} if isinstance(data, dict) else data)
    total, keep = 0, 0
    for frame in project["frames"]:
        keep += 1
        total += frame["ms"]
        if total >= SECONDS * 1000:
            break
    return sprites.from_project({**project, "frames": project["frames"][:keep]})


def built_in(name: str) -> sprites.Sprite:
    try:
        return sprites.load(name, own=False)
    except FileNotFoundError:
        raise Unknown(name) from None


def kind(name: str) -> str | None:
    """Which family of screens draws this sprite: "icon", "weather", "tree", or None (the logo and anything else a
    screen doesn't replace with a saved drawing)."""
    stem = animator.clean_project_name(name)
    if stem.startswith("icon_") and stem[5:] in icons.ICONS:
        return "icon"
    if TREE.fullmatch(stem):
        return "tree"
    return "weather" if WEATHER.fullmatch(stem) else None


def _stage(name: str, kind_: str, now: dt.datetime):
    """The module, the screens to try (best first) and a way to make each one's pictures: for the sprite ``name``."""
    if kind_ == "icon":
        icon = name[5:]
        event = {"title": icons.label(icon), "date": (now.date() + dt.timedelta(days=9)).isoformat(), "repeat": "once", "icon": icon}
        settings = {"holidays": [], "events": [event], "icons": True, "linked_calendars": False}
        return "calendar", ICON_SCREENS, settings, lambda style, at: style.render(at)
    if kind_ == "tree":   # the Seasonal tree clock on a day and at an hour that show this drawing (a visitor: always there)
        what = TREE.fullmatch(name)[1]
        visitor = f"butterfly:{what[10:]}:chase" if what.startswith("butterfly_") else TREE_VISITOR.get(what)
        month, day_, hour = TREE_MOMENT[visitor and visitor.split(":")[0]]

        def draw(style, at):
            style.show_off = visitor
            return style.render(dt.datetime(at.year, month, day_, hour, tzinfo=at.tzinfo) + (at - now))
        return "clock", ("tree",), {"tree": what if what in ("apple", "oak") else "apple", "hemisphere": "north"}, draw
    match = WEATHER.fullmatch(name)
    code, day = CODES[match[1]], match[2] is None
    here = now.replace(tzinfo=None)
    hours = tuple(weather_data.Hour(here + dt.timedelta(hours=3 * k), 14.0 - k, code, day) for k in range(1, 9))
    reading = weather_data.Reading(temp=14.0, feels=12.0, humidity=72, wind=4.0, code=code, is_day=day, high=17.0, low=9.0,
                                   hours=hours, fetched=time.time(), at=here)
    return "weather", WEATHER_SCREENS, {}, lambda style, at: style.draw(reading, at)   # drawn from a made-up reading: nothing fetched, no cache touched


def _gif(frames: list) -> str:
    return "data:image/gif;base64," + base64.b64encode(animator.encode_gif([(f, 1000 // FPS) for f in frames])).decode()


def render(modules, name: str, drawn: sprites.Sprite, area, *, fonts=None, pet=None, home=None, tz=None, calendar=None,
           budget: float = BUDGET, clock=time.monotonic) -> dict:
    """``{"screens": [{module, module_name, style, style_name, built_in, yours, ...}], "notes": [...]}`` for the
    sprite ``name``, drawn at the panel's size inside its safe area. Raises Unknown for a name with no built-in drawing."""
    name = animator.clean_project_name(name)
    original = built_in(name)
    kind_ = kind(name)
    began, notes = clock(), []
    if kind_ is None:
        return {"name": name, "screens": [], "notes": ["No screen on the Matrix draws this one from a saved drawing, so saving over it changes nothing you can see there."]}
    if kind_ == "icon" and (drawn.size != (icons.SIZE, icons.SIZE) or not drawn.frame(0).getbbox()):
        notes.append("The screens only use an icon that is 8 × 8 and has something drawn in its first frame, so they will keep showing the built-in one.")
    if kind_ == "weather":
        size = int(WEATHER.fullmatch(name)[3])
        if drawn.size != (size, size) or not any(f.getbbox() for f in drawn.frames):
            notes.append(f"The weather screens only use a drawing that is {size} × {size} and not empty, so they will keep showing the built-in one.")
    when = dt.datetime.combine(dt.datetime.now(tz).date(), dt.time(12), tzinfo=tz)
    module_id, candidates, settings, draw = _stage(name, kind_, when)
    module = modules.get(module_id)
    if module is None:
        return {"name": name, "screens": [], "notes": ["That screen isn't installed."]}
    keep = max(1, int(SECONDS * FPS))

    def make(style: str):
        ctx = Context(width=area.width, height=area.height, fonts=fonts or Context().fonts, pet=pet or Context().pet, home=home,
                      calendar=calendar, tz=tz, preview=True)
        return module.create(style, dict(settings), ctx)   # a new one each time: nothing carried over from another picture

    def pictures(style: str, stand_in: sprites.Sprite, count: int, asked: set | None = None):
        out = []
        with sprites.overriding({name: stand_in}, asked):
            shown = make(style)                 # (inside: a screen that reads its drawings as it starts sees the stand-in)
            for k in range(count):
                if k and clock() - began > budget:
                    return out, True
                at = when + dt.timedelta(seconds=k / FPS)
                shown.ctx.now = lambda at=at: at     # the style's own clock, on this picture's moment
                out.append(area.place(_fit(draw(shown, at), area)))
        return out, False

    screens, cut = [], False
    for style in candidates:
        if len(screens) >= MAX_SCREENS or clock() - began > budget:
            break
        asked: set = set()
        try:
            first, _ = pictures(style, original, 1, asked)   # one frame to see whether this screen draws the sprite at all
        except Exception:   # noqa: BLE001 - a screen that can't draw at this size just isn't one of the ones shown
            continue
        if name not in asked:
            continue
        mine, cut_mine = pictures(style, drawn, keep)
        theirs, cut_theirs = pictures(style, original, len(mine))
        cut = cut or cut_mine or cut_theirs
        count = min(len(mine), len(theirs))
        screens.append({"module": module_id, "module_name": module.name, "style": style, "style_name": _label(module, style),
                        "built_in": _gif(theirs[:count]), "yours": _gif(mine[:count]),
                        "width": area.panel_width, "height": area.panel_height, "frames": count, "ms": 1000 // FPS})
    if not screens:
        notes.append("No screen draws this one at your panel's size and safe area (a drawing at this size is used where there is less room, or more), so there is nothing to show.")
    if cut:
        notes.append("This took longer than it should on this Matrix, so only the first part of the movement is shown.")
    return {"name": name, "screens": screens, "notes": notes, "seconds": round(clock() - began, 2)}


def _fit(frame, area):
    return frame if frame.size == (area.width, area.height) else frame.crop((0, 0, area.width, area.height))


def _label(module, style: str) -> str:
    cls = module.styles[style]
    group = getattr(cls, "group", None)
    text = style.replace("_", " ").capitalize()
    return f"{group}: {text.split(' ', 1)[1].capitalize()}" if group and " " in text else text
