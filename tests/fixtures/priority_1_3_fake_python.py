#!/usr/bin/python3
"""Harmless staged interpreter fixture using the real acceptance parser and publisher."""
from __future__ import annotations

import os
import http.cookiejar
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.environ["FIXTURE_EXPORT"])
from config_store import ConfigStore
from deploy import verify_priority_1_3_acceptance as acceptance
from device_preview import PreviewAdmissionStatus
from display_controller import DisplayController
from pairing import PairedDeviceStore, PairingManager
from webapp import create_app
from werkzeug.serving import WSGIRequestHandler, make_server

root = Path(os.environ["FIXTURE_ROOT"])
real_executor = acceptance.PinnedRemoteExecutor


def fixture_executor(*args, **kwargs):
    def runner(argv, **run_kwargs):
        return subprocess.run([sys.executable, str(root / "bin/ssh"), *argv[1:]], **run_kwargs)

    return real_executor(*args, runner=runner, **kwargs)


acceptance.PinnedRemoteExecutor = fixture_executor
assert sys.argv[1:4] == [
    "-I", "-B", str(root / "stage/deploy/verify_priority_1_3_acceptance.py")
]
arguments = acceptance.build_parser().parse_args(sys.argv[4:])
os.environ["EXPECTED_ACCEPTANCE_OPERATION_ID"] = arguments.acceptance_operation_id
app_root = Path(arguments.app_root)
data = app_root / "data"
data.mkdir(parents=True, exist_ok=True)
(data / "acceptance-runtime").mkdir(mode=0o700, exist_ok=True)
(data / "acceptance-runtime").chmod(0o700)
authorization = data / "acceptance-authorization.key"
authorization.write_text("6" * 64, encoding="ascii")
authorization.chmod(0o600)


class DeterministicSwapMatrix:
    """Production observer seam with exact complete 64x32 RGB swap frames."""

    def __init__(self):
        self.frame = bytes([17]) * (64 * 32 * 3)
        self.revision = 10
        self.observer = None
        self.after_sleep = None

    def latest_frame_rgb(self):
        return self.frame, self.revision

    def set_physical_swap_observer(self, observer):
        self.observer = observer

    def set_brightness(self, value):
        self.brightness = value

    def show_gif(self, filename, **_options):
        assert Path(filename).name == "sleep.gif"
        self.physical_swap(bytes([17]) * (64 * 32 * 3))
        if self.after_sleep is not None:
            self.after_sleep()
        return True

    def physical_swap(self, frame):
        assert type(frame) is bytes and len(frame) == 64 * 32 * 3
        self.frame = frame
        self.revision += 1
        if self.observer is not None:
            self.observer(frame, self.revision)


class AcceptanceController(DisplayController):
    """Real controller lifecycle with deterministic physical frame scheduling."""

    def __init__(self, matrix, store):
        super().__init__(matrix, store)
        self._fixture_clock_real = time.monotonic()
        self._fixture_clock_value = self._fixture_clock_real
        self._fixture_clock_scale = 1
        self.device_preview_manager._clock = self._fixture_clock
        self._saved_swap_pending = False
        self._draft_workers = []
        self._draft_worker_errors = []

    def _fixture_clock(self):
        return self._fixture_clock_value + (
            time.monotonic() - self._fixture_clock_real
        ) * self._fixture_clock_scale

    def _accelerate_fixture_clock(self):
        current = self._fixture_clock()
        self._fixture_clock_real = time.monotonic()
        self._fixture_clock_value = current
        self._fixture_clock_scale = 2

    def start(self):
        return True

    def end_pairing(self, generation=None, timeout=None):
        return True

    def submit_device_draft(self, request, *, renderer):
        del renderer
        draft_published = threading.Event()
        worker_finished = threading.Event()

        def deterministic_renderer(matrix, claimed_request, *, stop_event):
            # Keep the deterministic fixture frame, but publish it only from the
            # real pending-draft scheduler claim while its exact lease token is
            # active. Natural content expiry releases this renderer.
            matrix.physical_swap(bytes([91]) * (64 * 32 * 3))
            self._accelerate_fixture_clock()
            draft_published.set()
            # Poll the real manager so its deterministic 2x fixture clock reaches
            # the exact content deadline through the normal autonomous expiry
            # path. Five-second verifier heartbeats still precede connectivity
            # expiry and renew it beyond the content deadline.
            while not stop_event.wait(0.05):
                self.device_preview_manager.active_lease()
            return True

        def execute_claimed_draft():
            try:
                if not self._execute_pending_device_draft():
                    raise RuntimeError("deterministic draft was not claimed")
            except BaseException as exc:
                self._draft_worker_errors.append(exc)
            finally:
                worker_finished.set()

        admission = super().submit_device_draft(
            request, renderer=deterministic_renderer,
        )
        if admission.status is PreviewAdmissionStatus.ACCEPTED:
            worker = threading.Thread(target=execute_claimed_draft, daemon=True)
            self._draft_workers.append(worker)
            worker.start()
            if not draft_published.wait(5):
                worker_finished.wait(1)
                if self._draft_worker_errors:
                    raise RuntimeError("deterministic draft execution failed") from self._draft_worker_errors[0]
                raise TimeoutError("deterministic draft publication deadline expired")
        return admission

    def _expire_device_draft(self, lease):
        super()._expire_device_draft(lease)
        self._saved_swap_pending = True

    def acceptance_transaction_status(self, owner, *, generation):
        # The status poll after natural content expiry runs the real scheduler
        # inactive branch and real sleep-module dispatch through deterministic
        # hardware primitives before reading status.
        if self._draft_worker_errors:
            raise RuntimeError("deterministic draft execution failed") from self._draft_worker_errors[0]
        if self._saved_swap_pending:
            self._saved_swap_pending = False
            self._shutdown.clear()
            self.matrix.after_sleep = self._shutdown.set
            try:
                self._run_loop_unchecked()
            finally:
                self.matrix.after_sleep = None
                self._shutdown.clear()
        return super().acceptance_transaction_status(owner, generation=generation)


matrix = DeterministicSwapMatrix()
store = ConfigStore(data / "config.json")
devices = PairedDeviceStore(data / "paired-devices.json")
controller = AcceptanceController(matrix, store)
pairing = PairingManager(testing_pin="731946")


class FixtureAcceptanceTime:
    """Keep client heartbeat scheduling in the controller's accelerated clock domain."""

    monotonic = staticmethod(controller._fixture_clock)
    sleep = staticmethod(time.sleep)


acceptance.time = FixtureAcceptanceTime
flask_app = create_app(
    controller, store, pairing,
    secret_key="guarded-export-acceptance-fixture",
    session_generation="guarded-export-generation",
    media_dir=app_root / "resources/media",
    device_store=devices,
    acceptance_authorization_file=authorization,
    acceptance_runtime=data / "acceptance-runtime",
)
class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        return None

    def log_error(self, *args, **kwargs):
        return None

server = make_server("127.0.0.1", 5000, flask_app, threaded=True,
                     request_handler=QuietRequestHandler)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()

# Reuse the production-style persistent paired session. Pair only when the
# supplied cookie is not yet signed by this app; repeated acceptance operations
# must not consume a new paired-device slot.
cookie_path = Path(arguments.session_cookie_file)
cookie = cookie_path.read_text(encoding="ascii").strip()
status_request = urllib.request.Request(
    "http://127.0.0.1:5000/api/status", headers={"Cookie": cookie}, method="GET",
)
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            status_request, timeout=5) as response:
        paired = response.status == 200
except urllib.error.HTTPError:
    paired = False
if not paired:
    pair_request = urllib.request.Request(
        "http://127.0.0.1:5000/pair",
        data=urllib.parse.urlencode({"device_name": "Acceptance fixture", "pin": "731946"}).encode(),
        method="POST",
    )
    cookie_jar = http.cookiejar.CookieJar()
    pair_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(cookie_jar),
    )
    with pair_opener.open(pair_request, timeout=5) as response:
        response.read()
    cookies = list(cookie_jar)
    assert len(cookies) == 1 and cookies[0].name == "session"
    cookie = f"{cookies[0].name}={cookies[0].value}"
    assert "\r" not in cookie and "\n" not in cookie
    cookie_path.write_text(cookie, encoding="ascii")
    cookie_path.chmod(0o600)


class ChromiumFixture:
    def run(self, *args, **kwargs):
        return {
            "paired": True, "explicit_ipv4": True,
            "status_checked": True, "frame_checked": True,
        }


executor = acceptance.PinnedRemoteExecutor(
    arguments.ssh_target, arguments.ssh_identity_file, arguments.ssh_known_hosts_file
)
try:
    result = acceptance.run_acceptance(
        arguments, executor, chromium_client=ChromiumFixture(),
    )
finally:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)
    controller.device_preview_manager.close()
    for worker in controller._draft_workers:
        worker.join(timeout=5)
        if worker.is_alive():
            raise RuntimeError("deterministic draft worker did not stop")
raise SystemExit(result)
