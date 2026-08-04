# Matrix Display Project — Deep Code Review

**Review date:** 2026-08-04  
**Reviewed tree:** `/root/matrix_new`  
**Baseline:** deliberately restored 173-test revision  
**Purpose:** backlog for later improvement work; no application improvements were implemented during this review.

> **Behavior update — 2026-08-04:** The paired media-reclassification API and dashboard controls were subsequently removed at the user's request. Built-in status is now immutable through the web application and derived only from physical placement under `resources/media/built-in/`. Findings that depend on the removed move transaction or `classifyMedia()` are retained as historical review evidence but no longer describe reachable production behavior.

## Executive summary

The project has a strong functional test suite for its size and contains unusually careful handling in several high-risk areas: paired access, media path confinement, `O_NOFOLLOW`, descriptor-relative file operations, atomic configuration replacement, helper-process privilege separation, and exclusive ownership of the physical Matrix.

The review nevertheless found two urgent correctness problems and several high-priority reliability, security, state-management, and browser/physical-parity issues.

The first work cycle should address:

1. Media-move fault handling that can delete both source and destination after an `fsync` failure.
2. Schedule strings accepted by configuration validation but rejected by the display scheduler.
3. Unauthenticated requests that can place an already-paired Matrix into pairing mode.
4. Sanitizer resource limits that are bypassed in the deployed execution path.
5. Explicit commit semantics for config persistence and display reload.
6. Optimistic concurrency for multiple paired dashboards.
7. Browser preview/physical-rendering parity and unsaved-editor state integrity.

Do not implement these as one large refactor. Add a failing regression or fault-injection test for each behavior, make narrowly scoped fixes, deploy, restart, and verify the real Matrix after each major group.

## Scope and method

Reviewed:

- Flask application and API routes
- Configuration schema, migration, and persistence
- Pairing/session/device persistence
- Media upload, sanitization, serving, classification, and removal
- Matrix hardware abstraction and display scheduler
- Built-in and custom module renderers
- Dashboard, advanced canvas editor, live preview, and browser state
- First-run Wi-Fi setup and NetworkManager operations
- systemd units and WSGI startup
- Automated tests, documentation, and dependencies

Measured source footprint, excluding caches, virtual environments, fonts, and binary media:

- 52 reviewed text/source files
- Approximately 9,498 lines
- Python: 41 files / approximately 8,415 lines
- JavaScript: 1 file / 621 lines
- HTML: 4 files / 266 lines
- Largest production files: `webapp.py` 887 lines, `matrix_framework.py` 699, `config_store.py` 654, `custom_canvas.js` 621
- `webapp.create_app()` spans 851 lines; `classify_media()` spans 219 lines
- `config_store.validate_config()` spans 235 lines
- `dashboard.html` is approximately 60 KB with lines up to 2,421 characters
- `custom_canvas.js` is approximately 55 KB with lines up to 1,104 characters

Verification performed:

- Full suite: **173 tests passed**
- Python compilation: passed
- `node --check app/static/custom_canvas.js`: passed
- Focused temporary-directory/mocked fault reproductions were run for media-move sync failures, config post-commit errors, paired-device write divergence, setup-thread failure, and Wi-Fi profile promotion ordering
- `pygount`, `ruff`, `bandit`, `pip-audit`, and coverage tooling were unavailable
- The project is not a Git repository, so history, blame, diff, and tracked-file checks were unavailable

## Severity model

- **Critical:** credible data loss or core service failure requiring first attention
- **High:** security boundary bypass, misleading committed state, user-state loss, or major product correctness failure
- **Medium:** durability, reliability, performance, accessibility, or maintainability defect with bounded impact
- **Low:** hardening, diagnostics, documentation, or portability issue

---

## Critical findings

### CR-01 — An `fsync` failure during media movement can delete both copies

**Status:** Confirmed and reproduced  
**Location:** `webapp.py:643-673`, rollback at `webapp.py:735-746`

The normal move sequence creates a hard link at the destination, syncs the destination directory, updates references, removes the source, and syncs the source directory. If the final source-directory `fsync` fails, the source pathname is already gone. The broad rollback then removes the destination, deleting the only remaining pathname.

Reproduced state:

```text
HTTP 409
source=False
destination=False
```

A failure at the first directory `fsync` has a different inconsistent result: the API reports failure while source and destination remain as the same inode because destination identity was not yet recorded for cleanup.

**Impact:** Complete user-media loss or an ambiguous partially committed move after a reported failure.

**Existing coverage:** Existing race/restart tests cover same-inode recovery, source/destination replacement, collision safety, and normal rollback. They do not cover these post-mutation `fsync` failure states in the restored revision.

**Recommended remediation — not implemented:**

- Model separate states for logical mutation, visible commit, and confirmed durability.
- Never treat an exception after source unlink as proof that the source still exists.
- Preserve a recoverable inode/path through the unlink boundary.
- Make rollback conditional on exact transaction-owned inode identity.
- Return a committed result with an explicit durability warning when logical commit succeeded but the durability barrier failed.
- Add fault tests for destination-directory sync failure, config-directory post-replace sync failure, source-directory sync failure after unlink, and destination disappearance during source unlink.

### CR-02 — Configuration accepts schedule values that can kill the scheduler thread

**Status:** Confirmed and reproduced  
**Location:** `config_store.py:108-114`, `display_controller.py:213-223`, loop at `display_controller.py:322-372`

`_validate_time()` accepts strings such as `"8:0"` by splitting and converting components to integers. Runtime later calls `datetime.time.fromisoformat()`, which rejects the same string.

```text
'8:0' accepted by ConfigStore
DisplayController._is_active(): ValueError: Invalid isoformat string: '8:0'
```

The exception occurs outside the per-module exception boundary in `_run_loop()`, so the display scheduler thread can terminate.

**Impact:** A configuration accepted and persisted by the API can stop scheduled display operation until corrected/restarted.

**Existing coverage:** Time-range behavior is tested for canonical values, but there is no contract test asserting that every accepted schedule is parseable by `_is_active()` and no top-level scheduler survival test for malformed persisted data.

**Recommended remediation — not implemented:** Parse with the exact runtime parser during validation, normalize accepted values to `HH:MM`, and add a top-level scheduler exception boundary with observable health state. Add accepted-value contract tests and corrupted-on-disk recovery tests.

---

## High-priority findings

### HI-01 — Any LAN client can initiate pairing and suspend normal display output

**Status:** Confirmed from route/control flow  
**Location:** `webapp.py:354-362`, authenticated initiation at `webapp.py:398-413`, display pause at `display_controller.py:139-158`

Unauthenticated `GET /pair` creates a PIN when none is active and calls `show_pairing_pin()` even when trusted devices already exist. This bypasses the intended authenticated “pair another device” gate.

**Impact:** A LAN client can replace normal output with the pairing screen for up to ten minutes and repeat the request after expiry. Someone with physical visibility can remotely initiate pairing and then read the PIN.

**Existing coverage:** Pairing PIN, expiry, attempts, lockout, additional pairing, and route authentication are tested, but there is no test proving unauthenticated `/pair` cannot initiate pairing after initial provisioning.

**Recommended remediation — not implemented:** Only auto-issue a PIN when the device store is empty. Once paired devices exist, require an authenticated initiation token/state before `/pair` displays or accepts a new PIN. Add a repeated-request denial-of-service test.

### HI-02 — The deployed sanitizer bypasses its CPU and memory limits

**Status:** Confirmed from production wiring  
**Location:** `app/wsgi.py:57-68`, `webapp.py:179-194`, `media_sanitizer.py:128-136`

Production launches the helper directly as `nobody`. Inside the helper, `drop_privileges()` returns immediately when EUID is not root, before applying `RLIMIT_CPU` and `RLIMIT_AS`.

**Impact:** Authenticated uploaders can drive an unbounded Pillow/FFmpeg helper. The parent timeout bounds elapsed time but not memory; termination can also leave a child FFmpeg process running. The main service accepts uploads up to 128 MB and runs as root.

**Existing coverage:** Format validation, sanitizer subprocess behavior, helper identity, timeout, and malformed media are tested. Tests do not assert effective limits in the exact deployed non-root-helper path or process-tree cleanup.

**Recommended remediation — not implemented:** Separate `apply_resource_limits()` from privilege dropping and always apply limits. Start the helper in a killable process group/cgroup, terminate descendants on timeout, and verify effective limits in an integration test matching systemd/Gunicorn wiring.

### HI-03 — Configuration commit errors have ambiguous success semantics

**Status:** Confirmed and reproduced  
**Location:** `config_store.py:623-640`

`_write()` performs `os.replace()` before syncing the parent directory. If parent-directory `fsync` raises, the new configuration is already visible but `update()` raises.

```text
OSError: directory fsync fault
on-disk config: requested change already visible
```

**Impact:** Callers receive failure while state has committed. Retrying a state-dependent operation can produce unexpected results. Initialization migrations have the same ambiguity.

**Existing coverage:** Atomic replacement and parent-directory sync are tested under normal conditions, but post-`os.replace()` failure semantics are not covered in the restored revision.

**Recommended remediation — not implemented:** Define a distinct post-commit durability exception/result carrying the committed config. Require callers to distinguish pre-commit failure from committed-but-not-confirmed-durable. Do not roll back filesystem state based solely on an exception raised after commit.

### HI-04 — Config save can succeed while the API reports HTTP 500

**Status:** Confirmed  
**Location:** `webapp.py:809-838`, frontend `app/templates/dashboard.html:148,155-157`

The route persists config, then calls `controller.reload()` outside exception handling. A reload exception returns an HTML 500 after durable save. The frontend cannot parse the HTML and reports “Unexpected server response.”

**Impact:** Users cannot tell whether changes were saved and may retry or overwrite state. This contradicts the classification endpoint’s best-effort reload semantics at `webapp.py:765-775`.

**Existing coverage:** Classification reload failure is covered; configuration-save reload failure is not.

**Recommended remediation — not implemented:** Treat persistence as commit. Return saved config plus `display_reloaded:false` and a warning if activation fails. Standardize JSON API errors and test the exact frontend message.

### HI-05 — Multiple paired dashboards can silently overwrite each other

**Status:** Confirmed design defect  
**Location:** `app/templates/dashboard.html:138-156`, `webapp.py:804-838`, `config_store.py:646-654`

Each dashboard holds a full snapshot and submits almost the entire configuration. Writes are serialized but have no revision or conditional update. A stale browser changing one setting can overwrite unrelated changes saved by another paired browser.

**Impact:** Legitimate multi-device use can lose recent user configuration without warning.

**Existing coverage:** Multiple paired devices and update validation are tested; there is no two-client stale-write test.

**Recommended remediation — not implemented:** Add a monotonic config revision or ETag; require `If-Match`/revision for updates and return 409 on stale writes. Prefer narrow PATCH-style endpoints for independent settings.

### HI-06 — Preview silently persists unrelated unsaved edits

**Status:** Confirmed  
**Location:** `app/templates/dashboard.html:133,155-160,207,217`

The UI says changes remain local until saved. Previewing a custom module calls `saveConfig(true)`, committing the complete dashboard form. Media “Use splash/sleep” also performs a full config save.

**Impact:** A harmless preview or media action applies unrelated draft device, weather, playlist, pairing-limit, or editor changes.

**Existing coverage:** Source-string tests assert preview/save functions exist, but do not validate persistence side effects.

**Recommended remediation — not implemented:** Add non-persistent draft preview or clearly scope and disclose persistence. Use dedicated media-assignment endpoints. Add browser/API tests proving unrelated draft fields remain unsaved.

### HI-07 — Media reclassification can discard unsaved simple-module edits

**Status:** Confirmed state-flow defect  
**Location:** `app/templates/dashboard.html:153,214-215`, `app/static/custom_canvas.js:182-189,605-609`

Simple-module values primarily live in DOM controls. `classifyMedia()` updates the model and rerenders custom modules without first reconciling current simple-editor values. Advanced modules mutate the model directly, so behavior differs by editor type.

**Impact:** Moving a media file can silently erase unsaved simple text, mode, font, position, speed, or loop edits.

**Existing coverage:** Browser source assertions cover state-adoption code but not actual DOM interaction and dirty-state preservation.

**Recommended remediation — not implemented:** Adopt one authoritative frontend state store updated by every input. Until then, collect DOM state before any rerender. Add an E2E test that edits simple text, classifies media, and verifies the edit remains.

### HI-08 — Browser custom-canvas preview is not physically faithful

**Status:** Confirmed algorithm mismatch  
**Location:** browser `app/static/custom_canvas.js:248-323`; physical renderer `modules/custom_text.py:110-226`; BDF drawing `matrix_framework.py:219-244`

Differences include approximate browser text metrics versus BDF glyph pixels, polygon rotation around a bounding-box center versus arithmetic centroid, rectangle edge conventions, native browser ellipses versus a 36-segment physical polygon, antialiasing, and rounding.

**Impact:** Controls can appear to work in the editor but render differently on the physical Matrix, undermining preview trust.

**Existing coverage:** Python renderer tests are strong and two Node helper harnesses exist, but there are no browser-to-physical golden-frame parity tests.

**Recommended remediation — not implemented:** Build a canonical software renderer using physical rounding and BDF rules, or expose server-rendered draft frames. Add pixel-hash fixtures for every primitive, rotation, clipping, z-order, interpolation, text, and media.

### HI-09 — Animated media in browser preview is detached from module timeline

**Status:** Confirmed  
**Location:** `app/static/custom_canvas.js:279-310,512-525,578-585`; physical timeline at `modules/custom_text.py:162-172,229-269`, `matrix_framework.py:386-449`

Browser `<img>`/`<video>` elements play on wall-clock time independently of selected timeline frame, FPS, keyframes, or layer appearance. Physical rendering deterministically samples media from the module timeline.

**Impact:** Timeline scrubbing, frame duplication, onion skinning, delayed appearance, multi-layer synchronization, GIF duration, and MP4 resampling are misleading.

**Existing coverage:** Physical media-frame selection is tested; browser synchronization is not.

**Recommended remediation — not implemented:** Sample media by module frame/FPS or use canonical server-rendered frames. Add delayed-appearance, variable-duration GIF, FPS conversion, looping, and scrub parity tests.

### HI-10 — MP4 playback ignores “Keep aspect ratio”

**Status:** Confirmed  
**Location:** `modules/media_player.py:26-34`, `matrix_framework.py:529-579`

`keep_aspect` reaches `show_video()` but is never read. FFmpeg always scales directly to width/height, while images and GIFs honor the setting.

**Impact:** Videos stretch despite the user-selected control, creating inconsistent behavior across media types.

**Existing coverage:** Layout forwarding is covered for media modules, but no test inspects generated FFmpeg filters for both aspect modes.

**Recommended remediation — not implemented:** Generate conditional scale/pad filters and test exact filter construction plus real output dimensions/padding.

### HI-11 — Controller stop timeout can still block indefinitely

**Status:** Confirmed lock/lifecycle defect  
**Location:** `display_controller.py:91-101`, lock held at `display_controller.py:295-307`

`stop()` joins the worker for five seconds, then unconditionally acquires `_display_lock`. A stuck module holds that lock for its entire run, so the nominal timeout is ineffective.

**Impact:** `/api/control/stop`, shutdown, or restart can hang if a module/hardware/media operation does not respond to cancellation.

**Existing coverage:** Normal start/stop/reload and interruption are tested; a non-cooperative module surviving the timeout is not.

**Recommended remediation — not implemented:** Restrict hardware lock scope to frame submissions, or clear only from the scheduler thread. If join times out, report failure without waiting on a lock held by the live worker. Add a deliberately non-cooperative module test.

---

## Medium-priority findings

### ME-01 — Paired-device persistence can diverge between memory and disk

**Status:** Confirmed and reproduced  
**Location:** `pairing.py:40-87`

`add()` and `remove()` mutate `_devices` before `_write()` succeeds and do not roll back on failure. The store also omits parent-directory `fsync` after replace.

**Impact:** Current-process authorization can differ from restart behavior; additions can vanish and removals can reappear after failure/power loss.

**Existing coverage:** Normal add/remove persistence is tested; injected write failure and directory durability are not.

**Recommended remediation — not implemented:** Write a candidate snapshot first and swap in-memory state only after success. Reuse a common atomic JSON utility with file sync, replace, directory sync, permission policy, and schema validation.

### ME-02 — Upload and delete acknowledgements are not crash-durable

**Status:** Confirmed  
**Location:** upload `webapp.py:497-515`; sanitizer output `media_sanitizer.py:196-201`; delete `webapp.py:795-802`

Uploads do not sync sanitized output or the media directory before HTTP 201. Deletion unlinks and returns without directory sync. There is no aggregate media count/storage quota.

**Impact:** A successful upload may disappear/be incomplete after power loss; a successful deletion may reappear. Repeated valid uploads can exhaust writable storage.

**Existing coverage:** Normal upload/delete, size limit, collision, malformed media, and references are covered; crash durability and storage quotas are not.

**Recommended remediation — not implemented:** Sync output then containing directory before acknowledgement; sync directory after deletion; expose free-space/media quotas and reject uploads before exhaustion.

### ME-03 — Wi-Fi profile promotion deletes the old working profile first

**Status:** Confirmed command ordering  
**Location:** `network_setup.py:234-249`

`promote_home()` deletes the stable profile before renaming/enabling the candidate. If modification fails, the old profile is already gone.

**Impact:** A transient NetworkManager failure can leave the appliance without a durable home connection after setup had connected.

**Existing coverage:** Connection command construction and cleanup are tested; failed promotion preserving an existing profile is not.

**Recommended remediation — not implemented:** Keep the old profile until the candidate is renamed, autoconnect-enabled, activated, and verified. Use a temporary backup name or rollback plan and fault-test every command boundary.

### ME-04 — Setup thread-start failure permanently wedges provisioning

**Status:** Confirmed and reproduced  
**Location:** `setup_portal.py:49-63`, PIN consumption `setup_portal.py:143-163`

`submit()` marks `_busy` and `connecting` before `Thread.start()`. If start fails, state is never restored and the already-verified PIN has been consumed.

**Impact:** All subsequent attempts report “already running” until service restart.

**Existing coverage:** Successful/failing worker operation is tested, but immediate thread-start failure is not.

**Recommended remediation — not implemented:** Roll back state when thread construction/start fails and issue/display a new PIN. Add a deterministic start-failure test.

### ME-05 — Long-lived bearer cookies travel over plaintext LAN HTTP

**Status:** Security posture risk  
**Location:** `webapp.py:60-66`, `app/wsgi.py:78-84`, `deploy/matrix-web.service:13-21`

Sessions last 365 days, the service binds to the LAN over HTTP, and `SESSION_COOKIE_SECURE` is absent. `HttpOnly` and `SameSite=Strict` are useful but do not prevent passive cookie capture. There is no explicit CSRF token system; SameSite is the primary cross-site defense.

**Impact:** Anyone capable of observing LAN traffic can replay a paired session until revocation/expiry/key rotation.

**Existing coverage:** Cookie lifetime, persistence, pairing generation, and unpairing are tested; transport security and CSRF are not.

**Recommended remediation — not implemented:** Prefer local HTTPS with managed device certificate/trust guidance, shorten/rotate sessions, optionally bind to a trusted interface/firewall, and add explicit CSRF tokens/Origin validation for mutations.

### ME-06 — One-frame ping-pong browser playback runs out of bounds

**Status:** Confirmed and reproduced  
**Location:** `app/static/custom_canvas.js:578-585`, physical behavior `modules/custom_text.py:32-37`

For one frame, both ping-pong boundary checks execute and then increment frame 0 to 1, 2, 3, and onward. Physical rendering correctly remains on frame 0.

**Impact:** Browser preview becomes invalid and differs from hardware for a valid configuration.

**Existing coverage:** Physical three-frame playback exists; browser frame counts 1/2 and playback modes are untested.

**Recommended remediation — not implemented:** Special-case one frame or centralize a shared sequence algorithm. Contract-test frame counts 1 and 2 for loop, hold, and ping-pong.

### ME-07 — Numeric UI controls can disagree with the saved model

**Status:** Confirmed  
**Location:** `app/static/custom_canvas.js:217-224,388-393,590-600`; brightness validation `config_store.py:316-320`

Many advanced inputs have no bounds. FPS silently clamps model values while leaving entered UI text unchanged. Empty values can become `NaN`, serialized as `null`, and fail only during save. Python brightness validation accepts booleans because `bool` is an `int`.

**Impact:** Misleading controls, late/confusing errors, and brightness `true` becoming physical brightness 1.

**Existing coverage:** Many schema bounds are tested, but boolean brightness and browser normalization/invalid-state behavior are not.

**Recommended remediation — not implemented:** Generate controls from a shared schema, reflect normalized values immediately, show inline validation, disable save/preview while invalid, and reject booleans in every numeric validator.

### ME-08 — API error contracts are inconsistent

**Status:** Confirmed  
**Location:** `webapp.py:809-885`, client `app/templates/dashboard.html:148`

The frontend expects JSON, but malformed forced JSON, `abort()`, controller errors, and uncaught reload errors can produce HTML responses. The UI collapses these into “Unexpected server response.”

**Impact:** Actionable diagnostics are lost and committed-state ambiguity becomes harder to understand.

**Existing coverage:** Success and many expected JSON errors are tested; malformed JSON/content type and controller exceptions are incomplete.

**Recommended remediation — not implemented:** Add an `/api/*` JSON error handler with stable `code`, `error`, and optional detail fields. Require JSON content type instead of `force=True`. Add endpoint contract tests.

### ME-09 — Live-frame polling regenerates/transfers PNGs ten times per second per browser

**Status:** Confirmed performance risk  
**Location:** `app/templates/dashboard.html:198`, `webapp.py:840-847`, `frame_capture.py:175-180`

Polling runs even when revision is unchanged or the tab is hidden. Each request generates/transfers a PNG and decodes an `ImageBitmap`.

**Impact:** CPU/network usage scales with paired dashboards and can compete with rendering on constrained hardware.

**Existing coverage:** Frame response and revision header are tested; unchanged-frame caching, hidden-tab pause, and multi-client load are not.

**Recommended remediation — not implemented:** Cache PNG bytes by revision, support conditional requests/304, pause when `document.hidden`, or notify changes through SSE/WebSocket.

### ME-10 — Maximum canvas timelines have expensive redraw behavior

**Status:** Confirmed complexity risk  
**Location:** `app/static/custom_canvas.js:42-68,448-478,588-600`

Editor refresh rebuilds full cards/timelines, repeatedly sorts/scans keyframes, and can create thousands of DOM cells at schema maxima.

**Impact:** Input latency and browser freezes on valid large modules.

**Existing coverage:** Functional maxima are validated, but there are no frontend performance budgets.

**Recommended remediation — not implemented:** Cache resolved frames, incrementally update affected nodes, virtualize timelines, and set measurable interaction budgets for maximum configurations.

### ME-11 — Dynamic dashboard controls have accessibility gaps

**Status:** Confirmed by source review  
**Location:** `app/templates/dashboard.html:44-60,70,133,197-203`; `app/static/custom_canvas.js:331-361,426-478,595-600`

Messages lack live-region semantics; location combobox keyboard behavior is incomplete; canvas manipulation is pointer-only; some icon/remove controls lack meaningful accessible names; timeline state relies heavily on symbols/CSS; motion/autoplay lacks reduced-motion behavior.

**Impact:** Keyboard and assistive-technology users cannot reliably operate key features.

**Existing coverage:** No axe, keyboard-only, or browser accessibility suite.

**Recommended remediation — not implemented:** Complete ARIA combobox semantics, add `aria-live`, labels, keyboard manipulation, focus styling, and reduced-motion handling. Add automated axe checks plus manual keyboard verification.

### ME-12 — Frontend tests mostly assert implementation strings rather than behavior

**Status:** Confirmed  
**Location:** `tests/test_webapp.py:189-279,315-339,374-550`

Many tests use `assertIn()` against minified HTML/JS fragments. These can pass when controls exist but are disconnected. Only small Node harnesses execute isolated helper behavior.

**Impact:** State-loss, preview side effects, malformed API responses, accessibility, and browser/physical mismatches can regress despite a green suite.

**Recommended remediation — not implemented:** Add Playwright tests against the real Flask app and shared renderer fixtures. Retain route/schema unit tests, but replace fragile source-fragment assertions with user-flow behavior.

---

## Low-priority and architectural findings

### LO-01 — `create_app()` and the dashboard are monolithic composition/state layers

**Location:** `webapp.py:37-887`; `app/templates/dashboard.html:8-220`; `app/static/custom_canvas.js:1-621`; `app/static/custom_canvas.css:1`

`create_app()` combines dependency wiring, authentication, media repository logic, subprocesses, transaction orchestration, and routes. The dashboard combines minified CSS, global state, API client, pairing, media, playlist, module editors, and polling. Canvas code depends on and overrides dashboard globals, including duplicate `customModulesFromEditor()`/renderer implementations.

**Recommended remediation — not implemented:** Incrementally introduce Flask blueprints and explicit services (`MediaRepository`, sanitizer, classification service), ES modules, a single frontend state store, formatted assets, and one implementation per editor function. Preserve behavior with characterization tests before extraction.

### LO-02 — Canvas schema/semantics and media-reference traversal are duplicated

**Location:** canvas behavior `custom_canvas.js:5-99,248-323`, `config_store.py:147-303`, `modules/custom_text.py:40-226`; references `webapp.py:266-322`, `config_store.py:543-566`, `dashboard.html:214`

**Impact:** Every new layer/property/reference-bearing path requires coordinated changes; omission can cause validation rejection, preview drift, stale references, or unsafe deletion.

**Recommended remediation — not implemented:** Define a versioned declarative canvas schema and canonical Python reference iterators/replacers. Use shared JSON fixtures to test JS and Python consumers.

### LO-03 — Hardware activity occurs at WSGI import time

**Location:** `app/wsgi.py:48-75`

Importing the module reads/writes stores, acquires the hardware lock, initializes hardware, may start display activity, and registers shutdown callbacks.

**Impact:** Diagnostics/tooling have physical side effects; startup is tightly coupled to hardware; the single-worker constraint is implicit.

**Recommended remediation — not implemented:** Add explicit lifecycle/bootstrap management or a dedicated display process. Document and startup-test the one-worker invariant.

### LO-04 — `Matrix` combines hardware, drawing, media decoding, capture, and demo code

**Location:** `matrix_framework.py:18-619`, demo `matrix_framework.py:640-699`

**Recommended remediation — not implemented:** Separate hardware adapter, drawing renderer, media locator, image/video decoder, and capture observer; move demo code to an example executable.

### LO-05 — PIL image/GIF handles are not explicitly closed in two public paths

**Location:** `matrix_framework.py:470-478`, `480-527`

`load_image()` and `show_gif()` call `Image.open()` without context management. Long-running/repeated playback relies on object finalization to release descriptors.

**Impact:** Potential descriptor/resource pressure and harder deterministic cleanup after decoding errors or early returns.

**Existing coverage:** Rendering behavior is covered; descriptor lifetime is not.

**Recommended remediation — not implemented:** Use context managers and return detached copies; ensure GIF closes on every stop/max-duration path. Add repeated-playback descriptor-count tests.

### LO-06 — Deployment host/port settings are duplicated and misleading

**Location:** `deploy/matrix-web.service:13-21`, `app/wsgi.py:78-84`, `display_controller.py:166-175`

The unit advertises `MATRIX_HOST`/`MATRIX_PORT`, but Gunicorn bind and pairing display independently hardcode `0.0.0.0:5000`.

**Recommended remediation — not implemented:** Use one deployment configuration source and pass public host/port into server and pairing display. Add unit/startup validation.

### LO-07 — Session-key and captive-DNS setup lack full filesystem hardening

**Location:** session key `app/wsgi.py:25-45`; DNS copy `setup/first_run.py:29-30,69-70,131-136`

The new session key is file-synced but its directory is not. Captive DNS uses direct `copyfile()` to a fixed root-owned path, follows an existing destination symlink, is non-atomic, and begins before the cleanup `try/finally`.

**Recommended remediation — not implemented:** Use shared symlink-safe atomic write/copy helpers with parent-directory sync and cleanup around all mutations.

### LO-08 — Paired-device files lack entry schema validation

**Location:** `pairing.py:31-36,60-62`

Only the top-level list is validated. An entry such as `{}` can load, then cause `KeyError` during authorization.

**Recommended remediation — not implemented:** Validate every ID/name/timestamp, reject duplicates, enforce permissions, and provide safe startup recovery/diagnostics for malformed state.

### LO-09 — Documentation and dependencies have drifted

**Location:** `README.md:23-25`, `requirements.txt:1-4`, runtime references in `webapp.py`, `matrix_framework.py`, `network_setup.py`

README says 12-hour sessions invalidated by restart, while runtime uses persistent 365-day sessions. README says 16 MB and omits MP4, while runtime/UI allow 128 MB and MP4. OpenCV is listed but unused. RGB Matrix bindings, FFmpeg/ffprobe, NetworkManager, absolute tool paths, and `wlan0` assumptions are incompletely documented.

**Recommended remediation — not implemented:** Update documentation from tested constants, remove unused OpenCV, document/install native dependencies, and make tool/interface paths configurable with startup checks.

### LO-10 — Operational errors are under-observed

**Location:** media inspection `webapp.py:156-176`; location search `webapp.py:854-863`; module state `display_controller.py:252-263,295-311`

Unreadable media is silently skipped, location errors are not logged, and only one mutable `last_error` is exposed.

**Recommended remediation — not implemented:** Add structured bounded logs/health history without exposing secrets or Wi-Fi credentials. Surface actionable but safe UI diagnostics.

### LO-11 — Frame-capture exactness depends on callers using wrappers

**Location:** `matrix_framework.py:282-308`, `frame_capture.py:93-180`

`Matrix.scroll_text()` draws via `graphics.DrawText()` directly and does not mirror text into `FrameCapture`. If reused, browser live preview can omit hardware text.

**Recommended remediation — not implemented:** Route every public draw/display API through capture-aware primitives and add parity coverage for each public method.

---

## Positive observations worth preserving

- 173 tests pass quickly and cover configuration validation, route pairing, media sanitization, path confinement, move races, migration, hardware ownership, render helpers, weather, setup, and interruption behavior.
- Media path validation uses strict names, root confinement, regular-file checks, and `O_NOFOLLOW` in sensitive paths.
- Media classification rejects unrelated collisions and verifies inode identity across race windows.
- Sanitization re-encodes media instead of trusting extension/MIME metadata.
- Configuration replacement writes and syncs a temporary file, atomically replaces, and syncs the parent directory in the normal path.
- The systemd service has meaningful hardening (`ProtectSystem`, private temp, restricted writable paths, kernel/control-group protections) even though root hardware access remains.
- The physical display has explicit cross-process lock ownership and a one-worker deployment.
- Module execution generally supports cancellation through `stop_event`.
- Built-in controls are grouped and provide reset-to-default behavior.
- Browser live frame uses captured physical submissions rather than pretending the editor alone is exact.

## Recommended implementation sequence

### Phase 1 — Prevent data loss and scheduler failure

1. Add the media `fsync` fault tests described in CR-01.
2. Introduce explicit logical-commit/durability state; fix media rollback without deleting independent replacements.
3. Canonicalize schedule validation and add scheduler survival tests.
4. Deploy, restart, run full local/deployed suites, verify hardware lock, live HTTP, journal, persistent checksums, and physical output.

### Phase 2 — Close pairing and sanitizer security gaps

1. Gate pairing initiation once trusted devices exist.
2. Apply sanitizer resource limits regardless of starting UID and kill process trees reliably.
3. Add realistic integration tests matching production helper identity and systemd/Gunicorn behavior.
4. Decide and document LAN HTTPS/session/CSRF policy.

### Phase 3 — Make commits and multi-client state explicit

1. Separate pre-commit, committed, activation, and durability outcomes.
2. Standardize JSON API errors.
3. Add config revision/ETag conflict handling and narrow update endpoints.
4. Stop preview/media actions from saving unrelated drafts.

### Phase 4 — Protect frontend state and rendering trust

1. Replace DOM-as-state with a single store.
2. Fix one-frame playback and numeric-control normalization.
3. Add browser E2E tests for save, preview, rerender preservation, malformed responses, and two-client conflict.
4. Build canonical browser/physical rendering fixtures and timeline-synchronized media preview.

### Phase 5 — Durability and provisioning

1. Create one atomic JSON/file persistence utility.
2. Make device changes, upload, delete, session-key creation, and setup files crash-durable.
3. Make Wi-Fi profile promotion rollback-safe.
4. Add storage quotas and free-space checks.

### Phase 6 — Incremental refactoring and operations

1. Extract Flask blueprints/services behind existing tests.
2. Modularize and format frontend assets; remove duplicate global implementations.
3. Optimize live-frame delivery and timeline rendering.
4. Add accessibility and performance gates.
5. Align README, dependencies, deployment configuration, and native prerequisite checks.

## Definition of done for future fixes

For every improvement selected from this review:

- Add a test that fails on this restored baseline before implementation.
- Keep unrelated behavior and persistent state unchanged.
- Run all local tests and language syntax checks.
- Back up source before deployment; exclude `venv/`, `data/`, `resources/media/`, caches, bytecode, and pre-change backups.
- Run the deployed suite on the Matrix.
- Restart affected services after each major deployed change.
- Verify service state, restart count, HTTP readiness, actual hardware-lock exclusion, and warning/error journal output.
- Verify local/deployed source parity.
- Verify paired-device, config, and media checksums/state where applicable.
- Validate browser behavior and physical Matrix output, not implementation alone.
- Update this document by marking findings resolved only after deployed verification.
