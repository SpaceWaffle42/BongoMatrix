"""Lava Lamp: a clock over moving wax. Two styles share the module's clock settings and your
custom palettes; each pass picks one palette at random from the ones you have ticked (like v2)."""
from bongo.module import Module

from .classic import Classic
from .metro import Metro
from .palettes import check_custom, enabled_custom


def check(settings: dict) -> None:
    """Custom palettes are well-formed, and each style has at least one palette to pick from."""
    check_custom(settings["custom_palettes"])
    chosen = settings["palettes"]
    if chosen != "all" and not chosen and not enabled_custom(settings["custom_palettes"]):
        raise ValueError("pick at least one palette, or turn on a custom one")


V2_FPS = 10  # roughly what v2's Lava managed on a Pi; per-frame speeds were tuned to it


def migrate(settings: dict) -> list[str]:
    """Classic's ``blob_speed`` (pixels per frame, before 11 Nov 2026) -> ``drift`` (pixels per second)."""
    if "blob_speed" not in settings:
        return []
    old = settings.pop("blob_speed")
    if isinstance(old, (int, float)) and not isinstance(old, bool) and "drift" not in settings:
        settings["drift"] = round(old * V2_FPS, 2)
    return [f"lava_lamp: blob_speed {old} per frame is now drift {settings.get('drift')} px/s"]


MODULE = Module(
    id="lava_lamp",
    name="Lava Lamp",
    description="Time and date over a lava lamp. Classic is the original look; Metro is the refresh.",
    styles={"classic": Classic, "metro": Metro},
    choices={"time_align": ["left", "center", "right"], "date_align": ["left", "center", "right"],
             "custom_palettes": "palettes"},
    defaults=dict(
        show_time=True, show_date=True, time_format="%H:%M",
        time_align="center", date_align="center",
        # Your own palettes, shared by both styles: a name and three colours (see palettes.py).
        custom_palettes=[],
    ),
    check=check,
    migrate=migrate,
    basic=("show_time", "show_date", "custom_palettes"),
)
