# Future development notes

Last updated: 2026-10-11

**Status:** Priority 0 and 1.1-1.9 are complete and live (0.3.69-preview, 2026-10-11). **Owner reprioritization, 2026-10-11: start 2.1 Event Countdown; leave 1.10 paused.** Priority 1.10 remains reserved for Opus; its existing work is preserved and is not part of 2.1. Event Countdown is in development, not complete or deployed. Open findings and deferred v1 hardening live in GitLab issue #33.

This file records requested Matrix features that are intentionally deferred. They are product requirements and design notes, not descriptions of current behavior. Do not treat an item as implemented until its code, tests, review, merge, deployment, and physical-device verification are complete.

## Delivery and priority policy

Work through the numbered backlog in priority order. **Priority 0 fixes and correctness work take precedence over Priority 1 improvements, and both take precedence over Priority 2 new modules.** A later item may move ahead only through an explicit reprioritization decision made before implementation of the next item begins.

Each numbered item, such as **0.1** or **1.3**, is an independent implementation and merge boundary:

- Use one focused branch and merge request for one priority item; do not bundle unrelated priorities into one release.
- Work on only one numbered priority item at a time. Do not overlap implementation, review, testing, deployment, or physical verification between numbered items.
- Complete the item's implementation, migration, automated tests, mobile/tablet/desktop checks where relevant, independent review, and exact-artifact verification before merging.
- Merge the item as soon as its completion gate passes rather than leaving completed work stacked on an unmerged branch.
- After merge, deploy that item independently, retain a specific rollback artifact, verify the live service and physical Matrix behavior, and only then begin the next priority item.
- If the active item discovers a prerequisite from later in the queue, stop at a clean baseline and explicitly reprioritize that prerequisite as the next independent item; do not develop the two items concurrently.
- If an item is too large for one safely reviewable merge, divide it into numbered sub-items that each preserve compatibility and can be deployed and rolled back independently.
- Treat newly discovered regressions, data-loss risks, security issues, broken physical rendering, and cross-device consistency failures as new Priority 0 work.

This order controls implementation sequence. Reprioritization changes only the queued order before the next item starts. Once a numbered item starts, complete, merge, deploy, and verify it before another item begins, unless it is explicitly abandoned and the clean baseline is restored. Fixes must not be silently displaced by new feature work.

## Cross-cutting requirements

### Shared acceptance criteria

When these items are implemented:

- preserve existing configuration and unrelated user state;
- provide friendly grouped controls and a reset-to-default action where appropriate;
- keep browser and physical timing/rendering consistent;
- add focused regression tests plus Pi/no-Node portability coverage;
- verify real browser behavior at desktop and narrow mobile widths;
- review, merge, deploy, restart affected services when required, and verify the physical Matrix; and
- update this file to mark completed items or move finished behavior into the README.

### Advanced Canvas composition and physical parity

Use Advanced Canvas for text over GIFs or videos, multiple layers, overlays, and complex positioning.

- Advanced Canvas controls and examples must be runnable on the physical Matrix.
- Browser previews must match physical rendering.
- Do not expose browser-only controls or examples that cannot run on the device.

### Physical playlist navigation versus draft preview

Treat **Show on physical Matrix** and **Cycle physical module** as playlist navigation, not temporary preview actions.

- **Show on physical Matrix** must let the user explicitly select a currently playable saved playlist item, interrupt the current item, and move the active playlist cursor directly to that selection, skipping intervening items.
- Run the selected item with its saved configuration and normal playlist duration, then continue with the following enabled item in normal order, wrapping at the end.
- **Cycle physical module** must use the same playlist-skip path, targeting the next enabled item rather than creating a separate one-off preview.
- Do not autosave or send unsaved editor values, apply a preview-only duration, enqueue a temporary preview, or return to the old cursor after the selected item finishes.
- Reject a stale, missing, disabled, or otherwise non-playable selection without running unrelated content.
- Unsaved edits may reach the physical Matrix only through the separate, explicit **Preview edits on device** opt-in described in Priority 1.3.

### Provider cost and access policy

Every external provider used by these future modules must remain free to use for the required Matrix behavior.

- Do not require a paid subscription, pay-as-you-go billing, prepaid credit, or a temporary trial that later requires payment.
- A genuinely free account, OAuth connection, or API key is acceptable when the feature inherently accesses personal data or no practical anonymous source exists.
- Prefer providers and local data sources that require no account, login, API key, or credential.
- Rank otherwise suitable sources by access friction: local/open data first, anonymous public API or feed second, free authenticated API third.
- Do not collect payment details merely to activate a nominally free tier.
- Record applicable free-tier quotas and design polling, caching, and refresh behavior to remain safely within them.
- Revalidate pricing, account requirements, licensing, redistribution terms, and quotas immediately before implementation and in provider-maintenance documentation.
- If a provider removes the required free access, continue any valid local calculation or bounded stale cache where safe, otherwise yield cleanly and mark the integration unavailable. Do not silently switch to a paid provider.
- Account-bound Spotify and optional Google Calendar features may require user authorization, but their required API capabilities must still be available without provider API charges. If the confirmed behavior requires a paid account or paid API tier at implementation time, leave that provider path deferred until a compliant free option exists.

## Priority 0 — Fixes, correctness, and consistency

Complete these before usability enhancements or new modules unless an explicit dependency requires otherwise.

### ~~0.1 Synchronize saved changes across paired devices~~ — Completed

**Completed 2026-08-10:** implemented, tested, independently reviewed, merged, deployed, and verified with paired clients and the physical Matrix.

~~Treat the Matrix server's saved configuration as the single source of truth and synchronize each committed display change to every currently paired browser or device. This must prevent one paired client from showing an outdated playlist, missing a custom module referenced by the playlist, or exposing controls that no longer match the physical display.~~

- ~~Publish one revisioned, atomic configuration update after every successful save, covering the playlist, custom-module definitions, module settings, media references, and other dependent state together.~~
- ~~Ensure dependent records such as custom modules are available in the same synchronized revision as playlist entries that reference them; never expose a partially updated playlist.~~
- ~~Update open paired clients promptly without requiring a manual page refresh, including module pickers, playlist rows, editors, Live preview state, and any cached client-side catalogs derived from configuration.~~
- ~~On reconnect, page focus, or recovery from a missed update, compare revisions and fetch the latest complete saved state before permitting another save.~~
- ~~Do not silently overwrite an unsaved draft on another paired device. Clearly report that the saved display changed elsewhere and offer that user a safe choice to reload the latest saved state or remain with their unsaved draft for review.~~
- ~~Reject or reconcile stale saves using the revision they were based on so an older client cannot unknowingly replace newer changes.~~
- ~~Synchronize saved changes only; do not broadcast another user's temporary Edit Preview or opt-in on-device draft as if it were committed configuration.~~
- ~~Stop sending updates immediately when a device is unpaired or its session is no longer authorized.~~
- ~~Add multi-client tests using at least two independently paired sessions, including custom-module creation, playlist insertion, editing, deletion, stale drafts, reconnect recovery, and update-order interruption.~~

### ~~0.2 Fix the mobile Advanced Canvas layer timeline~~ — Completed

**Completed 2026-08-11:** implemented, tested in real Chromium, independently reviewed, merged, deployed, and accepted after visual inspection on Billy PC.

~~Revise the Advanced Canvas layer timeline for mobile and narrow-screen users. The current responsive layout can compress the layer timeline until tracks, controls, and timing positions appear squashed and are difficult to understand or operate.~~

- ~~Preserve readable layer rows and sufficiently large touch targets instead of shrinking the complete timeline to fit the viewport.~~
- ~~Prefer a deliberate mobile layout and bounded horizontal timeline scrolling over compressing timing tracks and handles into unusable widths.~~
- ~~Keep the active layer identity and essential timeline controls visible while the timing area scrolls.~~
- ~~Ensure selecting layers, seeking, dragging timeline items, and editing keyframes remain practical with touch input.~~
- ~~Preserve the existing desktop experience and verify the revised timeline in real browsers at representative narrow mobile widths.~~

### ~~0.3 Standardize scrolling-text timing in seconds~~ — Completed

**Completed 2026-08-12:** implemented, tested in real Chromium and on the production Pi, independently reviewed, merged, deployed, and verified against the live physical framebuffer.

~~Every scrolling-text module should display its calculated runtime in seconds, consistent with the duration shown for Lava Clock and Splash.~~

- ~~Calculate runtime from the configured tick/frame speed and number of scrolls.~~
- ~~Keep scroll count as an independent user choice.~~
- ~~If the user edits the displayed seconds, change tick speed to achieve that runtime.~~
- ~~Editing seconds must never change the number of scrolls.~~
- ~~Do not add another manual duration source that can disagree with physical playback.~~
- ~~Browser values and physical runtime must use the same calculation and rounding rules.~~
- ~~Display every calculated duration with one decimal place in both module editors and playlist rows.~~
- ~~If an edited duration requires an unsupported tick speed, clamp to the nearest achievable duration and explain the adjustment beside the control.~~
- ~~Provider-backed scrolling modules must fetch content before opening the editor so their duration is calculated and displayed like any other module.~~
- ~~Bound the pre-editor fetch. If it fails, use the last valid cached content and mark it stale; when neither fresh nor cached content exists, open the editor with timing explicitly unavailable rather than displaying zero or a fabricated estimate.~~

## Priority 1 — Improvements to existing workflows

Begin these after Priority 0 is clear. They improve existing editing, preview, media, and composition workflows without introducing an unrelated provider-backed module.

### ~~1.1a Add Lava Clock pattern selection controls~~ — Completed

**Completed 2026-08-21:** added grouped Select all and Deselect all actions that update only the unsaved draft, preserve unrelated edits, remain individually adjustable, and persist through normal Save. A saved zero-palette selection is explicit: physical playback and Edit Preview render a neutral Lava background without silently enabling a palette, while configured time and date remain visible. Mobile, keyboard, save/reload, validator, runtime, and preview behavior are covered by automated tests.

- ~~**Select all** selects every available Lava Clock pattern.~~
- ~~**Deselect all** clears every selected Lava Clock pattern.~~
- ~~Apply either action immediately to the current unsaved editor state without changing other Lava Clock settings.~~
- ~~Keep the individual pattern controls synchronized so the result is visible and can still be adjusted one pattern at a time.~~
- ~~Persist the resulting pattern selection only through the normal Save action; the bulk controls must not save automatically.~~
- ~~Keep the separate Edit Preview inspection radio buttons unselected until the user deliberately chooses one; bulk selection must never choose an inspection palette.~~
- ~~Keep both actions usable and clearly labelled on mobile, tablet, and desktop, including keyboard and assistive-technology access.~~

### ~~1.1 Keep the Matrix preview sticky~~ — Completed

**Completed 2026-08-13:** implemented as the original full preview at the top with the same live preview popping into a compact fixed strip only after scrolling beyond it. Responsive flow, focus, reduced motion, one 64×32 canvas, and one framebuffer polling lifecycle were tested in real Chromium. The exact artifact was independently approved, accepted on the physical Matrix, merged through MR !50, deployed, and verified against the live device.

### ~~1.2 Add Live and Edit Preview modes~~ — Completed

**Completed 2026-08-22:** added mutually exclusive Live and Edit Preview modes. Live continues to show the physical framebuffer, while Edit Preview renders the selected module from current unsaved controls with bounded debouncing, explicit draft status, no automatic save, deterministic custom-module source selection, deliberate single-palette Lava inspection, browser/physical parity coverage, and a two-choice warning before discarding a draft. The exact reviewed artifact passed protected CI and physical acceptance, merged through MR !69, and was deployed with preservation and rollback evidence.

~~Add a mode control below the preview with two mutually exclusive states:~~

- ~~**Live** shows the current physical Matrix framebuffer.~~
- ~~**Edit Preview** renders the selected module from its current unsaved editor values.~~

~~A two-option segmented control is preferred over a checkbox because the modes are mutually exclusive.~~

~~Edit Preview should:~~

- ~~be available for every module, with opening a module editor making that module the active Edit Preview target without switching preview mode automatically;~~
- ~~update as module controls change;~~
- ~~use a short debounce for sliders, position controls, and rapid input;~~
- ~~accurately render position, colour, ticker, icon, timing, and other supported changes;~~
- ~~in the Lava Clock palette editor, render the currently selected built-in or custom palette immediately as a complete animated 64×32 Lava Clock preview, updating as each colour control changes without requiring Save;~~
- ~~leave every Lava palette inspection radio unselected initially and use normal configured Lava rendering until the user deliberately selects a palette;~~
- ~~after a palette is selected, preview only that palette rather than rotating through every enabled palette, so the user can inspect that exact colour combination deliberately;~~
- ~~use the same palette order, interpolation, animation frame generation, clipping, and colour conversion as physical Lava Clock rendering, with browser/physical parity fixtures;~~
- ~~keep the Lava Clock colour preview in the browser unless the user separately enables the opt-in physical-device draft preview;~~
- ~~avoid saving configuration automatically; and~~
- ~~clearly distinguish draft output from saved live output.~~

~~When the user tries to switch from Edit Preview to Live with unsaved changes, warn them and offer exactly **Stay Editing** or **Discard Draft and switch to Live**. Do not silently preserve, save, or discard the draft.~~

### ~~1.3 Add optional preview on the physical device~~ — Completed

**Completed 2026-08-29:** MR !95 reviewed head `3115ee09ed0af18e580dde4f10d3c0e04bb6fa4e` passed independent exact-artifact re-review `deleg_10856fa7` and exact-SHA pipeline 293, then merged as protected-main commit `f3443b9a891a282c1d706cc74f7541a31211b1f6`. Protected-main pipeline 294 passed, including source-bound physical-device acceptance and recovery. The accepted production process-loss journey recorded a final heartbeat at 11:23:36 BST and a physical transition away from the draft at 11:23:52 BST after the 15-second connectivity lease; an already-paired client reported saved non-draft playback before any later pairing request. GitLab issue #4 is closed. This completion does not claim that the separately deferred v1.0 findings were remediated.

~~Add a separate **Preview edits on device** checkbox beneath the browser preview.~~

- ~~Keep it off by default.~~
- ~~When enabled, send the current unsaved draft to the physical Matrix temporarily.~~
- ~~Clearly indicate that the device is showing draft content.~~
- ~~Use unchecking the Edit-only **Preview edits on device** checkbox as the sole explicit stop action; do not add a separate Stop button.~~
- ~~Do not mutate or persist the saved configuration.~~
- ~~Return to normal playback after explicit uncheck, render failure, or a clean page-exit cancellation.~~
- ~~Restore saved playback after abrupt browser/process or connectivity loss, release renderer/display-lock ownership, close the lease, and admit a later owner.~~
- ~~Preserve the single-owner hardware-lock model; preview must run through the existing display controller rather than starting another Matrix process.~~
- ~~Keep accepted device-preview content active without a content timeout while the checkbox remains enabled and the client remains connected; do not silently expire it after 60 seconds.~~
- ~~While device preview is enabled, send each later edit automatically after debouncing rather than requiring a separate Apply action.~~
- ~~Use adaptive debounce timing: 150 ms for simple controls and 500 ms for media changes or other expensive renders.~~
- ~~Cancel superseded draft work so an older slow render can never replace a newer edit on the device.~~

#### Priority 1.3 post-completion remediation — Deferred to v1.0

Whole-project review findings F1–F15 were reproduced, fixed under TDD, independently reviewed and merged (commits and review IDs are recorded in GitLab issue #2). The remaining release-reliability findings are deferred to v1.0 hardening and stay open in the consolidated issue #33.

### ~~1.4 Add borders and backgrounds to basic custom modules~~ — Completed

~~Add an **Appearance** control group to the basic **Add custom module** editor for backgrounds and borders, matching the controls and executed rendering behavior already used by Announcement Board rather than creating a second styling model.~~

- ~~Provide **Background colour**, **Border style**, and **Border colour** controls.~~
- ~~Use the same Announcement Board border choices and labels: **None**, **Solid**, and **Inset**.~~
- ~~Keep **None** as the default border style and preserve the existing default background for newly created modules.~~
- ~~Apply these controls to both static-text and scrolling-text basic custom modules, including the complete physical rendering path rather than preview-only decoration.~~
- ~~Reuse the Announcement Board border geometry, clipping, colour validation, and reset behavior so equivalent settings render identically.~~
- ~~Fill the complete 64×32 module background before drawing its text and border.~~
- ~~Hide or disable the border-colour control when **None** is selected without discarding the user's previously chosen colour.~~
- ~~Include background and border changes in Live/Draft preview, reset defaults, persistence, playlist playback, and browser-versus-physical parity tests.~~

**Completed 2026-08-29:** protected merge `c4f40d5e3ec1ccb02be3940a8684a3acf4c460f4` passed protected-main pipeline 281, including source-bound physical-device acceptance and recovery. Production then passed a preservation-safe deploy/candidate/physical/rollback/rollback-verification/redeploy transaction: exact schema-v0 revision-53 bytes and metadata were restored during rollback, the accepted release migrated deterministically to schema v2 revision 54, and the final real 64×32 acceptance produced one static frame plus 85 frames for each left- and right-scrolling fixture. A live Chrome 151 journey at 390×844 and 1440×900 verified the pair-page version, Basic text/static-and-scroll controls, left/right direction, background and None/Solid/Inset border behavior, 64×32 preview, touch target, responsive layout, and zero save/preview mutations; temporary pairing and test residue were removed with the paired-device registry and final configuration independently re-attested.

### ~~1.5 Add GIF and video support to basic custom modules~~ — Completed

~~Basic custom modules should allow the user to select a GIF or video as the module content.~~

- ~~A selected GIF or video creates a media-only custom module.~~
- ~~Keep controls appropriate to the selected media type.~~
- ~~Do not add text overlays, layered elements, or compositing to the basic editor.~~
- ~~Do not imply that the basic module can combine text and media.~~
- ~~Offer **Contain**, **Cover**, **Stretch**, and manual crop positioning.~~
- ~~Reuse the Advanced Canvas media drag/resize positioning interaction and its executed physical-rendering semantics rather than creating a visually similar browser-only crop tool.~~
- ~~Reset basic media modules to **Contain** at **100%** playback speed.~~

**Completed 2026-08-31:** reviewed source `5594f106fdbb91f46c20da65b397456f6d447b01` merged as protected-main commit `41355e00217c3f8df45ca83063828c939aa99c7e` after exact-SHA pipelines 307 and 308 passed. Production passed exact deploy, rollback, and redeploy with a healthy locked display service and byte-identical mutable state, and the accepted transaction was finalized. The independent exact-artifact review passed with zero findings. The separately discovered sub-10 ms GIF-cadence/verifier decision is deferred to v1 in GitLab issue 2 and does not alter Priority 1.5's completed media-only product behavior.

**Completion-record correction 2026-08-31:** the original Priority 1.5 closeout overstated the final requirement above. Priority 1.5 delivered media-only Basic custom modules, but it did not yet provide or reset a Basic-media playback-speed control to **100%**. That requirement is struck above only because Priority 1.6 subsequently implemented, merged, rolled back, redeployed, and accepted the shared **100%** default; it is not retroactive Priority 1.5 evidence.

### ~~1.6 Standardize GIF and MP4 playback speed across modules~~ — Completed

~~Every module that displays a GIF or MP4 should offer a playback-speed control. Use the same control model and terminology across built-in modules, basic custom modules, announcements, and Advanced Canvas wherever that media type is supported.~~

- ~~The default `100%` or `1×` setting should preserve the media's original playback timing.~~
- ~~Lower values should slow playback and higher values should speed it up.~~
- ~~GIF speed should scale the embedded frame delays without rewriting the source file.~~
- ~~MP4 speed should scale effective video playback timing without rewriting or transcoding the source file.~~
- ~~Use safe, bounded values and reject zero, negative, Boolean, malformed, or unsupported speeds.~~
- ~~Browser previews and physical Matrix output must use the same effective playback rate.~~
- ~~Playlist timing and displayed runtime should reflect the adjusted physical playback time rather than the source duration alone.~~
- ~~Slow playback must remain interruptible by module changes, preview changes, schedule changes, service stop, and shutdown.~~
- ~~Existing saved configurations should migrate to normal speed without altering unrelated settings.~~
- ~~Present speed as a shared slider plus an exact percentage input in every supporting module.~~
- ~~Allow `10.0%` through `400.0%` inclusive, with 0.1-percentage-point precision; normalize equivalent numeric representations before saving.~~
- ~~Changing media speed must not change loop count. Recalculate playlist runtime from the unchanged loop count and adjusted playback timing.~~

**Completed 2026-08-31:** reviewed source `2d4b35852b632260d5dc5fb6c4be6d36cb3f8341` merged through MR !101 as protected-main commit `6c312d40cc8d050d58a691d6266f91f6545d9194`. Exact-SHA push/MR pipelines 312 and 313 passed after one unchanged pre-existing heartbeat-test retry and a fresh aggregate retry; protected-main pipeline 314 passed 1,351 tests with zero failures, errors, or skips, zero quality/security findings, and source-bound physical acceptance plus recovery. Production's historical product port was narrowed to independently approved nine-runtime-path artifact `a67aeb4731708ee06d65e72a274d7ceea91604ea` (tree `e9b822596ac9171c003c496b2d29a5ca18aa71b5`) so unrelated production test files remained untouched. The guarded transaction passed deploy, deterministic schema-v2 revision-55 to schema-v3 revision-56 migration, deliberate byte-exact rollback, and final redeploy; unrelated mutable data and paired-device registry bytes remained exact. Live Chrome at 390×844 verified version `0.3.60-preview`, bounded shared speed controls, independent announcement headline/message fonts, horizontal containment, and zero configuration saves. Fresh physical 64×32 acceptance observed three distinct 200% sleep-animation frames and a later announcement-board publication, then removed the temporary pairing exactly. Hostile-runtime reaper hardening remains explicitly deferred to v1 in GitLab issue #12 and is not claimed complete here.

### ~~1.7 Advanced Canvas Pixel drawing tool~~ — Completed

**Completed 2026-10-10:** Boundaries 1–3 released; the remastered direct-canvas editor shipped in 0.3.68-preview and was approved on 5002 UAT.

~~Treat **Pixel** in Advanced Canvas as a transparent, editable pixel-art layer rather than a single manually positioned pixel element.~~

#### ~~Boundary 1 — Backward-compatible bitmap schema and renderer foundation~~ — Completed

**Completed 2026-09-03:** the UI-inert B1 foundation was implemented and corrected through RED→GREEN TDD, immutable review, protected MR !113 and exact-source pipelines 351/352. Fresh accepted-lineage product-port candidate `fc745163460a9316998a25be574af01ec25019d4` passed immutable staging, prepare, deploy, candidate checks, deliberate rollback and baseline verification, exact-candidate redeployment, cache-disabled Chrome acceptance, both named real 64×32 framebuffer fixtures, cancellation before framebuffer submission, mutable-state preservation, and finalization. The rejected physical candidate `953f640145fa576bb6019bd5665df572aefa9435` remains truthfully recorded in GitLab issue #22 and was not reused.

- ~~Accept one optional canonical 64×32 packed RGBA bitmap representation with exactly 8192 decoded bytes, binary alpha, zero RGB in transparent cells, bounded integer offsets, optional rotation, and strict rejection of malformed, mixed, keyframe-embedded, or unsupported values.~~
- ~~Keep existing and newly created Pixels layers on the legacy `points` representation by default; expose no bitmap editor control and perform no automatic configuration migration or save in B1.~~
- ~~Preserve legacy point validation and rendering exactly while providing deterministic legacy/bitmap cell conversion semantics.~~
- ~~Use equivalent browser and physical cell semantics for translation, occupied-bounds-centred rotation, clipping, transparency, opaque black, visibility, depth, and lower-layer composition.~~
- ~~Verify the named `corners_hole_black_composite` and `translated_clipped_rotation` fixtures byte-for-byte in cache-disabled Chrome and on the real 64×32 panel.~~
- ~~Keep cancellation effective after frame preparation and before framebuffer submission so a cancelled frame cannot change the panel, revision, or swap history.~~
- ~~Keep bitmap decoding bounded to 2048 cells with no decompression, provider, secret, or file input, and retain measured configuration-size headroom.~~
- ~~Provide the exact source-only Priority 1.7 transaction for build, prepare, deploy, status, rollback, redeploy, recover, and finalize while preserving excluded mutable configuration, drafts, paired devices, data, and media.~~
- ~~Prove immutable local/remote stage identity, candidate health, deliberate byte-exact rollback, baseline health, exact-candidate redeployment, browser/physical parity, service/display-lock recovery, and zero mutable-state drift before finalization.~~

**Boundary 2 is complete:** the user-facing drawing surface, grouped/contextual controls, pointer/touch/keyboard interactions, undo/redo history, and editable save/reset flow are implemented and released.

#### Drawing surface and pointer behaviour

- ~~Draw directly on a 64×32 grid in which every editor cell maps to exactly one physical Matrix pixel.~~
- ~~Provide **Zoom in**, **Zoom out**, and **Fit canvas** controls, display the current zoom percentage, and never let zoom alter the 64×32 artwork. Keep unrestricted panning out of the first version so the complete small canvas remains manageable.~~
- ~~Highlight the grid cell beneath the pointer and show its exact `X` and `Y` coordinate together with whether the current action will draw, erase, or pick a colour.~~
- ~~Use an editor-only transparency pattern so transparent pixels are visibly distinct from black pixels.~~
- ~~Use left-click and drag to draw continuously with the selected colour.~~
- ~~Use right-click and drag to erase continuously, and suppress the browser context menu inside the drawing surface.~~
- ~~Fill every grid cell crossed by a stroke so quick pointer movement does not leave gaps.~~
- ~~End a stroke cleanly when the pointer is released or leaves the drawing surface.~~
- ~~Provide explicit **Draw** and **Erase** modes for touch and keyboard users.~~
- ~~Start with an exact one-physical-pixel brush.~~

#### Grouped drawing controls

~~Provide:~~

- ~~**Draw**, **Erase**, and **Pick colour** tools;~~
- ~~a colour picker and the existing Matrix colour swatches;~~
- ~~**Undo** and **Redo**;~~
- ~~**Clear layer** and **Fill layer**; and~~
- ~~**Reset to last saved state**.~~

~~Treat each continuous stroke as one undo action rather than one action per pixel. Undo and redo must cover drawing, erasing, clearing, and filling. Clear and fill must be deliberate actions that cannot destroy the drawing accidentally.~~

~~The **Pick colour** tool must sample the visible pixel under the pointer from the currently displayed composite and set it as the selected drawing colour. It must work through an explicit control for mouse, touch, and keyboard users rather than depending on a pointer-only gesture.~~

#### Contextual drawing toolbar

~~Show a compact drawing toolbar only while a pixel-art layer is selected. Keep it inside the Pixel layer editor directly above the 64×32 drawing surface, hide it for every other layer type, and never float it over or obscure the artwork.~~

~~On desktop, keep the primary actions in one horizontal toolbar in this order:~~

~~**Draw | Erase | Pick | Colour | Undo | Redo | Zoom − | Zoom + | Fit | More**~~

- ~~Clearly highlight the active drawing tool.~~
- ~~Give every icon an accessible name and a visible tooltip; do not rely on unexplained icons.~~
- ~~Put **Grid on/off**, **Fill layer**, **Clear layer**, **Reset to last saved state**, and keyboard-shortcut help in **More**.~~
- ~~Keep destructive actions out of the primary row and require the deliberate safeguards already specified for them.~~
- ~~Keep the toolbar attached to the drawing editor while that editor scrolls, without making it a page-wide Advanced Canvas toolbar.~~

~~On narrow and touch layouts, keep the toolbar inside the Pixel editor, allow it to wrap into two compact rows, use sufficiently large touch targets, and show icons with short labels for **Draw**, **Erase**, and **Pick**. Do not use a hover-only toolbar or a draggable floating palette.~~

#### Keyboard shortcuts

~~Provide:~~

- ~~`Ctrl/Cmd + Z` for undo;~~
- ~~`Ctrl/Cmd + Shift + Z` and `Ctrl/Cmd + Y` for redo;~~
- ~~`D` for Draw;~~
- ~~`E` for Erase; and~~
- ~~`I` for Pick colour.~~

~~Do not activate drawing shortcuts while the user is typing in a text, number, or other editable form field. Keep every shortcut paired with a visible control so pointer, touch, and keyboard users receive the same functionality.~~

#### Layer and editing behaviour

- ~~Keep the artwork as a normal Advanced Canvas layer that can be renamed, shown or hidden, reordered, duplicated, deleted, repositioned, and reset to its default position.~~
- ~~Moving the layer moves the complete drawing while preserving its internal pixel grid.~~
- ~~Transparent pixels reveal lower layers so pixel artwork can be composed with text, images, GIFs, videos, and other pixel-art layers.~~
- ~~Selecting an existing pixel-art layer reopens it for drawing, with every saved pixel still individually editable.~~
- ~~Switching tools or layers must not discard the current draft.~~
- ~~Provide an optional editor-only grid overlay that never appears on the physical Matrix.~~

#### Preview, persistence, and cancellation

- ~~Update the browser draft preview immediately while drawing.~~
- ~~Change the physical Matrix during editing only when the user enables the existing opt-in on-device draft preview.~~
- ~~Use the same coordinates, clipping, transparency, and compositing rules in the editor preview and physical renderer.~~
- ~~Save through the normal Advanced Canvas save flow, preserving the bounded bitmap in an editable form.~~
- ~~Cancelling or resetting restores the last saved version.~~

#### Confirmed first-version boundaries

~~The first version is static pixel artwork with a one-pixel brush. Do not include gradients, anti-aliasing, smoothing, spray brushes, shape tools, symmetry tools, imported-image conversion, or a separate animation-frame editor. Existing Advanced Canvas layer positioning and compositing still apply.~~

#### ~~Boundary 3 — Remastered direct-canvas pixel editor~~ — Completed

~~The Boundary 2 side-panel editor (separate zoomable grid, editor-only checkerboard, single-layer history cache) is replaced by direct drawing on the main 64×32 Advanced Canvas stage, matching the owner's direction that the canvas stays a fixed 64×32 surface and is drawn on directly. The saved bitmap schema, physical renderer, legacy bake/flatten confirmation, and save/reload/stale-peer flows are unchanged.~~

- ~~Show the pixel toolbar above the stage only while a Pixels layer is selected: **Move | Draw | Erase | Blend | Pick | Colour | Size | Shape | Undo | Redo | More**, with Matrix swatches below. **More** holds Grid on/off, Fill layer, Clear layer, Reset to last saved state, and shortcut help.~~
- ~~Paint every display cell under the brush through the layer's current offset and occupied-bounds rotation, so strokes land exactly where the pixel is shown, including on moved and rotated layers.~~
- ~~Brush size 1–16 physical pixels (size 1 is exactly one pixel) with **Square**, **Round**, and **Flat** shapes; an on-stage brush outline previews the footprint, and the coordinate/tool/brush status sits under the stage.~~
- ~~**Blend** averages the colour into opaque pixels and paints transparent pixels with the colour, keeping canonical binary alpha.~~
- ~~**Pick** samples the composited 64×32 output (background and every visible layer), then returns to Draw.~~
- ~~**Move** drags the whole layer by integer offsets without touching bitmap bytes; keyframes and overrides never carry bitmap data.~~
- ~~History is per canvas, covers every Pixels layer in it, survives re-renders and layer switches, holds 32 steps, treats one stroke as one step, and is cleared by a successful save. Pointer capture keeps a stroke alive past the stage edge; release commits, cancel/blur or switching layers reverts.~~
- ~~Shortcuts: `M`, `D`, `E`, `B`, `I`, `[`/`]` brush size, `Ctrl/Cmd + Z`, `Ctrl/Cmd + Shift + Z`, `Ctrl/Cmd + Y`; right-click erases. New Pixels layers start as an empty bitmap rather than a legacy point.~~

### ~~Splash/Sleep media screens~~ — Completed

**Completed 2026-10-10:** shipped in 0.3.68-preview and approved on 5002 UAT. The old 0.3.64 release checklist (independent reviews, rollback/redeploy rehearsal, Kanban handoffs) was superseded by the lean `deploy/ship.py` delivery path.

- ~~One flow per screen with screen-specific media, saved-state repair, and a selected-GIF still-duration calculation.~~
- ~~Splash plays a shuffled bag of its enabled screens.~~
- **Fix (owner, 2026-10-30; 0.3.85-preview):** each Splash slot plays exactly **one** random pick from everything ticked, then the playlist moves on. The dashboard saves every ticked item as its own (hidden) Splash row, and those rows used to play one after another; the scheduler now treats all Splash rows as one pool (shuffled bag: every item once before repeats) played at the first row's place.
- ~~Sleep picks one enabled screen at random as the night begins and plays only that one until wake; the next night picks again.~~
- ~~The pick survives previews, reloads and pairing; it moves to another enabled screen only when the picked one has no usable media.~~
- ~~A Sleep screen with several sources keeps its one retained media choice for the night.~~
- ~~Focused backend and real-browser tests (`tests/test_screen_media.py`, `tests/test_media_playlists.py`, `tests/test_webapp.py`).~~

### ~~1.8 Rework the Advanced Canvas timeline and animated-media import~~ — Completed (merged 2026-10-10, !139; ships with the next release)

~~The Advanced Canvas timeline is currently confusing to understand and operate. Rework it so creating, selecting, ordering, editing, and previewing frames is clear and predictable for new and existing users.~~

- ~~Make the current frame, frame order, playback position, layer state, and available timeline actions visually unambiguous.~~
- ~~Use clear labels, grouping, and interaction feedback rather than relying on unexplained icons or implicit timeline behavior.~~
- ~~Keep frame creation, duplication, deletion, reordering, seeking, and playback practical with mouse, touch, and keyboard input.~~
- ~~When a GIF is added, automatically create the complete corresponding timeline frame sequence from all frames in the GIF, preserving its frame order and timing.~~
- ~~When an MP4 is added, automatically create the complete corresponding timeline frame sequence from the video, preserving playback order and timeline timing at the module's configured frame rate.~~
- ~~Do not require users to create one Advanced Canvas frame manually for each decoded GIF or MP4 frame.~~
- ~~Keep browser preview, saved timeline data, and physical Matrix playback consistent, including media timing, looping, clipping, and cancellation.~~
- ~~Preserve existing Advanced Canvas configurations and add real-browser usability coverage at mobile, tablet, and desktop widths.~~

Delivered: Playhead / Frames / Selected-layer groups; Duplicate, Delete, Move earlier/later whole frames (rebuilt so saved data and the panel match); keyboard Left/Right, Home/End, Shift+arrows, D, Delete. Choosing a GIF/MP4 on a layer builds its frames; a single-frame canvas adopts the media's own timing, otherwise the module FPS is kept. The 120-frame cap remains and the import message states how much plays and which FPS would fit all of it. Tests: `tests/test_canvas_timeline_rework.py`.

### ~~1.9 Add static-image support to basic custom modules~~ — Completed (merged 2026-10-10, !138; ships with the next release)

~~The basic **Add custom module** workflow should allow the user to select a single static image as the module content, alongside the existing GIF and MP4 media choices.~~

- ~~Create an image-only Basic custom module without requiring text, animation, layers, or Advanced Canvas.~~
- ~~Reuse the existing Basic media selection, upload, validation, positioning, preview, persistence, and physical-rendering workflow wherever the behavior is equivalent.~~
- ~~Provide the same **Contain**, **Cover**, **Stretch**, and manual crop-positioning options available to Basic GIF and MP4 modules.~~
- ~~Preserve the source image's aspect ratio for **Contain** and **Cover**, and make clipping or stretching explicit in the browser preview.~~
- ~~Keep browser preview and physical 64x32 Matrix output consistent.~~
- ~~Preserve existing custom modules and reject unsupported, malformed, or oversized image files with a clear validation message.~~

Delivered: PNG/JPEG/WebP stills in Basic media modules, held for the playlist duration (Loops/Playback speed hidden), with a fit note explaining bars, cropping or stretching. Tests: `tests/test_basic_static_images.py`.

**Also delivered alongside Priority 1 (!140):** the media library pages 10 items at a time, with 20/30/40/50 per page; the choice is remembered per browser.

### 1.10 Find the Matrix after an address change — Owner-approved 2026-10-11, in progress

When the router restarts it can hand the Matrix a new IP address and the user no longer knows where the dashboard is. The device is used by anyone on any network, so the fix must live on the device: no router reservation, no router or DNS changes. Approved mockup: `http://192.168.1.123:5050/` (`/single`, `/multi`); GitLab issue #34.

Split into independently mergeable sub-items, in this order:

- **1.10a Panel shows the address when it changes.** The device watches its own address. On a change, and once at start-up, it pauses the playlist for about 10-15 seconds and scrolls the name and address (for example `matrix-3f9a.local 192.168.1.7:5000`), then resumes. With no address it shows "No network". The pairing PIN takeover keeps priority.
- **1.10b A fixed per-device name.** Default `matrix-` plus the last 4 hex characters of the built-in (not randomised) MAC address, for example `matrix-3f9a.local`. Stable across reboots and power cuts.
- **1.10c Rename only at the display.** One "Display name" setting on that display's own paired dashboard (never on matrix.local). Letters, digits and hyphens, at most 63 characters, not starting or ending with a hyphen; `matrix` is reserved; names already seen on the network are refused; Reset to default. Saving also needs a one-time 4-digit code shown on the panel (2-minute expiry, 3 wrong tries cancels), so a paired browser that is not in front of the display (for example children on the Wi-Fi) cannot rename it. Pairing/auth change: needs the second reviewer.
- **1.10d Displays announce and find each other** on the local network (mDNS service), which feeds the directory list, flags duplicate names and older versions, and greys out displays that went quiet (dropped after 10 minutes).
- **1.10e `matrix.local` is a directory page, not a display.** Any one display also answers to `matrix.local`; requests for that name get a read-only page listing every Matrix display (name, address, version, now playing, last seen) with links to each dashboard. No settings, no controls, no PIN. If the serving display goes away another takes over within seconds. With one display it shows a single card and a "bookmark this" note.
- **1.10g Pairing screen shows the name; opening by IP moves to it** (owner 2026-10-15, !167, 0.3.74-preview). The panel's pairing rotation adds a `PAIR AT / matrix-7d03 / .local:5000` page between the PIN and IP pages; names wider than the panel scroll. A pair page opened by IP moves to the `.local` name when that name answers with this display's own id, unless the visitor has started typing; Back returns to the IP. Browsers already paired by IP are never moved and no pairing travels between addresses (mDNS is unauthenticated). They get an "Open this display by name" link and pair once with the PIN on that address. Browser preferences (media page size, log colours, device-preview choice) are per address and start fresh on the name. Deferred risks are recorded in #33.
- **1.10f Deploys find the device by identity.** `deploy/ship.py` and the physical-device CI job stop relying on a hardcoded IP: they locate the device by its pinned SSH host key and refuse anything else. Ship tooling change: needs the second reviewer.

Open before 1.10d/e can ship: they need avahi (`avahi-daemon`, publishing via `avahi-publish`) on the device; whether it is installed is not yet checked, and installing a package is a system change that needs the owner's go-ahead. Limits to state in the UI: guest or business networks that isolate clients only list the serving display; the first visit needs `http://` and `:5000`.

### 1.11 System-wide Screen Safe Area — Owner-requested 2026-10-15, implemented in 0.3.78-preview (branch `feat/safe-area`, not yet merged/deployed)

Matrix panels sit in frames of different sizes, and a frame can hide the outer pixels. One device-wide safe area tells every module where it may draw.

- **Where:** a "Screen Safe Area" setting in Device settings, on that display's own paired dashboard. It is saved per display.
- **Shape:** space from the top, right, bottom and left edges, in whole physical pixels, 0 to 6 per edge (owner 2026-10-15: maximum 6).
- **Full-screen media toggle:** "Full-screen media stays inside the safe area", default **on**. On: splash, sleep, GIF/video and image screens are fitted inside the safe area like everything else. Off: they fill the whole 64×32 panel, under the frame.
- **No confirmation code:** changing the safe area does not need a code shown on the panel (owner 2026-10-15); any paired browser on that display can change it.
- **Marking the area:** while the setting is being edited, the physical panel and the dashboard preview show the edge of the safe area as a **dotted, pulsing yellow line**. The line is never drawn during normal playback.
- **Applies everywhere:** all modules respect it. That covers the clock, weather, RSS, announcements, calendar, custom modules and Advanced Canvas, plus the system screens (pairing PIN, address/name banner, splash, sleep). Edit Preview and Live preview show the same clipped result as the panel.
- **Removes the calendar border (owner 2026-10-15):** once the safe area ships, the calendar's fixed 2-pixel frame border (0.3.75, `FRAME_MARGIN` in `modules/event_countdown.py`) is redundant and is removed; the system safe area alone defines the frame. The calendar's own per-edge "space from edge" settings stay as optional extra spacing inside the safe area. No saved settings are lost. To keep calendars that have the border today unchanged, existing displays should start with a 2-pixel safe area.
- **Decided while building:** new displays start at 0 px; displays saved before 1.11 read as 2 px on every edge (so calendars keep the old border). ~~Background colours (`fill`/`clear`) cover the whole panel, under the frame;~~ all other drawing is translated and clipped to the inner rectangle.
- **Margin is always black (owner 2026-10-31, 0.3.84-preview):** the ring outside the safe area is never lit, as if the panel were physically smaller. Fills, backgrounds and borders paint only the inner canvas, on the panel, the Live/device preview and Edit Preview alike. One shared layer enforces it (`Matrix._finish_safe_area_frame` at every swap, `CaptureMatrix.snapshot`, `SafeArea.compose`); modules carry no margin code. Only full-screen media with the toggle off may still cover the whole panel. A calendar with a non-black background therefore loses the background-coloured 0.3.75 border ring (it is black now).

**Implementation status (0.3.78-preview):**
- `safe_area.py` holds the setting (`config["device"]["safe_area"] = {top, right, bottom, left (0..6), media_inside}`); `Matrix`, `FrameCapture` and the Edit Preview `CaptureMatrix` translate and clip identically, so Edit/Live preview match the panel.
- Calendar `FRAME_MARGIN` removed; calendar, clock lava and the weather tickers lay out at the inner size.
- Full-screen media (splash, sleep, `media:*` playlist items and custom media screens) with the toggle on is **scaled** into the inner rectangle (saved panel geometry such as x0 y0 w64 h32 becomes the full inner area), identically in `play_media`, Edit Preview `media_preview_frames` and custom media screens; with the toggle off it covers the whole 64×32 panel.
- Device settings has a "Screen Safe Area" group (Top/Right/Bottom/Left 0..6 and the media toggle) saved through the normal Save. While those inputs are focused the dashboard preview shows the unsaved edge as a dotted, pulsing yellow line; it disappears on blur/save and never shows otherwise.

**Rollback to 0.3.75 (release-auditor M1):** 0.3.75 refuses to start with an unknown `device.safe_area` key, and `ship.py --rollback` restores code only (never `data/`). So an existing display's `data/config.json` gets the key **only when the owner actually changes the safe area**: `ConfigStore.update()` drops the read-time 2 px projection before writing (also when a client re-sends that unchanged 2 px value), and the dashboard sends `device.safe_area` only after the inputs were changed. Untouched displays stay loadable by 0.3.75. Fresh installs (and legacy schema 0..3 migrations) write the key at first start; they have no 0.3.75 config to roll back to.
- **Rollback after the owner has changed the safe area requires removing `device.safe_area` from `data/config.json`** on the display (e.g. `jq 'del(.device.safe_area)'` into a temp file, then replace atomically, keeping owner/mode) before or right after `ship.py --rollback`; otherwise 0.3.75's matrix-web fails to load its configuration. Tell the owner this in the DEPLOYED message for 0.3.78.

**Known limitations:**
- RSS and astronomy layouts are translated and clipped into the safe area rather than re-laid-out for the smaller size; at 2 px the astronomy countdown row is partly clipped.
- The dotted yellow marker is shown on the dashboard preview and, since 0.3.81-preview (owner 2026-10-31), on the physical panel while Preview edits on device is on and a Screen Safe Area field is focused or edited: the page sends `safe_area_marker` with the device-preview request and the controller turns `Matrix.set_safe_area_marker` on for that draft only.

### 1.12 Trusted HTTPS on `<id>.mtx.xert.me` — Owner-requested 2026-10-15, planned

Browsers only trust certificates for public names, so `.local` cannot get one. Each display gets its own name under the owner's domain `xert.me` (DNS on Cloudflare). For example, `7d03.mtx.xert.me` points to the display's LAN IP and carries an automatic Let's Encrypt certificate. The display never needs to be reachable from the internet.

- **Name and DNS:** `<id>.mtx.xert.me`, an A record set to the display's LAN IP, DNS-only (grey cloud). It is updated when the address changes; 1.10a already detects address changes.
- **Certificate:** Let's Encrypt with the DNS-01 challenge. Renewal is automatic and well ahead of expiry, because certificate lifetimes are shrinking toward about 47 days. The web server serves HTTPS on 443 (`--certfile`/`--keyfile`) and reloads when the certificate is renewed.
- **Redirects:** port 80 sends browsers to `https://<id>.mtx.xert.me`. The IP, `matrix.local` and `<name>.local` all end on the padlock address when it is reachable; the probe checks the id, as in 1.10g.
- **Fallback:** if the network blocks public names that point to private addresses (DNS rebinding protection), or there is no internet, the display stays on `http://<name>.local:5000`. It can test which case applies and show it.
- **Credentials:**
  - **Stage A (owner's display):** a Cloudflare API token limited to Zone DNS Edit on xert.me, stored only on that device. The owner places the token; it is never pasted into chat.
  - **Stage B (product):** a small xert.me service holds the token. Each display gets a key at first boot and may only edit its own `<id>.mtx` records, so no Cloudflare credential ever ships on a device.
- **Knock-on work:**
  - the 1.10g name probe moves to HTTPS (mixed content);
  - cookies become `Secure`;
  - browsers paired over HTTP pair once more;
  - a matching systemd unit change.

  The work touches auth and the unit, so it needs the second reviewer.
- **Limits:** Let's Encrypt allows about 50 new certificates per registered domain per week. That is fine for early sales; at volume it needs a rate-limit increase.
- **Benefit:** it closes the #33 risk of a spoofing device phishing the PIN, because an impostor cannot get the display's certificate.
- **Waiting on:** the owner's Cloudflare token for Stage A.

## Web app architecture and load-time hardening — Owner-approved 2026-10-31

Plan: [`docs/WEB_ARCHITECTURE_PLAN.md`](WEB_ARCHITECTURE_PLAN.md). Trigger: dashboard first load was 509 KB uncompressed and the web UI froze while pairing during a long module (hotfixes !185 and !186, shipped 2026-10-31).

- **P0 (in flight)** hotfix: gzip + cache headers, slim dashboard payload, measurement script.
- **P1 safety net (start now):** route-manifest test, page-weight budget in CI, `/api/health`, Server-Timing and slow-request log, `tests/web/` fixtures.
- **P2 backend:** split `create_app()` (~2,800 lines, 48 routes) into blueprints on a shared services object, one domain per MR; pairing and release-acceptance last, with the second reviewer.
- **P3 front end:** thin dashboard shell plus lazily loaded native ES modules per editor, **no build step** (owner rule), cacheable data endpoints.
- **P4 deployment:** teach `ship.py` the new paths and fix the data-file gap (#33) first.
- P2-P4 run between features, one MR at a time; they never block a feature. P5 (a framework/build) only if P3 leaves state management hard to maintain.

## Priority 2 — New playlist modules

Implement these only after higher-priority fixes and workflow improvements are complete. Their order favors lower-friction local or free-data behavior before more provider-dependent integrations.

**Owner continuation authorization, 2026-10-11:** after the local portion of 2.1 is implemented and verified, continue sequentially through the remaining eligible Priority 2 work without another routine start approval. Priority 1.10 remains paused and reserved for Opus. External API integrations, or entire modules that require them, may remain deferred; preserve their requirements and label them deferred rather than complete. The development sequence is **2.1 local Event Countdown → 2.2 RSS/Headline Ticker → 2.3 local Astronomy**. Public RSS/Atom feeds and versioned static astronomy datasets are treated as data sources, not account/API integrations; optional Google and astronomy API providers remain deferred, and **2.4 Spotify is deferred** because its functionality requires the Spotify API. This authorizes development and verification, not access to or deployment on production.

### 2.1 Event Countdown module

Add an Event Countdown playlist module for birthdays, anniversaries, launches, holidays, openings, and other deliberately selected events.

**Delivery decision, 2026-10-11:** the owner selected **deliver local events first; defer Google connection**. The active implementation is the local-event module, editor and shared physical/browser rendering. Google synchronization below remains a preserved future requirement, not part of the first local-event delivery or a claim of completion. See `docs/event-countdown-provider-notes.md` for the verified OAuth callback constraints.

**Local development checkpoint, 2026-10-11:** local implementation and affected browser/configuration/runtime tests are verified; required config/state review closed all blocking findings at 17:18 UTC. Google remains deferred. A nonblocking first-frame GIF sampling discrepancy remains recorded in the independent review. This is not merged, CI-green, deployed, or physical-device acceptance; requirements below remain visible rather than declaring the entire priority shipped. Authorized sequential local development now proceeds to 2.2.

#### Confirmed behavior

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

#### Confirmed layouts

- Large countdown with a short event title.
- Title, countdown, and optional icon.
- Compact progress bar showing time remaining.

Media-background composition is not part of the confirmed first-version layouts. Advanced Canvas remains the place for layered text-over-media countdown designs.

#### Google Calendar synchronization

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

#### Confirmed implementation decisions

##### Typography, colour, icons, and overflow

- Use `5x7` as the default title font and the largest installed countdown font that fits the selected layout. Allow installed-font overrides for both, with live browser-preview warnings when an override overflows.
- Scroll a title only when its measured BDF pixel width exceeds its allocated region. Complete exactly one pass with a four-pixel trailing gap, then restore and freeze the title at its starting position.
- Use one shared module style with optional per-event accent overrides. Default to a black background, white text, and Matrix accent blue `#7170FF` for progress and icon accents.
- When an event has no selected artwork, use a bundled event-type icon and fall back to a generic calendar icon when no event type matches.

##### Progress geometry and display safe area

- Default to a full-width, two-physical-pixel-high bar at the bottom. Keep its fill anchored on the left and reduce the filled width as the event approaches.
- Allow progress-bar height from one through four physical pixels, defaulting to two.
- Apply configurable top, right, bottom, and left safe-area insets to the complete Event Countdown layout, including the progress bar. Allow zero through eight physical pixels per edge and default every inset to zero.

##### Google synchronization bounds and ordering

- Import upcoming eligible events for the next two years plus relevant annual recurrences.
- Fetch at most 20 provider pages per selected calendar while also enforcing combined limits of 10,000 normalized events, 25 MB of fetched payload, 20 MB of stored cache, and 64 MB of transient synchronization memory.
- When a pagination, event-count, payload, or storage bound is reached, retain usable partial results and explain the truncation clearly in the dashboard. When the memory bound is exceeded, abort the refresh safely and retain the last valid cache.
- After timezone conversion, sort recurrence exceptions by Matrix-local start time and use Google provider order only to break equal-time ties.

### 2.2 RSS and Headline Ticker module

Add an RSS/Atom playlist module for BBC news and other user-selected publishers. It should present deliberately selected headlines rather than acting as a device-monitoring dashboard.

#### Confirmed sources and feed controls

- Provide built-in BBC presets for **Top Stories**, **World**, **UK**, and regional news.
- Also allow arbitrary user-supplied RSS or Atom feed URLs.
- Refresh feeds every 30 minutes.
- Apply one shared include-word list and one shared exclude-word list across the module.
- Merge eligible stories in round-robin feed order so a high-volume source cannot dominate.
- Make stories per playlist pass configurable, with a default of five.
- Prefer stories not previously shown; when all eligible stories have been shown, fall back to the newest stories.
- Deduplicate stories across feeds using stable IDs or canonical links where available, with a conservative title/date fallback.

BBC preset URLs and regional availability must be verified against BBC's official current feed catalogue during implementation rather than copied from an unverified or stale list.

#### Confirmed 64×32 presentation

- Keep the headline fixed while a short overview scrolls upward beneath it.
- When a usable feed image is available, place a 16×16 thumbnail on the left, keep the headline beside it, and use the full width below for the upward-scrolling overview.
- When no usable image exists, reflow into the text-only layout without leaving an empty image box.
- Strip markup from feed descriptions and render bounded plain text; do not execute embedded HTML, scripts, tracking pixels, or feed styling.
- Offer shared **Slow**, **Normal**, and **Fast** overview presets for the whole module. Persist their canonical numeric frame delays so preview and physical playback use the same timing path.
- Calculate and display physical runtime after content is fetched because headline, overview, image availability, and story count affect the duration.
- Each story should complete one bounded upward-summary pass before the next selected story begins.
- Scroll an overlong headline horizontally while the summary scrolls upward. Provide a toggle between one headline pass followed by a frozen headline and continuous headline looping until the summary pass finishes.
- After one headline pass, restore and freeze the headline at its starting position. Verify that exact behavior with a 64×32 browser-preview/physical fixture before implementation is considered closed.
- Accept every thumbnail media format already accepted by the media library.
- Animated thumbnails use source timing by default and expose the shared `10.0%`–`400.0%` speed control only in Advanced settings.

#### Refresh, cache, and failure behavior

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

#### Custom-feed security boundaries

- Treat feed URLs, redirects, XML, story text, links, and images as untrusted input.
- Block loopback, link-local, private-network, metadata-service, and other unsafe destinations for custom URLs; revalidate every redirect and resolved destination.
- Defend against DNS rebinding and enforce bounded redirect counts.
- Never expose fetched feed content as trusted HTML in the dashboard.
- Reject unsupported URL schemes and oversized or invalid media without affecting the rest of the feed.

#### Confirmed implementation decisions

##### Typography, clipping, and scroll geometry

- Default headlines to `5x7` and summaries to `4x6`. Allow installed-font overrides with live clipping warnings.
- In the image layout, place the 16×16 thumbnail at columns 0–15 and rows 0–15, reserve column 16 as a one-pixel gap, hard-clip the headline to columns 17–63 and rows 0–15, leave row 16 as vertical separation, and hard-clip the upward-scrolling summary to columns 0–63 and rows 17–31.
- In the text-only layout, hard-clip the headline to columns 0–63 and rows 0–6, leave row 7 as separation, and hard-clip the summary to columns 0–63 and rows 8–31.
- Hard-clip thumbnail, headline, and summary rendering to their assigned regions and to the 64×32 frame; they must never overlap or bleed into another region.
- Wrap the summary to its available width, start it below the fixed header region, and complete one pass only after its final line has fully left the top of the summary viewport.
- After a one-pass horizontal headline scroll, restore and freeze the headline at its starting position while the upward-summary pass finishes.

##### Thumbnail preparation and cache

- Limit the disk cache to 50 MB, use deterministic least-recently-used eviction, and write cache entries atomically.
- Limit each thumbnail to a 5 MB download, 4096×4096 decoded dimensions, and a three-second preparation budget before conversion to its 16×16 display representation.
- If a thumbnail is absent, invalid, rejected, or times out, use no generic fallback artwork and reflow the story into the text-only layout.

##### BBC preset maintenance

- Keep BBC preset URLs in a version-controlled catalogue, verify them against BBC's official feed catalogue during each relevant release, and change them only through the normal reviewed and merged release process.

##### Editor states, reset, warnings, and accessibility

- If no eligible current or cached stories remain after filtering, yield on the Matrix and show a dashboard explanation identifying the responsible feed or filter condition together with a **Test feeds** action.
- **Reset to defaults** restores BBC Top Stories as the only feed, a 30-minute refresh, five stories per pass, **Normal** speed, empty filters, and one-pass headlines, and clears seen-story and feed/thumbnail cache state.
- Use action-oriented warning patterns including **Could not update [feed]; showing cache from [time]**, **Cache expired; module will skip**, **Feed rejected: [reason]**, and **Results limited: [limit]**. Announce status accessibly and place raw technical details behind an expandable **Details** section.
- Give controls explicit accessible names containing the feed or control name and action, expose reorder position and state, and never rely on colour, an icon, or a tooltip alone.

### 2.3 Moon Phases and Astronomical Events module

Add one configurable Astronomy playlist module with independent checkboxes for current Moon information, lunar events, meteor showers, and planetary events.

#### Confirmed content

- **Current Moon:** stylized phase icon, phase name, illumination percentage, local moonrise/moonset, and the next major phase.
- **Lunar events:** new and full moons, eclipses, supermoons, and blue moons.
- **Meteor showers:** peak date/window, expected activity level, and radiant direction.
- **Planetary events:** conjunctions, oppositions, and selected visible-planet highlights, including object names, local visibility window, and compass direction.
- Show only events observable from the selected location.
- Show the current Moon first, then all enabled upcoming events in chronological order.
- Make the future-event horizon configurable, with a 90-day default.
- If no enabled upcoming event is observable, show the current Moon when enabled and otherwise yield immediately to the next playlist module.

#### Location and time

- Reuse the configured weather location by default.
- Allow an optional astronomy-specific location override using place search or a map pin rather than requiring coordinate entry.
- Resolve and store the location's timezone explicitly; all dates, rise/set times, visibility windows, and countdowns must use that timezone with daylight-saving transitions handled correctly.
- Preserve the place label and location provenance so the editor can explain which location is being used.

#### Confirmed 64×32 layouts

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

#### Calculation and provider model

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
- Include every locally visible partial or total eclipse that meets the confirmed minimum local-visibility duration and label its type.

#### Failure and edge-case behavior

- If the optional provider fails, continue local calculations and use the last valid provider-event cache, visibly marked stale in the editor.
- Expire provider-event fallback data after 24 hours.
- Provider errors must never block current-Moon rendering or the playlist loop.
- Use bounded network timeouts, retries, response sizes, cache sizes, and event counts.
- Represent polar and unusual rise/set outcomes accurately, including Moon always above, always below, or no rise/set during the local date; never invent a time.
- Recheck event eligibility immediately before displaying each event because the visibility window may have passed since the list was prepared.
- Cancellation must stop calculations, provider work, card holds, scrolling, and framebuffer submissions promptly.
- All disabled content sections should be rejected by validation or yield safely without hot-looping.

#### Confirmed implementation decisions

##### Local calculations, ephemeris, and updates

- Use Skyfield with NASA/JPL's DE440s short ephemeris file (`de440s.bsp`), pinned by checksum; support dates from 2000 through 2100 and target rise/set results within one minute.
- Update the ephemeris only through a versioned, reviewed release with a pinned source URL and checksum. Retain the previous verified file for rollback.
- Allow at most 128 MB total for the active and rollback ephemerides. Reject oversized or checksum-mismatched files without replacing the last valid ephemeris.

##### Provider and catalogue policy

- Ship local calculations and versioned open catalogues first. Keep optional provider labels disabled until a compliant free no-login source is verified.
- Require any future optional provider to have clear licensing, redistribution, and attribution terms, permit the bounded cache, and provide quotas sufficient for six-hour refreshes. Do not enable a provider that fails any of these requirements.
- Preserve a verified provider label's meaning while normalizing only whitespace and casing. Show its source attribution in the editor and never invent a competing local equivalent.

##### Partial visibility

- Include an event when it has at least 10 continuous minutes that satisfy the local visibility criteria. Show only its locally visible window and label it **Partly visible**.

##### Card presentation and incomplete data

- Default titles to `5x7`, details to `4x6`, and countdowns to the largest installed font that fits. Allow installed-font overrides with clipping warnings.
- Default to a black background and white text, with Moon `#D9D9D9`, lunar/eclipse `#B388FF`, meteor `#FFD166`, and planetary `#4CC9F0` accents. Allow colour overrides.
- Use bundled pixel icons per event type and a generic star icon for unknown event types.
- Format event countdowns with at most two units: days/hours when distant, hours/minutes within one day, minutes within one hour, and **NOW** while active. Never show seconds.
- Omit missing optional catalogue/provider fields and reflow the card. Reject only events missing identity, time, or location eligibility, and explain each rejection in the dashboard.

##### Offline behavior and diagnostics

- Keep local Moon and planet calculations available indefinitely while the requested date remains inside the supported ephemeris range. Mark an open event catalogue stale after its published update date without blocking valid local cards.
- Show source/version, checksum status, supported date range, catalogue freshness, last successful update, and stale/error state in the editor, with raw technical details in an expandable area.

### 2.4 Spotify Now Playing module

Add a display-only Spotify Now Playing playlist module. Playback control remains in Spotify; Matrix must not expose play, pause, previous, next, seek, volume, shuffle, repeat, or device-selection controls.

#### Account and authorization model

- Support one connected Spotify account in the first version.
- Provide a dashboard **Connect Spotify** action that opens browser authorization and returns automatically to the dashboard.
- Follow whichever device is currently active on that account without displaying the device name.
- Use a user-created Spotify application/client ID with Authorization Code and PKCE so the installation does not need to retain a client secret.
- Request only the minimum read scopes required for current-playback metadata; do not request playback-control scopes.
- Store access and refresh tokens in protected secret storage outside the repository and `config.json`. Never return tokens through dashboard APIs, logs, previews, exports, or diagnostics.
- Validate OAuth state, redirect targets, account eligibility, API availability, and current Spotify platform requirements against provider documentation during implementation.
- Support explicit disconnect, token deletion, and provider revocation. If authorization becomes permanently invalid, yield on the Matrix and show a reconnect warning only in the dashboard.

#### Supported playback and metadata

Display every item Spotify reports as currently playing, including music, podcasts, audiobooks, advertisements, local items, and other provider-defined playback types when usable metadata exists.

- Display explicit tracks and show a small **E** indicator when the 64×32 layout has room.
- For music, use the item title and credited artist metadata.
- For podcasts, use the episode title and show name.
- For audiobooks, use the title and author or narrator when available.
- For advertisements and unknown item types, use Spotify's supplied creator when present; otherwise show the normalized playback type.
- Treat provider text as untrusted bounded plain text. Normalize missing, malformed, excessively long, or unsupported metadata without blocking playlist playback.

#### Confirmed 64×32 layout

Use one combined layout rather than rotating separate artwork and detail screens:

- center-cropped **Cover** artwork in a 16×16 area on the left;
- title and a secondary artist/creator line on the right; and
- a full-width playback-progress bar along the bottom.

Additional behavior:

- Keep the secondary line fixed on one line and truncate overflow with an ellipsis.
- Scroll a long title exactly once using the shared scrolling-text timing controls.
- Fall back automatically to a text-only arrangement when no usable artwork exists.
- Hide the progress bar when duration or position is unavailable rather than inventing progress.
- Provide Spotify-style default colours, optional per-element overrides, and an option to derive colours deterministically from album artwork.
- Automatic colours must enforce readable physical-display contrast and fall back to Spotify defaults when a safe palette cannot be derived.
- Browser preview and physical rendering must use the same prepared artwork, text fitting, colours, progress geometry, frame timing, and transition semantics.

#### Playlist timing and playback state

- Make the module hold configurable with a 10-second default.
- If one title pass needs longer than the configured hold, extend physical runtime until that single pass completes; never increase scroll count or cut the title off mid-pass.
- When nothing is playing, yield immediately to the next playlist module.
- When playback is paused, retain the current item for 30 seconds, then yield if playback has not resumed.
- On an item change, cancel superseded artwork/text work and switch immediately to the newest normalized playback state.
- Holds, title scrolling, transitions, provider waits, and image preparation must remain cancellable so playlist changes, preview changes, schedule changes, service stop, and shutdown advance promptly.

#### Polling, progress, cache, and failure behavior

- Poll continuously in the background while the Spotify integration is enabled.
- Make the polling interval configurable from 2 through 60 seconds, with a 5-second default.
- Interpolate progress locally between successful provider responses instead of polling once per rendered frame.
- Respect rate-limit responses and provider backoff even when that temporarily exceeds the configured interval. Use bounded timeouts and cancellation-aware retries.
- Keep provider network and artwork preparation outside the physical rendering loop.
- During a temporary API failure, retain the last known item for 30 seconds, mark it stale in the editor, and then yield.
- Cache recently played artwork in a bounded 50 MB disk cache with deterministic eviction and safe atomic writes.
- Fetch artwork only from validated provider-returned HTTPS locations using bounded redirects, response sizes, decode dimensions, formats, and preparation time. A failed image must fall back to the text-only layout.

#### Configurable transitions

Configure module entry, item change, and module exit independently.

- Each stage offers **Cut**, **Dissolve**, and **Fade**.
- Default every stage to **Cut**.
- Give entry, item change, and exit separate duration controls, each defaulting to 0.5 seconds when a non-Cut transition is selected.
- **Dissolve** uses linear frame blending.
- **Fade** uses smooth ease-in/out frame blending.
- Transition execution must be cancellable and must blend the exact physical frames, not a browser-only approximation.

#### Migration, validation, and tests

- Add the module disabled by default and migrate existing configurations without changing unrelated settings.
- Bound every string, duration, polling interval, colour, cache setting, transition type, and transition duration in the configuration layer.
- Test OAuth success, denial, state mismatch, refresh, disconnect, revocation, and secret non-disclosure.
- Test tracks, explicit items, podcasts, audiobooks, advertisements, local/unknown items, missing metadata, missing artwork, and unavailable duration/progress.
- Test playing, paused grace, idle yield, temporary stale grace, permanent authorization failure, item changes, rate limiting, and cancellation.
- Test title timing, the 10-second default hold, long-title runtime extension, progress interpolation/correction, all transition paths, automatic-colour contrast fallback, and browser/physical parity.
- Test the 50 MB artwork-cache boundary, eviction, malformed images, oversized responses, redirects, partial writes, and restart recovery.

#### Confirmed implementation decisions

##### Typography, coordinates, overflow, and progress

- Default the title to `5x7` and the secondary line to `4x6`. Allow installed-font overrides with live clipping warnings.
- Anchor 16×16 artwork at the safe-area top-left, start text 18 physical pixels from that left edge, place the title at the top and the secondary line eight pixels below, and anchor the progress bar inside the safe-area bottom.
- Allow independent top, right, bottom, and left safe-area insets from zero through eight physical pixels. Default every inset to zero.
- Allow progress-bar height from one through four physical pixels and default it to two.
- Validate inset and bar-height combinations so `top + bottom + bar height` never exceeds 16 physical pixels. Reject or constrain an invalid combination before saving so the 16-pixel-high artwork/text region and progress bar cannot overlap.
- Ellipsize the fixed secondary line by measured BDF pixel width, retaining the longest text that fits and appending an ellipsis glyph or `...` fallback.
- Show **E** only when its measured width plus a one-pixel gap fits while leaving at least 24 physical pixels for the title; otherwise hide it.

##### Artwork validation, preparation, cache, and colour

- Accept only provider-returned HTTPS artwork on a maintained official Spotify CDN allowlist. Permit at most three redirects and revalidate HTTPS scheme, destination safety, and allowed host at every hop.
- Accept JPEG, PNG, and WebP, decode only a static/first image, and enforce a 5 MB download, 4096×4096 decoded dimensions, and a three-second preparation budget.
- Use deterministic least-recently-used cache eviction and atomic writes. Do not evict the currently displayed item's artwork during its active hold.
- With automatic colours off, default to a black background, white text, and Spotify green `#1DB954` progress/accent elements.
- For automatic colours, use deterministic median-cut quantization to 16 colours; discard candidates below 15% saturation or below 5%/above 95% relative luminance; select the most frequent remaining candidate; and choose black or white text with at least 4.5:1 contrast. Fall back to the default Spotify palette if no safe result remains.

##### Transition bounds

- Allow each transition duration from 0.1 through 3.0 seconds in 0.1-second steps. Keep 0.5 seconds as the default when a non-Cut transition is selected.
- Use smoothstep `3t²−2t³` easing for **Fade** while retaining linear blending for **Dissolve**.

##### Spotify setup, eligibility, quotas, and branding

- Provide guided setup in the dashboard with the exact redirect URI and a copy action, client-ID entry, a PKCE connection test, minimum-scope explanation, and disconnect/provider-revocation guidance. Never request or retain a client secret.
- Implement the module only while required playback metadata is available to an eligible free Spotify account without API charges. If a paid account or paid API capability becomes mandatory, keep the module deferred.
- Treat the configured polling interval as a preference. Increase the effective interval automatically when current quotas or provider backoff require it and show the effective interval in the dashboard.
- Show required official Spotify attribution in the dashboard connection/settings area, not on the 64×32 playback card unless current policy mandates it.
- Revalidate application eligibility, exact redirect rules, minimum scopes, quotas, attribution, branding, and all current provider policies against official documentation during implementation.

##### Dashboard states, reset, cache management, and accessibility

- Use explicit connection states: **Not connected**, **Connecting…**, **Connected**, **Temporarily unavailable**, and **Reconnect required**.
- Use plain-language **Paused**, **Nothing playing**, and **Stale—last updated…** playback states, showing the remaining grace time where applicable.
- **Reset to defaults** disables the module and restores a 10-second hold, five-second preferred polling, the default Spotify palette, automatic colours off, and **Cut** transitions while preserving the account connection and artwork cache.
- Show artwork-cache size and item count and provide a confirmation-protected **Clear artwork cache** action; normal LRU eviction remains automatic.
- Use a polite accessible live region for connection, item, and playback-state changes, never announce interpolated progress continuously, and give every control an explicit accessible label.

## LATER — project-wide contextual help audit

- [ ] Audit every module and every confusing element for contextual **? help**: explain purpose, units, examples, actual matching/selection semantics, saved-versus-draft behavior and side effects (especially reset/cache actions). Support hover, keyboard focus and tap, accessible names/relationships, Escape/outside dismissal and unclipped narrow-screen content. RSS coverage is only the first slice, not completion of this project-wide audit. This deferred work does not block the current grouped RSS/Astronomy editor usability update. Track in consolidated GitLab issue #33.

## Planning appendix

### Implementation budget estimate

Free data providers do not make roadmap implementation cost-free. GPT-5.6-sol access through ChatGPT Pro, subscription renewals, usage limits, and any explicitly approved additional credits remain genuine development costs.

Current planning estimate as of 2026-08-07:

- Assume ChatGPT Pro costs approximately £200 per billing period; replace this assumption with the actual billed amount if it differs.
- Completing, testing, reviewing, merging, deploying, and physically verifying the full roadmap is expected to require roughly 2–4 billing periods.
- Budget **£400–£800** of allocated ChatGPT Pro cost for completion, assuming no additional credits or top-ups.
- If the current billing period is already paid and treated as a sunk cost, budget approximately **£200–£600** in future renewals.
- An optimistic one-period completion would allocate approximately **£200**; a six-period contingency would allocate approximately **£1,200**.
- Keep external data/API provider charges at **£0** under the provider policy above.
- Treat additional model credits, top-ups, hardware, or other purchases as excluded unless separately estimated and explicitly approved.

The estimate covers the 14 numbered independent implementation/review/release workstreams currently listed, grouped into approximately 6–10 substantial implementation domains; independently releasable sub-items may increase the workstream count. Expected model usage is approximately 10–20 million fresh input tokens, 250–600 million cached-input tokens, 0.8–1.8 million output tokens, and 3,000–7,000 model calls. Cached-input volume represents reused context, not newly generated content. This is a planning range rather than a fixed quote; update it if the roadmap scope, priority count, subscription price, model, quotas, or observed delivery rate changes materially.
