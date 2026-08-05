import threading
import time
import unittest
from unittest.mock import Mock, patch

import display_controller
from config_store import DEFAULT_CONFIG
from display_controller import DisplayController


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

    def test_openweather_module_receives_secret_without_adding_it_to_config(self):
        matrix = Mock()
        store = Mock()
        secrets = Mock()
        secrets.get.return_value = "test_key_1234567890"
        controller = DisplayController(matrix, store, openweather_secrets=secrets)
        module = Mock()
        module.run.return_value = True
        config = {"custom_modules": [], "openweather_overview": {"location": "Brighton, GB"}}

        with patch("display_controller.importlib.import_module", return_value=module) as importer:
            result = controller._execute("openweather_overview", config, 9)

        self.assertTrue(result)
        importer.assert_called_once_with("modules.openweather_overview")
        module.run.assert_called_once()
        self.assertEqual(module.run.call_args.kwargs["api_key"], "test_key_1234567890")
        self.assertNotIn("api_key", str(config))

    def test_openweather_preparation_runs_outside_display_lock(self):
        matrix = Mock()
        secrets = Mock()
        secrets.get.return_value = "test_key_1234567890"
        controller = DisplayController(matrix, Mock(), openweather_secrets=secrets)
        module = Mock()
        module.run.return_value = True
        preparation_started = threading.Event()
        release_preparation = threading.Event()

        def prepare(*_args, **_kwargs):
            preparation_started.set()
            release_preparation.wait(1)
            return "prepared-weather"

        module.prepare.side_effect = prepare
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
        self.assertEqual(module.run.call_args.kwargs["prepared"], "prepared-weather")

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


if __name__ == "__main__":
    unittest.main()
