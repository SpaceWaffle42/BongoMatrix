"""Sprites: the fixed art modules draw (icons, clock parts, little animations), loaded by name.

Every sprite is an Animator project, the same text file the Animator opens and saves
(``bongo/animator.py``): the built-in ones are in ``resources/animator``, and one you save from the
dashboard under the same name is used in its place. So any piece of art can be opened in the Animator,
changed, and seen on the panel, with no exported images to keep in step.

    moon = sprites.load("moon_16")
    image.alpha_composite(moon.frame(3), (x, y))      # frame 3
    image.alpha_composite(moon.at(seconds), (x, y))   # whichever frame is showing now
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from bongo import animator


@dataclass(frozen=True)
class Sprite:
    name: str
    frames: tuple[Image.Image, ...]   # RGBA, see-through where nothing is drawn
    ms: tuple[int, ...]               # how long each frame shows
    loop: str                         # loop, pingpong or once

    @property
    def size(self) -> tuple[int, int]:
        return self.frames[0].size

    def frame(self, index: int) -> Image.Image:
        return self.frames[index % len(self.frames)]

    def at(self, seconds: float) -> Image.Image:
        """The frame showing ``seconds`` after the animation started, by its own timings and loop."""
        order = list(range(len(self.frames)))
        if self.loop == "pingpong" and len(order) > 2:
            order += order[-2:0:-1]
        total = sum(self.ms[i] for i in order)
        t = seconds * 1000
        t = min(t, total - 1) if self.loop == "once" else t % total
        for i in order:
            if t < self.ms[i]:
                return self.frames[i]
            t -= self.ms[i]
        return self.frames[order[-1]]


_cache: dict[str, tuple[tuple, Sprite | str]] = {}   # name -> (which files it was read from, the sprite or why there isn't one)


def _read(path: Path) -> Sprite:
    project = animator.read_project(path)
    pictures = animator.flatten(project)
    return Sprite(path.stem, tuple(img for img, _ in pictures), tuple(ms for _, ms in pictures), project["loop"])


def load(name: str, own: bool = True) -> Sprite:
    """The sprite called ``name``: your own version if you've saved one from the dashboard, else the built-in
    one (also if yours can't be read). Raises FileNotFoundError if there is neither, and ValueError if the
    built-in file is broken, so a typo shows up in the tests, not as a blank patch on the panel.

    ``own=False``: the built-in one only, whatever has been saved under its name (the logo: it is on pages
    anyone on the network can open, so it must stay the size and shape it was drawn).

    Read once and kept until a file changes, so it costs nothing to call every frame. The pictures are
    shared: paste them, don't draw on them."""
    stem = animator.clean_project_name(name)
    places = (animator.projects_dir(), animator.BUILT_IN) if own else (animator.BUILT_IN,)
    paths = [p for p in (place / f"{stem}.json" for place in places) if p.is_file()]
    if not paths:
        raise FileNotFoundError(f"no sprite called {name!r} in {animator.BUILT_IN} or {animator.projects_dir()}")
    slot = stem if own else f"{stem} (built-in)"
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    if _cache.get(slot, (None,))[0] != key:
        result: Sprite | str = "no file"
        for path in paths:
            try:
                result = _read(path)
                break
            except (ValueError, OSError) as error:
                result = f"the sprite {name!r} can't be read ({path.name}): {error}"
        _cache[slot] = (key, result)   # one entry per name: a saved change takes the old one's place
    result = _cache[slot][1]
    if isinstance(result, str):
        raise ValueError(result)   # a fresh error each time: nothing piles up if a module asks every frame
    return result
