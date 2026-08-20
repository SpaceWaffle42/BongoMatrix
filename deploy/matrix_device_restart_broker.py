#!/usr/bin/env python3
"""Host-owned broker for exactly one fixed delayed whole-device reboot."""

import ctypes
import errno
import fcntl
import itertools
import os
import pwd
import stat
import subprocess
import time

PARENT = "/run/matrix-device-restart"
REQUEST = "request"
LOCK = "transaction.lock"
TERMINALS = ("failed", "cancelled", "invalid")
DELAY_SECONDS = 30
RENAME_NOREPLACE = 1
_TRANSACTION_CONTEXT_KEY = object()
_INVALID_TEMPORARY_PREFIX = f"{REQUEST}.invalid.tmp."
_TEMPORARY_SEQUENCE = itertools.count()
_TEMPORARY_LIMIT = 128


class ManualRecoveryError(RuntimeError):
    pass


class _TransactionLock:
    """Capability proving this module acquired and still owns the transaction fd."""

    __slots__ = ("_key", "_parent_fd", "_parent_identity", "_fd", "_lock_identity", "_active")

    def __init__(self, key, parent_fd, parent_identity, lock_fd, lock_identity):
        if key is not _TRANSACTION_CONTEXT_KEY:
            raise RuntimeError("invalid restart transaction lock context")
        self._key = key
        self._parent_fd = parent_fd
        self._parent_identity = parent_identity
        self._fd = lock_fd
        self._lock_identity = lock_identity
        self._active = True

    def close(self):
        if self._active:
            self._active = False
            os.close(self._fd)


def _open_trusted_runtime(path):
    if not os.path.isabs(path):
        raise RuntimeError("unsafe fixed restart request directory ancestry")
    components = path.split(os.sep)[1:]
    if not components or any(component in ("", ".", "..") for component in components):
        raise RuntimeError("unsafe fixed restart request directory ancestry")
    parent_fd = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        for index, component in enumerate(components):
            if index:
                ancestor = os.fstat(parent_fd)
                if (not stat.S_ISDIR(ancestor.st_mode) or ancestor.st_uid != 0
                        or ancestor.st_gid != 0 or stat.S_IMODE(ancestor.st_mode) & 0o022):
                    raise RuntimeError("unsafe fixed restart request directory ancestry")
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _validate_parent(parent_fd, account):
    parent = os.fstat(parent_fd)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
            or parent.st_gid != account.pw_gid or stat.S_IMODE(parent.st_mode) != 0o1730):
        raise RuntimeError("unsafe fixed restart request directory")


def _acquire_transaction_lock(parent_fd):
    parent = os.fstat(parent_fd)
    before = os.stat(LOCK, dir_fd=parent_fd, follow_symlinks=False)
    lock_fd = os.open(
        LOCK, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(lock_fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0 or opened.st_gid != 0
                or stat.S_IMODE(opened.st_mode) != 0o644 or opened.st_nlink != 1
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise RuntimeError("unsafe fixed restart transaction lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = os.stat(LOCK, dir_fd=parent_fd, follow_symlinks=False)
        if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or not stat.S_ISREG(current.st_mode) or current.st_uid != 0
                or current.st_gid != 0 or stat.S_IMODE(current.st_mode) != 0o644
                or current.st_nlink != 1):
            raise RuntimeError("fixed restart transaction lock changed")
        return _TransactionLock(
            _TRANSACTION_CONTEXT_KEY,
            parent_fd,
            (parent.st_dev, parent.st_ino),
            lock_fd,
            (opened.st_dev, opened.st_ino),
        )
    except BaseException:
        os.close(lock_fd)
        raise


def _terminal_exists(parent_fd, account):
    for terminal in TERMINALS:
        try:
            info = os.stat(f"{REQUEST}.{terminal}", dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_uid, info.st_gid) not in ((account.pw_uid, account.pw_gid), (0, 0))
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or info.st_size != 0):
            raise RuntimeError("unsafe fixed restart terminal marker")
        return True
    return False


def _assert_transaction_lock(parent_fd, transaction):
    if (
        type(transaction) is not _TransactionLock
        or transaction._key is not _TRANSACTION_CONTEXT_KEY
        or not transaction._active
        or transaction._parent_fd != parent_fd
    ):
        raise RuntimeError("invalid restart transaction lock context")
    parent = os.fstat(parent_fd)
    if (parent.st_dev, parent.st_ino) != transaction._parent_identity:
        raise RuntimeError("restart transaction parent changed")
    opened = os.fstat(transaction._fd)
    current = os.stat(LOCK, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o644
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != transaction._lock_identity
        or transaction._lock_identity != (current.st_dev, current.st_ino)
    ):
        raise RuntimeError("unsafe fixed restart transaction lock")


def _discard_request_for_terminal_with_lock(parent_fd, transaction, account):
    _assert_transaction_lock(parent_fd, transaction)
    info = os.stat(REQUEST, dir_fd=parent_fd, follow_symlinks=False)
    _remove_validated_request_with_lock(parent_fd, transaction, (info.st_dev, info.st_ino))


def _validated_invalid_terminal(parent_fd):
    info = os.stat(f"{REQUEST}.invalid", dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size != 0):
        raise RuntimeError("unsafe fixed restart terminal marker")


def _unlink_owned_invalid_temporary(parent_fd, name, identity):
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            return
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        pass


def _scavenge_invalid_temporaries(parent_fd):
    removed = False
    visited = 0
    with os.scandir(parent_fd) as entries:
        for entry in entries:
            if visited >= _TEMPORARY_LIMIT:
                break
            visited += 1
            if not entry.name.startswith(_INVALID_TEMPORARY_PREFIX):
                continue
            try:
                info = os.stat(entry.name, dir_fd=parent_fd, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or info.st_size != 0):
                    continue
                os.unlink(entry.name, dir_fd=parent_fd)
                removed = True
            except FileNotFoundError:
                continue
    if removed:
        os.fsync(parent_fd)


def _publish_invalid_terminal_with_lock(parent_fd, transaction):
    _assert_transaction_lock(parent_fd, transaction)
    _scavenge_invalid_temporaries(parent_fd)
    name = None
    fd = None
    identity = None
    try:
        for _attempt in range(_TEMPORARY_LIMIT):
            candidate = f"{_INVALID_TEMPORARY_PREFIX}{os.getpid()}.{next(_TEMPORARY_SEQUENCE)}"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                name = candidate
                break
            except FileExistsError:
                continue
        if fd is None or name is None:
            raise RuntimeError("restart invalid terminal temporary namespace exhausted")
        os.fchmod(fd, 0o600)
        os.fchown(fd, 0, 0)
        os.fsync(fd)
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0 or opened.st_gid != 0
                or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
                or opened.st_size != 0 or identity != (current.st_dev, current.st_ino)):
            raise RuntimeError("unsafe restart invalid terminal temporary")
        try:
            _rename_noreplace(parent_fd, name, f"{REQUEST}.invalid")
            name = None
            os.fsync(parent_fd)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
            _validated_invalid_terminal(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if name is not None and identity is not None:
            _unlink_owned_invalid_temporary(parent_fd, name, identity)


def _normalize_invalid_request_with_lock(parent_fd, transaction, identity):
    _publish_invalid_terminal_with_lock(parent_fd, transaction)
    _remove_validated_request_with_lock(parent_fd, transaction, identity)


def _validated_request():
    account = pwd.getpwnam("matrix-web")
    parent_fd = _open_trusted_runtime(PARENT)
    transaction = None
    try:
        _validate_parent(parent_fd, account)
        transaction = _acquire_transaction_lock(parent_fd)
        if _terminal_exists(parent_fd, account):
            _discard_request_for_terminal_with_lock(parent_fd, transaction, account)
            raise ManualRecoveryError("restart requires manual recovery")
        observed = os.stat(REQUEST, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(observed.st_mode) or observed.st_uid != account.pw_uid
                or observed.st_gid != account.pw_gid or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_nlink != 1 or observed.st_size != 0):
            _normalize_invalid_request_with_lock(
                parent_fd, transaction, (observed.st_dev, observed.st_ino)
            )
            raise RuntimeError("unsafe fixed restart request marker")
        fd = os.open(REQUEST, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            current = os.stat(REQUEST, dir_fd=parent_fd, follow_symlinks=False)
            if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino)):
                raise RuntimeError("restart request marker changed during admission")
            os.fchown(fd, 0, 0)
            claimed = os.fstat(fd)
            current = os.stat(REQUEST, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (claimed.st_dev, claimed.st_ino) != (info.st_dev, info.st_ino)
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or claimed.st_uid != 0
                or claimed.st_gid != 0
                or current.st_uid != 0
                or current.st_gid != 0
                or stat.S_IMODE(claimed.st_mode) != 0o600
                or stat.S_IMODE(current.st_mode) != 0o600
                or claimed.st_nlink != 1
                or current.st_nlink != 1
            ):
                raise RuntimeError("restart request claim changed during admission")
            os.fsync(parent_fd)
            return parent_fd, fd, (info.st_dev, info.st_ino)
        except BaseException:
            os.close(fd)
            raise
        finally:
            if transaction is not None:
                transaction.close()
                transaction = None
    except BaseException:
        if transaction is not None:
            transaction.close()
        os.close(parent_fd)
        raise


def _remove_validated_request_with_lock(parent_fd, transaction, identity):
    _assert_transaction_lock(parent_fd, transaction)
    current = os.stat(REQUEST, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != identity:
        raise RuntimeError("restart request marker changed before cleanup")
    os.unlink(REQUEST, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _remove_validated_request(parent_fd, identity):
    transaction = _acquire_transaction_lock(parent_fd)
    try:
        _remove_validated_request_with_lock(parent_fd, transaction, identity)
    finally:
        transaction.close()


def _rename_noreplace(parent_fd, source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic restart-state publication is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _quarantine_validated_request_with_lock(parent_fd, transaction, identity, terminal):
    _assert_transaction_lock(parent_fd, transaction)
    current = os.stat(REQUEST, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != identity:
        raise RuntimeError("restart request marker changed before terminal cleanup")
    try:
        _rename_noreplace(parent_fd, REQUEST, f"{REQUEST}.{terminal}")
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise
        _remove_validated_request_with_lock(parent_fd, transaction, identity)
        return
    os.fsync(parent_fd)


def _quarantine_validated_request(parent_fd, identity, terminal):
    transaction = _acquire_transaction_lock(parent_fd)
    try:
        _quarantine_validated_request_with_lock(parent_fd, transaction, identity, terminal)
    finally:
        transaction.close()


def _abort_before_reboot_if_terminal(parent_fd, identity):
    account = pwd.getpwnam("matrix-web")
    transaction = _acquire_transaction_lock(parent_fd)
    try:
        if not _terminal_exists(parent_fd, account):
            return
        _remove_validated_request_with_lock(parent_fd, transaction, identity)
        raise ManualRecoveryError("restart requires manual recovery")
    finally:
        transaction.close()


def run_broker(*, sleep, reboot):
    parent_fd, request_fd, identity = _validated_request()
    try:
        try:
            sleep(DELAY_SECONDS)
            _abort_before_reboot_if_terminal(parent_fd, identity)
            reboot()
        except BaseException as error:
            if isinstance(error, ManualRecoveryError):
                raise
            terminal = "cancelled" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            _quarantine_validated_request(parent_fd, identity, terminal)
            raise
        _remove_validated_request(parent_fd, identity)
    finally:
        os.close(request_fd)
        os.close(parent_fd)


def main():
    if os.geteuid() != 0:
        raise SystemExit("restart broker must be root")
    run_broker(
        sleep=time.sleep,
        reboot=lambda: subprocess.run(
            ("/bin/systemctl", "reboot"), check=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        ),
    )


if __name__ == "__main__":
    main()
