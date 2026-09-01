import ast
import unittest
from pathlib import Path

import display_controller
import webapp
from modules.openweather_common import OpenWeatherClient
from modules.weather_app import OpenMeteoClient
from preview_provider import PreviewProvider


class ApiTimeoutContractTests(unittest.TestCase):
    def test_outbound_api_defaults_are_thirty_seconds(self):
        expectations = {
            "Open-Meteo transport": OpenMeteoClient().timeout,
            "OpenWeather transport": OpenWeatherClient().timeout,
            "preview provider hard deadline": PreviewProvider.MAX_REQUEST_SECONDS,
            "edit preview provider deadline": webapp.EDIT_PREVIEW_PROVIDER_DEADLINE_SECONDS,
            "device preview provider deadline": display_controller.OPENWEATHER_PREPARATION_DEADLINE_SECONDS,
        }
        for seam, timeout in expectations.items():
            with self.subTest(seam=seam):
                self.assertEqual(timeout, 30.0)

    def test_provider_call_sites_do_not_replace_the_shared_default_with_short_literals(self):
        root = Path(__file__).resolve().parents[1]
        paths = (
            root / "webapp.py",
            root / "modules" / "weather_app.py",
            root / "modules" / "openweather_forecast.py",
            root / "modules" / "openweather_overview.py",
        )
        short_overrides = []
        for path in paths:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None)
                if name not in {"OpenMeteoClient", "OpenWeatherClient"}:
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "timeout":
                        continue
                    numeric_literals = [
                        child.value
                        for child in ast.walk(keyword.value)
                        if isinstance(child, ast.Constant)
                        and isinstance(child.value, (int, float))
                        and not isinstance(child.value, bool)
                    ]
                    if numeric_literals:
                        short_overrides.append(
                            (
                                str(path.relative_to(root)),
                                node.lineno,
                                numeric_literals,
                            )
                        )
        self.assertEqual(short_overrides, [])


if __name__ == "__main__":
    unittest.main()
