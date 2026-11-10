import datetime as dt
import random

import numpy as np
import pytest

from bongo.module import Context
from bongo.modules.calendar import MODULE

NOW = dt.datetime(2026, 11, 10, 14, 20)


def style(name, w=64, h=32, **settings):
    return MODULE.create(name, settings, Context(width=w, height=h, rng=random.Random(1)))


def test_upcoming_mixes_holidays_and_events_in_date_order():
    s = style("page", events=[{"date": "11-21", "title": "Mum's birthday"}, {"date": "2026-11-01", "title": "Gone"}])
    got = s.upcoming(NOW.date(), 3)
    assert [t for _, t, _ in got] == ["Mum's birthday", "Christmas", "New Year's Day"]
    assert got[0][0] == dt.date(2026, 11, 21) and got[0][2] is True


def test_events_are_checked_when_the_style_is_built():
    with pytest.raises(ValueError, match="events\\[0\\]"):
        style("page", events=[{"date": "next tuesday", "title": "x"}])
    with pytest.raises(ValueError, match="needs a date and a title"):
        style("page", events=[{"date": "12-01"}])
    with pytest.raises(ValueError):
        style("page", holidays=["not_a_holiday"])


def test_month_marks_holidays_in_this_month():
    s = style("month", events=[{"date": "11-21", "title": "B"}])
    assert s.marked(2026, 11) == {5, 21}  # bonfire night + the event


def test_progress_fractions():
    s = style("progress")
    f = s.fractions(dt.datetime(2026, 1, 1, 12))
    assert abs(f["day"] - 0.5) < 1e-9 and f["year"] < 0.002 and abs(f["week"] - (3.5 / 7)) < 1e-9  # a Thursday
    sunday = style("progress", week_start="sunday").fractions(dt.datetime(2026, 11, 8))
    assert sunday["week"] == 0


def test_long_titles_scroll_and_short_ones_stay_put():
    s = style("page", holidays=[], events=[{"date": "11-21", "title": "A very long birthday party title"}])
    a, b = (np.asarray(s.render(NOW + dt.timedelta(seconds=k))) for k in (3, 4))
    assert (a != b).any()
    s = style("page", holidays=["christmas"])
    assert (np.asarray(s.render(NOW)) == np.asarray(s.render(NOW + dt.timedelta(seconds=1)))).all()


def test_nothing_coming_up():
    s = style("page", holidays=[], events=[{"date": "2020-01-01", "title": "Past"}])
    assert np.asarray(s.render(NOW)).any()
