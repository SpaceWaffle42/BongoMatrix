"""PIN-protected first-run Wi-Fi setup portal."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent


class ProvisioningCoordinator:
    def __init__(
        self,
        network,
        pairing,
        display_pin,
        on_connected,
        handoff_delay=2,
    ):
        self.network = network
        self.pairing = pairing
        self.display_pin = display_pin
        self.on_connected = on_connected
        self.handoff_delay = handoff_delay
        self._lock = threading.Lock()
        self._busy = False
        self._status = {"state": "waiting", "message": "Waiting for Wi-Fi details."}

    def status(self):
        with self._lock:
            return dict(self._status)

    def is_busy(self):
        with self._lock:
            return self._busy

    def ensure_pin(self):
        pin = self.pairing.active_pin()
        if pin is None:
            pin = self.pairing.issue_pin()
            self.display_pin(pin)
        return pin

    def submit(self, ssid, password):
        self.network.validate_credentials(ssid, password)
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._status = {"state": "connecting", "message": f"Connecting to {ssid}."}
        worker = threading.Thread(
            target=self._connect,
            args=(ssid, password),
            name="matrix-wifi-handoff",
            daemon=True,
        )
        worker.start()
        return True

    def _connect(self, ssid, password):
        candidate = None
        try:
            time.sleep(self.handoff_delay)
            candidate = self.network.connect_home(ssid, password)
            if not self.network.wait_for_home_connection(timeout=30):
                raise RuntimeError("NetworkManager did not establish the home connection")
            self.network.promote_home(ssid, candidate)
            self.network.stop_hotspot()
            with self._lock:
                self._status = {"state": "connected", "message": f"Connected to {ssid}."}
            self.on_connected()
        except Exception:
            LOGGER.exception("Home Wi-Fi provisioning failed")
            try:
                if candidate:
                    self.network.forget_connection(candidate)
                self.network.start_hotspot()
            except Exception:
                LOGGER.exception("Could not restore the setup hotspot")
            pin = self.pairing.issue_pin()
            self.display_pin(pin)
            with self._lock:
                self._busy = False
                self._status = {
                    "state": "error",
                    "message": "Could not connect. Check the network name and password, then try again.",
                }


def create_setup_app(
    network,
    pairing,
    coordinator,
    setup_ssid,
    networks,
    secret_key,
):
    app = Flask(
        __name__,
        template_folder=str(ROOT / "setup" / "templates"),
    )
    app.config.update(
        SECRET_KEY=secret_key,
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    submission_lock = threading.Lock()

    @app.get("/")
    def setup_page():
        coordinator.ensure_pin()
        return render_template(
            "wifi_setup.html",
            setup_ssid=setup_ssid,
            networks=networks,
            status=coordinator.status(),
        )

    @app.post("/connect")
    def connect():
        ssid = request.form.get("ssid", "")
        password = request.form.get("password", "")
        with submission_lock:
            try:
                network.validate_credentials(ssid, password)
            except ValueError as exc:
                return (
                    render_template(
                        "wifi_setup.html",
                        setup_ssid=setup_ssid,
                        networks=networks,
                        status=coordinator.status(),
                        error=str(exc),
                    ),
                    400,
                )
            if coordinator.is_busy():
                return "A connection attempt is already running.", 409
            pin = request.form.get("pin", "").strip()
            if not pairing.verify(pin):
                coordinator.ensure_pin()
                error = (
                    "Too many PIN attempts. Wait one minute and try again."
                    if pairing.is_locked()
                    else "The setup PIN is invalid or expired. A fresh PIN is shown on the Matrix when needed."
                )
                return (
                    render_template(
                        "wifi_setup.html",
                        setup_ssid=setup_ssid,
                        networks=networks,
                        status=coordinator.status(),
                        error=error,
                    ),
                    401,
                )
            accepted = coordinator.submit(ssid, password)
            if not accepted:
                return "A connection attempt is already running.", 409
        return render_template("wifi_connecting.html", ssid=ssid), 202

    @app.get("/status")
    def status():
        return jsonify(coordinator.status())

    # Common Apple, Android, Windows, and Firefox captive-portal probes, plus
    # any other path, are directed to the setup page.
    @app.route("/<path:path>", methods=["GET", "HEAD"])
    def captive_redirect(path):
        return redirect("/")

    return app
