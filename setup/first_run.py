"""First-run hotspot bootstrap entry point.

If Ethernet or a non-setup Wi-Fi connection is active, this exits immediately.
Otherwise it owns the Matrix, creates a NetworkManager shared hotspot, and runs
a PIN-protected setup portal until home Wi-Fi connects successfully.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from matrix_framework import Matrix
from network_setup import NetworkManager
from pairing import PairingManager
from setup_portal import ProvisioningCoordinator, create_setup_app
from werkzeug.serving import make_server

LOGGER = logging.getLogger(__name__)
CAPTIVE_DNS_SOURCE = ROOT / "deploy" / "matrix-captive-dnsmasq.conf"
CAPTIVE_DNS_TARGET = Path("/etc/NetworkManager/dnsmasq-shared.d/matrix-captive.conf")


def draw_setup(matrix, ssid, pin):
    suffix = ssid.rsplit("-", 1)[-1]
    matrix.clear()
    white = matrix.colour("white")
    cyan = matrix.colour("vapor_cyan")
    black = matrix.colour("black")
    matrix.draw_text_with_border(matrix.font("4x6"), 2, 7, cyan, black, "WIFI SETUP")
    matrix.draw_text_with_border(matrix.font("4x6"), 9, 16, white, black, f"AP {suffix}")
    matrix.draw_text_with_border(matrix.font("4x6"), 4, 27, white, black, f"PIN {pin}")
    matrix.swap()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    network = NetworkManager()
    if network.wait_for_home_connection(timeout=20):
        network.stop_hotspot()
        CAPTIVE_DNS_TARGET.unlink(missing_ok=True)
        LOGGER.info("Existing Ethernet or home Wi-Fi connection is active")
        return 0

    try:
        networks = network.scan_networks()
    except Exception:
        LOGGER.exception("Wi-Fi scan failed; hidden SSIDs can still be entered")
        networks = []

    # A saved profile may finish connecting during the scan. Do not displace it.
    if network.has_home_connection():
        CAPTIVE_DNS_TARGET.unlink(missing_ok=True)
        LOGGER.info("Home connection became active during Wi-Fi scan")
        return 0

    matrix = None
    monitor_stop = threading.Event()
    monitor_thread = None
    CAPTIVE_DNS_TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CAPTIVE_DNS_SOURCE, CAPTIVE_DNS_TARGET)
    try:
        setup_ssid = network.start_hotspot()
        matrix = Matrix()
        pairing = PairingManager(ttl_seconds=600, max_attempts=5, lockout_seconds=60)
        pin = pairing.issue_pin()
        draw_setup(matrix, setup_ssid, pin)

        server_holder = {}

        def display_pin(new_pin):
            draw_setup(matrix, setup_ssid, new_pin)

        def connected():
            server = server_holder.get("server")
            if server is not None:
                server.shutdown()

        coordinator = ProvisioningCoordinator(
            network,
            pairing,
            display_pin=display_pin,
            on_connected=connected,
        )
        app = create_setup_app(
            network,
            pairing,
            coordinator,
            setup_ssid=setup_ssid,
            networks=networks,
            secret_key=secrets.token_hex(32),
        )
        server = make_server("10.42.0.1", 80, app, threaded=True)
        server_holder["server"] = server

        def monitor_connections():
            while not monitor_stop.wait(2):
                try:
                    if not coordinator.is_busy() and network.has_home_connection():
                        LOGGER.info("A non-setup connection appeared; closing onboarding")
                        network.stop_hotspot()
                        server.shutdown()
                        return
                except Exception:
                    LOGGER.exception("Connectivity monitor failed")

        monitor_thread = threading.Thread(
            target=monitor_connections,
            name="matrix-connectivity-monitor",
            daemon=True,
        )
        monitor_thread.start()

        def terminate(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        LOGGER.info("Setup hotspot %s is ready at http://10.42.0.1", setup_ssid)
        server.serve_forever()
        return 0 if network.has_home_connection() else 1
    finally:
        monitor_stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=3)
        network.stop_hotspot()
        CAPTIVE_DNS_TARGET.unlink(missing_ok=True)
        if matrix is not None:
            matrix.clear()
            matrix.swap()


if __name__ == "__main__":
    raise SystemExit(main())
