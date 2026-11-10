# Bongo Matrix v3

A Raspberry Pi and a 64×32 HUB75 RGB LED panel, turned into a modular display.

v3 is a ground-up rewrite. Everything up to v2 is frozen on the
[`archive/v2`](../../tree/archive/v2) branch. The guiding rule for v3 is
**modularity with as little code as possible**: the core knows how to play a
playlist, and every screen lives in its own folder under `bongo/modules/`.

## How it fits together

```
bongo/
  module.py      the contract: Module, Style, Context
  registry.py    finds every folder in bongo/modules/ that exposes MODULE
  player.py      plays a playlist of (module, style, settings, seconds)
  panel.py       HardwarePanel (rgbmatrix) and RecordingPanel (previews, tests)
  fonts.py       pixel-exact BDF fonts from resources/fonts
  config.py      data/config.json: panel, timezone, playlist
  modules/
    lava_lamp/   Lava Lamp, styles: classic, metro
```

A **module** is one idea (a clock, the weather). A **style** is one look of that
module. Styles share the module's settings (time format, alignment) and add
their own. Picking a style is just a playlist setting:

```json
{"module": "lava_lamp", "style": "metro", "seconds": 30, "settings": {"palette": "ocean"}}
```

See [docs/MODULES.md](docs/MODULES.md) to write a module and
[docs/ROADMAP.md](docs/ROADMAP.md) for the remaster plan.

## Modules

| Module | Styles | What it shows |
| --- | --- | --- |
| Lava Lamp | classic, metro | Time and date over moving wax. Classic is the v2 look (outlined, blurred blobs, 37 palettes). Metro is the refresh: wax that rises and sinks with a soft glow and hot core, clean type, 5 gradient palettes (magma, ocean, aurora, synthwave, mono). |

## Try it without a panel

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python -m bongo list
./venv/bin/python -m bongo preview lava_lamp --style metro --out metro.gif
./venv/bin/python -m bongo preview lava_lamp --style classic --set 'palettes=["fire_burst"]'
```

Previews are LED-look GIFs rendered by exactly the code that drives the panel.

## Run it on the Pi

1. Install [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
   and its Python bindings so the venv's Python can `import rgbmatrix`.
2. Copy `config.example.json` to `data/config.json` and edit the playlist.
   An empty playlist plays every module in its default style.
3. `sudo ./venv/bin/python -m bongo run`, or install `deploy/bongo-matrix.service`.

If the frame around the panel hides its edges, set `safe_area` in the config
(margins in pixels). Modules then draw only in the visible part. Find the right
margins with the test card: `sudo ./venv/bin/python -m bongo calibrate --margins 1,2`
(top/bottom, left/right; or four values T,R,B,L), and add `--save` when it lines up.

The defaults target one 64×32 panel on an Adafruit RGB Matrix HAT/Bonnet
(`adafruit-hat-pwm`); change `panel` in the config for anything else.

## Tests

```bash
./venv/bin/pip install pytest
./venv/bin/python -m pytest -q
```

`tests/test_contract.py` runs every module and every style through the module
contract automatically, so a new module is tested the moment its folder exists.
