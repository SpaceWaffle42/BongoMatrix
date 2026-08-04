"""NetworkManager primitives for first-run Matrix Wi-Fi provisioning."""

from __future__ import annotations

import hashlib
import secrets
import subprocess
import time
from pathlib import Path

HOTSPOT_NAME = "matrix-setup"
HOTSPOT_ADDRESS = "10.42.0.1/24"
WIFI_INTERFACE = "wlan0"


class NetworkManager:
    def __init__(self, runner=subprocess.run, mac_reader=None):
        self.runner = runner
        self.mac_reader = mac_reader or self._read_mac

    @staticmethod
    def _read_mac():
        return Path(f"/sys/class/net/{WIFI_INTERFACE}/address").read_text().strip()

    def _run(self, args, check=True, timeout=45, input_text=None):
        options = {
            "check": check,
            "capture_output": True,
            "text": True,
            "timeout": timeout,
        }
        if input_text is not None:
            options["input"] = input_text
        return self.runner(args, **options)

    def _ignore(self, args):
        try:
            return self._run(args, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    def has_home_connection(self):
        result = self._run(
            ["nmcli", "--terse", "--escape", "no", "--fields", "DEVICE,TYPE,STATE", "device", "status"]
        )
        for line in result.stdout.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) != 3:
                continue
            device, kind, state = parts
            if state != "connected" or kind not in {"wifi", "ethernet"}:
                continue
            if kind == "ethernet":
                return True
            active = self._run(
                ["nmcli", "--get-values", "GENERAL.CONNECTION", "device", "show", device]
            ).stdout.strip()
            if active and active != HOTSPOT_NAME:
                return True
        return False

    def wait_for_home_connection(self, timeout=20, interval=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.has_home_connection():
                return True
            time.sleep(interval)
        return self.has_home_connection()

    def setup_ssid(self):
        suffix = self.mac_reader().replace(":", "")[-4:].upper()
        return f"Matrix-Setup-{suffix}"

    def scan_networks(self):
        result = self._run(
            [
                "nmcli",
                "--terse",
                "--escape",
                "no",
                "--fields",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "ifname",
                WIFI_INTERFACE,
                "--rescan",
                "yes",
            ],
            timeout=30,
        )
        by_ssid = {}
        for line in result.stdout.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) != 3 or not parts[0]:
                continue
            ssid, signal, security = parts
            try:
                signal_value = int(signal)
            except ValueError:
                continue
            candidate = {
                "ssid": ssid,
                "signal": signal_value,
                "security": security or "Open",
            }
            if ssid not in by_ssid or signal_value > by_ssid[ssid]["signal"]:
                by_ssid[ssid] = candidate
        return sorted(by_ssid.values(), key=lambda item: item["signal"], reverse=True)

    def start_hotspot(self):
        ssid = self.setup_ssid()
        self._ignore(["nmcli", "connection", "down", HOTSPOT_NAME])
        self._ignore(["nmcli", "connection", "delete", HOTSPOT_NAME])
        self._run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                WIFI_INTERFACE,
                "con-name",
                HOTSPOT_NAME,
                "ssid",
                ssid,
            ]
        )
        self._run(
            [
                "nmcli",
                "connection",
                "modify",
                HOTSPOT_NAME,
                "802-11-wireless.mode",
                "ap",
                "802-11-wireless.band",
                "bg",
                "802-11-wireless.ap-isolation",
                "yes",
                "ipv4.method",
                "shared",
                "ipv4.addresses",
                HOTSPOT_ADDRESS,
                "ipv6.method",
                "disabled",
                "connection.autoconnect",
                "no",
            ]
        )
        self._run(["nmcli", "connection", "up", HOTSPOT_NAME], timeout=60)
        return ssid

    def stop_hotspot(self):
        self._ignore(["nmcli", "connection", "down", HOTSPOT_NAME])
        self._ignore(["nmcli", "connection", "delete", HOTSPOT_NAME])

    @staticmethod
    def home_connection_name(ssid):
        digest = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:10]
        return f"matrix-home-{digest}"

    @staticmethod
    def validate_credentials(ssid, password=""):
        if not isinstance(ssid, str) or not ssid.strip() or len(ssid.strip().encode("utf-8")) > 32:
            raise ValueError("SSID must be between 1 and 32 UTF-8 bytes")
        if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
            raise ValueError("Wi-Fi password must be empty for open networks or 8-63 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in password):
            raise ValueError("Wi-Fi password cannot contain control characters")

    def connect_home(self, ssid, password=""):
        self.validate_credentials(ssid, password)
        ssid = ssid.strip()
        digest = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:8]
        name = f"matrix-candidate-{digest}-{secrets.token_hex(3)}"
        try:
            self._run(
                [
                    "nmcli",
                    "connection",
                    "add",
                    "type",
                    "wifi",
                    "ifname",
                    WIFI_INTERFACE,
                    "con-name",
                    name,
                    "ssid",
                    ssid,
                ]
            )
            self._run(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    name,
                    "connection.autoconnect",
                    "no",
                    "connection.autoconnect-priority",
                    "100",
                    "ipv4.method",
                    "auto",
                    "ipv6.method",
                    "auto",
                ]
            )
            if password:
                self._run(
                    [
                        "nmcli",
                        "connection",
                        "modify",
                        name,
                        "wifi-sec.key-mgmt",
                        "wpa-psk",
                    ]
                )
                self._run(
                    ["nmcli", "--ask", "connection", "up", name],
                    timeout=60,
                    input_text=f"{password}\n",
                )
            else:
                self._run(["nmcli", "connection", "up", name], timeout=60)
            return name
        except Exception:
            self.forget_connection(name)
            raise

    def promote_home(self, ssid, candidate_name):
        stable_name = self.home_connection_name(ssid.strip())
        self._ignore(["nmcli", "connection", "delete", stable_name])
        self._run(
            [
                "nmcli",
                "connection",
                "modify",
                candidate_name,
                "connection.id",
                stable_name,
                "connection.autoconnect",
                "yes",
            ]
        )
        return stable_name

    def forget_connection(self, name):
        if isinstance(name, str) and name.startswith("matrix-candidate-"):
            self._ignore(["nmcli", "connection", "delete", name])
