# Matrix web app: architecture plan for reliability and longevity

> Owner approved 2026-10-31: P1 now; P2-P4 as background work between features; "no build step" rule for the front end; recorded in GitLab #33 and docs/FUTURE_DEVELOPMENT.md. Written against main 177bc0f.
> Execution: one builder per step (see AGENTS.md), each step its own MR, behaviour-preserving.

**Goal:** make the control panel fast to load, hard to break, and cheap to change, without a rewrite and without new runtime dependencies on the Pi.

**Recommendation in one line:** keep Flask; split the 2,800-line `create_app()` into blueprints on a shared services object (strangler migration, one domain per MR); split the 540 KB single-page dashboard into a thin shell plus lazily loaded native ES modules (no build step); protect both with route-manifest, page-weight and contract tests.

---

## 1. What the code looks like today (measured, not assumed)

| Fact | Evidence |
|---|---|
| One function holds the whole server | `webapp.py` 3,455 lines; `create_app()` runs lines 672-3455 (~2,780 lines), 48 routes, ~100 nested helper closures, 9 `nonlocal` mutations. No blueprints anywhere (`grep Blueprint` = 0). |
| Page is a monolith | `dashboard.html` 243 KB, mostly single-line inline JS; plus 4 jinja-included editor scripts (~95 KB) and 2 static scripts (`custom_canvas.js` 178 KB, `screen_media.js` 22 KB). ~540 KB of source, all global functions, no modules, no build. |
| First load is heavy | Dashboard HTML 509 KB raw (117 KB gzip), nothing compressed, of which one inline `initialMedia` literal is 405 KB. |
| Tests are one huge file | `tests/test_webapp.py` 22,554 lines (125 test files total); full suite ~20 min in CI. |
| Prod is constrained | Raspberry Pi (aarch64), gunicorn 1 worker x 4 threads (`deploy/matrix-web.service`), fixed venv, `ship.py` deploys only `.py` / `app/` / `modules/` / `setup` and cannot pip install or ship data files (gap tracked in #33). |
| Production incident found while measuring (2026-10-31) | `pair_submit` held the PIN-display and device-store locks while `end_pairing()` waited, unbounded, for the display lock that a running module (Sleep GIF) holds for its whole run; all 4 gunicorn threads ended up queued behind it and port 5000 stopped answering. Fixed in !186 (bounded wait), but it shows why shared closure state and lock ordering need isolating (P2) and a health/watchdog endpoint (P1). |
| Server render is no longer the problem | ~20 ms per dashboard request after !185; remaining cost is payload and browser parse/execute (hotfix in flight). |

Why this hurts reliability and longevity:
- Any change to one route risks all others: they share closure state, so nothing can be tested or reviewed in isolation.
- Security-sensitive code (pairing, release-acceptance, restart) lives in the same function as cosmetic routes, so every webapp diff is high-blast-radius.
- The front end cannot be loaded, cached or tested in pieces, so page weight only grows.
- 22k-line test file: slow to navigate, slow to shard, easy to leave dead tests.

## 2. Options considered

| Option | Verdict | Reasoning |
|---|---|---|
| **A. Flask application factory + blueprints + services object** | **Adopt** | Official Flask pattern for exactly this problem. Same dependency, same gunicorn, same `create_app` signature; can be done route-by-route with the route table unchanged. |
| B. Rewrite on FastAPI/Starlette | Reject | Needs new deps on the Pi, async rewrite of 3.4k lines + 22k lines of tests, and the code is not I/O-bound enough to gain. High risk, little reliability win. |
| C. SPA with a build (React/Preact + Vite) | Defer | Would solve modularity but adds a Node toolchain and build artefacts that `ship.py` cannot deploy today, and means rewriting ~2,700 lines of canvas JS. Revisit only if step F3 below still leaves state management painful. |
| D. htmx / server-rendered fragments | Reject for editors | Great for CRUD pages, poor fit here: editors hold large unsaved client-side drafts, live previews and one cross-editor Save (`configBody()`). Could suit small read-only panels later. |
| E. Separate pages per section ("split the page") | Reject as stated, achieve the goal differently | The single draft + single Save spans all editors; separate pages would lose cross-editor state. Lazy-loading sections inside one page gives the same load-time win without breaking that. |
| **F. Native ES modules + dynamic `import()`, no build step** | **Adopt** | Supported by all current browsers, works offline on the LAN, same-origin so no import map needed, each editor loads on first open, files cache independently. |
| G. Flask-Compress / Flask-Caching etc. | Skip | A 60-line stdlib gzip hook (in the hotfix) avoids a new dependency in the Pi venv. |

## 3. Target architecture

### 3.1 Backend (Flask, same deps)

```
webapp.py                     # thin shim: re-exports create_app, APPLICATION_VERSION (tests, wsgi, ship.py keep importing it)
matrix_web/
  __init__.py                 # create_app(...) factory: SAME signature as today
  services.py                 # MatrixServices dataclass in app.extensions["matrix"]: controller, config_store,
                              #   pairing, device_store, media_root, caches, locks, secrets (replaces closure variables)
  security.py                 # requires_pairing, paired-action/CSRF signal, client-contract header, error handlers
  http.py                     # compression, cache headers, Server-Timing, slow-request log
  blueprints/
    pairing.py                # /pair*, /api/devices*, /logout          (security-sensitive: second reviewer)
    dashboard.py              # /, dashboard data endpoints (catalogues, ETag)
    media.py                  # /api/media*, /media/*, screen-media, media-timeline
    config.py                 # /api/config, /api/scroll-timing/*
    providers.py              # rss/astronomy status, locations, openweather secrets
    preview.py                # edit-preview, device-preview, preview/*
    system.py                 # control, restart, display-name, logs, status, display-frame, health, directory
    release.py                # release-acceptance transaction      (security-sensitive: second reviewer)
```

Rules:
1. Helpers stop being closures; they become plain functions that take `services` (or read `current_app.extensions["matrix"]`). The 9 `nonlocal` variables become attributes on a small state object with its own lock.
2. `create_app(...)` keeps its exact parameters; tests and `app/wsgi.py` do not change.
3. Endpoint names change with blueprints (`pair` becomes `pairing.pair`): update `url_for` calls and templates in the same MR, and pin every route with a manifest test (below).
4. Security-sensitive blueprints (`pairing`, `release`) stay in AGENTS.md second-reviewer scope and are extracted last or in their own MR with the auditor.

### 3.2 Front end (no build step)

```
app/templates/dashboard.html         # shell: layout, playlist, status, save bar (<60 KB)
app/static/js/core/{api,state,draft,preview}.js
app/static/js/editors/{rss,calendar,astronomy,announcement,weather,lava,media,custom_text,canvas}.js
app/static/js/main.js                # <script type="module">; dynamic import() of an editor on first open, idle-time preload
```

- Heavy static data (media catalogue, font advances, holiday catalogue, icon art, timezones) served from paired, read-only JSON endpoints with ETag / content-hash URLs, loaded after first paint or when their editor opens.
- HTML stays `no-store`; JS/CSS/JSON get content-hash URLs plus long-lived `Cache-Control`; everything text is gzipped.
- Migration bridge: while editors move one by one, expose the existing globals through a single `window.matrix` object so the browser/acceptance probes keep working; delete the bridge at the end.
- One-time reformat of the single-line JS into readable source (formatting only, reviewed by diff-ignore-whitespace) so future changes are reviewable.

### 3.3 Reliability layer

- `GET /api/health`: unauthenticated, no I/O beyond memory, for a watchdog.
- `Server-Timing` header and a log line for any request over 500 ms, so regressions like the font-parsing one show up in minutes, not weeks.
- Golden **route manifest** test: list of `(rule, methods, auth requirement, endpoint)` that must not change unless the MR says so.
- **Page-weight budget** test in CI: dashboard HTML <= 60 KB raw; first-load transferred bytes <= 150 KB gzip; fail the pipeline when exceeded.
- **Client-contract test**: one shared constant for `MATRIX_CLIENT_CONTRACT`, asserted in both Python and the module bundle.
- Split `tests/test_webapp.py` by blueprint into `tests/web/test_<blueprint>.py` with a shared fixture module; shard CI by directory.
- Optional later: `gunicorn.conf.py` in the app root (gunicorn reads it from the working directory by default) to add `--max-requests` with jitter and sane timeouts without needing a unit-file install. Needs a check that `WorkingDirectory` is the app root and that `ship.py` deploys the file.

## 4. Phases (each step = one MR, behaviour-preserving, own tests)

| Phase | Content | Exit criteria |
|---|---|---|
| **P0 (in flight)** | Hotfix: gzip + cache headers; slim dashboard payload; measurement script. | Dashboard first load <= 150 KB gzip; baseline vs after numbers recorded in GitLab. |
| **P1 safety net** | Route manifest test; page-weight budget test; `/api/health`; Server-Timing + slow-request log; `tests/web/` fixture module (no test moves yet). | Tests green; manifest equals today's 48 routes. |
| **P2 backend strangler** | Extract in this order: `system` (display-name, logs, status), `providers`, `config`, `media`, `preview`, `dashboard`, then `pairing` and `release` with the auditor. Introduce `services.py` first (MR 0) with no route moves. After each MR: manifest unchanged, moved tests live in `tests/web/`. | `create_app()` < 200 lines; no module > 600 lines; zero `nonlocal` in `webapp.py`. |
| **P3 front-end modules** | MR 0: shell + `core/*` + bridge. Then one editor per MR (smallest first: astronomy, rss, calendar, announcement, weather/lava, media, custom text, canvas). | `custom_canvas.js` and the inline script gone; each editor loads on first open; browser acceptance probes unchanged and green. |
| **P4 deployment hardening** | Teach `ship.py` the new package dirs / `app/static/js` / data files (fix the #33 data-file gap first); optional `gunicorn.conf.py`; remove the bridge; update docs. | Dry run and a real ship on the Pi succeed with the new layout; rollback verified. |
| **P5 optional** | Re-evaluate Preact/Vite only if P3 leaves draft/state code hard to maintain. | Decision recorded, not assumed. |

Rough size: P1 ~1-2 days, P2 8-10 small MRs, P3 9-10 small MRs, P4 ~1 day. Each MR stays under the "CI-green small change" bar so merges do not need owner checks, except `ship.py`, pairing, secrets and release-acceptance, which use the second reviewer.

## 5. Tests and validation per step

- Route manifest unchanged (or the diff is exactly the intended one).
- Existing tests moved, not rewritten; add a RED test first for any behaviour found missing.
- Browser: `verify_module_ux.py`, calendar/rss/astronomy verifiers on the 5001/5002 previews for each editor MR; Chromium tests that load the dashboard.
- Budget tests: payload size, number of blocking scripts, time-to-interactive in headless Chrome with 4x CPU throttle (<= 2.5 s target on x86 as a proxy for the Pi).
- Rollback: every step ships behind the normal `ship.py` backup/rollback; since routes and data formats are unchanged, rollback is a plain code revert.

## 6. Risks and trade-offs

- **Blueprint endpoint renames** break `url_for` and any test using endpoint names: mitigated by grepping/updating in the same MR and the manifest test.
- **Two reviewers for the sensitive blueprints** slows P2's tail: keep them last and small.
- **Roadmap vs hardening:** the owner rule is roadmap features first, hardening at v1. P0/P1 are justified now (user-visible load time, safety net). P2-P4 are structural hardening; recommend scheduling them between feature sprints, one MR at a time, never blocking a feature.
- **`ship.py` limits** (no data files, no pip, unit install unsupported) constrain layout: new code must live under deployed paths, and no new runtime dependency is assumed anywhere in this plan.
- **ES modules need a modern browser** (all current ones are fine; the display UI is used from phones and laptops on the LAN).
- **Doing nothing** keeps page weight and review risk growing with every feature (Calendar, RSS and Lunar editors each added ~50 KB of inline JS).

## 7. Owner decisions (2026-10-31, all approved)

1. P1 starts now; P2-P4 run as background work between features, one MR at a time.
2. Front end keeps the "no build step" rule (revisit only at P5).
3. This plan is recorded in GitLab #33 and `docs/FUTURE_DEVELOPMENT.md`.

## 8. Not covered / to verify before P2 starts

- Exact list of `nonlocal` state and which routes touch it (inventory in P2 MR 0).
- Whether `gunicorn.conf.py` is picked up in prod (`WorkingDirectory` of the unit; read-only check at the next ship).
- Browser-side measurement on the real Pi (needs a paired session; the hotfix measurement script provides the harness).
