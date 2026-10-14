"""Offline astronomy reference contracts (USNO fixtures, not self-derived dates)."""
import datetime as dt
import importlib.util
import json
import dataclasses
import threading
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

UTC = dt.timezone.utc


class MoonReferenceTests(unittest.TestCase):
    def test_all_2024_major_phases_against_downloaded_usno_fixture(self):
        from astronomy import Astronomy, Location
        fixture = Path(__file__).parent / 'fixtures/astronomy/usno-phases-2024.json'
        data = json.loads(fixture.read_text())
        self.assertEqual(data['numphases'], 50)
        location = Location(-33.8688, 151.2093, 'Australia/Sydney', 'Sydney', 'map pin')
        with Astronomy() as engine:
            for phase in data['phasedata']:
                expected = dt.datetime.fromisoformat(f"{phase['year']:04}-{phase['month']:02}-{phase['day']:02}T{phase['time']}+00:00")
                with self.subTest(phase=phase):
                    result = engine.moon(expected - dt.timedelta(hours=1), location).next_phase
                    self.assertEqual(result.name, phase['phase'])
                    self.assertLess(abs((result.time - expected).total_seconds()), 60)

    def test_greenwich_eclipse_day_matches_usno_reference(self):
        self.assertIsNotNone(importlib.util.find_spec('astronomy'), 'Local astronomy core absent')
        from astronomy import Astronomy, Location
        location = Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
        with Astronomy() as engine:
            moon = engine.moon(dt.datetime(2024, 4, 8, 12, tzinfo=UTC), location)
        self.assertLess(moon.illumination, .01)
        self.assertEqual(moon.phase_name, 'New Moon')
        self.assertEqual(moon.next_phase.name, 'New Moon')
        # USNO API v4.0.1, 2024 phases and Greenwich one-day data, UTC.
        self.assertLess(abs((moon.next_phase.time - dt.datetime(2024, 4, 8, 18, 21, tzinfo=UTC)).total_seconds()), 60)
        self.assertLess(abs((moon.rises[0] - dt.datetime(2024, 4, 8, 5, 15, tzinfo=UTC)).total_seconds()), 60)
        self.assertLess(abs((moon.sets[0] - dt.datetime(2024, 4, 8, 18, 41, tzinfo=UTC)).total_seconds()), 60)
        self.assertEqual(moon.rises[0].tzinfo.key, 'Europe/London')
        self.assertEqual(moon.source['ephemeris'], 'DE440s')
        self.assertTrue(moon.source['checksum_verified'])

    def test_calendar_dst_polar_validation_and_cancel(self):
        import astronomy as a
        self.assertTrue(hasattr(a, 'local_day_bounds'), 'Explicit local-calendar day boundary absent')
        for day, hours in ((dt.date(2024, 3, 31), 23), (dt.date(2024, 10, 27), 25),
                           (dt.date(2024, 2, 29), 24)):
            start, end = a.local_day_bounds(day, 'Europe/London')
            self.assertEqual((end - start).total_seconds(), hours * 3600)
        location = a.Location(90, 0, 'UTC', 'North Pole', 'map pin')
        with a.Astronomy() as engine:
            for value in (dt.datetime(2024, 1, 1), dt.datetime(1999, 12, 31, tzinfo=UTC),
                          dt.datetime(2101, 1, 1, tzinfo=UTC)):
                with self.assertRaises(ValueError):
                    engine.moon(value, location)
            for changes in ({'latitude': float('nan')}, {'longitude': 181}, {'timezone': 'invalid'},
                            {'latitude': True}, {'label': ''}, {'provenance': ''}):
                with self.assertRaises(ValueError):
                    dataclasses.replace(location, **changes)
            for month, day, state in ((6, 21, 'always_below'), (12, 15, 'always_above')):
                moon = engine.moon(dt.datetime(2024, month, day, 12, tzinfo=UTC), location)
                self.assertEqual(moon.horizon_state, state)
                self.assertEqual((moon.rises, moon.sets), ((), ()))
            stop = threading.Event()
            stop.set()
            with patch.object(engine.ephemeris, 'close'):
                with self.assertRaises(a.Cancelled):
                    engine.moon(dt.datetime(2024, 1, 1, tzinfo=UTC), location, stop_event=stop)
            # Cancellation within the numerical search, not merely before it.
            stop.clear()
            original = a.almanac.find_discrete
            def cancelling(start, end, function, **kwargs):
                stop.set()
                return original(start, end, function, **kwargs)
            with patch.object(a.almanac, 'find_discrete', side_effect=cancelling):
                with self.assertRaises(a.Cancelled):
                    engine.moon(dt.datetime(2024, 1, 1, tzinfo=UTC), location, stop_event=stop)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            bad = Path(directory) / 'bad.bsp'
            bad.write_bytes(b'not an ephemeris')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                a.Astronomy(bad)
