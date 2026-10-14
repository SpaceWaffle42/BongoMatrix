"""Owner 2026-10-14: "all time should default from the weather location".

Every wall-clock feature (night/active window, clocks, time layers, countdown,
announcement schedules) uses the Weather location's IANA zone unless the user
explicitly chose another zone for that feature. An unknown Weather zone ("")
keeps the device clock, the previous behaviour.
"""
import copy
import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import local_clock
from config_store import DEFAULT_CONFIG

UTC = dt.timezone.utc
# 2026-07-01 23:30 UTC = 2026-07-02 09:30 in Sydney (AEST, +10).
INSTANT = dt.datetime(2026, 7, 1, 23, 30, tzinfo=UTC)


def sydney_config():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["weather"].update(location="Sydney, AU", latitude=-33.87, longitude=151.21,
                             timezone="Australia/Sydney")
    return config


class LocalClockTests(unittest.TestCase):
    def test_weather_zone_drives_naive_local_now(self):
        now = local_clock.local_now(sydney_config(), utc_now=INSTANT)
        self.assertIsNone(now.tzinfo)
        self.assertEqual(now, dt.datetime(2026, 7, 2, 9, 30))

    def test_unknown_weather_zone_keeps_device_clock(self):
        config = sydney_config()
        config["weather"]["timezone"] = ""
        self.assertIsNone(local_clock.weather_zone(config))
        device = dt.datetime(2026, 7, 2, 0, 30)
        with patch.object(local_clock, "_device_now", return_value=device):
            self.assertEqual(local_clock.local_now(config), device)

    def test_feature_zone_explicitly_saved_wins_over_weather(self):
        self.assertEqual(local_clock.effective_zone(sydney_config(), "Europe/London"), "Europe/London")
        self.assertEqual(local_clock.effective_zone(sydney_config(), ""), "Australia/Sydney")
        self.assertEqual(local_clock.effective_zone(sydney_config(), None), "Australia/Sydney")

    def test_night_window_uses_weather_location(self):
        from display_controller import DisplayController
        config = sydney_config()
        config["display"].update(active_start="07:00", active_end="22:00")
        # 23:30 UTC is night in London but 09:30 morning in Sydney.
        with patch.object(local_clock, "_utc_now", return_value=INSTANT):
            self.assertTrue(DisplayController._is_active(config))
            self.assertAlmostEqual(
                DisplayController._seconds_until_schedule_change(config), 12.5 * 3600, delta=1,
            )

    def test_lava_clock_text_uses_weather_location(self):
        from modules import clock_lava

        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return INSTANT.astimezone(tz) if tz else INSTANT.replace(tzinfo=None)

        with patch("modules.clock_lava.datetime.datetime", Frozen):
            self.assertEqual(clock_lava.clock_texts("Australia/Sydney"), ("09:30", "02 Jul"))
            self.assertEqual(clock_lava.clock_texts(None), ("23:30", "01 Jul"))

    def test_canvas_time_layer_uses_weather_location(self):
        from modules import custom_text
        with patch.object(local_clock, "_utc_now", return_value=INSTANT):
            self.assertEqual(custom_text._time_text("24h", "Australia/Sydney"), "09:30")
            self.assertEqual(custom_text._time_text("12h", "Australia/Sydney"), "9:30 AM")

    def test_announcement_schedule_uses_weather_location(self):
        from modules import announcement_board
        self.assertEqual(
            announcement_board.local_now_provider(sydney_config(), utc_now=lambda: INSTANT)(),
            dt.datetime(2026, 7, 2, 9, 30),
        )


class CountdownTimezoneDefaultTests(unittest.TestCase):
    def test_new_countdown_follows_weather_but_saved_zone_is_kept(self):
        import event_countdown_config
        from modules import event_countdown
        self.assertEqual(event_countdown_config.DEFAULTS["timezone"], "")
        config = sydney_config()
        config["event_countdown"] = copy.deepcopy(event_countdown_config.DEFAULTS)
        config["event_countdown"]["events"] = [{
            "id": "e1", "title": "Party", "start": "2026-07-02", "end": None, "all_day": True,
            "annual": False, "enabled": True, "now_minutes": 15, "accent": None, "icon": "calendar",
        }]
        event_countdown_config.validate(config["event_countdown"])
        # 22:30 UTC: 08:30 on 2 July in Sydney, 23:30 on 1 July in London (BST).
        now = dt.datetime(2026, 7, 1, 22, 30, tzinfo=UTC)
        # Weather zone (Sydney): 2026-07-02 has already started locally.
        selected = event_countdown.select_event(
            event_countdown.effective_settings(config), now=now,
        )
        self.assertEqual(selected["seconds"], 0)
        self.assertEqual(selected["start"].tzinfo, ZoneInfo("Australia/Sydney"))
        # An explicitly saved zone still wins (London: 30 minutes to go).
        config["event_countdown"]["timezone"] = "Europe/London"
        selected = event_countdown.select_event(
            event_countdown.effective_settings(config), now=now,
        )
        self.assertEqual(selected["seconds"], 30 * 60)



class AuditFollowUpTests(unittest.TestCase):
    """matrix-release-auditor findings on the local-time change (2026-10-14)."""

    def test_to_utc_is_absolute_for_any_browser_zone(self):
        import datetime as dt
        import local_clock
        moment = local_clock.to_utc(dt.datetime(2030, 7, 1, 9, 31), 'Europe/London')
        self.assertEqual(moment, dt.datetime(2030, 7, 1, 8, 31, tzinfo=dt.timezone.utc))
        moment = local_clock.to_utc(dt.datetime(2030, 1, 15, 9, 31), 'Asia/Tokyo')
        self.assertEqual(moment, dt.datetime(2030, 1, 15, 0, 31, tzinfo=dt.timezone.utc))

    def test_announcement_timing_refresh_at_is_utc_instant(self):
        import datetime as dt
        from unittest.mock import patch
        from tests import test_config_store
        from tests.test_webapp import WebAppTests
        case = WebAppTests('test_announcement_timing_api_drives_fresh_and_unavailable_aggregate')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.pair_browser()
        current = case.store.get()
        active = test_config_store.ConfigStoreTests.announcement()
        fixed = dt.datetime(2030, 1, 15, 12, 0, 30, tzinfo=dt.timezone.utc)
        # Unsaved Weather draft in Tokyo (UTC+9): refresh_at must still be an absolute instant.
        weather = dict(current['weather'], timezone='Asia/Tokyo')
        with patch('local_clock._utc_now', return_value=fixed):
            response = case.paired_request('POST', '/api/scroll-timing/announcement_board', json={
                'base_revision': current['revision'], 'settings': {'announcements': [active]}, 'weather': weather})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body['refresh_at'].endswith('Z'), body['refresh_at'])
        refresh = dt.datetime.fromisoformat(body['refresh_at'].replace('Z', '+00:00'))
        self.assertGreater(refresh, fixed)
        self.assertLessEqual(refresh - fixed, dt.timedelta(minutes=1))

    def test_validator_accepts_blank_and_explicit_utc_survives_save(self):
        import json
        import tempfile
        from pathlib import Path as P
        from config_store import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(P(directory) / 'config.json')
            self.assertEqual(store.get()['event_countdown']['timezone'], '')
            store.update({'event_countdown': {'timezone': 'UTC'}})
            self.assertEqual(ConfigStore(store.path).get()['event_countdown']['timezone'], 'UTC')
            self.assertEqual(json.loads(store.path.read_text())['event_countdown']['timezone'], 'UTC')
            store.update({'event_countdown': {'timezone': ''}})
            self.assertEqual(ConfigStore(store.path).get()['event_countdown']['timezone'], '')


if __name__ == "__main__":
    unittest.main()
