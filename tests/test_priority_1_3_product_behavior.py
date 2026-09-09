import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from config_store import DEFAULT_CONFIG
from device_preview import (
    DevicePreviewManager,
    DevicePreviewOwner,
    DevicePreviewRequest,
    PreviewAdmissionStatus,
    PreviewHeartbeatStatus,
)
from display_controller import DisplayController
from edit_preview import CaptureMatrix


class Priority13ProductBehaviorTests(unittest.TestCase):
    def test_unbounded_preview_replays_finite_renderer_until_stop(self):
        request = DevicePreviewRequest(
            owner=DevicePreviewOwner("paired-device", "browser-preview"),
            generation=1,
            config={
                "module": "weather",
                "candidate": copy.deepcopy(DEFAULT_CONFIG),
            },
            timeout_seconds=None,
        )
        stop = threading.Event()
        durations = []
        module = Mock()

        def render_once(_matrix, _config, *, duration, stop_event, **_options):
            durations.append(duration)
            if len(durations) == 3:
                stop_event.set()
            return None

        module.run.side_effect = render_once
        controller = DisplayController(Mock(), Mock())
        with patch("display_controller.importlib.import_module", return_value=module):
            rendered = controller.render_device_draft(
                Mock(), request, stop_event=stop,
            )

        self.assertIsNone(rendered)
        self.assertEqual(durations, [3600] * 3)

    def test_unbounded_lease_and_real_clock_renderer_continue_until_stop(self):
        now = [100.0]
        manager = DevicePreviewManager(clock=lambda: now[0])
        owner = DevicePreviewOwner("paired-device", "browser-preview")
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        request = DevicePreviewRequest(
            owner=owner,
            generation=1,
            config={
                "module": "clock_lava",
                "candidate": candidate,
                "selected_lava_palette": None,
            },
            timeout_seconds=None,
        )

        admission = manager.submit(request)
        self.assertIs(admission.status, PreviewAdmissionStatus.ACCEPTED)
        self.assertIsNotNone(admission.lease)
        assert admission.lease is not None
        self.assertEqual(admission.lease.content_deadline, float("inf"))

        for heartbeat_at in range(105, 175, 5):
            now[0] = float(heartbeat_at)
            heartbeat = manager.heartbeat(owner, generation=1)
            self.assertIs(heartbeat.status, PreviewHeartbeatStatus.ACCEPTED)
            self.assertIsNotNone(heartbeat.lease)
            assert heartbeat.lease is not None
            self.assertEqual(heartbeat.lease.content_deadline, float("inf"))

        class StopAfterThreeFrames(threading.Event):
            def __init__(self):
                super().__init__()
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits >= 3:
                    self.set()
                    return True
                return False

        stop = StopAfterThreeFrames()
        matrix = CaptureMatrix()
        controller = DisplayController(Mock(), Mock())
        rendered = controller.render_device_draft(
            matrix, admission.lease.request, stop_event=stop,
        )

        self.assertIsNone(rendered)
        self.assertTrue(stop.is_set())
        self.assertEqual(stop.waits, 3)

    def test_process_loss_expires_active_lava_and_restores_saved_playback_for_later_owner(self):
        now = [200.0]
        splash = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "splash"
        ))
        splash["duration"] = 1
        saved_config = {
            **copy.deepcopy(DEFAULT_CONFIG),
            "playlist": [splash],
        }
        matrix = CaptureMatrix()
        first_frame = threading.Event()
        capture_swap = matrix.swap

        def observe_swap(canvas=None):
            result = capture_swap(canvas)
            first_frame.set()
            return result

        matrix.swap = observe_swap
        setattr(matrix, "set_brightness", Mock())
        controller = DisplayController(
            matrix,
            Mock(
                get=Mock(return_value=saved_config),
                media_root=Path(__file__).parents[1] / "resources" / "media",
            ),
        )
        controller.device_preview_manager._clock = lambda: now[0]
        controller.start = Mock()
        first_owner = DevicePreviewOwner("paired-device", "lost-browser")
        first = controller.submit_device_draft(
            DevicePreviewRequest(
                owner=first_owner,
                generation=1,
                config={
                    "module": "clock_lava",
                    "candidate": copy.deepcopy(DEFAULT_CONFIG),
                    "selected_lava_palette": None,
                },
                timeout_seconds=None,
            ),
            renderer=controller.render_device_draft,
        )
        self.assertIs(first.status, PreviewAdmissionStatus.ACCEPTED)

        rendered = threading.Thread(target=controller._execute_pending_device_draft)
        rendered.start()
        self.assertTrue(first_frame.wait(1), "real Lava renderer did not publish a frame")
        self.assertEqual(controller.current_module, "device-draft:clock_lava")

        now[0] = 215.0
        rendered.join(1)
        self.assertFalse(rendered.is_alive(), "connectivity expiry left the Lava renderer active")
        deadline = time.monotonic() + 1
        while controller.device_preview_manager._closing is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(controller.device_preview_manager._closing)
        self.assertIsNone(controller.device_preview_manager.active_lease())
        self.assertIsNone(controller.current_module)
        self.assertTrue(controller._reload_requested.is_set())

        saved = []

        def execute_saved(name, config, duration):
            del config
            saved.append((name, duration))
            controller._shutdown.set()
            return True

        controller._is_active = Mock(return_value=True)
        controller._execute = execute_saved
        controller._run_loop_unchecked()
        self.assertEqual(saved, [("splash", 1)])

        later = controller.submit_device_draft(
            DevicePreviewRequest(
                owner=DevicePreviewOwner("paired-device-2", "replacement-browser"),
                generation=1,
                config={"module": "weather", "candidate": copy.deepcopy(DEFAULT_CONFIG)},
                timeout_seconds=None,
            ),
            renderer=Mock(),
        )
        self.assertIs(later.status, PreviewAdmissionStatus.ACCEPTED)
        controller.shutdown_device_drafts()


if __name__ == "__main__":
    unittest.main()
