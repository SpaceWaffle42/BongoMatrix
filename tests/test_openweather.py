import json
import threading
import unittest
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, call, patch

from PIL import Image

from modules.openweather_common import OpenWeatherClient, OpenWeatherError
from modules.openweather_common import OpenWeatherForecastPeriod, OpenWeatherSnapshot
from modules.openweather_forecast import prepare as prepare_forecast, run as run_forecast
from modules.openweather_overview import prepare as prepare_overview, run as run_overview


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeBinaryResponse(FakeResponse):
    def __init__(self, payload):
        self.payload = payload


class OpenWeatherClientTests(unittest.TestCase):
    @patch("modules.openweather_common.urlopen")
    def test_current_weather_is_normalized_with_units_and_selected_coordinates(self, urlopen):
        urlopen.return_value = FakeResponse({
            "name": "Brighton",
            "sys": {"country": "GB"},
            "weather": [{"description": "broken clouds", "icon": "04d"}],
            "main": {
                "temp": 17.4,
                "feels_like": 16.2,
                "temp_min": 12.1,
                "temp_max": 20.8,
                "humidity": 68,
            },
        })

        snapshot = OpenWeatherClient().fetch_current(
            api_key="test_key_1234567890",
            location="Brighton, England, GB",
            latitude=50.82,
            longitude=-0.14,
            units="celsius",
        )

        self.assertEqual(snapshot.location, "Brighton, England, GB")
        self.assertEqual(snapshot.status, "Broken clouds")
        self.assertEqual(snapshot.temperature, 17)
        self.assertEqual(snapshot.high, 21)
        self.assertEqual(snapshot.low, 12)
        self.assertEqual(snapshot.icon_code, "04d")
        query = parse_qs(urlparse(urlopen.call_args.args[0].full_url).query)
        self.assertEqual(query["lat"], ["50.82"])
        self.assertEqual(query["lon"], ["-0.14"])
        self.assertEqual(query["units"], ["metric"])
        self.assertEqual(query["appid"], ["test_key_1234567890"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)

    @patch("modules.openweather_common.urlopen")
    def test_three_hour_forecast_is_normalized_in_the_location_timezone(self, urlopen):
        urlopen.return_value = FakeResponse({
            "city": {"timezone": 3600},
            "list": [
                {
                    "dt": 1785945600,
                    "weather": [{"description": "light rain", "icon": "10d"}],
                    "main": {"temp": 18.4, "feels_like": 17.2, "temp_min": 16.1, "temp_max": 19.8},
                },
                {
                    "dt": 1785956400,
                    "weather": [{"description": "clear sky", "icon": "01n"}],
                    "main": {"temp": 16.2, "feels_like": 15.4, "temp_min": 15.3, "temp_max": 17.1},
                },
            ],
        })

        periods = OpenWeatherClient().fetch_forecast(
            api_key="test_key_1234567890",
            location="Brighton, England, GB",
            latitude=50.82,
            longitude=-0.14,
            units="fahrenheit",
            max_items=2,
        )

        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0].status, "Light rain")
        self.assertEqual(periods[0].temperature, 18)
        self.assertEqual(periods[0].high, 20)
        self.assertEqual(periods[0].low, 16)
        self.assertRegex(periods[0].label, r"^[A-Z][a-z]{2} \d{2}:\d{2}$")
        self.assertEqual(periods[1].icon_code, "01n")
        query = parse_qs(urlparse(urlopen.call_args.args[0].full_url).query)
        self.assertEqual(query["units"], ["imperial"])
        self.assertEqual(query["cnt"], ["2"])

    @patch("modules.openweather_common.urlopen")
    def test_provider_errors_never_include_the_api_key(self, urlopen):
        key = "test_key_1234567890"
        urlopen.side_effect = HTTPError(
            f"https://api.openweathermap.org/data/2.5/weather?appid={key}",
            401,
            "Unauthorized",
            {},
            BytesIO(b"{}"),
        )

        with self.assertRaisesRegex(OpenWeatherError, "key rejected") as raised:
            OpenWeatherClient().fetch_current(api_key=key, location="Brighton, GB")

        self.assertNotIn(key, str(raised.exception))

    @patch("modules.openweather_common.urlopen")
    def test_timeout_and_malformed_payload_return_safe_provider_errors(self, urlopen):
        client = OpenWeatherClient()
        urlopen.side_effect = TimeoutError()
        with self.assertRaises(OpenWeatherError) as timeout_error:
            client.fetch_current(
                api_key="test_key_1234567890", location="Brighton", units="celsius"
            )
        self.assertIn("timed out", str(timeout_error.exception).lower())

        urlopen.side_effect = None
        urlopen.return_value = FakeResponse({"weather": []})
        with self.assertRaises(OpenWeatherError) as malformed_error:
            client.fetch_current(
                api_key="test_key_1234567890", location="Brighton", units="celsius"
            )
        self.assertIn("incomplete data", str(malformed_error.exception).lower())

    @patch("modules.openweather_common.urlopen")
    def test_icon_is_downloaded_from_fixed_provider_host_and_bounded_to_matrix_size(self, urlopen):
        encoded = BytesIO()
        source = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        source.putpixel((50, 50), (0, 0, 255, 255))
        source.save(encoded, format="PNG")
        urlopen.return_value = FakeBinaryResponse(encoded.getvalue())

        icon = OpenWeatherClient().fetch_icon("04d")

        self.assertEqual(icon.size, (20, 20))
        self.assertEqual(icon.mode, "RGBA")
        self.assertEqual(icon.getpixel((0, 0))[3], 0)
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "https://openweathermap.org/img/wn/04d@2x.png",
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)


class OpenWeatherOverviewTests(unittest.TestCase):
    def test_prepare_uses_the_shared_top_level_weather_location(self):
        client = Mock()
        client.fetch_current.return_value = OpenWeatherSnapshot(
            location="Worthing, England, GB", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70, icon_code="01d",
        )
        client.fetch_icon.return_value = Image.new("RGBA", (2, 2), "white")
        config = {
            "weather": {
                "location": "Worthing, England, GB",
                "latitude": 50.8179,
                "longitude": -0.3729,
            },
            "openweather_overview": {
                "location": "Stale per-app location",
                "latitude": 1,
                "longitude": 2,
                "units": "celsius",
                "background_colour": "#000000",
            },
        }

        prepared = prepare_overview(config, client=client, api_key="x")

        self.assertIsNone(prepared.error)
        client.fetch_current.assert_called_once_with(
            api_key="x",
            location="Worthing, England, GB",
            latitude=50.8179,
            longitude=-0.3729,
            units="celsius",
        )

    def test_already_cancelled_run_performs_no_provider_requests(self):
        stop_event = threading.Event()
        stop_event.set()
        client = Mock()

        result = run_overview(
            Mock(),
            {"openweather_overview": {}},
            stop_event=stop_event,
            client=client,
            api_key="test_key_1234567890",
        )

        self.assertFalse(result)
        client.assert_not_called()
        self.assertEqual(client.method_calls, [])

    def test_cancellation_after_current_weather_skips_icon_and_rendering(self):
        stop_event = threading.Event()
        snapshot = OpenWeatherSnapshot(
            location="Brighton, England, GB",
            status="Cloudy",
            temperature=17,
            feels_like=16,
            high=21,
            low=12,
            humidity=68,
            icon_code="04d",
        )
        client = Mock()

        def cancel_after_weather(**_kwargs):
            stop_event.set()
            return snapshot

        client.fetch_current.side_effect = cancel_after_weather
        matrix = Mock()

        result = run_overview(
            matrix,
            {"openweather_overview": {}},
            stop_event=stop_event,
            client=client,
            api_key="test_key_1234567890",
        )

        self.assertFalse(result)
        client.fetch_icon.assert_not_called()
        matrix.matrix.CreateFrameCanvas.assert_not_called()

    @patch("modules.openweather_overview._wait", return_value=False)
    def test_overview_uses_ticker_loops_instead_of_playlist_duration(self, wait):
        canvas = SimpleNamespace(width=2)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 1
        matrix.widget_scroll.side_effect = (
            lambda text, font, x, y, colour, canvas=None, step=1, wrap=True:
            (x - step, 1)
        )
        matrix.swap.return_value = canvas
        client = Mock()
        client.fetch_current.return_value = OpenWeatherSnapshot(
            location="London", status="Clear", temperature=16,
            feels_like=15, high=18, low=12, humidity=70,
            icon_code="01d",
        )
        client.fetch_icon.return_value = Image.new("RGBA", (20, 20), "blue")
        config = {
            "weather": {"location": "London", "latitude": 51.5, "longitude": -0.1},
            "openweather_overview": {
                "scroll_direction": "left", "scroll_speed": "fast",
                "scroll_loops": 2,
            },
        }

        self.assertTrue(run_overview(
            matrix, config, duration=0, client=client, api_key="x"
        ))

        self.assertEqual(matrix.swap.call_count, 8)
        self.assertEqual(wait.call_count, 8)
        wait.assert_called_with(None, 0.025)
        self.assertTrue(all(
            invocation.kwargs["wrap"] is False
            for invocation in matrix.widget_scroll.call_args_list
        ))

    def test_overview_recreates_icon_temperature_high_low_and_ticker(self):
        canvas = SimpleNamespace(width=64)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 180
        matrix.widget_scroll.return_value = (-1, 180)
        matrix.swap.return_value = canvas
        client = Mock()
        client.fetch_current.return_value = OpenWeatherSnapshot(
            location="Brighton, England, GB",
            status="Broken clouds",
            temperature=17,
            feels_like=16,
            high=21,
            low=12,
            humidity=68,
            icon_code="04d",
        )
        icon = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        icon.putpixel((10, 10), (0, 0, 255, 255))
        client.fetch_icon.return_value = icon
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True
        settings = {
            "location": "Brighton, England, GB",
            "latitude": 50.82,
            "longitude": -0.14,
            "units": "celsius",
            "icon_x": 0,
            "icon_y": 0,
            "now_x": 2,
            "now_y": 22,
            "temperature_x": 26,
            "temperature_y": 15,
            "high_x": 50,
            "high_y": 11,
            "low_x": 50,
            "low_y": 20,
            "ticker_y": 28,
            "scroll_direction": "left",
            "scroll_speed": "fast",
            "background_colour": "#010203",
            "text_colour": "#dcd5d5",
            "high_colour": "#ff71ce",
            "low_colour": "#01cdfe",
            "ticker_colour": "#dcd5d5",
        }

        result = run_overview(
            matrix,
            {"openweather_overview": settings},
            duration=1,
            stop_event=stop_event,
            client=client,
            api_key="test_key_1234567890",
        )

        self.assertTrue(result)
        client.fetch_current.assert_called_once_with(
            api_key="test_key_1234567890",
            location="Brighton, England, GB",
            latitude=50.82,
            longitude=-0.14,
            units="celsius",
        )
        client.fetch_icon.assert_called_once_with("04d")
        drawn_icon = matrix.draw_image.call_args.args[0]
        self.assertEqual(drawn_icon.mode, "RGB")
        self.assertEqual(drawn_icon.getpixel((0, 0)), (1, 2, 3))
        self.assertEqual(drawn_icon.getpixel((10, 10)), (0, 0, 255))
        self.assertEqual(matrix.draw_image.call_args.args[1:3], (0, 0))
        self.assertIs(matrix.draw_image.call_args.kwargs["canvas"], canvas)
        self.assertIn(call("5x7", 2, 22, "#dcd5d5", "#010203", "NOW", canvas=canvas), matrix.draw_text_with_border.call_args_list)
        self.assertIn(call("5x7", 26, 15, "#dcd5d5", "#010203", "17C", canvas=canvas), matrix.draw_text_with_border.call_args_list)
        self.assertIn(call("5x7", 50, 11, "#ff71ce", "#010203", "21", canvas=canvas), matrix.draw_text_with_border.call_args_list)
        self.assertIn(call("5x7", 50, 20, "#01cdfe", "#010203", "12", canvas=canvas), matrix.draw_text_with_border.call_args_list)
        matrix.widget_scroll.assert_called_once_with(
            "Brighton, England, GB | Broken clouds | Feels like 16C",
            "4x6", 64, 28, "#dcd5d5", canvas=canvas, step=1, wrap=False,
        )
        stop_event.wait.assert_called_once_with(0.025)


class OpenWeatherForecastTests(unittest.TestCase):
    def test_prepare_uses_the_shared_top_level_weather_location(self):
        period = OpenWeatherForecastPeriod(
            label="Tue 15:00", status="Rain", temperature=18,
            feels_like=17, high=20, low=16, icon_code="10d",
        )
        client = Mock()
        client.fetch_forecast.return_value = [period]
        client.fetch_icon.return_value = Image.new("RGBA", (2, 2), "white")
        config = {
            "weather": {
                "location": "Worthing, England, GB",
                "latitude": 50.8179,
                "longitude": -0.3729,
            },
            "openweather_forecast": {
                "location": "Stale per-app location",
                "latitude": 1,
                "longitude": 2,
                "units": "fahrenheit",
                "forecast_items": 4,
                "background_colour": "#000000",
            },
        }

        prepared = prepare_forecast(config, client=client, api_key="x")

        self.assertIsNone(prepared.error)
        client.fetch_forecast.assert_called_once_with(
            api_key="x",
            location="Worthing, England, GB",
            latitude=50.8179,
            longitude=-0.3729,
            units="fahrenheit",
            max_items=4,
        )

    def test_already_cancelled_run_performs_no_provider_requests(self):
        stop_event = threading.Event()
        stop_event.set()
        client = Mock()

        result = run_forecast(
            Mock(),
            {"openweather_forecast": {}},
            stop_event=stop_event,
            client=client,
            api_key="test_key_1234567890",
        )

        self.assertFalse(result)
        client.assert_not_called()
        self.assertEqual(client.method_calls, [])

    def test_cancellation_during_icon_download_stops_further_requests_and_rendering(self):
        stop_event = threading.Event()
        periods = [
            OpenWeatherForecastPeriod(
                label=f"Tue {hour}:00",
                status="Rain",
                temperature=18,
                feels_like=17,
                high=20,
                low=16,
                icon_code=icon_code,
            )
            for hour, icon_code in ((15, "10d"), (18, "04d"))
        ]
        client = Mock()
        client.fetch_forecast.return_value = periods

        def cancel_after_first_icon(_icon_code):
            stop_event.set()
            return Image.new("RGBA", (20, 20), (0, 0, 0, 0))

        client.fetch_icon.side_effect = cancel_after_first_icon
        matrix = Mock()

        result = run_forecast(
            matrix,
            {"openweather_forecast": {}},
            stop_event=stop_event,
            client=client,
            api_key="test_key_1234567890",
        )

        self.assertFalse(result)
        client.fetch_icon.assert_called_once_with("10d")
        matrix.matrix.CreateFrameCanvas.assert_not_called()

    @patch("modules.openweather_forecast._wait", return_value=False)
    def test_forecast_uses_ticker_loops_per_card_instead_of_seconds(self, wait):
        canvas = SimpleNamespace(width=2)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 1
        matrix.widget_scroll.side_effect = (
            lambda text, font, x, y, colour, canvas=None, step=1, wrap=True:
            (x - step, 1)
        )
        matrix.swap.return_value = canvas
        periods = [
            OpenWeatherForecastPeriod(
                label="Tue 15:00", status="Rain", temperature=18,
                feels_like=17, high=20, low=14, icon_code="10d",
            ),
            OpenWeatherForecastPeriod(
                label="Tue 18:00", status="Cloudy", temperature=16,
                feels_like=15, high=18, low=13, icon_code="04d",
            ),
        ]
        client = Mock()
        client.fetch_forecast.return_value = periods
        client.fetch_icon.return_value = Image.new("RGB", (20, 20), "blue")
        config = {
            "weather": {"location": "London", "latitude": 51.5, "longitude": -0.1},
            "openweather_forecast": {
                "forecast_items": 2, "card_seconds": 999,
                "scroll_direction": "right", "scroll_speed": "normal",
                "scroll_loops": 2,
            },
        }

        self.assertTrue(run_forecast(
            matrix, config, duration=0, client=client, api_key="x"
        ))

        self.assertEqual(matrix.swap.call_count, 16)
        self.assertEqual(wait.call_count, 16)
        wait.assert_called_with(None, 0.05)
        rendered_labels = [
            invocation.args[5]
            for invocation in matrix.draw_text_with_border.call_args_list
            if len(invocation.args) > 5 and invocation.args[5].startswith("Tue")
        ]
        self.assertEqual(
            rendered_labels,
            ["Tue 15:00"] * 8 + ["Tue 18:00"] * 8,
        )
        self.assertTrue(all(
            invocation.kwargs["wrap"] is False
            for invocation in matrix.widget_scroll.call_args_list
        ))
        self.assertEqual(
            matrix.widget_scroll.call_args_list[0].kwargs["step"], -1
        )

    def test_forecast_renders_upcoming_period_as_legacy_weather_card(self):
        canvas = SimpleNamespace(width=64)
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = canvas
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.font.side_effect = lambda name: name
        matrix.draw_text.return_value = 180
        matrix.widget_scroll.return_value = (-1, 180)
        matrix.swap.return_value = canvas
        period = OpenWeatherForecastPeriod(
            label="Tue 15:00",
            status="Light rain",
            temperature=18,
            feels_like=17,
            high=20,
            low=16,
            icon_code="10d",
        )
        client = Mock()
        client.fetch_forecast.return_value = [period]
        icon = Image.new("RGB", (20, 20), "blue")

        client.fetch_icon.return_value = icon
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True
        settings = {
            "location": "Brighton, England, GB",
            "latitude": 50.82,
            "longitude": -0.14,
            "units": "celsius",
            "forecast_items": 3,
            "card_seconds": 5,
            "icon_x": 0,
            "icon_y": 0,
            "time_x": 2,
            "time_y": 22,
            "temperature_x": 26,
            "temperature_y": 15,
            "high_x": 50,
            "high_y": 11,
            "low_x": 50,
            "low_y": 20,
            "ticker_y": 28,
            "scroll_direction": "left",
            "scroll_speed": "normal",
            "background_colour": "#010203",
            "text_colour": "#dcd5d5",
            "high_colour": "#ff71ce",
            "low_colour": "#01cdfe",
            "ticker_colour": "#dcd5d5",
        }

        result = run_forecast(
            matrix,
            {"openweather_forecast": settings},
            duration=1,
            stop_event=stop_event,
            client=client,
            api_key="test_key_1234567890",
        )

        self.assertTrue(result)
        client.fetch_forecast.assert_called_once_with(
            api_key="test_key_1234567890",
            location="Brighton, England, GB",
            latitude=50.82,
            longitude=-0.14,
            units="celsius",
            max_items=3,
        )
        client.fetch_icon.assert_called_once_with("10d")
        matrix.draw_image.assert_called_once_with(icon, 0, 0, canvas=canvas)
        self.assertIn(call("4x6", 2, 22, "#dcd5d5", "#010203", "Tue 15:00", canvas=canvas), matrix.draw_text_with_border.call_args_list)
        self.assertIn(call("5x7", 26, 15, "#dcd5d5", "#010203", "18C", canvas=canvas), matrix.draw_text_with_border.call_args_list)
        matrix.widget_scroll.assert_called_once_with(
            "Brighton, England, GB | Light rain | Feels like 17C",
            "4x6", 64, 28, "#dcd5d5", canvas=canvas, step=1, wrap=False,
        )
        stop_event.wait.assert_called_once_with(0.05)


class OpenWeatherMissingSecretTests(unittest.TestCase):
    def test_both_modules_fail_closed_with_a_clear_matrix_message(self):
        for runner, section in (
            (run_overview, "openweather_overview"),
            (run_forecast, "openweather_forecast"),
        ):
            with self.subTest(section=section):
                matrix = Mock()
                matrix.width = 64
                matrix.height = 32
                self.assertFalse(
                    runner(
                        matrix, {section: {}}, 1,
                        stop_event=threading.Event(), api_key=None, client=Mock(),
                    )
                )
                rendered = " ".join(
                    str(call.args[5])
                    for call in matrix.draw_text_with_border.call_args_list
                )
                self.assertIn("KEY NOT SET", rendered)


if __name__ == "__main__":
    unittest.main()
