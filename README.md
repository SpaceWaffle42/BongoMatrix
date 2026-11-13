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
{"module": "clock", "style": "lava_metro", "seconds": 30, "settings": {"palettes": ["ocean"]}}
```

See [docs/MODULES.md](docs/MODULES.md) to write a module and
[docs/ROADMAP.md](docs/ROADMAP.md) for the remaster plan.

## Modules

| Module | Styles | What it shows |
| --- | --- | --- |
| Clock | lava_classic, lava_metro, big, flip, seven, nixie, nixie_seconds, word_grid, word_lines, dial, minimal, binary, pet | Every clock face in one place, in groups. **Lava**: time and date over moving wax; lava_classic is the v2 look (outlined, blurred blobs, 27 palettes). Metro is the refresh: wax that rises and sinks with a soft glow and hot core, clean type, 5 gradient palettes (magma, ocean, aurora, synthwave, mono). **Digital**: big, split-flap and seven-segment. **Glass tube**: nixie tubes. **Word**: a letter grid or typed-in lines ("twenty five past seven"). **Analogue**: a dial and a minimal wide face. **Binary**: BCD dots. **Pet**: a clock your pet visits now and then (walks by, naps on it, bats a ball, sits on the colon). 12 or 24 hours. Old `lava_lamp` playlist entries become `clock/lava_*` on their own. |
| Ambient | digital_rain, starfield, fire, plasma, rain_on_glass, aurora, fireflies, ocean, snow, life, sand, starlings, maze, pipes | Calm moving pictures with nothing to read, grouped Retro, Sky, Weather and Patterns. Each is a small simulation that fills the safe area; `speed` (0.25-4) sets the pace. Snow melts, sand drains, mazes and pipes start again on their own. |
| QR code | standard, wifi_guests, call_or_text, two_codes, code_only, code_and_message (+ `corner` overlay) | A real scannable code, one LED per module, with a label and the address scrolling underneath: a link, guest Wi-Fi (joins without typing), a number to call or text, two links side by side, a bigger code alone (about 50 characters), or swapping with a big message. Links too long to fit are refused on save with how much to cut. |
| Spotify | cover, backdrop, vinyl (+ `ticker` overlay) | What's playing: the cover, title and artist (scrolling when long), time and progress in the cover's colour. Greys out when paused and hands over to the next screen after a while, or at once when nothing plays. Sign in once in Settings > Spotify with your own Spotify app's Client ID (Development Mode; the app owner needs Premium). |
| Weather | overview, ticker, forecast | Current conditions, high/low and a 4-day forecast from Open-Meteo (no key). |
| Text | static, scroll | Your own message, centred or scrolling. |
| Media | fit, fill, pixel | Images, animated GIFs and short videos (first 15 s, needs `ffmpeg`) from the media library; `pixel` scales pixel art crisply. |
| Announcements | banner, card, flash | Notices scheduled by date, weekday and hour; several rotate. |
| News | ticker, headline, picture | Headlines from RSS/Atom feeds, newest first; `picture` shows the story's thumbnail beside it. A feed must be an http(s) address that really serves RSS or Atom; the dashboard checks new feeds when you save. |
| Calendar | Today: page, desk, date_line, week · Month: month, numbers, dots, year · Progress: progress, rings, weeks, percent · Countdown: countdown_days, countdown_clock, countdown_flip, countdown_ring, countdown_tearoff | One list of your dates (once, every year or a holiday; time, message, icon, colour) shown on the calendar looks; the countdowns count down to the soonest (or, with none, the next ticked holiday). The Countdown module moved in on 12 Nov 2026: saved entries move over by themselves. |
| Pet | life, idle, eating, napping, playing, zoomies, … | Mochi the cat (your Cat 10 art) in her room. Life plays the ticked modes through the day; the `wander` overlay walks her across other screens. Name her in Settings. |
| Sky | moon, sun | Moon phase and next full/new moon; today's sun arc with sunrise and sunset. |

## Try it without a panel

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python -m bongo list
./venv/bin/python -m bongo preview clock --style lava_metro --out metro.gif
./venv/bin/python -m bongo preview clock --style lava_classic --set 'palettes=["fire_burst"]'
```

Previews are LED-look GIFs rendered by exactly the code that drives the panel.

## Run it on the Pi

1. Install [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
   and its Python bindings so the venv's Python can `import rgbmatrix`.
2. For video in the Media module: `sudo apt install ffmpeg`.
3. Copy `config.example.json` to `data/config.json` and edit the playlist.
   An empty playlist plays every module in its default style.
4. `sudo ./venv/bin/python -m bongo run`, or install `deploy/bongo-matrix.service`.

A display only needs the `prod_device` branch: the same code without tests, tools, docs or the
dashboard demo (`tools/prod_tree.py` decides what goes in). Install it as a git checkout:

```bash
git clone -b prod_device http://gitlab.home.lan/matrix_display/bongo_matrix.git /home/admin/matrix
```

### Wi-Fi and finding it

No keyboard or SSH needed: a Matrix with no Wi-Fi it knows opens a `Matrix-Setup-XXXX` network
(the panel shows its password and a join code), and a phone that joins gets the setup page. Once
on Wi-Fi it answers at `http://<its name>.local`, shows its address on the panel when that changes,
and every Matrix on the network is listed at `http://matrix.local`. See
[docs/NETWORK.md](docs/NETWORK.md), including what to test on a real Pi.

### Updates

Settings → Software shows the running version, checks for a new one and pulls it. The
display service can only write to `data/`, so it never replaces its own code: it leaves a
request in `data/update-request.json`, and the updater (`deploy/bongo-update.path` +
`deploy/bongo-update.service`) fetches the branch, installs it, reinstalls packages if
`requirements.txt` changed, restarts the display and waits for the dashboard to answer. If the
new version doesn't start, it puts the old one back. Install the updater once:

```bash
sudo cp deploy/bongo-update.path deploy/bongo-update.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now bongo-update.path
```

Updates come from `prod_device` on the GitLab above. Settings → Software → Advanced can point a
display somewhere else (for example the public GitHub copy); it warns first, because whatever
is at that address runs on the display. `python -m bongo update` checks from the command line.

`prod_device` is rebuilt by CI after every green `main` (`tools/publish_prod.sh`), once a project
access token with `write_repository` is saved as the masked CI/CD variable `PROD_PUSH_TOKEN`.

Media comes from two folders: `built-in/` (`resources/media/built-in`, shipped
with the project, holding only screens: `splash_*` files are built-in Splash screens and
`sleep_*` files are built-in Sleep screens) and `uploaded/` (`data/uploads`, git-ignored, added from the dashboard under
Settings → Media library, up to 50 MB a file and 1 GB in all). Settings name files
as `built-in/sleep_default.gif` or `uploaded/holiday.mp4`; nothing outside those folders
can be shown.

**Splash and Sleep** work as in v2. Put a Splash item anywhere in the playlist: each time it
comes round it shows the next file from the ones ticked in Settings → Splash (a shuffled list),
a GIF for exactly one pass. A **boot screen** can also be picked to play once at start-up.
Sleep dims the panel between two times and shows one of its ticked files, picked at random
each night and kept until morning; whatever is playing stops when sleep starts.

**Custom Lava palettes** (Clock) are a name and three colours (background, wax, highlight),
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
