#!/usr/bin/env python3
"""Fail-closed live install policy gate for the physical CI verifier.

This gate is intentionally separate from repository tests: it inspects effective
systemd and sshd policy on the appliance.  It never starts/stops either service.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

SYSTEMCTL = "/bin/systemctl"
SSHD = "/usr/sbin/sshd"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
UNITS = ("matrix-ci-physical.service", "matrix-ci-physical.path", "matrix-web.service")
TMPFILES_POLICY = Path("/etc/tmpfiles.d/matrix-display.conf")
DISPLAY_LOCK_DIRECTORY = Path("/run/matrix-display")
DISPLAY_LOCK = DISPLAY_LOCK_DIRECTORY / "matrix-display.lock"
EXPECTED_TMPFILES_POLICY = (
    b"# Host-owned persistent display arbitration inode; services receive it read-only.\n"
    b"d /run/matrix-display 0755 root root -\n"
    b"f /run/matrix-display/matrix-display.lock 0644 root root -\n"
)
SUPPORTED_KEY_TYPES = {"ssh-ed25519"}
REQUIRED_AUTHORIZED_KEY_OPTIONS = (
    'command="/usr/local/libexec/matrix-ci-physical-dispatcher",restrict,'
    "no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding"
)


class SshConnectionContext(NamedTuple):
    source_address: str
    source_hostname: str
    local_address: str
    local_port: int
    routing_domain: str

    def canonical(self):
        source = str(ipaddress.ip_address(self.source_address))
        local = str(ipaddress.ip_address(self.local_address))
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", self.source_hostname):
            raise RuntimeError("invalid resolved SSH source hostname")
        if not 1 <= self.local_port <= 65535:
            raise RuntimeError("invalid local SSH port")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", self.routing_domain):
            raise RuntimeError("invalid SSH routing domain")
        return (
            f"user=root,host={self.source_hostname},addr={source},"
            f"laddr={local},lport={self.local_port},rdomain={self.routing_domain}"
        )


def _effective_sshd_argv(context: SshConnectionContext, *, config_path=None):
    argv = [SSHD, "-T"]
    if config_path is not None:
        argv.extend(("-f", config_path))
    argv.extend(("-C", context.canonical()))
    return tuple(argv)


def _authorized_key_fields(line: str):
    fields = []
    current = []
    quoted = False
    escaped = False
    for character in line.strip():
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\" and quoted:
            current.append(character)
            escaped = True
        elif character == '"':
            current.append(character)
            quoted = not quoted
        elif character.isspace() and not quoted:
            if current:
                fields.append("".join(current))
                current = []
        else:
            current.append(character)
    if escaped or quoted:
        raise RuntimeError("malformed authorized_keys quoting")
    if current:
        fields.append("".join(current))
    return fields


def _valid_ed25519_blob(blob: bytes):
    if len(blob) < 4:
        return False
    type_length = int.from_bytes(blob[:4], "big")
    type_end = 4 + type_length
    if blob[4:type_end] != b"ssh-ed25519" or len(blob) < type_end + 4:
        return False
    key_length = int.from_bytes(blob[type_end:type_end + 4], "big")
    return key_length == 32 and len(blob) == type_end + 4 + key_length


def _has_exact_forced_policy(options: str):
    if options == REQUIRED_AUTHORIZED_KEY_OPTIONS:
        return True
    prefix = 'from="'
    marker = '",' + REQUIRED_AUTHORIZED_KEY_OPTIONS
    if not options.startswith(prefix) or not options.endswith(marker):
        return False
    source = options[len(prefix):-len(marker)]
    return bool(source) and all(character.isalnum() or character in ".:/,-" for character in source)


def _matching_forced_key_lines(source: str, fingerprint: str):
    matching = []
    for line_number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = _authorized_key_fields(line)
        if not fields:
            continue
        if fields[0] in SUPPORTED_KEY_TYPES:
            key_index = 0
            options = None
        elif len(fields) >= 2 and fields[1] in SUPPORTED_KEY_TYPES:
            key_index = 1
            options = fields[0]
        else:
            continue
        if len(fields) <= key_index + 1:
            raise RuntimeError(f"malformed supported authorized key on line {line_number}")
        try:
            blob = base64.b64decode(fields[key_index + 1], validate=True)
        except ValueError as error:
            raise RuntimeError(f"malformed supported authorized key on line {line_number}") from error
        if fields[key_index] == "ssh-ed25519" and not _valid_ed25519_blob(blob):
            raise RuntimeError(f"key type/blob mismatch on line {line_number}")
        line_fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
        if line_fingerprint != fingerprint:
            continue
        if options is None or not _has_exact_forced_policy(options):
            raise RuntimeError("dedicated forced-command authorized_keys policy is missing")
        matching.append(line_number)
    return matching


def run(*argv):
    try:
        return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"install-policy command failed: {' '.join(argv)}") from error


def require_file(path: Path, mode: int):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != mode
        or info.st_nlink != 1
    ):
        raise RuntimeError(f"unsafe installed file: {path}")


def verify_display_lock_install():
    require_file(TMPFILES_POLICY, 0o644)
    if TMPFILES_POLICY.read_bytes() != EXPECTED_TMPFILES_POLICY:
        raise RuntimeError("installed display-lock tmpfiles policy differs from reviewed source")
    directory_info = DISPLAY_LOCK_DIRECTORY.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or directory_info.st_uid != 0
        or directory_info.st_gid != 0
        or stat.S_IMODE(directory_info.st_mode) != 0o755
    ):
        raise RuntimeError("unsafe host-created display lock directory")
    require_file(DISPLAY_LOCK, 0o644)


def properties(unit, *names):
    # effective policy: systemctl show, not repository text
    output = run(SYSTEMCTL, "show", unit, *(item for name in names for item in ("-p", name)))
    result = {}
    for line in output.splitlines():
        if "=" not in line:
            raise RuntimeError(f"malformed systemctl show output: {unit}")
        key, value = line.split("=", 1)
        if key in result:
            raise RuntimeError(f"duplicate effective property: {key}")
        result[key] = value
    if set(result) != set(names):
        raise RuntimeError(f"missing effective properties: {unit}")
    return result


def _parse_effective_sshd_policy(output: str):
    """Parse canonical ``sshd -T`` output and bind every root key source."""
    directives = {}
    for line in output.splitlines():
        if not line or line != line.strip() or " " not in line:
            raise RuntimeError("malformed effective sshd output")
        name, value = line.split(" ", 1)
        if not name.islower() or not value or value != value.strip():
            raise RuntimeError("malformed effective sshd output")
        if name in directives and name in {
            "authorizedkeysfile", "authorizedkeyscommand", "authorizedkeyscommanduser", "permitrootlogin"
        }:
            raise RuntimeError(f"duplicate effective sshd directive: {name}")
        if name not in directives:
            directives[name] = value

    required = {
        "authorizedkeysfile",
        "authorizedkeyscommand",
        "authorizedkeyscommanduser",
        "permitrootlogin",
    }
    if not required.issubset(directives):
        raise RuntimeError("effective sshd key-source policy is incomplete")
    key_file = directives["authorizedkeysfile"]
    if key_file not in (".ssh/authorized_keys", "/root/.ssh/authorized_keys"):
        raise RuntimeError("effective AuthorizedKeysFile is not the reviewed file")
    if directives["authorizedkeyscommand"] != "none":
        raise RuntimeError("effective AuthorizedKeysCommand provides an alternate key source")
    command_user = directives["authorizedkeyscommanduser"]
    if len(command_user.split()) != 1 or any(character in command_user for character in "%/"):
        raise RuntimeError("effective AuthorizedKeysCommandUser is malformed")
    if directives["permitrootlogin"] != "forced-commands-only":
        raise RuntimeError("effective PermitRootLogin is incompatible with forced-key-only access")
    return {
        "authorized_keys_file": "/root/.ssh/authorized_keys",
        "permit_root_login": directives["permitrootlogin"],
    }


def verify_units():
    # systemctl cat exposes effective source and drop-ins. Unexpected drop-ins
    # are rejected because they can weaken the reviewed installed unit.
    for unit in UNITS:
        content = run(SYSTEMCTL, "cat", unit)  # systemctl cat
        if not content or f".d/" in content:
            raise RuntimeError(f"unexpected effective unit source: {unit}")

    physical = properties(
        "matrix-ci-physical.service",
        "Type", "RuntimeMaxUSec", "CapabilityBoundingSet", "ExecStart", "ExecStop", "ExecStopPost",
        "Requires", "After", "ReadOnlyPaths", "UMask",
    )
    if physical["Type"] != "exec":
        raise RuntimeError("physical service Type is not exec")
    if physical["RuntimeMaxUSec"] in ("", "0", "infinity"):
        raise RuntimeError("physical RuntimeMaxUSec is not effective")
    if not physical["CapabilityBoundingSet"]:
        raise RuntimeError("physical CapabilityBoundingSet is empty")
    if "flock" in physical["ExecStart"] or "matrix-ci-physical-verifier" not in physical["ExecStart"]:
        raise RuntimeError("physical ExecStart violates single path admission")
    if "--restore-only" not in physical["ExecStop"] or "--restore-only" not in physical["ExecStopPost"]:
        raise RuntimeError("physical recovery hooks are missing")
    if "systemd-tmpfiles-setup.service" not in physical["Requires"].split() or "systemd-tmpfiles-setup.service" not in physical["After"].split():
        raise RuntimeError("physical service can start before display-lock tmpfiles setup")
    if "/run/matrix-display" not in physical["ReadOnlyPaths"].split():
        raise RuntimeError("physical service display-lock namespace is not read-only")
    if physical["UMask"] != "0077":
        raise RuntimeError("physical service UMask is not 0077")

    path = properties("matrix-ci-physical.path", "Unit", "Paths", "ActiveState")
    if "matrix-ci-physical.service" not in path["Unit"] or "/run/matrix-ci-physical/claim" not in path["Paths"]:
        raise RuntimeError("matrix-ci-physical.path is not the claim admission owner")
    if path["ActiveState"] != "active":
        raise RuntimeError("matrix-ci-physical.path is not active")

    web = properties(
        "matrix-web.service", "InaccessiblePaths", "CapabilityBoundingSet",
        "Requires", "After", "ReadOnlyPaths", "UMask",
    )
    if "/run/matrix-ci-physical" not in web["InaccessiblePaths"]:
        raise RuntimeError("matrix-web lacks physical namespace isolation")
    if not web["CapabilityBoundingSet"]:
        raise RuntimeError("matrix-web CapabilityBoundingSet is empty")
    if "systemd-tmpfiles-setup.service" not in web["Requires"].split() or "systemd-tmpfiles-setup.service" not in web["After"].split():
        raise RuntimeError("matrix-web can start before display-lock tmpfiles setup")
    if "/run/matrix-display" not in web["ReadOnlyPaths"].split():
        raise RuntimeError("matrix-web display-lock namespace is not read-only")
    if web["UMask"] != "0077":
        raise RuntimeError("matrix-web UMask is not 0077")
    verify_display_lock_install()


def verify_ssh(fingerprint: str, context: SshConnectionContext):
    ssh_dir = Path("/root/.ssh")
    auth = ssh_dir / "authorized_keys"
    directory_info = ssh_dir.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or directory_info.st_gid != 0
        or stat.S_IMODE(directory_info.st_mode) != 0o700
    ):
        raise RuntimeError("unsafe /root/.ssh")
    require_file(auth, 0o600)  # authorized_keys
    source = auth.read_text(encoding="ascii")
    fingerprints = run(SSH_KEYGEN, "-lf", str(auth))  # ssh-keygen
    if fingerprints.count(fingerprint) != 1:
        raise RuntimeError("dedicated key fingerprint is absent or reused")
    matching_forced_lines = _matching_forced_key_lines(source, fingerprint)
    if len(matching_forced_lines) != 1:
        raise RuntimeError("dedicated forced-command authorized_keys policy is missing")
    run(SSHD, "-t")
    effective = run(*_effective_sshd_argv(context))  # sshd -T with complete real connection context
    _parse_effective_sshd_policy(effective)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dedicated-key-fingerprint", required=True)
    parser.add_argument("--runner-address", required=True, help="actual CI source address")
    parser.add_argument("--runner-hostname", required=True, help="sshd-resolved CI source hostname")
    parser.add_argument("--local-address", required=True, help="actual appliance address receiving SSH")
    parser.add_argument("--local-port", required=True, type=int, help="actual appliance SSH port")
    parser.add_argument("--routing-domain", required=True, help="actual sshd connection routing domain")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root is required")
    verify_units()
    context = SshConnectionContext(
        source_address=args.runner_address,
        source_hostname=args.runner_hostname,
        local_address=args.local_address,
        local_port=args.local_port,
        routing_domain=args.routing_domain,
    )
    context.canonical()
    verify_ssh(args.dedicated_key_fingerprint, context)
    print("physical CI installed policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
