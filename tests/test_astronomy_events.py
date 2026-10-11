"""Offline event contracts; synthetic intervals are scheduling fixtures, not sky data."""
import dataclasses
import datetime as dt
import itertools
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import astronomy as a

UTC = dt.timezone.utc


def instant(text):
    return dt.datetime.fromisoformat(text).astimezone(UTC)


class EventTests(unittest.TestCase):
    def test_canonical_selection_dedup_local_date_dst_and_end_exclusive(self):
        self.assertTrue(hasattr(a, 'AstronomyEvent'), 'Canonical event selection boundary absent')
        location = a.Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
        now = instant('2024-10-27T00:30:00+00:00')
        window = a.VisibilityWindow(now, now + dt.timedelta(hours=2))
        event = a.AstronomyEvent('fixture:one', 'full_moon', 'Full Moon', now,
                                 location, (window,), 'fixture A')
        duplicate = dataclasses.replace(event, source='fixture Z')
        tied = dataclasses.replace(event, identity='fixture:two')
        tomorrow = dataclasses.replace(event, identity='fixture:tomorrow',
            time=instant('2024-10-28T00:00:00+00:00'))
        wrong_place = dataclasses.replace(event, location=dataclasses.replace(location, latitude=0))
        for order in itertools.permutations((duplicate, event, tied)):
            selected = a.select_events(order + (tomorrow, wrong_place), now, location, horizon_days=1)
            self.assertEqual(selected, (event, tied))
        self.assertEqual(a.select_events((event,), window.end, location), ())
        self.assertEqual(a.select_events((event,), now, location, limit=1), (event,))
        self.assertEqual(a.event_horizon(now, location, 1), instant('2024-10-28T00:00:00+00:00'))
        spring = instant('2024-03-31T00:00:00+00:00')
        self.assertEqual((a.event_horizon(spring, location, 1) - spring).total_seconds(), 23 * 3600)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.source = 'changed'
        for changes in ({'identity': ''}, {'time': dt.datetime(2024, 1, 1)}, {'windows': ()},
                        {'windows': [window]}, {'source': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dataclasses.replace(event, **changes)
        with self.assertRaises(ValueError):
            a.VisibilityWindow(now, now + dt.timedelta(minutes=9, seconds=59))
        with self.assertRaises(ValueError):
            a.select_events((event,), now, location, horizon_days=True)

    def test_real_phase_dates_local_visibility_and_cancellation(self):
        self.assertTrue(hasattr(a.Astronomy, 'lunar_events'), 'Offline lunar event preparation absent')
        location = a.Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
        now = instant('2024-04-01T00:00:00+00:00')
        fixture = json.loads((Path(__file__).parent / 'fixtures/astronomy/usno-phases-2024.json').read_text())
        full = next(p for p in fixture['phasedata'] if p['month'] == 4 and p['phase'] == 'Full Moon')
        expected = dt.datetime(full['year'], full['month'], full['day'],
            *map(int, full['time'].split(':')), tzinfo=UTC)
        with a.Astronomy() as engine:
            events = engine.lunar_events(now, location, horizon_days=30)
            self.assertEqual([e.title for e in events], ['Full Moon'])
            event = events[0]
            self.assertLess(abs((event.time - expected).total_seconds()), 60)
            # USNO UTC Apr 23 is local Apr 24, not Apr 23.
            self.assertEqual(event.time.astimezone(a.ZoneInfo(location.timezone)).date(), dt.date(2024, 4, 24))
            self.assertIn('DE440s', event.source)
            day_start, day_end = a.local_day_bounds(dt.date(2024, 4, 24), location.timezone)
            observer = engine.ephemeris['earth'] + a.wgs84.latlon(location.latitude, location.longitude)
            for window in event.windows:
                self.assertGreaterEqual(window.start, day_start)
                self.assertLessEqual(window.end, day_end)
                self.assertGreaterEqual((window.end - window.start).total_seconds(), 600)
                # Internal geometry consistency, NOT independent altitude accuracy evidence.
                for moment in (window.start + dt.timedelta(seconds=1), window.end - dt.timedelta(seconds=1)):
                    observer_at = observer.at(engine.timescale.from_datetime(moment))
                    self.assertGreater(observer_at.observe(engine.ephemeris['moon']).apparent().altaz()[0].degrees, 10)
                    self.assertLess(observer_at.observe(engine.ephemeris['sun']).apparent().altaz()[0].degrees, -18)
            # No fabricated new-Moon observation under the altitude/darkness rule.
            self.assertEqual(engine.moon_visibility(dt.date(2024, 4, 8), location), ())
            pole = a.Location(90, 0, 'UTC', 'North Pole', 'map pin')
            self.assertEqual(engine.moon_visibility(dt.date(2024, 6, 21), pole), ())
            stop = threading.Event()
            stop.set()
            with self.assertRaises(a.Cancelled):
                engine.lunar_events(now, location, stop_event=stop)
            stop.clear()
            original = a.almanac.find_discrete
            def cancel(start, end, function, **kwargs):
                stop.set()
                return original(start, end, function, **kwargs)
            with patch.object(a.almanac, 'find_discrete', side_effect=cancel):
                with self.assertRaises(a.Cancelled):
                    engine.lunar_events(now, location, stop_event=stop)
            with self.assertRaises(ValueError):
                engine.lunar_events(instant('2100-12-31T12:00:00+00:00'), location)

    def test_all_2024_observable_full_moons_match_independent_usno_dates(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/astronomy/usno-phases-2024.json').read_text())
        expected = [p for p in fixture['phasedata'] if p['phase'] == 'Full Moon']
        location = a.Location(0, 0, 'UTC', 'Equator', 'map pin')
        with a.Astronomy() as engine:
            events = engine.lunar_events(instant('2024-01-01T00:00:00+00:00'), location, horizon_days=366)
        self.assertEqual(len(events), len(expected))
        for event, phase in zip(events, expected, strict=True):
            reference = dt.datetime(phase['year'], phase['month'], phase['day'],
                *map(int, phase['time'].split(':')), tzinfo=UTC)
            self.assertEqual(event.title, 'Full Moon')
            self.assertLess(abs((event.time - reference).total_seconds()), 60)

    def test_fold_window_uses_elapsed_time_not_ambiguous_wall_time(self):
        zone = a.ZoneInfo('Europe/London')
        start = dt.datetime(2024, 10, 27, 1, 50, tzinfo=zone, fold=0)
        end = dt.datetime(2024, 10, 27, 1, 10, tzinfo=zone, fold=1)
        window = a.VisibilityWindow(start, end)
        self.assertEqual((window.end - window.start).total_seconds(), 20 * 60)
        with self.assertRaises(ValueError):
            a.VisibilityWindow(end, start)
