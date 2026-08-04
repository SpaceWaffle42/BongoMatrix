import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock

from pairing import PairedDeviceStore, PairingManager, initialize_pairing_display


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class PairingManagerTests(unittest.TestCase):
    def test_initial_display_runs_normally_when_a_paired_device_exists(self):
        devices = Mock()
        devices.list.return_value = [{"id": "device"}]
        controller = Mock()
        pairing = Mock()

        initialize_pairing_display(controller, pairing, devices)

        controller.start.assert_called_once_with()
        pairing.issue_pin.assert_not_called()

    def test_initial_display_shows_pairing_when_no_device_exists(self):
        devices = Mock()
        devices.list.return_value = []
        controller = Mock()
        pairing = Mock()
        pairing.issue_pin.return_value = "[REDACTED]"
        pairing.remaining_seconds.return_value = 600

        initialize_pairing_display(controller, pairing, devices)

        controller.show_pairing_pin.assert_called_once_with("[REDACTED]", ttl_seconds=600)

    def test_paired_device_registry_persists_named_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-devices.json"
            store = PairedDeviceStore(path)
            device = store.add("Kitchen tablet", max_devices=4)

            self.assertRegex(device["id"], r"^[A-Za-z0-9_-]{20,}$")
            self.assertEqual(device["name"], "Kitchen tablet")
            self.assertEqual(PairedDeviceStore(path).list(), [device])
            self.assertTrue(PairedDeviceStore(path).contains(device["id"]))

    def test_paired_device_registry_enforces_capacity(self):
        store = PairedDeviceStore()
        store.add("First phone", max_devices=1)
        with self.assertRaisesRegex(ValueError, "maximum paired devices reached"):
            store.add("Second phone", max_devices=1)

    def test_paired_device_registry_removes_devices(self):
        store = PairedDeviceStore()
        device = store.add("Old phone", max_devices=4)
        self.assertTrue(store.remove(device["id"]))
        self.assertFalse(store.contains(device["id"]))
        self.assertFalse(store.remove(device["id"]))

    def test_pin_is_six_digits_single_use_and_expires(self):
        clock = FakeClock()
        manager = PairingManager(ttl_seconds=60, clock=clock)
        pin = manager.issue_pin()
        self.assertRegex(pin, r"^\d{6}$")
        self.assertFalse(manager.verify("000000" if pin != "000000" else "111111"))
        self.assertTrue(manager.verify(pin))
        self.assertFalse(manager.verify(pin))

        pin = manager.issue_pin()
        clock.value += 61
        self.assertFalse(manager.verify(pin))

    def test_failed_attempts_lock_pin_until_lockout_expires(self):
        clock = FakeClock()
        manager = PairingManager(ttl_seconds=600, max_attempts=3, lockout_seconds=30, clock=clock)
        pin = manager.issue_pin()
        for _ in range(3):
            self.assertFalse(manager.verify("000000" if pin != "000000" else "111111"))
        self.assertTrue(manager.is_locked())
        self.assertFalse(manager.verify(pin))
        clock.value += 31
        self.assertFalse(manager.is_locked())
        self.assertTrue(manager.verify(pin))


if __name__ == "__main__":
    unittest.main()
