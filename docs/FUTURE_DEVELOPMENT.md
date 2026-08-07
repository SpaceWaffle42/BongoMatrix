# Future development notes

Last updated: 2026-08-06

This file records requested Matrix features that are intentionally deferred. They are product requirements and design notes, not descriptions of current behavior. Do not treat an item as implemented until its code, tests, review, merge, deployment, and physical-device verification are complete.

## Scrolling-text timing in seconds

Every scrolling-text module should display its calculated runtime in seconds, consistent with the duration shown for Lava Clock and Splash.

- Calculate runtime from the configured tick/frame speed and number of scrolls.
- Keep scroll count as an independent user choice.
- If the user edits the displayed seconds, change tick speed to achieve that runtime.
- Editing seconds must never change the number of scrolls.
- Do not add another manual duration source that can disagree with physical playback.
- Browser values and physical runtime must use the same calculation and rounding rules.

## Sticky Matrix preview

Keep the Matrix preview visible while the user scrolls through module controls, similar to the sticky navigation bar.

- The user should be able to adjust positions, colours, tickers, icons, and other settings without losing sight of the preview.
- On narrow screens, use a compact, collapsible, or otherwise space-conscious sticky presentation.
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

## Optional preview on the physical device

Add a separate **Preview edits on device** checkbox beneath the browser preview.

- Keep it off by default.
- When enabled, send the current unsaved draft to the physical Matrix temporarily.
- Clearly indicate that the device is showing draft content.
- Provide an obvious **Stop device preview** action.
- Do not mutate or persist the saved configuration.
- Return to normal playback when preview stops, expires, disconnects, fails, or the user leaves the page.
- Preserve the single-owner hardware-lock model; preview must run through the existing display controller rather than starting another Matrix process.

## GIF and video support in basic custom modules

Basic custom modules should allow the user to select a GIF or video as the module content.

- A selected GIF or video creates a media-only custom module.
- Keep controls appropriate to the selected media type.
- Do not add text overlays, layered elements, or compositing to the basic editor.
- Do not imply that the basic module can combine text and media.

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
