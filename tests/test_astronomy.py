import datetime as dt
import random

import numpy as np

from bongo.module import Context
from bongo.modules.astronomy import MODULE, astro

U = dt.timezone.utc
H = dt.timedelta(hours=1)
LONDON = (51.5074, -0.1278)


def D(*a):
    return dt.datetime(*a, tzinfo=U)


def test_known_phase_moments():
    got = astro.next_phases(D(2024, 4, 1))
    assert abs(got["new"] - D(2024, 4, 8, 18, 21)) < 3 * H
    assert abs(got["full"] - D(2024, 4, 23, 23, 49)) < 3 * H
    assert list(got) == ["last_quarter", "new", "first_quarter", "full"]
    assert abs(astro.next_phases(D(2026, 11, 10))["first_quarter"] - D(2026, 11, 17, 11, 48)) < 3 * H


def test_phases_are_ordered_and_in_the_future():
    t = D(2026, 11, 10, 12)
    times = list(astro.next_phases(t).values())
    assert times == sorted(times) and times[0] > t and times[-1] - t < dt.timedelta(days=30)


def test_illumination_at_new_and_full():
    assert astro.moon_phase(D(2024, 4, 8, 18, 21))[1] < 0.02
    assert astro.moon_phase(D(2024, 4, 23, 23, 49))[1] > 0.98
    age, frac, waxing = astro.moon_phase(D(2024, 4, 15, 19, 13))  # first quarter
    assert abs(frac - 0.5) < 0.02 and waxing and abs(age - 7.4) < 0.3
    assert not astro.moon_phase(D(2024, 4, 30))[2]


def test_phase_names():
    assert astro.phase_name(0.0, True) == "New Moon" and astro.phase_name(1.0, False) == "Full Moon"
    assert astro.phase_name(0.5, True) == "First Quarter" and astro.phase_name(0.5, False) == "Last Quarter"
    assert astro.phase_name(0.2, True) == "Waxing Crescent" and astro.phase_name(0.8, False) == "Waning Gibbous"
    assert astro.phase_name(0.8, True) == "Waxing Gibbous" and astro.phase_name(0.2, False) == "Waning Crescent"


def test_london_sun_times():
    for day, rise, set_ in ((dt.date(2024, 6, 21), D(2024, 6, 21, 3, 43), D(2024, 6, 21, 20, 21)),
                            (dt.date(2024, 12, 21), D(2024, 12, 21, 8, 4), D(2024, 12, 21, 15, 53))):
        got = astro.sun_times(day, *LONDON)
        assert abs(got[0] - rise) < dt.timedelta(minutes=5) and abs(got[1] - set_) < dt.timedelta(minutes=5)


def test_midnight_sun_and_polar_night():
    assert astro.sun_times(dt.date(2024, 6, 21), 69.65, 18.96) is None
    assert astro.sun_times(dt.date(2024, 12, 21), 69.65, 18.96) is None


def test_sun_elevation():
    assert 60 < astro.sun_elevation(D(2024, 6, 21, 12, 2), *LONDON) < 63  # 90 - 51.5 + 23.4
    assert astro.sun_elevation(D(2024, 6, 21, 0), *LONDON) < -10


def frame(style, now, w=64, h=32, **settings):
    ctx = Context(width=w, height=h, rng=random.Random(1))
    return np.asarray(MODULE.create(style, settings, ctx).render(now), int)


def test_moon_differs_between_crescent_and_full():
    assert np.abs(frame("moon", D(2024, 4, 12, 12)) - frame("moon", D(2024, 4, 23, 23, 49))).sum() > 5000


def left_right(img):
    left, right = img[4:24, 2:13].sum(), img[4:24, 13:24].sum()
    return left, right


def test_southern_hemisphere_flips_the_lit_side():
    t = D(2024, 4, 12, 12)  # waxing crescent
    nl, nr = left_right(frame("moon", t))
    sl, sr = left_right(frame("moon", t, latitude=-33.9))
    assert nr > nl and sl > sr


def test_sun_style_differs_between_noon_and_midnight():
    noon, night = frame("sun", D(2024, 6, 21, 12)), frame("sun", D(2024, 6, 21, 23, 30))
    assert noon.mean() > night.mean() + 40


def test_styles_survive_polar_and_naive_times():
    frame("sun", dt.datetime(2024, 6, 21, 12), latitude=69.65, longitude=18.96)
    frame("sun", dt.datetime(2024, 12, 21, 12), latitude=69.65, longitude=18.96)
    assert (frame("moon", dt.datetime(2024, 4, 12, 12)) == frame("moon", D(2024, 4, 12, 12))).all()


def test_all_styles_fit_small_canvases():
    for style in MODULE.styles:
        for w, h in ((32, 16), (40, 18), (58, 28)):
            assert frame(style, D(2024, 4, 12, 12), w, h).shape == (h, w, 3)


def test_sun_day_length_font_is_a_setting_and_bigger_by_default():
    import random
    from bongo.module import Context
    now = dt.datetime(2026, 11, 11, 15, 40, tzinfo=dt.timezone.utc)
    draw = lambda **s: MODULE.create("sun", s, Context(rng=random.Random(1))).render(now).tobytes()
    assert MODULE.settings_for("sun")["day_length_font"] == "6x10"
    assert draw() != draw(day_length_font="tom-thumb")
    assert draw(day_length_font="5x7") not in (draw(), draw(day_length_font="tom-thumb"))
