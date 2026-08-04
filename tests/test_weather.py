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

    @patch("modules.weather_app.time.monotonic", side_effect=[0, 0, 2])
    def test_renderer_applies_units_layout_direction_and_speed(self, monotonic):
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
        stop_event.wait.return_value = False
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
            "4x6", -120, 27, "#ffff00", canvas=canvas, step=-1, wrap=True,
        )
        stop_event.wait.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()
