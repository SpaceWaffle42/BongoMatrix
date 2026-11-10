import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from bongo.module import Context
from bongo.modules.announcements import MODULE, active

D = dt.datetime  # 2026-11-09 is a Monday


def one(**kw):
    return {"title": "T", "text": "x", **kw}


def test_unscheduled_always_active():
    assert active([one()], D(2026, 11, 9, 12)) == [one()]


def test_date_window_is_half_open():
    it = one(start="2026-11-09T09:00", end="2026-11-09T17:00")
    assert not active([it], D(2026, 11, 9, 8, 59, 59))
    assert active([it], D(2026, 11, 9, 9))
    assert active([it], D(2026, 11, 9, 16, 59, 59))
    assert not active([it], D(2026, 11, 9, 17))


def test_weekdays():
    it = one(days=["mon", "Tue"])
    assert active([it], D(2026, 11, 9)) and active([it], D(2026, 11, 10))
    assert not active([it], D(2026, 11, 11))


def test_time_window():
    it = one(**{"from": "09:00", "to": "17:00"})
    assert not active([it], D(2026, 11, 9, 8, 59))
    assert active([it], D(2026, 11, 9, 9, 0))
    assert not active([it], D(2026, 11, 9, 17, 0))


def test_overnight_window_and_its_weekday():
    it = one(days=["fri"], **{"from": "22:00", "to": "06:00"})
    assert active([it], D(2026, 11, 13, 23))       # Fri night
    assert active([it], D(2026, 11, 14, 2))        # Sat early morning belongs to Friday's window
    assert not active([it], D(2026, 11, 13, 2))    # Fri early morning belongs to Thursday's
    assert not active([it], D(2026, 11, 13, 12))
    assert not active([one(**{"from": "22:00", "to": "06:00"})], D(2026, 11, 9, 12))


def test_open_ended_times():
    assert active([one(**{"from": "18:00"})], D(2026, 11, 9, 20))
    assert not active([one(**{"to": "06:00"})], D(2026, 11, 9, 20))


def test_aware_now_adopts_zone_for_naive_values():
    tz = ZoneInfo("Europe/London")
    it = one(start="2026-11-09T09:00", end="2026-11-09T17:00")
    assert active([it], D(2026, 11, 9, 12, tzinfo=tz))
    assert not active([it], D(2026, 11, 9, 18, tzinfo=tz))
    aware = one(start="2026-11-09T09:00+00:00")
    assert active([aware], D(2026, 11, 9, 10, tzinfo=ZoneInfo("UTC")))


def mk(style, items=None, w=64, h=32, **s):
    settings = dict(s, **({"items": items} if items is not None else {}))
    return MODULE.create(style, settings, Context(width=w, height=h, rng=random.Random(1)))


def test_rotation_depends_only_on_time():
    a, b = one(title="AAA", text="aaa"), one(title="BBB", text="bbb")
    st = mk("card", [a, b], rotate_seconds=10)
    base = D(2026, 11, 9, 12).timestamp() // 10 * 10
    f = lambda off: st.render(dt.datetime.fromtimestamp(base + off)).tobytes()
    assert f(1) == f(2)
    assert f(1) != f(11)
    assert f(1) == f(21)
    assert st.pick(dt.datetime.fromtimestamp(base + 1))[0] is not st.pick(dt.datetime.fromtimestamp(base + 11))[0]


def test_idle_state_default_and_custom():
    now = D(2026, 11, 9, 12)
    gone = one(end="2020-01-01T00:00")
    plain = mk("banner", [gone]).render(now)
    custom = mk("banner", [gone], idle_text="Back soon").render(now)
    assert plain.tobytes() != custom.tobytes()
    assert plain.getbbox() and plain.tobytes() != mk("banner", [one()]).render(now).tobytes()
    assert mk("flash", []).render(now).size == (64, 32)


@pytest.mark.parametrize("style", ["banner", "card", "flash"])
@pytest.mark.parametrize("size", [(64, 32), (58, 28), (48, 20), (32, 16)])
def test_long_and_empty_text_render(style, size):
    for it in (one(title="A very long title that cannot fit", text="word " * 40), one(title="", text=""), one()):
        assert mk(style, [it], *size).render(D(2026, 11, 9, 12)).size == size


def test_styles_animate_where_expected():
    now = D(2026, 11, 9, 12)
    it = [one(title="Fire drill", text="Fire alarm test at 11:00 today, no need to leave")]
    later = now + dt.timedelta(seconds=0.7)
    assert mk("banner", it).render(now).tobytes() != mk("banner", it).render(later).tobytes()
    assert mk("flash", it).render(now).tobytes() != mk("flash", it).render(later).tobytes()
    assert mk("card", it).render(now).tobytes() == mk("card", it).render(later).tobytes()


def test_item_accent_overrides():
    now = D(2026, 11, 9, 12)
    a = mk("banner", [one(accent="#ff0000")]).render(now)
    assert a.getpixel((0, 0)) == (51, 0, 0)  # bar is the item's accent, dimmed
    assert (255, 0, 0) in {a.getpixel((x, y)) for x in range(a.width) for y in range(a.height)}
