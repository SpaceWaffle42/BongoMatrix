# Event Countdown: Google Calendar provider review

Checked against official documentation on 2026-10-11. This records implementation constraints, not a claim that synchronization exists or has been authenticated.

## Local-first delivery

Local Matrix events must remain fully usable without a Google account or network access. Priority 2.1 is active by the owner's explicit ordering exception; Priority 1.10 remains paused and reserved for Opus. On 2026-10-11 the owner chose **deliver local events first; defer Google connection**. Google synchronization remains a preserved future requirement, outside the first local-event delivery, until a supported authorization topology is selected, implemented and verified.

## Access and charging

Source: https://developers.google.com/workspace/calendar/api/guides/quota

The current documentation says standard Calendar API use is available at no additional cost. It also announces planned charges for exceeding quota thresholds later in 2026, with at least 90 days' notice. Listed time quotas are 10,000 requests per minute per project and 600 per minute per user per project; the stated daily billing threshold is 1,000,000 requests per project. Actual project quotas must be checked during setup; do not request increases, enable billing, or assume a shared project has unused budget.

Matrix must stay within free access. Implement bounded background refresh, cancellation, backoff on 403/429, a 15-minute default refresh, and safe unavailable state rather than buying capacity. Recheck this page before enabling a production connection.

## Minimum read-only scopes

Source: https://developers.google.com/workspace/calendar/api/auth

- `https://www.googleapis.com/auth/calendar.calendarlist.readonly` — list subscribed calendars for the selection UI.
- `https://www.googleapis.com/auth/calendar.events.readonly` — read events from selected calendars.

Do not request write scopes. No Google event edit/delete routes belong in Matrix.

## Authorization topology needs an explicit supported choice

Source: https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation

Google web OAuth redirects require HTTPS, disallow raw IP hosts except localhost, and require a public-suffix domain. Consequently neither the current `http://192.168.1.5:5000` dashboard nor a `.local` dashboard is a valid web-client callback. An HTTPS callback on an owned public domain or a correctly implemented companion/desktop authorization flow must be selected before Google connection UI can truthfully be offered. Do not expose the LAN dashboard publicly or introduce a third-party relay without owner authorization.

Source: https://developers.google.com/identity/protocols/oauth2/limited-input-device#allowedscopes

The actual allowed-scopes table lists OpenID/profile/email, Drive appdata/file, and YouTube scopes. It does NOT list Calendar. A Calendar scope appearing in an illustrative token-response example is not an authorization guarantee. Do not use Google device-code flow for Calendar on the strength of that example.

Source: https://developers.google.com/identity/protocols/oauth2#expiration

External apps left in Testing receive refresh tokens expiring after seven days for these Calendar scopes. Setup guidance must explain consent publishing/testing restrictions instead of presenting recurring reconnects as reliable permanent synchronization.

## Security and data requirements still to implement and verify

- Keep credentials, OAuth state/PKCE secrets, access tokens, and refresh tokens outside config.json and API responses; use the repository's protected secret-storage conventions.
- Pairing, CSRF, one-time state, bounded authorization lifetime, safe redirects, disconnect, deletion and revocation need second review under AGENTS.md.
- Normalize events into the configured Matrix timezone. Exclude cancelled/declined records; replace private content with Busy before persistence/display.
- Deterministic ordering: local start time, then Google provider order for equal starts, then local configured order.
- Whole-word, case-insensitive filters across title, description and location.
- 20 pages per selected calendar; combined 10,000 normalized events, 25 MB fetch, 20 MB cache, 64 MB transient memory. Useful partial results must be explicitly marked truncated. Memory overflow retains the previous valid cache.
- Cache only display-required data; expire offline Google candidates after 24 hours, without affecting local candidates.
- Import the next two years plus relevant annual recurrences; sort recurrence exceptions by converted start time.

Verification of a real connection requires an owner-authorized Google client and account. No credentials were read, created, exchanged, or stored during this documentation review.
