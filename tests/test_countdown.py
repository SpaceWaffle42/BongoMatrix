import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from bongo.module import Context
from bongo.modules.countdown import MODULE
from bongo.modules.countdown.events import current, from_old, occurrence, problems

TARGET = dt.datetime(2026, 12, 25)


def make(style, **settings):
    return MODULE.create(style, settings, Context(rng=random.Random(1)))


def left(now, events=None):
    """(seconds left, progress, on now?) for the event a Days countdown would show."""
    st = make("days", **({"events": events} if events is not None else {}))
    return st.pick(now)


XMAS = [{"title": "Xmas", "holiday": "christmas"}]


def test_days_remaining_is_exact():
    assert left(TARGET - dt.timedelta(days=42))[0] // 86400 == 42
    assert left(TARGET - dt.timedelta(days=41, hours=23))[0] // 86400 == 41


def test_days_style_differs_by_day_and_switches_to_clock_under_a_day():
    st = make("days", events=XMAS)
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
    st = make("days", events=XMAS)
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
    other = make("tearoff", events=[{"title": "Trip", "date": "2027-03-25", "from": "2026-11-01"}])
    assert st.render(TARGET - dt.timedelta(days=30)).tobytes() != other.render(
        dt.datetime(2027, 3, 25) - dt.timedelta(days=30)).tobytes()
    under = TARGET - dt.timedelta(hours=3)
    assert st.render(under).tobytes() != st.render(under + dt.timedelta(minutes=1)).tobytes()
    over = TARGET - dt.timedelta(days=5, hours=12)
    assert st.render(over).tobytes() == st.render(over + dt.timedelta(minutes=1)).tobytes()


def test_holiday_rolls_to_next_year_and_is_on_all_day():
    assert left(dt.datetime(2026, 12, 25, 15))[2]                          # on all Christmas Day
    assert left(dt.datetime(2026, 12, 26))[0] // 86400 == 364               # then next year's
    easter = [{"title": "Easter", "holiday": "easter"}]
    secs, _, on = left(dt.datetime(2027, 3, 1), easter)
    assert not on and secs // 86400 == 27  # Easter Sunday 2027-03-28


def test_progress_defaults_to_the_last_30_days_or_starts_from_a_date():
    once = [{"title": "Xmas", "date": "2026-12-25"}]
    assert left(dt.datetime(2026, 11, 1), once)[1] == 0.0
    assert left(dt.datetime(2026, 12, 10), once)[1] == 0.5
    since = [{"title": "Xmas", "date": "2026-12-25", "from": "2026-12-05"}]
    assert left(dt.datetime(2026, 12, 15), since)[1] == 0.5


EVENTS = [
    {"title": "Mum", "date": "1960-03-14", "repeat": "yearly"},
    {"title": "Opening", "date": "2026-12-01", "time": "09:00", "until": "17:00", "message": "We're open!"},
    {"title": "Party", "date": "2026-12-01", "time": "19:30"},
    {"title": "Xmas", "holiday": "christmas", "colour": "#FF0000"},
]


def test_the_soonest_unfinished_event_shows():
    at = lambda *t: current(EVENTS, dt.datetime(*t)).event["title"]
    assert at(2026, 11, 11) == "Opening"
    assert at(2026, 12, 1, 12) == "Opening"       # on from 09:00 until 17:00
    assert at(2026, 12, 1, 17, 1) == "Party"
    assert at(2026, 12, 1, 20) == "Party"         # an hour from 19:30 with no end time
    assert at(2026, 12, 1, 20, 31) == "Xmas"
    assert at(2026, 12, 26) == "Mum"              # one-offs are gone; the yearly ones come round
    assert at(2027, 3, 14, 23) == "Mum"           # all day on the day
    assert at(2027, 3, 15) == "Xmas"


def test_yearly_29_february_falls_on_28_february():
    o = occurrence({"title": "Leap", "date": "2024-02-29", "repeat": "yearly"}, dt.datetime(2026, 1, 1))
    assert o.target == dt.datetime(2026, 2, 28)


def test_one_off_events_finish_and_then_nothing_is_left():
    past = [{"title": "Done", "date": "2026-01-01"}]
    assert current(past, dt.datetime(2026, 11, 11)) is None
    st = make("days", events=past)
    blank = st.render(dt.datetime(2026, 11, 11))
    idle = make("days", events=past, idle_text="Nothing booked").render(dt.datetime(2026, 11, 11))
    assert blank.tobytes() != idle.tobytes()


def test_an_event_brings_its_own_message_and_colour():
    st = make("days", events=EVENTS)
    on = st.render(dt.datetime(2026, 12, 1, 10)).tobytes()
    assert on != make("days", events=[dict(EVENTS[1], message="Other")]).render(dt.datetime(2026, 12, 1, 10)).tobytes()
    red = make("days", events=EVENTS[3:]).render(dt.datetime(2026, 12, 20))
    assert (255, 0, 0) in {red.getpixel((x, 31)) for x in range(64)}  # progress bar in the event colour


def test_bad_events_are_explained():
    assert problems([{"title": "A", "date": "2026-01-01"}, {"title": "B", "holiday": "christmas"}]) == []
    found = " ".join(problems([{"title": ""}, {"title": "X", "date": "2026-13-01"},
                               {"title": "Y", "holiday": "nope"}, {"title": "Z", "date": "2026-01-01", "time": "25:00"},
                               {"title": "W", "date": "2026-01-01", "colour": "red", "extra": 1}]))
    for words in ("event 1 needs a title", "date or a holiday", "isn't a valid date", "unknown holiday",
                  "isn't a valid time", "colour must look like", "unknown fields"):
        assert words in found
    with pytest.raises(ValueError):
        MODULE.settings_for("days", {"events": [{"title": "No date"}]})


@pytest.mark.parametrize("old, event", [
    ({"label": "Xmas", "target": "next:christmas"}, {"title": "Xmas", "holiday": "christmas"}),
    ({"label": "Launch", "target": "2027-01-05T18:30", "start": "2026-12-01T00:00"},
     {"title": "Launch", "date": "2027-01-05", "time": "18:30", "from": "2026-12-01"}),
    ({"label": "Party", "target": "next:12-24"}, {"title": "Party", "date": "2000-12-24", "repeat": "yearly"}),
    ({"target": "2027-02-01T00:00"}, {"title": "Christmas", "date": "2027-02-01"}),
])
def test_old_single_event_settings_become_the_events_list(old, event):
    settings = dict(old, done_message="Yay")
    assert from_old(settings)
    assert settings == {"events": [event], "done_message": "Yay"}
    assert from_old(settings) == []  # already moved: nothing to do


def test_config_tidy_moves_an_old_countdown_into_events():
    from bongo import config as cfg
    from bongo.registry import discover
    config = {"playlist": [{"module": "countdown", "style": "days",
                            "settings": {"label": "Xmas", "target": "next:christmas", "progress_bar": False}}]}
    notes = cfg.tidy(config, discover())
    assert any("events list" in n for n in notes)
    assert config["playlist"][0]["settings"] == {"events": [{"title": "Xmas", "holiday": "christmas"}],
                                                 "progress_bar": False}
