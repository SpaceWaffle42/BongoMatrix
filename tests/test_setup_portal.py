import unittest
from unittest.mock import Mock

from setup_portal import create_setup_app


class SetupPortalTests(unittest.TestCase):
    def setUp(self):
        self.network = Mock()
        self.pairing = Mock()
        self.coordinator = Mock()
        self.coordinator.is_busy.return_value = False
        self.app = create_setup_app(
            self.network,
            self.pairing,
            self.coordinator,
            setup_ssid="Matrix-Setup-ABCD",
            networks=[{"ssid": "Home", "signal": 80, "security": "WPA2"}],
            secret_key="test",
        )
        self.client = self.app.test_client()

    def test_setup_page_lists_networks_and_hotspot_name(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Matrix-Setup-ABCD", response.data)
        self.assertIn(b"Home", response.data)

    def test_pin_required_before_credentials_are_submitted(self):
        self.pairing.verify.return_value = False
        response = self.client.post(
            "/connect",
            data={"pin": "000000", "ssid": "Home", "password": "password123"},
        )
        self.assertEqual(response.status_code, 401)
        self.coordinator.submit.assert_not_called()

    def test_invalid_credentials_do_not_consume_pin(self):
        self.network.validate_credentials.side_effect = ValueError("bad credentials")
        response = self.client.post(
            "/connect",
            data={"pin": "123456", "ssid": "Home", "password": "bad\nvalue"},
        )
        self.assertEqual(response.status_code, 400)
        self.pairing.verify.assert_not_called()

    def test_busy_handoff_does_not_consume_pin(self):
        self.coordinator.is_busy.return_value = True
        response = self.client.post(
            "/connect",
            data={"pin": "123456", "ssid": "Home", "password": "password123"},
        )
        self.assertEqual(response.status_code, 409)
        self.pairing.verify.assert_not_called()

    def test_valid_pin_starts_background_handoff(self):
        self.pairing.verify.return_value = True
        self.coordinator.submit.return_value = True
        response = self.client.post(
            "/connect",
            data={"pin": "123456", "ssid": "Home", "password": "password123"},
        )
        self.assertEqual(response.status_code, 202)
        self.coordinator.submit.assert_called_once_with("Home", "password123")
        self.assertIn(b"reconnect", response.data.lower())

    def test_captive_portal_probe_redirects_to_setup(self):
        response = self.client.get("/generate_204")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
