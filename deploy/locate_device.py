#!/usr/bin/env python3
"""Roadmap 1.10f: find the Matrix device by its pinned SSH host key, not by IP.

The device's address can change (DHCP, another network).  Deploy tooling keeps a
dedicated known_hosts file holding only that device's host keys.  This module
tries likely addresses in order and returns the first whose SSH host key is one
of the pinned keys:

  1. an explicit hint (``MATRIX_PROD_HOST`` / ``MATRIX_DEVICE_TARGET``),
  2. the address that matched last time,
  3. displays announcing ``_matrix._tcp`` over mDNS (1.10d), when avahi-browse exists,
  4. every address of the given subnets (``--subnet`` / ``MATRIX_DEVICE_SUBNETS``),
     else this machine's local IPv4 /24 (a container on a bridge needs ``--subnet``).

Matching a scanned key only selects the address.  Every later ``ssh`` uses
``StrictHostKeyChecking=yes`` with the same file and ``HostKeyAlias``, so the
device must still prove it holds the private key.  Anything else is refused.

    python3 deploy/locate_device.py --known-hosts FILE [--hint user@addr] [--user admin]

prints ``<user>@<address> <host-key-alias>`` or exits 1.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

KEYSCAN_TIMEOUT_SECONDS = 5
KEYSCAN_PARALLEL = 64
BROWSE_TIMEOUT_SECONDS = 5
SERVICE_TYPE = '_matrix._tcp'
_KEY_LINE = re.compile(r'^(\S+)\s+(ssh-ed25519|ecdsa-sha2-\S+|ssh-rsa)\s+([A-Za-z0-9+/=]+)')


class LocateError(RuntimeError):
    pass


def pinned_keys(known_hosts: Path) -> tuple[str, set[tuple[str, str]]]:
    """(host-key alias, {(type, key)}) from a dedicated known_hosts file."""
    try:
        text = known_hosts.read_text(encoding='utf-8')
    except OSError as error:
        raise LocateError(f'cannot read pinned host keys {known_hosts}: {error}') from error
    alias, keys = None, set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # ssh honours markers, hashed and wildcard hosts; this file must be exactly
        # what locate checks, so anything wider is refused, not skipped.
        if line.startswith(('@', '|')):
            raise LocateError(f'{known_hosts}: markers and hashed hosts are not a single-device pin')
        match = _KEY_LINE.match(line)
        if not match:
            raise LocateError(f'{known_hosts}: unreadable line; expected "<alias> <type> <key>"')
        names = match.group(1).split(',')
        if any(ch in name for name in names for ch in '*?!['):
            raise LocateError(f'{known_hosts}: wildcard hosts are not a single-device pin')
        if alias is None:
            alias = names[0]
        elif alias not in names:
            raise LocateError(f'{known_hosts} pins more than one host; keep only the device')
        keys.add((match.group(2), match.group(3)))
    if alias is None or not keys:
        raise LocateError(f'{known_hosts} holds no pinned host key')
    return alias, keys


def _ipv4(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value.strip().strip('[]'))
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_multicast or address.is_unspecified:
        return None
    return str(address)


_USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}')


def split_target(target: str | None, user: str) -> tuple[str, str | None]:
    who, where = user, None
    if target:
        found, _, where = target.rpartition('@')
        who, where = (found or user), (where or None)
    if not _USER.fullmatch(who):
        raise LocateError(f'ssh user {who!r} is not a plain login name')
    return who, where


def resolve(host: str) -> list[str]:
    literal = _ipv4(host)
    if literal:
        return [literal]
    try:
        infos = socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def mdns_addresses(run=subprocess.run) -> list[str]:
    try:
        done = run(['avahi-browse', '-r', '-p', '-t', '-k', SERVICE_TYPE],
                   capture_output=True, text=True, timeout=BROWSE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (done.stdout or '').splitlines():
        fields = line.split(';')
        if len(fields) > 7 and fields[0] == '=' and fields[2] == 'IPv4' and _ipv4(fields[7]):
            found.append(_ipv4(fields[7]))
    return list(dict.fromkeys(found))


def local_subnets(probe: str | None = None) -> list[ipaddress.IPv4Network]:
    """This machine's outward IPv4 address as a /24 (no iproute2 needed)."""
    networks = []
    for target in filter(None, (probe, '192.0.2.1')):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.connect((target, 9))
                own = ipaddress.ip_address(sock.getsockname()[0])
            except OSError:
                continue
        if own.is_private and not own.is_loopback:
            network = ipaddress.ip_network(f'{own}/24', strict=False)
            if network not in networks:
                networks.append(network)
    return networks


def _keyscan_one(address: str, run) -> str:
    try:
        done = run(['ssh-keyscan', '-T', str(KEYSCAN_TIMEOUT_SECONDS), '-f', '-'],
                   input=address + '\n', capture_output=True, text=True,
                   timeout=KEYSCAN_TIMEOUT_SECONDS * 3)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout or ''


def keyscan(addresses: list[str], run=subprocess.run) -> dict[str, set[tuple[str, str]]]:
    """Host keys offered on port 22.

    One ssh-keyscan per address, run in parallel: a single batched scan drops
    slow hosts (a Pi on Wi-Fi) once many other addresses are pending.
    """
    if not addresses:
        return {}
    with ThreadPoolExecutor(max_workers=KEYSCAN_PARALLEL) as pool:
        outputs = list(pool.map(lambda address: _keyscan_one(address, run), addresses))
    offered: dict[str, set[tuple[str, str]]] = {}
    for address, output in zip(addresses, outputs):
        for line in output.splitlines():
            match = _KEY_LINE.match(line.strip())
            if match and match.group(1) == address:
                offered.setdefault(address, set()).add((match.group(2), match.group(3)))
    return offered


def parse_subnets(text: str | None) -> list[ipaddress.IPv4Network]:
    networks = []
    for item in re.split(r'[\s,]+', text or ''):
        if not item:
            continue
        network = ipaddress.ip_network(item, strict=False)
        if network.version != 4 or not network.is_private or network.num_addresses > 1024:
            raise LocateError(f'subnet {item} must be a private IPv4 network of at most /22')
        networks.append(network)
    return networks


def locate(known_hosts: Path, *, user: str = 'admin', hint: str | None = None,
           memory: Path | None = None, run=subprocess.run, subnets=local_subnets,
           scan: list[ipaddress.IPv4Network] | None = None) -> dict:
    alias, keys = pinned_keys(known_hosts)
    user, hinted = split_target(hint, user)
    tried: list[str] = []

    def first_match(addresses):
        fresh = [a for a in dict.fromkeys(addresses) if a and a not in tried]
        tried.extend(fresh)
        offered = keyscan(fresh, run=run)
        for address in fresh:
            if offered.get(address, set()) & keys:
                return address
        return None

    remembered = None
    if memory is not None:
        try:
            remembered = _ipv4(json.loads(memory.read_text(encoding='utf-8')).get('address', ''))
        except (OSError, ValueError, AttributeError):
            remembered = None
    stages = [
        ('hint', lambda: resolve(hinted) if hinted else []),
        ('last known', lambda: [remembered] if remembered else []),
        ('mDNS', lambda: mdns_addresses(run=run)),
        ('local network', lambda: [str(h) for n in (scan or subnets(remembered or (hinted and _ipv4(hinted))))
                                   for h in n.hosts()]),
    ]
    for how, addresses in stages:
        address = first_match(addresses())
        if address:
            if memory is not None:
                try:
                    memory.parent.mkdir(parents=True, exist_ok=True)
                    memory.write_text(json.dumps({'address': address}) + '\n', encoding='utf-8')
                except OSError:
                    pass
            return {'target': f'{user}@{address}', 'address': address, 'alias': alias, 'found_by': how}
    raise LocateError(f'no device on {len(tried)} address(es) presented the pinned host key '
                      f'({alias}); refusing to guess')


def ssh_options(known_hosts: Path, alias: str) -> list[str]:
    """Options that make ssh accept only the pinned key for the located address."""
    return ['-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known_hosts}',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', f'HostKeyAlias={alias}']


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--known-hosts', type=Path, required=True)
    parser.add_argument('--hint', default=None)
    parser.add_argument('--user', default='admin')
    parser.add_argument('--memory', type=Path, default=None)
    parser.add_argument('--subnet', default=os.environ.get('MATRIX_DEVICE_SUBNETS'),
                        help='private IPv4 networks to scan, comma separated')
    args = parser.parse_args(argv)
    try:
        found = locate(args.known_hosts, user=args.user, hint=args.hint, memory=args.memory,
                       scan=parse_subnets(args.subnet))
    except LocateError as error:
        print(f'locate_device: {error}', file=sys.stderr)
        return 1
    print(f"{found['target']} {found['alias']}")
    print(f"locate_device: {found['address']} matched the pinned key ({found['found_by']})", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
