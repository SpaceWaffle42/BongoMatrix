"""Holiday packs, calendar icons and birthday confetti (owner request 2026-10-13)."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from config_store import ConfigStore, DEFAULT_CONFIG
from tests.test_event_countdown import event

UTC = dt.timezone.utc


def settings(**changes):
    result = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
    result.update(changes)
    return result


class HolidayDateTests(unittest.TestCase):
    # Published dates (government/religious calendars). Every pack date is calculated offline.
    KNOWN = {
        'christian.christmas': {2026: '2026-12-25'},
        'christian.easter': {2024: '2024-03-31', 2025: '2025-04-20', 2026: '2026-04-05', 2027: '2027-03-28'},
        'christian.good_friday': {2026: '2026-04-03'},
        'christian.ash_wednesday': {2025: '2025-03-05'},
        'christian.pentecost': {2025: '2025-06-08'},
        'orthodox.orthodox_easter': {2024: '2024-05-05', 2025: '2025-04-20', 2026: '2026-04-12'},
        'judaism.passover': {2024: '2024-04-23', 2025: '2025-04-13', 2026: '2026-04-02', 2027: '2027-04-22'},
        'judaism.rosh_hashanah': {2024: '2024-10-03', 2025: '2025-09-23', 2026: '2026-09-12'},
        'judaism.yom_kippur': {2024: '2024-10-12', 2025: '2025-10-02', 2026: '2026-09-21'},
        'judaism.hanukkah': {2025: '2025-12-15', 2026: '2026-12-05'},
        'judaism.purim': {2024: '2024-03-24', 2025: '2025-03-14', 2026: '2026-03-03'},
        'islam.ramadan': {2024: '2024-03-11', 2025: '2025-03-01'},
        'islam.eid_al_fitr': {2023: '2023-04-21', 2024: '2024-04-10', 2025: '2025-03-30', 2026: '2026-03-20'},
        'islam.eid_al_adha': {2023: '2023-06-28', 2024: '2024-06-16', 2025: '2025-06-06'},
        'hinduism.diwali': {2022: '2022-10-24', 2023: '2023-11-12', 2024: '2024-10-31', 2025: '2025-10-20'},
        'hinduism.holi': {2023: '2023-03-08', 2024: '2024-03-25', 2025: '2025-03-14', 2026: '2026-03-04'},
        'hinduism.navaratri': {2023: '2023-10-15', 2024: '2024-10-03', 2025: '2025-09-22'},
        'hinduism.dussehra': {2023: '2023-10-24', 2024: '2024-10-12', 2025: '2025-10-02'},
        'buddhism.vesak': {2023: '2023-05-05', 2024: '2024-05-23', 2025: '2025-05-12'},
        'sikhism.vaisakhi': {2024: '2024-04-13', 2025: '2025-04-14'},
        'sikhism.guru_nanak': {2023: '2023-11-27', 2025: '2025-11-05'},
        'chinese.lunar_new_year': {2023: '2023-01-22', 2024: '2024-02-10', 2025: '2025-01-29', 2026: '2026-02-17'},
        'chinese.qingming': {2024: '2024-04-04', 2025: '2025-04-04'},
        'general.halloween': {2026: '2026-10-31'},
    }

    def test_calculated_dates_match_published_calendars(self):
        import holiday_calendar as holidays
        for key, years in self.KNOWN.items():
            for year, want in years.items():
                with self.subTest(key=key, year=year):
                    self.assertEqual([d.isoformat() for d in holidays.calculated_dates(key, year)], [want])

    def test_every_holiday_has_a_date_every_supported_year_and_eid_can_fall_twice(self):
        import holiday_calendar as holidays
        for key in holidays.HOLIDAYS:
            for year in (1950, 2000, 2026, 2100, 2150):
                self.assertTrue(holidays.calculated_dates(key, year), (key, year))
        self.assertEqual([d.isoformat() for d in holidays.calculated_dates('islam.eid_al_fitr', 2030)][-1][:7], '2030-02')

    def test_packs_cover_major_religions_and_holiday_icons(self):
        import holiday_calendar as holidays
        self.assertTrue({'christian', 'islam', 'judaism', 'hinduism', 'buddhism', 'sikhism'} <= set(holidays.PACKS))
        icons = {key: item['icon'] for key, item in holidays.HOLIDAYS.items()}
        self.assertEqual(icons['christian.christmas'], 'tree')
        self.assertEqual(icons['general.halloween'], 'pumpkin')
        self.assertEqual(icons['christian.easter'], 'egg')
        self.assertEqual(icons['judaism.hanukkah'], 'menorah')
        self.assertEqual(icons['islam.eid_al_fitr'], 'crescent')
        self.assertEqual(icons['hinduism.diwali'], 'diya')


class PackSettingsTests(unittest.TestCase):
    def test_defaults_have_every_pack_off_and_no_edits(self):
        import holiday_calendar as holidays
        defaults = DEFAULT_CONFIG['event_countdown']
        self.assertEqual(defaults['packs'], {pack: False for pack in holidays.PACKS})
        self.assertEqual(defaults['holiday_dates'], {})
        self.assertEqual(defaults['hidden_holidays'], [])

    def test_legacy_saved_countdown_without_pack_fields_loads_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            store = ConfigStore(path)
            store.update({'event_countdown': {'events': [event()]}})
            raw = json.loads(path.read_text())
            for key in ('packs', 'holiday_dates', 'hidden_holidays'):
                raw['event_countdown'].pop(key, None)
            path.write_text(json.dumps(raw))
            loaded = ConfigStore(path).get()['event_countdown']
            self.assertEqual(loaded['events'], [event()])
            self.assertFalse(any(loaded['packs'].values()))
            self.assertEqual(loaded['holiday_dates'], {})

    def test_pack_edits_validate_and_save(self):
        from event_countdown_config import validate
        good = settings(packs=dict(settings()['packs'], christian=True),
                        holiday_dates={'christian.easter': {'2027': '2027-04-04'}},
                        hidden_holidays=['christian.ash_wednesday'])
        validate(good)
        for bad in ({'packs': {'unknown': True}}, {'packs': dict(settings()['packs'], christian=1)},
                    {'holiday_dates': {'christian.nope': {'2027': '2027-04-04'}}},
                    {'holiday_dates': {'christian.easter': {'2027': '2027-13-04'}}},
                    {'holiday_dates': {'christian.easter': {'27': '2027-04-04'}}},
                    {'holiday_dates': {'christian.easter': {'2027': '2028-04-04'}}},
                    {'hidden_holidays': ['christian.nope']}, {'hidden_holidays': 'christian.easter'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate(dict(good, **bad))


class PackSelectionTests(unittest.TestCase):
    def test_enabled_pack_supplies_the_next_event_with_its_icon(self):
        from modules.event_countdown import select_event
        packs = dict(settings()['packs'], christian=True)
        chosen = select_event(settings(packs=packs), now=dt.datetime(2026, 12, 20, tzinfo=UTC))
        self.assertEqual((chosen['event']['title'], chosen['event']['icon']), ('Christmas Eve', 'tree'))
        self.assertIsNone(select_event(settings(), now=dt.datetime(2026, 12, 20, tzinfo=UTC)))

    def test_hidden_holiday_and_edited_date_are_respected(self):
        from modules.event_countdown import select_event
        packs = dict(settings()['packs'], christian=True)
        chosen = select_event(settings(packs=packs, hidden_holidays=['christian.christmas_eve']),
                              now=dt.datetime(2026, 12, 20, tzinfo=UTC))
        self.assertEqual(chosen['event']['title'], 'Christmas Day')
        moved = settings(packs=dict(settings()['packs'], general=True),
                         holiday_dates={'general.halloween': {'2026': '2026-10-30'}})
        chosen = select_event(moved, now=dt.datetime(2026, 10, 29, 12, tzinfo=UTC))
        self.assertEqual((chosen['event']['title'], chosen['start'].date().isoformat()), ('Halloween', '2026-10-30'))

    def test_own_events_still_win_when_earlier(self):
        from modules.event_countdown import select_event
        own = event(id='mine', title='Mine', start='2026-12-21', all_day=True)
        chosen = select_event(settings(events=[own], packs=dict(settings()['packs'], christian=True)),
                              now=dt.datetime(2026, 12, 20, tzinfo=UTC))
        self.assertEqual(chosen['event']['id'], 'mine')


class IconAndConfettiTests(unittest.TestCase):
    def test_holiday_icons_are_coloured_pixel_art(self):
        from modules import event_countdown as module
        icon_settings = settings(layout='icon')
        pumpkin, _ = module.render_frame(icon_settings, dict(event=event(icon='pumpkin'), seconds=86400))
        tree, _ = module.render_frame(icon_settings, dict(event=event(icon='tree'), seconds=86400))
        colours = lambda image: {image.getpixel((x, y)) for x in range(8) for y in range(32)} - {(0, 0, 0)}
        self.assertTrue(any(r > 200 and 60 < g < 170 and b < 60 for r, g, b in colours(pumpkin)), 'pumpkin is not orange')
        self.assertTrue(any(g > 120 and r < 100 for r, g, b in colours(tree)), 'tree is not green')
        self.assertNotEqual(pumpkin.tobytes(), tree.tobytes())

    def test_birthday_now_shows_moving_confetti_only_then(self):
        from modules import event_countdown as module
        cake = event(icon='birthday', title='My birthday')
        first, _ = module.render_frame(settings(), dict(event=cake, seconds=0), elapsed=0)
        later, _ = module.render_frame(settings(), dict(event=cake, seconds=0), elapsed=.5)
        self.assertNotEqual(first.tobytes(), later.tobytes(), 'confetti does not move')
        palette = {first.getpixel((x, y)) for x in range(64) for y in range(32)}
        self.assertGreaterEqual(len(palette - {(0, 0, 0), (255, 255, 255)}), 3, 'no multicoloured confetti')
        waiting = [module.render_frame(settings(), dict(event=cake, seconds=60), elapsed=t)[0].tobytes() for t in (0, .5)]
        self.assertEqual(waiting[0], waiting[1], 'confetti before NOW')
        other = [module.render_frame(settings(), dict(event=event(icon='launch'), seconds=0), elapsed=t)[0].tobytes() for t in (0, .5)]
        self.assertEqual(other[0], other[1], 'confetti on a non-birthday event')


if __name__ == '__main__':
    unittest.main()
