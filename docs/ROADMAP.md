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

## Screens (one module each, styles where v2 had variants)

- [ ] **Weather**: styles `ticker` (v2 Weather), `overview`, `forecast`
      (v2 OpenWeather modules). Provider is a setting: Open-Meteo (keyless) or
      OpenWeather (key kept in `data/secrets/`).
- [ ] **Text**: v2 Custom text (font, colour, position, speed, loops).
- [ ] **Announcements**: scheduled static/scrolling notices, icon, banner,
      full-image. Also powers "push a message" from the dashboard.
- [ ] **Media**: images, GIFs, video, protected built-in library
      (`resources/media/built-in`).
- [ ] **Countdown**: v2 event countdown.
- [ ] **Calendar**: v2 calendar layouts, holidays, progress styles.
- [ ] **RSS**: feeds, banners, thumbnails.
- [ ] **Astronomy**: moon phases and events (`resources/astronomy`).
- [ ] **Pet**: the cat (remastered Cat 10 sprites), needs, modes.

## Core features still to add

- [ ] **Overlays**: things drawn over whatever module is playing (pet
      wandering, pushed messages, pairing PIN). One small layer in the player.
- [ ] **Splash and sleep**: startup screen and night schedule, as playlist
      rules rather than modules.
- [ ] **Settings schema**: give `defaults` types/ranges so the dashboard can
      build every module's editor automatically, with no per-module UI code.
- [ ] **Web control panel** per the design refresh: now playing with live
      preview (frames from the same `render`), filmstrip playlist, module
      editor, phone remote, logs.
- [ ] **Pairing** (PIN on the panel) and **first-run Wi-Fi** hotspot portal.
- [ ] **Brightness schedule** and single-owner hardware lock.

## Left behind on purpose

v2's release-control and priority transaction scripts, the physical-CI
dispatcher, sharded CI and SAST baselines are not coming forward. Bring any of
them back only when v3 actually needs it; they're all on `archive/v2`.
