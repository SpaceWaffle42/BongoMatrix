import copy
import threading
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


if __name__ == "__main__":
    unittest.main()
