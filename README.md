# Matrix Display Framework

A 64x32 RGB LED Matrix runtime with a PIN-paired Flask control panel.

## Modules migrated from the original framework

- `clock_lava`: animated lava clock
- `weather`: configurable keyless Open-Meteo weather view with dashboard place search and exact saved coordinates
- `openweather_overview`: optional legacy-style OpenWeather current-conditions view
- `openweather_forecast`: optional legacy-style OpenWeather three-hour forecast cards
- `splash`: configurable splash GIF
- `sleep`: configurable night GIF
- `network`: lightweight connectivity helper

## First-run Wi-Fi setup

At boot, `matrix-network-setup.service` waits briefly for Ethernet or an existing non-setup Wi-Fi connection. If neither works, the device creates an open NetworkManager shared hotspot named `Matrix-Setup-XXXX`, where `XXXX` is the final four characters of the Wi-Fi MAC address. Connect a phone or laptop to that hotspot; its captive portal is available at `http://10.42.0.1` and requires the six-digit PIN shown on the Matrix before accepting home Wi-Fi credentials. Five failed PIN attempts trigger a one-minute lockout.

After NetworkManager establishes the home connection, the setup hotspot and temporary profile are removed, the portal exits, and `matrix-web.service` starts. On later boots, a working saved Wi-Fi or Ethernet connection causes setup to exit without creating a hotspot.

The setup hotspot is intentionally open for easy onboarding; the LED PIN authorizes credential submission but does not encrypt the radio link. Perform setup in a trusted physical environment.

## Control panel

After onboarding, the service listens on port 5000 on the home LAN. During browser pairing, the LED panel alternates every three seconds between the six-digit PIN and a second screen showing the LAN IP address and port 5000. Open `http://<shown-ip>:5000/pair` and enter the PIN. Five failed attempts trigger a one-minute lockout. Paired browser sessions last up to 365 days, survive ordinary service restarts, and are rejected immediately when their device is revoked. The redesigned dashboard provides a 64×32 screen simulator, structured module/device workspaces, playlist controls, and a media library.

Configuration is saved atomically in `data/config.json`. Paired users can upload supported media, assign it to splash or sleep, preview it, and remove unreferenced user files. Every regular media file physically stored under `resources/media/built-in/` is a protected default; classification cannot be changed from the dashboard or API. Administrators must deliberately move or replace files in that directory on the host filesystem to change the default collection. Upload names and media contents are validated, and media remains confined to `resources/media`. The Flask session secret is created without following symlinks and forced to owner-only permissions.

The sleep editor includes a **GIF playback speed (%)** control from 25% to 400%. The 100% default preserves each GIF's embedded frame timing; lower values slow playback and higher values speed it up. The setting changes runtime delays only, never rewrites media, and has no effect on static sleep images or videos. The browser preview polls the same captured frames sent to the physical Matrix, so it reflects the same playback rate. Slow playback remains interruptible by schedule wake-up, preview changes, service stop, and shutdown.

### Announcement Board

The separate **Announcement Board** playlist module displays an ordered set of deliberately scheduled notices. Open its grouped **Edit** controls to add, duplicate, reorder, delete, or reset notices. New notices and duplicates start disabled; edit the draft, enable it when ready, and use **Save configuration** to persist local changes.

Each notice has an inclusive start date, inclusive end date, selected weekdays, and a start-inclusive/end-exclusive time window. Overnight windows such as `22:00–02:00` belong to their starting date and may continue into the following morning. When no notice is currently eligible, the module yields immediately to the next playlist item instead of displaying a blank screen.

The four layouts are Text notice, Icon notice, Image banner, and Full-image notice. Media is selected from the existing Media Library; a missing item fails safely with a visible fallback. Static notices use their own display duration. Scrolling notices use only loop count and frame delay, with no contradictory manual duration. Each notice can use Cut, Fade, Slide, Wipe, or Pixel dissolve where compatible with its timing mode.

**Preview on Matrix** sends the exact unsaved notice draft through the normal physical-display controller without saving it or creating another Matrix owner. Preview does not bypass validation. The live browser image polls the same captured frame submitted to the physical panel.

## Weather apps

The original `weather` playlist app uses Open-Meteo and remains the enabled, free-access default. Two additional legacy-style apps are available but disabled by default:

- **OpenWeather Overview** displays the provider icon, `NOW`, current temperature, high/low values, and a scrolling location/status/feels-like ticker.
- **OpenWeather Forecast** cycles through configurable upcoming three-hour forecast cards with the same icon, temperature, high/low, and ticker presentation.

All three weather apps use one shared searchable location and exact saved coordinates. Each app retains independent units, layout, colour, and ticker controls. Forecast card count is configurable; runtime is derived from each card's ticker loops and tick speed rather than a separate manual duration. The two OpenWeather apps share one API key entered through the paired dashboard's masked **OpenWeather secret** field. The key is never returned by the API or included in `config.json`; it is atomically stored at `data/secrets/openweather-api-key` with owner-only file permissions inside an owner-only directory. `data/` is runtime state and must remain excluded from source control and source-only deployments.

## Run manually as root

```bash
cd /home/admin/matrix
sudo ./venv/bin/pip install -r requirements.txt
sudo ./venv/bin/python app/wsgi.py
```

## Install the service

```bash
sudo cp deploy/matrix-network-setup.service deploy/matrix-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now matrix-network-setup.service matrix-web.service
sudo systemctl status matrix-network-setup.service matrix-web.service
```

## Tests

```bash
cd /home/admin/matrix
sudo ./venv/bin/python -m unittest discover -s tests -v
```
