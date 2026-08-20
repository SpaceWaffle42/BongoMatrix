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
import pwd
import re
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

SYSTEMCTL = "/bin/systemctl"
SSHD = "/usr/sbin/sshd"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
UNITS = (
    "matrix-ci-physical.service",
    "matrix-ci-physical.path",
    "matrix-device-restart.service",
    "matrix-device-restart.path",
    "matrix-web.service",
)
TMPFILES_POLICY = Path("/etc/tmpfiles.d/matrix-display.conf")
DISPLAY_LOCK_DIRECTORY = Path("/run/matrix-display")
DISPLAY_LOCK = DISPLAY_LOCK_DIRECTORY / "matrix-display.lock"
EXPECTED_TMPFILES_POLICY = (
    b"# Host-owned persistent display arbitration inode; services receive it read-only.\n"
    b"d /run/matrix-display 0755 root root -\n"
    b"f /run/matrix-display/matrix-display.lock 0644 root root -\n"
)
PHYSICAL_TMPFILES_POLICY = Path("/etc/tmpfiles.d/matrix-ci-physical.conf")
PHYSICAL_RUNTIME_DIRECTORY = Path("/run/matrix-ci-physical")
EXPECTED_PHYSICAL_TMPFILES_POLICY = (
    b"# Host-owned prerequisite for matrix-web InaccessiblePaths.\n"
    b"d /run/matrix-ci-physical 0700 root root -\n"
)
RESTART_BROKER = Path("/usr/local/libexec/matrix-device-restart-broker")
RESTART_SERVICE = Path("/etc/systemd/system/matrix-device-restart.service")
RESTART_PATH = Path("/etc/systemd/system/matrix-device-restart.path")
RESTART_TMPFILES_POLICY = Path("/etc/tmpfiles.d/matrix-device-restart.conf")
RESTART_DIRECTORY = Path("/run/matrix-device-restart")
EXPECTED_RESTART_TMPFILES_POLICY = (
    b"# Host-owned fixed whole-device restart transaction namespace.\n"
    b"d /run/matrix-device-restart 1730 root matrix-web -\n"
    b"f /run/matrix-device-restart/transaction.lock 0644 root root -\n"
)
EXPECTED_ARTIFACT_DIGESTS = {}
REVIEWED_ARTIFACTS = {
    "matrix-ci-physical-verifier": (Path("/usr/local/libexec/matrix-ci-physical-verifier"), 0o755),
    "matrix-ci-physical-dispatcher": (Path("/usr/local/libexec/matrix-ci-physical-dispatcher"), 0o755),
    "matrix-ci-install-rgb-bindings": (Path("/usr/local/libexec/matrix-ci-install-rgb-bindings"), 0o755),
    "verify-matrix-ci-physical-install": (Path("/usr/local/libexec/verify-matrix-ci-physical-install"), 0o755),
    "matrix-device-restart-broker": (RESTART_BROKER, 0o755),
    "matrix-ci-physical.service": (Path("/etc/systemd/system/matrix-ci-physical.service"), 0o644),
    "matrix-ci-physical.path": (Path("/etc/systemd/system/matrix-ci-physical.path"), 0o644),
    "matrix-device-restart.service": (RESTART_SERVICE, 0o644),
    "matrix-device-restart.path": (RESTART_PATH, 0o644),
    "matrix-web.service": (Path("/etc/systemd/system/matrix-web.service"), 0o644),
    "matrix-ci-physical.conf": (PHYSICAL_TMPFILES_POLICY, 0o644),
    "matrix-display.conf": (TMPFILES_POLICY, 0o644),
    "matrix-device-restart.conf": (RESTART_TMPFILES_POLICY, 0o644),
}
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


def _require_reviewed_digest(path: Path):
    expected = EXPECTED_ARTIFACT_DIGESTS.get(path)
    if expected is None:
        raise RuntimeError(f"missing separately reviewed digest: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f"installed file differs from reviewed digest: {path}")


def verify_reviewed_artifacts(expected):
    """Bind installed policy/code to operator-supplied, separately reviewed digests."""
    if set(expected) != set(REVIEWED_ARTIFACTS):
        raise RuntimeError("expected artifact digest set is incomplete")
    EXPECTED_ARTIFACT_DIGESTS.clear()
    for name, (path, mode) in REVIEWED_ARTIFACTS.items():
        digest = expected[name]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"invalid reviewed digest: {name}")
        require_file(path, mode)
        EXPECTED_ARTIFACT_DIGESTS[path] = digest
        _require_reviewed_digest(path)


def _parse_expected_artifact_digests(values):
    expected = {}
    for value in values:
        if "=" not in value:
            raise RuntimeError("expected artifact digest must be NAME=SHA256")
        name, digest = value.split("=", 1)
        if name in expected:
            raise RuntimeError(f"duplicate expected artifact digest: {name}")
        expected[name] = digest
    return expected


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


def verify_physical_runtime_install():
    require_file(PHYSICAL_TMPFILES_POLICY, 0o644)
    if PHYSICAL_TMPFILES_POLICY.read_bytes() != EXPECTED_PHYSICAL_TMPFILES_POLICY:
        raise RuntimeError("installed physical-runtime tmpfiles policy differs from reviewed source")
    info = PHYSICAL_RUNTIME_DIRECTORY.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("unsafe host-created physical runtime directory")


def verify_restart_broker_install():
    require_file(RESTART_BROKER, 0o755)
    _require_reviewed_digest(RESTART_BROKER)
    require_file(RESTART_SERVICE, 0o644)
    require_file(RESTART_PATH, 0o644)
    require_file(RESTART_TMPFILES_POLICY, 0o644)
    if RESTART_TMPFILES_POLICY.read_bytes() != EXPECTED_RESTART_TMPFILES_POLICY:
        raise RuntimeError("installed restart tmpfiles policy differs from reviewed source")
    account = pwd.getpwnam("matrix-web")
    info = RESTART_DIRECTORY.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != account.pw_gid
        or stat.S_IMODE(info.st_mode) != 0o1730
    ):
        raise RuntimeError("unsafe fixed restart request directory")
    require_file(RESTART_DIRECTORY / "transaction.lock", 0o644)


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
        security_directives = {
            "authorizedkeysfile", "authorizedkeyscommand", "authorizedkeyscommanduser",
            "permitrootlogin", "trustedusercakeys", "authorizedprincipalsfile",
            "authorizedprincipalscommand", "authorizedprincipalscommanduser",
            "hostbasedauthentication", "pubkeyauthentication", "pubkeyauthoptions",
            "ignorerhosts",
        }
        if name in directives and name in security_directives:
            raise RuntimeError(f"duplicate effective sshd directive: {name}")
        if name not in directives:
            directives[name] = value

    required = {
        "authorizedkeysfile", "authorizedkeyscommand", "authorizedkeyscommanduser",
        "permitrootlogin", "trustedusercakeys", "authorizedprincipalsfile",
        "authorizedprincipalscommand", "authorizedprincipalscommanduser",
        "hostbasedauthentication", "pubkeyauthentication", "pubkeyauthoptions",
        "ignorerhosts",
    }
    if not required.issubset(directives):
        raise RuntimeError("effective sshd key-source policy is incomplete")
    key_file = directives["authorizedkeysfile"]
    if key_file not in (".ssh/authorized_keys", "/root/.ssh/authorized_keys"):
        raise RuntimeError("effective AuthorizedKeysFile is not the reviewed file")
    exact = {
        "authorizedkeyscommand": "none",
        "trustedusercakeys": "none",
        "authorizedprincipalsfile": "none",
        "authorizedprincipalscommand": "none",
        "hostbasedauthentication": "no",
        "pubkeyauthentication": "yes",
        "pubkeyauthoptions": "none",
        "ignorerhosts": "yes",
    }
    for name, expected in exact.items():
        if directives[name] != expected:
            raise RuntimeError(f"effective {name} permits an alternate SSH key source")
    for name in ("authorizedkeyscommanduser", "authorizedprincipalscommanduser"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", directives[name]):
            raise RuntimeError(f"effective {name} is malformed")
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
        "Type", "RuntimeMaxUSec", "User", "Group", "CapabilityBoundingSet",
        "AmbientCapabilities", "ExecStart", "ExecStop", "ExecStopPost", "Requires",
        "After", "ReadOnlyPaths", "ReadWritePaths", "DevicePolicy", "DeviceAllow", "UMask",
    )
    if physical["Type"] != "exec":
        raise RuntimeError("physical service Type is not exec")
    if physical["RuntimeMaxUSec"] in ("", "0", "infinity"):
        raise RuntimeError("physical RuntimeMaxUSec is not effective")
    if physical["User"] != "root" or physical["Group"] != "root":
        raise RuntimeError("physical service identity differs from review")
    if set(physical["CapabilityBoundingSet"].split()) != {"CAP_SYS_RAWIO", "CAP_DAC_READ_SEARCH"}:
        raise RuntimeError("physical CapabilityBoundingSet differs from review")
    if physical["AmbientCapabilities"]:
        raise RuntimeError("physical AmbientCapabilities differs from review")
    if "flock" in physical["ExecStart"] or "matrix-ci-physical-verifier" not in physical["ExecStart"]:
        raise RuntimeError("physical ExecStart violates single path admission")
    if "--restore-only" not in physical["ExecStop"] or "--restore-only" not in physical["ExecStopPost"]:
        raise RuntimeError("physical recovery hooks are missing")
    if "systemd-tmpfiles-setup.service" not in physical["Requires"].split() or "systemd-tmpfiles-setup.service" not in physical["After"].split():
        raise RuntimeError("physical service can start before display-lock tmpfiles setup")
    if "/run/matrix-display" not in physical["ReadOnlyPaths"].split():
        raise RuntimeError("physical service display-lock namespace is not read-only")
    if physical["ReadWritePaths"].split() != ["/run/matrix-ci-physical"]:
        raise RuntimeError("physical writable paths differ from review")
    if physical["DevicePolicy"] != "auto" or physical["DeviceAllow"]:
        raise RuntimeError("physical device access differs from review")
    if physical["UMask"] != "0077":
        raise RuntimeError("physical service UMask is not 0077")

    path = properties("matrix-ci-physical.path", "Unit", "Paths", "ActiveState")
    if "matrix-ci-physical.service" not in path["Unit"] or "/run/matrix-ci-physical/claim" not in path["Paths"]:
        raise RuntimeError("matrix-ci-physical.path is not the claim admission owner")
    if path["ActiveState"] != "active":
        raise RuntimeError("matrix-ci-physical.path is not active")

    restart_path = properties(
        "matrix-device-restart.path", "Unit", "Paths", "ActiveState"
    )
    if (
        "matrix-device-restart.service" not in restart_path["Unit"]
        or "/run/matrix-device-restart/request" not in restart_path["Paths"]
        or restart_path["ActiveState"] != "active"
    ):
        raise RuntimeError("fixed restart path admission is not active")

    restart = properties(
        "matrix-device-restart.service", "Type", "ExecStart", "User", "Group",
        "CapabilityBoundingSet", "AmbientCapabilities", "ReadWritePaths", "DevicePolicy", "DeviceAllow",
    )
    if (
        restart["Type"] != "oneshot"
        or "matrix-device-restart-broker" not in restart["ExecStart"]
        or restart["User"] != "root"
        or restart["Group"] != "root"
        or restart["CapabilityBoundingSet"].split() != ["CAP_SYS_BOOT"]
        or restart["AmbientCapabilities"]
        or restart["ReadWritePaths"].split() != ["/run/matrix-device-restart"]
        or restart["DevicePolicy"] != "auto"
        or restart["DeviceAllow"]
    ):
        raise RuntimeError("fixed restart broker effective policy differs from review")

    web = properties(
        "matrix-web.service", "User", "Group", "InaccessiblePaths", "CapabilityBoundingSet",
        "AmbientCapabilities", "Requires", "After", "ReadOnlyPaths", "ReadWritePaths",
        "DevicePolicy", "DeviceAllow", "UMask",
    )
    if web["User"] != "matrix-web" or web["Group"] != "matrix-web":
        raise RuntimeError("matrix-web effective identity differs from review")
    if set(web["InaccessiblePaths"].split()) != {
        "/run/systemd/private", "/run/dbus/system_bus_socket", "/run/matrix-ci-physical"
    }:
        raise RuntimeError("matrix-web inaccessible paths differ from review")
    if web["CapabilityBoundingSet"].split() != ["CAP_SYS_RAWIO"]:
        raise RuntimeError("matrix-web CapabilityBoundingSet differs from review")
    if web["AmbientCapabilities"].split() != ["CAP_SYS_RAWIO"]:
        raise RuntimeError("matrix-web AmbientCapabilities differs from review")
    if "systemd-tmpfiles-setup.service" not in web["Requires"].split() or "systemd-tmpfiles-setup.service" not in web["After"].split():
        raise RuntimeError("matrix-web can start before display-lock tmpfiles setup")
    if "/run/matrix-display" not in web["ReadOnlyPaths"].split():
        raise RuntimeError("matrix-web display-lock namespace is not read-only")
    if set(web["ReadWritePaths"].split()) != {
        "/home/admin/matrix/data", "/home/admin/matrix/resources/media", "/run/matrix-device-restart"
    }:
        raise RuntimeError("matrix-web writable paths differ from review")
    if web["DevicePolicy"] != "closed" or web["DeviceAllow"] != "/dev/mem rw /dev/gpiomem rw /dev/vcio rw":
        raise RuntimeError("matrix-web device access differs from review")
    if web["UMask"] != "0077":
        raise RuntimeError("matrix-web UMask is not 0077")
    verify_physical_runtime_install()
    verify_display_lock_install()
    verify_restart_broker_install()


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
    parser.add_argument(
        "--expected-artifact-sha256", action="append", required=True, metavar="NAME=SHA256",
        help="digest copied from the separately reviewed installation input manifest",
    )
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root is required")
    verify_reviewed_artifacts(_parse_expected_artifact_digests(args.expected_artifact_sha256))
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
