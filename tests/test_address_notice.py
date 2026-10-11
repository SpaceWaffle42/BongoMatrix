"""Roadmap 1.10a: the panel shows its address at start-up and when it changes."""

import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config_store import DEFAULT_CONFIG
from display_controller import AddressWatch, DisplayController, address_notice_text
from edit_preview import CaptureMatrix


class AddressWatchTests(unittest.TestCase):
    def test_start_up_announces_current_address_once(self):
        watch = AddressWatch()
        self.assertEqual(watch.start("192.168.1.5"), ("start", "192.168.1.5"))
        self.assertIsNone(watch.observe("192.168.1.5"))
        self.assertIsNone(watch.observe("192.168.1.5"))

    def test_start_up_without_network_says_so(self):
        self.assertEqual(AddressWatch().start(None), ("offline", None))

    def test_change_must_hold_for_two_readings(self):
        watch = AddressWatch()
        watch.start("192.168.1.4")
        self.assertIsNone(watch.observe("192.168.1.5"))
        self.assertEqual(watch.observe("192.168.1.5"), ("changed", "192.168.1.5"))
        self.assertIsNone(watch.observe("192.168.1.5"))

    def test_brief_drop_back_to_same_address_is_silent(self):
        watch = AddressWatch()
        watch.start("192.168.1.5")
        self.assertIsNone(watch.observe(None))
        self.assertIsNone(watch.observe("192.168.1.5"))
        self.assertIsNone(watch.observe("192.168.1.5"))

    def test_lasting_drop_then_same_address_reports_offline_then_reconnected(self):
        watch = AddressWatch()
        watch.start("192.168.1.5")
        watch.observe(None)
        self.assertEqual(watch.observe(None), ("offline", None))
        watch.observe("192.168.1.5")
        self.assertEqual(watch.observe("192.168.1.5"), ("reconnected", "192.168.1.5"))

    def test_lasting_drop_then_new_address_reports_change(self):
        watch = AddressWatch()
        watch.start("192.168.1.4")
        watch.observe(None)
        watch.observe(None)
        watch.observe("10.0.0.9")
        self.assertEqual(watch.observe("10.0.0.9"), ("changed", "10.0.0.9"))

    def test_flapping_readings_never_announce(self):
        watch = AddressWatch()
        watch.start("192.168.1.4")
        for value in ["192.168.1.5", "192.168.1.6", None, "192.168.1.7"] * 3:
            self.assertIsNone(watch.observe(value))


class NoticeTextTests(unittest.TestCase):
    def test_labels_fit_the_panel_and_carry_the_port(self):
        self.assertEqual(address_notice_text(("changed", "192.168.1.5"), 5000),
                         ("NEW ADDRESS", "192.168.1.5:5000"))
        self.assertEqual(address_notice_text(("start", "192.168.1.5"), 5000),
                         ("ADDRESS", "192.168.1.5:5000"))
        self.assertEqual(address_notice_text(("reconnected", "192.168.1.5"), 8080),
                         ("CONNECTED", "192.168.1.5:8080"))
        self.assertEqual(address_notice_text(("offline", None), 5000),
                         ("NO NETWORK", "check Wi-Fi"))
        font = CaptureMatrix.font("5x7")
        for kind in ("changed", "start", "reconnected", "offline"):
            label, _ = address_notice_text((kind, "192.168.100.200"), 5000)
            self.assertLessEqual(font.text_pixels(0, 7, label)[0], 64, label)


def active_store(playlist=None):
    config = copy.deepcopy(DEFAULT_CONFIG)
    if playlist is not None:
        config["playlist"] = playlist
    store = Mock(media_root=Path("/nonexistent"))
    store.get.return_value = config
    return store


class ControllerNoticeTests(unittest.TestCase):
    def make(self, readings, **kwargs):
        values = iter(readings)
        last = [readings[0]]

        def provider():
            try:
                last[0] = next(values)
            except StopIteration:
                pass
            return last[0]

        capture = CaptureMatrix()
        capture.set_brightness = Mock()
        controller = DisplayController(
            capture, active_store(kwargs.pop("playlist", None)),
            ip_address_provider=provider, announce_address=True, **kwargs,
        )
        return controller, capture

    def test_disabled_by_default(self):
        controller = DisplayController(Mock(), active_store())
        self.assertIsNone(controller._address_watch)

    def test_start_up_notice_renders_label_and_address_on_panel(self):
        controller, capture = self.make(["192.168.1.5"], address_port=5000)
        frames = []
        original_swap = capture.swap

        def record(canvas=None):
            frames.append(capture._capture.raw_snapshot(canvas or capture.canvas))
            return original_swap(canvas)

        capture.swap = record
        controller._queue_address_notice(controller._address_watch.start("192.168.1.5"))
        with patch("display_controller.ADDRESS_NOTICE_SECONDS", (0.2, 0.2)), \
                patch("display_controller.ADDRESS_NOTICE_FRAME_SECONDS", 0.01):
            self.assertTrue(controller._run_pending_address_notice())
        self.assertGreater(len(frames), 3)
        lit_rows = {
            index // (64 * 3) for index in range(0, len(frames[0]), 3)
            if any(frames[0][index:index + 3])
        }
        self.assertTrue(any(row < 10 for row in lit_rows), "label row lit")
        self.assertTrue(any(row > 14 for row in lit_rows), "address row lit")
        self.assertNotEqual(frames[1], frames[-1], "address scrolls")
        self.assertIsNone(controller._address_notice)

    def test_change_interrupts_running_playlist_item(self):
        controller, _ = self.make(["192.168.1.4"])
        controller._address_watch.start("192.168.1.4")
        stop = threading.Event()
        controller._module_stop = stop
        with patch.object(controller, "_is_active", return_value=True):
            controller._address_seen("192.168.1.5")
            self.assertFalse(stop.is_set(), "one reading is not enough")
            controller._address_seen("192.168.1.5")
        self.assertTrue(stop.is_set())
        self.assertEqual(controller._address_notice, ("changed", "192.168.1.5"))

    def test_change_never_interrupts_pairing_or_device_draft(self):
        controller, _ = self.make(["192.168.1.4"])
        controller._address_watch.start("192.168.1.4")
        stop = threading.Event()
        controller._module_stop = stop
        controller._pairing_pause.set()
        controller._address_seen("192.168.1.5")
        controller._address_seen("192.168.1.5")
        self.assertFalse(stop.is_set())
        self.assertEqual(controller._address_notice, ("changed", "192.168.1.5"))
        controller._pairing_pause.clear()
        controller._device_draft_active_lease = object()
        self.assertFalse(controller._address_notice_may_interrupt())

    def test_sleep_hours_keep_notice_until_wake(self):
        controller, _ = self.make(["192.168.1.4"])
        controller._address_watch.start("192.168.1.4")
        stop = threading.Event()
        controller._module_stop = stop
        with patch.object(controller, "_is_active", return_value=False):
            controller._address_seen("192.168.1.5")
            controller._address_seen("192.168.1.5")
        self.assertFalse(stop.is_set())
        self.assertEqual(controller._address_notice, ("changed", "192.168.1.5"))

    def test_scheduler_shows_pending_notice_before_next_playlist_item(self):
        playlist = [{"module": "clock_lava", "duration": 5, "enabled": True}]
        controller, _ = self.make(["192.168.1.5"], playlist=playlist)
        controller._queue_address_notice(("changed", "192.168.1.5"))
        order = []

        def notice():
            order.append("notice")
            controller._address_notice = None
            return True

        def execute(name, *_args, **_kwargs):
            order.append(name)
            controller._shutdown.set()
            return True

        with patch.object(controller, "_is_active", return_value=True), \
                patch.object(controller, "_run_pending_address_notice", side_effect=notice), \
                patch.object(controller, "_execute", side_effect=execute):
            controller._run_loop_unchecked()
        self.assertEqual(order, ["notice", "clock_lava"])

    def test_playlist_breaks_out_when_notice_arrives_mid_cycle(self):
        playlist = [
            {"module": "clock_lava", "duration": 5, "enabled": True},
            {"module": "pet_cat", "duration": 5, "enabled": True},
        ]
        controller, _ = self.make(["192.168.1.5"], playlist=playlist)
        order = []

        def notice():
            if controller._address_notice is None:
                return False
            order.append("notice")
            controller._address_notice = None
            controller._shutdown.set()
            return True

        def execute(name, *_args, **_kwargs):
            order.append(name)
            controller._queue_address_notice(("changed", "192.168.1.9"))
            return True

        with patch.object(controller, "_is_active", return_value=True), \
                patch.object(controller, "_run_pending_address_notice", side_effect=notice), \
                patch.object(controller, "_execute", side_effect=execute):
            controller._run_loop_unchecked()
        self.assertEqual(order, ["clock_lava", "notice"])

    def test_watcher_thread_follows_controller_lifecycle(self):
        controller, _ = self.make(["192.168.1.5"])
        with patch("display_controller.ADDRESS_POLL_SECONDS", 0.01), \
                patch.object(controller, "_run_loop"):
            controller.start()
            watcher = controller._address_thread
            self.assertTrue(watcher.is_alive())
            for _ in range(100):
                if controller._address_notice is not None:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(controller._address_notice, ("start", "192.168.1.5"))
            controller._shutdown.set()
            watcher.join(1)
            self.assertFalse(watcher.is_alive())

    def test_provider_failure_counts_as_no_network(self):
        controller, _ = self.make(["192.168.1.5"])
        controller._ip_address_provider = Mock(side_effect=OSError("down"))
        self.assertIsNone(controller._read_address())
        controller._ip_address_provider = lambda: "IP unavailable"
        self.assertIsNone(controller._read_address())


if __name__ == "__main__":
    unittest.main()
