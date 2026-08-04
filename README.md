# Matrix Display Framework

A 64x32 RGB LED Matrix runtime with a PIN-paired Flask control panel.

## Modules migrated from the original framework

- `clock_lava`: animated lava clock
- `weather`: configurable keyless Open-Meteo weather view with dashboard place search and exact saved coordinates
- `splash`: configurable splash GIF
- `sleep`: configurable night GIF
- `network`: lightweight connectivity helper

## First-run Wi-Fi setup

At boot, `matrix-network-setup.service` waits briefly for Ethernet or an existing non-setup Wi-Fi connection. If neither works, the device creates an open NetworkManager shared hotspot named `Matrix-Setup-XXXX`, where `XXXX` is the final four characters of the Wi-Fi MAC address. Connect a phone or laptop to that hotspot; its captive portal is available at `http://10.42.0.1` and requires the six-digit PIN shown on the Matrix before accepting home Wi-Fi credentials. Five failed PIN attempts trigger a one-minute lockout.

After NetworkManager establishes the home connection, the setup hotspot and temporary profile are removed, the portal exits, and `matrix-web.service` starts. On later boots, a working saved Wi-Fi or Ethernet connection causes setup to exit without creating a hotspot.

The setup hotspot is intentionally open for easy onboarding; the LED PIN authorizes credential submission but does not encrypt the radio link. Perform setup in a trusted physical environment.

## Control panel

After onboarding, the service listens on port 5000 on the home LAN. During browser pairing, the LED panel alternates every three seconds between the six-digit PIN and a second screen showing the LAN IP address and port 5000. Open `http://<shown-ip>:5000/pair` and enter the PIN. Five failed attempts trigger a one-minute lockout. Browser sessions expire after 12 hours and are invalidated by a service restart. The redesigned dashboard provides a 64×32 screen simulator, structured module/device workspaces, playlist controls, and a media library.

Configuration is saved atomically in `data/config.json`. Paired users can upload GIF, PNG, JPEG, and WebP files up to 16 MB, assign them to splash or sleep, preview them, and remove files that are not currently assigned. Upload names and image contents are validated, and media remains confined to `resources/media`. The Flask session secret is created without following symlinks and forced to owner-only permissions.

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
