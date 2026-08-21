#!/usr/bin/env python3
"""Restricted SSH forced command for the fixed appliance physical verifier."""

from __future__ import annotations

import fcntl
import ctypes
import errno
import itertools
import os
import re
import select
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path


COMMAND = re.compile(r"verify ([0-9a-f]{40}) ([0-9]{1,20})\Z")
SERVICE = "matrix-ci-physical.service"
PRODUCTION_SERVICE = "matrix-web.service"
RUNTIME = Path("/run/matrix-ci-physical")
LABELS = RUNTIME / "claim"
REPORT = RUNTIME / "report"
REPORT_LOCK = RUNTIME / "report.lock"
REPORT_COMMIT_LOCK = RUNTIME / "report.commit.lock"
DISPATCH_LOCK = RUNTIME / "dispatch.lock"
CLAIM_TEMPORARIES = "claim-temporaries"

MAX_REPORT_BYTES = 4096
MAX_ERROR_CHARS = 512
MAX_HTTP_STATUS_BYTES = 256
SYSTEMCTL_TIMEOUT = 10
STOP_WAIT_SECONDS = 170
REPORT_WAIT_SECONDS = 85
RENAME_NOREPLACE = 1
_TEMPORARY_SEQUENCE = itertools.count()

_CLEANUP_WARNINGS = {
    "unlink": "physical verifier cleanup warning: temporary unlink failed\n",
    "fsync": "physical verifier cleanup warning: directory sync failed\n",
}


def _cleanup_warning(kind):
    """Emit one fixed non-secret warning; diagnostics can never affect lifecycle."""
    try:
        sys.stderr.write(_CLEANUP_WARNINGS[kind])
        sys.stderr.flush()
    except BaseException:
        pass


def _rename_noreplace(old_name, new_name, directory_fd, new_directory_fd=None):
    """Linux renameat2: atomically move a complete inode without replacement."""
    target_fd = directory_fd if new_directory_fd is None else new_directory_fd
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace publication is unavailable")
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint
    )
    renameat2.restype = ctypes.c_int
    if renameat2(directory_fd, os.fsencode(old_name), target_fd, os.fsencode(new_name), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), new_name)


def _create_unique_temporary(runtime_fd, prefix):
    """Create an owner-private temp; stale or hostile names never block progress."""
    while True:
        name = f"{prefix}.{os.getpid()}.{next(_TEMPORARY_SEQUENCE)}"
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=runtime_fd,
            )
            return name, fd
        except FileExistsError:
            continue


def _unlink_owned_temporary(runtime_fd, name, identity):
    """Best-effort cleanup without deleting a replaced node or masking errors."""
    try:
        current = os.stat(name, dir_fd=runtime_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            return
        os.unlink(name, dir_fd=runtime_fd)
    except OSError:
        _cleanup_warning("unlink")
        return
    try:
        os.fsync(runtime_fd)
    except OSError:
        _cleanup_warning("fsync")


_LAST_SCAVENGE_VISITED = 0


def _scavenge_quiescent_temporaries(temporary_fd, *, limit=128):
    """At a host-proven quiescent point, unlink bounded entries without following."""
    global _LAST_SCAVENGE_VISITED
    visited = 0
    removed = 0
    unlink_warning_emitted = False
    with os.scandir(temporary_fd) as entries:
        for entry in entries:
            if visited >= limit:
                break
            visited += 1
            try:
                os.unlink(entry.name, dir_fd=temporary_fd)
                removed += 1
            except OSError:
                if not unlink_warning_emitted:
                    _cleanup_warning("unlink")
                    unlink_warning_emitted = True
    _LAST_SCAVENGE_VISITED = visited
    if removed:
        try:
            os.fsync(temporary_fd)
        except OSError:
            _cleanup_warning("fsync")
    return removed


def _scavenge_stale_temporaries(temporary_fd, prefix, *, limit=128):
    """Compatibility entry point restricted to the claim-temporary namespace."""
    if prefix != LABELS.name + ".tmp":
        raise RuntimeError("invalid claim temporary namespace")
    return _scavenge_quiescent_temporaries(temporary_fd, limit=limit)


def _require_metadata(info, *, mode, kind, description):
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise RuntimeError(f"unsafe {description}")
    if stat.S_IMODE(info.st_mode) != mode or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
        raise RuntimeError(f"unsafe {description} metadata")


def _require_trusted_directory(info, description, *, exact_mode=None):
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise RuntimeError(f"unsafe {description}")


def _open_trusted_parent(path):
    """Walk an absolute parent path by descriptor, rejecting writable ancestry."""
    if not path.is_absolute():
        raise RuntimeError("unsafe physical verifier runtime namespace")
    current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _require_trusted_directory(os.fstat(current_fd), "runtime root ancestry")
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
            _require_trusted_directory(os.fstat(current_fd), "runtime root ancestry")
        return current_fd
    except BaseException as error:
        os.close(current_fd)
        if isinstance(error, RuntimeError):
            raise RuntimeError("unsafe physical verifier runtime namespace") from error
        raise RuntimeError("unsafe physical verifier runtime namespace") from error


def open_runtime_fd():
    """Open the fixed runtime directory without following any pathname component."""
    parent_fd = _open_trusted_parent(RUNTIME.parent)
    try:
        try:
            os.mkdir(RUNTIME.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        runtime_fd = os.open(
            RUNTIME.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except BaseException as error:
        raise RuntimeError("unsafe physical verifier runtime namespace") from error
    finally:
        os.close(parent_fd)
    try:
        _require_trusted_directory(
            os.fstat(runtime_fd), "physical verifier runtime directory", exact_mode=0o700
        )
    except BaseException:
        os.close(runtime_fd)
        raise
    return runtime_fd


def open_temporary_namespace(runtime_fd, name):
    try:
        os.mkdir(name, 0o700, dir_fd=runtime_fd)
        os.fsync(runtime_fd)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=runtime_fd)
    try:
        _require_trusted_directory(os.fstat(fd), "temporary namespace", exact_mode=0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_named_lock(name, description):
    runtime_fd = open_runtime_fd()
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=runtime_fd)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=runtime_fd)
        try:
            _require_metadata(
                os.fstat(fd), mode=0o600, kind=stat.S_ISREG,
                description=description,
            )
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(runtime_fd)
    return os.fdopen(fd, "a+b", buffering=0)


def open_dispatch_lock():
    return open_named_lock(DISPATCH_LOCK.name, "physical verifier dispatcher lock")


def report_commit_is_held():
    commit_lock = open_named_lock(
        REPORT_COMMIT_LOCK.name, "physical verifier report commit lock"
    )
    try:
        try:
            fcntl.flock(commit_lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        commit_lock.close()


def _reset_protocol_lock(runtime_fd, path, description):
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=runtime_fd)
    except FileExistsError:
        fd = os.open(path.name, flags, dir_fd=runtime_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) not in (0, 0o600)
            or info.st_nlink != 1
        ):
            raise RuntimeError(f"unsafe physical verifier {description}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("physical verifier report publisher is busy") from error
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.fsync(runtime_fd)
    finally:
        os.close(fd)


def reset_report_lock(runtime_fd):
    _reset_protocol_lock(runtime_fd, REPORT_LOCK, "report publication lock")
    _reset_protocol_lock(runtime_fd, REPORT_COMMIT_LOCK, "report commit lock")


def parse_command(command):
    match = COMMAND.fullmatch(command)
    if not match:
        raise ValueError("expected: verify <40-lowercase-hex-sha> <decimal-pipeline-id>")
    return match.groups()


def parse_verifier_report(payload, expected_sha, expected_pipeline_id):
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("physical verifier report exceeded bound")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("physical verifier report is not ASCII") from error
    if not text.endswith("\n") or "\r" in text:
        raise ValueError("physical verifier report has invalid line endings")
    lines = text[:-1].split("\n")
    if len(lines) not in (3, 4):
        raise ValueError("physical verifier report has unexpected fields")
    pairs = []
    for line in lines:
        if "=" not in line:
            raise ValueError("physical verifier report has malformed field")
        pairs.append(line.split("=", 1))
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("physical verifier report has duplicate fields")
    values = dict(pairs)
    acceptance = values.get("PHYSICAL_DEVICE_ACCEPTANCE")
    expected_keys = ["SOURCE_SHA", "PIPELINE_ID", "PHYSICAL_DEVICE_ACCEPTANCE"]
    if acceptance == "FAIL":
        expected_keys.append("ERROR")
    elif acceptance != "PASS":
        raise ValueError("physical verifier report has invalid acceptance")
    if keys != expected_keys:
        raise ValueError("physical verifier report has unexpected or contradictory fields")
    if values["SOURCE_SHA"] != expected_sha or values["PIPELINE_ID"] != expected_pipeline_id:
        raise ValueError("physical verifier report labels do not match request")
    if acceptance == "FAIL":
        error = values["ERROR"]
        if not 1 <= len(error) <= MAX_ERROR_CHARS or any(not " " <= char <= "~" for char in error):
            raise ValueError("physical verifier report has invalid error evidence")
    return values


def canonical_report(values):
    lines = [
        f"SOURCE_SHA={values['SOURCE_SHA']}",
        f"PIPELINE_ID={values['PIPELINE_ID']}",
        f"PHYSICAL_DEVICE_ACCEPTANCE={values['PHYSICAL_DEVICE_ACCEPTANCE']}",
        "PHYSICAL_DEVICE_RECOVERY=PASS",
    ]
    if values["PHYSICAL_DEVICE_ACCEPTANCE"] == "FAIL":
        lines.append(f"ERROR={values['ERROR']}")
    return ("\n".join(lines) + "\n").encode("ascii")


class SystemOps:
    def await_pid1_ownership(self):
        deadline = time.monotonic() + SYSTEMCTL_TIMEOUT
        while time.monotonic() < deadline:
            state = _systemctl(
                "show", SERVICE, "-p", "ActiveState", "--value", check=False
            ).stdout.decode().strip()
            if state == "active":
                return
            if state == "failed":
                raise RuntimeError("physical verifier did not establish PID 1 ownership")
            time.sleep(0.05)
        raise RuntimeError("physical verifier PID 1 ownership wait timed out")

    def prepare(self, sha, pipeline_id):
        runtime_fd = open_runtime_fd()
        temporary_fd = open_temporary_namespace(runtime_fd, CLAIM_TEMPORARIES)
        temporary_name = None
        temporary_identity = None
        try:
            # A surviving claim is the complete admission record.  Check it
            # before touching prior evidence so a later dispatcher cannot
            # damage the in-flight verifier's report after an SSH disconnect.
            try:
                os.stat(LABELS.name, dir_fd=runtime_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("physical verifier durable claim is busy")
            reset_report_lock(runtime_fd)
            try:
                temporary_name, fd = _create_unique_temporary(temporary_fd, LABELS.name + ".tmp")
            except OSError as error:
                raise RuntimeError("unsafe physical verifier label temporary") from error
            try:
                info = os.fstat(fd)
                temporary_identity = (info.st_dev, info.st_ino)
                payload = f"{sha} {pipeline_id}\n".encode("ascii")
                written = 0
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count <= 0:
                        raise RuntimeError("physical verifier label write made no progress")
                    written += count
                os.fsync(fd)
                _require_metadata(
                    os.fstat(fd), mode=0o600, kind=stat.S_ISREG,
                    description="physical verifier labels",
                )
            except BaseException:
                if temporary_identity is not None:
                    _unlink_owned_temporary(temporary_fd, temporary_name, temporary_identity)
                raise
            finally:
                os.close(fd)
            try:
                report_info = os.stat(REPORT.name, dir_fd=runtime_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _require_metadata(
                    report_info, mode=0o600, kind=stat.S_ISREG,
                    description="physical verifier report",
                )
                os.unlink(REPORT.name, dir_fd=runtime_fd)
            try:
                # A no-replace move publishes one name and one link atomically:
                # once claim exists, its labels survive caller/SSH death and the
                # host path unit can finish admission independently.
                _rename_noreplace(temporary_name, LABELS.name, temporary_fd, runtime_fd)
            except OSError as error:
                if temporary_identity is not None:
                    _unlink_owned_temporary(temporary_fd, temporary_name, temporary_identity)
                raise RuntimeError("physical verifier durable claim is busy") from error
            os.fsync(runtime_fd)
        finally:
            os.close(temporary_fd)
            os.close(runtime_fd)


    def read_report(self):
        deadline = time.monotonic() + REPORT_WAIT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            state = _systemctl(
                "show", SERVICE, "-p", "ActiveState", "--value", check=False,
                timeout=min(SYSTEMCTL_TIMEOUT, remaining),
            ).stdout.decode().strip()
            if state in ("failed", "inactive"):
                raise RuntimeError("physical verifier became inactive before reporting")
            report_lock = open_named_lock(
                REPORT_LOCK.name, "physical verifier report publication lock"
            )
            try:
                try:
                    fcntl.flock(report_lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    lock_acquired = True
                except BlockingIOError:
                    lock_acquired = False
                if lock_acquired and report_commit_is_held():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    state = _systemctl(
                        "show", SERVICE, "-p", "ActiveState", "--value",
                        check=False, timeout=min(SYSTEMCTL_TIMEOUT, remaining),
                    ).stdout.decode().strip()
                    if state in ("failed", "inactive"):
                        raise RuntimeError(
                            "physical verifier became inactive before reporting"
                        )
                    if state == "active":
                        runtime_fd = open_runtime_fd()
                        try:
                            try:
                                fd = os.open(
                                    REPORT.name,
                                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                                    | os.O_CLOEXEC,
                                    dir_fd=runtime_fd,
                                )
                            except FileNotFoundError:
                                fd = None
                            if fd is not None:
                                try:
                                    _require_metadata(
                                        os.fstat(fd), mode=0o600, kind=stat.S_ISREG,
                                        description="physical verifier report",
                                    )
                                    return os.read(fd, MAX_REPORT_BYTES + 1)
                                finally:
                                    os.close(fd)
                        finally:
                            os.close(runtime_fd)
            finally:
                report_lock.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
        raise RuntimeError("physical verifier report wait timed out")

    def stop_and_wait(self):
        # Start the stop transaction without imposing the per-command timeout
        # on the complete PID 1 stop/recovery job.
        status = _systemctl("stop", "--no-block", SERVICE, check=False).returncode
        if status != 0:
            return status
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        while time.monotonic() < deadline:
            result = _systemctl(
                "show", SERVICE, "-p", "ActiveState", "-p", "Job",
                check=False,
            )
            if result.returncode != 0:
                return result.returncode
            records = {}
            for line in result.stdout.decode("ascii", "strict").splitlines():
                if "=" not in line:
                    raise RuntimeError("physical verifier stop state was malformed")
                name, value = line.split("=", 1)
                if name in records:
                    raise RuntimeError("physical verifier stop state was malformed")
                records[name] = value
            if set(records) != {"ActiveState", "Job"}:
                raise RuntimeError("physical verifier stop state was malformed")
            job = records["Job"]
            if job == "":
                return 0 if records["ActiveState"] == "inactive" else 1
            if re.fullmatch(r"[1-9][0-9]*", job) is None:
                raise RuntimeError("physical verifier stop state was malformed")
            time.sleep(0.05)
        raise RuntimeError("physical verifier PID 1 stop wait timed out")

    def recovery_ready(self):
        if _systemctl("is-active", "--quiet", PRODUCTION_SERVICE, check=False).returncode != 0:
            return False
        deadline = time.monotonic() + 2
        connection = None
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.setblocking(False)
            result = connection.connect_ex(("127.0.0.1", 5000))
            if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                return False
            if result:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _, writable, exceptional = select.select(
                    [], [connection], [connection], remaining
                )
                if exceptional or not writable:
                    return False
                if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    return False

            request = memoryview(
                b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            while request:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _, writable, exceptional = select.select(
                    [], [connection], [connection], remaining
                )
                if exceptional or not writable:
                    return False
                sent = connection.send(request)
                if sent <= 0:
                    return False
                request = request[sent:]

            status = bytearray()
            while len(status) < MAX_HTTP_STATUS_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                readable, _, exceptional = select.select(
                    [connection], [], [connection], remaining
                )
                if exceptional or not readable:
                    return False
                chunk = connection.recv(MAX_HTTP_STATUS_BYTES - len(status))
                if not chunk:
                    return False
                status.extend(chunk)
                if time.monotonic() >= deadline:
                    return False
                marker = status.find(b"\r\n")
                if marker >= 0:
                    return re.fullmatch(
                        rb"HTTP/1\.[01] (?:200|302)(?: [^\r\n]*)?",
                        bytes(status[:marker]),
                    ) is not None
            return False
        except Exception:
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


def dispatch(command, ops=None):
    sha, pipeline_id = parse_command(command)
    ops = ops or SystemOps()
    await_pid1_ownership = getattr(ops, "await_pid1_ownership", None)
    # Publishing the immutable no-replace claim is the sole start operation.
    # The enabled PID-1 path unit observes it, so SSH loss cannot abandon a
    # queued start and there is no second explicit-start path to race it.
    ops.prepare(sha, pipeline_id)
    if await_pid1_ownership is not None:
        await_pid1_ownership()
    parsed = None
    parse_error = None
    try:
        # Parse into dispatcher memory while production is still stopped and the
        # verifier unit remains active. Production cannot alter accepted evidence.
        parsed = parse_verifier_report(ops.read_report(), sha, pipeline_id)
    except (RuntimeError, ValueError) as error:
        parse_error = error
    finally:
        stop_status = ops.stop_and_wait()
    if stop_status != 0 or not ops.recovery_ready():
        raise RuntimeError("physical device recovery failed")
    if parse_error is not None:
        raise RuntimeError(str(parse_error)) from parse_error
    if parsed is None:
        raise RuntimeError("physical verifier report was not accepted")
    report = canonical_report(parsed)
    return (0 if parsed["PHYSICAL_DEVICE_ACCEPTANCE"] == "PASS" else 1), report


def _systemctl(*args, check=True, timeout: float = SYSTEMCTL_TIMEOUT):
    try:
        return subprocess.run(
            ("/bin/systemctl", *args),
            check=check,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"bounded systemctl call timed out: {args[0]}") from error


def main():
    if os.geteuid() != 0:
        print("dispatcher must be root", file=sys.stderr)
        return 126
    try:
        sha, pipeline_id = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 64
    try:
        lock_context = open_dispatch_lock()
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 75
    with lock_context as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("physical verifier dispatcher is busy", file=sys.stderr)
            return 75
        runtime_fd = open_runtime_fd()
        temporary_fd = open_temporary_namespace(runtime_fd, CLAIM_TEMPORARIES)
        try:
            _scavenge_quiescent_temporaries(temporary_fd)
        finally:
            os.close(temporary_fd)
            os.close(runtime_fd)
        try:
            status, report = dispatch(f"verify {sha} {pipeline_id}")
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 75
        sys.stdout.buffer.write(report)
        return status


if __name__ == "__main__":
    raise SystemExit(main())
