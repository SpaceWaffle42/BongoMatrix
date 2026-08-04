import subprocess
import unittest
from unittest.mock import Mock

from network_setup import HOTSPOT_NAME, NetworkManager


class FakeRunner:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        output = self.outputs.pop(0) if self.outputs else ""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")


class NetworkManagerTests(unittest.TestCase):
    def test_connected_home_wifi_is_ready_but_setup_hotspot_is_not(self):
        home = FakeRunner(["wlan0:wifi:connected\n", "Home WiFi\n"])
        self.assertTrue(NetworkManager(runner=home).has_home_connection())

        setup = FakeRunner(["wlan0:wifi:connected\n", f"{HOTSPOT_NAME}\n"])
        self.assertFalse(NetworkManager(runner=setup).has_home_connection())

    def test_hotspot_uses_networkmanager_shared_mode_and_generated_ssid(self):
        runner = FakeRunner()
        manager = NetworkManager(runner=runner, mac_reader=lambda: "aa:bb:cc:dd:ee:ff")
        ssid = manager.start_hotspot()
        self.assertEqual(ssid, "Matrix-Setup-EEFF")
        commands = [call[0] for call in runner.calls]
        self.assertIn(["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", HOTSPOT_NAME, "ssid", ssid], commands)
        modify = next(command for command in commands if command[:4] == ["nmcli", "connection", "modify", HOTSPOT_NAME])
        self.assertIn("ipv4.method", modify)
        self.assertIn("shared", modify)
        self.assertIn("802-11-wireless.ap-isolation", modify)
        self.assertIn("yes", modify)
        self.assertNotIn("wifi-sec.psk", modify)

    def test_connect_home_passes_credentials_as_argv_not_shell(self):
        runner = FakeRunner()
        manager = NetworkManager(runner=runner)
        manager.connect_home("Family WiFi", "correct horse battery staple")
        commands = [call[0] for call in runner.calls]
        add = next(command for command in commands if command[:3] == ["nmcli", "connection", "add"])
        self.assertIn("Family WiFi", add)
        secure = next(command for command in commands if "wifi-sec.key-mgmt" in command)
        self.assertNotIn("correct horse battery staple", secure)
        self.assertTrue(all("correct horse battery staple" not in command for command in commands))
        prompted = next(kwargs for _, kwargs in runner.calls if kwargs.get("input"))
        self.assertEqual(prompted["input"], "correct horse battery staple\n")
        self.assertTrue(all(not kwargs.get("shell") for _, kwargs in runner.calls))
        self.assertTrue(all(command[:3] != ["nmcli", "connection", "delete"] for command in commands))
        self.assertTrue(any("matrix-candidate-" in part for part in add))

    def test_credentials_reject_control_characters(self):
        manager = NetworkManager(runner=FakeRunner())
        for password in ("password\nnext", "password\rnext", "password\x00next"):
            with self.subTest(password=password), self.assertRaises(ValueError):
                manager.connect_home("Home", password)

    def test_scan_parses_colons_in_ssids_and_deduplicates(self):
        runner = FakeRunner(["Kitchen:IoT:72:WPA2\nKitchen:IoT:55:WPA2\nOpen:31:--\n"])
        networks = NetworkManager(runner=runner).scan_networks()
        self.assertEqual(networks[0], {"ssid": "Kitchen:IoT", "signal": 72, "security": "WPA2"})
        self.assertEqual(len(networks), 2)


if __name__ == "__main__":
    unittest.main()
