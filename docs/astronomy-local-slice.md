# Priority 2.3: offline Moon slice (not registered)

## Delivered boundary

`astronomy.Astronomy` loads only the bundled checksum-pinned NASA/JPL DE440s.
`moon(aware_datetime, Location, stop_event=None)` returns local phase name,
phase angle, illuminated fraction, zero-or-more rise/set instants for the local
civil date, explicit all-day above/below horizon status, next major phase and
source diagnostics. `Location` preserves label, explicit IANA zone and provenance.
An absent individual rise or set is an empty tuple, never a manufactured time.

`modules.astronomy_renderer.prepare_pass(engine, now, location,
current_moon=True, options=None, stop_event=None)` recalculates each pass.
Disabled/cancelled Moon returns an empty tuple. Calculation errors propagate to
the future host for diagnostics; this slice never silently fabricates fallback.
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

## Parent integration contract

- `requirements.txt` includes `requirements-astronomy.txt`, so the existing CI
  install, dependency audit and README runtime install resolve it automatically.
  NumPy is resolved jointly with OpenCV, not separately pinned or overridden.
  Focused verification uses a fresh isolated venv with `PYTHONPATH` unset;
  no packages from the previous `.astronomy-deps` target directory are imported.
- Ship `resources/astronomy/de440s.bsp` and its manifest as reviewed static data.
  Active file is 32,726,016 bytes. This is a real JPL download, not synthetic data.
  Checksum is anchored in Python, not accepted from a mutable manifest.
- Construct/reuse one engine outside physical rendering; close via context manager.
  Do not mutate its ephemeris while in use. No auto-download or update path exists.
- Resolve weather-default or map-search location and explicit IANA timezone in
  the future host; this core does not geocode or guess a timezone from longitude.
- Prepare separately for each playlist pass / preview. Surface source diagnostics,
  clipping flags and no-crossing explanations in the editor. The Moon icon is
  deliberately stylized; it does not claim local-sky orientation.
- Register playlist/controller/config/preview/editor only in the parent's
  integration change. Host must prevent all-empty playlist cycles hot-looping.
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

No canonical lunar/meteor/planet event model, observability-window solver, event
horizon/deduplication/eligibility recheck, eclipse catalogue, open meteor catalogue,
planet calculations, event cards/countdowns, supermoon/blue-moon labels, provider
integration/cache, UI, host registration, location search, persisted settings,
release updater or active+rollback installation transaction. Only an active-file
64 MiB ceiling and checksum rejection exist; the full 128 MB rollback policy is
not implemented. Engine construction/file verification is not cancellable;
Moon numerical searches and playback are cooperatively cancellable between
bounded operations, not forcibly interruptible inside NumPy work.

No browser end-to-end, production or physical-device acceptance performed.
GitLab status belongs to the exact draft MR pipeline, not to local test results.
Runtime emits a Skyfield 1.54 NumPy 2.5 dtype deprecation warning in this environment;
all focused assertions pass despite it. Full-suite verification belongs to GitLab.
