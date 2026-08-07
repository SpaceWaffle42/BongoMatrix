# Future development notes

Last updated: 2026-08-07

This file records requested Matrix features that are intentionally deferred. They are product requirements and design notes, not descriptions of current behavior. Do not treat an item as implemented until its code, tests, review, merge, deployment, and physical-device verification are complete.

## Scrolling-text timing in seconds

Every scrolling-text module should display its calculated runtime in seconds, consistent with the duration shown for Lava Clock and Splash.

- Calculate runtime from the configured tick/frame speed and number of scrolls.
- Keep scroll count as an independent user choice.
- If the user edits the displayed seconds, change tick speed to achieve that runtime.
- Editing seconds must never change the number of scrolls.
- Do not add another manual duration source that can disagree with physical playback.
- Browser values and physical runtime must use the same calculation and rounding rules.
- Display every calculated duration with one decimal place in both module editors and playlist rows.
- If an edited duration requires an unsupported tick speed, clamp to the nearest achievable duration and explain the adjustment beside the control.
- Provider-backed scrolling modules must fetch content before opening the editor so their duration is calculated and displayed like any other module.
- Bound the pre-editor fetch. If it fails, use the last valid cached content and mark it stale; when neither fresh nor cached content exists, open the editor with timing explicitly unavailable rather than displaying zero or a fabricated estimate.

## Sticky Matrix preview

Keep the Matrix preview visible while the user scrolls through module controls, similar to the sticky navigation bar.

- On desktop, use a full-width sticky strip directly beneath the navigation bar.
- On mobile, keep a compact preview visible at all times rather than collapsing or disabling sticky behavior.
- The user should be able to adjust positions, colours, tickers, icons, and other settings without losing sight of the preview.
- The sticky behavior must not hide editor controls or prevent keyboard and touch interaction.

## Live and Edit Preview modes

Add a mode control below the preview with two mutually exclusive states:

- **Live** shows the current physical Matrix framebuffer.
- **Edit Preview** renders the selected module from its current unsaved editor values.

A two-option segmented control is preferred over a checkbox because the modes are mutually exclusive.

Edit Preview should:

- update as module controls change;
- use a short debounce for sliders, position controls, and rapid input;
- accurately render position, colour, ticker, icon, timing, and other supported changes;
- avoid saving configuration automatically; and
- clearly distinguish draft output from saved live output.

When the user tries to switch from Edit Preview to Live with unsaved changes, warn them and offer exactly **Stay Editing** or **Discard Draft and switch to Live**. Do not silently preserve, save, or discard the draft.

## Optional preview on the physical device

Add a separate **Preview edits on device** checkbox beneath the browser preview.

- Keep it off by default.
- When enabled, send the current unsaved draft to the physical Matrix temporarily.
- Clearly indicate that the device is showing draft content.
- Provide an obvious **Stop device preview** action.
- Do not mutate or persist the saved configuration.
- Return to normal playback when preview stops, expires, disconnects, fails, or the user leaves the page.
- Preserve the single-owner hardware-lock model; preview must run through the existing display controller rather than starting another Matrix process.
- Make the preview timeout configurable, with a 60-second default that restarts after each accepted draft update.
- While device preview is enabled, send each later edit automatically after debouncing rather than requiring a separate Apply action.
- Use adaptive debounce timing: 150 ms for simple controls and 500 ms for media changes or other expensive renders.
- Cancel superseded draft work so an older slow render can never replace a newer edit on the device.

## GIF and video support in basic custom modules

Basic custom modules should allow the user to select a GIF or video as the module content.

- A selected GIF or video creates a media-only custom module.
- Keep controls appropriate to the selected media type.
- Do not add text overlays, layered elements, or compositing to the basic editor.
- Do not imply that the basic module can combine text and media.
- Offer **Contain**, **Cover**, **Stretch**, and manual crop positioning.
- Reuse the Advanced Canvas media drag/resize positioning interaction and its executed physical-rendering semantics rather than creating a visually similar browser-only crop tool.
- Reset basic media modules to **Contain** at **100%** playback speed.

## GIF and MP4 playback speed across modules

Every module that displays a GIF or MP4 should offer a playback-speed control. Use the same control model and terminology across built-in modules, basic custom modules, announcements, and Advanced Canvas wherever that media type is supported.

- The default `100%` or `1×` setting should preserve the media's original playback timing.
- Lower values should slow playback and higher values should speed it up.
- GIF speed should scale the embedded frame delays without rewriting the source file.
- MP4 speed should scale effective video playback timing without rewriting or transcoding the source file.
- Use safe, bounded values and reject zero, negative, Boolean, malformed, or unsupported speeds.
- Browser previews and physical Matrix output must use the same effective playback rate.
- Playlist timing and displayed runtime should reflect the adjusted physical playback time rather than the source duration alone.
- Slow playback must remain interruptible by module changes, preview changes, schedule changes, service stop, and shutdown.
- Existing saved configurations should migrate to normal speed without altering unrelated settings.
- Present speed as a shared slider plus an exact percentage input in every supporting module.
- Allow `10.0%` through `400.0%` inclusive, with 0.1-percentage-point precision; normalize equivalent numeric representations before saving.
- Changing media speed must not change loop count. Recalculate playlist runtime from the unchanged loop count and adjusted playback timing.

## Event Countdown module

Add an Event Countdown playlist module for birthdays, anniversaries, launches, holidays, openings, and other deliberately selected events.

### Confirmed behavior

- Keep an event list and automatically display the nearest upcoming eligible event.
- Support one-off date/time events and annually recurring events in the first version.
- Use an automatic compact countdown format based on the time remaining rather than always forcing every time unit onto the 64×32 screen.
- At zero, show **NOW** for a configurable period, then advance to the next eligible event.
- When no event is eligible, yield immediately to the next playlist module instead of showing a blank screen or hot-looping.
- Convert every imported or local event to the configured Matrix timezone before countdown ordering and display, while retaining source identity needed for deterministic resynchronization.
- For annual February 29 events in non-leap years, use February 28.
- When events share a start time, retain Google Calendar order first, followed by local Matrix events in their configured order.
- Allow long titles, but make truncation or scrolling behavior explicit in the editor preview rather than shrinking text unpredictably.
- Let each normal timed event configure its **NOW** duration, defaulting to 15 minutes.
- For all-day events, count down to local midnight and show **NOW** for the entire calendar day.
- For multi-day events, count down to the start and show **NOW** for the full event duration.

### Confirmed layouts

- Large countdown with a short event title.
- Title, countdown, and optional icon.
- Compact progress bar showing time remaining.

Media-background composition is not part of the confirmed first-version layouts. Advanced Canvas remains the place for layered text-over-media countdown designs.

### Google Calendar synchronization

Support optional read-only Google Calendar synchronization alongside local Matrix-only events.

- Let the user select one or more Google calendars.
- For each selected calendar, allow either all events or a user-defined event filter.
- Match calendar filters as case-insensitive whole words across title, description, and location.
- Imported events must remain read-only in Matrix; the dashboard must not edit or delete Google Calendar events.
- Local Matrix-only events remain editable and must work when Google Calendar is not connected.
- Keep OAuth credentials and refresh tokens outside `config.json`, never return them through the API, and support clean account disconnection and token revocation.
- Handle temporary Google API failures with bounded retries and a visible stale/offline state rather than blocking the playlist.
- Cache only the minimum event data needed for reliable display and define a safe expiry policy.
- Merge imported and local candidates deterministically before selecting the nearest upcoming event.
- Exclude cancelled and declined events. Render private events as **Busy** without exposing their private title, description, or location.
- Make synchronization configurable with a 15-minute default and allow cached Google events for at most 24 hours while offline.
- Do not impose a user-facing event-count limit; enforce bounded provider pagination, payload, memory, and storage safeguards internally.
- Offer bundled icons plus images selected from the existing media library.

### Decisions still required before implementation

- Exact title truncation/scroll thresholds, colours, font sizes, progress-bar geometry, and icon fallback.
- Bounded provider pagination, payload, memory, and storage limits.
- Whether Google recurrence exceptions retain provider order after conversion to Matrix local time.

## RSS and Headline Ticker module

Add an RSS/Atom playlist module for BBC news and other user-selected publishers. It should present deliberately selected headlines rather than acting as a device-monitoring dashboard.

### Confirmed sources and feed controls

- Provide built-in BBC presets for **Top Stories**, **World**, **UK**, and regional news.
- Also allow arbitrary user-supplied RSS or Atom feed URLs.
- Refresh feeds every 30 minutes.
- Apply one shared include-word list and one shared exclude-word list across the module.
- Merge eligible stories in round-robin feed order so a high-volume source cannot dominate.
- Make stories per playlist pass configurable, with a default of five.
- Prefer stories not previously shown; when all eligible stories have been shown, fall back to the newest stories.
- Deduplicate stories across feeds using stable IDs or canonical links where available, with a conservative title/date fallback.

BBC preset URLs and regional availability must be verified against BBC's official current feed catalogue during implementation rather than copied from an unverified or stale list.

### Confirmed 64×32 presentation

- Keep the headline fixed while a short overview scrolls upward beneath it.
- When a usable feed image is available, place a 16×16 thumbnail on the left, keep the headline beside it, and use the full width below for the upward-scrolling overview.
- When no usable image exists, reflow into the text-only layout without leaving an empty image box.
- Strip markup from feed descriptions and render bounded plain text; do not execute embedded HTML, scripts, tracking pixels, or feed styling.
- Offer shared **Slow**, **Normal**, and **Fast** overview presets for the whole module. Persist their canonical numeric frame delays so preview and physical playback use the same timing path.
- Calculate and display physical runtime after content is fetched because headline, overview, image availability, and story count affect the duration.
- Each story should complete one bounded upward-summary pass before the next selected story begins.
- Scroll an overlong headline horizontally while the summary scrolls upward. Provide a toggle between one headline pass followed by a frozen headline and continuous headline looping until the summary pass finishes.
- The exact frozen position after one headline pass must be established with a 64×32 preview/physical fixture before implementation is considered closed.
- Accept every thumbnail media format already accepted by the media library.
- Animated thumbnails use source timing by default and expose the shared `10.0%`–`400.0%` speed control only in Advanced settings.

### Refresh, cache, and failure behavior

- Fetch and parse feeds outside physical rendering so network latency cannot block the Matrix loop.
- Use bounded connection/read timeouts, response-size limits, item-count limits, safe XML parsing, and cancellation-aware image preparation.
- Cache the last successful normalized story set and the minimum required thumbnails.
- If refresh fails, continue with the last successful cache and visibly mark its state as stale in the editor.
- Expire stale stories after 24 hours; after expiry they are no longer valid fallback content.
- If no valid cached stories exist, yield immediately to the next playlist module instead of showing a blank screen or hot-looping.
- Isolate feed failures: one invalid or unavailable source must not prevent healthy sources from refreshing or displaying.
- Preserve the unseen/story-rotation cursor across normal playlist passes, but reset it safely when feeds or filters materially change.
- Reset seen-story state whenever the service restarts.
- Match shared include/exclude filters as case-insensitive whole words across headline, summary, and feed categories/tags. Any include match qualifies; any exclude match rejects.
- Suggest the BBC regional preset matching the configured weather location while keeping every verified region searchable and available.
- A custom-feed **Test feed** action must report feed title, item count, newest-item date, image availability, and warnings without saving the feed.

### Custom-feed security boundaries

- Treat feed URLs, redirects, XML, story text, links, and images as untrusted input.
- Block loopback, link-local, private-network, metadata-service, and other unsafe destinations for custom URLs; revalidate every redirect and resolved destination.
- Defend against DNS rebinding and enforce bounded redirect counts.
- Never expose fetched feed content as trusted HTML in the dashboard.
- Reject unsupported URL schemes and oversized or invalid media without affecting the rest of the feed.

### Decisions still required before implementation

- Exact font sizes, clipping rules, upward-scroll geometry, and the one-pass headline freeze position.
- Disk cache limits, thumbnail preparation budget, and fallback artwork.
- How verified BBC preset URLs are updated when the provider changes them.
- Accessibility labels, empty states, reset defaults, and detailed warning language in the editor.

## Moon Phases and Astronomical Events module

Add one configurable Astronomy playlist module with independent checkboxes for current Moon information, lunar events, meteor showers, and planetary events.

### Confirmed content

- **Current Moon:** stylized phase icon, phase name, illumination percentage, local moonrise/moonset, and the next major phase.
- **Lunar events:** new and full moons, eclipses, supermoons, and blue moons.
- **Meteor showers:** peak date/window, expected activity level, and radiant direction.
- **Planetary events:** conjunctions, oppositions, and selected visible-planet highlights, including object names, local visibility window, and compass direction.
- Show only events observable from the selected location.
- Show the current Moon first, then all enabled upcoming events in chronological order.
- Make the future-event horizon configurable, with a 90-day default.
- If no enabled upcoming event is observable, show the current Moon when enabled and otherwise yield immediately to the next playlist module.

### Location and time

- Reuse the configured weather location by default.
- Allow an optional astronomy-specific location override using place search or a map pin rather than requiring coordinate entry.
- Resolve and store the location's timezone explicitly; all dates, rise/set times, visibility windows, and countdowns must use that timezone with daylight-saving transitions handled correctly.
- Preserve the place label and location provenance so the editor can explain which location is being used.

### Confirmed 64×32 layouts

- Moon disc card with phase name, illumination, and local rise/set times.
- Next-event card with event type, local date/time, and compact countdown.
- Meteor-shower card with peak window, activity level, and radiant direction.
- Planetary-event card with object names, event type, visibility window, and compass direction.
- Use a simple stylized Moon phase icon without claiming local-sky or north-up orientation.
- Use separate configurable hold seconds for the Moon screen and event cards.
- Default the Moon hold to 10 seconds and event-card hold to 8 seconds.
- If text must scroll, use the shared physical-time calculation and keep browser preview timing identical to physical rendering.
- Display event times to the nearest minute and directions using eight compass points.
- Make event cards per pass configurable, with a default of five. Every pass restarts with the nearest eligible events rather than continuing a rotation cursor.

### Calculation and provider model

Support local astronomical calculations plus an optional provider fallback.

- Recalculate local Moon values for every playlist pass so the displayed phase and rise/set data are current.
- Refresh provider-backed events every six hours outside the physical rendering loop.
- Use versioned, checksum-verified ephemeris or astronomical data files for local calculations.
- Record data-source identity and freshness in the editor without cluttering the physical display.
- Define one canonical normalized event model so local and provider events use the same visibility, ordering, rendering, and deduplication paths.
- Deduplicate overlapping local/provider events deterministically.
- Provider credentials must remain outside `config.json` and must never be returned through dashboard APIs.
- Use provider labels for supermoons, blue moons, meteor activity, and curated visible-planet highlights rather than inventing competing local definitions.
- Use provider visibility guidance when available. Otherwise require an event to exceed 10° altitude during local astronomical darkness.
- Include every locally visible partial or total eclipse and label its type.

### Failure and edge-case behavior

- If the optional provider fails, continue local calculations and use the last valid provider-event cache, visibly marked stale in the editor.
- Expire provider-event fallback data after 24 hours.
- Provider errors must never block current-Moon rendering or the playlist loop.
- Use bounded network timeouts, retries, response sizes, cache sizes, and event counts.
- Represent polar and unusual rise/set outcomes accurately, including Moon always above, always below, or no rise/set during the local date; never invent a time.
- Recheck event eligibility immediately before displaying each event because the visibility window may have passed since the list was prepared.
- Cancellation must stop calculations, provider work, card holds, scrolling, and framebuffer submissions promptly.
- All disabled content sections should be rejected by validation or yield safely without hot-looping.

### Decisions still required before implementation

- Local astronomy library, ephemeris source, update process, calculation accuracy target, and supported date range.
- Optional provider and its authentication, licensing, attribution, rate limits, redistribution terms, and exact label semantics.
- Treatment of events only partly visible within the local event window.
- Fonts, colours, icons, compact countdown formatting, and cards with missing provider fields.
- Ephemeris download/storage limits, offline longevity, and editor diagnostics.

## Advanced Canvas owns composition

Use Advanced Canvas for text over GIFs or videos, multiple layers, overlays, and complex positioning.

- Advanced Canvas controls and examples must be runnable on the physical Matrix.
- Browser previews must match physical rendering.
- Do not expose browser-only controls or examples that cannot run on the device.

## Shared acceptance criteria

When these items are implemented:

- preserve existing configuration and unrelated user state;
- provide friendly grouped controls and a reset-to-default action where appropriate;
- keep browser and physical timing/rendering consistent;
- add focused regression tests plus Pi/no-Node portability coverage;
- verify real browser behavior at desktop and narrow mobile widths;
- review, merge, deploy, restart affected services when required, and verify the physical Matrix; and
- update this file to mark completed items or move finished behavior into the README.
