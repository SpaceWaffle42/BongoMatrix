# Dashboard UI consistency

Owner of the shared visual rules for `app/templates/dashboard.html`, `app/static/screen_media.js`
and `app/static/custom_canvas.css`. Module-specific CSS may set layout, never its own colours,
radii, type sizes or control heights — use the tokens below.

## Audit (2026-10-09, live demo, 1440 px)

| Area | Before | Problem |
|---|---|---|
| Radii | 4/6/7/8/9/10/12 px mixed | Tiles of the same rank looked different per module |
| Tile surfaces | 5 alpha levels (.01–.025) | Media, palette, playlist, custom module, device tiles all distinct |
| Buttons | 34 px and 44 px heights side by side, no disabled or focus-visible style | Disabled Edit looked clickable; keyboard focus invisible |
| Checkbox labels | `label{display:grid}` stacks the box above its text | "Enable whole splash screen" and card selectors misaligned |
| Notes | 11/12/13/14 px muted text | Hint hierarchy unclear |
| Dialogs | 3 styles (config upgrade, logs, screen media) | Different radius/backdrop/shadow |
| Media library actions | Four full-width-ish buttons wrap to 4 lines | Tall, noisy cards vs. Splash/Sleep cards |

## Tokens

| Token | Value | Use |
|---|---|---|
| `--radius-sm` | 6px | controls, buttons, swatches |
| `--radius` | 8px | tiles inside a card (media, palette, playlist, devices, groups) |
| `--radius-lg` | 10px | top-level cards, dialogs |
| `--tile` | rgba(255,255,255,.018) | every tile surface |
| `--control-h` | 36px (44px under 600px) | buttons, inputs, selects |
| `--fs-note` | 12px | all muted notes/hints |
| `--fs-meta` | 11px | counts, sizes, ids |
| `--primary` | #5e6ad2 | primary buttons, pressed toggles |
| `--danger-text` / `--success-text` | #ff9da3 / #86efac | destructive / ok text |
| `--focus-ring` | 0 0 0 3px #7170ff40 | focus-visible on every interactive element |

## Component rules

- Card → section-level container (`.card`, radius-lg). Tile → item inside a card (radius, `--tile`, `--border`).
- Media tiles (library and Splash/Sleep) share thumbnail height, name and meta styles; actions are a wrapping row.
- Checkbox-first labels are inline rows; field labels (text then control) stay stacked.
- Disabled buttons are dimmed with `not-allowed`; every button/summary/input shows the focus ring on keyboard focus.
- Dialogs share radius-lg, `#000b` backdrop and the same shadow.

## Verification

GitLab CI `full_test_suite` (browser probes measure geometry and editor flows) plus a one-off Playwright
pass at 390 and 1440 px against the LAN demo.
