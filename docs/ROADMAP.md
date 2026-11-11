# v3 remaster roadmap

v2 is frozen on the `archive/v2` branch (last commit `37f1395d`). Each step below
ports one area from v2, rewritten small and as its own module where it is a
screen. The UI follows the remaster design canvas (dashboard, module editor,
phone remote, pet screens, logs).

## Done

- [x] **Core**: module contract, registry, player, config, BDF fonts,
      hardware panel, recording panel + LED-look GIF previews, CLI.
- [x] **Lava Lamp**: `classic` (v2 look and all 37 v2 palettes) and `metro`
      (refreshed look, 5 gradient palettes); custom palettes are three colours, shared by both.
- [x] **Safe area**: configurable margins for a frame that hides the panel
      edges; modules draw only in the visible part. `bongo calibrate` test card.
- [x] **Settings schema**: types, colours and choices inferred from defaults;
      bad settings rejected with readable messages.
- [x] **Splash and sleep** (v2 behaviour): Splash shuffles through its files, one GIF pass each; Sleep keeps one random pick all night at its own brightness.
- [x] **Dashboard**: now playing with live preview, filmstrip playlist, module editor
      built from the settings schema, safe-area editor, logs, phone layout; PIN pairing.
- [x] **Overlays**: styles drawn over the playing module on a timer; Announcements `ribbon` and Pet `wander`.
- [x] **Hardware lock**: one process drives the panel; a second `run`/`calibrate` stops with a clear message.
- [x] **Weather**: `overview`, `ticker`, `forecast` (Open-Meteo, keyless).
- [x] **Text**: `static`, `scroll`.
- [x] **Countdown**: `days`, `clock`, `flip`, `ring`, `tearoff`; a list of events (the soonest unfinished one shows), holiday targets.
- [x] **Media**: `fit`, `fill`, `pixel` for images, GIFs and video (ffmpeg).
- [x] **Announcements**: `banner`, `card`, `flash`, scheduled by day and hour.
- [x] **News (RSS)**: `ticker`, `headline`, `picture` (thumbnails).
- [x] **Calendar**: `page`, `month`, `progress`; holidays shared with Countdown.
- [x] **Sky (astronomy)**: `moon`, `sun`, without the 30 MB ephemeris.

## Screens (one module each, styles where v2 had variants)

- [x] **Pet**: the cat from the Cat 10 art; every engine mode is a style, `life` plays the ticked ones each day, `wander` overlay walks her across other screens.

## Core features still to add

- [ ] **First-run Wi-Fi** hotspot portal.

## Left behind on purpose

v2's release-control and priority transaction scripts, the physical-CI
dispatcher, sharded CI and SAST baselines are not coming forward. Bring any of
them back only when v3 actually needs it; they're all on `archive/v2`.
