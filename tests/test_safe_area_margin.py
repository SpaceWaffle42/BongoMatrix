"""0.3.84: the Screen Safe Area margin is always black (owner decision 6, 2026-10-31).

The ring outside the safe area behaves as if the panel were physically smaller:
it is never lit. Module fills, backgrounds and borders paint only the inner
canvas. One shared layer enforces this on every surface:

* the physical Matrix (fake hardware, ``tests.test_safe_area.physical_matrix``),
* the software mirror the dashboard live view and device preview publish
  (``Matrix.latest_frame_rgb``), and
* the edit preview (``webapp._edit_preview_iterator``: ``CaptureMatrix`` and
  ``SafeArea.compose``).

Full-screen media with ``media_inside`` off has no viewport and may still bleed
under the frame; that path is unchanged and out of scope here.
"""
import base64
import copy
import hashlib
import io
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from config_store import DEFAULT_CONFIG
from safe_area import SafeArea

INSETS = (
    SafeArea(top=2, right=2, bottom=2, left=2),
    SafeArea(top=4, right=4, bottom=4, left=4),
    SafeArea(top=6, right=6, bottom=6, left=6),
    SafeArea(top=1, right=3, bottom=5, left=6),  # uneven: 55x26
)
ZERO = SafeArea()
FRAMES = 12  # frames sampled per surface and inset


# --------------------------------------------------------------------------- fixtures

def _png(colour, size=(64, 32)):
    image = Image.new("RGB", size, colour)
    image.paste((255, 255, 255), (size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _BytesPin:
    def __init__(self, data):
        self.data = data

    def fileno(self):
        return 0

    def dup_file(self):
        return io.BytesIO(self.data)

    def dup_fd(self):
        import os
        descriptor = os.memfd_create("margin-media")
        os.write(descriptor, self.data)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor


def _announcement(**changes):
    item = {
        "id": "announcement_margin", "enabled": True, "name": "Margin",
        "playback_speed_percent": 100.0, "layout": "text_notice",
        "headline": "Welcome", "message": "Front desk closes at 5 PM", "media": None,
        "schedule": {"start_date": "2020-01-01", "end_date": "2099-12-31",
                     "start_time": "00:00", "end_time": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]},
        "style": {"foreground": "#ffffff", "background": "#203040", "headline_font": "5x7",
                  "message_font": "4x6", "alignment": "center", "border": "solid",
                  "border_colour": "#ff0000"},
        "timing": {"mode": "static", "duration_seconds": 2},
        "transition": {"type": "cut"},
    }
    for key, value in changes.items():
        if key == "style":
            item["style"] = dict(item["style"], **value)
        else:
            item[key] = value
    return item


def _text_module(**changes):
    module = {"id": "custom_margin", "name": "Margin", "type": "text", "text": "HELLO SAFE AREA",
              "font": "5x7", "colour": "#ffffff", "background": "#402000", "border": "solid",
              "border_colour": "#00ff00", "x": 0, "y": 18, "direction": "left", "speed": 0.05,
              "loops": 1}
    module.update(changes)
    return module


def _canvas_module():
    return {
        "id": "custom_canvas", "name": "Canvas", "type": "canvas", "background": "#003020",
        "frame_count": 4, "fps": 12, "playback": "loop",
        "layers": [{
            "id": "layer_square", "name": "Square", "type": "rectangle", "visible": True,
            "properties": {"x": -4, "y": -4, "width": 80, "height": 12, "rotation": 0,
                           "colour": "#ffff00", "filled": True},
            "keyframes": [],
        }],
        "overrides": [],
    }


def _media_module():
    return {"id": "custom_media", "name": "Media", "type": "media", "source": "solid.png",
            "fit": "stretch", "x": 0, "y": 0, "width": 64, "height": 32, "loops": 1,
            "playback_speed_percent": 100.0}


def _config(safe_area, **sections):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["device"]["safe_area"] = safe_area.as_dict()
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = dict(config[key], **value)
        else:
            config[key] = value
    return config


def _weather_fixtures():
    from modules.openweather_common import OpenWeatherForecastPeriod, OpenWeatherSnapshot
    from modules.openweather_forecast import PreparedForecast
    from modules.openweather_overview import PreparedOverview
    from modules.weather_app import WeatherSnapshot
    weather = WeatherSnapshot(location="Hove", status="Clear", temperature=16, feels_like=15,
                              high=18, low=12, humidity=70)
    current = OpenWeatherSnapshot(location="Hove", status="Cloudy", temperature=17, feels_like=16,
                                  high=19, low=13, humidity=68, icon_code="04d")
    periods = (OpenWeatherForecastPeriod(label="Tue 15:00", status="Rain", temperature=18,
                                         feels_like=17, high=20, low=15, icon_code="10d"),)
    icon = Image.new("RGB", (20, 20), "blue")
    return (weather, PreparedOverview(weather=current, icon=icon),
            PreparedForecast(periods=periods, icons={"10d": icon}))


class Case:
    """One module configuration rendered on every surface."""

    def __init__(self, label, module, sections=None, *, prepared=None, physical=None,
                 preview=True, pins=None, deterministic=True):
        self.label, self.module = label, module
        self.sections = sections or {}
        self.prepared = prepared
        self.physical = physical  # callable(matrix, config, stop) -> runs the module
        self.preview = preview
        self.pins = pins or {}
        self.deterministic = deterministic

    def config(self, safe_area):
        return _config(safe_area, **self.sections)


def cases():
    from modules import (announcement_board, astronomy_display, clock_lava, custom_text,
                         event_countdown, openweather_forecast, openweather_overview, rss,
                         sleep_screen, splash_screen, weather_app)
    from tests.test_safe_area_rss_lunar import (_fast_run, _lunar_prepared, _rss_prepared,
                                                lunar_cases, rss_cases)
    weather, overview, forecast = _weather_fixtures()
    solid = _BytesPin(_png((200, 40, 40)))
    result = []

    rss_story = rss_cases()["text-once-blue"]
    rss_prepared = _rss_prepared(rss_story)

    def run_rss(matrix, config, stop, prepared=rss_prepared):
        with _fast_run(rss, True):
            rss.run(matrix, config, stop_event=stop, prepared=prepared)
    result.append(Case("rss", "rss", prepared=rss_prepared, physical=run_rss))

    for name in ("event", "moon-simple"):
        card = lunar_cases()[name]
        if name == "moon-simple":
            import dataclasses
            card = dataclasses.replace(card, options=dataclasses.replace(card.options, background="#100010"))
        prepared = _lunar_prepared(card)

        def run_lunar(matrix, config, stop, prepared=prepared):
            with _fast_run(astronomy_display, False):
                astronomy_display.run(matrix, config, stop_event=stop, prepared=prepared)
        result.append(Case(f"lunar/{name}", "astronomy", prepared=prepared, physical=run_lunar))

    calendar = copy.deepcopy(DEFAULT_CONFIG["event_countdown"])
    calendar.update(layout="large", timezone="UTC", background="#102030", events=[dict(
        id="launch", title="Launch party tonight", start="2030-01-01T12:00", end=None,
        all_day=False, annual=False, enabled=True, now_seconds=900, accent=None, icon="calendar")])
    result.append(Case(
        "calendar", "event_countdown", {"event_countdown": calendar},
        physical=lambda m, c, s: event_countdown.run(m, c, duration=999, stop_event=s),
        deterministic=False))

    result.append(Case(
        "weather", "weather", {"weather": {"background_colour": "#002040"}}, prepared=weather,
        physical=lambda m, c, s: weather_app.run(
            m, c, duration=999, stop_event=s,
            client=type("Client", (), {"fetch": lambda self, *a, **k: weather})())))
    result.append(Case(
        "openweather/overview", "openweather_overview",
        {"weather": {"location": "Hove"},
         "openweather_overview": {"background_colour": "#301000"}}, prepared=overview,
        physical=lambda m, c, s: openweather_overview.run(m, c, 999, s, prepared=overview)))
    result.append(Case(
        "openweather/forecast", "openweather_forecast",
        {"weather": {"location": "Hove"},
         "openweather_forecast": {"background_colour": "#300030"}}, prepared=forecast,
        physical=lambda m, c, s: openweather_forecast.run(m, c, 999, s, prepared=forecast)))

    announcement_variants = [
        ("text/solid/cut", _announcement()),
        ("text/inset/fade", _announcement(style={"border": "inset"},
                                          transition={"type": "fade", "duration_seconds": 0.4})),
        ("text/none/dissolve", _announcement(style={"border": "none"},
                                             transition={"type": "pixel_dissolve", "duration_seconds": 0.4})),
        ("text/solid/slide", _announcement(transition={"type": "slide", "direction": "left",
                                                       "duration_seconds": 0.4})),
        ("text/solid/wipe", _announcement(transition={"type": "wipe", "direction": "right",
                                                      "duration_seconds": 0.4})),
        ("scroll/none", _announcement(style={"border": "none"},
                                      timing={"mode": "scroll", "loops": 1, "frame_delay": 0.05})),
        ("icon/solid", _announcement(layout="icon_notice", media="solid.png")),
        ("banner/inset", _announcement(layout="image_banner", media="solid.png", style={"border": "inset"})),
        ("full-image/solid", _announcement(layout="full_image_notice", media="solid.png")),
    ]
    for name, item in announcement_variants:
        pins = {"solid.png": solid} if item["media"] else {}

        def run_announcement(matrix, config, stop, item=item, pins=pins):
            announcement_board.run_preview(matrix, item, stop, pinned_handles=pins)
        result.append(Case(f"announcement/{name}", "announcement_board",
                           {"announcement_board": {"announcements": [item]}},
                           physical=run_announcement, pins=pins))

    for name, module in (("text", _text_module()),
                         ("text-dotted", _text_module(border="dotted", x=-6)),
                         ("scroll", _text_module(type="text_scroll", border="none")),
                         ("canvas", _canvas_module()),
                         ("media", _media_module())):
        pins = {"solid.png": solid} if module["type"] == "media" else {}

        def run_custom(matrix, config, stop, module=module, pins=pins):
            with patch.object(custom_text, "_wait", lambda s, d: s.wait(d)):
                custom_text.run(matrix, config, 999, stop, module_config=module, pinned_handles=pins)
        result.append(Case(f"custom/{name}", module["id"], {"custom_modules": [module]},
                           physical=run_custom, pins=pins))

    result.append(Case(
        "lava", "clock_lava",
        physical=lambda m, c, s: clock_lava.run(m, c, duration=999, stop_event=s),
        deterministic=False))

    for name, module in (("splash", splash_screen), ("sleep", sleep_screen)):
        def run_screen(matrix, config, stop, module=module, name=name):
            module.run(matrix, dict(config, media={name: "solid.png"},
                                    media_layout={name: {"x": 0, "y": 0, "width": 64, "height": 32,
                                                         "keep_aspect": False}}),
                       duration=999, stop_event=stop, pinned_handle=solid)
        result.append(Case(name, name, physical=run_screen, pins={"solid.png": solid}))

    result.append(Case("pairing-pin", "pairing", physical=_run_pairing, preview=False))
    result.append(Case("address-notice", "address", physical=_run_address, preview=False))
    return result


def _run_pairing(matrix, config, stop):
    import tempfile
    from pathlib import Path

    from config_store import ConfigStore
    from display_controller import DisplayController
    with tempfile.TemporaryDirectory() as directory:
        store = ConfigStore(Path(directory) / "config.json")
        store.update({"device": {"safe_area": config["device"]["safe_area"]}}, base_revision=0)
        controller = DisplayController(matrix, store, ip_address_provider=lambda: "192.168.1.4")
        try:
            controller.show_pairing_pin("123456", ttl_seconds=60, rotate=False)
            controller._draw_pairing_page(show_ip=True)
        finally:
            controller.end_pairing()


def _run_address(matrix, config, stop):
    import tempfile
    from pathlib import Path

    from config_store import ConfigStore
    from display_controller import DisplayController, apply_safe_area
    with tempfile.TemporaryDirectory() as directory:
        store = ConfigStore(Path(directory) / "config.json")
        controller = DisplayController(matrix, store, ip_address_provider=lambda: "192.168.1.4")
        apply_safe_area(matrix, config)
        controller._draw_address_notice(("online", "192.168.1.44"), stop)


# --------------------------------------------------------------------------- surfaces

class _StopAfter:
    """stop_event that never sleeps and stops once ``limit`` frames were swapped."""

    def __init__(self, counter, limit):
        self.counter, self.limit = counter, limit

    def is_set(self):
        return len(self.counter) >= self.limit

    def wait(self, _seconds=None):
        return self.is_set()

    def set(self):
        self.counter.extend([None] * self.limit)


def physical_frames(case, safe_area, limit=FRAMES):
    """(hardware, mirror) frame pairs from the physical Matrix on fake hardware."""
    from display_controller import safe_area_scope
    from tests.test_safe_area import physical_matrix
    matrix, patcher = physical_matrix()
    try:
        hardware = matrix._hardware
        frames = []

        def swap_on_vsync(canvas):
            frames.append(canvas.image.copy())
            return type(canvas)()
        hardware.SwapOnVSync = swap_on_vsync
        mirrors = []
        matrix.set_physical_swap_observer(
            lambda raw, revision: mirrors.append(Image.frombytes("RGB", (64, 32), raw)))
        stop = _StopAfter(frames, limit)
        config = case.config(safe_area)
        with patch("time.sleep", lambda _s: None):
            if case.module in {"pairing", "address"}:
                case.physical(matrix, config, stop)
            else:
                with safe_area_scope(matrix, config, case.module):
                    case.physical(matrix, config, stop)
        return list(zip(frames, mirrors))[:limit]
    finally:
        patcher.stop()


def preview_frames(case, safe_area, limit=FRAMES):
    """Frames of the real /api/edit-preview iterator (splash/sleep: its media seam)."""
    import webapp
    from edit_preview import CaptureMatrix, media_preview_frames
    config = case.config(safe_area)
    stop = threading.Event()
    if case.module in {"splash", "sleep"}:
        capture = CaptureMatrix(safe_area=safe_area, media=True)
        iterator = media_preview_frames(
            capture, "solid.png", 1, {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": False},
            pinned_handle=case.pins["solid.png"])
    else:
        iterator = webapp._edit_preview_iterator(case.module, config, None, dict(case.pins),
                                                 case.prepared, stop)
    frames = []
    for frame in iterator:
        frames.append(Image.open(io.BytesIO(base64.b64decode(frame["png"]))).convert("RGB"))
        if len(frames) >= limit:
            stop.set()
            break
    return frames


def surfaces(case, safe_area, limit=FRAMES):
    """{surface name: [frames]} for every surface this case has."""
    pairs = physical_frames(case, safe_area, limit)
    result = {"physical": [hw for hw, _ in pairs], "mirror": [mirror for _, mirror in pairs]}
    if case.preview:
        result["edit-preview"] = preview_frames(case, safe_area, limit)
    return result


def margin_points(safe_area, width=64, height=32):
    return [(x, y) for y in range(height) for x in range(width)
            if not (safe_area.left <= x < width - safe_area.right
                    and safe_area.top <= y < height - safe_area.bottom)]


def lit_margin(frame, safe_area):
    """Number of margin pixels that are not (0, 0, 0)."""
    return sum(1 for point in margin_points(safe_area) if frame.getpixel(point) != (0, 0, 0))


def inner_crop(frame, safe_area):
    return frame.crop((safe_area.left, safe_area.top, 64 - safe_area.right, 32 - safe_area.bottom))


def digest(frames):
    value = hashlib.sha256()
    for frame in frames:
        value.update(frame.tobytes())
    return value.hexdigest()


def _inset_key(safe_area):
    return f"{safe_area.top}{safe_area.right}{safe_area.bottom}{safe_area.left}"


_RENDERED = {}


def rendered(case, safe_area):
    """surfaces() once per process for each (case, inset); the frames are read-only."""
    key = (case.label, safe_area)
    if key not in _RENDERED:
        # A loaded CI shard can exceed the 5 s announcement decode budget, which
        # swaps the picture for "Media unavailable" and changes the digest.
        from modules import announcement_board
        with patch.object(announcement_board, "ANNOUNCEMENT_MEDIA_DECODE_TIMEOUT", 60):
            _RENDERED[key] = surfaces(case, safe_area)
    return _RENDERED[key]


def combined_digest(case, safe_areas, crop):
    """One sha256 over every surface/inset (keys + per-key frame digests), as pinned below."""
    entries = {}
    for safe_area in safe_areas:
        for surface, frames in rendered(case, safe_area).items():
            key = f"{case.label}|{_inset_key(safe_area)}|{surface}"
            entries[key] = digest(inner_crop(f, safe_area) if crop else f for f in frames)
    value = hashlib.sha256()
    for key in sorted(entries):
        value.update(key.encode())
        value.update(entries[key].encode())
    return value.hexdigest()


# Rendered by origin/main ba6725d (0.3.83-preview) on Pillow 13.0.0 (CI/prod) with this file's fixtures:
# (0 px full frames on every surface, inner pixels at 2/4/6/uneven on every surface).
# The black margin must leave both unchanged. Calendar (real-time countdown) and
# Lava (random blobs, wall clock) are not deterministic and are checked for
# surface agreement instead.
MAIN_DIGESTS = {
    'address-notice': ('ecd5a684e12622ece098f6080eab0df780d08cc08d16647aeb44034476e636f1',
                       'f76caa0cd4a43ab0a8ece82d414436b6bd6b81399661cc6d2e253fa8b742c85f'),
    'announcement/banner/inset': ('d43559ec124e1586ed9b44366a6b7ad936ae413fb494aa4e5b001f73cf8d5f6b',
                                  '6b2616478b9d28bd1ee9ffcbfc603fdf34a3ea66a400f42c8f3aa03176378e41'),
    'announcement/full-image/solid': ('0d95dac918ce123c2da22cdaffb494e583c75b43d29979252c140408fe71d312',
                                      'c0ed57a8be2509b96a0f36e0213c1dac54d219d8978bc5caac9fa9b6e19b18cc'),
    'announcement/icon/solid': ('b6a5161009193f7edf44f90bea9c581d47d9a13e1d48d42022af7a40cf19a567',
                                'ee0ddd930a06ea4ddd6bf3be6cd8f6226431fa456cb6e1775c9e1cafbc2769be'),
    'announcement/scroll/none': ('df1a4088a64b889161cbb856f6b508425d50d9dc16e3b86438ea11433f12d4d1',
                                 '40c386997ea59f460931d901af364ddd4626202d7bcb12c12a700f0cb20596a7'),
    'announcement/text/inset/fade': ('b19875401fa10939e16916594cc2779d5297e64a6e1de8f7fdfa0588aba00ff5',
                                     'b6d402d60e62904597be00a0fc550b6d2ca4e40c1d0aa55f13e28637e76430bc'),
    'announcement/text/none/dissolve': ('7de66b55d0061f988e144ad0a9a5dc2ddcc2d3b38d764feca832b7854d893942',
                                        'c7dd44d4d628337c40c46a86726479f12974c5c467a6e394f69143533fd5e196'),
    'announcement/text/solid/cut': ('700782334cd8a292ed23139c30d8e8bdc352d67e1eaf20af9acf3a6045baa20d',
                                    '183ad77749bb1ccdff899ba42400dd02277b1a23c3fa5151c6454dc7130e97f4'),
    'announcement/text/solid/slide': ('ee9f038937db79c32294d32e73ebad4a4783653dd1608d436069761d79a185f2',
                                      '5eb77f2410c4be73912809f63609222b350cb6c63bdc17507cc439be2da93357'),
    'announcement/text/solid/wipe': ('809059aac2aa317cfdee85088e396eb388a7b8387050c3ef1073f3933e342ae8',
                                     '5a0aa4eaa0f566528af97844d44f76aa5b8b38fc96057d863543226ab9090f82'),
    'custom/canvas': ('f94153902fcc8809e2ad48fc0b6cc782cf3ca2748e75fe9e9bd80ba7bf1c86c8',
                      'fa74df6b2062a63151c723d3e1db21ca07efaf752fe23e41e13356729f2b5325'),
    'custom/media': ('bd87197c7c880efa31e0ad7134393f76e6f039ee1d7ea3d3decac64553847be0',
                     '80cc3420f5017f0be78a8056af75c4950f603c132c29ccc94c70374d2c1fc671'),
    'custom/scroll': ('6182934f8967b75833c2cad6a0788d774a9c75c164082754d0f879b945bb6d10',
                      '48940c68f6b0ea58b4bd0b77118c640fc2fd66c99c6d464d8634a8883fa4efdf'),
    'custom/text': ('513954d5efcc24fca5aadcbeed5c7a12a165fa46f9e3d6a5a0d0ae5859a55cb3',
                    '6883e94d09df9aafba960b9e324276f49a6ba7b49e66be4f5a799bceb4f7c8df'),
    'custom/text-dotted': ('49e1f9e9c39b9515b912909fb3de5f557cc267fdbb7eb7ea777972f70a7d2c42',
                           '4c1dfdcd610d0b719d96f320fabfef005c33c599c2affb8d86b19020739c6560'),
    'lunar/event': ('bd964bb210e3512d142191baa38623c79afffdb60164f8c74335e50dc3d09719',
                    '256c7de87bf066968b60c9c643ebbee676cec593cf2c4e639064ac31968daee3'),
    'lunar/moon-simple': ('6a19b30b9ed55fe77af3254f167c7560248aea8722bf952a07031c4582be917f',
                          'ca2694e6c25df81fd58ab14c0bf782c47c3e08703db8d3dac308625b638b375f'),
    'openweather/forecast': ('2479db113d27d80acd674da26c05a8a158a74c38a2e1374d0efe89c9cb22d007',
                             '2ae2970bf504de58d28951fda51ab81c8f7102cf1285d148d66ddefc72941704'),
    'openweather/overview': ('72de5c80b5a2816070527f4a5ef9dbcd42c420984049c57c0ad9eac18c581d31',
                             '3ac9a524800c803cb873795e6b71d561401d5004f8ed0aae12e5a35be1152ab7'),
    'pairing-pin': ('b7d837f4310e13fb36066693f5332d26f864802f00dc287e6a55d5d7ed388691',
                    '61348fcd7049351158c37e1a2c7b9cd23ae27cd57b8146b13b4ef39c818162bf'),
    'rss': ('92aebb3cd650334a180070b2927d950689acf4f7caaa50e79e59475637797f01',
            '64405088cdef716a35fff54cd0a26fdba54d8a719e1257f8813fc385a9efc5a0'),
    'sleep': ('8d6e1463d82258ec6e286684f797ca4920a31405cb5cb436d92f2dae95c63429',
              '2b591469b59b9f6e54f5f379c0ec9f39e7a3d2c1e6ab32e836a23f6a5cf50b35'),
    'splash': ('9c36daa82109b9fce1d4606381ec32704f6beb00a087f4b9db2b6ea44200cf7f',
               '599ccade8db218e77e1ea8aa926034711e838d9e9b7e6c28cbca2070bdefd8c6'),
    'weather': ('88bd4d84afc4382b4023e11d5e2ff9a05f71e87b95d6d8a7ff59f2fc64c3009a',
                '7f94bd1f8c44cd2bebf864e5e397d40d2b2c089dc13f9656553f89866d4f128b'),
}


# --------------------------------------------------------------------------- tests

class ModuleMarginTests(unittest.TestCase):
    """Every module with a background, every surface, insets 2/4/6/uneven."""

    @classmethod
    def setUpClass(cls):
        cls.cases = cases()

    def test_margin_is_black_on_every_surface(self):
        for case in self.cases:
            for safe_area in INSETS:
                for surface, frames in rendered(case, safe_area).items():
                    with self.subTest(case=case.label, inset=_inset_key(safe_area), surface=surface):
                        self.assertTrue(frames, "the surface rendered at least one frame")
                        lit = [lit_margin(frame, safe_area) for frame in frames]
                        self.assertEqual(max(lit), 0, f"lit margin pixels per frame: {lit}")

    def test_inner_pixels_are_unchanged_from_main(self):
        for case in self.cases:
            with self.subTest(case=case.label):
                if case.deterministic:
                    self.assertEqual(combined_digest(case, INSETS, crop=True), MAIN_DIGESTS[case.label][1])
                    continue
                for safe_area in INSETS:
                    by_surface = rendered(case, safe_area)
                    for hardware, mirror in zip(by_surface["physical"], by_surface["mirror"]):
                        self.assertEqual(hardware.tobytes(), mirror.tobytes())

    def test_zero_safe_area_is_byte_identical_to_main(self):
        for case in self.cases:
            if case.deterministic:
                with self.subTest(case=case.label):
                    self.assertEqual(combined_digest(case, (ZERO,), crop=False), MAIN_DIGESTS[case.label][0])

    def test_fixtures_really_paint_a_non_black_background(self):
        """Guard the fixtures: the dominant inner colour is not black (fades may start dark)."""
        quiet = {"lava", "splash", "sleep", "custom/media", "pairing-pin", "address-notice"}
        for case in self.cases:
            if case.label in quiet:
                continue
            with self.subTest(case=case.label):
                safe_area = INSETS[0]
                dominant = []
                for frame in rendered(case, safe_area)["physical"]:
                    colours = {colour: count for count, colour in inner_crop(frame, safe_area).getcolors(4096)}
                    dominant.append(max(colours, key=colours.get))
                self.assertTrue(any(colour != (0, 0, 0) for colour in dominant), dominant)


class SharedLayerTests(unittest.TestCase):
    """The single choke points: Matrix swap, CaptureMatrix snapshot, SafeArea.compose."""

    def test_compose_keeps_its_signature_but_never_paints_the_margin(self):
        inner = Image.new("RGB", (55, 26), (9, 8, 7))
        area = INSETS[3]
        panel = area.compose(inner, "#102030")
        self.assertEqual(lit_margin(panel, area), 0)
        self.assertEqual(inner_crop(panel, area).tobytes(), inner.tobytes())
        full = Image.new("RGB", (64, 32), (1, 2, 3))
        self.assertEqual(ZERO.compose(full, "#ffffff").tobytes(), full.tobytes(), "0 px is untouched")

    def test_capture_matrix_snapshot_has_a_black_margin_after_fill(self):
        from edit_preview import CaptureMatrix
        for area in INSETS:
            with self.subTest(area=area):
                capture = CaptureMatrix(safe_area=area)
                capture.fill(0, 50, 0)
                frame = capture.snapshot()
                self.assertEqual(lit_margin(frame, area), 0)
                self.assertEqual(set(inner_crop(frame, area).getdata()), {(0, 50, 0)})
        capture = CaptureMatrix()
        capture.fill(0, 50, 0)
        self.assertEqual(set(capture.snapshot().getdata()), {(0, 50, 0)}, "0 px fill covers the panel")

    def test_physical_swap_blacks_the_margin_on_hardware_and_mirror(self):
        from tests.test_safe_area import physical_matrix
        for area in INSETS:
            with self.subTest(area=area):
                matrix, patcher = physical_matrix()
                self.addCleanup(patcher.stop)
                matrix.set_safe_area(area)
                matrix.fill(0, 0, 90)
                white = matrix.colour("white")
                # Native text spills past the inner edge on hardware; scroll_text bypasses the mirror.
                matrix.draw_text(matrix.font("5x7"), -3, 6, white, "HELLO WORLD WIDE SPILL")
                matrix.swap()
                hardware = matrix._hardware.shown
                mirror = Image.frombytes("RGB", (64, 32), matrix.latest_frame_rgb()[0])
                for frame in (hardware, mirror):
                    self.assertEqual(lit_margin(frame, area), 0)
                    self.assertEqual(inner_crop(frame, area).getpixel((0, inner_crop(frame, area).height - 1)),
                                     (0, 0, 90))
                self.assertEqual(hardware.tobytes(), mirror.tobytes())

    def test_scroll_text_never_lights_the_margin_on_hardware(self):
        from tests.test_safe_area import physical_matrix
        area = INSETS[1]
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        shown = []

        def swap_on_vsync(canvas):
            shown.append(canvas.image.copy())
            return type(canvas)()
        matrix._hardware.SwapOnVSync = swap_on_vsync
        matrix.set_safe_area(area)
        with patch("time.sleep", lambda _s: None):
            matrix.scroll_text("AB", matrix.font("6x10"), 20, matrix.colour("white"), loops=1)
        self.assertGreater(len(shown), 10)
        self.assertEqual(max(lit_margin(frame, area) for frame in shown), 0)
        self.assertTrue(any(frame.getbbox() for frame in shown), "the text was drawn")

    def test_marker_still_pulses_on_the_inner_edge_with_a_black_margin(self):
        from safe_area import marker_points
        from tests.test_safe_area import physical_matrix
        area = INSETS[3]
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        matrix.set_safe_area(area)
        matrix.set_safe_area_marker(True)
        matrix.fill(0, 0, 90)
        matrix.swap()
        frame = matrix._hardware.shown
        self.assertEqual(lit_margin(frame, area), 0)
        viewport = area.viewport()
        points = set(marker_points(viewport, phase=0)) | set(marker_points(viewport, phase=1))
        marked = [point for point in points if frame.getpixel(point) != (0, 0, 90)]
        self.assertGreater(len(marked), 0)
        self.assertTrue(all(frame.getpixel(p)[0] > 100 and frame.getpixel(p)[2] == 0 for p in marked))

    def test_full_screen_media_may_still_bleed_when_media_inside_is_off(self):
        from edit_preview import CaptureMatrix
        from tests.test_safe_area import physical_matrix
        outside = SafeArea(top=4, right=4, bottom=4, left=4, media_inside=False)
        red = Image.new("RGB", (64, 32), (255, 0, 0))
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        with matrix.safe_area_scope(outside, media=True):
            matrix.clear()
            matrix.draw_image(red, fit="stretch")
            matrix.swap()
        self.assertEqual(set(matrix._hardware.shown.getdata()), {(255, 0, 0)})
        capture = CaptureMatrix(safe_area=outside, media=True)
        capture.draw_image(red, fit="stretch")
        self.assertEqual(set(capture.snapshot().getdata()), {(255, 0, 0)})


if __name__ == "__main__":
    unittest.main()
