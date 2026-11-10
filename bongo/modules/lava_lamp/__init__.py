"""Lava Lamp: a clock over moving wax. Two styles share the module's clock settings."""
from bongo.module import Module

from .classic import Classic
from .metro import Metro

MODULE = Module(
    id="lava_lamp",
    name="Lava Lamp",
    description="Time and date over a lava lamp. Classic is the original look; Metro is the refresh.",
    styles={"classic": Classic, "metro": Metro},
    defaults=dict(
        show_time=True, show_date=True, time_format="%H:%M",
        time_align="center", date_align="center",
    ),
)
