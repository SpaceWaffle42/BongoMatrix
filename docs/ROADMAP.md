# v3 remaster roadmap

v2 is frozen on the `archive/v2` branch (last commit `37f1395d`). Each step below
ports one area from v2, rewritten small and as its own module where it is a
screen. The UI follows the remaster design canvas (dashboard, module editor,
phone remote, pet screens, logs).

## Done

- [x] **Core**: module contract, registry, player, config, BDF fonts,
      hardware panel, recording panel + LED-look GIF previews, CLI.
- [x] **Lava Lamp**: `classic` (v2 look and all 37 v2 palettes) and `metro`
      (refreshed look, 5 gradient palettes).
- [x] **Safe area**: configurable margins for a frame that hides the panel
      edges; modules draw only in the visible part. `bongo calibrate` test card.
- [x] **Settings schema**: types, colours and choices inferred from defaults;
      bad settings rejected with readable messages.
- [x] **Splash and sleep**: playlist rules with their own brightness.
- [x] **Weather**: `overview`, `ticker`, `forecast` (Open-Meteo, keyless).
- [x] **Text**: `static`, `scroll`.
- [x] **Countdown**: `days`, `clock`, `flip`, `ring`, `tearoff`; holiday targets.
- [x] **Media**: `fit`, `fill`, `pixel` for images and GIFs.
- [x] **Announcements**: `banner`, `card`, `flash`, scheduled by day and hour.
- [x] **News (RSS)**: `ticker`, `headline`.
- [x] **Sky (astronomy)**: `moon`, `sun`, without the 30 MB ephemeris.

## Screens (one module each, styles where v2 had variants)

- [ ] **Calendar**: v2 calendar layouts, holidays, progress styles.
- [ ] **Media video** and **RSS thumbnails** (left out of the first port).
- [ ] **Pet**: the cat (remastered Cat 10 sprites), needs, modes.

## Core features still to add

- [ ] **Overlays**: things drawn over whatever module is playing (pet
      wandering, pushed messages, pairing PIN). One small layer in the player.
- [ ] **Web control panel** per the design refresh: now playing with live
      preview (frames from the same `render`), filmstrip playlist, module
      editor, phone remote, logs.
- [ ] **Pairing** (PIN on the panel) and **first-run Wi-Fi** hotspot portal.
- [ ] **Single-owner hardware lock** (brightness schedule is done, in the sleep rules).

## Left behind on purpose

v2's release-control and priority transaction scripts, the physical-CI
dispatcher, sharded CI and SAST baselines are not coming forward. Bring any of
them back only when v3 actually needs it; they're all on `archive/v2`.
