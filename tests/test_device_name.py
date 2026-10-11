"""Roadmap 1.10b: each display has a fixed matrix-xxxx.local name."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import device_name
from device_name import AvahiHostName, DeviceName, builtin_mac, default_name


def make_iface(root, name, mac, *, assign="0", device=True, wireless=False):
    iface = root / name
    iface.mkdir()
    (iface / "address").write_text(mac + "\n")
    (iface / "addr_assign_type").write_text(assign + "\n")
    if device:
        (iface / "device").mkdir()
    if wireless:
        (iface / "wireless").mkdir()


class BuiltinMacTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_prefers_wired_built_in_interface(self):
        make_iface(self.root, "lo", "00:00:00:00:00:00", device=False)
        make_iface(self.root, "wlan0", "d8:3a:dd:4f:7d:04", wireless=True)
        make_iface(self.root, "eth0", "D8:3A:DD:4F:7D:03")
        self.assertEqual(builtin_mac(self.root), "d8:3a:dd:4f:7d:03")

    def test_skips_random_virtual_and_locally_administered(self):
        make_iface(self.root, "eth0", "d8:3a:dd:4f:7d:03", assign="1")  # random
        make_iface(self.root, "docker0", "02:42:ac:11:00:02", device=False)
        make_iface(self.root, "usb0", "06:11:22:33:44:55")  # locally administered
        make_iface(self.root, "wlan0", "d8:3a:dd:4f:7d:04", wireless=True)
        self.assertEqual(builtin_mac(self.root), "d8:3a:dd:4f:7d:04")

    def test_no_usable_interface(self):
        make_iface(self.root, "lo", "00:00:00:00:00:00", device=False)
        self.assertIsNone(builtin_mac(self.root))
        self.assertIsNone(builtin_mac(self.root / "missing"))

    def test_default_name_uses_last_four_hex(self):
        self.assertEqual(default_name("d8:3a:dd:4f:7d:03"), "matrix-7d03")
        self.assertEqual(default_name("D8:3A:DD:4F:3F:9A"), "matrix-3f9a")
        self.assertIsNone(default_name(None))


def busctl_runner(state):
    """Fake `busctl --json=short call ... Avahi.Server <method> [s name]`."""

    def run(argv, **kwargs):
        method = argv[argv.index("org.freedesktop.Avahi.Server") + 1]
        state["calls"].append((method, argv[-1] if method == "SetHostName" else None))
        if state.get("fail"):
            raise subprocess.TimeoutExpired(argv, 5)
        if method == "GetHostName":
            out = json.dumps({"type": "s", "data": [state["host"]]})
        elif method == "SetHostName":
            state["host"] = state.get("rename", {}).get(argv[-1], argv[-1])
            out = ""
        else:
            raise AssertionError(method)
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


class AvahiHostNameTests(unittest.TestCase):
    def test_get_and_set(self):
        state = {"host": "Matrix", "calls": []}
        avahi = AvahiHostName(run=busctl_runner(state))
        self.assertEqual(avahi.get(), "Matrix")
        avahi.set("matrix-7d03")
        self.assertEqual(state["host"], "matrix-7d03")

    def test_failure_reads_as_none(self):
        avahi = AvahiHostName(run=busctl_runner({"fail": True, "calls": []}))
        self.assertIsNone(avahi.get())
        with self.assertLogs("device_name", "WARNING"):
            self.assertFalse(avahi.set("matrix-7d03"))

    def test_missing_busctl_reads_as_none(self):
        avahi = AvahiHostName(run=Mock(side_effect=FileNotFoundError("busctl")))
        self.assertIsNone(avahi.get())


class DeviceNameTests(unittest.TestCase):
    def make(self, state, desired="matrix-7d03"):
        return DeviceName(desired=lambda: desired, avahi=AvahiHostName(run=busctl_runner(state)))

    def test_refresh_applies_default_name(self):
        state = {"host": "Matrix", "calls": []}
        self.assertEqual(self.make(state).refresh(), "matrix-7d03.local")
        self.assertIn(("SetHostName", "matrix-7d03"), state["calls"])

    def test_already_named_does_not_set_again(self):
        state = {"host": "matrix-7d03", "calls": []}
        self.assertEqual(self.make(state).refresh(), "matrix-7d03.local")
        self.assertNotIn("SetHostName", [call[0] for call in state["calls"]])

    def test_clash_rename_is_kept_and_reported(self):
        state = {"host": "Matrix", "calls": [], "rename": {"matrix-7d03": "matrix-7d03-2"}}
        name = self.make(state)
        self.assertEqual(name.refresh(), "matrix-7d03-2.local")
        self.assertEqual(name.refresh(), "matrix-7d03-2.local")
        sets = [call for call in state["calls"] if call[0] == "SetHostName"]
        self.assertEqual(len(sets), 1, "never fights avahi's clash rename")
        self.assertEqual(name.current(), "matrix-7d03-2.local")

    def test_avahi_restart_reapplies_name(self):
        state = {"host": "matrix-7d03", "calls": []}
        name = self.make(state)
        name.refresh()
        state["host"] = "Matrix"  # avahi-daemon restarted with the system host name
        self.assertEqual(name.refresh(), "matrix-7d03.local")

    def test_no_avahi_or_no_mac_means_no_name(self):
        self.assertIsNone(self.make({"fail": True, "calls": []}).refresh())
        state = {"host": "Matrix", "calls": []}
        self.assertIsNone(self.make(state, desired=None).refresh())
        self.assertEqual(state["calls"], [])

    def test_lookalike_name_is_not_treated_as_clash_rename(self):
        state = {"host": "matrix-7d03-old", "calls": []}
        self.assertEqual(self.make(state).refresh(), "matrix-7d03.local")

    def test_for_device_uses_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_iface(Path(tmp), "eth0", "d8:3a:dd:4f:7d:03")
            state = {"host": "Matrix", "calls": []}
            name = DeviceName.for_device(Path(tmp), avahi=AvahiHostName(run=busctl_runner(state)))
            self.assertEqual(name.refresh(), "matrix-7d03.local")
        self.assertEqual(device_name.LOCAL_SUFFIX, ".local")


if __name__ == "__main__":
    unittest.main()
