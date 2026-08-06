# Bongo Matrix

Bongo Matrix turns a Raspberry Pi and a 64×32 HUB75 RGB LED panel into a configurable display for homes, shops, offices, events, and public spaces. It runs the display directly from the Pi and provides a paired web control panel for playlists, weather, announcements, media, and custom text.

The browser preview is captured from the same frames submitted to the physical panel. What you see in the dashboard is the output the Matrix is being asked to show, rather than a separate browser-only recreation.

> [!IMPORTANT]
> The current hardware defaults target one 64×32 panel connected through an Adafruit RGB Matrix HAT/Bonnet using the `adafruit-hat-pwm` mapping. Other panel sizes and GPIO mappings need code changes in `matrix_framework.py` and have not been packaged as dashboard settings yet.

## Features

- PIN-paired control panel for trusted devices on the local network
- Ordered playlists with enable/disable and drag-style reordering controls
- Live 64×32 browser preview from the physical framebuffer
- First-run Wi-Fi hotspot and captive setup portal
- Searchable weather locations with saved coordinates
- Free Open-Meteo weather plus optional OpenWeather views
- Scheduled announcement board with static and scrolling notices
- Image, GIF, and video playback with a protected built-in media library
- Configurable custom text modules
- Atomic configuration writes and persistent paired-device sessions
- Single-owner hardware locking so two processes cannot drive the panel at once
- Hardened systemd services for startup and recovery

## Included display modules

| Module | What it displays |
| --- | --- |
| Lava Clock | Animated clock with configurable palettes, time, date, and layout |
| Weather | Keyless Open-Meteo conditions with a scrolling ticker |
| OpenWeather Overview | Current conditions, high/low values, icon, and ticker |
| OpenWeather Forecast | Configurable three-hour forecast cards |
| Announcement Board | Scheduled text, icon, banner, or full-image notices |
| Splash | Configurable startup or playlist media |
| Sleep | Night media with adjustable GIF playback speed |
| Custom text | User-defined scrolling text modules with fonts, colours, position, speed, and loops |

OpenWeather Overview and OpenWeather Forecast are disabled by default and require one API key. The key is stored separately from `config.json` and is never returned by the API.

## Hardware

The tested target is:

- Raspberry Pi with GPIO access
- One 64×32 HUB75 RGB LED panel
- Adafruit RGB Matrix HAT or Bonnet configured for the `adafruit-hat-pwm` mapping
- A suitable external power supply for the panel
- Ethernet or Wi-Fi managed by NetworkManager
- A phone, tablet, or computer on the same LAN for the dashboard

Follow the wiring and power guidance from your panel and adapter manufacturer. Do not assume the Raspberry Pi can power the LED panel directly.

## Software requirements

- Raspberry Pi OS or another systemd-based Linux distribution for the supplied services
- Python 3 with virtual environment support
- [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) and its Python bindings
- NetworkManager and `nmcli` for first-run Wi-Fi setup
- FFmpeg and FFprobe for video handling
- Git

The Python application dependencies are listed in [`requirements.txt`](requirements.txt). The `rgbmatrix` binding is hardware-specific and is intentionally not installed from that file; build and install it using the upstream Raspberry Pi instructions.

## Installation

### 1. Prepare the Pi

Install the system packages needed by the application:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg network-manager
```

Install `rpi-rgb-led-matrix` and its Python bindings by following the [upstream Python binding guide](https://github.com/hzeller/rpi-rgb-led-matrix/tree/master/bindings/python). Confirm that the Python interpreter you will use for Bongo Matrix can import `rgbmatrix`.

### 2. Clone the repository

The supplied service files currently expect the project at `/home/admin/matrix`:

```bash
sudo mkdir -p /home/admin/matrix
sudo chown "$USER":"$USER" /home/admin/matrix
git clone https://github.com/<your-github-name>/bongo-matrix.git /home/admin/matrix
cd /home/admin/matrix
```

Replace `<your-github-name>` after this project is published on GitHub. If you use another directory, update every `/home/admin/matrix` path in both files under [`deploy/`](deploy/) before installing the services.

### 3. Create the virtual environment

```bash
cd /home/admin/matrix
python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/python -c "import flask, PIL, cv2; print('application dependencies OK')"
sudo ./venv/bin/python -c "from rgbmatrix import RGBMatrix; print('rgbmatrix binding OK')"
```

### 4. Install the services

```bash
sudo cp deploy/matrix-network-setup.service deploy/matrix-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now matrix-network-setup.service matrix-web.service
sudo systemctl status matrix-network-setup.service matrix-web.service
```

Both services run as root because the Matrix driver needs hardware access. The web service uses one Gunicorn worker with four threads and an exclusive runtime lock. Do not start `main.py`, `app/wsgi.py`, or another Matrix process while the service is running.

## First-run Wi-Fi setup

At boot, `matrix-network-setup.service` waits briefly for Ethernet or a saved non-setup Wi-Fi connection. If neither is available, the Pi creates an open hotspot named `Matrix-Setup-XXXX`, where `XXXX` is taken from the Wi-Fi MAC address.

1. Connect a phone or laptop to the setup hotspot.
2. Read the six-digit PIN shown on the LED panel.
3. Open `http://10.42.0.1` if the captive portal does not open automatically.
4. Select or enter the home Wi-Fi network, then submit its credentials with the displayed PIN.

Five failed PIN attempts cause a one-minute lockout. After NetworkManager connects to the home network, the temporary hotspot is removed and `matrix-web.service` starts.

The setup hotspot is intentionally open so new devices can connect without a pre-shared password. The LED PIN authorizes the credential submission, but it does not encrypt the hotspot traffic. Run first-time setup in a trusted physical environment.

## Pair a browser

Once the Pi is on the LAN, the Matrix alternates between a six-digit pairing PIN and its local IP address with port 5000.

1. Open `http://<matrix-ip>:5000/pair` from another device on the same network.
2. Enter the PIN shown on the Matrix.
3. Give the browser a recognizable device name.

Paired sessions last up to 365 days, survive ordinary service restarts, and stop working immediately if the device is revoked. Pairing is designed for a trusted LAN. The supplied service does not provide HTTPS or internet-facing authentication, so do not expose port 5000 directly to the public internet.

## Using the dashboard

The dashboard groups controls by module and separates playlist, media, device, and secret management. Changes remain local to the editor until **Save configuration** is used.

- Use **Preview on Matrix** to display an unsaved announcement draft without saving it.
- Upload supported images, GIFs, or videos through the Media Library.
- Assign media to Splash, Sleep, or an announcement.
- Reorder playlist entries and disable modules without deleting their settings.
- Search for a weather location instead of entering coordinates manually.
- Remove paired browsers that should no longer have access.

Scrolling weather and announcement runtimes come from their loop count and per-frame delay. There is no second manual duration that can disagree with the physical playback time.

## Runtime data and backups

Bongo Matrix creates mutable state under paths that are excluded from Git:

| Path | Contents |
| --- | --- |
| `data/config.json` | Saved module, playlist, and dashboard configuration |
| `data/paired-devices.json` | Paired browser registry |
| `data/session.key` | Flask session signing secret |
| `data/secrets/` | Provider secrets such as the OpenWeather API key |
| `resources/media/` | User-uploaded media alongside the tracked `built-in/` directory |

Back up `data/` and user media before replacing a Pi, changing storage, or performing a destructive reinstall. Source-only updates should never overwrite these paths.

The application validates upload names and media content, confines media operations to `resources/media`, writes configuration atomically, and stores secrets with owner-only permissions. Built-in media is protected from deletion through the dashboard and API.

## Manual development run

Stop the production service before starting another hardware-owning process:

```bash
sudo systemctl stop matrix-web.service
cd /home/admin/matrix
sudo ./venv/bin/python main.py
```

Restart the service when finished:

```bash
sudo systemctl start matrix-web.service
```

## Tests

Run the complete unittest suite from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python -B -m unittest discover -s tests -v
```

Browser-backed checks run when their executable prerequisites are available. On a minimal Raspberry Pi without Node.js or Chromium, those checks skip rather than making the application or Pi-focused suite fail.

Useful service checks on a deployed Pi:

```bash
sudo systemctl is-active matrix-web.service
curl -I http://127.0.0.1:5000/
sudo journalctl -u matrix-web.service --since today
```

An unauthenticated request to `/` normally redirects to pairing, so an HTTP 302 response is expected.

## Project layout

```text
app/                    Flask entry point, templates, and dashboard assets
deploy/                 systemd units and captive-DNS configuration
modules/                Built-in display modules
resources/fonts/        BDF fonts used by the physical and browser renderers
resources/media/built-in/  Protected media shipped with the project
setup/                  First-run Wi-Fi setup service
tests/                  Unit, browser, portability, and regression tests
config_store.py         Defaults, validation, migration, and atomic persistence
display_controller.py   Playlist scheduler, previews, and framebuffer capture
matrix_framework.py     RGB panel, font, image, GIF, and video primitives
webapp.py               Paired dashboard and JSON API
```

## Contributing

Issues and pull requests are welcome once the repository is public. Keep changes focused, add regression coverage for behavior changes, and run the complete suite before submitting a pull request. Changes to rendering or timing should be checked against both the browser framebuffer and a physical 64×32 panel when possible.

Do not include `data/`, session keys, paired-device records, API keys, user uploads, local backups, or other runtime state in commits.

## Known limitations

- The panel geometry and GPIO mapping are currently Python defaults rather than dashboard settings.
- The supplied services assume `/home/admin/matrix` unless edited before installation.
- The dashboard is intended for a trusted LAN and does not terminate TLS.
- The first-run setup hotspot is open and relies on the physical LED PIN for authorization.
- There is no packaged installer or OS image yet; hardware-library setup is still manual.

## License

A public-use license has not been added yet. Until a `LICENSE` file is selected and committed, normal copyright restrictions apply. Choose a license before publishing this repository for general reuse or accepting outside contributions.
