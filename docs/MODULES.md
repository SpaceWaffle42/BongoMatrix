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
  (see `clock/lava/field.py` and `clock/common.py`).

## Adding a style to an existing module

Add a `Style` subclass and one line in `styles={...}`. Clock is the model:
every face is a style with a `group` (Lava, Digital, Word…), sharing `common.py`;
the Lava pair `classic.py` and `metro.py` share the lava clock settings and the
`field()` maths, and each keeps its own palettes, fonts and drawing.

## Checks beyond types

Types and `choices` cover most settings. When a value needs more, give the `Module`:

- `limits={"blob_count": (1, 24)}` (on the `Module`, or on a `Style` for its own settings): the
  lowest and highest a number may be. Give one to any number that could stall or crash the panel
  (counts, radii, speeds). Saves outside it are refused, the editor's slider keeps to it, and
  older saved values are pulled inside it when the config loads.
- `check(settings)`: quick, raises `ValueError` with a readable message. Runs whenever
  settings are validated (config load, every dashboard save, previews).
- `verify(settings) -> list[str]`: may be slow or use the network. Runs only when the
  dashboard saves settings that are new, so an unchanged entry is never re-checked.
- `verify_parts(settings) -> list`: the things `verify` checks, one by one (News: its feeds'
  addresses). With it, `verify(settings, parts)` is given only the parts that no screen of the
  module in the saved config already has, and isn't called when there are none. So changing a
  screen's other settings checks nothing, and a part that was accepted once never blocks a save.

News uses both: `check` insists every feed is an `http(s)://` address (and that a feed's word
filters, if it has any, are two boxes of words), and `verify` fetches each new feed once and
refuses anything that isn't RSS or Atom; `verify_parts` is its feeds' addresses, so a feed is
fetched at a save only the first time its address is saved.

## Secrets, and what a backup leaves out

Settings > Back up and restore saves the playlist with every screen's settings as one file (`bongo/backup.py`).
Nothing secret may be in it:

- A key or sign-in the whole Matrix shares doesn't belong in a screen's settings at all. Keep it in a file of its
  own, written with `bongo.private.write` (as Weather's key and Spotify's sign-in are), and give the `Module`
  `needs=lambda settings: None if have_it() else "an OpenWeather key: skipped until you add one in Settings › Weather"`:
  what a screen with these settings needs that this Matrix doesn't have, and where to add it, as the end of the
  sentence "3 Weather screens need ...". A restore says it, since no backup can bring it along.
- A secret that is one screen's own (the guest Wi-Fi password of a QR screen) is a setting like any other, named in
  `secrets=("password",)` on the `Style` (its own settings) or the `Module` (shared ones). A backup leaves those
  out, and the restore says which screens need them typed in again.
- As a net under that, a backup also leaves out any setting whose name reads as a secret (password, token, secret,
  api_key, key, auth and the like), at any depth, and any web address with a password in it. Don't rely on the net:
  the tests insist every built-in setting it would catch is one its module names in `secrets`.

An imported module's `secrets` cross to the Matrix with the rest of its description. To stay importable on a Matrix
from before backups existed, pass `secrets` to `Module` only when `"secrets" in Module.__dataclass_fields__`
(on a `Style` it is a plain attribute and always safe). Its `needs` hook isn't run on the Matrix's side.

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
- `word_filters={"feeds": "/api/rss/filtered"}`: each entry of that list setting can carry word filters.
  An entry with none stays the plain value it always was (`"https://..."`); one with filters is saved as
  `{"url": "https://...", "only": "words, or phrases", "never": "more words"}`. Both forms are valid for good, so
  nothing is migrated and an older config loads untouched; the module's `check` and its own code accept either.
  The dashboard draws the two boxes under each entry and asks that route (a POST of `{"feeds": [...]}`, behind
  pairing) what gets through. Built-in modules only (it needs that route). News is the one user: `bongo/modules/rss/filters.py` has the matching rule
  (headline and summary, whole words, any case and accent, a phrase as a phrase, Never show wins over Only),
  and `data.latest` applies it when the stories are put together, not per frame.

## Secret settings (an API key, a password)

A setting that is a secret is named in `secrets`. Once saved, the Matrix never sends it back to a browser: the
editor masks it, says whether one is saved, and lets you replace or clear it. It is never in what the hub or
the Now page are told is playing, and never in the log. Where it is kept depends on whose it is:

```python
class Guests(Style):
    defaults = {"network": "Guest", "password": ""}
    secrets = ("password",)                     # this screen's own: each screen has its own

MODULE = Module(id="departures", name="Departures", styles={"board": Board, "guests": Guests},
                defaults={"station": "", "api_key": ""},      # a secret is a text setting, "" until one is saved
                secrets=("api_key",),                         # the module's: one for all its screens
                notes={"api_key": "Your own key, from your account page."})
```

- **The module's** (`secrets` on the `Module`: an API key every screen of it uses). The Matrix keeps one value for
  the module in a file only it can read (`data/secrets.json`, `bongo/private.py`), never with a screen and so never
  in the config. In the editor it is saved the moment Save is pressed, apart from the playlist, and every screen
  of the module has it from its next turn.
- **A screen's own** (`secrets` on the `Style`: one screen's password). It is a setting of that screen, saved with
  it. The dashboard is sent a stand-in in its place, `(saved:…)`, which comes back with the screen however the
  playlist is rearranged; the Matrix swaps the real one back in (`bongo/standins.py`).
- Either way a screen finds it in `self.settings` like any other setting. Never draw it, print it or put it in an
  error: a web address with the key in it is the usual way one leaks. (What an imported module prints has its
  secrets blanked out before it reaches the log, as a net under that, not a licence.)
- `bongo.private.is_secret(module, name, style)` is how any other code asks whether a setting is one, before it
  copies, shows or logs it.
- Imported modules can have both kinds: the values cross to the module's own process with its settings, and the
  module's own is forgotten when the module is removed. To stay importable on a Matrix from before secrets, pass
  `secrets` to `Module` only when `"secrets" in Module.__dataclass_fields__` (on a `Style` it is a plain attribute
  and always safe); there it is an ordinary text setting, kept with the screen and shown as typed.
- `notes` (the line under a setting) works for imported modules too, guarded the same way.

## Buttons (actions)

A module can offer buttons that the dashboard shows on the Now page while one of its screens is on the panel:

```python
class Reef(Style):
    def act(self, name):          # called between frames, on the screen that is on the panel
        if name == "feed":
            self.food_at = self.ctx.now()

MODULE = Module(id="aquarium", name="Aquarium", styles={"reef": Reef}, actions={"feed": "Feed"})
```

- `actions` is `{name: the words on the button}`: at most 6; a name is lower-case letters, digits and `_`
  (up to 24, starting with a letter); a label is 1 to 20 characters.
- `act` must only note what was asked and return; `render` draws it. A button that raises is logged and
  the screen carries on.
- Buttons are extras. A screen must be complete with nobody pressing anything, and a press is only
  delivered while the module is on the panel (previews never get one). A new screen object is built each
  time the module's turn comes, so anything that should last longer than one turn lives at module level
  or, for an imported module, in a file in its working folder.
- `ctx.preview` is True when the frame is a picture for the dashboard (which may be of a made-up
  moment) and not the screen on the panel. If your module keeps something going between frames, give
  a preview a copy of its own so that looking at one never disturbs the panel. The editor's **Preview on
  the panel** switch shows such a preview on the LEDs themselves (`bongo/panelpreview.py`): the same
  draft, built with `ctx.preview` True and drawn in real time over the playlist. `finished` and `busy` are
  ignored there, and a `render` that raises ends the preview.
- Imported modules get them too: the press crosses to the module's own process as a name and nothing
  else. To stay importable on a Matrix from before buttons existed, pass `actions` only when
  `"actions" in Module.__dataclass_fields__`.

## Scrolling text, and screens that need a moment longer

A line too long for its space should scroll so it can be read, from its first letter when the screen
comes on, and the screen shouldn't move on until it has gone all the way through. `bongo.scroll.Scrolls`
does the bookkeeping:

```python
from bongo.scroll import HOLD, Scrolls

class Ticker(Scrolls, Style):                 # Scrolls before Style
    def render(self, now):
        need = HOLD + (width + gap) / speed    # seconds to read it through once
        t = self.scrolled(("line", text), now, need)   # seconds since this screen first drew this line
        x = -int(max(0.0, t - HOLD) * speed) % (width + gap)
        ...
```

- `scrolled(key, now, need)` starts a line from 0 when the screen first draws it (or after a gap or a
  clock jump), and marks the screen **busy** until it has been on for `need` seconds.
- `turns(key, items, now, seconds)` is for things that take turns on one screen (stories, notices): a
  screen starts on the one after whichever was showing last time, and stays until the one it's on has
  had its time.
- Three attributes of every `Style` are the player's: **`stay`** (below), **`busy`** (True: keep this screen up a little past
  its seconds, at most 2 minutes, or 1 for an imported module, and never into a sleep change) and
  **`hand_over_at`** (set by the player: when it means to move on, so nothing new is started after it).
  Don't use those names for anything else. An imported module that doesn't use `Scrolls` can simply
  set `self.busy` in `render`.
- `bongo.scroll` came with a later Matrix. An imported module that wants to run on an older one too
  imports it inside `try: ... except ImportError:` and falls back to setting `self.busy` itself (an
  older Matrix ignores `busy`).

## Staying on while something is live

`busy` is for a moment longer. A screen showing something that is happening right now (a timer running, a
live event) can ask to keep the panel until it's over, by setting one more of the player's attributes in `render`:

```python
class Stopwatch(Style):
    defaults = {"keep_the_panel": False}       # the user's choice: only ask when they've said so

    def render(self, now):
        self.stay = self.settings["keep_the_panel"] and self.running(now)
        ...
```

- **`stay`** is True while it's live and False the moment it's over. The player keeps the screen on past its
  seconds for as long as it's True, and `hand_over_at` is None meanwhile (no end in sight). Say it afresh
  every frame; don't use the name for anything else.
- It only starts on the screen's own turn: a screen isn't drawn, and so can't ask, until its turn in the
  playlist comes round. Say so wherever your settings offer it.
- It's the user's to switch on. Give the screen a setting for it, off unless they turn it on.
- Next moves on as always (the screen may ask again on its next turn), Pause holds whatever is on, and a
  pushed message cuts in and then gives the panel back to the live screen: for a moment, and then only while it is
  still live. That is not a fresh turn, and the message's time counts towards the allowance below.
- Never in the sleep hours: a live screen is cut where sleep starts, and nothing is kept while the Matrix is
  asleep or has been woken in its sleep hours. Sleep, splash and pushed screens are never kept.
- A backstop, so a mistake can't hold the panel for good: a module's screens may be kept at most 4 hours in any 12
  (`player.STAY_MOST`, `STAY_WINDOW`). After that they hand over on time like any other screen. The allowance is
  per module (all its screens share it; another module has its own), outlasts a saved config, and starts afresh
  when the display restarts.
- The Now page says the screen is staying on because it's live.
- Imported modules can ask too, if their `bongo-module.json` says so: `"abilities": {"stay": true}` (the import review
  tells the owner before "I trust" is ticked). One that doesn't is never kept, whatever it sets. One whose process stops answering (2 seconds without a frame, or a crash)
  hands over at once, live or not. An older Matrix ignores `stay`, so there is nothing to guard.

## Overlays

An overlay is drawn *over* whatever module is playing: a notice ribbon, or the pet
walking across. It is a `Style` listed in the module's `overlays={...}` (not in
`styles`), and its `render` returns an **RGBA** frame the size of the canvas, transparent
wherever the module underneath should show. Return a fully transparent frame when there
is nothing to draw. Announcements' `ribbon` is the example.

Overlays are switched on in the config, on a timer (`every` 0 means always on):

```json
"overlays": [{"module": "announcements", "overlay": "ribbon", "every": 600, "seconds": 45}]
```

They play over the day playlist only, never over splash, sleep or pushed messages.

## Sprites

Fixed art (an icon, a clock part, a little animation) is drawn in the dashboard's **Animator**, not in
code. A module asks for it by name:

```python
from bongo import sprites

moon = sprites.load("moon_16")            # resources/animator/moon_16.json
frame.alpha_composite(moon.frame(3), (x, y))
frame.alpha_composite(moon.at(seconds), (x, y))   # the frame showing now, by the sprite's own timings
```

Each sprite is an Animator project: a small text file holding its size, layers, frames and timings.
`sprites.load` flattens it with the same sums the Animator's preview uses, so the panel shows what
was drawn; there are no exported images to keep in step. To add or change one, draw it in the
Animator at the size you need (Size, top left), **File > Download this drawing**, and put the file in
`resources/animator/`. A project saved on a Matrix under the same name is used in place of the
built-in one, so anyone can restyle a piece of art from the dashboard (**File > See it where it's used…** shows the screens that draw it, with the drawing on screen in its place, before anything is saved). (**Open > Duplicate** on a built-in one makes a copy
of your own under another name, `<name>_copy`, which no module uses until it is saved under the built-in's name.) A layer
marked `"locked": true` in a project file is only locked against changes in the Animator; it is drawn like any other.

A drawing whose layers mean different things to the code gives each layer on its own, shown or hidden:
`sprites.load("tree_apple").layer("Leaves")` (RGBA, or None if there is no layer of that name). The Seasonal
tree clock reads its trees that way: `Wood`, `Leaves` (drawn in up to four greens, light to shade, which are
summer; the other seasons are drawn in the same four tones), `Blossom`, `Fruit`, and a hidden `Perch` layer
holding the one pixel of branch the robin stands on. Redraw any of them in the Animator and keep the layer names.

Timing: a sprite keeps each frame's time exactly, in ms, and `sprite.at(seconds)` follows it at whatever rate
your module draws (its `fps`). That is not what happens to a drawing saved with **Save to library**: a GIF holds
hundredths of a second, and the Media module (playlist, Splash, boot and Sleep screens, **Show on Matrix**) draws
20 times a second, so a frame is shown for a whole number of 50 ms and a time like 65 ms comes out sometimes
shorter, sometimes longer (right on average). It plays the first 300 frames of a file, and **Show on Matrix** plays
15 seconds. The Animator's preview and the frames line ("22 frames · 1.5 s") show what the Media module will do, and its
frame time boxes step in 50s (any time from 20 to 5000 ms can still be typed).
A GIF also holds 256 colours: with more, the Animator says so when you save, and the GIF (not the drawing, which keeps
every colour) uses close ones.

**More > Import GIF or picture** takes a GIF, PNG, JPEG, BMP or WebP (up to 16 megapixels) and asks where it goes
(replace the drawing, a new layer, or new frames after this one), whether to show the whole picture or fill the
canvas, and whether black counts as empty.

Draw only what is fixed. Anything that depends on the time, on data or on chance stays in the
module's code, which places the sprites.

An imported module's art travels with it: Animator project files in its own folder, read with
`sprites.read(path)` (in `setup`, once). Nothing is exported from them, so the module's pictures can be
opened in the Animator (File > Open a drawing file…; on a phone, ≡) and changed like any other.

## Checking it

```bash
python -m bongo list
python -m bongo preview my_module --style plain --out my_module.gif
python -m pytest -q
```

## Sharing a module (Settings > Modules > Import)

Anyone can import your module from a GitHub link, a GitLab project (gitlab.com or a GitLab of your own), a
direct link to a .zip, or a .zip they upload. Put the module's folder at the top of the repo (or the zip),
exactly as it would sit in `bongo/modules/`, with a `bongo-module.json` beside its `__init__.py`:

```json
{
  "id": "tube_departures",
  "name": "London Tube departures",
  "version": "1.2.0",
  "author": "sam-k",
  "description": "Next trains from a station you pick.",
  "network": ["api.tfl.gov.uk"],
  "homepage": "https://github.com/sam-k/bongo-tube-departures"
}
```

- `id` must match `MODULE.id`, and can't be a built-in module's name.
- `network` lists every site it fetches from (`*.example.com` for any name under one). It can reach
  nothing else: an imported module runs in a process of its own, as a user of its own, sees none of the
  Matrix's files (settings, calendars, the pet art) and has no network but a gate that lets it through to
  these sites over the usual web ports, never to the home network. `urllib` and other libraries that
  honour the usual proxy settings work as normal. (One device on the home network, named by the owner, is
  something a module can ask for: see "Background modules, controls and the home network" below.)
- Only what the Matrix already has installed (Pillow, numpy and Python's own library) can be imported.
- It may write files in its working folder (the current folder when it starts); everything else is read-only.
- A GitHub link (`https://github.com/SpaceWaffle42/BongoMatrix-imports`) installs the latest release if there is one,
  otherwise the default branch.
- A GitLab link (`https://gitlab.com/group/project`, `http://gitlab.example.lan/group/bongo_imports`)
  installs the default branch (asked of GitLab's API, or `main`, then `master`), or the branch or tag in the
  link (`…/-/tree/v1.2`; `…/-/tree/main/sports` goes straight to that folder). A link straight to a .zip
  (`https://example.com/clock.zip`) works too, but isn't checked for updates.
- Before anything is kept, every style is drawn on a test screen; if one fails, nothing is installed.
- Imported modules can't have overlays yet, and their `check`, `verify`, `migrate` and `extend` hooks
  aren't run on the Matrix's side.
- On the Matrix each module may use up to 300 MB of memory (all its processes together); past that it's stopped.

### Several modules in one repository

A repository may hold several modules, one folder each, one folder down from the top (never deeper):

```
BongoMatrix-imports/
├── README.md
├── sports/            (bongo-module.json, __init__.py, ...)
└── home_assistant/    (bongo-module.json, __init__.py, ...)
```

A folder counts only if it has both a `bongo-module.json` and an `__init__.py`; anything else (a README, a
folder of notes) is ignored. A repository whose top is itself a module is one module, as before. When there
are several, the import lists them all in a pop-up (name, version, author, what it does, its sites and what
it asks for, and whether it's installed already or this would be an update), none ticked; the owner ticks
the ones they want and presses Review selected. Each then gets its own review, test screen and "I trust"
tick, one after another, and is installed only if its own checks pass: one that fails never stops the
others. A folder whose `bongo-module.json` is wrong is listed with the reason and can't be ticked; two
folders with the same `id` can't be either. Give each folder the module's `id` as its name.

Each installed module remembers the repository and its own folder, so its updates come from that folder.

### Links that aren't GitHub

GitHub is the normal place to publish a module. Other links are allowed at the owner's own risk: before
anything is fetched from a link that isn't GitHub, the Import box says what it is and what's true of it, and
the owner ticks "I understand" before Fetch. A link is never refused for being http, on the home network or
not GitHub; it is refused only where the Matrix itself, or somewhere the owner never chose, would be reached
(below). The warnings:

- "This isn't GitHub. Only import from a place you run or trust: the module's code runs on this Matrix (in
  its sandbox)."
- "This address is on your home network." (a private or link-local address, or a name ending .lan, .local,
  .home, .internal, or with no dots)
- "This address isn't encrypted (http): someone on the network could change what's downloaded."
- With an access token typed for an http link, also: "This address isn't encrypted: the access token is sent
  as plain text, and anyone on your network could read it." (in the same list, under the same tick)

The review says them again, beside "I trust". The Matrix does the fetch itself, carefully
(`bongo/linkfetch.py`): the name is looked up once and only the addresses that passed are dialled; never
this Matrix itself (loopback, 0.0.0.0, any address of its own), link-local addresses (169.254.x.x, fe80:)
or IPv4 hidden inside IPv6; a redirect is followed only to the same scheme, host and port (anything else is
refused, naming where, so you can decide to import from there), and an access token is only ever sent to that
same scheme, host and port; the same 5 MB download cap, a timeout on every request and two minutes for a
whole download. Only one import is at work at a time, a repository waiting at the picker included (for up to
15 minutes).

**Private GitLab projects.** If the project needs a sign-in, the import says so and opens an **Access
token** box: a read-only project or personal token with `read_api` (so the Matrix can see the default
branch and the tags), or `read_repository` alone (then it fetches `main`, or `master`, and follows that branch). It is sent only as GitLab's `PRIVATE-TOKEN` header, only to that host (downloads then come from
GitLab's API, `/api/v4/projects/<id>/repository/archive.zip`, which takes the header), and never in a
link. Once a module from the project is installed it is kept by `bongo/private.py` (`data/secrets.json`,
under `@sources`), never in the config, a backup, the log or anything a browser is sent, and it is
forgotten when the last module from that project is removed.

**Updates from GitLab** are looked for on the same daily schedule as GitHub's: the project's newest tag if it
has tags, otherwise the latest commit of the branch it came from. Either way the version in the module's own
folder's `bongo-module.json` is compared with the installed one, so a tag made for one module of a repository
doesn't show as an update for the others. Versions are compared as versions (a leading v and +build ignored,
1.0 the same as 1.0.0, a pre-release such as 1.1.0-rc.1 older than 1.1.0); one that can't be read is never
offered. **Once a repository has tags, updates follow the newest tag only: a newer version that is only on
the branch isn't offered until it is tagged.** If the project stops accepting the kept token, the module's
row says it needs a sign-in (no stale update is left), and Add a new token asks for one. Nothing installs by itself: Update
shows the review again, and anything new it asks for is agreed again.

### Accepted risks of imported modules

Anyone who imports a module accepts these; the import review says so before "I trust" is ticked:

- **Shared-service risk.** The gate checks the site a secure connection names, not what's asked for inside
  it, so a module that lists a big service many websites share (a CDN) may also reach those other websites.
- **`*.` sites.** `*.` sites under well-known shared hosting are refused, but that list can't be complete;
  the review spells out what each `*.` site means.
- **"Any site".** A module whose "Only allow the sites it lists" is unticked shares the Matrix's network:
  any site, the home network, and other programs on the Matrix that listen on its network.
- **Disk.** A module may keep up to 100 MB in its data folder. Over that it is stopped and the folder emptied.
- **Idle.** A module's process is stopped 30 minutes after its last frame on the panel (10 minutes if only
  previews used it) and started again when next needed, so one taken out of the playlist doesn't keep running.
  Not one that asked to work in the background, and was let: that one runs for as long as it is switched on.
- **Location.** A module isn't given the Matrix's location from Settings (`ctx.home` is `None`): if it needs a
  place, it asks for one in its own settings.
- **Links that aren't GitHub.** Whoever runs that place decides what is downloaded; the Matrix can't tell
  who made it. Over plain http (a GitLab on the home network, say) anyone on the network between could
  change the download, and an access token crosses the network in the clear. The review and the test
  screen still run before anything is installed, and the module still runs in its sandbox, but only import
  from a place you run or trust.

For a module that asked for more than drawing its screens (the next section), the import review lists each
thing it asked for before "I trust" can be ticked, and anyone who agrees accepts these too:

- **Background.** It runs all the time it is switched on, playlist or no playlist, with the same limits as
  before (memory, disk, output, its own user and files, a low priority). It is never part of drawing the
  panel: a background task that hangs is stopped after 5 seconds and tried again less and less often (5
  seconds, 30 seconds, 5 minutes, then every half hour), and said in the log once. Switch the module off and
  it stops.
- **Controls.** It gets only the controls it named, and each is limited (below), so the worst a broken or
  unkind module can do with them is what its owner could do by leaning on the dashboard's own buttons, more
  slowly: sleep or wake the panel at most once a second and 6 times in ten minutes, change the brightness once a
  second (60 times), pause or skip once a second (60 times), show 10 short messages in ten minutes. It can
  never change the config, the playlist or any setting, and what it is told of the panel is what the Now page
  shows: never a setting, never the picture.
- **One device on the home network.** The gate lets it open plain connections to the one address its owner
  typed into its settings, on a port from its bongo-module.json, and to nothing else there. What is still
  refused, whatever is typed and whatever a name looks up to: this Matrix itself (loopback, 0.0.0.0 and every
  address the Matrix has on any network, the setup hotspot's too, so never the dashboard or anything else
  listening on it); link-local addresses (169.254.x.x and fe80:, where a router or a cloud machine keeps
  services of its own); multicast and broadcast (and any x.x.x.255, the broadcast of a usual home network); IPv4 hidden inside IPv6; and anything on the internet (that
  is what `network` is for). Only 10.x.x.x, 172.16-31.x.x, 192.168.x.x and fc00::/7 pass. A name is looked
  up by the Matrix, not the module, each time a connection is made, and only the addresses that passed the check are
  dialled (each in turn, when a name has more than one, such as an IPv6 and an IPv4 address), so a name can't
  be pointed somewhere else in between; the module never names an address
  or a port at all, so there is nothing for it to forge. The address in use is shown in the module's card.
  What this leaves: **the module can say anything to that one device on that one port.** If the device passes
  things on (a proxy, a router that forwards the port), the module reaches what it passes on. Name a device
  you would let its author talk to.
- **Secrets.** A password typed into its card is kept on the Matrix (`data/secrets.json`, readable only by the
  Matrix), never sent back to a browser, never in a backup, and blanked out of anything the module prints.
  But it is given to the module's code, which could send it wherever it can reach: its listed sites and its
  one device. Give it a password made for it, not one you use elsewhere.
- **Staying on.** A module's screen may keep the panel while it says something is live (`stay`, above): at most
  4 hours in any 12, never in the sleep hours, and Next moves on. Only a module whose bongo-module.json asks for it ("abilities": {"stay": true}), which the import review says; the Now page says why it's staying.
- **"Any site" and the home address.** Unticking "Only allow the sites it lists" still gives a module the
  whole of the Matrix's network, as before; its one address then means nothing.

## A settings page of your own (built-in modules)

What is the same for every screen of a module (the Pet's animal, colour and name) can have a page of its own in the
dashboard, at `/modules/<id>`, reached from the module's row in Settings > Modules ("Pet settings ›"). A module that
comes with the Matrix declares it:

```python
Module(id="pet", ...,
       settings_page={"title": "Pet", "card": "Your pet", "script": "petpage.js",
                      "blurb": "Animal, colour, name and outfits are set once for the whole Matrix, on the Pet page."})
```

- `title` and `script` are required. `script` is a bare file name in the dashboard's `js/` folder (`[a-z0-9_-]+.js`,
  never a path or an address) that exports `render(view)`, and optionally `rowNote(on)`: the line shown beside the
  button in Settings > Modules (it may be async).
- `card` and `blurb` (optional) are the title and words of the card each screen's editor shows with a "Pet settings ›"
  button; the screen's own card is then called "This screen". They are plain words: up to 40 and 300 characters, with no
  `<`, `>` or `&` and no control or text-direction characters. No other key is accepted. `Module(...)` raises
  `ValueError` for any of this; when modules are discovered, a module with a bad declaration is skipped with a warning
  that names it (the others still load), and the dashboard checks the script name again before it imports anything.
- `page_links={"pet": "pet"}` (a style name and a module id, each `[a-z0-9_]{1,40}`): this module's style `pet` also shows the pet module's card in its editor (the Clock's
  Pet style uses the Matrix's one pet).
- Imported modules cannot declare a page (the Matrix ignores it: they have a settings card, see below).
- Keep each part of the page saving on its own (`saveConfig`), never with Apply to Matrix, and show only what exists.

## Background modules, controls and the home network

An imported module can ask for these things beyond drawing its screens (and `"stay": true`, keeping the panel
while something is live: see "Staying on while something is live" above). Each is asked for in its
`bongo-module.json`, shown in plain words on the import review before "I trust" can be ticked, listed on its
row in Settings > Modules afterwards, and given only if it was asked for and agreed to. A module that asks
for none of them runs exactly as described above. Built-in modules don't use any of this.

```json
{
  "id": "garage_door",
  "name": "Garage Door",
  "version": "1.0.0",
  "author": "sam-k",
  "description": "Shows the garage door, and dims the panel when the house is asleep.",
  "api": 2,
  "abilities": {
    "background": true,
    "controls": ["status", "sleep", "brightness", "playback", "message"],
    "home_address": {"label": "Hub address", "ports": [8000, 8443]},
    "settings": {
      "fields": [
        {"name": "username", "type": "text", "label": "Username"},
        {"name": "password", "type": "text", "label": "Password"},
        {"name": "secure", "type": "bool", "label": "Encrypted (TLS)", "default": true}
      ],
      "table": {"title": "What the hub gets", "rows": [["Panel: on is awake, off is asleep", "switch"], ["Screen now playing", "sensor"]]},
      "note": "The module only connects to the hub named here."
    }
  }
}
```

The manifest is checked strictly: an ability, a control or a field type this Matrix doesn't know is refused
with the reason, never skipped. A later version that asks for more (another control, another port, a
background task) is shown as "New in this version, it asks to: ..." and installed only when agreed to again;
the daily look for updates never installs anything.

### `background`: working while no screen of yours is on

```python
from bongo.module import Background, Module

class Bridge(Background):
    every = 2                                   # seconds between ticks: 1 to 3600 (5 if left out)

    def start(self):                            # once, before the first tick
        self.sock = None

    def tick(self, now):                        # every ``every`` seconds, in your module's own process
        ...
        self.report("Connected")                # your one-line status (below)

    def settings_changed(self):                 # someone saved your card: self.settings is the new one
        self.sock = None

    def stop(self):                             # optional: switched off, removed, updated, or the Matrix stopping
        if self.sock:
            self.sock.sendall(b"offline\n")      # a last word: 2 seconds at most

MODULE = Module(id="garage_door", name="Garage Door", styles={}, background=Bridge)
```

- `"abilities": {"background": true}` and `Module(background=YourClass)` go together: one without the other
  fails the import, so the review always says what the code does.
- It starts when the module is switched on (and when the Matrix starts), and stops when the module is
  switched off or removed: off still means not loaded or run at all.
- `stop()` (optional) is called once when the module is switched off, removed or updated to a new version,
  or the Matrix is stopping: the place to say goodbye (close a connection, tell the far end you are going
  offline). It has **2 seconds**; past that your process is stopped anyway, and that is said in the log once.
  It isn't called after a crash or a hang (your process has gone already), nor when the owner changes the
  "Only allow the sites it lists" switch (the process is simply started again). Controls asked for in
  `stop` are not done. `start` runs again when the module next starts.
- `tick(now)` gets the Matrix's time (in its time zone). **Do a little and return**: one that takes longer
  than 5 seconds is taken for a hang. Your process is then stopped and started again after a rest (5 seconds,
  then 30, then 5 minutes, then every half hour), `start` runs again, and anything you kept in memory is
  gone. The same happens if it crashes. A `tick` that raises is logged and tried again after the same rests,
  without the restart. So: no `time.sleep`, short timeouts on anything that waits (`home_connect(timeout=3)`,
  `sock.settimeout(...)` or a non-blocking socket), and keep what must last in a file in your working folder.
- The first tick comes straight after `start`, and both together have the 5 seconds.
- It runs in the same process as your screens, so they can share what you keep at module level (a dict the
  task fills and a screen draws). While a tick runs, a screen of yours that is on the panel shows its last
  frame again; that is one more reason to keep ticks short. No other module's screen is ever affected.
- **A module with a background task may have no screens at all**: `styles={}`. It is then not offered in Add a
  module and has no playlist entry; it shows in Settings > Modules ("On: it works in the background") and has
  its card. A module without a background task still needs at least one style.
- The limits of every imported module apply as before: 300 MB of memory, 100 MB on disk, 32 processes or
  threads, output cut to 20 lines every 10 seconds.
- What it prints (`print`, or anything written to stderr: a traceback too) goes to the Matrix's log, one line
  at a time, each starting with `[your_module_id]` (`[garage_door] connected to 192.168.1.60`), at most 500
  characters a line and 20 lines every 10 seconds, with any of its saved secrets blanked out as `(secret)`.

### `controls`: working the panel

Name the ones you use; you get only those, and the review lists each one.

| in `"controls"` | on `self.panel` | what it does | limit, per module |
| --- | --- | --- | --- |
| `"status"` | `self.panel.status` | a dict, fresh at every tick (below) | none: it is only read |
| `"sleep"` | `.sleep(minutes=None)`, `.wake()` | Sleep now and Wake now. `minutes` 1 to 10080; left out: until the sleep hours next start or end, or a week if there are none | one a second, and 6 (sleep and wake together) in ten minutes |
| `"brightness"` | `.brightness(value)` | 1 to 100, as if set by hand. A brightness set by hand stays until you switch back to Auto; with Follow daylight on (Settings > Device) it also ends by itself at the next sunrise, sunset or sleep change. `None`: back to Auto | one a second, 60 in ten minutes |
| `"playback"` | `.pause()`, `.resume()`, `.next()` | the Now page's Pause, Resume and Next | one a second, 60 in ten minutes |
| `"message"` | `.message(text, title="", seconds=10)` | a few words on the panel, like Show on the Matrix: text up to 120 characters, title up to 40, 3 to 30 seconds | one every 5 seconds, 10 in ten minutes |

- `self.panel.status` is `{"asleep": bool, "paused": bool, "brightness": 1 to 100 or None, "brightness_by_hand":
  bool, "brightness_set_by": None or "dashboard" or a module's id, "screen": "Clock" or None, "module": "clock"
  or None, "style": "big" or None}`: what the Now page shows. `None` without the `status` control. Nothing of
  any module's settings, and no picture.
- `brightness_set_by` says who set the brightness in force by hand: `None` when nobody has (Auto),
  `"dashboard"` when the owner did (even to the same level yours set), or the id of the module that did. So
  `status["brightness_set_by"] == "garage_door"` (your own id) means your brightness is still the one in force.
- Calling one of them only notes what you want; the Matrix does it when `tick` returns, in order, at most 8
  from one tick. Nothing is returned. Anything refused (a control you didn't ask for, too soon, too many, a
  value out of range) is in `self.panel.refused` at your next tick, as `[{"do": "sleep", "why": "..."}]`, and
  in the log once. A refusal for being too soon or too many is not kept for later: ask again if it still matters.
- Two changes inside one second are refused on purpose (nobody can strobe the panel), so send the latest
  value, not every step of a dimmer being dragged.
- Each one done is a line in the Logs ("module garage_door: put the panel to sleep"; a brightness at most
  every half minute), and the Now page says which module a sleep or a brightness came from.

### `home_address`: one device on the home network

```python
from bongo.module import home_connect

sock = home_connect(timeout=3)      # a connected TCP socket to the address in your settings, or OSError
```

- `"home_address": {"ports": [8000, 8443], "label": "Hub address"}`: 1 to 4 port numbers (no ranges), and
  the words over the address box (up to 30 characters; "Address" if left out).
- Your card gets two fields of the Matrix's own for it, always first, which you read like the rest:
  - `address`, type `"host"`, with your `label` over it: `self.settings["address"]` is the name or IP address
    the owner typed (lower-case, without spaces, `""` until they type one).
  - `port`, type `"port"`, labelled "Port": a pick list of your ports (a fixed box when there is only one).
    `self.settings["port"]` is a number, and **the first port you list until the owner saves another**.
  Don't name fields of your own `address` or `port`.
- The address is checked when the card is **saved**: anything but a plain name or IP address (`http://...`,
  `name:8000`, a path) is refused there, and so is an IP address that can never be allowed (this Matrix's own,
  127.0.0.1, 0.0.0.0, link-local, broadcast such as 192.168.1.255, multicast, the internet). The save fails as a
  whole with the reason ("Hub address: 192.168.1.50 is this Matrix itself") and nothing changes. A **name**
  can only be checked when it is used: it is saved as typed, and looked up each time you connect.
- `home_connect()` takes no address: the Matrix dials the address and port in the settings, and nothing else
  on the home network can be reached, by any other means (`socket.create_connection` to it fails: your
  process has no network of its own). It works from the background task and from a screen.
- It is a plain TCP stream. For TLS, wrap it yourself:
  `ssl.create_default_context().wrap_socket(sock, server_hostname=self.settings["address"])` (a home device
  often has a certificate of its own making: that is yours to decide and to offer as a setting). For plain
  HTTP to a device, give `http.client.HTTPConnection` the socket (`conn.sock = sock`); `urllib` and the proxy
  settings are for the internet sites in `network` and never reach the home network.
- When it can't connect it raises one of these. `str(error)` is the reason in words, the same words the card
  shows under your status line as "Not connected: ...":

  | exception (all are `OSError`) | when | `str(error)`, for example |
  | --- | --- | --- |
  | `PermissionError` | the Matrix won't dial it: no address set, an address that is never allowed, or a name that only leads to one | "no home-network address is set in its settings", "nas.local is this Matrix itself", "printer.example.com is on the internet, not your home network" |
  | `PermissionError` (`errno` `EAGAIN`) | too many tries or connections just now | "too many connections to its home-network address: try again in a minute" |
  | `socket.gaierror` | the name can't be found | "couldn't find nas.local on the network" |
  | `ConnectionRefusedError` | the device is there but nothing listens on that port | "nas.local refused the connection on port 8000" |
  | `OSError` (`errno` `EHOSTUNREACH`) | no route to it (another network, or it's switched off) | "there's no way to nas.local from this Matrix just now (no route)" |
  | `TimeoutError` | no answer within `timeout` seconds (5 at most) | "nas.local didn't answer on port 8000" |

  Catch `OSError` for all of them, and report it: `self.report(f"Not connected: {error}", ok=False)`. Your
  card also shows the latest reason on its own (once: not again if your status already says it).
- Refused whatever is typed: this Matrix itself, link-local, multicast and broadcast addresses, and anything
  on the internet (see "Accepted risks"). Up to 4 connections open at once and 20 tries a minute; don't
  reconnect in a tight loop. When the owner changes the address, your open connection is closed and
  `settings_changed` is called.
- On the import's test screen (before "I trust") there is no address, and `home_connect` raises.

### `settings`: a card of your own in Settings, with secrets and a status line

A module's own settings are one set for the whole module, not one per screen: the dashboard builds a card
for them, titled with the module's name, under Settings > Modules.

- `fields`: up to 12, each `{"name", "type", "label"}` (name: lower-case letters, digits and `_`; label: up
  to 30 characters), with `"note"` (a line under it) and `"default"` if wanted. Types: `"text"` (`"max"`
  length, up to 200, 100 if left out), `"bool"` (a switch), `"int"` and `"number"` (both need
  `"limits": [lowest, highest]`), `"choice"` (needs `"choices": [...]`, up to 20).
- **The order on the card** is fixed: first the address and port (if you asked for `home_address`), then your
  boxes (text, secret, number and choice fields) in the order you list them, two to a row on a wide screen and
  one under another on a phone, then your switches (`"bool"`) in the order you list them, then Save, then the
  status line, then the `table` and the `note`. There is no grouping and no way to hide a field until a switch
  is on: list them in the order that reads best, and say in a field's `note` when it only matters with a switch.
- `table` (`{"title": ..., "rows": [["what", "tag"], ...]}`, up to 8 rows) and `note` (up to 400
  characters) are for saying what the module does with what it is given. Plain text only.
- **A secret** (a password, a token) is a `"text"` field with no default that the module names in
  `Module.secrets`, like any other secret setting: `Module(..., secrets=("password",))`. The card then masks
  it; once saved it is never sent back to a browser, only shown as saved, with Replace and Clear. The Matrix
  keeps it with every module's secrets (`data/secrets.json`, `bongo/private.py`), never in the config, so
  never in a backup. It is forgotten when the module is removed, and blanked out of whatever your module
  prints or reports. The review says the module keeps a secret, by its label ("Keep a secret you type in
  (Password)"). You declare it as `"type": "text"`; everywhere after the import (the card, and
  `GET /api/addons/<id>/settings`) it is a field of type `"secret"`, and only whether one is saved is ever shown.
- Your code gets them read-only: `self.settings` in the background task (`{"address": ..., "port": ...,
  "username": ..., "password": ..., "secure": ...}`, a secret as its value or `""`), and
  `self.ctx.module_settings` in a screen. `settings_changed()` is called before the next tick after a save.
  A module can't change its own settings.
- On the import's test screen your screens get the defaults (`""` for a secret), never what was typed into an
  installed version. Draw something sensible with nothing set.
- **Status**: `self.report("Connected")` or `self.report("Wrong password", ok=False)` from the background
  task. One line of plain text, up to 80 characters, shown in the card and on the module's row with the
  dashboard's square dot (green, or amber for `ok=False`); `""` takes it away. If your task isn't running the
  Matrix says so there instead ("Not running: ...").

### When the module is off, and what the owner sees

- Off: nothing of it runs, its connection is closed, its row says "Off: not running" (or "Off: hidden from
  Add a module, ... not running"), its card stays, says "Switched off in Modules: it isn't running. Its
  settings are kept.", and can still be filled in.
- On: its row says "On: it works in the background" and shows its status line; "What it can do, and removing it" lists what it was allowed.
- The Now page shows a sleep, a pause or a brightness from a module exactly as if done by hand, and adds
  "Put to sleep by <name>." or "Brightness set by <name>." followed by the line on how long a brightness set by hand lasts.
- The Logs page has a line for each thing a module did with a control, each refusal once, and one line when
  its background task stops and when it is running again.

### An older Matrix

A Matrix from before this (module API 1) ignores `"api"` and `"abilities"` in the manifest and knows
nothing of `Background`. An author chooses:

- **Refuse to import there, with a clear reason.** Put this at the top of `__init__.py`; the import then
  fails on its test screen with your words in the message:

  ```python
  import bongo.module
  if getattr(bongo.module, "API", 1) < 2:
      raise ImportError("This module needs a newer Matrix: update it in Settings > Software, then import it again.")
  ```

- **Or stay importable**, as a module with screens only: import `Background` and `home_connect` inside
  `try: ... except ImportError:`, and pass `background=` and `secrets=` to `Module` only when
  `"background" in Module.__dataclass_fields__` (the same test as for `actions`).

From this version on the Matrix checks for itself: `"api": 3` on a Matrix whose API is 2 fails the import
with "it needs a newer Matrix than this one ...", and so does an ability or control name it doesn't know.
State `"api": 2` when you use anything in this section.

### A whole one, small

`bongo-module.json` as above. `__init__.py`:

```python
"""Garage Door: sleeps the panel when the hub says the house is asleep, and tells the hub what's playing."""
import json

import bongo.module
if getattr(bongo.module, "API", 1) < 2:
    raise ImportError("This module needs a newer Matrix: update it in Settings > Software, then import it again.")
from bongo.module import Background, Module, Style, home_connect

SEEN = {"door": "?"}                               # filled by the background task, drawn by the screen


class Bridge(Background):
    every = 2

    def start(self):
        self.sock, self.buffer, self.told = None, b"", None

    def settings_changed(self):
        self.hang_up()

    def hang_up(self):
        if self.sock is not None:
            self.sock.close()
        self.sock, self.buffer, self.told = None, b"", None

    def tick(self, now):
        if not self.settings["address"]:
            self.hang_up()
            return self.report("Type the hub's address above", ok=False)
        try:
            if self.sock is None:
                self.sock = home_connect(timeout=3)
                self.sock.sendall(f"login {self.settings['username']} {self.settings['password']}\n".encode())
                self.sock.setblocking(False)
            try:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("the hub hung up")
                self.buffer += data
            except BlockingIOError:
                pass                               # nothing new: fine
            *lines, self.buffer = self.buffer.split(b"\n")
            for line in lines:
                word, _, rest = line.decode().strip().partition(" ")
                if word == "night":
                    self.panel.sleep()
                elif word == "morning":
                    self.panel.wake()
                elif word == "dim":
                    self.panel.brightness(int(rest))
                elif word == "door":
                    SEEN["door"] = rest
                    self.panel.message(f"Garage door {rest}", seconds=8)
            if self.panel.status != self.told:     # tell the hub when something changed
                self.told = self.panel.status
                self.sock.sendall(json.dumps(self.told).encode() + b"\n")
        except OSError as error:
            self.hang_up()
            return self.report(f"Not connected: {error}", ok=False)
        self.report("Connected to the hub")


class Door(Style):
    fps = 2

    def render(self, now):
        img = self.ctx.canvas()
        self.ctx.fonts("6x10").draw(img, 2, 12, SEEN["door"].upper(), (255, 181, 71))
        return img


MODULE = Module(id="garage_door", name="Garage Door", styles={"door": Door}, background=Bridge,
                secrets=("password",),
                description="Shows the garage door, and dims the panel when the house is asleep.")
```

The tests' own example is `tests/fixtures/addons/echo_bridge` (with `tests/test_module_abilities.py`, which
plays the device it talks to).
