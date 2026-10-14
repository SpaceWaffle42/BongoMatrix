"""Roadmap 1.10f: deploy tooling finds the device by pinned SSH host key, not IP."""
import importlib.util
import ipaddress
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEVICE_KEY = ('ssh-ed25519', 'AAAAC3NzaC1lZDI1NTE5AAAAIDEVICEKEY')
OTHER_KEY = ('ssh-ed25519', 'AAAAC3NzaC1lZDI1NTE5AAAAIOTHERKEY0')


def load(name, path):
    sys.path.insert(0, str(ROOT / 'deploy'))
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / 'deploy'))


class FakeNetwork:
    """ssh-keyscan / avahi-browse stand-in: {address: host key}."""

    def __init__(self, hosts, mdns=()):
        self.hosts = hosts
        self.mdns = list(mdns)
        self.scanned = []

    def __call__(self, argv, **kwargs):
        if argv[0] == 'ssh-keyscan':
            asked = [line for line in kwargs['input'].splitlines() if line]
            self.scanned.append(asked)
            out = ''.join(f'{a} {self.hosts[a][0]} {self.hosts[a][1]}\n' for a in asked if a in self.hosts)
            return subprocess.CompletedProcess(argv, 0, out, '')
        if argv[0] == 'avahi-browse':
            out = ''.join(f'=;wlan0;IPv4;matrix-7d03;_matrix._tcp;local;m.local;{a};5000;"id=x"\n'
                          for a in self.mdns)
            return subprocess.CompletedProcess(argv, 0, out, '')
        raise AssertionError(argv)


class LocateDeviceTests(unittest.TestCase):
    def setUp(self):
        self.mod = load('locate_device_under_test', 'deploy/locate_device.py')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.known = Path(self.temp.name) / 'device-known-hosts'
        self.known.write_text(f'matrix-device {DEVICE_KEY[0]} {DEVICE_KEY[1]}\n', encoding='utf-8')
        self.memory = Path(self.temp.name) / 'device-address.json'
        self.subnets = lambda probe=None: [ipaddress.ip_network('10.9.8.0/29')]

    def locate(self, network, **kwargs):
        return self.mod.locate(self.known, run=network, subnets=self.subnets, memory=self.memory, **kwargs)

    def test_hint_with_pinned_key_is_used_first(self):
        network = FakeNetwork({'10.9.8.5': DEVICE_KEY})
        found = self.locate(network, hint='admin@10.9.8.5')
        self.assertEqual(found['target'], 'admin@10.9.8.5')
        self.assertEqual(found['alias'], 'matrix-device')
        self.assertEqual(found['found_by'], 'hint')
        self.assertEqual(network.scanned, [['10.9.8.5']])

    def test_each_address_is_scanned_on_its_own(self):
        network = FakeNetwork({'10.9.8.3': DEVICE_KEY})
        self.locate(network)
        self.assertTrue(network.scanned)
        self.assertTrue(all(len(batch) == 1 for batch in network.scanned), network.scanned)

    def test_moved_device_is_found_by_key_on_the_network(self):
        network = FakeNetwork({'10.9.8.5': OTHER_KEY, '10.9.8.3': DEVICE_KEY})
        found = self.locate(network, hint='admin@10.9.8.5')
        self.assertEqual(found['address'], '10.9.8.3')
        self.assertEqual(found['found_by'], 'local network')
        self.assertEqual(json.loads(self.memory.read_text())['address'], '10.9.8.3')
        again = FakeNetwork({'10.9.8.3': DEVICE_KEY})
        self.assertEqual(self.locate(again)['found_by'], 'last known')

    def test_mdns_candidates_are_checked_before_scanning(self):
        network = FakeNetwork({'10.9.8.6': DEVICE_KEY}, mdns=['10.9.8.6'])
        self.assertEqual(self.locate(network)['found_by'], 'mDNS')

    def test_impostor_at_the_old_address_is_refused(self):
        network = FakeNetwork({'10.9.8.5': OTHER_KEY, '10.9.8.2': OTHER_KEY})
        with self.assertRaisesRegex(self.mod.LocateError, 'refusing to guess'):
            self.locate(network, hint='admin@10.9.8.5')
        self.assertFalse(self.memory.exists())

    def test_known_hosts_must_pin_exactly_one_device(self):
        self.known.write_text(f'a {DEVICE_KEY[0]} {DEVICE_KEY[1]}\nb {OTHER_KEY[0]} {OTHER_KEY[1]}\n')
        with self.assertRaisesRegex(self.mod.LocateError, 'more than one host'):
            self.locate(FakeNetwork({}))
        self.known.write_text('# nothing\n')
        with self.assertRaisesRegex(self.mod.LocateError, 'no pinned host key'):
            self.locate(FakeNetwork({}))

    def test_ssh_options_accept_only_the_pinned_key(self):
        options = self.mod.ssh_options(self.known, 'matrix-device')
        self.assertIn('StrictHostKeyChecking=yes', options)
        self.assertIn(f'UserKnownHostsFile={self.known}', options)
        self.assertIn('HostKeyAlias=matrix-device', options)
        self.assertIn('GlobalKnownHostsFile=/dev/null', options)

    def test_explicit_subnets_are_bounded_private_ipv4(self):
        self.assertEqual(self.mod.parse_subnets('192.168.1.0/24, 10.0.0.0/30'),
                         [ipaddress.ip_network('192.168.1.0/24'), ipaddress.ip_network('10.0.0.0/30')])
        for bad in ('8.8.8.0/24', '10.0.0.0/16', 'fd00::/120'):
            with self.assertRaises(self.mod.LocateError):
                self.mod.parse_subnets(bad)
        network = FakeNetwork({'10.1.1.2': DEVICE_KEY})
        found = self.locate(network, scan=self.mod.parse_subnets('10.1.1.0/30'))
        self.assertEqual(found['address'], '10.1.1.2')


class ShipLocatesDeviceTests(unittest.TestCase):
    def setUp(self):
        self.ship = load('ship_locate_under_test', 'deploy/ship.py')
        self.addCleanup(mock.patch.stopall)

    def test_ship_never_connects_without_locating(self):
        with self.assertRaisesRegex(self.ship.ShipError, 'not located'):
            self.ship.ssh_argv()
        with self.assertRaisesRegex(self.ship.ShipError, 'not located'):
            self.ship.fetch_public()

    def test_ship_has_no_hardcoded_device_address(self):
        source = (ROOT / 'deploy/ship.py').read_text(encoding='utf-8')
        self.assertNotRegex(source, r'192\.168\.\d+\.\d+')

    def test_located_device_drives_ssh_and_public_url(self):
        found = {'target': 'admin@10.9.8.3', 'address': '10.9.8.3', 'alias': 'matrix-device',
                 'found_by': 'local network'}
        with mock.patch.object(self.ship.locate_device, 'locate', return_value=found):
            self.ship.locate_target()
        argv = self.ship.ssh_argv()
        self.assertEqual(argv[-1], 'admin@10.9.8.3')
        self.assertIn('HostKeyAlias=matrix-device', argv)
        self.assertEqual(self.ship.PUBLIC_URL, 'http://10.9.8.3:5000/pair')

    def test_unlocatable_device_refuses_before_any_change(self):
        mock.patch.object(self.ship, 'write_result', return_value=Path('/dev/null')).start()
        stage = mock.patch.object(self.ship, 'stage_and_run', side_effect=AssertionError('deployed')).start()
        mock.patch.object(self.ship.locate_device, 'locate',
                          side_effect=self.ship.locate_device.LocateError('no device')).start()
        with mock.patch('sys.stdout', new=io.StringIO()), mock.patch('sys.stderr', new=io.StringIO()):
            self.assertNotEqual(self.ship.main(['--apply']), 0)
        stage.assert_not_called()


if __name__ == '__main__':
    unittest.main()
