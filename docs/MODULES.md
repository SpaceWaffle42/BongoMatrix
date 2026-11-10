# Writing a module

A module is a folder in `bongo/modules/`. The folder's `__init__.py` exposes one
`MODULE`. Nothing outside the folder needs to change: the registry finds it,
the player can play it, and the contract tests check it.

```
bongo/modules/my_module/
  __init__.py     MODULE = Module(...)
  plain.py        one file per style (or keep everything in __init__.py if tiny)
```

## The contract

```python
from bongo.module import Module, Style

class Plain(Style):
    fps = 10                                  # frames per second the player asks for
    defaults = {"colour": "#ffffff"}          # this style's own settings

    def setup(self):                          # optional, runs once per playlist slot
        self.font = self.ctx.fonts("6x10")

    def render(self, now):                    # return a ctx.width x ctx.height RGB image
        img = self.ctx.canvas()
        self.font.draw(img, 2, 10, now.strftime("%H:%M"), (255, 255, 255))
        return img

MODULE = Module(
    id="my_module", name="My Module",
    styles={"plain": Plain},                  # the first style is the default
    defaults={"show_seconds": False},         # settings every style shares
)
```

Rules that keep modules independent:

- **Lay out relative to `ctx.width` and `ctx.height`.** The canvas is the panel's
  *safe area* (the part the frame doesn't hide), which can be smaller than 64x32.
  Anchor rows to the top or bottom of the canvas; `bongo.fonts.middle()` centres a
  font vertically. The contract tests render every style at several sizes.
- **Only use what `self.ctx` gives you**: size, timezone (`ctx.now()`), a seeded
  `ctx.rng`, `ctx.fonts`, `ctx.canvas()`. Never import another module.
- **`render` draws one frame and returns it.** The player owns timing and the
  panel. Prefer drawing from `now` rather than counting frames, so a frame can
  be reproduced from a timestamp (previews, tests, the dashboard).
- **Settings are validated for you.** Module defaults, then style defaults, then
  the user's playlist settings. An unknown key is an error, so typos surface.
- **Use `ctx.rng` for randomness** so a seed reproduces a frame exactly.
- **Shared code between styles stays inside the module folder**
  (see `lava_lamp/field.py` and `lava_lamp/palettes.py`).

## Adding a style to an existing module

Add a `Style` subclass and one line in `styles={...}`. Lava Lamp is the model:
`classic.py` and `metro.py` share the module's clock settings and the
`field()` maths, and each keeps its own palettes, fonts and drawing.

## Checking it

```bash
python -m bongo list
python -m bongo preview my_module --style plain --out my_module.gif
python -m pytest -q
```
