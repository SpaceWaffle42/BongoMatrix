import datetime as dt

import pytest

from bongo.modules.countdown import holidays as h

D = dt.date


def on(rule, year):
    return h.next_date(rule, D(year, 1, 1))


@pytest.mark.parametrize("year,expected", [(2024, D(2024, 3, 31)), (2025, D(2025, 4, 20)),
                                           (2026, D(2026, 4, 5)), (2027, D(2027, 3, 28))])
def test_easter(year, expected):
    assert on("easter", year) == expected


def test_easter_relative_and_mothering_sunday():
    assert on("good_friday", 2026) == D(2026, 4, 3)
    assert on("easter_monday", 2026) == D(2026, 4, 6)
    assert on("mothers_day_uk", 2026) == D(2026, 3, 15)


def test_weekday_rules():
    assert on("thanksgiving_us", 2026) == D(2026, 11, 26)
    assert on("spring_bank_holiday_uk", 2026) == D(2026, 5, 25)
    assert on("may_bank_holiday_uk", 2026) == D(2026, 5, 4)
    assert on("mothers_day_us", 2026) == D(2026, 5, 10)
    assert h.next_date("last-mon-08", D(2026, 1, 1)) == D(2026, 8, 31)
    assert h.next_date("last-mon-12", D(2026, 1, 1)) == D(2026, 12, 28)


def test_uk_substitution():
    assert on("christmas_bank_holiday_uk", 2021) == D(2021, 12, 27)
    assert on("boxing_day_bank_holiday_uk", 2021) == D(2021, 12, 28)
    assert on("christmas_bank_holiday_uk", 2022) == D(2022, 12, 27)
    assert on("boxing_day_bank_holiday_uk", 2022) == D(2022, 12, 26)
    assert on("new_year_bank_holiday_uk", 2022) == D(2022, 1, 3)
    assert on("new_year_bank_holiday_uk", 2023) == D(2023, 1, 2)
    assert on("christmas_bank_holiday_uk", 2024) == D(2024, 12, 25)


def test_next_date_rolls_over():
    assert h.next_date("christmas", D(2026, 12, 25)) == D(2026, 12, 25)
    assert h.next_date("christmas", D(2026, 12, 26)) == D(2027, 12, 25)
    assert h.next_date("12-25", D(2026, 12, 26)) == D(2027, 12, 25)
    assert h.next_date("easter+1", D(2026, 4, 6)) == D(2026, 4, 6)
    assert h.next_date("4th-thu-11", D(2026, 11, 27)) == D(2027, 11, 25)


def test_bad_inputs():
    for bad in ("nope", "13-01", "02-30", "5th-fri-02", "easter+x", "1st-xyz-05"):
        with pytest.raises(ValueError):
            h.next_date(bad, D(2026, 1, 1))
    with pytest.raises(ValueError):
        h.label("nope")
    with pytest.raises(ValueError):
        h.resolve("next:nope", dt.datetime(2026, 1, 1))


def test_labels_and_rules():
    assert h.label("christmas") == "Christmas"
    assert h.label("easter") == "Easter Sunday"
    assert h.label("may_bank_holiday_uk") == "Early May bank holiday"
    for rule_id in h.RULES:
        assert h.next_date(rule_id, D(2026, 1, 1)) >= D(2026, 1, 1)


def test_resolve_same_day_and_rollover():
    t, name = h.resolve("next:christmas", dt.datetime(2026, 12, 25, 15, 30))
    assert (t, name) == (dt.datetime(2026, 12, 25), "Christmas")
    t, _ = h.resolve("next:christmas", dt.datetime(2026, 12, 26, 0, 0, 1))
    assert t == dt.datetime(2027, 12, 25)


def test_resolve_specs_and_tz():
    tz = dt.timezone(dt.timedelta(hours=2))
    t, name = h.resolve("next:4th-thu-11", dt.datetime(2026, 11, 10, 9, tzinfo=tz))
    assert t == dt.datetime(2026, 11, 26, tzinfo=tz) and t.tzinfo is tz and name == "4th-thu-11"
    assert h.resolve("next:easter+1", dt.datetime(2026, 1, 1))[0] == dt.datetime(2026, 4, 6)
    assert h.resolve("next:12-25", dt.datetime(2026, 1, 1)) [0] == dt.datetime(2026, 12, 25)


def test_resolve_iso_passthrough():
    naive, aware = dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert h.resolve("2027-01-01T10:00:00", naive) == (dt.datetime(2027, 1, 1, 10), None)
    assert h.resolve("2027-01-01T10:00:00+00:00", naive)[0] == dt.datetime(2027, 1, 1, 10)
    t, name = h.resolve("2027-01-01T10:00:00", aware)
    assert t == dt.datetime(2027, 1, 1, 10, tzinfo=dt.timezone.utc) and name is None
