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
  safe_area.py   margins for a frame that hides the panel edges
  schedule.py    splash, sleep hours and brightness
  schema.py      setting types inferred from defaults, for validation and the dashboard
  media.py       the media library: built-in/ (shipped) and uploaded/ (from the dashboard)
  holidays.py    offline holiday rules (Easter, bank holidays, "last Monday in May")
  live.py        what's on now, shared between the player and the dashboard
  web/           the dashboard: JSON API + a small single-page app (pair with a PIN)
  modules/       one folder per screen (table below)
```

A **module** is one idea (a clock, the weather). A **style** is one look of that
module. Styles share the module's settings (time format, alignment) and add
their own. Picking a style is just a playlist setting:

```json
{"module": "lava_lamp", "style": "metro", "seconds": 30, "settings": {"palettes": ["ocean"]}}
```

See [docs/MODULES.md](docs/MODULES.md) to write a module and
[docs/ROADMAP.md](docs/ROADMAP.md) for the remaster plan.

## Modules

| Module | Styles | What it shows |
| --- | --- | --- |
| Lava Lamp | classic, metro | Time and date over moving wax. Classic is the v2 look (outlined, blurred blobs, 37 palettes). Metro is the refresh: wax that rises and sinks with a soft glow and hot core, clean type, 5 gradient palettes (magma, ocean, aurora, synthwave, mono). |
| Weather | overview, ticker, forecast | Current conditions, high/low and a 4-day forecast from Open-Meteo (no key). |
| Countdown | days, clock, flip, ring, tearoff | Time left to a date or a holiday (`next:christmas`), with a progress bar. |
| Text | static, scroll | Your own message, centred or scrolling. |
| Media | fit, fill, pixel | Images, animated GIFs and short videos (first 15 s, needs `ffmpeg`) from the media library; `pixel` scales pixel art crisply. |
| Announcements | banner, card, flash | Notices scheduled by date, weekday and hour; several rotate. |
| News | ticker, headline, picture | Headlines from RSS/Atom feeds, newest first; `picture` shows the story's thumbnail beside it. A feed must be an http(s) address that really serves RSS or Atom; the dashboard checks new feeds when you save. |
| Calendar | page, month, progress | Today's date with the next holidays and your own dates; the month as a grid; year/month/week/day progress bars. |
| Sky | moon, sun | Moon phase and next full/new moon; today's sun arc with sunrise and sunset. |

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
2. For video in the Media module: `sudo apt install ffmpeg`.
3. Copy `config.example.json` to `data/config.json` and edit the playlist.
   An empty playlist plays every module in its default style.
4. `sudo ./venv/bin/python -m bongo run`, or install `deploy/bongo-matrix.service`.

Media comes from two folders: `built-in/` (`resources/media/built-in`, shipped
with the project) and `uploaded/` (`resources/media/uploaded`, git-ignored, added from the dashboard under
Settings → Media library, up to 50 MB a file and 1 GB in all). Settings name files
as `built-in/sleep.gif` or `uploaded/holiday.mp4`; nothing outside those folders
can be shown.

**Splash and Sleep** work as in v2. Splash plays at start-up: tick any GIFs or images
(Settings → Splash) and each start-up shows the next one from a shuffled list, a GIF for
exactly one pass. Sleep dims the panel between two times and shows one of its ticked files,
picked at random each night and kept until morning.

**Custom Lava Lamp palettes** are a name and three colours (background, wax, highlight),
added in the module editor. Both styles use them: Classic directly, Metro works out its
gradients from the three colours. Every pass picks one palette at random from the built-in
ones you have ticked plus your custom ones that are switched on.

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
