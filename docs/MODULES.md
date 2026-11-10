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

## Checks beyond types

Types and `choices` cover most settings. When a value needs more, give the `Module`:

- `check(settings)`: quick, raises `ValueError` with a readable message. Runs whenever
  settings are validated (config load, every dashboard save, previews).
- `verify(settings) -> list[str]`: may be slow or use the network. Runs only when the
  dashboard saves settings that are new, so an unchanged entry is never re-checked.

News uses both: `check` insists every feed is an `http(s)://` address, and `verify`
fetches each new feed once and refuses anything that isn't RSS or Atom.

## Keeping the dashboard simple

The dashboard builds each module's editor from its settings. Two hints keep it friendly:

- `basic=("text", "colour")` on the `Module` (and `basic = (...)` on a `Style`): the settings
  most people change, shown up front. Everything else goes under a folded **Advanced settings**
  card. Leave it out and every setting is shown.
- `item_fields={"items": [{"name": "title", "type": "text", "label": "Title"}, ...]}`: a
  setting that is a list of entries gets a small form per entry instead of raw JSON. Field
  types: `text`, `longtext`, `date`, `time`, `days`, `colour`; `"more": True` tucks a field
  under the entry's "More options".
- `swatches = {"palettes": {name: [colours]}}` on a `Style`: each choice is shown with its colours.

## Overlays

An overlay is drawn *over* whatever module is playing: a notice ribbon now, the pet
walking across later. It is a `Style` listed in the module's `overlays={...}` (not in
`styles`), and its `render` returns an **RGBA** frame the size of the canvas, transparent
wherever the module underneath should show. Return a fully transparent frame when there
is nothing to draw. Announcements' `ribbon` is the example.

Overlays are switched on in the config, on a timer (`every` 0 means always on):

```json
"overlays": [{"module": "announcements", "overlay": "ribbon", "every": 600, "seconds": 45}]
```

They play over the day playlist only, never over splash, sleep or pushed messages.

## Checking it

```bash
python -m bongo list
python -m bongo preview my_module --style plain --out my_module.gif
python -m pytest -q
```
