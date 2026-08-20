import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from pairing import PairedDeviceStore, PairingManager, initialize_pairing_display


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class PairingManagerTests(unittest.TestCase):
    TESTING_PIN = "731946"

    def test_testing_pin_is_reusable_without_consuming_the_one_time_pin(self):
        manager = PairingManager(testing_pin=self.TESTING_PIN)
        one_time_pin = manager.issue_pin()

        self.assertTrue(manager.verify(self.TESTING_PIN))
        self.assertTrue(manager.verify(self.TESTING_PIN))
        self.assertEqual(manager.active_pin(), one_time_pin)
        self.assertTrue(manager.verify(one_time_pin))
        self.assertIsNone(manager.active_pin())
        self.assertTrue(manager.verify(self.TESTING_PIN))

    def test_testing_pin_bypasses_lockout_without_resetting_transient_state(self):
        clock = FakeClock()
        manager = PairingManager(
            max_attempts=2,
            lockout_seconds=30,
            clock=clock,
            testing_pin=self.TESTING_PIN,
        )
        one_time_pin = manager.issue_pin()
        wrong_pin = "000000" if one_time_pin != "000000" else "111111"

        self.assertFalse(manager.verify(wrong_pin))
        self.assertFalse(manager.verify(wrong_pin))
        self.assertTrue(manager.is_locked())
        self.assertTrue(manager.verify(self.TESTING_PIN))
        self.assertTrue(manager.is_locked())
        self.assertEqual(manager.active_pin(), one_time_pin)
        self.assertFalse(manager.verify(one_time_pin))
        clock.value += 31
        self.assertTrue(manager.verify(one_time_pin))

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

    def test_paired_device_registry_rejects_malformed_or_duplicate_entries(self):
        valid_id = "A" * 32
        cases = [
            [{"id": valid_id}],
            [
                {"id": valid_id, "name": "Phone", "paired_at": 1},
                {"id": valid_id, "name": "Tablet", "paired_at": 2},
            ],
            [{"id": "x", "name": "Phone", "paired_at": 1}],
            [{"id": "A" * 33, "name": "Phone", "paired_at": 1}],
            [{"id": valid_id, "name": "   ", "paired_at": 1}],
            [{"id": valid_id, "name": "Phone\nInjected", "paired_at": 1}],
            [{"id": valid_id, "name": "N" * 41, "paired_at": 1}],
            [{"id": valid_id, "name": "Phone", "paired_at": True}],
            [{"id": valid_id, "name": "Phone", "paired_at": -1}],
            [{"id": valid_id, "name": "Phone", "paired_at": 1, "extra": True}],
            [
                {"id": f"{index:032d}", "name": "Phone", "paired_at": 1}
                for index in range(11)
            ],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-devices.json"
            for devices in cases:
                with self.subTest(devices=devices):
                    path.write_text(json.dumps(devices))
                    with self.assertRaisesRegex(ValueError, "paired device"):
                        PairedDeviceStore(path)

    def test_paired_device_registry_does_not_mutate_memory_when_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-devices.json"
            store = PairedDeviceStore(path)
            existing = store.add("Existing phone", max_devices=4)

            with patch.object(store, "_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.add("New phone", max_devices=4)
                with self.assertRaises(OSError):
                    store.remove(existing["id"])

            self.assertEqual(store.list(), [existing])
            self.assertEqual(PairedDeviceStore(path).list(), [existing])

    def test_paired_device_registry_reports_visible_state_when_directory_sync_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-devices.json"
            store = PairedDeviceStore(path)
            device = store.add("Existing phone", max_devices=4)

            with patch(
                "pairing.os.fsync",
                side_effect=[None, OSError("directory sync unavailable")],
            ), self.assertRaisesRegex(OSError, "durability"):
                store.remove(device["id"])

            self.assertEqual(store.list(), [])
            self.assertEqual(PairedDeviceStore(path).list(), [])

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
