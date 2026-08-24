import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from modules.weather_app import OpenMeteoClient, WeatherSnapshot, run


class FakeResponse:
    def __init__(self, payload):
        import json
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class WeatherTests(unittest.TestCase):
    def test_open_meteo_serial_transports_share_one_monotonic_deadline(self):
        now = [10.0]

        class DeadlineClient(OpenMeteoClient):
            def __init__(self):
                super().__init__(timeout=8, clock=lambda: now[0])
                self.timeouts = []

            def _get_json(self, base_url, params, timeout=None):
                self.timeouts.append(timeout)
                if len(self.timeouts) == 1:
                    now[0] = 14.5
                    return {"results": [{"name": "Brighton", "country_code": "GB", "latitude": 50.82, "longitude": -0.14}]}
                return {
                    "current": {"temperature_2m": 17, "apparent_temperature": 16, "relative_humidity_2m": 68, "weather_code": 1},
                    "daily": {"temperature_2m_max": [20], "temperature_2m_min": [12]},
                }

        client = DeadlineClient()
        client.fetch("Brighton, GB", deadline=15.0)
        self.assertEqual(client.timeouts, [5.0, 0.5])

        now[0] = 10.0
        client = DeadlineClient()
        original = client._get_json
        def consume_deadline(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 15.0
            return result
        client._get_json = consume_deadline
        with self.assertRaises(TimeoutError):
            client.fetch("Brighton, GB", deadline=15.0)
        self.assertEqual(len(client.timeouts), 1)

    @patch("modules.weather_app.urlopen")
    def test_location_is_geocoded_and_forecast_normalized(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"results": [{"name": "Brighton", "country_code": "GB", "latitude": 50.82, "longitude": -0.14}]}),
            FakeResponse({
                "current": {"temperature_2m": 17.4, "apparent_temperature": 16.2, "relative_humidity_2m": 68, "weather_code": 2},
                "daily": {"temperature_2m_max": [20.8], "temperature_2m_min": [12.1]},
            }),
        ]
        snapshot = OpenMeteoClient().fetch("Brighton, GB")
        self.assertEqual(snapshot.location, "Brighton, GB")
        self.assertEqual(snapshot.temperature, 17)
        self.assertEqual(snapshot.high, 21)
        self.assertEqual(snapshot.low, 12)
        self.assertEqual(snapshot.status, "Partly cloudy")

    def test_weather_code_has_safe_fallback(self):
        self.assertEqual(OpenMeteoClient.status_for_code(999), "Unknown")

    @patch("modules.weather_app.urlopen")
    def test_location_search_returns_labels_and_coordinates(self, urlopen):
        urlopen.return_value = FakeResponse({"results": [{
            "name": "Brighton", "admin1": "England", "country_code": "GB",
            "latitude": 50.82, "longitude": -0.14,
        }]})
        results = OpenMeteoClient().search_locations("Brighton", count=6)
        self.assertEqual(results, [{
            "label": "Brighton, England, GB",
            "latitude": 50.82,
            "longitude": -0.14,
        }])

    @patch("modules.weather_app.urlopen")
    def test_selected_coordinates_skip_geocoding(self, urlopen):
        urlopen.return_value = FakeResponse({
            "current": {"temperature_2m": 17, "apparent_temperature": 16, "relative_humidity_2m": 68, "weather_code": 1},
            "daily": {"temperature_2m_max": [20], "temperature_2m_min": [12]},
        })
        snapshot = OpenMeteoClient().fetch(
            "Brighton, England, GB", latitude=50.82, longitude=-0.14
        )
        self.assertEqual(snapshot.location, "Brighton, England, GB")
        self.assertEqual(urlopen.call_count, 1)

    @patch("modules.weather_app.urlopen")
    def test_fahrenheit_is_requested_from_forecast_service(self, urlopen):
        urlopen.return_value = FakeResponse({
            "current": {"temperature_2m": 63, "apparent_temperature": 61, "relative_humidity_2m": 68, "weather_code": 1},
            "daily": {"temperature_2m_max": [68], "temperature_2m_min": [54]},
        })

        snapshot = OpenMeteoClient().fetch(
            "Brighton, England, GB",
            latitude=50.82,
            longitude=-0.14,
            units="fahrenheit",
        )

        self.assertEqual(snapshot.temperature, 63)
        request = urlopen.call_args.args[0]
        self.assertIn("temperature_unit=fahrenheit", request.full_url)

    def test_renderer_does_not_fetch_when_already_stopped(self):
        matrix = Mock()
        client = Mock()
        stop_event = Mock()
        stop_event.is_set.return_value = True

        self.assertFalse(run(
            matrix, {"weather": {}}, duration=0,
            stop_event=stop_event, client=client,
        ))

        client.fetch.assert_not_called()
        matrix.matrix.CreateFrameCanvas.assert_not_called()

    def test_renderer_does_not_publish_error_after_provider_cancels(self):
        matrix = Mock()
        client = Mock()
        stop_event = threading.Event()

        def cancel_then_fail(*_args, **_kwargs):
            stop_event.set()
            raise TimeoutError("cancelled provider")

        client.fetch.side_effect = cancel_then_fail

        self.assertFalse(run(
            matrix, {"weather": {}}, duration=5,
            stop_event=stop_event, client=client,
        ))

        matrix.matrix.CreateFrameCanvas.assert_not_called()
        matrix.swap.assert_not_called()

    def test_cancellation_during_error_draw_prevents_error_frame_swap(self):
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        stop_event = threading.Event()
        matrix.draw_text_with_border.side_effect = (
            lambda *_args, **_kwargs: stop_event.set()
        )
        client = Mock()
        client.fetch.side_effect = TimeoutError("provider failed")

        self.assertFalse(run(
            matrix, {"weather": {}}, duration=5,
            stop_event=stop_event, client=client,
        ))

        matrix.swap.assert_not_called()

    @patch("modules.weather_app._wait", return_value=False)
    def test_cancellation_during_final_ticker_draw_prevents_first_swap(self, wait):
        canvas = SimpleNamespace(width=64)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 80
        matrix.swap.return_value = canvas
        stop_event = threading.Event()
        matrix.widget_scroll.side_effect = lambda *_args, **_kwargs: stop_event.set()
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="London", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
        )

        self.assertFalse(run(
            matrix, {"weather": {"scroll_loops": 1}}, duration=0,
            stop_event=stop_event, client=client,
        ))

        matrix.swap.assert_not_called()
        wait.assert_not_called()

    @patch("modules.weather_app._wait", return_value=False)
    def test_publication_guard_rejection_does_not_count_first_weather_frame(self, wait):
        from matrix_framework import Matrix

        matrix = Matrix.__new__(Matrix)
        canvas = SimpleNamespace(width=64)
        matrix.canvas = canvas
        matrix.matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix._frame_capture = Mock()
        matrix._physical_swap_observer = None
        matrix.clear = Mock()
        matrix.fill = Mock()
        matrix.colour = Mock(side_effect=lambda name: name)
        matrix.colour_rgb = Mock(side_effect=lambda value: value)
        matrix.font = Mock(side_effect=lambda name: name)
        matrix.draw_text = Mock(return_value=80)
        matrix.draw_text_with_border = Mock()
        matrix.draw_line = Mock()
        matrix.widget_scroll = Mock()
        stop_event = threading.Event()

        def reject_at_hardware_admission():
            stop_event.set()
            return False

        matrix._publication_guard = reject_at_hardware_admission
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="London", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
        )

        self.assertFalse(run(
            matrix, {"weather": {"scroll_loops": 1}}, duration=0,
            stop_event=stop_event, client=client,
        ))

        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()
        wait.assert_not_called()

    @patch("modules.weather_app._wait", return_value=False)
    def test_guard_rejection_after_admitted_weather_frame_preserves_true(self, wait):
        from matrix_framework import Matrix

        matrix = Matrix.__new__(Matrix)
        canvas = SimpleNamespace(width=64)
        matrix.canvas = canvas
        matrix.matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.matrix.SwapOnVSync.return_value = canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        matrix.clear = Mock()
        matrix.fill = Mock()
        matrix.colour = Mock(side_effect=lambda name: name)
        matrix.colour_rgb = Mock(side_effect=lambda value: value)
        matrix.font = Mock(side_effect=lambda name: name)
        matrix.draw_text = Mock(return_value=80)
        matrix.draw_text_with_border = Mock()
        matrix.draw_line = Mock()
        matrix.widget_scroll = Mock()
        stop_event = threading.Event()
        admissions = iter((True, False))

        def admit_once_then_reject():
            admitted = next(admissions)
            if not admitted:
                stop_event.set()
            return admitted

        matrix._publication_guard = admit_once_then_reject
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="London", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
        )

        self.assertTrue(run(
            matrix, {"weather": {"scroll_loops": 1}}, duration=0,
            stop_event=stop_event, client=client,
        ))

        matrix.matrix.SwapOnVSync.assert_called_once_with(canvas)
        matrix._frame_capture.publish.assert_called_once_with(canvas)
        self.assertEqual(wait.call_count, 1)

    @patch("modules.weather_app._wait", return_value=False)
    def test_renderer_uses_ticker_loops_instead_of_playlist_duration(self, wait):
        canvas = SimpleNamespace(width=2)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 1
        matrix.widget_scroll.side_effect = (
            lambda text, font, x, y, colour, canvas=None, step=1, wrap=True:
            (x - step, 1)
        )
        matrix.swap.return_value = canvas
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="London", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
        )
        config = {"weather": {
            "scroll_direction": "left", "scroll_speed": 0.075,
            "scroll_loops": 2,
        }}

        self.assertTrue(run(matrix, config, duration=0, client=client))

        self.assertEqual(matrix.swap.call_count, 8)
        self.assertEqual(wait.call_count, 8)
        wait.assert_called_with(None, 0.075)
        self.assertTrue(all(
            call.kwargs["wrap"] is False
            for call in matrix.widget_scroll.call_args_list
        ))

    @patch("modules.weather_app._wait", return_value=False)
    def test_renderer_counts_right_scrolling_ticker_loops(self, wait):
        canvas = SimpleNamespace(width=2)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 1
        matrix.widget_scroll.side_effect = (
            lambda text, font, x, y, colour, canvas=None, step=1, wrap=True:
            (x - step, 1)
        )
        matrix.swap.return_value = canvas
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="London", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
        )

        self.assertTrue(run(matrix, {"weather": {
            "scroll_direction": "right", "scroll_speed": "normal",
            "scroll_loops": 1,
        }}, duration=0, client=client))

        self.assertEqual(matrix.swap.call_count, 4)
        self.assertEqual(wait.call_count, 4)
        first_scroll = matrix.widget_scroll.call_args_list[0]
        self.assertEqual(first_scroll.args[2], -1)
        self.assertEqual(first_scroll.kwargs["step"], -1)
        self.assertFalse(first_scroll.kwargs["wrap"])

    def test_renderer_fallback_location_is_london(self):
        canvas = SimpleNamespace(width=64)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 80
        matrix.widget_scroll.return_value = (-1, 80)
        matrix.swap.return_value = canvas
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="London, England, GB", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
        )
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True

        self.assertTrue(
            run(matrix, {"weather": {}}, duration=1, stop_event=stop_event, client=client)
        )

        client.fetch.assert_called_once_with(
            "London, England, GB", latitude=None, longitude=None, units="celsius"
        )

    def test_renderer_applies_units_layout_direction_and_speed(self):
        canvas = SimpleNamespace(width=64)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 120
        matrix.widget_scroll.return_value = (-17, 120)
        matrix.swap.return_value = canvas
        client = Mock()
        client.fetch.return_value = WeatherSnapshot(
            location="Brighton, GB", status="Clear sky", temperature=63,
            feels_like=61, high=68, low=54, humidity=70,
        )
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True
        config = {"weather": {
            "location": "Brighton, GB", "latitude": 50.82, "longitude": -0.14,
            "units": "fahrenheit", "temperature_y": 2, "ticker_y": 27,
            "scroll_direction": "right", "scroll_speed": "slow",
            "now_x": -4, "now_y": 1, "temperature_x": 5,
            "high_x": 30, "high_y": 3, "low_x": 31, "low_y": 9,
            "line_y": 23, "background_colour": "#010203",
            "text_colour": "#aabbcc", "high_colour": "#ff0000",
            "low_colour": "#00ff00", "line_colour": "#0000ff",
            "ticker_colour": "#ffff00",
        }}

        self.assertTrue(run(matrix, config, duration=1, stop_event=stop_event, client=client))

        client.fetch.assert_called_once_with(
            "Brighton, GB", latitude=50.82, longitude=-0.14, units="fahrenheit"
        )
        self.assertIn(
            call("5x7", 5, 2, "#aabbcc", "#010203", "63F", canvas=canvas),
            matrix.draw_text_with_border.call_args_list,
        )
        self.assertIn(
            call("4x6", 30, 3, "#ff0000", "#010203", "H 68", canvas=canvas),
            matrix.draw_text_with_border.call_args_list,
        )
        self.assertIn(
            call("4x6", 31, 9, "#00ff00", "#010203", "L 54", canvas=canvas),
            matrix.draw_text_with_border.call_args_list,
        )
        matrix.fill.assert_called_once_with(1, 2, 3, canvas=canvas)
        matrix.draw_line.assert_called_once_with(0, 23, 63, 23, "#0000ff", canvas=canvas)
        matrix.widget_scroll.assert_called_once_with(
            "Brighton, GB | Clear sky | Feels 61F | Humidity 70%",
            "4x6", -120, 27, "#ffff00", canvas=canvas, step=-1, wrap=False,
        )
        stop_event.wait.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()
