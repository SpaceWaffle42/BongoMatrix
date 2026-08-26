#!/usr/bin/env python3
"""Digest-pinned, idempotent installer for the Priority 1.3 privileged endpoint."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_argparse import add_store_once_argument

REQUIRED_KEY_OPTIONS = (
    'command="/usr/local/libexec/matrix-priority-1-3-remote",restrict,'
    "no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding"
)
_ARTIFACTS = {
    "dispatcher": ("deploy/matrix_priority_1_3_remote.py", "usr/local/libexec/matrix-priority-1-3-remote", 0o755),
    "guarded": ("deploy/guarded_priority_1_3_release.py", "usr/local/lib/matrix-priority-1-3/deploy/guarded_priority_1_3_release.py", 0o644),
    "argparse": ("release_argparse.py", "usr/local/lib/matrix-priority-1-3/release_argparse.py", 0o644),
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _regular(path: Path, *, mode: int | None = None) -> os.stat_result:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise ValueError(f"unsafe regular file: {path}")
    return info


def _key(public_key: Path) -> tuple[str, str]:
    _regular(public_key)
    try:
        fields = public_key.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeError) as error:
        raise ValueError("public key is unreadable") from error
    if len(fields) not in {2, 3} or fields[0] != "ssh-ed25519":
        raise ValueError("dedicated public key is not canonical Ed25519")
    try:
        raw = base64.b64decode(fields[1], validate=True)
    except ValueError as error:
        raise ValueError("dedicated public key is malformed") from error
    label = b"ssh-ed25519"
    if (len(raw) != 4 + len(label) + 4 + 32
            or int.from_bytes(raw[:4], "big") != len(label)
            or raw[4:4 + len(label)] != label
            or int.from_bytes(raw[4 + len(label):8 + len(label)], "big") != 32):
        raise ValueError("dedicated public key blob is malformed")
    return fields[1], "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def _sources(source_root: Path, digests: dict[str, str]) -> dict[str, tuple[Path, bytes, str, int]]:
    if set(digests) != set(_ARTIFACTS):
        raise ValueError("reviewed digest set is incomplete")
    result = {}
    for name, (relative, destination, mode) in _ARTIFACTS.items():
        expected = digests[name]
        if type(expected) is not str or _DIGEST.fullmatch(expected) is None:
            raise ValueError("reviewed digest is invalid")
        path = source_root / relative
        _regular(path)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"reviewed source digest mismatch: {name}")
        result[name] = (path, payload, destination, mode)
    return result


def _desired_authorization(public_key: Path, source_restriction: str) -> tuple[bytes, str, str]:
    try:
        network = ipaddress.ip_network(source_restriction, strict=True)
    except ValueError as error:
        raise ValueError("coordinator source restriction is invalid") from error
    if network.num_addresses != 1 or str(network) != source_restriction:
        raise ValueError("coordinator source restriction must be one canonical host")
    blob, fingerprint = _key(public_key)
    line = (
        f'from="{source_restriction}",{REQUIRED_KEY_OPTIONS} '
        f"ssh-ed25519 {blob} matrix-priority-1-3\n"
    )
    return line.encode("ascii"), blob, fingerprint


def _authorization_state(current: bytes, desired: bytes, blob: str) -> bool:
    try:
        lines = current.decode("ascii").splitlines()
    except UnicodeError as error:
        raise ValueError("authorized_keys is not ASCII") from error
    matching = [line for line in lines if blob in line.split()]
    desired_line = desired.decode("ascii").rstrip("\n")
    if len(matching) > 1 or (matching and matching[0] != desired_line):
        raise ValueError("dedicated key is reused without exact forced policy")
    return bool(matching)


def _preflight_authorized_keys(path: Path, desired: bytes, blob: str) -> tuple[bytes, bool, os.stat_result]:
    info = _regular(path, mode=0o600)
    if info.st_uid != 0 or info.st_gid != 0:
        raise ValueError("authorized_keys owner is unsafe")
    if info.st_size > 4 * 1024 * 1024:
        raise ValueError("authorized_keys is unreasonably large")
    current = path.read_bytes()
    return current, _authorization_state(current, desired, blob), info


def _ensure_parent(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    _regular_directory(root)
    for part in relative.parts[:-1]:
        current /= part
        if current.exists() or current.is_symlink():
            _regular_directory(current)
        else:
            current.mkdir(mode=0o755)
            os.chown(current, 0, 0)


def _regular_directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError(f"unsafe installation directory: {path}")
    return info


def _publish_absent(path: Path, payload: bytes, mode: int) -> bool:
    if path.exists() or path.is_symlink():
        info = _regular(path, mode=mode)
        if info.st_uid != 0 or info.st_gid != 0 or path.read_bytes() != payload:
            raise ValueError(f"installed artifact differs from reviewed source: {path}")
        return False
    temporary = path.with_name(f".{path.name}.install-{os.getpid()}-{os.urandom(8).hex()}")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short install write")
            view = view[written:]
        os.fchmod(descriptor, mode); os.fchown(descriptor, 0, 0); os.fsync(descriptor); os.close(descriptor); descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(parent)
        finally: os.close(parent)
        return True
    finally:
        if descriptor is not None: os.close(descriptor)
        try: temporary.unlink()
        except FileNotFoundError: pass


def _append_authorization(path: Path, desired: bytes, blob: str) -> bool:
    _regular_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != 0 or info.st_gid != 0):
            raise ValueError("authorized_keys descriptor is unsafe")
        current_path = path.lstat()
        if (current_path.st_dev, current_path.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("authorized_keys changed before admission")
        chunks = []
        offset = 0
        while True:
            chunk = os.pread(descriptor, 65536, offset)
            if not chunk:
                break
            chunks.append(chunk); offset += len(chunk)
            if offset > 4 * 1024 * 1024:
                raise ValueError("authorized_keys is unreasonably large")
        current = b"".join(chunks)
        if _authorization_state(current, desired, blob):
            return False
        payload = (b"" if not current or current.endswith(b"\n") else b"\n") + desired
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short authorized_keys append")
        os.fsync(descriptor)
        final_size = os.fstat(descriptor).st_size
        if final_size > 4 * 1024 * 1024:
            raise ValueError("authorized_keys became unreasonably large")
        final_bytes = os.pread(descriptor, final_size, 0)
        if len(final_bytes) != final_size or not _authorization_state(final_bytes, desired, blob):
            raise ValueError("authorized_keys admission did not verify")
        final_path = path.lstat()
        if (final_path.st_dev, final_path.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("authorized_keys changed during admission")
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return True
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _preflight_destinations(
    install_root: Path, sources: dict[str, tuple[Path, bytes, str, int]],
) -> None:
    for _, payload, destination, mode in sources.values():
        path = install_root / destination
        if not path.exists() and not path.is_symlink():
            continue
        info = _regular(path, mode=mode)
        if info.st_uid != 0 or info.st_gid != 0 or path.read_bytes() != payload:
            raise ValueError(f"installed artifact differs from reviewed source: {path}")


def _validate_parent(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    _regular_directory(current)
    for part in relative.parts[:-1]:
        current /= part
        _regular_directory(current)


def _authorized_keys_path(install_root: Path) -> Path:
    path = install_root / "root/.ssh/authorized_keys"
    _validate_parent(path, install_root)
    if stat.S_IMODE(_regular_directory(path.parent).st_mode) != 0o700:
        raise ValueError("root SSH directory mode is unsafe")
    return path


def _preflight_parent_prefix(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    _regular_directory(current)
    for part in relative.parts[:-1]:
        current /= part
        if not current.exists() and not current.is_symlink():
            break
        _regular_directory(current)


def _prepare_all_parents(
    install_root: Path, sources: dict[str, tuple[Path, bytes, str, int]],
) -> None:
    for _, _, destination, _ in sources.values():
        _preflight_parent_prefix(install_root / destination, install_root)
    for _, _, destination, _ in sources.values():
        _ensure_parent(install_root / destination, install_root)
    for _, _, destination, _ in sources.values():
        _validate_parent(install_root / destination, install_root)


def _validate_all_parents(
    install_root: Path, sources: dict[str, tuple[Path, bytes, str, int]],
) -> None:
    for _, _, destination, _ in sources.values():
        _validate_parent(install_root / destination, install_root)


def verify_effective_sshd(output: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    security = {
        "authorizedkeysfile", "authorizedkeyscommand", "authorizedkeyscommanduser",
        "permitrootlogin", "trustedusercakeys", "authorizedprincipalsfile",
        "authorizedprincipalscommand", "authorizedprincipalscommanduser",
        "hostbasedauthentication", "pubkeyauthentication", "pubkeyauthoptions", "ignorerhosts",
    }
    for line in output.splitlines():
        if not line or line != line.strip() or " " not in line:
            raise ValueError("malformed effective sshd output")
        name, value = line.split(" ", 1)
        if name in directives and name in security:
            raise ValueError("duplicate effective sshd security directive")
        directives.setdefault(name, value)
    if not security.issubset(directives):
        raise ValueError("effective sshd policy is incomplete")
    if directives["authorizedkeysfile"] not in {".ssh/authorized_keys", "/root/.ssh/authorized_keys"}:
        raise ValueError("effective authorized key source differs")
    exact = {
        "authorizedkeyscommand": "none", "permitrootlogin": "forced-commands-only",
        "trustedusercakeys": "none", "authorizedprincipalsfile": "none",
        "authorizedprincipalscommand": "none", "hostbasedauthentication": "no",
        "pubkeyauthentication": "yes", "pubkeyauthoptions": "none", "ignorerhosts": "yes",
    }
    if any(directives[name] != value for name, value in exact.items()):
        raise ValueError("effective sshd policy permits alternate authority")
    for name in ("authorizedkeyscommanduser", "authorizedprincipalscommanduser"):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", directives[name]) is None:
            raise ValueError("effective sshd command identity is malformed")
    return {"authorized_keys_file": "/root/.ssh/authorized_keys",
            "permit_root_login": directives["permitrootlogin"]}


def verify_live_sshd(
    *, source_address: str, source_host: str, local_address: str,
    local_port: int, rdomain: str,
) -> dict[str, str]:
    for value, label in ((source_address, "source"), (local_address, "local")):
        try:
            canonical = str(ipaddress.ip_address(value))
        except ValueError as error:
            raise ValueError(f"invalid sshd {label} address") from error
        if canonical != value:
            raise ValueError(f"noncanonical sshd {label} address")
    if (type(local_port) is not int or not 1 <= local_port <= 65535
            or re.fullmatch(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", source_host) is None
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", rdomain) is None):
        raise ValueError("invalid sshd connection context")
    context = (
        f"user=root,addr={source_address},host={source_host},"
        f"laddr={local_address},lport={local_port},rdomain={rdomain}"
    )
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    syntax = subprocess.run(
        ["/usr/sbin/sshd", "-t"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=10, check=False, env=environment,
    )
    if syntax.returncode != 0 or syntax.stdout or syntax.stderr:
        raise ValueError("live sshd syntax verification failed")
    effective = subprocess.run(
        ["/usr/sbin/sshd", "-T", "-C", context], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=10, check=False, env=environment,
    )
    if (effective.returncode != 0 or effective.stderr
            or len(effective.stdout.encode("utf-8")) > 1024 * 1024):
        raise ValueError("live sshd effective-policy query failed")
    return verify_effective_sshd(effective.stdout)


def verify(
    source_root: Path, install_root: Path, public_key: Path,
    digests: dict[str, str], source_restriction: str,
) -> dict[str, str]:
    source_root, install_root, public_key = map(Path, (source_root, install_root, public_key))
    sources = _sources(source_root, digests)
    desired, blob, fingerprint = _desired_authorization(public_key, source_restriction)
    authorized = _authorized_keys_path(install_root)
    _, present, _ = _preflight_authorized_keys(authorized, desired, blob)
    if not present:
        raise ValueError("dedicated forced-command key is absent")
    _validate_all_parents(install_root, sources)
    for _, payload, destination, mode in sources.values():
        path = install_root / destination
        info = _regular(path, mode=mode)
        if info.st_uid != 0 or info.st_gid != 0 or path.read_bytes() != payload:
            raise ValueError("installed artifact verification failed")
    return {"status": "verified", "key_fingerprint": fingerprint}


def install(
    source_root: Path, install_root: Path, public_key: Path,
    digests: dict[str, str], source_restriction: str,
) -> dict[str, str]:
    source_root, install_root, public_key = map(Path, (source_root, install_root, public_key))
    sources = _sources(source_root, digests)
    desired, blob, fingerprint = _desired_authorization(public_key, source_restriction)
    authorized = _authorized_keys_path(install_root)
    _, key_present, _ = _preflight_authorized_keys(authorized, desired, blob)
    _preflight_destinations(install_root, sources)
    installed = [
        (install_root / destination).exists()
        for _, _, destination, _ in sources.values()
    ]
    if key_present and not all(installed):
        raise ValueError("active forced key has an incomplete privileged runtime")
    _prepare_all_parents(install_root, sources)
    for name in ("guarded", "argparse", "dispatcher"):
        _, payload, destination, mode = sources[name]
        path = install_root / destination
        _ensure_parent(path, install_root)
        _publish_absent(path, payload, mode)
    if not key_present:
        _append_authorization(authorized, desired, blob)
    verify(source_root, install_root, public_key, digests, source_restriction)
    return {"status": "already-installed" if key_present else "installed", "key_fingerprint": fingerprint}


def _digests(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value: raise ValueError("digest must be NAME=SHA256")
        name, digest = value.split("=", 1)
        if name in result: raise ValueError("duplicate digest")
        result[name] = digest
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("install", "verify"))
    add_store_once_argument(parser, "--source-root", type=Path, required=True)
    add_store_once_argument(parser, "--install-root", type=Path, default=Path("/"))
    add_store_once_argument(parser, "--public-key", type=Path, required=True)
    add_store_once_argument(parser, "--source-restriction", required=True)
    add_store_once_argument(parser, "--sshd-source-address", required=True)
    add_store_once_argument(parser, "--sshd-source-host", required=True)
    add_store_once_argument(parser, "--sshd-local-address", required=True)
    add_store_once_argument(parser, "--sshd-local-port", type=int, required=True)
    add_store_once_argument(parser, "--sshd-rdomain", required=True)
    parser.add_argument("--expected-sha256", action="append", default=[], required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if os.geteuid() != 0: parser.error("root is required")
    try:
        network = ipaddress.ip_network(args.source_restriction, strict=True)
        if str(network.network_address) != args.sshd_source_address:
            raise ValueError("source restriction and live sshd source differ")
        verify_live_sshd(
            source_address=args.sshd_source_address,
            source_host=args.sshd_source_host,
            local_address=args.sshd_local_address,
            local_port=args.sshd_local_port,
            rdomain=args.sshd_rdomain,
        )
        result = globals()[args.operation](
            args.source_root, args.install_root, args.public_key,
            _digests(args.expected_sha256), args.source_restriction,
        )
    except (OSError, ValueError, TypeError):
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
