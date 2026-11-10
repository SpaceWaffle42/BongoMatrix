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
