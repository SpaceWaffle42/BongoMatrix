"""Ambient: calm moving pictures with nothing to read, grouped as Retro, Sky, Weather and
Patterns. Every style is a small simulation (``sim.py``) that fills whatever canvas it gets."""
from bongo.module import Module

from .patterns import Life, Maze, Sand
from .retro import DigitalRain, Fire, Pipes, Plasma
from .sky import Aurora, Fireflies, Ocean, Starfield, Starlings
from .weather import RainOnGlass, Snow


def check(settings: dict) -> None:
    if not 0.25 <= float(settings["speed"]) <= 4:
        raise ValueError(f"speed {settings['speed']} must be between 0.25 and 4")


MODULE = Module(
    id="ambient",
    name="Ambient",
    description="Calm moving pictures: digital rain, a starfield, fire, plasma, rain on glass, aurora, "
                "fireflies, an ocean sunset, snow, the Game of Life, falling sand, starlings, a maze, pipes.",
    styles={
        "digital_rain": DigitalRain, "starfield": Starfield, "fire": Fire, "plasma": Plasma,
        "rain_on_glass": RainOnGlass, "aurora": Aurora, "fireflies": Fireflies, "ocean": Ocean,
        "snow": Snow, "life": Life, "sand": Sand, "starlings": Starlings, "maze": Maze, "pipes": Pipes,
    },
    check=check,
)
