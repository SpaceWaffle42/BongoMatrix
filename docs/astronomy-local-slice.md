# Priority 2.3: offline Moon and lunar-event slice (locally integrated)

## Delivered boundary

`astronomy.Astronomy` loads only the bundled checksum-pinned NASA/JPL DE440s.
`moon(aware_datetime, Location, stop_event=None)` returns local phase name,
phase angle, illuminated fraction, zero-or-more rise/set instants for the local
civil date, explicit all-day above/below horizon status, next major phase and
source diagnostics. `Location` preserves label, explicit IANA zone and provenance.
An absent individual rise or set is an empty tuple, never a manufactured time.

`modules.astronomy_renderer.prepare_pass(engine, now, location,
current_moon=True, options=None, stop_event=None)` recalculates each pass.
With lunar events disabled, disabled/cancelled Moon returns an empty tuple. Calculation errors propagate to
`astronomy_host.PreparationLane`, which publishes an explicit error status;
this slice never silently fabricates fallback.
`MoonOptions` controls installed title/detail fonts, 1–300 second static hold,
0.01–1 second per-pixel scroll delay and background/text/Moon RGB colours.
Defaults: 5x7 / 4x6, 10 seconds, .05 seconds, black/white/#D9D9D9.
Prepared cards carry immutable glyph points and per-line vertical clipping flags.
Long lines scroll once concurrently, restore to the start, then hold. The shared
`duration_seconds(cards)` accounts for that entire physical timeline, not merely
the hold. The next-phase line normally needs scrolling at 64x32.

`image_frames(cards, stop_event)` yields RGB Pillow images, seconds and metadata.
`preview_frames` PNG-encodes that exact stream. `run(matrix, cards, stop_event)`
uses `draw_image` then `swap`, checks cancellation before submissions and after
drawing, and uses interruptible monotonic-deadline waits. It returns successful
swap count, including swaps before cancellation. No framebuffer work for empty
passes. Slow hardware can exceed the calculated duration; frames are not dropped.

## Local lunar-event checkpoint

`Astronomy.lunar_events(now, location, horizon_days=90, stop_event=None)` now
finds real new/full phase instants with the existing checksum-verified DE440s.
Quarter phases remain current-Moon details, not invented event categories.
The horizon counts local civil dates including today, ending exclusively at
midnight after the configured number of dates (internal bound 1–366). It is
not a fixed multiple of 24 hours. A request extending beyond supported 2100
UTC is rejected rather than silently truncated.

For each phase's **local date**, `moon_visibility(day, location)` calculates
continuous windows where the Moon's apparent topocentric centre exceeds 10°
and the Sun's is below −18°. Only windows lasting at least ten minutes survive.
This is local-date Moon observability, NOT a claim that the instantaneous global
phase is itself visible or lasts all day. New Moon normally has no qualifying
window and is omitted, not granted a fictitious viewing opportunity. No eclipse,
meteor or planet dates are manufactured. No provider guidance is being used.

The solver uses five-minute initial sampling and Skyfield crossing refinement,
with cancellation checks around numerical operations. Terrain, atmosphere and
weather are not modelled. The independent USNO fixture verifies phase dates;
window endpoint tests verify internal geometry consistency only, not independent
altitude/darkness accuracy or grazing-contact completeness.

`AstronomyEvent` and `VisibilityWindow` are frozen internal records carrying
canonical identity, kind/title, phenomenon instant, exact `Location`, tuple of
UTC observing windows, and source identity. Empty identity/time/location
eligibility is rejected; these are trusted prepared-data contracts, not a
provider/API input schema. Optional catalogue-field omission and rejection
diagnostics still need future adapters/editor integration. Windows are sorted,
deduplicated and end-exclusive; they do not assign a duration to a phase.

`select_events` filters the selected location and local-date horizon, drops
exhausted windows, orders by instant then identity, and defaults to five cards
(internal bound 1–50). Each pass restarts at the nearest eligible records.
Exact canonical identities deduplicate independently of input order: earliest
instant wins, then lexical source/kind/title/windows break ties. Local phase IDs
use phase kind and the UTC phase date. Future provider adapters must map to the
same identity before using this path; fuzzy cross-provider matching and source
trust/visibility precedence are **not implemented**. The full `Location` record
must match; adapters must use the selected location, including its provenance.

`prepare_pass(..., lunar_events=True, horizon_days=90, event_limit=5,
event_options=EventOptions(...))` prepares current Moon first when enabled,
followed by local phase cards. Lunar events default off in the persisted settings.
All-disabled/empty/cancelled passes return `()`. The local host prepares these
cards for the registered Astronomy playlist module and preview paths; calculation
exceptions become explicit host error status, not fabricated cards.

Event cards show the phase title, nearest-minute local phenomenon date/time,
the next remaining **Partly visible** window with zone abbreviations, and a
seconds-free countdown **to that local observing window**. NOW means currently
inside that window, not that a mathematical phase instant lasts several hours.
The native editor explicitly explains this distinction. The format is
days/hours, hours/minutes, minutes (minimum 1m before start), or NOW; no seconds.
The Event Countdown module's helper deliberately remains unchanged because its
seconds and all-day/recurrence definitions are different.

`EventOptions` defaults to an independent eight-second hold, 5x7 title, 4x6
details, auto-largest-fitting installed countdown font, and #B388FF accent.
The countdown occupies 64×7 pixels below three text rows; oversized overrides
have clipping diagnostics. Bundled new/full Moon pixel icons have a generic
star fallback for unknown kinds; no new catalogue is implied by that fallback.
All static text and countdown glyphs are copied into immutable prepared tuples.
Playback accesses no fonts, ephemeris, source files or network.

One frame-spec timeline drives duration, preview pixels and physical pixels.
`duration_seconds(cards, now=...)` accounts for scrolling, holds and expired
windows without rendering or loading fonts. Preview defaults to preparation
time; pass `now` for a different deterministic preview start. Physical `run`
uses real UTC (injectable `utc_clock`) plus monotonic waits. It rechecks before
each frame, before drawing and before swapping. Holds split at countdown/window
boundaries without extending their configured duration. Expired cards submit
nothing and do not consume their former duration. Changed windows use already
prepared glyphs, never calculations in playback. No real hardware/browser
acceptance is claimed by these software-frame comparisons.

## Parent integration contract

- `requirements.txt` includes `requirements-astronomy.txt`, so the existing CI
  install, dependency audit and README runtime install resolve it automatically.
  NumPy is resolved jointly with OpenCV, not separately pinned or overridden.
  Focused verification uses a fresh isolated venv with `PYTHONPATH` unset;
  no packages from the previous `.astronomy-deps` target directory are imported.
- Ship `resources/astronomy/de440s.bsp` and its manifest as reviewed static data.
  Active file is 32,726,016 bytes. This is a real JPL download, not synthetic data.
  Checksum is anchored in Python, not accepted from a mutable manifest.
- `astronomy_host._prepare` constructs and closes an engine via context manager
  on a background lane, outside physical rendering. No auto-download or update
  path exists. Saved and draft lanes have immutable snapshots, generation fencing,
  cooperative cancellation and application cleanup.
- `astronomy_config.location_values` reuses the effective saved/draft weather coordinates or explicit
  coordinate/label overrides; both require an explicitly entered IANA timezone.
  No astronomy timezone inference, geocoding or place search/map is implemented.

### Preparation ownership and freshness

The two application-owned workers remain bounded: one saved lane and one draft
lane, each with one replaceable pending job. Draft claims retain consumer kind,
paired device and page identity. Same-key waiters share preparation but hold
separate claims; cancellation releases only that waiter. A sole owner can replace
its own job. Different live owners receive explicit contention (HTTP 429 with
Retry-After for timing/edit requests); device callbacks wait cancellably rather
than invalidating admitted work. At most 64 claims are admitted. Poll-only claims
expire after 30 seconds without renewal; blocking claims release on return,
cancellation or the existing 30-second wait deadline. No per-page workers exist.

Calculate serializes and validates the unsaved weather envelope along with the
astronomy settings and page identity, under the existing 16 KiB/revision checks.
Changing the effective weather dependency invalidates displayed/pending timing;
late results cannot repopulate it. Override coordinates and explicit timezone are
not inferred from a weather search. Location provenance no longer calls an
unsaved weather selection saved.

Ready and empty snapshots have an explicit freshness deadline: at most five
minutes from preparation, shortened to the next local midnight, next Moon phase,
or a prepared event instant/window boundary. The host's injectable UTC clock
checks freshness on requests; preparing jobs are never restarted just because a
poll crosses a boundary. Stale saved snapshots are not reused for draft requests,
and draft refresh does not mutate the saved lane. Continuous device previews
check the host between passes; empty passes wait cancellably for one second
between checks. Expensive calculation/font work stays on the lane worker.
Already-admitted edit pages remain immutable virtual timelines; this is not a
promise of live numerical recomputation during one card/page. Physical event
expiry checks remain in the shared renderer. Catalogue/scientific omissions and
production delivery blockers above remain unchanged.

### Remaining integration surface

- `config_store.py` persists bounded settings and registers a disabled-by-default
  Astronomy playlist row. `display_controller.py` prepares each saved pass through
  the host; `modules/astronomy_display.py` plays only prepared cards using the
  shared renderer. Empty passes yield without framebuffer work.
- `webapp.py` wires application-owned host lifecycle, status, prepared duration,
  edit preview and cancellably awaited cold device preview. Saved-ready snapshots
  can serve previews without consuming playback; other drafts use the draft lane.
- `app/templates/astronomy_editor.js`, included by `dashboard.html`, integrates
  native Save/reload, content flags, location, installed fonts, colours, separate
  holds, horizon and card limits. It displays preparation/clipping diagnostics,
  source limitations and unsupported capabilities. Empty edit preview terminates.
  The Moon icon is deliberately stylized, not a claim of local-sky orientation.
- Treat prepared-card structures as internal trusted output, not API input schemas.
  UI payloads must construct validated options/location and cannot inject glyphs.

## Scientific provenance and uncertainty

See `resources/astronomy/manifest.json` and `tests/fixtures/astronomy/provenance.json`.
Skyfield uses JPL vectors for apparent geocentric ecliptic elongation (major phases)
and true Sun/Moon/observer geometry for illuminated fraction. Eight current phase
names are conventional 45-degree bins. Rise/set is topocentric apparent altitude,
standard 34 arcminute refraction and mean 16 arcminute lunar semidiameter, WGS84
sea level, no terrain/pressure/temperature model. Searches use hourly initial
sampling with refinement. Very grazing high-latitude horizon contacts need more
independent validation; one-minute accuracy is a target, not a global guarantee.

Query instants from 2000 through 2100 UTC are supported. Local-day endpoints and
next-phase lookahead may cross the query-year boundary inside DE440s coverage.
Local-day intervals use civil midnights converted to UTC (23/25-hour DST days).
Minute rounding happens on UTC before converting back to the location zone.
Skyfield's built-in time tables avoid network access but future leap seconds and
UT1 cannot be known indefinitely; phase/rise predictions retain that uncertainty.
The source timestamp describes the ephemeris release, not fresh live observations.

Independent test data: USNO v4.0.1, 50 major phases in 2024 and Greenwich
2024-04-08 rise/set. The annual fixture test was additional initial-GREEN
acceptance coverage after the single-reference RED/GREEN, not a fabricated RED.
No event dates, meteor rates, visibility windows or provider labels were invented.
No optional provider is enabled.

## Kernel redistribution and delivery boundary

Original download: https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de440s.bsp.
On 2026-10-11 a full independent download from the official NAIF distribution
https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp
was verified to have the identical 32,726,016 bytes and SHA256
`c1c7feeab882263fc493a9d5a5b2ddd71b54826cdf65d8d17a76126b260a49f2`.
The bundled file is unmodified, including its embedded comments/attribution.

Authoritative terms, fetched successfully on 2026-10-11:
https://naif.jpl.nasa.gov/naif/rules.html, section "Kernels Redistribution":
"Redistribution of SPICE kernels distributed by NAIF is permitted as long as
they have not been modified." The page distinguishes kernels from the SPICE
Toolkit and says redistribution of other entities' kernels follows their rules.
This bundle's bytes were verified against NAIF itself; it is not a Toolkit
redistribution. The same rules page allows commercial use, states support
limitations and provides the Caltech/JPL/NASA as-is/no-warranty disclaimer.
Consult that page for the complete terms; this is not an invented SPDX license
or a blanket assertion that all NASA/JPL material is public domain.

Credit: NASA/JPL Solar System Dynamics for DE440s and NASA/JPL NAIF for kernel
distribution. NAIF encourages credit to the data producers and preservation of
embedded metadata: https://naif.jpl.nasa.gov/naif/credit.html.

Repository policy: no tracked Git LFS attributes or explicit large-data cap
were found; tracked fonts/built-in media establish a static-resource convention.
`.gitignore` excludes user media, not `resources/astronomy`. This foundation
tracks the real binary for checkout-based CI; no runtime HTTP downloader exists.

PRODUCTION DELIVERY BLOCKER: `deploy/ship.py` excludes all `resources/`, and
refuses changed `requirements.txt` because it cannot install dependencies.
`deploy/ship_remote.py` also protects `resources/`. A repository clone plus
`pip install -r requirements.txt` can test this foundation, but the current
ship path cannot deliver its kernel or new dependencies. Parent must separately
approve and implement a static-data/dependency installation and rollback
contract before enabling or shipping Astronomy. No deploy scripts are changed
here; this is a draft CI foundation, not a deployable Priority 2.3 release.

## Explicitly not complete

The internal canonical record, local lunar-date visibility, horizon/identity
deduplication, eligibility rechecks and local phase event cards/countdowns now
exist. Still absent: eclipse catalogue, open meteor catalogue, planet calculations,
meteor/planet cards, supermoon/blue-moon labels, cross-provider adapters, provider
integration/cache, location search/map,
release updater or active+rollback installation transaction. Only an active-file
64 MiB ceiling and checksum rejection exist; the full 128 MB rollback policy is
not implemented. Engine construction/file verification is not cancellable;
Moon numerical searches and playback are cooperatively cancellable between
bounded operations, not forcibly interruptible inside NumPy work.

Local Chromium integration coverage exercised native Save/reload, prepared
duration, 64×32 edit preview, empty-preview termination and widths 320/768/1280.
The final focused log `.astronomy-deps/integration-final-focused.log` records
184 tests in 60.002 seconds, OK. This is not production or physical-device
acceptance; neither was performed.
GitLab status belongs to the exact draft MR pipeline, not to local test results.
Runtime emits a Skyfield 1.54 NumPy 2.5 dtype deprecation warning in this environment;
all focused assertions pass despite it. Full-suite verification belongs to GitLab.
