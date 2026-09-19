import base64
import copy
import datetime
import io
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import display_controller
import edit_preview
from edit_preview import CaptureMatrix
from config_store import DEFAULT_CONFIG
from display_controller import DisplayController
from media_resolver import PinnedMediaHandle
from preview_provider import PreviewProvider, PreviewProviderCancelled


def blocked_physical_openweather_provider(settings, _credentials, timeout):
    """Spawn-safe provider that cannot return before its parent must cancel it."""
    marker = settings["_test_entered_marker"]
    with open(marker, "x", encoding="ascii") as handle:
        handle.write("entered")
    time.sleep(timeout + 1)
    return {"error": "released unexpectedly", "cancelled": False}


class DisplayControllerTests(unittest.TestCase):
    def test_pairing_display_alternates_between_pin_and_ip_screens(self):
        matrix = Mock()
        timers = []

        class FakeTimer:
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.daemon = False
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        with patch("display_controller.threading.Timer", FakeTimer):
            controller = DisplayController(
                matrix,
                Mock(),
                ip_address_provider=lambda: "192.168.1.4",
            )
            controller.show_pairing_pin("123456", ttl_seconds=60)
            first_screen = [
                call.args[5] for call in matrix.draw_text_with_border.call_args_list
            ]
            self.assertIn("123456", first_screen)

            rotation = next(timer for timer in timers if timer.interval == 3)
            matrix.draw_text_with_border.reset_mock()
            rotation.callback()
            second_screen = [
                call.args[5] for call in matrix.draw_text_with_border.call_args_list
            ]
            self.assertIn("PAIR IP", second_screen)
            self.assertIn("192.168.1.4", second_screen)
            self.assertIn("PORT 5000", second_screen)

            controller.end_pairing()
            self.assertTrue(all(timer.cancelled for timer in timers))


    def test_show_pairing_pin_draws_pin_on_matrix(self):
        matrix = Mock()
        matrix.matrix.width = 64
        controller = DisplayController(matrix, Mock())
        controller.show_pairing_pin("123456", ttl_seconds=60)
        self.assertTrue(controller.pairing_active)
        matrix.clear.assert_called()
        texts = [call.args[5] for call in matrix.draw_text_with_border.call_args_list]
        self.assertIn("PAIR", texts)
        self.assertIn("123456", texts)
        matrix.swap.assert_called()
        controller.end_pairing()

    def test_show_pairing_pin_rejects_shutdown_controller(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        controller._shutdown.set()

        with self.assertRaisesRegex(RuntimeError, "pairing display unavailable"):
            controller.show_pairing_pin("123456", ttl_seconds=60)

        self.assertFalse(controller.pairing_active)
        matrix.swap.assert_not_called()

    def test_show_pairing_pin_rejects_shutdown_after_initial_admission(self):
        matrix = Mock()
        provider_started = threading.Event()
        release_provider = threading.Event()
        failures = []

        def stalled_ip_provider():
            provider_started.set()
            self.assertTrue(release_provider.wait(timeout=2))
            return "192.168.1.4"

        controller = DisplayController(
            matrix, Mock(), ip_address_provider=stalled_ip_provider,
        )

        def show_pin():
            try:
                controller.show_pairing_pin("123456", ttl_seconds=600)
            except Exception as error:
                failures.append(error)

        display_thread = threading.Thread(target=show_pin)
        display_thread.start()
        self.assertTrue(provider_started.wait(timeout=2))
        self.assertFalse(controller.stop(timeout=1))
        release_provider.set()
        display_thread.join(timeout=2)

        self.assertFalse(display_thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        self.assertEqual(str(failures[0]), "pairing display unavailable")
        matrix.draw_text.assert_not_called()

    def test_show_pairing_pin_rejects_shutdown_during_admitted_first_draw(self):
        matrix = Mock()
        swap_started = threading.Event()
        release_swap = threading.Event()
        failures = []

        def blocked_swap():
            swap_started.set()
            self.assertTrue(release_swap.wait(timeout=3))

        matrix.swap.side_effect = blocked_swap
        controller = DisplayController(
            matrix, Mock(), ip_address_provider=lambda: "192.168.1.4",
        )

        def show_pin():
            try:
                controller.show_pairing_pin("123456", ttl_seconds=600)
            except Exception as error:
                failures.append(error)

        display_thread = threading.Thread(target=show_pin)
        display_thread.start()
        self.assertTrue(swap_started.wait(timeout=2))
        self.assertFalse(controller.stop(timeout=1))
        release_swap.set()
        display_thread.join(timeout=2)

        self.assertFalse(display_thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        self.assertEqual(str(failures[0]), "pairing display unavailable")
        self.assertFalse(controller.pairing_active)

    def test_show_pairing_pin_rejects_expiry_during_admitted_first_draw(self):
        matrix = Mock()
        swap_started = threading.Event()
        release_swap = threading.Event()
        timers = []
        failures = []

        class FakeTimer:
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.daemon = False
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        def blocked_swap():
            swap_started.set()
            self.assertTrue(release_swap.wait(timeout=3))

        matrix.swap.side_effect = blocked_swap
        controller = DisplayController(
            matrix, Mock(), ip_address_provider=lambda: "192.168.1.4",
        )

        def show_pin():
            try:
                controller.show_pairing_pin("123456", ttl_seconds=600)
            except Exception as error:
                failures.append(error)

        with patch("display_controller.threading.Timer", FakeTimer):
            display_thread = threading.Thread(target=show_pin)
            display_thread.start()
            self.assertTrue(swap_started.wait(timeout=2))
            self.assertEqual(len(timers), 2)
            expiry_thread = threading.Thread(target=timers[0].callback)
            expiry_thread.start()
            deadline = time.monotonic() + 2
            while controller.pairing_active and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertFalse(controller.pairing_active)
            release_swap.set()
            display_thread.join(timeout=2)
            expiry_thread.join(timeout=2)

        self.assertFalse(display_thread.is_alive())
        self.assertFalse(expiry_thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        self.assertEqual(str(failures[0]), "pairing display unavailable")

    def test_stale_pairing_timer_cannot_draw_after_pairing_ends(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        controller._pairing_ip = "192.168.1.4"

        controller._draw_pairing_page(show_ip=True)

        matrix.clear.assert_not_called()
        matrix.swap.assert_not_called()

    def test_end_pairing_waits_for_draw_that_already_passed_generation_check(self):
        matrix = Mock()
        draw_started = threading.Event()
        release_draw = threading.Event()
        end_returned = threading.Event()

        def blocked_clear():
            draw_started.set()
            release_draw.wait(1)

        matrix.clear.side_effect = blocked_clear
        controller = DisplayController(matrix, Mock())
        controller._pairing_pause.set()
        controller._pairing_generation = 1
        controller._pairing_pin = "123456"

        draw = threading.Thread(
            target=controller._draw_pairing_page,
            kwargs={"show_ip": False, "generation": 1},
        )
        draw.start()
        self.assertTrue(draw_started.wait(0.5))

        ending = threading.Thread(
            target=lambda: (controller.end_pairing(1), end_returned.set())
        )
        ending.start()
        self.assertFalse(end_returned.wait(0.1))

        release_draw.set()
        draw.join(1)
        ending.join(1)
        self.assertFalse(draw.is_alive())
        self.assertFalse(ending.is_alive())
        self.assertTrue(end_returned.is_set())
        self.assertFalse(controller.pairing_active)

    def test_stale_pairing_callbacks_cannot_end_or_draw_a_new_generation(self):
        matrix = Mock()
        timers = []

        class FakeTimer:
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.daemon = False
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        with patch("display_controller.threading.Timer", FakeTimer):
            controller = DisplayController(matrix, Mock())
            controller.show_pairing_pin("111111", ttl_seconds=60)
            old_expiry, old_rotation = timers[:2]
            controller.show_pairing_pin("222222", ttl_seconds=60)

            matrix.reset_mock()
            old_expiry.callback()
            old_rotation.callback()

            self.assertTrue(controller.pairing_active)
            self.assertEqual(controller._pairing_pin, "222222")
            matrix.clear.assert_not_called()
            matrix.swap.assert_not_called()

            new_expiry = timers[2]
            new_expiry.callback()
            self.assertFalse(controller.pairing_active)

    def test_old_expiry_cannot_cancel_replacement_pairing_during_setup(self):
        matrix = Mock()
        timers = []
        replacement_interrupt_started = threading.Event()
        release_replacement = threading.Event()

        class FakeTimer:
            def __init__(self, interval, callback):
                self.callback = callback
                self.daemon = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                pass

        with patch("display_controller.threading.Timer", FakeTimer):
            controller = DisplayController(matrix, Mock())
            controller.show_pairing_pin("111111", ttl_seconds=60)
            old_expiry = timers[0]
            original_cancel_and_quiesce = controller._cancel_and_quiesce

            def block_replacement_interrupt(**_kwargs):
                if _kwargs.get("wait", True):
                    return original_cancel_and_quiesce(**_kwargs)
                replacement_interrupt_started.set()
                release_replacement.wait(1)
                return True

            controller._cancel_and_quiesce = block_replacement_interrupt
            replacement = threading.Thread(
                target=lambda: controller.show_pairing_pin("222222", ttl_seconds=60)
            )
            replacement.start()
            self.assertTrue(replacement_interrupt_started.wait(0.2))

            old_expiry.callback()
            release_replacement.set()
            replacement.join(1)

        self.assertFalse(replacement.is_alive())
        self.assertTrue(controller.pairing_active)
        self.assertEqual(controller._pairing_pin, "222222")
        texts = [call.args[5] for call in matrix.draw_text_with_border.call_args_list]
        self.assertIn("222222", texts)

    def test_unknown_preview_is_rejected(self):
        store = Mock()
        store.get.return_value = {"custom_modules": []}
        controller = DisplayController(Mock(), store)
        with self.assertRaises(KeyError):
            controller.preview("unknown")

    def test_announcement_board_executes_through_builtin_controller_path(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        module = Mock()
        module.run.return_value = False
        config = {"custom_modules": [], "announcement_board": {"announcements": []}}

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute("announcement_board", config, 0)

        self.assertFalse(result)
        importer.assert_called_once_with("modules.announcement_board")
        module.run.assert_called_once()
        self.assertIs(module.run.call_args.args[0], matrix)
        self.assertIs(module.run.call_args.args[1], config)
        self.assertEqual(module.run.call_args.kwargs["duration"], 0)
        self.assertIsNotNone(module.run.call_args.kwargs["stop_event"])

    def test_scheduler_continues_to_next_playlist_item_when_board_has_no_active_record(self):
        config = {
            **DEFAULT_CONFIG,
            "playlist": [
                {"module": "announcement_board", "duration": 0, "enabled": True},
                {"module": "clock_lava", "duration": 1, "enabled": True},
            ],
        }
        store = Mock()
        store.get.return_value = config
        controller = DisplayController(Mock(), store)
        calls = []

        def execute(name, *_args):
            calls.append(name)
            if name == "clock_lava":
                controller._shutdown.set()
            return name != "announcement_board"

        controller._execute = Mock(side_effect=execute)
        with patch.object(controller, "_is_active", return_value=True):
            controller._run_loop_unchecked()

        self.assertEqual(calls, ["announcement_board", "clock_lava"])

    def test_pending_physical_draft_preempts_next_saved_playlist_row(self):
        splash = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "splash"
        ))
        sleep = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "sleep"
        ))
        splash["duration"] = sleep["duration"] = 1
        config = {
            **DEFAULT_CONFIG,
            "playlist": [splash, sleep],
        }
        store = Mock(
            get=Mock(return_value=config),
            media_root=Path(__file__).parents[1] / "resources" / "media",
        )
        controller = DisplayController(Mock(), store)
        observed = []

        def execute(name, _config, _duration):
            observed.append(f"saved:{name}")
            if name == "splash":
                with controller._state_lock:
                    controller._device_draft_pending = (None, None)
            return True

        def execute_draft():
            observed.append("draft")
            with controller._state_lock:
                controller._device_draft_pending = None
            controller._shutdown.set()
            return True

        controller._execute = Mock(side_effect=execute)
        controller._execute_pending_device_draft = Mock(side_effect=execute_draft)
        with patch.object(controller, "_is_active", return_value=True):
            controller._run_loop_unchecked()

        self.assertEqual(observed, ["saved:splash", "draft"])
        controller._execute_pending_device_draft.assert_called_once_with()

    def test_builtin_device_draft_consumes_pinned_inode_after_path_replacement(self):
        for module_name in ("splash", "sleep"):
            with self.subTest(module=module_name), tempfile.TemporaryDirectory() as directory:
                media_root = Path(directory)
                filename = f"{module_name}.bin"
                path = media_root / filename
                path.write_bytes(b"original-inode:" + module_name.encode("ascii"))
                handle = PinnedMediaHandle(os.open(path, os.O_RDONLY), lambda _handle: None)
                replacement = media_root / "replacement"
                replacement.write_bytes(b"replacement-path")
                os.replace(replacement, path)
                candidate = copy.deepcopy(DEFAULT_CONFIG)
                candidate["media"][module_name] = filename
                playlist_item = next(
                    item for item in candidate["playlist"]
                    if item["module"] == module_name
                )
                playlist_item["source"] = filename
                playlist_item["sources"] = [filename]
                request = Mock(
                    config={"module": module_name, "candidate": candidate},
                    timeout_seconds=3,
                    pinned_media={filename: handle},
                )
                consumed = []
                module = Mock()

                def run(_matrix, _candidate, *, duration, stop_event, pinned_handle):
                    del duration, stop_event
                    with pinned_handle.dup_file() as source:
                        consumed.append(source.read())
                    return True

                module.run.side_effect = run
                try:
                    controller = DisplayController(Mock(), Mock())
                    with patch("display_controller.importlib.import_module", return_value=module):
                        self.assertTrue(controller.render_device_draft(
                            Mock(), request, stop_event=threading.Event(),
                        ))
                    self.assertEqual(consumed, [b"original-inode:" + module_name.encode("ascii")])
                    self.assertEqual(path.read_bytes(), b"replacement-path")
                finally:
                    handle.close()

    def test_announcement_device_draft_renders_disabled_out_of_schedule_content(self):
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["announcement_board"]["announcements"] = [{
            "id": "announcement_device_draft",
            "enabled": False,
            "layout": "text_notice",
            "headline": "UNSAVED DRAFT",
            "message": "Physical preview",
            "media": None,
            "schedule": {
                "start_date": "2099-01-01",
                "end_date": "2099-01-01",
                "days": [0],
                "start_time": "09:00",
                "end_time": "17:00",
            },
            "style": {
                "foreground": "#ffffff",
                "background": "#000000",
                "headline_font": "5x7",
                "message_font": "4x6",
                "alignment": "left",
                "border": "none",
                "border_colour": "#ffffff",
            },
            "timing": {"mode": "static", "duration_seconds": 8},
            "transition": {"type": "cut"},
        }]
        request = Mock(
            config={"module": "announcement_board", "candidate": candidate},
            timeout_seconds=1,
            pinned_media={},
        )
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.side_effect = lambda name: f"font:{name}"
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": Mock(red=255, green=255, blue=255),
            "#000000": Mock(red=0, green=0, blue=0),
        }[value]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False

        rendered = DisplayController(Mock(), Mock()).render_device_draft(
            matrix, request, stop_event=stop,
        )

        self.assertTrue(rendered)
        matrix.swap.assert_called_once_with("canvas")

    def test_announcement_device_draft_consumes_admitted_media_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "announcement.png"
            path.write_bytes(b"admitted-inode")
            handle = PinnedMediaHandle(os.open(path, os.O_RDONLY), lambda _handle: None)
            replacement = Path(directory) / "replacement.png"
            replacement.write_bytes(b"replacement-path")
            os.replace(replacement, path)
            candidate = copy.deepcopy(DEFAULT_CONFIG)
            candidate["announcement_board"]["announcements"] = [{
                "id": "announcement_media_draft",
                "enabled": False,
                "layout": "icon_notice",
                "headline": "MEDIA DRAFT",
                "message": "Pinned preview",
                "media": "announcement.png",
                "schedule": {
                    "start_date": "2099-01-01",
                    "end_date": "2099-01-01",
                    "days": [0],
                    "start_time": "09:00",
                    "end_time": "17:00",
                },
                "style": {
                    "foreground": "#ffffff",
                    "background": "#000000",
                    "headline_font": "5x7",
                    "message_font": "4x6",
                    "alignment": "left",
                    "border": "none",
                    "border_colour": "#ffffff",
                },
                "timing": {"mode": "static", "duration_seconds": 8},
                "transition": {"type": "cut"},
            }]
            request = Mock(
                config={"module": "announcement_board", "candidate": candidate},
                timeout_seconds=1,
                pinned_media={"announcement.png": handle},
            )
            consumed = []
            matrix = Mock()
            matrix.matrix.CreateFrameCanvas.return_value = "canvas"
            matrix.font.side_effect = lambda name: f"font:{name}"
            matrix.colour_rgb.side_effect = lambda value: {
                "#ffffff": Mock(red=255, green=255, blue=255),
                "#000000": Mock(red=0, green=0, blue=0),
            }[value]
            matrix.swap.return_value = "canvas"

            def media_frames(_reference, *_args, pinned_handle=None, **_kwargs):
                if pinned_handle is None:
                    consumed.append(path.read_bytes())
                else:
                    with pinned_handle.dup_file() as source:
                        consumed.append(source.read())
                return []

            matrix.media_frames.side_effect = media_frames
            stop = Mock()
            stop.is_set.return_value = False
            stop.wait.return_value = False
            try:
                self.assertTrue(DisplayController(Mock(), Mock()).render_device_draft(
                    matrix, request, stop_event=stop,
                ))
                self.assertEqual(consumed, [b"admitted-inode"])
            finally:
                handle.close()

    def test_scheduler_idles_when_announcement_only_playlist_has_no_active_record(self):
        config = {
            **DEFAULT_CONFIG,
            "playlist": [
                {"module": "announcement_board", "duration": 0, "enabled": True},
            ],
        }
        store = Mock()
        store.get.return_value = config
        controller = DisplayController(Mock(), store)
        controller._execute = Mock(return_value=False)
        original_wait = controller._shutdown.wait
        waits = []

        def stop_after_idle(seconds):
            waits.append(seconds)
            controller._shutdown.set()
            return original_wait(0)

        with (
            patch.object(controller, "_is_active", return_value=True),
            patch.object(controller._shutdown, "wait", side_effect=stop_after_idle),
        ):
            controller._run_loop_unchecked()

        controller._execute.assert_called_once_with("announcement_board", config, 0)
        self.assertEqual(waits, [0.25])

    def test_scheduler_idles_when_splash_gif_admits_no_frame(self):
        from matrix_framework import Matrix

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        matrix._publication_guard = lambda: False
        matrix.clear = Mock()
        matrix.draw_image = Mock()
        matrix.set_brightness = Mock()
        matrix._media_file = Mock(return_value=Mock())
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["media"]["splash"] = "custom.gif"
        config["playlist"] = [
            {"module": "splash", "duration": 1, "enabled": True},
        ]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = Mock()
        store.media_root = Path(temporary.name)
        (store.media_root / "custom.gif").write_bytes(b"fixture")
        store.get.return_value = config
        controller = DisplayController(matrix, store)
        original_wait = controller._shutdown.wait
        waits = []

        def stop_after_idle(seconds):
            waits.append(seconds)
            controller._shutdown.set()
            return original_wait(0)

        with (
            patch.object(controller, "_is_active", return_value=True),
            patch.object(controller._shutdown, "wait", side_effect=stop_after_idle),
            patch(
                "modules.media_player.gif_media_frames",
                return_value=[(Mock(), 0.0)],
            ),
        ):
            controller._run_loop_unchecked()

        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()
        self.assertEqual(waits, [0.25])

    def test_sleep_scheduler_waits_interruptibly_when_every_identity_is_unavailable(self):
        from screen_media import ScreenMediaRuntime

        config = copy.deepcopy(DEFAULT_CONFIG)
        sleep = next(item for item in config["playlist"] if item["module"] == "sleep")
        sleep.update(source="missing.gif", sources=["missing.gif"])
        config["playlist"] = [sleep]
        store = Mock(media_root=Path("/nonexistent"))
        controller = DisplayController(Mock(), store)
        reads = 0

        def get_config():
            nonlocal reads
            reads += 1
            if reads == 2:
                controller._shutdown.set()
            return config

        store.get = get_config
        controller._screen_runtime = ScreenMediaRuntime(catalog=lambda _screen: {})
        controller._is_active = Mock(return_value=False)
        controller._seconds_until_schedule_change = Mock(return_value=100)
        original_wait = controller._shutdown.wait
        waits = []

        def stop_after_idle(seconds):
            waits.append(seconds)
            controller._shutdown.set()
            return original_wait(0)

        with patch.object(controller._shutdown, "wait", side_effect=stop_after_idle):
            controller._run_loop_unchecked()

        self.assertEqual(waits, [0.25])

    def test_sleep_scheduler_waits_interruptibly_after_every_play_attempt_fails(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        config = copy.deepcopy(DEFAULT_CONFIG)
        sleep = next(item for item in config["playlist"] if item["module"] == "sleep")
        sleep.update(source="one.png", sources=["one.png", "two.png"])
        config["playlist"] = [sleep]
        catalog = {
            reference: MediaInfo(reference, identity, "still", None)
            for reference, identity in {
                "one.png": (1, 1, 1, 1, 1),
                "two.png": (1, 2, 1, 1, 1),
            }.items()
        }
        store = Mock(media_root=Path("/nonexistent"))
        controller = DisplayController(Mock(), store)
        reads = 0

        def get_config():
            nonlocal reads
            reads += 1
            if reads == 2:
                controller._shutdown.set()
            return config

        store.get = get_config
        controller._screen_runtime = ScreenMediaRuntime(
            catalog=lambda _screen: catalog, choice=lambda values: values[0],
        )
        controller._execute = Mock(side_effect=RuntimeError("decode failed"))
        controller._is_active = Mock(return_value=False)
        controller._seconds_until_schedule_change = Mock(return_value=100)
        original_wait = controller._shutdown.wait
        waits = []

        def stop_after_idle(seconds):
            waits.append(seconds)
            controller._shutdown.set()
            return original_wait(0)

        with patch.object(controller._shutdown, "wait", side_effect=stop_after_idle):
            controller._run_loop_unchecked()

        self.assertEqual(controller._execute.call_count, 2)
        self.assertEqual(waits, [0.25])

    def test_successful_ordinary_scheduler_cycle_skips_all_failed_backoff(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["playlist"] = [
            next(item for item in config["playlist"] if item["module"] == "clock_lava")
        ]
        controller = DisplayController(Mock(), Mock(get=Mock(return_value=config)))
        finished = threading.Event()
        waits = []
        executions = []
        controller._shutdown = Mock(
            is_set=finished.is_set,
            set=finished.set,
            wait=lambda seconds: waits.append(seconds),
        )
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller._is_active = Mock(return_value=True)
        controller._seconds_until_schedule_change = Mock(return_value=100)

        def execute(*args, **kwargs):
            executions.append((args, kwargs))
            if len(executions) == 2:
                finished.set()
            return True

        controller._execute = Mock(side_effect=execute)

        controller._run_loop_unchecked()

        self.assertEqual(controller._execute.call_count, 2)
        self.assertEqual(waits, [])

    def test_successful_splash_scheduler_cycle_skips_all_failed_backoff(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["playlist"] = [
            next(item for item in config["playlist"] if item["module"] == "splash")
        ]
        controller = DisplayController(Mock(), Mock(get=Mock(return_value=config)))
        finished = threading.Event()
        waits = []
        executions = []
        controller._shutdown = Mock(
            is_set=finished.is_set,
            set=finished.set,
            wait=lambda seconds: waits.append(seconds),
        )
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller._is_active = Mock(return_value=True)
        controller._seconds_until_schedule_change = Mock(return_value=100)

        def execute(*args, **kwargs):
            executions.append((args, kwargs))
            if len(executions) == 2:
                finished.set()
            return True

        controller._execute_scheduled_screen = Mock(side_effect=execute)

        controller._run_loop_unchecked()

        self.assertEqual(controller._execute_scheduled_screen.call_count, 2)
        self.assertEqual(waits, [])

    def test_openweather_module_receives_secret_without_adding_it_to_config(self):
        matrix = Mock()
        store = Mock()
        secrets = Mock()
        secrets.get.return_value = "test_key_1234567890"
        controller = DisplayController(matrix, store, openweather_secrets=secrets)
        provider = Mock()
        icon = io.BytesIO()
        Image.new("RGB", (1, 1), "blue").save(icon, format="PNG")
        provider.prepare.return_value = {
            "error": None,
            "cancelled": False,
            "weather": {
                "location": "Brighton, GB", "status": "Clear",
                "temperature": 16, "feels_like": 15, "high": 18,
                "low": 12, "humidity": 70, "icon_code": "01d",
            },
            "icon": base64.b64encode(icon.getvalue()).decode("ascii"),
        }
        controller._openweather_provider = provider
        module = Mock()
        module.run.return_value = True
        config = {"custom_modules": [], "openweather_overview": {"location": "Brighton, GB"}}

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute("openweather_overview", config, 9)

        self.assertTrue(result)
        importer.assert_called_once_with("modules.openweather_overview")
        module.run.assert_called_once()
        self.assertEqual(module.run.call_args.kwargs["api_key"], "test_key_1234567890")
        self.assertEqual(provider.prepare.call_args.kwargs["credentials"], "test_key_1234567890")
        self.assertNotIn("api_key", str(provider.prepare.call_args.kwargs["settings"]))
        self.assertNotIn("api_key", str(config))

    def test_openweather_preparation_runs_outside_display_lock(self):
        matrix = Mock()
        secrets = Mock()
        secrets.get.return_value = "test_key_1234567890"
        controller = DisplayController(matrix, Mock(), openweather_secrets=secrets)
        provider = Mock()
        controller._openweather_provider = provider
        module = Mock()
        module.run.return_value = True
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        icon = io.BytesIO()
        Image.new("RGB", (1, 1), "blue").save(icon, format="PNG")
        transported = {
            "error": None,
            "cancelled": False,
            "weather": {
                "location": "Brighton, GB", "status": "Clear",
                "temperature": 16, "feels_like": 15, "high": 18,
                "low": 12, "humidity": 70, "icon_code": "01d",
            },
            "icon": base64.b64encode(icon.getvalue()).decode("ascii"),
        }

        def prepare(*_args, **_kwargs):
            preparation_started.set()
            release_preparation.wait(1)
            return transported

        provider.prepare.side_effect = prepare
        result = []
        config = {"custom_modules": [], "openweather_overview": {}}

        with patch("display_controller.importlib.import_module", return_value=module):
            worker = threading.Thread(
                target=lambda: result.append(
                    controller._execute("openweather_overview", config, 9)
                )
            )
            worker.start()
            self.assertTrue(preparation_started.wait(0.5))
            self.assertTrue(controller._display_lock.acquire(timeout=0.2))
            controller._display_lock.release()
            release_preparation.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(module.run.call_args.kwargs["prepared"].weather.location, "Brighton, GB")

    def test_openweather_full_bounded_preparation_is_cancelled_within_reload_budget(self):
        matrix = Mock()
        secrets = Mock()
        secrets.get.return_value = "test_key_1234567890"
        controller = DisplayController(matrix, Mock(), openweather_secrets=secrets)
        module = Mock()
        preparation_started = threading.Event()
        release_legacy_preparation = threading.Event()
        provider_calls = []

        class BlockingProvider:
            def prepare(
                self, provider, *, settings, credentials, cancel_event,
                request_deadline,
            ):
                provider_calls.append({
                    "provider": provider,
                    "settings": settings,
                    "credentials": credentials,
                    "cancel_event": cancel_event,
                    "request_deadline": request_deadline,
                })
                preparation_started.set()
                if not cancel_event.wait(1):
                    raise AssertionError("controller did not cancel blocked preparation")
                raise PreviewProviderCancelled("test cancellation")

        def legacy_prepare(*_args, **_kwargs):
            preparation_started.set()
            release_legacy_preparation.wait(1)
            return "legacy-prepared-weather"

        controller._openweather_provider = BlockingProvider()
        module.prepare.side_effect = legacy_prepare
        results = []
        errors = []
        config = {"custom_modules": [], "openweather_overview": {}}

        def execute():
            try:
                results.append(controller._execute("openweather_overview", config, 9))
            except BaseException as error:
                errors.append(error)

        with patch("display_controller.importlib.import_module", return_value=module):
            worker = threading.Thread(target=execute)
            worker.start()
            try:
                self.assertTrue(preparation_started.wait(0.5))
                owned_stop_event = controller._module_stop
                started = time.monotonic()
                controller.reload()
                worker.join(0.25)
                settled_within_budget = not worker.is_alive()
                elapsed = time.monotonic() - started
            finally:
                release_legacy_preparation.set()
                worker.join(1)

        self.assertTrue(settled_within_budget, f"blocked preparation survived {elapsed:.3f}s")
        self.assertEqual(results, [False])
        self.assertEqual(errors, [])
        self.assertEqual(len(provider_calls), 1)
        self.assertIs(provider_calls[0]["cancel_event"], owned_stop_event)
        self.assertIsNotNone(owned_stop_event)
        self.assertTrue(owned_stop_event.is_set())
        self.assertEqual(
            provider_calls[0]["request_deadline"], PreviewProvider.MAX_REQUEST_SECONDS
        )
        module.run.assert_not_called()

    def test_openweather_reload_cancels_and_reaps_real_blocked_child(self):
        marker_handle = tempfile.NamedTemporaryFile(
            prefix="openweather-provider-entered-", dir="/tmp", delete=False,
        )
        marker = marker_handle.name
        marker_handle.close()
        os.unlink(marker)
        controller = DisplayController(
            Mock(), Mock(), openweather_secrets=Mock(get=Mock(return_value="x")),
        )
        module = Mock()
        results = []
        errors = []
        worker = None
        config = {
            "custom_modules": [],
            "openweather_overview": {},
            "_test_entered_marker": marker,
        }
        original_config = copy.deepcopy(config)

        def execute():
            try:
                results.append(controller._execute("openweather_overview", config, 9))
            except BaseException as error:
                errors.append(error)

        try:
            with (
                patch.object(
                    display_controller,
                    "_openweather_provider_child",
                    blocked_physical_openweather_provider,
                ),
                patch("display_controller.importlib.import_module", return_value=module),
            ):
                worker = threading.Thread(target=execute)
                worker.start()
                entered_deadline = time.monotonic() + 1
                while not os.path.exists(marker) and time.monotonic() < entered_deadline:
                    time.sleep(0.005)
                self.assertTrue(os.path.exists(marker))
                started = time.monotonic()
                controller.reload()
                worker.join(0.25)
                elapsed = time.monotonic() - started

            self.assertFalse(worker.is_alive(), f"blocked child survived {elapsed:.3f}s")
            self.assertEqual(results, [False])
            self.assertEqual(errors, [])
            self.assertEqual(config, original_config)
            self.assertEqual(controller._openweather_provider.active_child_count, 0)
            self.assertEqual(controller._openweather_provider.active_worker_count, 0)
            self.assertEqual(controller._openweather_provider.available_worker_slots, 2)
            module.run.assert_not_called()
        finally:
            if worker is not None:
                worker.join(1)
            controller._openweather_provider.shutdown()
            PreviewProvider._shutdown_reaper_for_tests()
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass

    def test_custom_module_can_be_previewed(self):
        store = Mock()
        store.get.return_value = {"custom_modules": [{"id": "custom_welcome"}]}
        controller = DisplayController(Mock(), store)
        controller.start = Mock()

        controller.preview("custom_welcome")

        pending = controller._take_preview()
        self.assertIsInstance(pending, display_controller._PinnedModulePreview)
        self.assertEqual(pending.module_name, "custom_welcome")
        self.assertEqual(pending.config["custom_modules"], [{"id": "custom_welcome"}])
        pending.close()
        controller.start.assert_called_once_with()

    def test_required_saved_preview_media_failure_preserves_existing_owners(self):
        cases = (
            (
                "custom_media",
                {
                    "custom_modules": [{
                        "id": "custom_media", "type": "media",
                        "source": "missing.png",
                    }],
                },
            ),
            (
                "custom_canvas",
                {
                    "custom_modules": [{
                        "id": "custom_canvas", "type": "canvas",
                        "layers": [{
                            "id": "media_layer", "type": "media",
                            "properties": {"source": "missing.png"},
                            "keyframes": [],
                        }],
                        "overrides": [],
                    }],
                },
            ),
            (
                "announcement_board",
                {
                    "custom_modules": [],
                    "announcement_board": {
                        "announcements": [{
                            "id": "announcement", "media": "missing.png",
                        }],
                    },
                },
            ),
        )
        for module_name, config in cases:
            with self.subTest(module=module_name), tempfile.TemporaryDirectory() as directory:
                media_root = Path(directory) / "media"
                media_root.mkdir()
                store = Mock(media_root=media_root, get=Mock(return_value=config))
                controller = DisplayController(Mock(), store)
                controller.start = Mock()
                prior_resolver = Mock()
                prior = display_controller._PinnedModulePreview(
                    "weather", {}, prior_resolver, {},
                )
                controller._preview_name = prior
                active_stop = threading.Event()
                controller._module_stop = active_stop
                try:
                    with self.assertRaises(display_controller.MediaResolutionError):
                        controller.preview(module_name)

                    self.assertIs(controller._preview_name, prior)
                    self.assertFalse(active_stop.is_set())
                    prior_resolver.close.assert_not_called()
                    controller.start.assert_not_called()
                finally:
                    if controller._preview_name is not None:
                        controller._preview_name.close()
                        controller._preview_name = None
                    controller.device_preview_manager.close()
                    controller._openweather_provider.shutdown()

    def test_saved_screen_preview_rejects_missing_preparation_authority_under_optimization(self):
        if __debug__:
            result = subprocess.run(
                [
                    sys.executable, "-O", "-m", "unittest",
                    "tests.test_display_controller.DisplayControllerTests."
                    "test_saved_screen_preview_rejects_missing_preparation_authority_under_optimization",
                    "-v",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return

        for screen in ("splash", "sleep"):
            with self.subTest(screen=screen):
                resolver = Mock()
                resolver.resolve.return_value = Mock()
                store = Mock(
                    media_root=Path("/unused"),
                    get=Mock(return_value=copy.deepcopy(DEFAULT_CONFIG)),
                )
                controller = DisplayController(Mock(), store)
                controller._preview_preparation_authority = Mock(return_value=None)
                controller._begin_preview_preparation = Mock()
                controller._queue_preview = Mock()
                controller.start = Mock()
                duration = Mock(return_value=1)
                try:
                    with (
                        patch("display_controller.MediaResolver", return_value=resolver),
                        patch.object(
                            edit_preview,
                            "bounded_screen_preview_duration_seconds",
                            duration,
                        ),
                        self.assertRaisesRegex(
                            RuntimeError,
                            "display controller is stopping or lifecycle changed",
                        ),
                    ):
                        controller.preview(screen)
                    controller._begin_preview_preparation.assert_not_called()
                    controller._queue_preview.assert_not_called()
                    controller.start.assert_not_called()
                    duration.assert_not_called()
                    resolver.close.assert_called_once_with()
                    self.assertEqual(controller._preview_preparations, set())
                    self.assertIsNone(controller._take_preview())
                finally:
                    controller.device_preview_manager.close()
                    controller._openweather_provider.shutdown()

    def test_announcement_preview_queue_is_last_request_wins_and_isolated(self):
        controller = DisplayController(Mock(), Mock(media_root=Path("/unused")))
        controller.start = Mock()
        controller._module_stop = Mock()
        first = {"id": "announcement_first", "headline": "FIRST"}
        second = {"id": "announcement_second", "headline": "SECOND"}
        first_resolver = Mock()
        second_resolver = Mock()

        with patch(
            "display_controller.MediaResolver",
            side_effect=[first_resolver, second_resolver],
        ):
            controller.preview_announcement(first)
            first["headline"] = "MUTATED"
            controller.preview_announcement(second)

        pending = controller._take_announcement_preview()
        self.assertEqual(pending.announcement, second)
        pending.close()
        self.assertIsNone(controller._take_announcement_preview())
        self.assertIsNone(controller._take_preview())
        self.assertIsNone(controller._take_media_preview())
        controller._module_stop.set.assert_called()
        self.assertEqual(controller.start.call_count, 2)
        first_resolver.close.assert_called_once_with()
        second_resolver.close.assert_called_once_with()

    def test_execute_announcement_preview_uses_exact_draft_and_stop_lifecycle(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        announcement = {"id": "announcement_preview", "enabled": False, "headline": "EXACT"}
        pending = display_controller._PinnedAnnouncementPreview(
            announcement, Mock(), {},
        )
        module = Mock()
        module.run_preview.return_value = True

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute_announcement_preview(pending)

        self.assertTrue(result)
        importer.assert_called_once_with("modules.announcement_board")
        module.run_preview.assert_called_once()
        self.assertIs(module.run_preview.call_args.args[0], matrix)
        self.assertEqual(module.run_preview.call_args.args[1], announcement)
        self.assertIsNotNone(module.run_preview.call_args.kwargs["stop_event"])
        self.assertIsNone(controller.current_module)
        self.assertIsNone(controller._module_stop)

    def test_announcement_preview_revalidates_reload_admission_after_display_wait(self):
        controller = DisplayController(Mock(), Mock())
        real_lock = controller._display_lock
        pending = display_controller._PinnedAnnouncementPreview(
            {"id": "announcement_preview"}, Mock(), {},
        )
        module = Mock()
        module.run_preview.return_value = True

        class ReloadBeforeDisplayOwnership:
            def __enter__(self):
                real_lock.acquire()
                with controller._state_lock:
                    controller._module_admission_generation += 1
                    generation = controller._module_admission_generation
                    controller._reload_obligations[generation] = "active"
                    controller._sync_reload_fence_aliases_locked()
                return self

            def __exit__(self, *_args):
                real_lock.release()

        object.__setattr__(controller, "_display_lock", ReloadBeforeDisplayOwnership())
        try:
            with patch("display_controller.importlib.import_module", return_value=module):
                result = controller._execute_announcement_preview(pending)
        finally:
            pending.close()
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(result)
        module.run_preview.assert_not_called()
        self.assertIsNone(controller._module_stop)

    def test_scheduler_executes_queued_announcement_before_normal_playlist(self):
        store = Mock()
        store.get.side_effect = [DEFAULT_CONFIG, RuntimeError("stop test loop")]
        controller = DisplayController(Mock(), store)
        draft = {"id": "announcement_preview"}
        pending = display_controller._PinnedAnnouncementPreview(draft, Mock(), {})
        controller._announcement_preview = pending
        controller._execute = Mock()
        controller._execute_media = Mock()
        controller._execute_announcement_preview = Mock(side_effect=lambda item: controller._shutdown.set() or True)

        controller._run_loop_unchecked()

        controller._execute_announcement_preview.assert_called_once_with(pending)
        pending.resolver.close.assert_called_once_with()
        controller._execute.assert_not_called()
        controller._execute_media.assert_not_called()

    def test_active_playlist_interleaved_pinned_preview_closes_owner_on_every_outcome(self):
        for preview_type in ("direct-media", "announcement"):
            for outcome in ("success", "rejected", "exception"):
                with self.subTest(preview=preview_type, outcome=outcome):
                    resolver = Mock()
                    if preview_type == "direct-media":
                        owner = display_controller._PinnedMediaPreview(
                            "selected.gif", resolver, Mock(),
                        )
                    else:
                        owner = display_controller._PinnedAnnouncementPreview(
                            {"id": "announcement_interleaved"}, resolver, {},
                        )
                    store = Mock()
                    store.get.return_value = DEFAULT_CONFIG
                    controller = DisplayController(Mock(), store)
                    controller._is_active = Mock(return_value=True)
                    controller._execute = Mock()

                    def execute(_owner, *args, **kwargs):
                        controller._shutdown.set()
                        if outcome == "exception":
                            raise RuntimeError("interleaved execution failed")
                        return outcome == "success"

                    controller._execute_media = Mock(side_effect=execute)
                    controller._execute_announcement_preview = Mock(side_effect=execute)
                    if preview_type == "direct-media":
                        controller._take_announcement_preview = Mock(return_value=None)
                        controller._take_media_preview = Mock(
                            side_effect=[None, owner],
                        )
                    else:
                        controller._take_announcement_preview = Mock(
                            side_effect=[None, owner],
                        )
                        controller._take_media_preview = Mock(return_value=None)

                    with (
                        patch(
                            "display_controller.scheduled_playlist_items",
                            return_value=[{
                                "module": "clock", "duration": 5, "enabled": True,
                            }],
                        ),
                        patch(
                            "display_controller.invocation_default_duration",
                            return_value=17,
                        ) as default_duration,
                    ):
                        if outcome == "exception":
                            with self.assertRaisesRegex(
                                RuntimeError, "interleaved execution failed",
                            ):
                                controller._run_loop_unchecked()
                        else:
                            controller._run_loop_unchecked()

                    resolver.close.assert_called_once_with()
                    if preview_type == "direct-media":
                        default_duration.assert_called_once_with("media:selected.gif")

    def test_media_file_can_be_queued_and_executed_as_a_one_off_preview(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock(media_root=Path("/unused")))
        controller.start = Mock()
        resolver = Mock()
        pinned_handle = Mock()
        resolver.resolve.return_value = pinned_handle

        with patch("display_controller.MediaResolver", return_value=resolver):
            controller.preview_media("custom.gif")

        pending = controller._take_media_preview()
        self.assertEqual(pending.filename, "custom.gif")
        controller.start.assert_called_once_with()

        with patch("display_controller.play_media", return_value=True) as player:
            self.assertTrue(controller._execute_media(pending, duration=15))

        player.assert_called_once()
        self.assertIs(player.call_args.args[0], matrix)
        self.assertEqual(player.call_args.args[1:3], ("custom.gif", 15))
        self.assertIsNotNone(player.call_args.kwargs["stop_event"])
        self.assertIs(player.call_args.kwargs["pinned_handle"], pinned_handle)
        pending.close()
        resolver.close.assert_called_once_with()

    def test_direct_media_and_announcement_preview_execute_admitted_inode(self):
        for preview_type in ("direct-media", "announcement"):
            with self.subTest(preview=preview_type), tempfile.TemporaryDirectory() as directory:
                media_root = Path(directory)
                source = media_root / "selected.gif"
                source.write_bytes(b"original-inode")
                store = Mock(media_root=media_root)
                controller = DisplayController(Mock(), store)
                controller.start = Mock()

                if preview_type == "direct-media":
                    controller.preview_media(source.name)
                    pending = controller._take_media_preview()
                    pinned_handle = pending.pinned_handle
                else:
                    announcement = {
                        "id": "announcement_pinned", "media": source.name,
                    }
                    controller.preview_announcement(announcement)
                    pending = controller._take_announcement_preview()
                    pinned_handle = pending.pinned_handles[source.name]

                replacement = media_root / "replacement.gif"
                replacement.write_bytes(b"replacement-path")
                os.replace(replacement, source)
                with pinned_handle.dup_file() as handle:
                    self.assertEqual(handle.read(), b"original-inode")

                try:
                    if preview_type == "direct-media":
                        with patch("display_controller.play_media", return_value=True) as player:
                            self.assertTrue(controller._execute_media(pending, duration=15))
                        self.assertIs(player.call_args.kwargs["pinned_handle"], pinned_handle)
                    else:
                        module = Mock(run_preview=Mock(return_value=True))
                        with patch(
                            "display_controller.importlib.import_module", return_value=module,
                        ):
                            self.assertTrue(controller._execute_announcement_preview(pending))
                        self.assertIs(
                            module.run_preview.call_args.kwargs["pinned_handles"][source.name],
                            pinned_handle,
                        )
                finally:
                    pending.close()
                self.assertTrue(pinned_handle.closed)

    def test_direct_media_and_announcement_start_failure_close_new_owner(self):
        for preview_type in ("direct-media", "announcement"):
            with self.subTest(preview=preview_type):
                resolver = Mock()
                if preview_type == "direct-media":
                    owner = display_controller._PinnedMediaPreview(
                        "selected.gif", resolver, Mock(),
                    )
                    queue = lambda controller: controller._queue_preview(
                        media_preview=owner,
                    )
                else:
                    owner = display_controller._PinnedAnnouncementPreview(
                        {"id": "announcement_start_failure"}, resolver, {},
                    )
                    queue = lambda controller: controller._queue_preview(
                        announcement_preview=owner,
                    )
                controller = DisplayController(Mock(), Mock())

                with (
                    patch.object(
                        controller, "start", side_effect=RuntimeError("start failed"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "start failed"),
                ):
                    queue(controller)

                resolver.close.assert_called_once_with()
                self.assertIsNone(controller._take_preview())
                self.assertIsNone(controller._take_media_preview())
                self.assertIsNone(controller._take_announcement_preview())

    def test_successful_stop_drains_each_pending_pinned_preview_type(self):
        cases = (
            ("media", lambda controller: controller.preview_media("custom.gif")),
            (
                "announcement",
                lambda controller: controller.preview_announcement({
                    "id": "announcement_pending", "headline": "PENDING",
                }),
            ),
        )
        for label, queue in cases:
            with self.subTest(preview=label):
                resolver = Mock()
                resolver.resolve.return_value = Mock()
                controller = DisplayController(
                    Mock(), Mock(media_root=Path("/unused")),
                )
                with (
                    patch("display_controller.MediaResolver", return_value=resolver),
                    patch.object(controller, "start"),
                ):
                    queue(controller)

                self.assertTrue(controller.stop())

                self.assertIsNone(controller._take_preview())
                self.assertIsNone(controller._take_media_preview())
                self.assertIsNone(controller._take_announcement_preview())
                resolver.close.assert_called_once_with()

    def test_stop_completion_invalidates_each_preview_resolving_before_admission(self):
        cases = (
            ("saved", lambda controller: controller.preview("splash")),
            ("direct-media", lambda controller: controller.preview_media("selected.gif")),
            (
                "announcement",
                lambda controller: controller.preview_announcement({
                    "id": "announcement_resolving", "media": "selected.gif",
                }),
            ),
        )
        for label, request_preview in cases:
            with self.subTest(preview=label):
                entered = threading.Event()
                release = threading.Event()
                errors = []
                resolver = Mock()

                def resolve(_reference):
                    entered.set()
                    self.assertTrue(release.wait(1))
                    return Mock()

                resolver.resolve.side_effect = resolve
                store = Mock(
                    media_root=Path("/unused"),
                    get=Mock(return_value=copy.deepcopy(DEFAULT_CONFIG)),
                )
                controller = DisplayController(Mock(), store)
                controller.start = Mock()

                def request():
                    try:
                        request_preview(controller)
                    except BaseException as error:
                        errors.append(error)

                with patch("display_controller.MediaResolver", return_value=resolver):
                    requester = threading.Thread(target=request)
                    requester.start()
                    self.assertTrue(entered.wait(0.2))
                    self.assertTrue(controller.stop())
                    release.set()
                    requester.join(1)

                self.assertFalse(requester.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], RuntimeError)
                self.assertIn("lifecycle", str(errors[0]))
                self.assertIsNone(controller._take_preview())
                self.assertIsNone(controller._take_media_preview())
                self.assertIsNone(controller._take_announcement_preview())
                controller.start.assert_not_called()
                resolver.close.assert_called_once_with()

    def test_queued_media_preview_prevents_a_scheduled_module_from_starting(self):
        controller = DisplayController(Mock(), Mock())
        controller._media_preview_name = "custom.gif"
        module = Mock()

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute("splash", {"custom_modules": []}, 60)

        self.assertFalse(result)
        importer.assert_not_called()
        module.run.assert_not_called()

    def test_execute_routes_custom_definition_to_safe_text_renderer(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        custom = {"id": "custom_welcome", "text": "HELLO"}
        config = {"custom_modules": [custom]}
        module = Mock()
        module.run.return_value = True

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute("custom_welcome", config, 9)

        self.assertTrue(result)
        importer.assert_called_once_with("modules.custom_text")
        module.run.assert_called_once()
        self.assertEqual(module.run.call_args.kwargs["module_config"], custom)

    def test_equal_active_times_mean_always_active(self):
        config = {"display": {"active_start": "08:00", "active_end": "08:00"}}
        self.assertTrue(DisplayController._is_active(config))

    def test_end_pairing_releases_pause(self):
        controller = DisplayController(Mock(), Mock())
        controller.show_pairing_pin("123456", ttl_seconds=60)
        controller.end_pairing()
        self.assertFalse(controller.pairing_active)

    def test_concurrent_start_creates_one_scheduler_thread(self):
        controller = DisplayController(Mock(), Mock())
        created = []

        class FakeThread:
            def __init__(self, **kwargs):
                self.alive = False
                created.append(self)

            def start(self):
                time.sleep(0.02)
                self.alive = True

            def is_alive(self):
                return self.alive

        real_thread = threading.Thread
        with patch("display_controller.threading.Thread", FakeThread):
            callers = [real_thread(target=controller.start) for _ in range(8)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()
        self.assertEqual(len(created), 1)

    def test_scheduler_survives_top_level_fault_and_reports_it(self):
        matrix = Mock()
        store = Mock()
        store.get.side_effect = RuntimeError("brightness hardware unavailable")
        controller = DisplayController(matrix, store)

        controller.start()
        deadline = time.time() + 0.5
        while controller.last_error is None and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(controller.running)
        self.assertEqual(
            controller.last_error,
            "RuntimeError: brightness hardware unavailable",
        )
        controller.stop(timeout=1)

    def test_persistent_scheduler_faults_back_off_and_rate_limit_tracebacks(self):
        store = Mock()
        store.get.return_value = DEFAULT_CONFIG
        matrix = Mock()
        matrix.set_brightness.side_effect = RuntimeError("persistent failure")
        controller = DisplayController(matrix, store)

        with patch.object(display_controller.LOGGER, "exception") as log_exception:
            controller.start()
            time.sleep(0.82)
            controller.stop(timeout=1)

        self.assertGreaterEqual(matrix.set_brightness.call_count, 2)
        self.assertLessEqual(matrix.set_brightness.call_count, 3)
        log_exception.assert_called_once()

    def test_downstream_scheduler_faults_back_off_after_brightness_succeeds(self):
        store = Mock()
        store.get.return_value = {**DEFAULT_CONFIG, "playlist": [None]}
        matrix = Mock()
        controller = DisplayController(matrix, store)

        with patch.object(display_controller.LOGGER, "exception") as log_exception, \
             patch.object(controller, "_is_active", return_value=True):
            controller.start()
            time.sleep(0.82)
            controller.stop(timeout=1)

        self.assertGreaterEqual(matrix.set_brightness.call_count, 2)
        self.assertLessEqual(matrix.set_brightness.call_count, 3)
        log_exception.assert_called_once()

    def test_module_failures_back_off_and_rate_limit_tracebacks(self):
        store = Mock()
        store.get.return_value = {
            **DEFAULT_CONFIG,
            "playlist": [{"module": "weather", "duration": 1, "enabled": True}],
        }
        matrix = Mock()
        module = Mock()
        module.run.side_effect = RuntimeError("module failed")
        controller = DisplayController(matrix, store)

        with patch.object(display_controller.importlib, "import_module", return_value=module), \
             patch.object(display_controller.LOGGER, "exception") as log_exception, \
             patch.object(controller, "_is_active", return_value=True):
            controller.start()
            time.sleep(0.82)
            controller.stop(timeout=1)

        self.assertGreaterEqual(module.run.call_count, 2)
        self.assertLessEqual(module.run.call_count, 3)
        log_exception.assert_called_once()

    def test_stop_blocks_concurrent_restart_until_hardware_is_cleared(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        old_thread = Mock()
        old_thread.is_alive.return_value = False
        controller._thread = old_thread
        restart_results = []
        old_thread.join.side_effect = lambda _timeout: restart_results.append(controller.start())

        self.assertTrue(controller.stop(timeout=1))

        self.assertEqual(restart_results, [False])
        self.assertFalse(controller.running)
        matrix.clear.assert_called_once()
        matrix.swap.assert_called_once()

    def test_stop_timeout_does_not_wait_for_display_lock_after_worker_exits(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_display_lock():
            with controller._display_lock:
                lock_held.set()
                release_lock.wait(1)

        holder = threading.Thread(target=hold_display_lock)
        holder.start()
        self.assertTrue(lock_held.wait(0.2))

        started = time.monotonic()
        result = controller.stop(timeout=0)
        elapsed = time.monotonic() - started

        release_lock.set()
        holder.join(1)
        self.assertFalse(result)
        self.assertLess(elapsed, 0.1)
        matrix.clear.assert_not_called()
        matrix.swap.assert_not_called()

    def test_stop_timeout_does_not_wait_on_display_lock_held_by_live_worker(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        worker = Mock()
        worker.is_alive.return_value = True
        controller._thread = worker
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_display_lock():
            with controller._display_lock:
                lock_held.set()
                release_lock.wait(1)

        holder = threading.Thread(target=hold_display_lock)
        holder.start()
        self.assertTrue(lock_held.wait(0.2))
        result = []
        caller = threading.Thread(target=lambda: result.append(controller.stop(timeout=0)))
        caller.start()
        caller.join(0.1)
        blocked = caller.is_alive()
        release_lock.set()
        holder.join(1)
        caller.join(1)

        self.assertFalse(blocked, "stop exceeded its timeout waiting for the display lock")
        self.assertEqual(result, [False])
        matrix.clear.assert_not_called()
        matrix.swap.assert_not_called()

    def test_stop_deadline_is_bounded_while_pairing_ip_resolution_is_stalled(self):
        matrix = Mock()
        provider_started = threading.Event()
        release_provider = threading.Event()

        def blocked_ip_provider():
            provider_started.set()
            release_provider.wait(1)
            return "192.168.1.4"

        controller = DisplayController(
            matrix, Mock(), ip_address_provider=blocked_ip_provider,
        )
        failures = []

        def show_pin():
            try:
                controller.show_pairing_pin("123456", ttl_seconds=60)
            except RuntimeError as error:
                failures.append(error)

        pairing_thread = threading.Thread(target=show_pin)
        pairing_thread.start()
        self.assertTrue(provider_started.wait(0.2))

        result = []
        started = time.monotonic()
        stop_thread = threading.Thread(target=lambda: result.append(controller.stop(timeout=0.05)))
        stop_thread.start()
        stop_thread.join(0.2)
        elapsed = time.monotonic() - started

        release_provider.set()
        pairing_thread.join(1)
        stop_thread.join(1)
        self.assertFalse(stop_thread.is_alive(), "stop exceeded its deadline on pairing state")
        self.assertEqual(result, [False])
        self.assertLess(elapsed, 0.2)
        self.assertEqual(
            [str(error) for error in failures], ["pairing display unavailable"],
        )
        self.assertFalse(controller.pairing_active)

    def test_timed_out_stop_invalidates_stalled_pairing_before_restart(self):
        matrix = Mock()
        provider_started = threading.Event()
        release_provider = threading.Event()

        def blocked_ip_provider():
            provider_started.set()
            release_provider.wait(1)
            return "192.168.1.4"

        controller = DisplayController(
            matrix, Mock(), ip_address_provider=blocked_ip_provider,
        )
        failures = []

        def show_pin():
            try:
                controller.show_pairing_pin("123456", ttl_seconds=60)
            except RuntimeError as error:
                failures.append(error)

        pairing_thread = threading.Thread(target=show_pin)
        pairing_thread.start()
        self.assertTrue(provider_started.wait(0.2))

        self.assertFalse(controller.stop(timeout=0.05))
        controller._run_loop = Mock()
        matrix.reset_mock()
        self.assertTrue(controller.start())

        release_provider.set()
        pairing_thread.join(1)

        self.assertFalse(pairing_thread.is_alive())
        self.assertEqual(
            [str(error) for error in failures], ["pairing display unavailable"],
        )
        self.assertFalse(controller.pairing_active)
        matrix.clear.assert_not_called()
        matrix.swap.assert_not_called()

    def test_timed_out_stop_invalidates_pairing_at_pre_draw_boundary(self):
        matrix = Mock()
        draw_admission_started = threading.Event()
        release_draw_admission = threading.Event()
        controller = DisplayController(
            matrix, Mock(), ip_address_provider=lambda: "192.168.1.4",
        )
        original_draw = controller._draw_pairing_page

        def blocked_draw(*args, **kwargs):
            draw_admission_started.set()
            release_draw_admission.wait(1)
            return original_draw(*args, **kwargs)

        controller._draw_pairing_page = blocked_draw
        failures = []

        def show_pin():
            try:
                controller.show_pairing_pin("123456", ttl_seconds=60)
            except RuntimeError as error:
                failures.append(error)

        pairing_thread = threading.Thread(target=show_pin)
        pairing_thread.start()
        self.assertTrue(draw_admission_started.wait(0.2))

        self.assertFalse(controller.stop(timeout=0.05))
        controller._run_loop = Mock()
        matrix.reset_mock()
        self.assertTrue(controller.start())

        release_draw_admission.set()
        pairing_thread.join(1)

        self.assertFalse(pairing_thread.is_alive())
        self.assertEqual(
            [str(error) for error in failures], ["pairing display unavailable"],
        )
        self.assertFalse(controller.pairing_active)
        matrix.clear.assert_not_called()
        matrix.swap.assert_not_called()

    def test_reload_breaks_playlist_and_loads_new_config(self):
        splash = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "splash"
        ))
        splash["duration"] = 2
        old = {
            **copy.deepcopy(DEFAULT_CONFIG),
            "display": {"active_start": "00:00", "active_end": "00:00", "day_brightness": 100, "night_brightness": 10},
            "playlist": [splash, {"module": "weather", "duration": 2, "enabled": True}],
        }
        new = {
            **copy.deepcopy(DEFAULT_CONFIG),
            "display": old["display"],
            "playlist": [{"module": "clock_lava", "duration": 2, "enabled": True}],
        }
        store = Mock(media_root=Path(__file__).parents[1] / "resources" / "media")
        store.get.side_effect = [old, new]
        controller = DisplayController(Mock(), store)
        calls = []

        def execute(name, config, duration):
            calls.append((name, config))
            if len(calls) == 1:
                controller._reload_requested.set()
            else:
                controller._shutdown.set()

        controller._execute = execute
        controller._run_loop()
        self.assertEqual([name for name, _ in calls], ["splash", "clock_lava"])
        self.assertIs(calls[1][1], new)

    def test_shutdown_during_queued_preview_does_not_start_scheduled_module(self):
        config = {"display": {"active_start": "00:00", "active_end": "00:00", "day_brightness": 100, "night_brightness": 10}, "playlist": [{"module": "splash", "duration": 2, "enabled": True}]}
        controller = DisplayController(Mock(), Mock(get=Mock(return_value=config)))
        controller._take_preview = Mock(side_effect=[None, "weather"])
        calls = []

        def execute(name, config, duration):
            calls.append(name)
            controller._shutdown.set()

        controller._execute = execute
        controller._run_loop()
        self.assertEqual(calls, ["weather"])


    def test_stop_does_not_join_expiry_reaper_while_holding_display_lock(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        class BreakableOwnedLock:
            """Test lock that can release an observed deadlock for clean teardown."""

            def __init__(self):
                self._condition = threading.Condition()
                self._owner = None
                self._depth = 0
                self.stop_acquired = threading.Event()
                self._ignore_orphaned_release = False

            def acquire(self, timeout=None):
                deadline = None if timeout is None else time.monotonic() + timeout
                ident = threading.get_ident()
                with self._condition:
                    while self._owner not in (None, ident):
                        if deadline is None:
                            self._condition.wait()
                        else:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                return False
                            self._condition.wait(remaining)
                    self._owner = ident
                    self._depth += 1
                    if threading.current_thread().name == "finding-3-stop":
                        self.stop_acquired.set()
                    return True

            def release(self):
                ident = threading.get_ident()
                with self._condition:
                    if self._owner != ident:
                        if self._ignore_orphaned_release:
                            self._ignore_orphaned_release = False
                            return
                        raise RuntimeError("cannot release unowned display lock")
                    self._depth -= 1
                    if self._depth == 0:
                        self._owner = None
                        self._condition.notify_all()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_exc):
                self.release()

            def break_deadlock_for_teardown(self):
                with self._condition:
                    if self._owner is not None:
                        self._owner = None
                        self._depth = 0
                        self._ignore_orphaned_release = True
                        self._condition.notify_all()

        now = [100.0]
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        display_lock = BreakableOwnedLock()
        controller._display_lock = display_lock
        controller.device_preview_manager._clock = lambda: now[0]
        controller.start = Mock()
        callback_before_display = threading.Event()
        release_callback = threading.Event()
        original_expire = controller._expire_device_draft

        def paused_expire(lease):
            callback_before_display.set()
            self.assertTrue(release_callback.wait(1))
            original_expire(lease)

        controller.device_preview_manager._on_expire = paused_expire
        cleanup = Mock()
        controller.submit_device_draft(
            DevicePreviewRequest(
                DevicePreviewOwner("device-a", "pending"),
                1,
                {"module": "clock_lava"},
                60,
                cleanup=cleanup,
            ),
            renderer=Mock(),
        )
        now[0] = 115.0
        self.assertTrue(callback_before_display.wait(0.5))

        stop_results = []
        stop_returned = threading.Event()
        stopping = threading.Thread(
            target=lambda: (
                stop_results.append(controller.stop(timeout=1)),
                stop_returned.set(),
            ),
            name="finding-3-stop",
        )
        stopping.start()
        release_callback.set()
        self.assertTrue(display_lock.stop_acquired.wait(0.5))
        blocked = not stop_returned.wait(0.15)

        if blocked:
            display_lock.break_deadlock_for_teardown()
        stopping.join(1)
        self.assertFalse(stopping.is_alive(), "test teardown could not settle stop/reaper")
        self.assertFalse(
            blocked,
            "stop joined the expiry reaper while owning its required display lock",
        )
        self.assertEqual(stop_results, [True])
        cleanup.assert_called_once_with()
        matrix.clear.assert_called_once_with()
        matrix.swap.assert_called_once_with()

    def test_stop_racing_active_render_expiry_defers_cleanup_and_drops_stale_publication(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        class GuardedMatrix:
            def __init__(self):
                self.guard = None
                self.publications = []
                self.clear_count = 0
                self.stop_swaps = 0

            def set_publication_guard(self, guard):
                self.guard = guard

            def publish(self, label):
                if self.guard is None or self.guard():
                    self.publications.append(label)

            def clear(self):
                self.clear_count += 1

            def swap(self):
                self.stop_swaps += 1

        now = [200.0]
        matrix = GuardedMatrix()
        controller = DisplayController(matrix, Mock())
        controller.device_preview_manager._clock = lambda: now[0]
        controller.start = Mock()
        entered = threading.Event()
        release_render = threading.Event()
        cleanup = Mock()
        owner = DevicePreviewOwner("device-a", "active-expiry")

        def renderer(matrix, _request, *, stop_event):
            entered.set()
            self.assertTrue(release_render.wait(1))
            matrix.publish("stale-unsaved")
            return not stop_event.is_set()

        controller.submit_device_draft(
            DevicePreviewRequest(
                owner, 1, {"module": "clock_lava"}, 60, cleanup=cleanup,
            ),
            renderer=renderer,
        )
        render_worker = threading.Thread(
            target=controller._execute_pending_device_draft,
            name="finding-3-active-render",
        )
        controller._thread = render_worker
        render_worker.start()
        self.assertTrue(entered.wait(0.5))

        now[0] = 215.0
        stop_event = controller._device_draft_stop
        self.assertIsNotNone(stop_event)
        self.assertTrue(stop_event.wait(0.5))
        stop_results = []
        stop_returned = threading.Event()
        stopping = threading.Thread(
            target=lambda: (
                stop_results.append(controller.stop(timeout=1)),
                stop_returned.set(),
            )
        )
        stopping.start()
        self.assertFalse(stop_returned.wait(0.1))
        cleanup.assert_not_called()

        release_render.set()
        render_worker.join(1)
        stopping.join(1)
        self.assertFalse(render_worker.is_alive())
        self.assertFalse(stopping.is_alive())
        self.assertEqual(stop_results, [True])
        self.assertEqual(matrix.publications, [])
        self.assertIsNone(matrix.guard)
        cleanup.assert_called_once_with()
        self.assertIsNone(controller.device_preview_manager.active_lease())

        self.assertTrue(controller.stop(timeout=1))
        controller.shutdown_device_drafts()
        cleanup.assert_called_once_with()

    def test_pending_device_draft_autonomously_expires_once_before_saved_scheduler_resumes(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        splash = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "splash"
        ))
        splash["duration"] = 1
        saved_config = {**DEFAULT_CONFIG, "playlist": [splash]}
        now = [300.0]
        store = Mock(
            get=Mock(return_value=saved_config),
            media_root=Path(__file__).parents[1] / "resources" / "media",
        )
        controller = DisplayController(Mock(), store)
        controller.device_preview_manager._clock = lambda: now[0]
        controller.start = Mock()
        cleanup_finished = threading.Event()
        cleanup = Mock(side_effect=cleanup_finished.set)
        controller.submit_device_draft(
            DevicePreviewRequest(
                DevicePreviewOwner("device-a", "pending-expiry"),
                1,
                {"module": "weather", "candidate": {"draft": "unsaved"}},
                60,
                cleanup=cleanup,
            ),
            renderer=Mock(),
        )
        self.assertIsNotNone(controller._device_draft_pending)

        now[0] = 315.0
        self.assertTrue(cleanup_finished.wait(0.5))
        cleanup.assert_called_once_with()
        self.assertIsNone(controller._device_draft_pending)
        self.assertIsNone(controller.device_preview_manager.active_lease())
        self.assertTrue(controller._reload_requested.is_set())

        saved_renders = []

        def render_saved(name, config, duration):
            saved_renders.append((name, config, duration))
            controller._shutdown.set()
            return True

        controller._is_active = Mock(return_value=True)
        controller._execute = render_saved
        controller._run_loop_unchecked()
        self.assertEqual(len(saved_renders), 1)
        self.assertEqual(saved_renders[0][:2], ("splash", saved_config))
        self.assertEqual(saved_renders[0][2], 7.23)
        store.set.assert_not_called()

        controller.shutdown_device_drafts()
        controller.device_preview_manager.close()
        controller.device_preview_manager.close()
        cleanup.assert_called_once_with()

    def test_device_draft_cancel_waits_for_submission_barrier_then_resumes(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        controller.start = Mock()
        owner = DevicePreviewOwner("device-a", "tab-a")
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        def render(_matrix, _request, *, stop_event):
            entered.set()
            release.wait(1)
            self.assertTrue(stop_event.is_set())
            return True

        controller.submit_device_draft(
            DevicePreviewRequest(owner, 1, {"module": "clock_lava"}, 60),
            renderer=render,
        )
        worker = threading.Thread(target=controller._execute_pending_device_draft)
        worker.start()
        self.assertTrue(entered.wait(0.5))

        cancelling = threading.Thread(
            target=lambda: (
                controller.cancel_device_draft(owner, generation=1),
                returned.set(),
            )
        )
        cancelling.start()
        self.assertFalse(returned.wait(0.1))
        release.set()
        worker.join(1)
        cancelling.join(1)

        self.assertFalse(worker.is_alive())
        self.assertFalse(cancelling.is_alive())
        self.assertTrue(returned.is_set())
        self.assertIsNone(controller.device_preview_manager.active_lease())
        self.assertTrue(controller._reload_requested.is_set())

    def test_device_draft_revalidates_reload_admission_after_display_wait(self):
        from device_preview import (
            DevicePreviewOwner, DevicePreviewRequest, PreviewAdmissionStatus,
        )

        controller = DisplayController(Mock(), Mock())
        controller.start = Mock()
        owner = DevicePreviewOwner("device-a", "reload-admission")
        renderer = Mock(return_value=True)
        admission = controller.submit_device_draft(
            DevicePreviewRequest(owner, 1, {"module": "clock_lava"}, 60),
            renderer=renderer,
        )
        self.assertIs(admission.status, PreviewAdmissionStatus.ACCEPTED)
        real_lock = controller._display_lock

        class ReloadBeforeDisplayOwnership:
            def __enter__(self):
                real_lock.acquire()
                with controller._state_lock:
                    controller._module_admission_generation += 1
                    generation = controller._module_admission_generation
                    controller._reload_obligations[generation] = "active"
                    controller._sync_reload_fence_aliases_locked()
                return self

            def __exit__(self, *_args):
                real_lock.release()

        object.__setattr__(controller, "_display_lock", ReloadBeforeDisplayOwnership())
        try:
            result = controller._execute_pending_device_draft()
        finally:
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(result)
        renderer.assert_not_called()
        self.assertIsNone(controller._module_stop)


    def test_device_draft_pairing_reload_stop_and_shutdown_cleanup(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        for terminal in ("pairing", "reload", "stop", "shutdown"):
            with self.subTest(terminal=terminal):
                matrix = Mock()
                controller = DisplayController(matrix, Mock())
                controller.start = Mock()
                owner = DevicePreviewOwner("device-a", terminal)
                cleanup = Mock()
                render_entered = threading.Event()
                release_render = threading.Event()

                def renderer(_matrix, _request, *, stop_event):
                    if terminal == "stop":
                        render_entered.set()
                        release_render.wait(1)
                    return not stop_event.is_set()

                controller.submit_device_draft(
                    DevicePreviewRequest(
                        owner, 1, {"module": "clock_lava"}, 60, cleanup=cleanup,
                    ),
                    renderer=renderer,
                )
                render_worker = None
                if terminal == "stop":
                    render_worker = threading.Thread(
                        target=controller._execute_pending_device_draft,
                    )
                    render_worker.start()
                    self.assertTrue(render_entered.wait(0.5))

                if terminal == "pairing":
                    with patch("display_controller.threading.Timer"):
                        controller.show_pairing_pin("123456", ttl_seconds=60)
                    controller.end_pairing()
                elif terminal == "reload":
                    controller.reload()
                elif terminal == "stop":
                    stop_result = []
                    stopping = threading.Thread(
                        target=lambda: stop_result.append(controller.stop(timeout=1)),
                    )
                    stopping.start()
                    self.assertTrue(controller._shutdown.wait(0.5))
                    self.assertFalse(cleanup.called)
                    release_render.set()
                    render_worker.join(1)
                    stopping.join(1)
                    self.assertEqual(stop_result, [True])
                else:
                    controller.shutdown_device_drafts()

                self.assertIsNone(controller.device_preview_manager.active_lease())
                self.assertIsNone(controller._device_draft_pending)
                cleanup.assert_called_once_with()


    def test_device_draft_render_failure_releases_owner_and_resumes(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        controller = DisplayController(Mock(), Mock())
        controller.start = Mock()
        owner = DevicePreviewOwner("device-a", "tab-a")
        cleanup = Mock()
        controller.submit_device_draft(
            DevicePreviewRequest(
                owner, 4, {"module": "weather"}, 60, cleanup=cleanup,
            ),
            renderer=Mock(side_effect=RuntimeError("draft failed")),
        )

        self.assertFalse(controller._execute_pending_device_draft())

        self.assertIsNone(controller.device_preview_manager.active_lease())
        cleanup.assert_called_once_with()
        self.assertTrue(controller._reload_requested.is_set())
        self.assertEqual(controller.last_error, "RuntimeError: draft failed")
        self.assertIsNone(controller.current_module)


    def test_device_draft_temporarily_overrides_and_restores_sleep(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        sleep_config = {
            **DEFAULT_CONFIG,
            "display": {
                **DEFAULT_CONFIG["display"],
                "active_start": "09:00",
                "active_end": "17:00",
            },
        }
        store = Mock(
            get=Mock(return_value=sleep_config),
            media_root=Path(__file__).parents[1] / "resources" / "media",
        )
        controller = DisplayController(Mock(), store)
        controller.start = Mock()
        events = []

        def render(_matrix, _request, *, stop_event):
            events.append("draft")
            return not stop_event.is_set()

        def execute(name, _config, _duration):
            events.append(name)
            controller._shutdown.set()
            return True

        controller._execute = execute
        controller.submit_device_draft(
            DevicePreviewRequest(
                DevicePreviewOwner("device-a", "tab-a"),
                1,
                {"module": "weather"},
                60,
            ),
            renderer=render,
        )
        with patch.object(controller, "_is_active", return_value=False):
            controller._run_loop_unchecked()

        self.assertEqual(events, ["draft", "sleep"])
        self.assertIsNone(controller.device_preview_manager.active_lease())


    def test_physical_draft_connectivity_expiry_settles_renderer_and_resumes_saved_config(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        class FakeClock:
            def __init__(self):
                self.now = 100.0
                self.reaper_checked = threading.Event()
                self.calls = 0

            def monotonic(self):
                self.calls += 1
                if self.calls > 1:
                    self.reaper_checked.set()
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class OwnedLock:
            def __init__(self):
                self._lock = threading.RLock()
                self._owner = None

            def __enter__(self):
                self._lock.acquire()
                self._owner = threading.get_ident()
                return self

            def __exit__(self, *_exc):
                self._owner = None
                self._lock.release()

            def owned_by_current_thread(self):
                return self._owner == threading.get_ident()

        class GuardedMatrix:
            def __init__(self, display_lock):
                self.display_lock = display_lock
                self.guard = None
                self.publications = []

            def set_publication_guard(self, guard):
                if not self.display_lock.owned_by_current_thread():
                    raise RuntimeError("publication guard changed outside display lock")
                self.guard = guard

            def swap(self, label):
                if self.guard is None or self.guard():
                    self.publications.append(label)

            def set_brightness(self, _brightness):
                pass

        splash = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "splash"
        ))
        splash["duration"] = 1
        saved_config = {**DEFAULT_CONFIG, "playlist": [splash]}
        unsaved_candidate = {"module": "weather", "candidate": {"draft": "unsaved"}}
        clock = FakeClock()
        display_lock = OwnedLock()
        matrix = GuardedMatrix(display_lock)
        store = Mock(
            get=Mock(return_value=saved_config),
            media_root=Path(__file__).parents[1] / "resources" / "media",
        )
        controller = DisplayController(matrix, store)
        controller._display_lock = display_lock
        controller.device_preview_manager._clock = clock.monotonic
        controller._is_active = Mock(return_value=True)
        controller.start = Mock()
        owner = DevicePreviewOwner("device-a", "tab-a")
        before_publication = threading.Event()
        release_renderer = threading.Event()
        cleanup_finished = threading.Event()
        cleanup = Mock(side_effect=cleanup_finished.set)
        saved_renders = []

        def render(matrix, _request, *, stop_event):
            before_publication.set()
            self.assertTrue(release_renderer.wait(1))
            matrix.swap("unsaved")
            return not stop_event.is_set()

        def render_saved(name, config, _duration):
            saved_renders.append((name, config))
            controller._shutdown.set()
            return True

        controller._execute = render_saved
        controller.submit_device_draft(
            DevicePreviewRequest(owner, 1, unsaved_candidate, 60, cleanup=cleanup),
            renderer=render,
        )
        scheduler = threading.Thread(
            target=controller._run_loop_unchecked,
            name="physical-draft-expiry-scheduler",
        )
        scheduler.start()
        self.assertTrue(before_publication.wait(0.5))
        self.assertTrue(clock.reaper_checked.wait(0.5))

        clock.advance(15)
        stop_event = controller._device_draft_stop
        self.assertIsNotNone(stop_event)
        self.assertTrue(stop_event.wait(0.5))
        self.assertFalse(cleanup_finished.wait(0.1))
        cleanup.assert_not_called()
        self.assertTrue(scheduler.is_alive())

        release_renderer.set()
        scheduler.join(1)
        self.assertFalse(scheduler.is_alive())
        self.assertTrue(cleanup_finished.wait(0.5))
        cleanup.assert_called_once_with()
        self.assertEqual(matrix.publications, [])
        self.assertIsNone(matrix.guard)
        self.assertIsNone(controller.device_preview_manager.active_lease())
        self.assertEqual(saved_renders, [("splash", saved_config)])
        store.set.assert_not_called()
        controller.device_preview_manager.close()

    def test_physical_draft_selected_lava_palette_matches_browser_complete_frame_sequence(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest, PreviewAdmissionStatus

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 15, 9, 30, tzinfo=tz)

        class FourFrameStop:
            def __init__(self):
                self.waits = 0

            def is_set(self):
                return False

            def wait(self, _delay):
                self.waits += 1
                return self.waits == 4

        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submitted = []

            def swap(self, canvas=None):
                submitted = canvas or self.canvas
                image = self.snapshot(submitted)
                self.assert_complete_frame(image)
                self.submitted.append(image.tobytes())
                return super().swap(submitted)

            @staticmethod
            def assert_complete_frame(image):
                if image.mode != "RGB" or image.size != (64, 32) or len(image.tobytes()) != 64 * 32 * 3:
                    raise AssertionError("physical Lava renderer did not submit a complete 64x32 RGB frame")

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "enabled_palettes": ["fire_burst", "ocean_wave"],
            "custom_enabled": False,
            "custom_palettes": [],
            "blur_radius": 0,
            "show_time": False,
            "show_date": False,
            "blob_count": 3,
            "blob_speed": 0.5,
        })
        original_lava = copy.deepcopy(candidate["lava"])
        selected = {"kind": "built_in", "id": "ocean_wave"}
        request = DevicePreviewRequest(
            owner=DevicePreviewOwner("paired-device", "b" * 32),
            generation=1,
            config={
                "module": "clock_lava",
                "candidate": candidate,
                "selected_lava_palette": selected,
            },
            timeout_seconds=60,
        )
        controller = DisplayController(Mock(), Mock())
        admission = controller.device_preview_manager.submit(request)
        self.assertIs(admission.status, PreviewAdmissionStatus.ACCEPTED)
        owned_request = admission.lease.request
        physical_matrix = RecordingMatrix()
        physical_choices = []

        def choose_physical(choices):
            physical_choices.append(tuple(choices))
            return choices[0]

        with patch("modules.clock_lava.datetime.datetime", FixedDateTime),              patch("modules.clock_lava.random.uniform", side_effect=random.Random(17).uniform),              patch("modules.clock_lava.random.choice", side_effect=choose_physical):
            controller.render_device_draft(
                physical_matrix, owned_request, stop_event=FourFrameStop(),
            )

        browser_choices = []

        def choose_browser(choices):
            browser_choices.append(tuple(choices))
            return choices[0]

        with patch("modules.clock_lava.datetime.datetime", FixedDateTime),              patch("modules.clock_lava.random.uniform", side_effect=random.Random(17).uniform),              patch("modules.clock_lava.random.choice", side_effect=choose_browser):
            iterator = edit_preview.continuous_lava_preview_frames(
                CaptureMatrix(), candidate, selected,
            )
            browser = [next(iterator) for _ in range(4)]
            iterator.close()

        browser_frames = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).convert("RGB").tobytes()
            for frame in browser
        ]
        expected_choices = [(('black', 'cyan', 'blue'),)]
        self.assertEqual(physical_choices, expected_choices)
        self.assertEqual(browser_choices, expected_choices)
        self.assertEqual([frame["delay_ms"] for frame in browser], [30] * 4)
        self.assertEqual(len(physical_matrix.submitted), 4)
        self.assertEqual(physical_matrix.submitted, browser_frames)
        self.assertEqual(candidate["lava"], original_lava)
        self.assertEqual(owned_request.config["candidate"]["lava"], original_lava)
        self.assertEqual(owned_request.config["selected_lava_palette"], selected)
        controller.device_preview_manager.close()

    def test_physical_draft_latest_generation_owns_hardware_until_bounded_return(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        class OwnedLock:
            def __init__(self):
                self._lock = threading.RLock()
                self._owner = None

            def __enter__(self):
                self._lock.acquire()
                self._owner = threading.get_ident()
                return self

            def __exit__(self, *_exc):
                self._owner = None
                self._lock.release()

            def owned_by_current_thread(self):
                return self._owner == threading.get_ident()

        class GuardedMatrix:
            def __init__(self, display_lock):
                self.display_lock = display_lock
                self.guard = None
                self.publications = []

            def set_publication_guard(self, guard):
                if not self.display_lock.owned_by_current_thread():
                    raise RuntimeError("publication guard changed outside display lock")
                self.guard = guard

            def swap(self, label):
                if self.guard is None or self.guard():
                    self.publications.append(label)

            def set_brightness(self, _brightness):
                pass

        splash = copy.deepcopy(next(
            item for item in DEFAULT_CONFIG["playlist"]
            if item["module"] == "splash"
        ))
        splash["duration"] = 1
        config = {**DEFAULT_CONFIG, "playlist": [splash]}
        display_lock = OwnedLock()
        matrix = GuardedMatrix(display_lock)
        controller = DisplayController(
            matrix,
            Mock(
                get=Mock(return_value=config),
                media_root=Path(__file__).parents[1] / "resources" / "media",
            ),
        )
        controller._display_lock = display_lock
        controller.start = Mock()
        owner = DevicePreviewOwner("device-a", "tab-a")
        old_entered = threading.Event()
        release_old = threading.Event()
        events = []

        def old_render(matrix, _request, *, stop_event):
            events.append("generation-1")
            old_entered.set()
            release_old.wait(1)
            matrix.swap("generation-1")
            return not stop_event.is_set()

        def new_render(matrix, _request, *, stop_event):
            events.append("generation-2")
            matrix.swap("generation-2")
            return not stop_event.is_set()

        old_cleanup = Mock()
        controller.submit_device_draft(
            DevicePreviewRequest(
                owner, 1, {"module": "weather"}, 60, cleanup=old_cleanup,
            ),
            renderer=old_render,
        )
        old_worker = threading.Thread(target=controller._execute_pending_device_draft)
        old_worker.start()
        self.assertTrue(old_entered.wait(0.5))
        replacement_returned = threading.Event()
        replacement = threading.Thread(
            target=lambda: (
                controller.submit_device_draft(
                    DevicePreviewRequest(
                        owner, 2, {"module": "clock_lava"}, 60,
                    ),
                    renderer=new_render,
                ),
                replacement_returned.set(),
            )
        )
        replacement.start()
        self.assertFalse(replacement_returned.wait(0.1))
        old_cleanup.assert_not_called()
        release_old.set()
        old_worker.join(1)
        replacement.join(1)
        self.assertFalse(old_worker.is_alive())
        self.assertFalse(replacement.is_alive())
        old_cleanup.assert_called_once_with()

        self.assertTrue(controller._execute_pending_device_draft())

        def saved_execute(name, _config, _duration):
            events.append(f"saved:{name}")
            controller._shutdown.set()
            return True

        controller._execute = saved_execute
        with patch.object(controller, "_is_active", return_value=True):
            controller._run_loop_unchecked()

        self.assertEqual(
            events, ["generation-1", "generation-2", "saved:splash"],
        )
        self.assertEqual(matrix.publications, ["generation-2"])
        self.assertIsNone(controller.device_preview_manager.active_lease())
        self.assertGreaterEqual(controller.config_store.get.call_count, 1)

    def test_acceptance_timeout_evidence_excludes_connectivity_expiry(self):
        from device_preview import (
            DevicePreviewLease, DevicePreviewOwner, DevicePreviewRequest,
        )

        controller = DisplayController(Mock(), Mock())
        owner = DevicePreviewOwner("device-a", "a" * 32)
        request = DevicePreviewRequest(owner, 4, {"module": "sleep"}, 60)
        lease = DevicePreviewLease(
            request=request, content_deadline=160.0,
            connectivity_deadline=115.0, last_heartbeat_at=100.0,
        )
        controller._acceptance_transaction = {
            "owner": owner, "generation": 4, "timeout_returned": False,
        }

        controller._expire_device_draft(lease)

        self.assertIs(
            controller._acceptance_transaction["timeout_returned"], False,
        )
        controller.device_preview_manager.close()

    def test_acceptance_binding_failure_does_not_leave_active_controller_context(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction

        matrix = Mock()
        matrix.latest_frame_rgb.return_value = (bytes(64 * 32 * 3), 10)
        controller = DisplayController(matrix, Mock())

        with tempfile.TemporaryDirectory() as directory:
            publisher = AcceptanceArtifactTransaction(Path(directory), {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64, "expected_source_sha": "c" * 40,
                "expected_version": "v", "mode": "deploy", "nonce": "d" * 64,
                "owner_revision": 1,
            }, owner_generation=4)
            with patch.object(
                AcceptanceArtifactTransaction, "_bind_display_controller",
                side_effect=RuntimeError("binding unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "binding unavailable"):
                    controller.begin_acceptance_transaction(
                        publisher, object(), generation=4,
                    )

        self.assertIsNone(controller._acceptance_transaction)
        controller.device_preview_manager.close()

    def test_acceptance_transaction_transient_sleep_preview_does_not_certify_inactive_sleep(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        class SwapMatrix:
            def __init__(self):
                self.observer = None

            def latest_frame_rgb(self):
                return bytes([17]) * (64 * 32 * 3), 10

            def set_physical_swap_observer(self, observer):
                self.observer = observer

            def physical_swap(self, frame):
                self.observer(frame, 11)

        class Publisher:
            INIT = "INIT"
            DRAFT_PUBLISHED = "DRAFT_PUBLISHED"
            owner_generation = 4

            def __init__(self):
                self.state = self.INIT
                self.events = []

            def publish_draft(self, frame, *, frame_revision):
                self.draft = (frame, frame_revision)
                self.state = self.DRAFT_PUBLISHED

            def _bind_display_controller(self, authority, lifecycle_token):
                self.authority = authority
                self.lifecycle_token = lifecycle_token

            def _observe(self, event, lifecycle_token):
                assert lifecycle_token is self.lifecycle_token
                self.events.append(event)

        matrix = SwapMatrix()
        controller = DisplayController(matrix, Mock())
        owner = DevicePreviewOwner("device-a", "a" * 32)
        controller.device_preview_manager.submit(
            DevicePreviewRequest(owner, 4, {"module": "sleep"}, 30)
        )
        publisher = Publisher()
        with patch("acceptance_artifacts.AcceptanceArtifactTransaction", Publisher):
            controller.begin_acceptance_transaction(publisher, owner, generation=4)
        sleep_frame = bytes([3]) * (64 * 32 * 3)

        matrix.physical_swap(sleep_frame)

        self.assertEqual(publisher.state, publisher.INIT)
        self.assertFalse(hasattr(publisher, "draft"))
        self.assertEqual(publisher.events, [])
        controller.device_preview_manager.close()

    def test_acceptance_draft_ignores_swap_between_lease_admission_and_renderer_claim(self):
        from device_preview import DevicePreviewOwner, DevicePreviewRequest

        class SwapMatrix:
            def __init__(self):
                self.observer = None
                self.revision = 10

            def latest_frame_rgb(self):
                return bytes([17]) * (64 * 32 * 3), self.revision

            def set_physical_swap_observer(self, observer):
                self.observer = observer

            def set_publication_guard(self, guard):
                self.guard = guard

            def physical_swap(self, frame):
                self.revision += 1
                assert self.observer is not None
                self.observer(frame, self.revision)

        class Publisher:
            INIT = "INIT"
            DRAFT_PUBLISHED = "DRAFT_PUBLISHED"
            owner_generation = 4

            def __init__(self):
                self.state = self.INIT
                self.drafts = []

            def _bind_display_controller(self, _authority, lifecycle_token):
                self.lifecycle_token = lifecycle_token

            def publish_draft(self, frame, *, frame_revision):
                self.drafts.append((frame, frame_revision))
                self.state = self.DRAFT_PUBLISHED

        matrix = SwapMatrix()
        controller = DisplayController(matrix, Mock())
        controller.start = Mock()
        owner = DevicePreviewOwner("device-a", "a" * 32)
        publisher = Publisher()
        with patch("acceptance_artifacts.AcceptanceArtifactTransaction", Publisher):
            controller.begin_acceptance_transaction(publisher, owner, generation=4)
        request = DevicePreviewRequest(owner, 4, {"module": "clock_lava"}, 30)
        unrelated = bytes([33]) * (64 * 32 * 3)
        draft = bytes([91]) * (64 * 32 * 3)
        admitted = controller.device_preview_manager.submit

        def admit_then_swap(*args, **kwargs):
            result = admitted(*args, **kwargs)
            matrix.physical_swap(unrelated)
            return result

        controller.device_preview_manager.submit = admit_then_swap
        rendered = []

        def renderer(render_matrix, _request, *, stop_event):
            rendered.append(True)
            self.assertFalse(stop_event.is_set())
            render_matrix.physical_swap(draft)
            return True

        controller.submit_device_draft(request, renderer=renderer)

        self.assertEqual(publisher.state, publisher.INIT)
        self.assertEqual(publisher.drafts, [])
        self.assertTrue(controller._execute_pending_device_draft())
        self.assertEqual(rendered, [True])
        self.assertEqual(publisher.drafts, [(draft, 12)])
        controller.device_preview_manager.close()

    def test_acceptance_timeout_evidence_rejects_equal_content_and_connectivity_deadlines(self):
        from device_preview import DevicePreviewLease, DevicePreviewOwner, DevicePreviewRequest

        controller = DisplayController(Mock(), Mock())
        owner = DevicePreviewOwner("device-a", "a" * 32)
        lease = DevicePreviewLease(
            request=DevicePreviewRequest(owner, 4, {"module": "clock_lava"}, 15),
            content_deadline=115.0, connectivity_deadline=115.0,
            last_heartbeat_at=100.0,
        )
        controller._acceptance_transaction = {
            "owner": owner, "generation": 4, "timeout_returned": False,
        }

        controller._expire_device_draft(lease)

        self.assertIs(controller._acceptance_transaction["timeout_returned"], False)
        controller.device_preview_manager.close()

    def test_content_expiry_after_heartbeat_renewal_stops_exact_active_renderer(self):
        from device_preview import (
            DevicePreviewOwner, DevicePreviewRequest, PreviewHeartbeatStatus,
        )

        controller = DisplayController(Mock(), Mock())
        controller.start = Mock()
        now = [0.0]
        controller.device_preview_manager._clock = lambda: now[0]
        owner = DevicePreviewOwner("device-a", "a" * 32)
        started = threading.Event()
        stopped = threading.Event()

        def renderer(_matrix, _request, *, stop_event):
            started.set()
            if stop_event.wait(1):
                stopped.set()
            return True

        admission = controller.submit_device_draft(
            DevicePreviewRequest(owner, 4, {"module": "clock_lava"}, 10),
            renderer=renderer,
        )
        worker = threading.Thread(target=controller._execute_pending_device_draft)
        worker.start()
        self.assertTrue(started.wait(1))
        now[0] = 5.0
        heartbeat = controller.heartbeat_device_draft(owner, generation=4)
        self.assertIs(heartbeat.status, PreviewHeartbeatStatus.ACCEPTED)
        now[0] = 10.0
        self.assertIsNone(controller.device_preview_manager.active_lease())
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(stopped.is_set())
        controller.device_preview_manager.close()

    def test_acceptance_transaction_natural_expiry_authorizes_only_scheduler_inactive_sleep_swap(self):
        from device_preview import (
            DevicePreviewOwner, DevicePreviewRequest, PreviewAdmissionStatus,
        )

        class SwapMatrix:
            def __init__(self):
                self.frame = bytes([17]) * (64 * 32 * 3)
                self.revision = 10
                self.observer = None

            def latest_frame_rgb(self):
                return self.frame, self.revision

            def set_physical_swap_observer(self, observer):
                self.observer = observer

            def set_brightness(self, value):
                self.brightness = value

            def physical_swap(self, frame):
                self.frame = frame
                self.revision += 1
                if self.observer is not None:
                    self.observer(frame, self.revision)

        class Publisher:
            INIT = "INIT"
            DRAFT_PUBLISHED = "DRAFT_PUBLISHED"
            owner_generation = 4

            def __init__(self):
                self.state = self.INIT
                self.saved_rollbacks = []
                self.events = []

            def publish_draft(self, _frame, *, frame_revision):
                self.state = self.DRAFT_PUBLISHED
                return {"draft_revision": frame_revision}

            def _bind_display_controller(self, authority, lifecycle_token):
                self.authority = authority
                self.lifecycle_token = lifecycle_token

            def _observe(self, event, lifecycle_token):
                assert lifecycle_token is self.lifecycle_token
                self.events.append(event)

            def publish_saved_rollback(self, frame, **evidence):
                self.saved_rollbacks.append((frame, evidence))
                self.state = "SAVED_ROLLBACK_PUBLISHED"

            def status(self):
                return {"state": self.state}

        now = [100.0]
        matrix = SwapMatrix()
        store = Mock()
        store.get.return_value = {
            "display": {"active_start": "00:00", "active_end": "00:00",
                        "day_brightness": 70, "night_brightness": 5},
            "playlist": [], "custom_modules": [],
        }
        controller = DisplayController(matrix, store)
        controller.start = Mock()
        controller.device_preview_manager._clock = lambda: now[0]
        owner = DevicePreviewOwner("device-a", "a" * 32)
        publisher = Publisher()
        with patch("acceptance_artifacts.AcceptanceArtifactTransaction", Publisher):
            controller.begin_acceptance_transaction(publisher, owner, generation=4)

        draft = bytes([91]) * (64 * 32 * 3)
        saved = bytes([17]) * (64 * 32 * 3)

        def renderer(render_matrix, _request, *, stop_event):
            render_matrix.physical_swap(draft)
            self.assertTrue(controller.mark_acceptance_foreign_rejection(
                DevicePreviewOwner("device-b", "b" * 32), generation=4,
            ))
            self.assertTrue(controller.mark_acceptance_result(
                owner, generation=4, result="heartbeat_accepted",
            ))
            now[0] = 101.0
            self.assertIsNone(controller.device_preview_manager.active_lease())
            self.assertTrue(stop_event.is_set())
            render_matrix.physical_swap(saved)
            self.assertEqual(publisher.saved_rollbacks, [])
            return True

        admission = controller.submit_device_draft(
            DevicePreviewRequest(owner, 4, {"module": "clock_lava"}, 1),
            renderer=renderer,
        )
        self.assertIs(admission.status, PreviewAdmissionStatus.ACCEPTED)
        self.assertTrue(controller._execute_pending_device_draft())

        def execute(name, config, duration):
            self.assertEqual(name, "sleep")
            matrix.physical_swap(saved)
            controller._shutdown.set()
            return True

        controller._execute = execute
        controller._run_loop_unchecked()

        self.assertEqual(len(publisher.saved_rollbacks), 1)
        self.assertIn("timeout_returned", publisher.events)
        self.assertIn("sleep_observed", publisher.events)
        returned_frame, evidence = publisher.saved_rollbacks[0]
        self.assertEqual(returned_frame, saved)
        self.assertNotIn("timeout_returned", evidence)
        controller.device_preview_manager.close()

    def test_acceptance_transaction_binds_exact_owner_and_finishes_only_after_later_saved_swap(self):
        import json
        import tempfile
        from pathlib import Path
        from acceptance_artifacts import AcceptanceArtifactTransaction
        from device_preview import DevicePreviewOwner, DevicePreviewRequest, PreviewAdmissionStatus

        class SwapMatrix:
            def __init__(self):
                self.frame = bytes([17]) * (64 * 32 * 3)
                self.revision = 10
                self.observer = None

            def latest_frame_rgb(self):
                return self.frame, self.revision

            def set_physical_swap_observer(self, observer):
                self.observer = observer

            def set_brightness(self, value):
                self.brightness = value

            def physical_swap(self, frame):
                self.frame = frame
                self.revision += 1
                if self.observer is not None:
                    self.observer(frame, self.revision)

        with tempfile.TemporaryDirectory(prefix="acceptance-controller-") as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            matrix = SwapMatrix()
            store = Mock()
            store.get.return_value = {
                "display": {"active_start": "00:00", "active_end": "00:00",
                            "day_brightness": 70, "night_brightness": 5},
                "playlist": [], "custom_modules": [],
            }
            controller = DisplayController(matrix, store)
            controller.start = Mock()
            now = [100.0]
            controller.device_preview_manager._clock = lambda: now[0]
            owner = DevicePreviewOwner("device-a", "a" * 32)
            foreign = DevicePreviewOwner("device-b", "b" * 32)
            publisher = AcceptanceArtifactTransaction(runtime, {
                "attempt_id": "1" * 32,
                "acceptance_operation_id": "2" * 64,
                "evidence_id": "2" * 64,
                "expected_source_sha": "3" * 40,
                "expected_version": "9.8.7-test",
                "mode": "deploy",
                "nonce": "4" * 64,
                "owner_revision": 7,
            }, owner_generation=4)
            controller.begin_acceptance_transaction(publisher, owner, generation=4)
            self.assertEqual(controller.acceptance_transaction_status(owner, generation=4), {
                "state": "INIT", "nonce": "4" * 64,
                "acceptance_operation_id": "2" * 64, "owner_generation": 4,
            })

            draft = bytes([91]) * (64 * 32 * 3)
            saved = bytes([17]) * (64 * 32 * 3)

            def renderer(render_matrix, _request, *, stop_event):
                render_matrix.physical_swap(draft)
                self.assertEqual(publisher.state, publisher.DRAFT_PUBLISHED)
                self.assertTrue(controller.mark_acceptance_foreign_rejection(
                    foreign, generation=4,
                ))
                self.assertTrue(controller.mark_acceptance_result(
                    owner, generation=4, result="heartbeat_accepted",
                ))
                with self.assertRaisesRegex(ValueError, "acceptance result is invalid"):
                    controller.mark_acceptance_result(
                        foreign, generation=4, result="timeout_returned",
                    )
                with self.assertRaisesRegex(ValueError, "acceptance result is invalid"):
                    controller.mark_acceptance_result(
                        owner, generation=3, result="timeout_returned",
                    )
                render_matrix.physical_swap(saved)
                self.assertEqual(publisher.state, publisher.DRAFT_PUBLISHED)
                with self.assertRaisesRegex(ValueError, "acceptance result is invalid"):
                    controller.mark_acceptance_result(
                        owner, generation=4, result="timeout_returned",
                    )
                now[0] = 110.0
                self.assertIsNone(controller.device_preview_manager.active_lease())
                self.assertTrue(stop_event.is_set())
                render_matrix.physical_swap(saved)
                self.assertEqual(publisher.state, publisher.DRAFT_PUBLISHED)
                return True

            admission = controller.submit_device_draft(
                DevicePreviewRequest(owner, 4, {"module": "clock_lava"}, 10),
                renderer=renderer,
            )
            self.assertIs(admission.status, PreviewAdmissionStatus.ACCEPTED)
            self.assertTrue(controller._execute_pending_device_draft())

            def execute(name, config, duration):
                self.assertEqual(name, "sleep")
                matrix.physical_swap(saved)
                controller._shutdown.set()
                return True

            controller._execute = execute
            controller._run_loop_unchecked()

            expected = {
                "state": "SAVED_ROLLBACK_PUBLISHED", "nonce": "4" * 64,
                "acceptance_operation_id": "2" * 64,
                "owner_generation": 4, "saved_revision": 10,
                "draft_revision": 11, "current_revision": 14,
                "foreign_rejected": True, "timeout_returned": True,
                "sleep_observed": True, "rollback_complete": True,
            }
            self.assertEqual(controller.acceptance_transaction_status(owner, generation=4), expected)
            self.assertEqual(set(expected), {
                "state", "nonce", "acceptance_operation_id", "owner_generation",
                "saved_revision", "draft_revision", "current_revision",
                "foreign_rejected", "timeout_returned", "sleep_observed",
                "rollback_complete",
            })
            self.assertIsNone(controller.acceptance_transaction_status(foreign, generation=4))
            self.assertIsNone(controller.acceptance_transaction_status(owner, generation=3))
            self.assertIsNone(matrix.observer)
            metadata = json.loads((publisher.path / "metadata.json").read_text())
            self.assertEqual(metadata["owner_generation"], 4)
            self.assertEqual(metadata["owner_revision"], 7)
            self.assertEqual((publisher.path / "draft.rgb").read_bytes(), draft)
            self.assertEqual((publisher.path / "saved.rgb").read_bytes(), saved)
            self.assertEqual((publisher.path / "current.rgb").read_bytes(), saved)
            controller.device_preview_manager.close()

    def test_reload_rejects_execution_prepared_before_reload_completed(self):
        controller = DisplayController(Mock(), Mock())
        import_started = threading.Event()
        release_import = threading.Event()
        module = Mock()
        module.run.return_value = True
        results = []

        def delayed_import(_name):
            import_started.set()
            self.assertTrue(release_import.wait(1))
            return module

        worker = threading.Thread(
            target=lambda: results.append(controller._execute("weather", {}, 1))
        )
        try:
            with patch("display_controller.importlib.import_module", side_effect=delayed_import):
                worker.start()
                self.assertTrue(import_started.wait(0.5))
                self.assertTrue(controller.reload(timeout=0.5))
                release_import.set()
                worker.join(1)
        finally:
            release_import.set()
            worker.join(1)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        module.run.assert_not_called()

    def test_scheduled_module_revalidates_reload_admission_after_display_wait(self):
        controller = DisplayController(Mock(), Mock())
        real_lock = controller._display_lock
        module = Mock()
        module.run.return_value = True

        class ReloadBeforeDisplayOwnership:
            def __enter__(self):
                real_lock.acquire()
                with controller._state_lock:
                    controller._module_admission_generation += 1
                    generation = controller._module_admission_generation
                    controller._reload_obligations[generation] = "active"
                    controller._sync_reload_fence_aliases_locked()
                return self

            def __exit__(self, *_args):
                real_lock.release()

        object.__setattr__(controller, "_display_lock", ReloadBeforeDisplayOwnership())
        try:
            with patch("display_controller.importlib.import_module", return_value=module):
                result = controller._execute("weather", {}, 1)
        finally:
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(result)
        module.run.assert_not_called()
        self.assertIsNone(controller._module_stop)

    def test_reload_fences_scheduler_admission_before_waiting_for_display(self):
        controller = DisplayController(Mock(), Mock())
        old_stop = threading.Event()
        controller._module_stop = old_stop
        old_owner_ready = threading.Event()
        allow_scheduler = threading.Event()
        scheduler_lock_acquired = threading.Event()
        scheduler_done = threading.Event()
        release_new_owner = threading.Event()
        module = Mock()

        def run_new_owner(*_args, **_kwargs):
            release_new_owner.wait(1)
            return True

        module.run.side_effect = run_new_owner

        class AdmissionOrderedLock:
            def __init__(self):
                self.lock = threading.RLock()

            def acquire(self, blocking=True, timeout=-1):
                if threading.current_thread().name == "reload-race-reloader":
                    allow_scheduler.set()
                    deadline = time.monotonic() + 0.5
                    while not (
                        scheduler_lock_acquired.is_set() or scheduler_done.is_set()
                    ) and time.monotonic() < deadline:
                        time.sleep(0.001)
                if timeout == -1:
                    acquired = self.lock.acquire(blocking)
                else:
                    acquired = self.lock.acquire(blocking, timeout)
                if (
                    acquired
                    and threading.current_thread().name == "reload-race-scheduler"
                ):
                    scheduler_lock_acquired.set()
                return acquired

            def release(self):
                self.lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_args):
                self.release()

        object.__setattr__(controller, "_display_lock", AdmissionOrderedLock())

        def old_owner():
            with controller._display_lock:
                old_owner_ready.set()
                old_stop.wait(1)

        def scheduler():
            try:
                self.assertTrue(allow_scheduler.wait(0.5))
                controller._execute("weather", {}, 1)
            finally:
                scheduler_done.set()

        reload_results = []
        owner = threading.Thread(target=old_owner, name="reload-race-old-owner")
        queued = threading.Thread(target=scheduler, name="reload-race-scheduler")
        reloader = threading.Thread(
            target=lambda: reload_results.append(controller.reload(timeout=0.1)),
            name="reload-race-reloader",
        )
        try:
            with patch("display_controller.importlib.import_module", return_value=module):
                owner.start()
                self.assertTrue(old_owner_ready.wait(0.5))
                queued.start()
                reloader.start()
                reloader.join(0.5)
        finally:
            release_new_owner.set()
            old_stop.set()
            owner.join(1)
            queued.join(1)
            reloader.join(1)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(reloader.is_alive())
        self.assertEqual(reload_results, [True])
        self.assertFalse(scheduler_lock_acquired.is_set())
        module.run.assert_not_called()

    def test_active_reload_fence_survives_real_scheduler_acknowledgement(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["playlist"] = [
            item for item in config["playlist"] if item["module"] == "weather"
        ]
        store = Mock(get=Mock(return_value=config))
        controller = DisplayController(Mock(), store)
        controller._is_active = lambda *_args: True
        controller._seconds_until_schedule_change = lambda *_args: 30
        old_stop = threading.Event()
        controller._module_stop = old_stop
        old_owner_ready = threading.Event()
        reloader_at_lock = threading.Event()
        allow_reloader = threading.Event()
        reload_done = threading.Event()
        replacement_started = threading.Event()
        release_replacement = threading.Event()
        reload_results = []
        reload_errors = []
        real_lock = controller._display_lock

        class ReloadOrderedLock:
            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == "full-loop-reloader":
                    reloader_at_lock.set()
                    if not allow_reloader.wait(2):
                        raise TimeoutError("reload interleaving gate")
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                real_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_args):
                self.release()

        controller._display_lock = ReloadOrderedLock()

        def old_owner():
            with controller._display_lock:
                old_owner_ready.set()
                old_stop.wait(2)

        module = Mock()

        def run_replacement(*_args, **_kwargs):
            replacement_started.set()
            release_replacement.wait(2)
            controller._shutdown.set()
            return True

        module.run.side_effect = run_replacement

        def reload():
            try:
                reload_results.append(controller.reload(timeout=1))
            except BaseException as error:
                reload_errors.append(error)
            finally:
                reload_done.set()

        owner = threading.Thread(target=old_owner, name="full-loop-old-owner")
        scheduler = threading.Thread(
            target=controller._run_loop_unchecked, name="full-loop-scheduler",
        )
        reloader = threading.Thread(target=reload, name="full-loop-reloader")
        replacement_before_settlement = None
        reload_settled_after_release = None
        try:
            with patch("display_controller.importlib.import_module", return_value=module):
                owner.start()
                self.assertTrue(old_owner_ready.wait(0.5))
                reloader.start()
                self.assertTrue(reloader_at_lock.wait(0.5))
                self.assertTrue(old_stop.is_set())
                scheduler.start()
                replacement_before_settlement = replacement_started.wait(0.2)
                allow_reloader.set()
                reload_settled_after_release = reload_done.wait(0.3)
                self.assertTrue(replacement_started.wait(0.5))
        finally:
            allow_reloader.set()
            release_replacement.set()
            old_stop.set()
            controller._shutdown.set()
            owner.join(1)
            scheduler.join(1)
            reloader.join(1)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(owner.is_alive())
        self.assertFalse(scheduler.is_alive())
        self.assertFalse(reloader.is_alive())
        self.assertEqual(reload_errors, [])
        self.assertFalse(
            replacement_before_settlement,
            "the scheduler retired a fence still owned by the active reload caller",
        )
        self.assertTrue(reload_settled_after_release)
        self.assertEqual(reload_results, [True])
        module.run.assert_called_once()

    def test_newer_reload_cannot_retire_an_older_active_reload_obligation(self):
        for newer_timeout, expected_newer in ((1, True), (0, False)):
            with self.subTest(newer_timeout=newer_timeout):
                controller = DisplayController(Mock(), Mock())
                real_lock = controller._display_lock
                older_waiting = threading.Event()
                release_older = threading.Event()

                class OrderedDisplayLock:
                    def acquire(self, *args, **kwargs):
                        if threading.current_thread().name == "reload-A":
                            older_waiting.set()
                            if not release_older.wait(2):
                                raise TimeoutError("older reload interleaving gate")
                        return real_lock.acquire(*args, **kwargs)

                    def release(self):
                        real_lock.release()

                    def __enter__(self):
                        self.acquire()
                        return self

                    def __exit__(self, *_args):
                        self.release()

                object.__setattr__(controller, "_display_lock", OrderedDisplayLock())
                results = {"older": [], "newer": []}
                older = threading.Thread(
                    target=lambda: results["older"].append(controller.reload(timeout=1)),
                    name="reload-A",
                )
                newer = threading.Thread(
                    target=lambda: results["newer"].append(
                        controller.reload(timeout=newer_timeout)
                    ),
                    name="reload-B",
                )
                try:
                    if newer_timeout == 0:
                        real_lock.acquire()
                    older.start()
                    self.assertTrue(older_waiting.wait(0.5))
                    newer.start()
                    newer.join(0.5)
                    self.assertFalse(newer.is_alive())
                    self.assertEqual(results["newer"], [expected_newer])
                    if newer_timeout == 0:
                        real_lock.release()
                    self.assertFalse(
                        controller._acknowledge_reload_boundary(),
                        "newer reload retired admission protection owed to reload A",
                    )
                    self.assertTrue(controller._reload_requested.is_set())
                    release_older.set()
                    older.join(1)
                    self.assertEqual(results["older"], [True])
                    self.assertTrue(controller._acknowledge_reload_boundary())
                    self.assertFalse(controller._reload_requested.is_set())
                finally:
                    if newer_timeout == 0:
                        try:
                            real_lock.release()
                        except RuntimeError:
                            pass
                    release_older.set()
                    older.join(1)
                    newer.join(1)
                    controller.device_preview_manager.close()
                    controller._openweather_provider.shutdown()

    def test_dequeued_direct_media_preview_is_rejected_during_reload_admission(self):
        controller = DisplayController(Mock(), Mock())
        preview = display_controller._PinnedMediaPreview(
            "selected.gif", Mock(), Mock(),
        )
        with controller._state_lock:
            controller._module_admission_generation += 1
            controller._reload_admission_fence = controller._module_admission_generation
            controller._reload_fence_state = "active"
            controller._reload_requested.set()

        try:
            with patch("display_controller.play_media", return_value=True) as player:
                self.assertFalse(controller._execute_media(preview, 15))
            player.assert_not_called()
            self.assertIsNone(controller._module_stop)
        finally:
            preview.close()
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

    def test_direct_media_preview_revalidates_reload_admission_after_display_wait(self):
        controller = DisplayController(Mock(), Mock())
        real_lock = controller._display_lock
        preview = display_controller._PinnedMediaPreview(
            "selected.gif", Mock(), Mock(),
        )

        class ReloadBeforeDisplayOwnership:
            def __enter__(self):
                real_lock.acquire()
                with controller._state_lock:
                    controller._module_admission_generation += 1
                    generation = controller._module_admission_generation
                    controller._reload_obligations[generation] = "active"
                    controller._sync_reload_fence_aliases_locked()
                return self

            def __exit__(self, *_args):
                real_lock.release()

        object.__setattr__(controller, "_display_lock", ReloadBeforeDisplayOwnership())
        try:
            with patch("display_controller.play_media", return_value=True) as player:
                result = controller._execute_media(preview, 15)
        finally:
            preview.close()
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(result)
        player.assert_not_called()
        self.assertIsNone(controller._module_stop)

    def _assert_saved_screen_preview_metadata_cancelled(
        self, screen, callers, lifecycle="stop",
    ):
        baseline_fds = set(os.listdir("/proc/self/fd"))
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "still.png")
            frames = (
                Image.new("RGB", (2, 2), "green"),
                Image.new("RGB", (2, 2), "blue"),
            )
            try:
                frames[0].save(
                    media_root / "pool.gif", save_all=True,
                    append_images=[frames[1]], duration=[3000, 3000], loop=0,
                )
            finally:
                for frame in frames:
                    frame.close()

            config = copy.deepcopy(DEFAULT_CONFIG)
            row = next(
                item for item in config["playlist"] if item["module"] == screen
            )
            row.update(source="still.png", sources=["still.png", "pool.gif"])
            config["media"][screen] = "still.png"
            original_config = copy.deepcopy(config)
            store = Mock(
                media_root=media_root,
                get=Mock(side_effect=lambda: copy.deepcopy(config)),
            )
            controller = DisplayController(Mock(), store)
            controller.start = Mock()
            entered = threading.Condition()
            release = threading.Event()
            local = threading.local()
            streams = []
            done = [threading.Event() for _ in range(callers)]
            errors = []
            owned_workers = ()
            owned_timers = ()
            original_read = display_controller.ScreenMediaCatalog._read_info
            original_open = Image.open

            def read_info(reference, pinned, identity):
                local.reading_timing_pool = reference == "pool.gif"
                try:
                    return original_read(reference, pinned, identity)
                finally:
                    local.reading_timing_pool = False

            def open_image(source, *args, **kwargs):
                if getattr(local, "reading_timing_pool", False):
                    with entered:
                        streams.append(source)
                        entered.notify_all()
                    if not release.wait(3):
                        raise TimeoutError("saved Preview metadata test gate was not released")
                return original_open(source, *args, **kwargs)

            def request_preview(index):
                try:
                    controller.preview(screen)
                except BaseException as error:
                    errors.append(error)
                finally:
                    done[index].set()

            requests = [
                threading.Thread(
                    target=request_preview, args=(index,),
                    name=f"saved-{screen}-metadata-{index}",
                )
                for index in range(callers)
            ]
            try:
                with (
                    patch.object(
                        display_controller.ScreenMediaCatalog, "_read_info",
                        side_effect=read_info,
                    ),
                    patch.object(Image, "open", side_effect=open_image),
                    patch("display_controller.random.choice", return_value="still.png"),
                ):
                    for request in requests:
                        request.start()
                    with entered:
                        self.assertTrue(entered.wait_for(
                            lambda: len(streams) >= min(callers, 4), timeout=1,
                        ))
                    owned_workers = tuple(
                        thread for thread in threading.enumerate()
                        if thread.ident not in baseline_threads
                        and thread.name == "matrix-preview-pillow"
                    )
                    if lifecycle == "stop":
                        lifecycle_result = controller.stop(timeout=1)
                    elif lifecycle == "reload":
                        lifecycle_result = controller.reload(timeout=1)
                    else:
                        controller.show_pairing_pin("123456", ttl_seconds=60)
                        lifecycle_result = True
                    with controller._state_lock:
                        owned_timers = tuple(
                            timer for timer in (
                                controller._pairing_timer,
                                controller._pairing_page_timer,
                            )
                            if timer is not None
                        )
                    callers_finished = all(event.wait(0.5) for event in done)
                    streams_closed = all(stream.closed for stream in streams)
                    admitted = len(streams)
            finally:
                release.set()
                for request in requests:
                    request.join(2)
                controller.end_pairing(timeout=1)
                controller.stop(timeout=1)
                controller.device_preview_manager.close()
                controller._openweather_provider.shutdown()
                for owner in (*owned_workers, *owned_timers):
                    owner.join(2)
                    self.assertFalse(
                        owner.is_alive(),
                        f"fixture-owned thread did not stop: {owner.name}",
                    )

            self.assertTrue(lifecycle_result)
            self.assertTrue(
                callers_finished,
                "stop reported completion while saved Preview metadata callers remained",
            )
            self.assertTrue(
                streams_closed,
                "stop reported completion while saved Preview metadata streams remained open",
            )
            self.assertLessEqual(admitted, 4)
            self.assertEqual(len(errors), callers)
            self.assertTrue(all(isinstance(error, RuntimeError) for error in errors))
            self.assertTrue(all("lifecycle" in str(error) for error in errors))
            self.assertEqual(config, original_config)

        self.assertEqual(set(os.listdir("/proc/self/fd")), baseline_fds)
        self.assertEqual(
            [
                thread.name for thread in threading.enumerate()
                if thread.ident not in baseline_threads
            ],
            [],
        )

    def test_saved_splash_preview_metadata_callers_cancel_on_stop(self):
        for callers in (1, 5):
            with self.subTest(callers=callers):
                self._assert_saved_screen_preview_metadata_cancelled(
                    "splash", callers,
                )

    def test_saved_sleep_preview_metadata_callers_cancel_on_stop(self):
        for callers in (1, 5):
            with self.subTest(callers=callers):
                self._assert_saved_screen_preview_metadata_cancelled(
                    "sleep", callers,
                )

    def test_saved_screen_preview_metadata_cancels_on_reload_and_pairing(self):
        for screen in ("splash", "sleep"):
            for lifecycle in ("reload", "pairing"):
                with self.subTest(screen=screen, lifecycle=lifecycle):
                    self._assert_saved_screen_preview_metadata_cancelled(
                        screen, 1, lifecycle,
                    )

    def _assert_saved_screen_preview_reload_timeout_settles(
        self, screen, callers, concurrent_reloads,
    ):
        baseline_fds = set(os.listdir("/proc/self/fd"))
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "still.png")
            config = copy.deepcopy(DEFAULT_CONFIG)
            row = next(
                item for item in config["playlist"] if item["module"] == screen
            )
            row.update(source="still.png", sources=["still.png"])
            config["media"][screen] = "still.png"
            original_config = copy.deepcopy(config)
            store = Mock(
                media_root=media_root,
                get=Mock(side_effect=lambda: copy.deepcopy(config)),
            )
            controller = DisplayController(Mock(), store)
            controller.start = Mock(return_value=True)
            gate = threading.Condition()
            release = threading.Event()
            preparations = []
            errors = []
            reload_results = []
            original_duration = edit_preview.bounded_screen_preview_duration_seconds

            def blocked_duration(*args, **kwargs):
                duration = original_duration(*args, **kwargs)
                with gate:
                    preparations.append(kwargs["stop_event"])
                    gate.notify_all()
                if not release.wait(2):
                    raise TimeoutError("saved Preview transfer gate was not released")
                return duration

            def request_preview():
                try:
                    controller.preview(screen)
                except BaseException as error:
                    errors.append(error)

            reload_barrier = threading.Barrier(concurrent_reloads)

            def reload_display():
                reload_barrier.wait(timeout=1)
                reload_results.append(controller.reload(timeout=0.1))

            requests = [
                threading.Thread(
                    target=request_preview,
                    name=f"saved-{screen}-timeout-preview-{index}",
                )
                for index in range(callers)
            ]
            reloaders = [
                threading.Thread(
                    target=reload_display,
                    name=f"saved-{screen}-timeout-reload-{index}",
                )
                for index in range(concurrent_reloads)
            ]
            try:
                with patch.object(
                    edit_preview,
                    "bounded_screen_preview_duration_seconds",
                    side_effect=blocked_duration,
                ):
                    for request in requests:
                        request.start()
                    with gate:
                        self.assertTrue(gate.wait_for(
                            lambda: len(preparations) == callers, timeout=1,
                        ))
                    for reloader in reloaders:
                        reloader.start()
                    self.assertTrue(all(
                        preparation.wait(1) for preparation in preparations
                    ))
                    deadline = time.monotonic() + 0.5
                    installed = 0
                    while time.monotonic() < deadline:
                        with controller._state_lock:
                            installed = len(controller._reload_obligations)
                        if installed == concurrent_reloads:
                            break
                        time.sleep(0.001)
                    self.assertEqual(installed, concurrent_reloads)
                    for reloader in reloaders:
                        reloader.join(1)
                    self.assertTrue(all(not reloader.is_alive() for reloader in reloaders))
                    self.assertEqual(reload_results, [False] * concurrent_reloads)
                    release.set()
                    for request in requests:
                        request.join(1)
                self.assertTrue(all(not request.is_alive() for request in requests))
                self.assertTrue(controller._acknowledge_reload_boundary())
                self.assertEqual(controller._reload_obligations, {})
                self.assertFalse(controller._reload_requested.is_set())
                self.assertEqual(config, original_config)
            finally:
                release.set()
                for thread in requests + reloaders:
                    thread.join(2)
                pending = controller._take_preview()
                if pending is not None and hasattr(pending, "close"):
                    pending.close()
                controller.stop(timeout=1)
                controller.device_preview_manager.close()
                controller._openweather_provider.shutdown()

        self.assertEqual(set(os.listdir("/proc/self/fd")), baseline_fds)
        self.assertEqual(
            [
                thread.name for thread in threading.enumerate()
                if thread.ident not in baseline_threads
            ],
            [],
        )

    def test_saved_screen_preview_reload_timeout_settles_exact_obligations(self):
        for screen in ("splash", "sleep"):
            for callers in (1, 5):
                for concurrent_reloads in (1, 3):
                    with self.subTest(
                        screen=screen,
                        callers=callers,
                        concurrent_reloads=concurrent_reloads,
                    ):
                        self._assert_saved_screen_preview_reload_timeout_settles(
                            screen, callers, concurrent_reloads,
                        )

    def _assert_saved_screen_preview_revalidates_preparation_before_transfer(
        self, screen, callers, lifecycle,
    ):
        baseline_fds = set(os.listdir("/proc/self/fd"))
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "still.png")
            config = copy.deepcopy(DEFAULT_CONFIG)
            row = next(
                item for item in config["playlist"] if item["module"] == screen
            )
            row.update(source="still.png", sources=["still.png"])
            config["media"][screen] = "still.png"
            original_config = copy.deepcopy(config)
            store = Mock(
                media_root=media_root,
                get=Mock(side_effect=lambda: copy.deepcopy(config)),
            )
            controller = DisplayController(
                Mock(), store, ip_address_provider=lambda: "192.0.2.1",
            )
            controller.start = Mock(return_value=True)
            gate = threading.Condition()
            release = threading.Event()
            preparations = []
            errors = []
            lifecycle_results = []
            owned_timers = ()
            original_duration = edit_preview.bounded_screen_preview_duration_seconds

            def blocked_duration(*args, **kwargs):
                duration = original_duration(*args, **kwargs)
                with gate:
                    preparations.append(kwargs["stop_event"])
                    gate.notify_all()
                if not release.wait(2):
                    raise TimeoutError("saved Preview transfer gate was not released")
                return duration

            def request_preview():
                try:
                    controller.preview(screen)
                except BaseException as error:
                    errors.append(error)

            def revoke_preview():
                try:
                    if lifecycle == "reload":
                        lifecycle_results.append(controller.reload(timeout=1))
                    else:
                        controller.show_pairing_pin("123456", ttl_seconds=60)
                        lifecycle_results.append(True)
                except BaseException as error:
                    lifecycle_results.append(error)

            requests = [
                threading.Thread(
                    target=request_preview,
                    name=f"saved-{screen}-{lifecycle}-transfer-{index}",
                )
                for index in range(callers)
            ]
            revoker = threading.Thread(
                target=revoke_preview,
                name=f"saved-{screen}-{lifecycle}-revoker",
            )
            pending = None
            try:
                with patch.object(
                    edit_preview,
                    "bounded_screen_preview_duration_seconds",
                    side_effect=blocked_duration,
                ):
                    for request in requests:
                        request.start()
                    with gate:
                        self.assertTrue(gate.wait_for(
                            lambda: len(preparations) == callers, timeout=1,
                        ))
                    revoker.start()
                    self.assertTrue(all(
                        preparation.wait(1) for preparation in preparations
                    ))
                    release.set()
                    for request in requests:
                        request.join(1)
                    revoker.join(1)
                self.assertTrue(all(not request.is_alive() for request in requests))
                self.assertFalse(revoker.is_alive())
                self.assertEqual(lifecycle_results, [True])
                self.assertEqual(len(errors), callers)
                self.assertTrue(all(isinstance(error, RuntimeError) for error in errors))
                pending = controller._take_preview()
                self.assertIsNone(pending)
                controller.start.assert_not_called()
                self.assertEqual(controller._preview_preparations, set())
                self.assertEqual(config, original_config)
            finally:
                release.set()
                for thread in requests + [revoker]:
                    thread.join(2)
                if pending is None:
                    pending = controller._take_preview()
                if pending is not None and hasattr(pending, "close"):
                    pending.close()
                with controller._state_lock:
                    owned_timers = tuple(
                        timer for timer in (
                            controller._pairing_timer,
                            controller._pairing_page_timer,
                        )
                        if timer is not None
                    )
                controller.end_pairing(timeout=1)
                for timer in owned_timers:
                    timer.join(2)
                    self.assertFalse(
                        timer.is_alive(),
                        f"fixture-owned pairing timer did not stop: {timer.name}",
                    )
                if lifecycle == "reload":
                    controller._acknowledge_reload_boundary()
                controller.stop(timeout=1)
                controller.device_preview_manager.close()
                controller._openweather_provider.shutdown()

        self.assertEqual(set(os.listdir("/proc/self/fd")), baseline_fds)
        self.assertEqual(
            [
                thread.name for thread in threading.enumerate()
                if thread.ident not in baseline_threads
            ],
            [],
        )

    def test_saved_screen_preview_revalidates_cancelled_preparation_at_transfer(self):
        for screen in ("splash", "sleep"):
            for callers in (1, 5):
                for lifecycle in ("reload", "pairing"):
                    with self.subTest(
                        screen=screen, callers=callers, lifecycle=lifecycle,
                    ):
                        self._assert_saved_screen_preview_revalidates_preparation_before_transfer(
                            screen, callers, lifecycle,
                        )

    def _assert_saved_screen_preview_captures_revocation_before_resolution(
        self, screen, callers, lifecycle,
    ):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "old.png")
            Image.new("RGB", (2, 2), "green").save(media_root / "new.png")
            config = copy.deepcopy(DEFAULT_CONFIG)
            row = next(
                item for item in config["playlist"] if item["module"] == screen
            )
            row.update(source="old.png", sources=["old.png"])
            config["media"][screen] = "old.png"
            store = Mock(
                media_root=media_root,
                get=Mock(side_effect=lambda: copy.deepcopy(config)),
            )
            controller = DisplayController(
                Mock(), store, ip_address_provider=lambda: "192.0.2.1",
            )
            controller.start = Mock(return_value=True)
            arrived = threading.Condition()
            blocked = []
            release = threading.Event()
            errors = []
            original_resolve = display_controller.MediaResolver.resolve

            class FakeTimer:
                def __init__(self, _interval, _callback):
                    self.daemon = False

                def start(self):
                    pass

                def cancel(self):
                    pass

            def blocked_resolve(resolver, reference):
                pinned = original_resolve(resolver, reference)
                if reference == "old.png":
                    with arrived:
                        blocked.append(pinned)
                        arrived.notify_all()
                    if not release.wait(3):
                        raise TimeoutError("pre-registration resolution gate was not released")
                return pinned

            def request_preview():
                try:
                    controller.preview(screen)
                except BaseException as error:
                    errors.append(error)

            dormant = lifecycle.startswith("dormant-")
            requests = [
                threading.Thread(
                    target=request_preview,
                    name=f"saved-{screen}-{lifecycle}-pre-registration-{index}",
                )
                for index in range(callers)
            ]
            pending = None
            try:
                if dormant:
                    self.assertTrue(controller.stop(timeout=1))
                with (
                    patch.object(
                        display_controller.MediaResolver, "resolve",
                        new=blocked_resolve,
                    ),
                    patch("display_controller.threading.Timer", FakeTimer),
                ):
                    for request in requests:
                        request.start()
                    with arrived:
                        self.assertTrue(arrived.wait_for(
                            lambda: len(blocked) == callers, timeout=1,
                        ))
                    self.assertEqual(controller._preview_preparations, set())
                    if lifecycle == "stop":
                        self.assertTrue(controller.stop(timeout=1))
                    elif lifecycle == "pairing":
                        controller.show_pairing_pin("123456", ttl_seconds=60)
                        self.assertTrue(controller.end_pairing(timeout=1))
                    else:
                        if "save" in lifecycle:
                            next_config = copy.deepcopy(config)
                            next_row = next(
                                item for item in next_config["playlist"]
                                if item["module"] == screen
                            )
                            next_row.update(source="new.png", sources=["new.png"])
                            next_config["media"][screen] = "new.png"
                            config.clear()
                            config.update(next_config)
                        self.assertTrue(controller.reload(timeout=1))
                        if not dormant:
                            self.assertTrue(controller._acknowledge_reload_boundary())
                    release.set()
                    for request in requests:
                        request.join(2)
                self.assertTrue(all(not request.is_alive() for request in requests))
                self.assertEqual(len(errors), callers)
                self.assertTrue(all(isinstance(error, RuntimeError) for error in errors))
                self.assertTrue(all("lifecycle" in str(error) for error in errors))
                pending = controller._take_preview()
                self.assertIsNone(pending)
                self.assertTrue(all(pinned.closed for pinned in blocked))
            finally:
                release.set()
                for request in requests:
                    request.join(2)
                if pending is None:
                    pending = controller._take_preview()
                if pending is not None and hasattr(pending, "close"):
                    pending.close()
                controller.end_pairing(timeout=1)
                if controller._reload_obligations:
                    controller._acknowledge_reload_boundary()
                controller.stop(timeout=1)
                controller.device_preview_manager.close()
                controller._openweather_provider.shutdown()

    def test_saved_screen_preview_captures_revocation_before_media_resolution(self):
        for screen in ("splash", "sleep"):
            for callers in (1, 5):
                for lifecycle in (
                    "stop", "reload", "save-reload", "pairing",
                    "dormant-reload", "dormant-save-reload",
                ):
                    with self.subTest(
                        screen=screen, callers=callers, lifecycle=lifecycle,
                    ):
                        self._assert_saved_screen_preview_captures_revocation_before_resolution(
                            screen, callers, lifecycle,
                        )

    def test_saved_screen_still_preview_uses_pool_hold_through_static_frames(self):
        original_static_media_frames = edit_preview.static_media_frames
        for screen, legacy_duration in (("splash", 1), ("sleep", 3600)):
            with self.subTest(screen=screen), tempfile.TemporaryDirectory() as directory:
                media_root = Path(directory) / "media"
                media_root.mkdir()
                Image.new("RGB", (2, 2), "red").save(media_root / "still.png")
                config = copy.deepcopy(DEFAULT_CONFIG)
                row = next(
                    item for item in config["playlist"] if item["module"] == screen
                )
                row.update(
                    source="still.png", sources=["still.png"],
                    duration=legacy_duration,
                )
                config["media"][screen] = "still.png"
                store = Mock(media_root=media_root, get=Mock(return_value=config))
                matrix = Mock()
                matrix.show_image.return_value = True
                controller = DisplayController(matrix, store)
                controller._mark_scheduler_healthy = lambda: controller._shutdown.set()
                observed = []

                def record_static_frames(filename, duration, settings=None, **kwargs):
                    times = iter((100.0, 100.125))
                    iterator = original_static_media_frames(
                        filename, duration, settings,
                        **{**kwargs, "monotonic": lambda: next(times)},
                    )
                    for frame, delay_ms, geometry in iterator:
                        observed.append((duration, delay_ms))
                        yield frame, 0, geometry

                try:
                    with (
                        patch.object(controller, "start"),
                        patch(
                            "edit_preview.static_media_frames",
                            side_effect=record_static_frames,
                        ),
                    ):
                        controller.preview(screen)
                        controller._run_loop_unchecked()
                finally:
                    controller.device_preview_manager.close()
                    controller._openweather_provider.shutdown()

                self.assertEqual(observed, [(7.0, 6875.0)])

    def test_saved_screen_still_preview_uses_pinned_usable_gif_pool_mean(self):
        original_static_media_frames = edit_preview.static_media_frames
        cases = (
            (10.0, 1, (250, 250), 5.0),
            (10.0, 99, (250, 250), 5.0),
            (400.0, 3600, (20000, 20000), 10.0),
        )
        for screen in ("splash", "sleep"):
            for speed, legacy_duration, gif_delays, expected in cases:
                with (
                    self.subTest(screen=screen, speed=speed),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    media_root = Path(directory) / "media"
                    media_root.mkdir()
                    Image.new("RGB", (2, 2), "red").save(media_root / "still.png")
                    frames = (
                        Image.new("RGB", (2, 2), "green"),
                        Image.new("RGB", (2, 2), "blue"),
                    )
                    try:
                        frames[0].save(
                            media_root / "timed.gif", save_all=True,
                            append_images=[frames[1]], duration=gif_delays, loop=0,
                        )
                    finally:
                        for frame in frames:
                            frame.close()
                    (media_root / "invalid.gif").write_bytes(b"not-an-image")
                    config = copy.deepcopy(DEFAULT_CONFIG)
                    row = next(
                        item for item in config["playlist"]
                        if item["module"] == screen
                    )
                    row.update(
                        source="still.png",
                        sources=[
                            "still.png", "timed.gif", "missing.gif", "invalid.gif",
                        ],
                        duration=legacy_duration,
                        enabled=False,
                        layout={
                            **row["layout"], "playback_speed_percent": speed,
                        },
                    )
                    decoy = copy.deepcopy(row)
                    decoy.update(
                        id=f"{screen}_enabled_decoy",
                        source="decoy.png", sources=["decoy.png"], enabled=True,
                    )
                    config["playlist"].insert(
                        config["playlist"].index(row) + 1, decoy,
                    )
                    config["media"][screen] = "still.png"
                    store = Mock(media_root=media_root, get=Mock(return_value=config))
                    matrix = Mock()
                    matrix.show_image.return_value = True
                    controller = DisplayController(matrix, store)
                    controller._screen_runtime = Mock()
                    controller._mark_scheduler_healthy = lambda: controller._shutdown.set()
                    observed = []

                    def record_static_frames(filename, duration, settings=None, **kwargs):
                        times = iter((100.0, 100.125))
                        iterator = original_static_media_frames(
                            filename, duration, settings,
                            **{**kwargs, "monotonic": lambda: next(times)},
                        )
                        for frame, delay_ms, geometry in iterator:
                            observed.append((duration, delay_ms))
                            yield frame, 0, geometry

                    try:
                        with (
                            patch.object(controller, "start"),
                            patch("display_controller.random.choice", return_value="still.png"),
                        ):
                            controller.preview(screen)
                        pending = controller._preview_name
                        self.assertIsNotNone(pending)
                        assert pending is not None
                        self.assertEqual(pending.duration, expected)
                        self.assertEqual(set(pending.pinned_handles), {
                            "still.png", "timed.gif", "invalid.gif",
                        })
                        controller._screen_runtime.assert_not_called()
                        controller._screen_runtime.choose_splash.assert_not_called()
                        controller._screen_runtime.begin_sleep.assert_not_called()
                        controller._screen_runtime.current_sleep.assert_not_called()
                        with patch(
                            "edit_preview.static_media_frames",
                            side_effect=record_static_frames,
                        ):
                            controller._run_loop_unchecked()
                        self.assertEqual(observed, [(expected, expected * 1000 - 125)])
                        self.assertTrue(
                            all(handle.closed for handle in pending.pinned_handles.values())
                        )
                    finally:
                        controller.device_preview_manager.close()
                        controller._openweather_provider.shutdown()

    def test_reload_signals_scheduled_module_before_waiting_for_display_quiescence(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        module_stop = threading.Event()
        controller._module_stop = module_stop
        owns_display = threading.Event()

        def scheduled_render():
            with controller._display_lock:
                owns_display.set()
                module_stop.wait(2)

        renderer = threading.Thread(target=scheduled_render)
        reload_done = threading.Event()
        reloader = threading.Thread(
            target=lambda: (controller.reload(), reload_done.set())
        )
        renderer.start()
        self.assertTrue(owns_display.wait(0.5))
        reloader.start()
        try:
            self.assertTrue(
                module_stop.wait(0.3),
                "reload waited for display ownership before signalling cancellation",
            )
            self.assertTrue(reload_done.wait(0.5))
            self.assertTrue(controller._reload_requested.is_set())
            self.assertEqual(controller._reload_fence_state, "settled-success")
        finally:
            module_stop.set()
            renderer.join(1)
            reloader.join(1)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()
        self.assertFalse(renderer.is_alive())
        self.assertFalse(reloader.is_alive())

    def test_pairing_signals_scheduled_module_before_waiting_for_display_quiescence(self):
        matrix = Mock()
        controller = DisplayController(
            matrix, Mock(), ip_address_provider=lambda: "192.0.2.1",
        )
        module_stop = threading.Event()
        controller._module_stop = module_stop
        owns_display = threading.Event()

        def scheduled_render():
            with controller._display_lock:
                owns_display.set()
                module_stop.wait(2)

        renderer = threading.Thread(target=scheduled_render)
        pairing_result = []
        pairing_done = threading.Event()

        def pair():
            try:
                pairing_result.append(controller.show_pairing_pin("123456", ttl_seconds=60))
            finally:
                pairing_done.set()

        pairing = threading.Thread(target=pair)
        renderer.start()
        self.assertTrue(owns_display.wait(0.5))
        pairing.start()
        try:
            self.assertTrue(
                module_stop.wait(0.3),
                "pairing waited for display ownership before signalling cancellation",
            )
            self.assertTrue(pairing_done.wait(0.5))
            self.assertTrue(controller._pairing_pause.is_set())
            self.assertTrue(pairing_result)
        finally:
            module_stop.set()
            renderer.join(1)
            pairing.join(1)
            controller.end_pairing(timeout=0.5)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()
        self.assertFalse(renderer.is_alive())
        self.assertFalse(pairing.is_alive())

    def test_pairing_draw_uses_the_single_post_cancellation_display_acquisition(self):
        class CountingLock:
            def __init__(self):
                self._lock = threading.RLock()
                self.acquisitions = 0

            def acquire(self, *args, **kwargs):
                acquired = self._lock.acquire(*args, **kwargs)
                if acquired:
                    self.acquisitions += 1
                return acquired

            def release(self):
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_args):
                self.release()

        matrix = Mock()
        controller = DisplayController(
            matrix, Mock(), ip_address_provider=lambda: "192.0.2.1",
        )
        display_lock = CountingLock()
        controller._display_lock = display_lock
        module_stop = threading.Event()
        controller._module_stop = module_stop
        owns_display = threading.Event()

        def scheduled_render():
            with display_lock:
                owns_display.set()
                module_stop.wait(2)

        renderer = threading.Thread(target=scheduled_render)
        renderer.start()
        self.assertTrue(owns_display.wait(0.5))
        try:
            controller.show_pairing_pin("123456", ttl_seconds=60)
            self.assertEqual(display_lock.acquisitions, 2)
            matrix.clear.assert_called_once_with()
            matrix.swap.assert_called_once_with()
        finally:
            module_stop.set()
            renderer.join(1)
            controller.end_pairing(timeout=0.5)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()
        self.assertFalse(renderer.is_alive())

    def test_reload_quiescence_timeout_returns_failure_without_hardware_draw(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        module_stop = threading.Event()
        controller._module_stop = module_stop
        owns_display = threading.Event()
        release_display = threading.Event()

        def blocked_render():
            with controller._display_lock:
                owns_display.set()
                release_display.wait(2)

        renderer = threading.Thread(target=blocked_render)
        renderer.start()
        self.assertTrue(owns_display.wait(0.5))
        try:
            self.assertFalse(controller.reload(timeout=0))
            self.assertTrue(module_stop.is_set())
            self.assertTrue(controller._reload_requested.is_set())
            self.assertIsNotNone(controller._reload_admission_fence)
            self.assertEqual(controller._reload_fence_state, "settled-timeout")
            matrix.clear.assert_not_called()
            matrix.swap.assert_not_called()
        finally:
            release_display.set()
            renderer.join(1)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()
        self.assertFalse(renderer.is_alive())
        self.assertTrue(controller._acknowledge_reload_boundary())
        self.assertFalse(controller._reload_requested.is_set())
        self.assertIsNone(controller._reload_admission_fence)

    def test_stale_timeout_acknowledgement_cannot_retire_newer_reload_generation(self):
        controller = DisplayController(Mock(), Mock())
        owns_display = threading.Event()
        release_display = threading.Event()
        acknowledgement_started = threading.Event()
        acknowledgement_results = []

        def blocked_owner():
            with controller._display_lock:
                owns_display.set()
                release_display.wait(2)

        owner = threading.Thread(target=blocked_owner, name="overlap-owner")
        owner.start()
        self.assertTrue(owns_display.wait(0.5))
        try:
            self.assertFalse(controller.reload(timeout=0))
            first_generation = controller._reload_admission_fence

            def acknowledge_first():
                acknowledgement_started.set()
                acknowledgement_results.append(
                    controller._acknowledge_reload_boundary()
                )

            acknowledger = threading.Thread(
                target=acknowledge_first, name="overlap-acknowledger",
            )
            acknowledger.start()
            self.assertTrue(acknowledgement_started.wait(0.5))
            self.assertFalse(controller.reload(timeout=0))
            second_generation = controller._reload_admission_fence
            self.assertGreater(second_generation, first_generation)
            self.assertEqual(controller._reload_fence_state, "settled-timeout")
            release_display.set()
            acknowledger.join(1)
        finally:
            release_display.set()
            owner.join(1)
            if "acknowledger" in locals():
                acknowledger.join(1)
            controller.device_preview_manager.close()
            controller._openweather_provider.shutdown()

        self.assertFalse(owner.is_alive())
        self.assertFalse(acknowledger.is_alive())
        self.assertEqual(acknowledgement_results, [False])
        self.assertEqual(controller._reload_admission_fence, second_generation)
        self.assertTrue(controller._reload_requested.is_set())
        self.assertTrue(controller._acknowledge_reload_boundary())
        self.assertIsNone(controller._reload_admission_fence)
        self.assertIsNone(controller._reload_fence_state)
        self.assertFalse(controller._reload_requested.is_set())


if __name__ == "__main__":
    unittest.main()
