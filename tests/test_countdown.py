import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from bongo.module import Context
from bongo.modules.countdown import MODULE
from bongo.modules.countdown.common import remaining

TARGET = dt.datetime(2026, 12, 25)


def make(style, **settings):
    return MODULE.create(style, settings, Context(rng=random.Random(1)))


def test_days_remaining_is_exact():
    s = MODULE.settings_for("days")
    assert remaining(s, TARGET - dt.timedelta(days=42))[0] // 86400 == 42
    assert remaining(s, TARGET - dt.timedelta(days=41, hours=23))[0] // 86400 == 41


def test_days_style_differs_by_day_and_switches_to_clock_under_a_day():
    st = make("days", label="Xmas")
    a = st.render(TARGET - dt.timedelta(days=42)).tobytes()
    assert a != st.render(TARGET - dt.timedelta(days=41)).tobytes()
    # under a day the picture changes with the minute, above a day it does not
    under = TARGET - dt.timedelta(hours=3)
    assert st.render(under).tobytes() != st.render(under + dt.timedelta(minutes=1)).tobytes()
    over = TARGET - dt.timedelta(days=5, hours=12)
    assert st.render(over).tobytes() == st.render(over + dt.timedelta(minutes=1)).tobytes()


@pytest.mark.parametrize("style", ["days", "clock"])
def test_done_message_after_target(style):
    st = make(style)
    before = st.render(TARGET - dt.timedelta(seconds=1)).tobytes()
    after = st.render(TARGET + dt.timedelta(hours=1)).tobytes()
    assert before != after
    assert after == make(style, done_message="It's here!").render(TARGET + dt.timedelta(hours=1)).tobytes()
    assert after != make(style, done_message="Yay").render(TARGET + dt.timedelta(hours=1)).tobytes()


def test_clock_ticks_between_seconds():
    st = make("clock")
    t = TARGET - dt.timedelta(days=2, hours=5)
    assert st.render(t).tobytes() != st.render(t + dt.timedelta(seconds=1)).tobytes()


def test_progress_bar_grows_and_can_be_disabled():
    st = make("days", label="Xmas")
    row = lambda now: sum(1 for x in range(64) if st.render(now).getpixel((x, 31)) == (255, 181, 71))
    assert row(dt.datetime(2026, 11, 5)) < row(dt.datetime(2026, 12, 1)) < row(dt.datetime(2026, 12, 20))
    assert make("days", progress_bar=False).render(dt.datetime(2026, 12, 20)).getpixel((0, 31)) != (255, 181, 71)


@pytest.mark.parametrize("style", ["days", "clock"])
def test_timezone_aware_now_matches_naive(style):
    st = make(style)
    naive = dt.datetime(2026, 12, 20, 12)
    aware = naive.replace(tzinfo=ZoneInfo("Europe/London"))
    assert st.render(naive).tobytes() == st.render(aware).tobytes()
    assert st.render(aware + dt.timedelta(days=10)).tobytes() != st.render(aware).tobytes()


NEW = ["flip", "ring", "tearoff"]


@pytest.mark.parametrize("style", NEW)
def test_new_styles_render_and_differ_by_day(style):
    st = make(style)
    a = st.render(TARGET - dt.timedelta(days=12, hours=2))
    assert a.size == (64, 32)
    assert a.tobytes() != st.render(TARGET - dt.timedelta(days=11, hours=2)).tobytes()


@pytest.mark.parametrize("style", NEW)
def test_new_styles_done_message(style):
    st = make(style)
    after = TARGET + dt.timedelta(hours=1)
    assert st.render(TARGET - dt.timedelta(seconds=1)).tobytes() != st.render(after).tobytes()
    assert st.render(after).tobytes() == make(style, done_message="It's here!").render(after).tobytes()
    assert st.render(after).tobytes() != make(style, done_message="Yay").render(after).tobytes()


def test_flip_animates_only_in_the_flip_window():
    st = make("flip")
    base = TARGET - dt.timedelta(hours=3, minutes=7, seconds=21)  # exact second boundary
    at = lambda ms: st.render(base + dt.timedelta(milliseconds=ms)).tobytes()
    assert at(50) != at(150) != at(220)             # mid-flip frames differ
    assert at(300) == at(900)                       # settled between changes
    assert at(300) != at(1300)                      # next second's digits
    days = TARGET - dt.timedelta(days=5, hours=3, minutes=7, seconds=21)
    assert st.render(days).tobytes() == st.render(days + dt.timedelta(milliseconds=500)).tobytes()


def test_ring_arc_shrinks_as_target_nears():
    st = make("ring")
    lit = lambda days: sum(1 for x in range(26) for y in range(1, 27)
                           if st.render(TARGET - dt.timedelta(days=days)).getpixel((x, y))[0] > 200)
    assert lit(40) > lit(20) > lit(5) > lit(1)


def test_tearoff_shows_target_month_and_hhmm_under_a_day():
    st = make("tearoff")
    other = make("tearoff", target="2027-03-25T00:00", start="2026-11-01T00:00")
    assert st.render(TARGET - dt.timedelta(days=30)).tobytes() != other.render(
        dt.datetime(2027, 3, 25) - dt.timedelta(days=30)).tobytes()
    under = TARGET - dt.timedelta(hours=3)
    assert st.render(under).tobytes() != st.render(under + dt.timedelta(minutes=1)).tobytes()
    over = TARGET - dt.timedelta(days=5, hours=12)
    assert st.render(over).tobytes() == st.render(over + dt.timedelta(minutes=1)).tobytes()


def test_holiday_target_rolls_to_next_year_and_is_done_on_the_day():
    from bongo.modules.countdown.common import parse
    assert parse("next:christmas", dt.datetime(2026, 12, 25, 15)) == dt.datetime(2026, 12, 25)
    assert parse("next:christmas", dt.datetime(2026, 12, 26)) == dt.datetime(2027, 12, 25)
    s = MODULE.settings_for("days", {"target": "next:easter"})
    left, _, over = remaining(s, dt.datetime(2027, 3, 1))
    assert not over and left // 86400 == 27  # Easter Sunday 2027-03-28
    assert remaining(MODULE.settings_for("days"), dt.datetime(2026, 12, 25, 12))[2]  # done all Christmas Day


def test_progress_defaults_to_the_last_30_days():
    s = MODULE.settings_for("days", {"target": "2026-12-25T00:00"})
    assert remaining(s, dt.datetime(2026, 11, 1))[1] == 0.0
    assert remaining(s, dt.datetime(2026, 12, 10))[1] == 0.5
