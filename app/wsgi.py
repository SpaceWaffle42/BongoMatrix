"""Production Flask entry point for the Matrix control panel."""

from __future__ import annotations

import atexit
import os
import pwd
import secrets
import stat
import sys
from pathlib import Path

import app as _entrypoint_owner

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_store import ConfigStore
from display_controller import DisplayController
from hardware_lock import DisplayOwnershipLock
from matrix_framework import Matrix
from pairing import PairedDeviceStore, PairingManager, initialize_pairing_display
from secret_store import SecretStore
from webapp import create_app


def load_secret(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, secrets.token_hex(32).encode())
        os.fsync(fd)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise RuntimeError(f"Refusing unsafe session-key path: {path}")
        fd = os.open(path, flags)
    try:
        os.fchmod(fd, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
        value = os.read(fd, 256).decode().strip()
    finally:
        os.close(fd)
    if len(value) < 32:
        raise RuntimeError("Session key is missing or too short")
    return value


with _entrypoint_owner._wsgi_runtime_lock:
    runtime = _entrypoint_owner._wsgi_runtime
    if runtime is None:
        config_store = ConfigStore(ROOT / "data" / "config.json")
        device_store = PairedDeviceStore(ROOT / "data" / "paired-devices.json")
        display_lock = DisplayOwnershipLock(
            os.environ.get("MATRIX_LOCK_FILE", "/run/matrix-display.lock")
        )
        display_lock.acquire()
        atexit.register(display_lock.release)
        matrix = Matrix()
        openweather_secrets = SecretStore(
            ROOT / "data" / "secrets" / "openweather-api-key"
        )
        controller = DisplayController(
            matrix, config_store, openweather_secrets=openweather_secrets
        )
        pairing = PairingManager(ttl_seconds=600)
        session_secret = load_secret(ROOT / "data" / "session.key")
        sanitizer_path = os.environ.get("MATRIX_SANITIZER_PATH")
        nobody = pwd.getpwnam("nobody") if sanitizer_path else None
        app = create_app(
            controller,
            config_store,
            pairing,
            secret_key=session_secret,
            session_generation=session_secret,
            device_store=device_store,
            openweather_secrets=openweather_secrets,
            sanitizer_path=sanitizer_path,
            sanitizer_python=os.environ.get("MATRIX_SANITIZER_PYTHON"),
            sanitizer_identity=(nobody.pw_uid, nobody.pw_gid) if nobody else None,
        )
        atexit.register(app.extensions["close_edit_preview_services"])

        # With no trusted device, show a fresh short-lived code. Once devices exist,
        # keep the Matrix running across service restarts; their signed sessions remain
        # valid until they expire, unpair, or are removed.
        initialize_pairing_display(controller, pairing, device_store)
        atexit.register(controller.stop)
        runtime = {
            "app": app,
            "config_store": config_store,
            "controller": controller,
            "device_store": device_store,
            "display_lock": display_lock,
            "matrix": matrix,
            "nobody": nobody,
            "openweather_secrets": openweather_secrets,
            "pairing": pairing,
            "sanitizer_path": sanitizer_path,
            "session_secret": session_secret,
        }
        _entrypoint_owner._wsgi_runtime = runtime
    globals().update(runtime)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("MATRIX_HOST", "0.0.0.0"),
        port=int(os.environ.get("MATRIX_PORT", "5000")),
        debug=False,
        threaded=True,
        use_reloader=False,
    )
