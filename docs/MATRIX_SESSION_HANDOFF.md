# Raspberry Pi RGB Matrix — Fresh-session handoff

Generated: 2026-08-04T00:04:14+00:00 (Pi timezone: Europe/London)

## Purpose

Read this file first in a fresh Hermes session. It records the current verified state, operating constraints, deployment procedure, and remaining roadmap so work can continue without relying on chat context.

## User expectations

- Finish deployment and real verification before starting another feature.
- Validate changes on the Raspberry Pi and physical 64×32 Matrix, not only with mocks or local tests.
- Restart/refresh `matrix-web.service` after every major deployed change, then verify the live service, logs, process layout, and hardware lock.
- Prefer friendly controls: searchable location selection and clickable groups rather than raw technical inputs.
- Never store, repeat, or log pairing PINs. Represent any credential as `[REDACTED]`.

## Hosts and paths

- Live host: `192.168.1.4`
- SSH user: `admin`
- SSH key: `/root/.ssh/id_ed25519_matrix` (never expose its contents)
- Application URL: `http://192.168.1.4:5000`
- Local source: `/root/matrix_new`
- Deployed source: `/home/admin/matrix`
- Deployed venv: `/home/admin/matrix/venv`
- Persistent configuration/data: `/home/admin/matrix/data`
- Persistent media: `/home/admin/matrix/resources/media`
- Active unit: `/etc/systemd/system/matrix-web.service`
- Hardware lock: `/run/matrix-display/matrix-display.lock`
- This local directory is **not a Git repository**.

Always preserve the deployed `venv`, `data`, and `resources/media` directories during synchronization.

## Current live health

Verified at the latest handoff update:

- `matrix-web.service`: `active`
- `MainPID`: `408482`
- `NRestarts`: `0`
- Hardware lock present at `/run/matrix-display/matrix-display.lock`
- Saved custom lava palettes: `1`
- Saved palette ID: `palette_migrated`
- Legacy custom source enabled: `False`
- Weather units: `celsius`
- Weather layout: NOW `(1,7)`, temperature `(2,17)`, separator height `19`, ticker height `28`
- Weather ticker defaults: direction `left`, speed `normal`
- Custom module count: `1`; the saved scrolling module uses `10` loops, `0.05s` frame delay, leftward movement, and an automatically derived nominal runtime of `62.5s`.
- Maximum paired devices: `4` (validated range `1..10`)
- Paired-device registry count: `4`; these trusted devices were preserved during verification.
- Persistent config SHA-256: `aab2dd9c9c5cc3a507edba192c3c0bb83d67c9ee7353c3741ba77e2e0b496c79`; the only change from the pre-auto-runtime backup is the intended scrolling playlist duration migration from `15` to `62.5` seconds.
- Latest source checksum dry-run showed no local/deployed file drift.
- Latest warning-level service journal query showed no entries.

PIDs and pairing PINs are ephemeral; re-check them rather than treating the values above as permanent.

## Latest completed feature: automatic scrolling-text runtime

- Simple `text_scroll` playlist time is no longer a manually editable scheduler ceiling. It is derived from `(64 + rendered text width + 1 boundary frame) × loops × frame delay`.
- The allowed simple fonts are monospaced; server and browser use their exact BDF advances (`4`, `5`, `6`, `7`, and `9` pixels). Python and JavaScript share positive half-up millisecond rounding, including the reviewed `0.7935 -> 0.794` boundary.
- The playlist displays a live read-only `Auto seconds` value for scrolling text. Editing text, font, mode, loops, or frame delay refreshes it in both the simple editor and advanced-canvas integration path. Built-ins, static text, and canvas modules retain editable seconds.
- The server is authoritative and overwrites contradictory submitted scrolling durations. The renderer ignores legacy/manual duration for scrolling text and completes every configured traversal unless its stop event interrupts it; left and right movement have identical frame semantics, including one final frame delay.
- TDD red phases proved the old manual value was retained, a tiny manual duration truncated all loops, the automatic UI was absent, and Python/JavaScript disagreed on half-millisecond rounding. Focused tests passed after each minimal correction.
- Final local suite: `Ran 161 tests ... OK`. Final Raspberry Pi suite: `Ran 161 tests ... OK (skipped=2)`. Python compilation, local static/rendered JavaScript syntax, and the fresh targeted ad-hoc verifier passed.
- Independent review initially found the cross-language rounding mismatch; a red regression reproduced it, positive half-up rounding fixed it, and the fail-closed follow-up review passed with no security concerns or logic errors.
- Live physical hardware verification drove both directions through two complete traversals despite a `0.001s` legacy/manual duration: `LIVE_PHYSICAL_AUTO_SCROLL_PASS left=2.431s right=2.433s expected=1.460s frames_each=146 revision_delta=292 manual_duration=0.001s`. The frame count, not the obsolete manual ceiling, controlled completion; wall time includes physical draw overhead in addition to configured frame delay.
- The existing user scrolling module migrated from manual `15s` to derived `62.5s` (`10` loops, `0.05s` frame delay) on service refresh. A semantic comparison confirmed this was the only configuration change.
- Final service state: active, expected HTTP `302`, `MainPID=408482`, lock owner `408486`, second root lock blocked, three Gunicorn processes, `NRestarts=0`, and warning journal count `0`.
- Four trusted paired devices, maximum device limit `4`, and all five media files were preserved. Confirmed backup: `/home/admin/matrix-pre-auto-scroll-20260804-005609`.

## Previous completed fix: readable dropdown colours

- Fixed native dashboard `<select>` controls whose opened menu could render white option text on a white background.
- Dashboard selects now explicitly use the dark `--surface` background and `--text` foreground, set `color-scheme: dark`, and apply the same contrasting colours to both `<option>` and `<optgroup>` rows.
- TDD red phase: `test_dashboard_dropdowns_have_contrasting_background_and_text` failed because the explicit dropdown rules were absent. The same test passed after the minimal CSS change.
- Final local suite: `Ran 158 tests ... OK`. Final Raspberry Pi suite: `Ran 158 tests ... OK (skipped=2)`.
- Fresh targeted ad-hoc verification passed locally and against the deployed template: `AD_HOC_DROPDOWN_CONTRAST_PASS background=#191a1b text=#f7f8f8 ratio=16.38:1 options=styled optgroups=styled`.
- The Matrix web service was refreshed after deployment. Final state: active, HTTP `/` returned the expected `302`, `MainPID=407093`, lock-owning worker `407100`, second lock acquisition blocked, `NRestarts=0`, and warning-level journal count `0`.
- Persistent configuration and paired-device registry remained byte-identical to their pre-change backups; paired count and limit both remained `4`. Source content drift is `0` (the two deployed files differ only in mtime metadata).
- Confirmed backups (`20260804-004148`, Pi local time):
  - `/home/admin/matrix-dashboard-pre-dropdown-contrast-20260804-004148.html`
  - `/home/admin/matrix-test-webapp-pre-dropdown-contrast-20260804-004148.py`
  - `/home/admin/matrix-config-pre-dropdown-contrast-20260804-004148.json`
  - `/home/admin/matrix-paired-pre-dropdown-contrast-20260804-004148.json`

## Previous completed feature: media-library preview on the physical Matrix

- The Media library `Preview` action no longer uses `window.open`, `_blank`, or navigates to a raw media file in a new browser tab.
- The browser now sends paired `POST /api/media/<safe media reference>/preview`; the action reports that the selected file is being previewed on the physical Matrix.
- The endpoint accepts uploaded media and `built-in/<name>` references, reuses the existing strict filename/path confinement, requires a regular non-symlink file, requires an active paired-device session, and returns `404` for missing or unsafe media.
- `DisplayController.preview_media()` queues one one-off preview without changing `config.media`, media layout, custom modules, or the playlist. Static images, GIFs, and MP4s use the shared interruptible media player with a 15-second preview ceiling.
- Module and media preview queues replace each other so the newest explicit preview wins. Queueing and interruption are atomic with scheduler module-stop registration, preventing an inactive sleep/scheduled module from delaying a requested media preview for its full duration.
- TDD red phase failed for all three original requirements: no controller media-preview API, dashboard still opened a new tab, and the media preview HTTP route returned `405`.
- Focused tests passed after implementation. A reviewer then found a lost-interrupt scheduler race; a deterministic red regression test reproduced it, the queue/stop registration was fixed, and the corrected focused tests passed.
- Final local suite: `Ran 157 tests ... OK`. Final Raspberry Pi suite: `Ran 157 tests ... OK (skipped=2)`. Python compilation, static JS syntax, rendered dashboard JS syntax, and the fresh ad-hoc API/controller/browser-path verifier passed.
- Added route tests cover ordinary uploads, built-in media, missing files, and symlink rejection. Added controller tests cover one-off media dispatch and scheduler race prevention. Dashboard tests require `previewMedia(item.name)` and reject every `window.open(` call.
- Independent final review passed with no security concerns or logic errors. It confirmed delete/re-upload remains confined and re-sanitized; deletion can only make playback fail safely. Static added-line security scan had zero findings.
- Authenticated live physical result: `LIVE_PHYSICAL_MEDIA_PREVIEW: PASS media=built-in/hermes_self_portrait.png frame_revision=1404 frame_matches_source=true config_unchanged=true new_tab=false`. The selected 64×32 PNG matched the exact production framebuffer sent to the physical display.
- Persistent config was semantically and byte-for-byte unchanged at SHA-256 `5f973631081d4cd6354067d26535d32e094b5d8e16dffa1fa0b1a024e38665bc`. All five media files matched the pre-deployment SHA-256 manifest. The four trusted paired devices returned byte-identically to SHA-256 `a481c7e2ef715d80e962a5db0c3f6d6018c84baa553ad4ae6260b96aa4d249d6`.
- The temporary verifier was revoked, the paired-device limit was restored to `4`, verifier cookies/scripts were removed, and the service was restarted/refreshed after cleanup.
- Final service state: active/running, `MainPID=406384`, worker/lock owner `406390`, `NRestarts=0`, expected HTTP `302`, blocked second lock claim, no warning-level journal entries, four trusted devices, zero verifier devices, unchanged media, and `SOURCE_FILE_DRIFT_COUNT=0`.
- Confirmed backups (`20260804-002216`, Pi local time):
  - `/home/admin/matrix-source-pre-media-display-preview-20260804-002216.tar.gz`
  - `/home/admin/matrix-config-pre-media-display-preview-20260804-002216.json`
  - `/home/admin/matrix-paired-pre-media-display-preview-20260804-002216.json`
  - `/home/admin/matrix-media-pre-media-display-preview-20260804-002216.sha256`
- Deployment note: the first staged-copy command retained the staging prefix and wrote an unused nested copy under `/home/admin/matrix/tmp/matrix-media-preview-deploy`; checksums caught this before service restart. The corrected copy deployed the five intended files, verified all five checksums, and removed the stray nested directory.

## Previous completed feature: bounded custom scrolling-text loop counts

- Simple custom scrolling-text modules now have a `loops` setting.
- Missing/legacy values migrate to `1` automatically when configuration is validated or reopened.
- Only non-boolean integers from `1` through `10` are accepted.
- The renderer counts complete screen traversals independently for left- and right-moving text and stops repeating after the saved count. The later automatic-runtime change supersedes the former manually entered playlist-duration upper bound for simple scrolling text.
- The simple custom-module editor exposes a friendly `Scroll loops` number control with minimum `1`, maximum `10`, and step `1`; editor serialization and newly created modules both default to `1`.

### Verification and deployment evidence

- Strict TDD began with three focused failing regressions, then passed after implementation. Additional review-suggested coverage verifies rightward traversal and reopen-from-disk legacy migration.
- Complete local suite: `154/154` passing. Complete deployed Pi suite: `154/154` passing.
- Local changed-Python compilation, static `custom_canvas.js` syntax checking, and rendered inline JavaScript syntax checking passed. Deployed Python compilation passed.
- Fresh local ad-hoc result: `AD_HOC_SCROLL_LOOPS: PASS default=1 max=10 left_loops=3 left_frames=18 right_loops=2 right_frames=12`.
- Fresh deployed ad-hoc result: `DEPLOYED_AD_HOC_SCROLL_LOOPS PASS left_loops=3 right_loops=2`.
- An independent pre-deployment reviewer passed the seven-file diff with no security concerns or logic errors. Its two non-blocking test suggestions were implemented before deployment.
- Seven changed files matched local/deployed SHA-256 checksums exactly. Final checksum dry-run reported `SOURCE_FILE_DRIFT_COUNT 0`.
- Authenticated live physical result: `LIVE_PHYSICAL_SCROLL_LOOPS: PASS loops=3 revisions=187 visible_frames=179 x_range=0..63 ui_bounds=1..10`. A temporary one-character module traversed the exact 64×32 physical-output/framebuffer path three times and stopped before a fourth traversal; the production dashboard and external editor asset exposed/serialized the bounded control.
- Two earlier verifier attempts falsely counted more than three traversals: the first detector treated clipped glyph gaps as new loops, while the second placed the verifier in the active playlist and therefore observed the correctly completed preview starting again as a scheduled module. Production code was unchanged; the corrected verifier kept the temporary module out of the playlist and counted right-edge entries.
- The temporary module was restored exactly. Concurrent user changes to `weather.line_y` (`17→19`) and `weather.ticker_y` (`31→28`) were detected and preserved. Final config SHA-256 is `5f973631081d4cd6354067d26535d32e094b5d8e16dffa1fa0b1a024e38665bc`.
- The verifier device was revoked, the device limit was restored to `4`, and temporary cookies/scripts were removed. The four trusted devices returned byte-identically to SHA-256 `a481c7e2ef715d80e962a5db0c3f6d6018c84baa553ad4ae6260b96aa4d249d6`.
- Final service state: active/running, `MainPID=405006`, worker/lock owner `405012`, `NRestarts=0`, expected HTTP `302`, blocked second lock claim, no warning-level journal entries, four trusted devices, and zero verifier devices.
- Pre-deployment backups (`20260803-235339`):
  - `/home/admin/matrix-source-pre-scroll-loops-20260803-235339.tar.gz`
  - `/home/admin/matrix-config-pre-scroll-loops-20260803-235339.json`
  - `/home/admin/matrix-paired-pre-scroll-loops-20260803-235339.json`

## Previous completed feature: full weather element positioning and separate separator/ticker heights

- Weather `NOW`, temperature, high, and low text can now move horizontally from `-63..63`, including partially off the left edge, and vertically across the full `0..31` Matrix height.
- Temperature is no longer restricted to baseline `12..20`; NOW/high/low are no longer restricted to `5..31`.
- Separator and ticker remain independent renderer properties and now appear as separate grouped editor sections, each with its own explicit `0..31` height control.
- The weather editor uses friendly labels such as `horizontal position` and `height`; ticker direction and speed remain grouped with ticker height and colour.
- Existing saved weather values remain compatible and unchanged.

### Verification and deployment evidence

- Strict TDD RED/GREEN regressions cover full-edge schema validation, renderer coordinates, independent separator/ticker heights, and the grouped live editor controls.
- Complete local suite: `149/149` passing. Complete deployed Pi suite: `149/149` passing with two expected Node-only browser harnesses skipped because Node is unavailable on the Pi.
- Changed Python compilation and fresh rendered-dashboard JavaScript syntax validation passed locally; deployed Python compilation passed.
- Fresh local ad-hoc result: `AD_HOC_WEATHER_POSITIONING: PASS separator=18 ticker=29 full_xy=true`.
- Authenticated live physical result: `AD_HOC_LIVE_PHYSICAL_WEATHER_POSITIONING: PASS frames=3 separator=18 ticker=29 ticker_positions=2`. The exact 64×32 framebuffer showed the independently positioned separator and moving ticker, plus left- and right-positioned weather elements, through the production controller and physical Matrix path.
- Temporary weather values were restored exactly. The temporary verifier device, cookie, and scripts were removed; the user's four trusted devices remained byte-identical at SHA-256 `a481c7e2ef715d80e962a5db0c3f6d6018c84baa553ad4ae6260b96aa4d249d6`.
- The device limit was temporarily raised for pairing, then restored to `4`. The user's newly created custom scrolling-text module and playlist entry were detected as legitimate concurrent activity and preserved rather than overwritten.
- Final service state: active/running, `MainPID=402066`, `NRestarts=0`, expected HTTP `302`, hardware lock owner `402075`, blocked second lock claim, no warning-level journal entries, no local/deployed source drift, and no verifier artifacts.
- Pre-deployment backups (`20260803-233414`):
  - `/home/admin/matrix-pre-weather-positioning-20260803-233414.tar.gz`
  - `/home/admin/matrix-config-pre-weather-positioning-20260803-233414.json`
  - `/home/admin/matrix-paired-devices-pre-weather-positioning-20260803-233414.json`
  - `/etc/systemd/system/matrix-web.service.pre-weather-positioning-20260803-233414`

## Previous completed feature: rotation for Text, Time, Pixels, and Line layers

- The apparent no-op rotation bug was traced to a type-specific gap rather than the keyframe engine: Text, Time, Pixels, and Line omitted `rotation` from their schema, defaults, inspector fields, browser draw path, and physical draw path even though the editor displayed a round rotation handle.
- Those four layer types now accept optional, backward-compatible `rotation` values in base properties, keyframes, and frame overrides. Existing saved layers without rotation remain valid and resolve to zero degrees.
- New Text, Time, Pixels, and Line layers initialize with `rotation: 0`; legacy layers show a friendly numeric zero in the inspector without rewriting saved data until edited.
- Browser preview rotates Text, Time, Pixels, and Line around the centre of their unrotated bounds. Direct manipulation now initializes missing legacy rotation values and lets Pixels use the rotation handle instead of always entering freehand mode.
- Rectangle, Polygon, Ellipse, and Media retain their existing rotation paths. Circle intentionally no longer shows a misleading rotation handle because rotating a circle cannot change its appearance.
- Physical Line and Pixels coordinates use the same positive-angle, bounds-centred point transform as the existing shape renderer. A horizontal Line therefore rotates vertically around its midpoint rather than moving around one endpoint.
- Physical Text and Time use exact BDF glyph pixels through a new capture-aware `Matrix.draw_text(..., rotation=...)` path. Rotated pixels are submitted to hardware and mirrored into `/api/display-frame` by the same `Matrix.pixel` calls, while the zero-rotation path and its established call shape remain unchanged.
- `BdfFont.text_pixels()` is now the single source of exact glyph coordinates for both normal frame capture and rotated text, preserving the existing baseline and advance semantics.

### Verification and deployment evidence

- Strict TDD RED/GREEN regressions cover optional/keyframeable schema support, Text and Time rotation forwarding, Line midpoint rotation, Pixels bounds-centred rotation, real BDF pixel rotation, browser controls/defaults, and executable browser transform pivots.
- Complete local suite: `147/147` passing. Complete deployed Pi suite: `147/147` passing with two expected Node-only browser harnesses skipped because Node is unavailable on the Pi.
- Local JavaScript syntax validation and changed-Python compilation passed; deployed Python compilation passed.
- Fresh local result: `AD_HOC_ADVANCED_ROTATION: PASS text=9x6->5x9 line=(15, 5)->(15, 15)`.
- Exact physical result: `AD_HOC_PHYSICAL_ADVANCED_ROTATION: PASS text=(9, 6)->(5, 9) line=(15, 1)->(1, 15) hashes=043a4a7278da,099b3e89c8c0`. Both frames were submitted through the real RGB Matrix canvas; the rotated frame was held for visual inspection.
- The first physical verifier attempt submitted only the unrotated frame before a verifier-only `BytesIO` wrapping error. Its cleanup trap restarted the service and deleted the script; the corrected fresh verifier then passed completely. No production change was needed for that verifier issue.
- Live-served `custom_canvas.js` matches deployed disk at SHA-256 `48ee86dd69463e1c2a0460a6c42660a4de5c11d6a6694563b9d48e1196565dae`; all five changed production files matched local/deployed checksums exactly.
- Persistent configuration remained byte-identical at `0a0c96c6008e1022c8c1a55e047fffcc761d476602ecf4efd98139579f01d76d`; paired devices remained byte-identical at `c169e7022199c57eaaaa0e7e5a79bdd327d42916ed45dd957eabd4e07c468d24`.
- Final service state: active/running, `MainPID=401366`, `NRestarts=0`, expected HTTP `302`, and no warning-level journal entries. Remote and local verifier/deployment artifacts were removed.
- Pre-deployment backups (`20260803-221520`):
  - `/home/admin/matrix-pre-rotation-20260803-221520.tar.gz`
  - `/home/admin/matrix-config-pre-rotation-20260803-221520.json`
  - `/home/admin/matrix-paired-devices-pre-rotation-20260803-221520.json`
  - `/etc/systemd/system/matrix-web.service.pre-rotation-20260803-221520`

## Previous completed feature: Aseprite-style layer timeline and animated depth

- The advanced 64×32 canvas editor now presents an Aseprite-style layer-by-frame grid: one row per existing declarative element/layer and one column per module frame.
- Clicking a grid cell selects both its layer and frame. Cells visibly distinguish complete keyframes (`◆`), per-frame edits/overrides (`●`), held/present exposure (`·`), and temporal absence (`○`). The current frame and selected cel are highlighted.
- Layer labels remain sticky while the bounded grid scrolls horizontally and vertically, so documents with up to 64 layers and 120 frames remain navigable.
- Rows at the selected frame are displayed front-to-back. The separate default stack panel displays top/front first and exposes friendly `Bring forward` and `Send backward` controls.
- Every layer type accepts an optional bounded integer `z_index`/Depth (`-64..64`) in base properties, keyframes, and frame overrides. Higher depth renders in front. Existing documents without depth remain byte-compatible and retain their layer-array draw order.
- Depth is deliberately stepped rather than numerically interpolated: front/behind changes occur exactly on the chosen keyframe, avoiding fractional depths or unexpected halfway crossings.
- Browser preview, onion skinning, physical rendering, existing presence controls, frame overrides, GIF/MP4 playback, and the declarative 64×32 schema all use the same per-frame resolved ordering.
- Animated media remains one compact `media` layer; the grid shows module-frame exposure without serializing decoded media pixels or creating one saved layer per source frame.

### Verification and deployment evidence

- Strict TDD regressions cover bounded depth schema validation, legacy array-order fallback, stepped keyframed ordering, actual physical draw-call order, Aseprite grid/control markers, and executable JavaScript ordering parity.
- Complete local suite: `139/139` passing. Complete deployed Pi suite: `139/139` passing with one expected Node-only test skipped because Node is not installed on the Pi.
- Local JavaScript syntax validation and Python compilation passed; deployed Python compilation passed.
- Exact physical result: `AD_HOC_PHYSICAL_ASEPRITE_DEPTH: PASS centres=blue,blue,red unique=2 size=64x32`. The production Matrix/capture path submitted blue-front, blue-front, then red-front frames as depth changed at frame 3.
- The physical verifier stopped the service under a restart trap, recreated systemd's root-owned `0755` runtime directory after systemd removed it, acquired `/run/matrix-display/matrix-display.lock`, cleared the panel afterward, removed its script, and restarted the service.
- Live-served JS and CSS SHA-256 values matched their deployed files exactly (`08cae457...` and `5d66f5f4...`). All seven changed source/test files matched local and deployed checksums.
- Persistent configuration remained byte-identical at `0a0c96c6008e1022c8c1a55e047fffcc761d476602ecf4efd98139579f01d76d`; the real user-saved 32-frame RGB cube remained present. Paired devices remained byte-identical at `c169e7022199c57eaaaa0e7e5a79bdd327d42916ed45dd957eabd4e07c468d24`.
- Final service state: active/running, `MainPID=400876`, `NRestarts=0`, expected HTTP `302`, hardware lock present, and no warning-level journal entries.
- Pre-deployment backups (`20260803-224755`):
  - `/home/admin/matrix-pre-aseprite-timeline-20260803-224755.tar.gz`
  - `/home/admin/matrix-config-pre-aseprite-timeline-20260803-224755.json`
  - `/etc/systemd/system/matrix-web.service.pre-aseprite-timeline-20260803-224755`

## Previous completed feature: automatic GIF and MP4 timeline frames

- Animated GIF and MP4 media items now expose bounded `animated`, `frame_count`, `fps`, and `duration_ms` metadata through the existing paired media API.
- Adding or uploading animated media in the advanced editor automatically expands the numbered module timeline from the current playhead. Users no longer create one module frame or keyframe per decoded media frame.
- A new one-frame canvas adopts the media's effective FPS and looping playback. An existing animation keeps its timing and expands by the media duration sampled at the existing module FPS.
- Expansion remains bounded by the existing 120-frame document limit and reports clipping when that limit is reached.
- The saved canvas remains compact and declarative: decoded pixel arrays are not copied into module JSON. The existing bounded time-based media decoder remains the physical rendering source of truth.
- Media inserted after frame 1 begins at decoded media frame zero when its insertion presence keyframe becomes active, then advances relative to that insertion frame.
- A physical MP4 acceptance exposed an owner-only persistent-media issue in the previous unprivileged FFmpeg path. `Matrix.media_frames()` now feeds the already-open `O_NOFOLLOW` descriptor to FFmpeg through `pipe:0`/stdin instead of asking the dropped identity to reopen `/proc/self/fd/<n>`. Explicit argv, no-shell execution, containment, privilege dropping, and decoder bounds remain intact.

### Verification and deployment evidence

- Test-driven regressions cover GIF metadata, MP4 metadata, automatic editor expansion, insertion-relative media indexing, and stdin-based bounded MP4 decoding.
- Complete local suite: `134/134` passing.
- Complete deployed Pi suite: `134/134` passing through `/home/admin/matrix/venv/bin/python`.
- Local Node syntax validation and changed-Python compilation passed; deployed Python compilation passed.
- Fresh local behavior result: `AD_HOC_ANIMATED_MEDIA_TIMELINE: PASS gif=3@10fps video=6@12fps blank=auto-loop+3frames inserted_at_6=source_frames_0,1,2 existing=expanded_to_11`.
- Real owner-only uploaded-video decoder result: `REAL_OWNER_ONLY_MP4_DECODE: PASS frames 115 size (64, 32)`.
- Live media metadata reported automatic timelines for four animated assets, including `built-in/squares_tb.gif` at 110 frames/11 FPS and the uploaded MP4 at 115 frames/15 FPS.
- Final exact physical result: `AD_HOC_PHYSICAL_ANIMATED_MEDIA_TIMELINE: PASS gif=samples:18,unique:16,sequence:[0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] mp4=samples:19,unique:19,sequence:[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]`.
- The physical verifier used existing persistent media without changing saved configuration or media, stopped the service under a restart trap, acquired the production lock, rendered through the production Matrix/capture path, and cleaned its remote script.
- All seven changed source/test files matched local and deployed SHA-256 checksums exactly. The served editor asset also matched deployed disk exactly.
- Final authenticated status: `running=true`, `pairing_active=false`, `last_error=null`, `current_module=clock_lava`; all three paired devices remain present and byte-identical at SHA-256 `c169e7022199c57eaaaa0e7e5a79bdd327d42916ed45dd957eabd4e07c468d24`.
- `matrix-web.service` recovered after physical verification with `NRestarts=0`; HTTP readiness returned the expected redirect, the hardware lock was present, and the warning-level journal had no entries.
- Deployment preserved the user's post-restart cube save rather than restoring the older pre-deployment configuration over it.
- Backups:
  - `/home/admin/matrix-pre-animated-timeline-20260803-221807.tar.gz`
  - `/home/admin/matrix-config-pre-animated-timeline-20260803-221807.json`
  - `/etc/systemd/system/matrix-web.service.pre-animated-timeline-20260803-221807`
  - `/home/admin/matrix-pre-mp4-stdin-20260803-222743.tar.gz`
  - `/home/admin/matrix-config-pre-mp4-stdin-20260803-222743.json`
  - `/etc/systemd/system/matrix-web.service.pre-mp4-stdin-20260803-222743`

## Previous completed feature: automatic layer keyframes, timed presence, and spinning RGB cube

- Every layer added through `Add layer`, `Add time`, `Add image`, or the upload flow now receives a complete initial keyframe at the editor's current frame.
- A layer added after frame 1 has a hidden base state and becomes present at its insertion keyframe, so elements can enter an animation naturally without manually backfilling earlier frames.
- Every layer now exposes `Present at this frame`, `Show from this frame`, and `Hide from this frame` controls.
- Show/hide actions create or update safe stepped `present` keyframes. An element can be shown, hidden, and shown again repeatedly without destructively deleting its layer, properties, or other keyframes.
- The existing layer-list visibility checkbox remains a global editor/render switch and is distinct from animated presence.
- `present` is an optional backward-compatible declarative property for base properties, keyframes, and per-frame overrides. Existing advanced modules without it continue to render as present.
- Browser preview, onion skinning, and physical rendering resolve the same presence timeline. Physically absent elements are skipped before drawing while every frame still follows the existing owned-canvas swap/capture path.
- The starter-template selector now includes `Spinning RGB cube`: a 32-frame, 16-FPS looping perspective wireframe built from 12 validated line layers and 60 coordinate keyframes.
- Cube edges are grouped by axis in red, green, and blue. The final keyframe returns to the starting orientation for a seamless loop.

### Verification and deployment evidence

- Test-driven regressions were added for optional/keyframed presence validation, physical presence rendering, automatic new-layer keyframes, show/hide controls, browser presence filtering, and RGB cube template availability.
- Complete local suite: `130/130` passing.
- Complete deployed Pi suite: `130/130` passing through `/home/admin/matrix/venv/bin/python`.
- Local Node syntax validation of `app/static/custom_canvas.js` and changed-Python compilation passed; deployed Python compilation also passed.
- Fresh local ad-hoc result: `AD_HOC_KEYFRAME_PRESENCE_CUBE: PASS auto_keyframe=frame4 intervals=hidden,shown,hidden,shown cube_layers=12 cube_keyframes=60 colours=RGB distinct_geometry=4`.
- Authenticated live physical result: `AD_HOC_LIVE_PRESENCE_CUBE: PASS active=True samples=78 unique_frames=32 presence=off,on,off,on max_presence_pixels=42 rgb=[True, True, True]`.
- The live verifier saved a temporary 32-frame RGB cube plus a separate timed red element through the normal API, synchronized on `current_module`, and polled exact 64×32 hardware framebuffer capture.
- All 32 cube animation frames produced distinct exact-frame hashes; red, green, and blue cube edges were observed, and the timed element was observed both absent and present across repeated intervals.
- The served editor asset matched deployed disk byte-for-byte and contained automatic-keyframe, presence, and cube-template controls.
- Original custom modules were restored; no temporary module, media, or verifier script remains (`TEMP_MODULE_COUNT=0`, `ORPHANS count=0`).
- Persistent config remained byte-identical at SHA-256 `0992c2587eadbe820c1135c262495724d6d60b2f6dabc3fc06b746f4c4a98d00`; all three paired devices remained byte-identical at SHA-256 `c169e7022199c57eaaaa0e7e5a79bdd327d42916ed45dd957eabd4e07c468d24`.
- Final source checksum dry-run was empty. `matrix-web.service` was active with `NRestarts=0`; authenticated status was `running=true`, `pairing_active=false`, `last_error=null`, and `current_module=clock_lava`.
- Hardware lock owner was PID `398681`; a second acquisition of `/run/matrix-display/matrix-display.lock` was blocked. Warning-level journal query showed no entries.
- Pre-deployment backup stamp: `20260803-215505`:
  - `/home/admin/matrix-pre-presence-cube-20260803-215505.tar.gz`
  - `/home/admin/matrix-config-pre-presence-cube-20260803-215505.json`
  - `/etc/systemd/system/matrix-web.service.pre-presence-cube-20260803-215505`

## Previous completed feature: rotation snapping and quick Time/Image elements

- The advanced custom-module editor now has a friendly per-editor `Rotation snap` selector with `Off`, `15°`, `30°`, `45°`, and `90°` choices.
- Snapping applies during direct rotation-handle manipulation. `Off` preserves arbitrary free rotation, and exact numeric rotation input remains available.
- `Add time` creates a declarative live Time layer without expressions or executable code.
- Time layers support safe formats `24h`, `24h_seconds`, `12h`, and `12h_seconds`, plus the existing safe font, colour, position, visibility, layer ordering, keyframe, and override controls.
- Browser preview refreshes Time layers once per second; physical rendering resolves the current system time on each rendered frame.
- `Add image` now provides a quick path through the existing validated `media` layer. It prefers an existing PNG/JPEG/WebP resource and opens an image-only upload picker when no static image is available.
- Image insertion continues to use the same safe media schema, resizing, aspect, rotation, animation, upload, and exact framebuffer path as existing media layers.
- Existing legacy simple/static/scrolling text modules and all pre-existing advanced records remain compatible.

### Verification and deployment evidence

- Test-driven regressions were added for Time schema/format validation, physical Time rendering, editor snap controls, degree labels, quick Time/Image actions, and static-image preference.
- Complete local suite: `126/126` passing.
- Complete deployed Pi suite: `126/126` passing through `/home/admin/matrix/venv/bin/python`.
- Local Node syntax validation of `app/static/custom_canvas.js` and changed-Python compilation passed.
- Fresh local ad-hoc result: `AD_HOC_TIME_SNAP_IMAGE_CURRENT_WORKSPACE: PASS snap=off,15,30,90 time=12h_seconds,24h image=rotated`.
- Authenticated live production result: `AD_HOC_LIVE_TIME_IMAGE: PASS active=True white_pixels=82 red_pixels=144 frame=45d236466043db9e`.
- The live verifier uploaded a deterministic image, saved and previewed a temporary Time + 45° Image module through normal APIs, synchronized on `current_module`, and proved both white clock pixels and red rotated-image pixels in the exact 64×32 hardware framebuffer capture.
- The served editor asset matched deployed disk byte-for-byte and contained the live snap and quick-add controls.
- Original custom modules were restored, the verifier upload was deleted, and no verifier scripts/modules/media remain (`TEMP_MODULE_COUNT=0`, `ORPHANS count=0`).
- Persistent config remained byte-identical at SHA-256 `0992c2587eadbe820c1135c262495724d6d60b2f6dabc3fc06b746f4c4a98d00`; all three paired devices remained byte-identical at SHA-256 `c169e7022199c57eaaaa0e7e5a79bdd327d42916ed45dd957eabd4e07c468d24`.
- Final source checksum dry-run was empty. `matrix-web.service` was active with `NRestarts=0`; authenticated status was `running=true`, `pairing_active=false`, `last_error=null`, and `current_module=clock_lava`.
- Hardware lock owner was PID `397901`; a second acquisition of `/run/matrix-display/matrix-display.lock` was blocked. Warning-level journal query showed no entries.
- Pre-deployment backup stamp: `20260803-213832`:
  - `/home/admin/matrix-pre-time-snap-image-20260803-213832.tar.gz`
  - `/home/admin/matrix-config-pre-time-snap-image-20260803-213832.json`
  - `/etc/systemd/system/matrix-web.service.pre-time-snap-image-20260803-213832`

## Previous completed feature: image, GIF, and MP4 custom-canvas layers

- Advanced custom modules now support one validated `media` layer type for PNG, JPEG, WebP, GIF, and MP4 resources.
- Media layers store only declarative properties: safe media reference, x/y position, width, height, rotation, and keep-aspect.
- Media can be selected from the existing library or uploaded directly inside the advanced editor.
- Image/GIF/MP4 layers share the existing drag, resize, rotation, layer-order, visibility, keyframe, override, and timeline controls.
- Browser preview uses image/video elements and redraws animated resources during timeline playback.
- Physical rendering preloads a bounded maximum of 120 timeline-aligned frames per media source. Static images repeat; GIF timing respects saved frame durations; MP4 is decoded by FFmpeg, looped, normalized to 64×32, and sampled at module FPS.
- Media decode time no longer consumes the module's configured display duration; playback timing begins after preload completes.
- Rendering stays inside `DisplayController`'s single owned `Matrix`, draws on the existing canvas, and publishes exact capture at the same hardware swap.
- Custom-module configuration updates reject missing media sources, unsafe paths, unsupported extensions, unknown fields, and out-of-range geometry.
- A user media file referenced by a layer, keyframe, or frame override cannot be deleted.

### Folder-based built-in classification

- Built-in status is now determined only by location: direct files in `resources/media/built-in/` are built-in and non-removable.
- Every direct file outside that folder is non-built-in user media, including uploads.
- Safe references allow either a root basename or exactly `built-in/<basename>`; traversal, nested arbitrary folders, symlinks, and non-regular files remain rejected.
- Bundled `squares_tb.gif`, `sleep.gif`, and `hermes_self_portrait.png` were migrated into `resources/media/built-in/` locally and on the Pi.
- The unrelated live `resources/media/squares.gif` remains at the media root and is correctly classified as non-built-in.
- Legacy saved references to the three known bundled names migrate to `built-in/...`; the user's selected splash `squares.gif` remained unchanged, while sleep migrated to `built-in/sleep.gif`.

### Verification and deployment evidence

- Test-driven regressions were added for folder classification/protection, safe nested serving, media-layer schema, existing-source validation, referenced-media deletion protection, bundled-path migration, renderer frame selection/resizing, capture-aware image drawing, bounded MP4 scaling, editor controls/upload/serialization, and preload timing.
- Complete local suite: `123/123` passing after the preload timing correction.
- Complete deployed Pi suite: `123/123` passing as EUID 0 through `/home/admin/matrix/venv/bin/python`.
- Local JavaScript syntax and changed-Python compilation passed. The Pi has no Node binary; the served editor asset was instead verified byte-for-byte against deployed disk after local syntax validation.
- Real local ad-hoc decode: PNG, timed GIF, MP4, resize, rotation, and capture path passed.
- Physical ad-hoc result: `AD_HOC_PHYSICAL_CUSTOM_MEDIA: PASS frames=4 unique=2 gif=[(0, 255, 0), (0, 0, 255), (0, 255, 0)]`.
- Authenticated live result: PNG/GIF/MP4 uploads all returned `201`; folder classification and served editor markers passed; temporary uploads were removed.
- Authenticated live render result: `AD_HOC_LIVE_CUSTOM_MEDIA_RENDER: PASS hashes=3 revisions=3 active=True` after synchronizing on `current_module`.
- Original custom modules were restored, no temporary module remains, all three paired devices remain byte-identical, and no verifier media/scripts remain.
- Source checksum dry-run is empty; warning-level journal query is empty; second hardware lock acquisition is blocked.
- Pre-deployment backup stamp: `20260803-210556`:
  - `/home/admin/matrix-pre-custom-media-20260803-210556.tar.gz`
  - `/home/admin/matrix-config-pre-custom-media-20260803-210556.json`
  - `/etc/systemd/system/matrix-web.service.pre-custom-media-20260803-210556`
- Readiness pitfall: do not probe `/pair` directly after restart because it intentionally activates physical pairing mode. Probe `/` without following its `302` redirect, then use an authenticated `/api/status` call for runtime state.

## Latest completed feature: advanced layered custom-canvas animations

The custom-module creator now starts with the existing friendly simple-text editor and offers a confirmed, one-way `Convert to advanced canvas` action. Advanced modules remain validated data and never execute uploaded Python.

### Layered canvas and drawing tools

- Each advanced module owns one 64×32 canvas with a validated background colour and up to 64 ordered layers.
- Supported layers are text, freehand/pixel collections, lines, rectangles, polygons, circles, and ellipses.
- Layers have friendly names, visibility controls, ordering, removal, exact numeric/property inputs, and direct canvas manipulation.
- Direct manipulation supports dragging, shape resize handles, rotation handles, and freehand pixel drawing.
- Text supports the existing safe BDF font allowlist. Geometric layers support validated colour, stroke/fill, size, position, point, and rotation data as applicable.
- The renderer uses only capture-aware `Matrix` wrapper calls, including calculated polygon/rotated-square/ellipse segments; it does not create another hardware owner.

### Hybrid timeline and playback

- Frame count is configurable from `1..120`; FPS is validated from `1..60`.
- Playback mode is selectable per module: loop, ping-pong, or hold the final frame.
- Per-layer keyframes support linear, ease-in, ease-out, ease-in-out, and stepped interpolation.
- Numeric values, colour channels, and compatible point arrays interpolate safely; text/type changes remain discrete.
- Per-frame per-layer overrides are applied after interpolation.
- Timeline tools include previous/next-frame onion skinning, browser playback, copy frame, paste frame, and duplicate frame.
- Starter templates include `Spinning square`, `Three lines and circle`, and a blank canvas.
- Physical preview still routes through the normal playlist/controller preview API and exact-frame capture.

### Validation and bounded data

- Existing simple `text` and `text_scroll` definitions remain valid without migration changes.
- Advanced IDs and layer IDs are constrained; names, fields, coordinates, dimensions, rotations, colours, fonts, points, frame references, and easing values are schema-validated.
- Advanced modules are capped at 120 frames, 64 layers, 512 total keyframes, 512 overrides, 8,192 aggregate points, and 1 MiB serialized JSON; polygon and pixel-point collections also have per-value bounded counts.
- Dangling/duplicate layer references, duplicate frame entries, unknown fields/types, and out-of-range values are rejected.
- Review hardening completed after the first deployment: a property now retains its layer base value until its first property-specific keyframe, consistently in the Python renderer and browser editor. This prevents future keyframes from leaking backward into earlier frames.

### Verification evidence

- Local complete suite: **113 tests passed**.
- Deployed Pi complete suite as root through `/home/admin/matrix/venv/bin/python`: **113 tests passed**.
- Changed Python compilation passed.
- `app/static/custom_canvas.js` passed local Node syntax checking; the Pi has no Node runtime, so deployed and served identity was proved by matching SHA-256 `5a098815b2405d02108f22c7d6e153bbd3aea8500f1c28ba85bf8f3d3abe18c8`.
- Rendered dashboard inline JavaScript passed local Node syntax checking.
- Standalone physical ad-hoc verification drew three independently coloured text layers plus a circle, found exact required RGB values, then rendered an eight-frame rotating square animation. Exact capture reported 8 submitted spinner frames, 7 unique framebuffer hashes, and final 64×32 output.
- Authenticated live-service verification loaded the dashboard/editor asset, persisted a temporary advanced module through `/api/config`, triggered `/api/preview/custom_live_spinner`, and polled `/api/display-frame`. It observed 7 unique PNG hashes, 19 distinct frame revisions, and all four expected exact colours.
- The authenticated verifier restored the original config byte-for-byte and removed only its temporary paired device. The three pre-existing trusted devices were preserved.
- A post-review physical verifier rendered frames 0 and 1 at the base rectangle bounds `(2,9)`, then frame 2 at keyframed bounds `(40,47)`, proving exact pre-first-keyframe behavior on hardware and capture.
- A fresh post-review ad-hoc verifier directly proved base-value retention and rejection above 8,192 aggregate points; its OS-generated temporary script was removed.
- Final config hash matches its pre-deployment value; service is active with one Gunicorn worker; lock text/`fuser` agree; a second lock claim is blocked; warnings are empty; temporary verifier files are absent; checksum dry-run drift is zero.

Backups created immediately before this deployment:

- `/home/admin/matrix-pre-advanced-canvas-20260803-202812.tar.gz`
- `/home/admin/matrix-config-pre-advanced-canvas-20260803-202812.json`
- `/etc/systemd/system/matrix-web.service.pre-advanced-canvas-20260803-202812`

Review-hardening backups:

- `/home/admin/matrix-pre-review-hardening-20260803-204245.tar.gz`
- `/home/admin/matrix-config-pre-review-hardening-20260803-204245.json`
- `/etc/systemd/system/matrix-web.service.pre-review-hardening-20260803-204245`

## Previous completed feature: paired-device access management

Additional-device pairing and revocable named device access are complete, deployed, and live verified.

### Pairing and sessions

- The pairing/login page asks for a friendly device name and includes a paired-only `PAIR ANOTHER DEVICE` action.
- The dashboard has a `Paired devices` settings section with an additional-pairing button, the configured device limit, named device rows, a `This device` marker, and per-device removal.
- Paired sessions carry a random server-registered device ID and are rejected immediately after that device is removed.
- Signed paired sessions remain valid for up to 365 days and survive service restarts; removal or `Unpair` revokes the server-side device entry.
- On startup, the Matrix resumes normal content when trusted devices exist. It displays a fresh pairing screen only when the registry is empty.
- Pairing PINs remain short-lived, single-use, brute-force throttled, and never appear in API responses or persistent files.

### Capacity and persistence

- Persistent configuration now contains `"pairing": {"max_devices": 4}`.
- The maximum is schema-validated from `1` through `10` and cannot be lowered below the current paired-device count.
- `/home/admin/matrix/data/paired-devices.json` is an atomic, root-owned `0600` registry of named devices.
- Additional pairing is blocked before a new PIN is issued when capacity is full, including a race-safe second check when registration completes.
- Deployment migration added only the new pairing default; semantic comparison proved every unrelated configuration value remained unchanged.

### Verification evidence

- Local complete suite: **104 tests passed**.
- Deployed Pi complete suite as root through `/home/admin/matrix/venv/bin/python`: **104 tests passed**.
- Changed Python compilation and rendered dashboard JavaScript syntax: passed.
- Authenticated live dashboard confirmed the paired-device menu, limit input, named rows, and additional-pairing control.
- Live two-browser verification paired two independently named sessions, identified the current device, revoked the second device, and proved its next API request returned `401`.
- Live capacity verification lowered the limit to the current count, proved another pairing request was blocked, then restored the default of `4`.
- Live restart verification proved the remaining signed paired session stayed authorized and the physical Matrix resumed normal operation when a trusted device existed.
- Exact paired framebuffer verification remained `64×32` before and after the live restart.
- Verification removed both temporary devices, restored the default capacity, removed all temporary scripts/cookies, and opened a fresh pairing screen without persisting any PIN.
- Final service has one Gunicorn worker; lock text and `fuser` owner match; a second lock claim is blocked; warnings are empty; checksum dry-run drift is zero.

Backups created immediately before this deployment:

- `/home/admin/matrix-pre-paired-devices-20260803-004805.tar.gz`
- `/home/admin/matrix-config-pre-paired-devices-20260803-004805.json`
- `/etc/systemd/system/matrix-web.service.pre-paired-devices-20260803-004805`

## Previous completed feature: complete built-in module editors

All four built-in modules are now configurable through grouped editors, deployed, and exact-frame verified on the physical Matrix path.

### Playlist and reset UI

- Every playlist row has an `Edit` button next to `Preview`.
- Built-in rows expand a grouped settings panel directly beneath that row.
- Custom-module `Edit` opens the existing safe custom-module builder.
- Splash and sleep also have dedicated layout-edit buttons beside their media selectors, so their settings remain reachable when either module is absent from the playlist.
- Every built-in editor has `Reset to default`; reset changes remain local until the normal Save configuration action.
- Weather reset includes layout, motion, colours, units, and location defaults.
- Lava reset includes blob/text settings, checked built-in palettes, and saved custom-palette defaults.

### Weather customization

In addition to units, temperature/ticker baselines, direction, and speed, weather now supports:

- Independent `NOW` X/baseline and temperature X.
- Independent high and low X/baseline positions.
- Independent separator-line height.
- Background, main text, high, low, separator, and ticker colours.
- All coordinates and `#RRGGBB` values are schema-validated and applied by the capture-aware renderer.

### Lava customization

The lava clock now supports validated controls for:

- Blob count, minimum/maximum radius, movement speed, field/merge threshold, blur radius, and edge width.
- Show/hide time and date independently.
- Time/date font, alignment, and baseline.
- Text and border colours.
- Existing built-in and saved custom palette controls remain available from the grouped editor.
- Blob state is created per run from the saved settings rather than relying on one fixed module-global shape.

### Splash and sleep customization

Both media modules now support validated X/Y position, width, height, keep-aspect toggle, and video FPS. These options apply consistently to static images, GIFs, and MP4 playback while retaining interruptible playback and exact-frame capture.

### Verification evidence

- Local complete suite: **88 tests passed**.
- Deployed Pi complete suite through the deployed venv: **88 tests passed**.
- Changed Python compilation: passed.
- Rendered dashboard JavaScript syntax: passed with Node.
- Final focused ad-hoc rendered-page check confirmed the independent splash/sleep edit controls and removed its temporary files.
- Authenticated live UI confirmed grouped playlist editors, reset controls, and the final splash/sleep layout buttons.
- Live API persistence accepted the full temporary built-in settings set.
- Exact physical-output framebuffer checks passed:
  - Weather: custom separator at Y=20, independently positioned high/low text, custom colours, and visible right-moving ticker.
  - Lava: one configured blob with text/date hidden and only the selected palette colours.
  - Splash: non-black output remained inside configured bounds `10,5,20,10`.
  - Sleep: non-black output remained inside configured bounds `20,8,16,16`.
  - Final post-restart exact framebuffer remained 64×32.
- Original persistent configuration was restored exactly after the live test.
- Final service is active with one worker; lock text and contention checks passed; warning-level journal output is empty.
- Final checksum dry-run drift is zero; all verifier scripts, cookies, and temporary files were removed; a fresh pairing PIN is displayed.

Backups created immediately before this deployment:

- `/home/admin/matrix-pre-builtin-editors-20260731-210803.tar.gz`
- `/home/admin/matrix-config-pre-builtin-editors-20260731-210813.json`

## Previous completed feature: weather layout, motion, and units controls

The requested weather customization is complete, deployed, and physically/exact-frame verified.

### Data model and validation

The persistent `weather` configuration now also supports:

```json
{
  "units": "celsius",
  "temperature_y": 17,
  "ticker_y": 31,
  "scroll_direction": "left",
  "scroll_speed": "normal"
}
```

- Units may be `celsius` or `fahrenheit`.
- Temperature baseline is bounded to `12..20`.
- Ticker baseline is bounded to `24..31`.
- Ticker direction may be `left` or `right`.
- Ticker speed may be `slow`, `normal`, or `fast`.
- Existing deployed configuration was deep-merged with the four new display/motion defaults; location and units were preserved.

### Runtime and dashboard

- Open-Meteo receives the selected `temperature_unit` and returns values in the requested unit.
- The renderer uses `C`/`F` consistently for current and feels-like values.
- Temperature and ticker baselines are configurable; the separator follows ticker height.
- Right-moving text wraps correctly and remains capture-aware through `draw_text`/`widget_scroll`.
- The dashboard provides friendly Celsius/Fahrenheit, direction, speed, and range-slider height controls through the normal `/api/config` save path.

### Verification evidence

- Local complete suite: **83 tests passed**.
- Deployed Pi complete suite as root through the deployed venv: **83 tests passed**.
- Changed Python compilation and rendered dashboard JavaScript syntax: passed.
- Service restart: active with one Gunicorn worker, `NRestarts=0`, no warning-level journal entries.
- Hardware lock text and `fuser` owner both matched the single worker; a second lock claim failed as required.
- Live authenticated dashboard and API persistence test: passed.
- Physical weather preview used temporary Fahrenheit/right/slow settings at baselines 18 and 29.
- Exact 64×32 framebuffer verification captured 20 revisions; ticker motion measured `+1` pixel to the right and the exact 5×7 `F` glyph was found at the configured temperature baseline.
- Temporary configuration was restored exactly, verification scripts/cookies were removed, source checksum drift is zero, and a fresh pairing PIN is displayed.

Backups created immediately before this deployment:

- `/home/admin/matrix-pre-weather-controls-20260731-204016.tar.gz`
- `/home/admin/matrix-config-pre-weather-controls-20260731-204016.json`
- `/etc/systemd/system/matrix-web.service.pre-weather-controls-20260731-204016`

## Earlier completed feature: saved custom lava palettes

The requested multi-palette feature is complete, deployed, and physically verified.

### Data model

`config_store.py` now supports:

```json
"lava": {
  "enabled_palettes": ["builtin IDs"],
  "custom_enabled": false,
  "custom_colours": ["legacy compatibility values"],
  "custom_palettes": [
    {
      "id": "palette_<stable-id>",
      "name": "Friendly name",
      "colours": ["#outside", "#inside", "#edge"],
      "enabled": true
    }
  ]
}
```

Rules:

- At most 20 saved palettes.
- Stable IDs must match `palette_[a-z0-9_-]{1,40}`.
- Names are trimmed, printable, 1–40 characters, and unique case-insensitively.
- IDs are unique.
- Each palette has exactly three validated `#RRGGBB` colours.
- Each palette has an explicit boolean enabled state.
- At least one built-in, legacy custom, or saved custom palette must be enabled.
- Runtime candidates are checked built-ins plus enabled saved custom palettes.

### Migration

The old enabled single custom palette was automatically migrated into:

- ID: `palette_migrated`
- Name: `My custom palette`
- Enabled: `true`
- Colours preserved: `#000000`, `#ffb638`, `#00ffff`

The legacy `custom_enabled` flag was changed to `false` to prevent selecting the same colours twice. A semantic comparison proved every unrelated configuration value was preserved.

### Dashboard

`app/templates/dashboard.html` now has clickable `<details>/<summary>` groups:

- Built-in palettes
- Saved custom palettes

The UI supports adding, naming, recolouring, enabling/disabling, and removing custom entries. Both groups show enabled counts. Controls are responsive for desktop and mobile. State is serialized through the normal `/api/config` path, not kept only in the browser.

### Verification evidence

- Local complete suite: **78 tests passed**.
- Deployed Pi complete suite: **78 tests passed**.
- Dashboard JavaScript syntax: passed with Node.
- Live authenticated persistence test temporarily saved a second palette and retrieved it from `/api/config`.
- Physical preview ran `clock_lava` with only the temporary saved palette enabled.
- Exact framebuffer verification: `64×32`, `104` unique colours.
- Test outside and inside colours were detected exactly (`distance=0.0`).
- Temporary palette was removed and the migrated user palette restored.
- Temporary verification script and cookie were removed.

Backups created immediately before this deployment:

- `/home/admin/matrix-pre-saved-lava-20260731-202558.tar.gz`
- `/home/admin/matrix-config-pre-saved-lava-20260731-202558.json`
- `/etc/systemd/system/matrix-web.service.pre-saved-lava-20260731-202558`

## Other completed and deployed capabilities

### MP4 media

`.mp4` support is complete and deployed:

- Paired upload, media-library listing, serving, and browser video preview.
- Content is probed with FFprobe rather than trusting extension/MIME.
- Sanitization runs as `nobody`/`nogroup`.
- Input request cap remains 128 MB.
- Maximum duration 300 seconds.
- Maximum source FPS 120.
- Maximum source pixels 40,000,000 per frame.
- Canonical output: within 64×32, 20 FPS, H.264/YUV420P fragmented MP4.
- Maximum output frames 6,000 and output size 48 MB.
- Audio/subtitle/data streams are removed.
- FFmpeg uses one thread and bounded CPU/memory/wall time.
- Physical playback is interruptible through existing duration/`stop_event` behavior.
- Decoding runs as `nobody` from an inherited file descriptor.
- Every decoded frame passes through physical canvas swap and exact framebuffer capture.
- FFmpeg/FFprobe 7.1.5 is installed on the Pi.

Live MP4 verification previously passed with a real 320×180, 24 FPS source containing audio; canonical output was 64×32, 20 FPS, audio-free. Six distinct exact-preview frames were captured.

### Exact preview

- `frame_capture.py` mirrors pixels, canvas operations, images/GIF/video frames, BDF text, and swaps.
- Paired `GET /api/display-frame` returns the exact 64×32 PNG submitted to hardware.
- It does not create a second Matrix owner.
- Weather scrolling was fixed to use capture-aware `draw_text`, so its ticker appears both physically and in the dashboard preview.

### Custom text modules

Safe, data-driven static and scrolling text items are supported through `modules/custom_text.py` and the normal controller/playlist. Arbitrary uploaded Python remains prohibited.

### Large image uploads

The request cap is 128 MB. Images/GIFs are decoded as an unprivileged sanitizer child, bounded, and normalized to the Matrix envelope. Existing source-pixel and frame limits must remain intact.

## Relevant local files

- `/root/matrix_new/config_store.py` — persistent schema, validation, built-in defaults, custom palette migration.
- `/root/matrix_new/module_options.py` — authoritative built-in lava catalog and colour metadata.
- `/root/matrix_new/modules/clock_lava.py` — configurable blobs/clock text and palette selection.
- `/root/matrix_new/modules/media_player.py` — configurable splash/sleep image, GIF, and MP4 geometry.
- `/root/matrix_new/modules/splash_screen.py` and `sleep_screen.py` — apply per-slot media layout.
- `/root/matrix_new/app/templates/dashboard.html` — grouped per-module editors, resets, palette/media UI, exact preview, custom modules.
- `/root/matrix_new/webapp.py` — paired routes, media APIs, config updates, exact-frame route.
- `/root/matrix_new/pairing.py` — one-time PIN manager, persistent named-device registry, startup pairing/display decision.
- `/root/matrix_new/app/wsgi.py` — production wiring for persistent device sessions and startup behavior.
- `/root/matrix_new/matrix_framework.py` — physical primitives, GIF/MP4 playback, exact capture integration.
- `/root/matrix_new/frame_capture.py` — software framebuffer mirror.
- `/root/matrix_new/media_sanitizer.py` — bounded image/GIF/MP4 canonicalization.
- `/root/matrix_new/display_controller.py` — single-owner playlist/preview execution.
- `/root/matrix_new/modules/weather_app.py` — weather renderer.
- `/root/matrix_new/modules/custom_text.py` — safe custom text renderer.
- `/root/matrix_new/app/static/custom_canvas.js` — direct layered canvas, timeline, keyframe, frame-override, template, and conversion editor.
- `/root/matrix_new/app/static/custom_canvas.css` — responsive advanced custom-canvas editor styling.
- `/root/matrix_new/tests/test_config_store.py` — migration and complete built-in validation coverage.
- `/root/matrix_new/tests/test_clock_lava.py` — saved palette and configurable runtime coverage.
- `/root/matrix_new/tests/test_media_modules.py` — configurable media geometry coverage.
- `/root/matrix_new/tests/test_weather.py` — weather request/render customization coverage.
- `/root/matrix_new/tests/test_webapp.py` — grouped editor, reset, dashboard, and API coverage.

## Media reorganization — completed and deployed 2026-08-04

The media classification limitation is resolved. Classification remains location-based, but paired users now have friendly controls to change it:

- Built-in cards expose `Move to My media`.
- My media cards expose `Move to Built-in`.
- The paired route is `POST /api/media/<path>/classification` with an exact boolean `built_in` body.
- Moves preserve the original inode with no-overwrite hard linking, descriptor-relative directory access, source/destination identity rechecks, config rollback, and replacement-safe cleanup.
- Splash, sleep, and advanced-canvas base/keyframe/override media references migrate automatically.
- The dashboard updates its local media/model/select state from the committed response before trying a second refresh request. A failed refresh therefore cannot leave stale controls after a successful move.
- A post-commit display reload failure is reported as a successful move with a clear warning, rather than a false HTTP 500.
- Legacy bundled-reference migration is filesystem-aware: it runs only when the built-in file exists and the root file does not. Intentionally demoted bundled names therefore survive service restarts. Canvas references participate in this migration too.
- Media and configuration directory entries are `fsync`ed after link, unlink, rollback, and atomic config replacement. If a process interruption leaves source and destination as the exact same regular-file inode, retrying the requested classification safely completes reference migration and source cleanup; a different-inode collision remains untouched.
- On 2026-08-04 the user explicitly rejected and rolled back the later fsync fault-handling/hidden-transaction-link revision. The five application/test files were restored exactly from `/home/admin/matrix-backups/pre-final-fsync-fault-fixes-20260804T173315Z`; do not reapply that rejected revision without a new explicit request.

Verification:

- Final local suite after rollback: 173 tests passed.
- Final deployed Pi suite after rollback: 173 tests passed, with two Node-only tests skipped on the Pi; both pass locally where Node is installed.
- Rendered inline dashboard JavaScript passed `/usr/local/bin/node --check`.
- The later fault-handling changes prompted by the final review were intentionally rolled back at the user's request.
- Backup: `/home/admin/matrix-backups/pre-media-reorganization-20260804T165128Z`.
- Crash-recovery hardening backup: `/home/admin/matrix-backups/pre-media-crash-recovery-20260804T171631Z`.
- Final fsync fault-handling backup: `/home/admin/matrix-backups/pre-final-fsync-fault-fixes-20260804T173315Z`.
- Source/config/media/paired data were backed up before deployment; deployment excluded `venv`, `data`, and `resources/media`.
- Current service after rollback refresh: active/running, master PID `426079`, worker PID `426095`, `NRestarts=0`.
- Hardware lock owner is worker `426095`; a second claim is blocked when tested against `/run/matrix-display/matrix-display.lock`.
- HTTP `/` returns the expected `302` when unpaired; paired-only media API returns `401` without a session.
- Warning-level journal after final restart: none.
- Local/deployed source checksum drift: none.
- Paired registry remains 3 devices; current registry SHA-256 is `c169e7022199c57eaaaa0e7e5a79bdd327d42916ed45dd957eabd4e07c468d24`.

The user chose to exercise the controls in their paired dashboard instead of allowing the agent's automated authenticated move/preview script. Live state confirms that test changed classification in both directions and survived the final service restart:

- Built-in: `built-in/sleep.gif`, `built-in/squares.gif`
- My media: `hermes_self_portrait.png`, `squares_tb.gif`
- Splash reference: `built-in/squares.gif`
- Sleep reference: `built-in/sleep.gif`
- Current config SHA-256: `f6c7fdde7f60bef16d8392d9fe0dff7d1303702887a49965e4874f5b88827460`
- Custom modules: 0

Do not restore the pre-deployment media/config arrangement: the current organization is a legitimate user change. The blocked automated live-check command did not execute, and its local temporary script was removed; no temporary pairing device, cookie, media, or remote verifier state was created.

## Remaining known roadmap

There is no unfinished deployment from advanced custom-canvas animations, paired-device management, the complete built-in editor, weather, saved-palette, or MP4 work. No further feature has been explicitly selected. Follow the user's latest request rather than inventing the next roadmap item.

## Safe deployment procedure

Load these skills before relevant work:

- `software-development:test-driven-development`
- `software-development:ssh-remote-operations`, especially `references/root-hardware-web-media.md`
- `software-development:systematic-debugging` when investigating failures

Then:

1. Inspect current local and deployed files before editing.
2. Add a failing test and observe the expected failure.
3. Implement the minimum behavior and run focused tests.
4. Run the complete local suite.
5. Syntax-check rendered dashboard JavaScript with `/usr/local/bin/node --check`.
6. Inspect the live configuration before migration-sensitive changes.
7. Create timestamped application/config/unit backups.
8. Synchronize source while excluding:
   - `/venv/`
   - `/data/`
   - `/resources/media/`
   - `__pycache__/`
   - `*.pyc`
   - `*.pre-*`
9. Compile changed Python and run the full deployed suite via `/home/admin/matrix/venv/bin/python`, using root where required.
10. Restart `matrix-web.service` and poll readiness rather than sleeping blindly.
11. Verify `is-active`, `MainPID`, `NRestarts`, warning/error journal, process layout, and lock ownership.
12. Run a checksum-based rsync dry-run.
13. Trigger `/pair`, request the newly displayed one-time PIN from the user, and never repeat or store it.
14. Perform an authenticated live API/UI test plus exact framebuffer/physical verification.
15. Restore temporary config/media, remove temporary scripts/cookies, and generate a fresh pairing PIN for the user.

Typical SSH prefix:

```bash
ssh -i /root/.ssh/id_ed25519_matrix \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  admin@192.168.1.4
```

## Security and architecture invariants

- Exactly one process may own the physical Matrix; preserve the hardware lock.
- Do not create a separate Matrix instance for dashboard preview.
- Never decode arbitrary uploaded media in the privileged playback worker.
- Keep sanitization unprivileged and bounded.
- Keep custom modules and palettes as validated data, never executable code.
- Keep `/api/display-frame` pairing-protected and `no-store`.
- Preserve the 64×32 physical/canonical envelope.
- Preserve the 128 MB upload request limit unless a separately tested bounded change is justified.
- `NoNewPrivileges=false` is narrowly required so the privileged parent can transition sanitizer children to `nobody`; do not broaden privilege.
- Pairing PINs are one-time and service restarts invalidate outstanding PINs, while registered paired-device sessions survive. Never persist PINs in this handoff or any other file.
- Paired sessions must remain bound to a server-side device ID; removing that registry entry must invalidate the corresponding browser immediately.
- Keep the paired-device registry atomic, root-owned, mode `0600`, and under the preserved `data` directory.
- Never return a pairing PIN through an API or render it in the browser; it is shown only on the physical Matrix.

## Fresh-session opening checklist

1. Read this file.
2. Read the user’s latest request; it overrides this roadmap.
3. Load matching skills.
4. Inspect current source/live state because PIDs, configuration, and user edits may have changed.
5. If continuing Matrix work, create a task list and maintain one `in_progress` item.
6. Never claim a feature is complete until local tests, deployed tests, service restart, and live physical/exact-preview verification have all succeeded.
