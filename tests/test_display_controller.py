import threading
import time
import unittest
from unittest.mock import Mock, patch

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

    def test_unknown_preview_is_rejected(self):
        store = Mock()
        store.get.return_value = {"custom_modules": []}
        controller = DisplayController(Mock(), store)
        with self.assertRaises(KeyError):
            controller.preview("unknown")

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
