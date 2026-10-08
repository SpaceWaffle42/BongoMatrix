# Bongo Matrix — Project Expectations

## Goal
Build a reliable Raspberry Pi display appliance for a physical 64×32 RGB LED panel, controlled through a friendly local-network browser dashboard. Deliver working behavior on the device, not browser-only demonstrations.

## Product
Support ordered playlists, Lava Clock, weather, announcements, custom text, images, GIFs, video, Advanced Canvas composition and pixel drawing. Provide PIN-paired access, persistent configuration, synchronized paired clients and straightforward Wi-Fi setup.

## Essential behavior
- Live preview shows frames submitted to the physical panel.
- Edit Preview renders unsaved editor values without saving or changing the panel.
- Physical draft preview is a separate explicit opt-in; cancellation or owner loss returns to saved playback.
- Save is the configuration commit action. Preview and playlist navigation must not silently save drafts.
- Browser and native rendering agree on selected content, geometry, colours and timing. The pixel canvas stays 64×32.
- Splash and Sleep have independent media selections. Splash uses shuffled bags; Sleep selects once per session, with repeats allowed between sessions.
- GIFs complete one native-speed pass per invocation without timer cutoff. Still duration uses the selected GIF mean when between 5–10 seconds, otherwise 7 seconds. No separate duration control.

## Delivery expectations
- Follow the approved roadmap, one numbered priority at a time; keep unrelated changes separate.
- Preserve existing features, configuration, drafts, paired sessions, secrets and uploaded media.
- Use focused test-first fixes, one writer per workspace, independent review and exact-source GitLab CI.
- Verify real browser/framebuffer behavior, rollback, state preservation and installed-source identity before claiming production completion.
- Local GREEN, a running service or a worker exit code is not deployment acceptance.
- Preserve failed evidence. Do not invent success, weaken assertions, blindly retry deployments or turn a small feature into unrelated hardening.
- Keep deferred v1 findings recorded as unresolved. Keep GitLab, Kanban and roadmap status consistent.

The objective is useful, verified delivery on the actual device. Consult the current roadmap and GitLab issues for scope and live status; this file is not release authorization.
