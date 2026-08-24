import base64
import copy
import datetime
import io
import os
from pathlib import Path
import random
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

            def block_replacement_interrupt(timeout=None):
                replacement_interrupt_started.set()
                release_replacement.wait(1)
                return True

            controller._interrupt_module = block_replacement_interrupt
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
        config = {
            **DEFAULT_CONFIG,
            "playlist": [
                {"module": "splash", "duration": 1, "enabled": True},
                {"module": "sleep", "duration": 1, "enabled": True},
            ],
        }
        store = Mock(get=Mock(return_value=config))
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
        store = Mock()
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

    def test_openweather_blocked_preparation_is_cancelled_within_reload_budget(self):
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
        self.assertLessEqual(provider_calls[0]["request_deadline"], 5)
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

        self.assertEqual(controller._take_preview(), "custom_welcome")
        controller.start.assert_called_once_with()

    def test_announcement_preview_queue_is_last_request_wins_and_isolated(self):
        controller = DisplayController(Mock(), Mock())
        controller.start = Mock()
        controller._module_stop = Mock()
        first = {"id": "announcement_first", "headline": "FIRST"}
        second = {"id": "announcement_second", "headline": "SECOND"}

        controller.preview_announcement(first)
        first["headline"] = "MUTATED"
        controller.preview_announcement(second)

        self.assertEqual(controller._take_announcement_preview(), second)
        self.assertIsNone(controller._take_announcement_preview())
        self.assertIsNone(controller._take_preview())
        self.assertIsNone(controller._take_media_preview())
        controller._module_stop.set.assert_called()
        self.assertEqual(controller.start.call_count, 2)

    def test_execute_announcement_preview_uses_exact_draft_and_stop_lifecycle(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        announcement = {"id": "announcement_preview", "enabled": False, "headline": "EXACT"}
        module = Mock()
        module.run_preview.return_value = True

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute_announcement_preview(announcement)

        self.assertTrue(result)
        importer.assert_called_once_with("modules.announcement_board")
        module.run_preview.assert_called_once()
        self.assertIs(module.run_preview.call_args.args[0], matrix)
        self.assertEqual(module.run_preview.call_args.args[1], announcement)
        self.assertIsNotNone(module.run_preview.call_args.kwargs["stop_event"])
        self.assertIsNone(controller.current_module)
        self.assertIsNone(controller._module_stop)

    def test_scheduler_executes_queued_announcement_before_normal_playlist(self):
        store = Mock()
        store.get.side_effect = [DEFAULT_CONFIG, RuntimeError("stop test loop")]
        controller = DisplayController(Mock(), store)
        draft = {"id": "announcement_preview"}
        controller._announcement_preview = draft
        controller._execute = Mock()
        controller._execute_media = Mock()
        controller._execute_announcement_preview = Mock(side_effect=lambda item: controller._shutdown.set() or True)

        controller._run_loop_unchecked()

        controller._execute_announcement_preview.assert_called_once_with(draft)
        controller._execute.assert_not_called()
        controller._execute_media.assert_not_called()

    def test_media_file_can_be_queued_and_executed_as_a_one_off_preview(self):
        matrix = Mock()
        controller = DisplayController(matrix, Mock())
        controller.start = Mock()

        controller.preview_media("custom.gif")

        self.assertEqual(controller._take_media_preview(), "custom.gif")
        controller.start.assert_called_once_with()

        with patch("display_controller.play_media", return_value=True) as player:
            self.assertTrue(controller._execute_media("custom.gif", duration=15))

        player.assert_called_once()
        self.assertIs(player.call_args.args[0], matrix)
        self.assertEqual(player.call_args.args[1:3], ("custom.gif", 15))
        self.assertIsNotNone(player.call_args.kwargs["stop_event"])

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
             patch.object(display_controller.LOGGER, "exception") as log_exception:
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
        pairing_thread = threading.Thread(
            target=lambda: controller.show_pairing_pin("123456", ttl_seconds=60),
        )
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
        pairing_thread = threading.Thread(
            target=lambda: controller.show_pairing_pin("123456", ttl_seconds=60),
        )
        pairing_thread.start()
        self.assertTrue(provider_started.wait(0.2))

        self.assertFalse(controller.stop(timeout=0.05))
        controller._run_loop = Mock()
        matrix.reset_mock()
        self.assertTrue(controller.start())

        release_provider.set()
        pairing_thread.join(1)

        self.assertFalse(pairing_thread.is_alive())
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
        pairing_thread = threading.Thread(
            target=lambda: controller.show_pairing_pin("123456", ttl_seconds=60),
        )
        pairing_thread.start()
        self.assertTrue(draw_admission_started.wait(0.2))

        self.assertFalse(controller.stop(timeout=0.05))
        controller._run_loop = Mock()
        matrix.reset_mock()
        self.assertTrue(controller.start())

        release_draw_admission.set()
        pairing_thread.join(1)

        self.assertFalse(pairing_thread.is_alive())
        self.assertFalse(controller.pairing_active)
        matrix.clear.assert_not_called()
        matrix.swap.assert_not_called()

    def test_reload_breaks_playlist_and_loads_new_config(self):
        old = {"display": {"active_start": "00:00", "active_end": "00:00", "day_brightness": 100, "night_brightness": 10}, "playlist": [{"module": "splash", "duration": 2, "enabled": True}, {"module": "weather", "duration": 2, "enabled": True}]}
        new = {"display": old["display"], "playlist": [{"module": "clock_lava", "duration": 2, "enabled": True}]}
        store = Mock()
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

        saved_config = {
            **DEFAULT_CONFIG,
            "playlist": [{"module": "splash", "duration": 1, "enabled": True}],
        }
        now = [300.0]
        store = Mock(get=Mock(return_value=saved_config))
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
        self.assertEqual(saved_renders, [("splash", saved_config, 1)])
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
        store = Mock(get=Mock(return_value=sleep_config))
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

        saved_config = {
            **DEFAULT_CONFIG,
            "playlist": [{"module": "splash", "duration": 1, "enabled": True}],
        }
        unsaved_candidate = {"module": "weather", "candidate": {"draft": "unsaved"}}
        clock = FakeClock()
        display_lock = OwnedLock()
        matrix = GuardedMatrix(display_lock)
        store = Mock(get=Mock(return_value=saved_config))
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

        config = {
            **DEFAULT_CONFIG,
            "playlist": [{"module": "splash", "duration": 1, "enabled": True}],
        }
        display_lock = OwnedLock()
        matrix = GuardedMatrix(display_lock)
        controller = DisplayController(matrix, Mock(get=Mock(return_value=config)))
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

            def physical_swap(self, frame):
                self.frame = frame
                self.revision += 1
                if self.observer is not None:
                    self.observer(frame, self.revision)

        with tempfile.TemporaryDirectory(prefix="acceptance-controller-") as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            matrix = SwapMatrix()
            controller = DisplayController(matrix, Mock())
            owner = DevicePreviewOwner("device-a", "a" * 32)
            foreign = DevicePreviewOwner("device-b", "b" * 32)
            admission = controller.device_preview_manager.submit(
                DevicePreviewRequest(owner, 4, {"module": "clock_lava"}, 60)
            )
            self.assertIs(admission.status, PreviewAdmissionStatus.ACCEPTED)
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
            matrix.physical_swap(draft)
            self.assertEqual(publisher.state, publisher.DRAFT_PUBLISHED)
            self.assertTrue(controller.mark_acceptance_foreign_rejection(foreign, generation=4))
            self.assertTrue(controller.mark_acceptance_result(
                owner, generation=4, result="heartbeat_accepted",
            ))
            self.assertFalse(controller.mark_acceptance_result(
                foreign, generation=4, result="timeout_returned",
            ))
            self.assertFalse(controller.mark_acceptance_result(
                owner, generation=3, result="timeout_returned",
            ))
            self.assertTrue(controller.cancel_device_draft(owner, generation=4))

            matrix.physical_swap(saved)
            self.assertEqual(publisher.state, publisher.DRAFT_PUBLISHED)
            self.assertTrue(controller.mark_acceptance_result(
                owner, generation=4, result="timeout_returned",
            ))
            matrix.physical_swap(saved)

            expected = {
                "state": "SAVED_ROLLBACK_PUBLISHED", "nonce": "4" * 64,
                "acceptance_operation_id": "2" * 64,
                "owner_generation": 4, "saved_revision": 10,
                "draft_revision": 11, "current_revision": 13,
            }
            self.assertEqual(controller.acceptance_transaction_status(owner, generation=4), expected)
            self.assertEqual(set(expected), {
                "state", "nonce", "acceptance_operation_id", "owner_generation",
                "saved_revision", "draft_revision", "current_revision",
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


if __name__ == "__main__":
    unittest.main()
