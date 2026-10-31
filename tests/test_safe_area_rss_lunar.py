"""Hotfix 0.3.83: RSS and Lunar lay out on the real inner canvas (owner 2026-10-31).

Owner rule: the Screen Safe Area acts as if the panel were (64-left-right) x
(32-top-bottom). A module never resizes its own frame: no squeeze, no font
step-down, no smaller thumbnail or moon. Text wraps at the inner width, rows use
the inner height and anything that does not fit is clipped like on a real
smaller panel. Scheduled playback (physical Matrix), the edit preview and a
plain inner-size canvas must show the same pixels.
"""
import base64
import dataclasses
import datetime as dt
import hashlib
import io
import threading
import types
import unittest
from unittest.mock import patch

from PIL import Image

from safe_area import SafeArea
from tests.test_safe_area import physical_matrix

INSETS = (
    SafeArea(),
    SafeArea(top=2, right=2, bottom=2, left=2),
    SafeArea(top=4, right=4, bottom=4, left=4),
    SafeArea(top=6, right=6, bottom=6, left=6),
    SafeArea(top=1, right=3, bottom=5, left=6),  # uneven: 55x26
)

# sha256 over every 0 px frame (image bytes + delay) rendered by origin/main
# 91f21e6 (0.3.82-preview) for the fixtures below. 0 px must stay byte-identical.
MAIN_0PX_DIGESTS = {
    'lunar/event': 'dcd6a3ba75a97be4e27b1ac50e7fad1b212f91934e4ba2e686ff4365fc982917',
    'lunar/moon-advanced': 'edbf754e9ca8728c79d6aeb48f2f2d1c5f753d56175c71a803bdfce9ca93f6b4',
    'lunar/moon-simple': '08bbcf6a22c3d6a788d8c8d342770a5b56f0f3b828ed905312e67954dc70079a',
    'rss/banner-continuous': '7519a495cc8d67ba050ba0997b886f2acb97fbeaa82d401e73fa81b694ddc5ac',
    'rss/banner-once': 'c7a673e44152c41887d778579ac0d2ba755b5e668aed317d32a071307cb09e86',
    'rss/text-continuous': 'd7a3c579365ae0fd17fa2cf819f223aafac9d998f71e2bf70c0638de11c8958c',
    'rss/text-once': '03dcaf4b119722e57a6bd2ff031c7f3aeba42db6a89f0217990cae9a37e209e7',
    'rss/text-once-blue': 'b33ffc50a87a820d10a82a324e9e7957510eeee153c027d4ad1516958b06edbd',
    'rss/thumbnail-continuous': 'bd8b2f1248f05f1220d305bd87bb81444f0ca5c9ad2a8b6c005c4fed99348caf',
    'rss/thumbnail-once': '5cb4903edb3e6f0a20da97c87056002dbdd34b8b50c8e4d573c468cd03b7e822',
}


def _config(safe_area):
    return {'device': {'safe_area': safe_area.as_dict()}, 'custom_modules': []}


def rss_cases():
    from modules import rss_renderer as r
    from rss_config import RenderOptions
    from rss_data import Story
    story = Story(('k',), 'Safe zone headline scrolls',
                  'The quick brown fox jumps over the lazy dog while the frame hides edges.', '', None)
    gradient = bytes(v for y in range(16) for x in range(16) for v in (x * 16, y * 16, 128))
    thumbnail = r.PreparedThumbnail(frames=(gradient,), durations_ms=(100,))
    banner = r.PreparedBanner(20, 7, bytes((200, 20, 20)) * 140)
    cases = {}
    for mode in ('once', 'continuous'):
        cases[f'text-{mode}'] = r.prepare_story(story, RenderOptions(layout='text', headline_mode=mode))
        cases[f'thumbnail-{mode}'] = r.prepare_story(
            story, RenderOptions(layout='auto', headline_mode=mode), thumbnail=thumbnail)
        cases[f'banner-{mode}'] = r.prepare_story(
            story, RenderOptions(layout='auto', headline_mode=mode), banner=banner)
    # A non-black background proves the margin matches on every surface.
    cases['text-once-blue'] = r.prepare_story(
        story, RenderOptions(layout='text', headline_mode='once', background_colour='#000040'))
    return cases


NOW = dt.datetime(2024, 10, 27, 0, 30, tzinfo=dt.timezone.utc)


def lunar_cases():
    from astronomy import Astronomy, AstronomyEvent, Location, VisibilityWindow
    from modules import astronomy_renderer as r
    location = Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
    with Astronomy() as engine:
        moon = engine.moon(dt.datetime(2024, 4, 15, 12, tzinfo=dt.timezone.utc), location)
    # Window start sits mid-hour so the "2d 3h" label never changes during playback.
    start = NOW + dt.timedelta(days=2, hours=3, minutes=30)
    event = AstronomyEvent('test:full', 'full_moon', 'Full Moon', start, location,
                           (VisibilityWindow(start, start + dt.timedelta(hours=1)),), 'fixture')
    return {
        'moon-simple': r.prepare_moon(moon, r.MoonOptions(view='simple', hold_seconds=1)),
        'moon-advanced': r.prepare_moon(moon, r.MoonOptions(view='advanced', hold_seconds=1)),
        'event': r.prepare_event(event, NOW, r.EventOptions(hold_seconds=1, background='#100010')),
    }


def _digest(frames):
    digest = hashlib.sha256()
    for image, delay in frames:
        digest.update(image.tobytes())
        digest.update(repr(round(delay, 6)).encode())
    return digest.hexdigest()


def main_0px_digests():
    """Renderer-level 0 px frames (used to pin MAIN_0PX_DIGESTS from origin/main)."""
    from modules import astronomy_renderer, rss_renderer
    result = {}
    for name, story in rss_cases().items():
        result['rss/' + name] = _digest((i, d) for i, d, _ in rss_renderer.image_frames((story,)))
    for name, card in lunar_cases().items():
        result['lunar/' + name] = _digest(
            (i, d) for i, d, _ in astronomy_renderer.image_frames((card,), now=NOW))
    return result


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def _decode(frame):
    return Image.open(io.BytesIO(base64.b64decode(frame['png']))).convert('RGB')


def _rss_prepared(story):
    return types.SimpleNamespace(items=(story,), stories=(story,))


def _lunar_prepared(card):
    return types.SimpleNamespace(cards=(card,), prepared_at=NOW, status='ready')


def _fast_run(module, rss):
    """Run a playlist module with no real sleeping (and a fixed UTC clock for Lunar)."""
    from modules import astronomy_renderer, rss_renderer
    clock = _Clock()
    if rss:
        original = rss_renderer.run
        replacement = lambda m, stories, stop=None: original(m, stories, stop, clock=clock, sleep=clock.sleep)
        return patch.object(rss_renderer, 'run', replacement)
    original = astronomy_renderer.run
    replacement = lambda m, cards, stop=None: original(
        m, cards, stop, clock=clock, sleep=clock.sleep, utc_clock=lambda: NOW)
    return patch.object(astronomy_renderer, 'run', replacement)


def scheduled_frames(test, module_name, prepared, safe_area):
    """S4/S3: module.run on the physical Matrix (fake hardware) inside safe_area_scope."""
    from display_controller import safe_area_scope
    from modules import astronomy_display, rss
    matrix, patcher = physical_matrix()
    test.addCleanup(patcher.stop)
    published, drawn, resized = [], [], []
    hardware = matrix._hardware

    def swap_on_vsync(canvas):
        published.append(canvas.image.copy())
        return type(canvas)()
    hardware.SwapOnVSync = swap_on_vsync
    draw_image = matrix.draw_image

    def record_draw(image, *args, **kwargs):
        drawn.append(image.size)
        return draw_image(image, *args, **kwargs)
    original_resize = Image.Image.resize

    def record_resize(image, *args, **kwargs):
        resized.append(image.size)
        return original_resize(image, *args, **kwargs)
    module = rss if module_name == 'rss' else astronomy_display
    with patch.object(matrix, 'draw_image', record_draw), \
            patch.object(Image.Image, 'resize', record_resize), \
            _fast_run(module, module_name == 'rss'), \
            safe_area_scope(matrix, _config(safe_area), module_name):
        inner = (matrix.matrix.width, matrix.matrix.height)
        test.assertTrue(module.run(matrix, _config(safe_area), prepared=prepared))
    return published, drawn, resized, inner


def edit_preview_frames(module_name, prepared, safe_area):
    """S2: the real /api/edit-preview iterator."""
    import webapp
    frames = webapp._edit_preview_iterator(module_name, _config(safe_area), None, {}, prepared,
                                           threading.Event())
    return [(_decode(frame), frame['delay_ms']) for frame in frames]


def inner_canvas_frames(test, module_name, prepared, safe_area, background):
    """A plain CaptureMatrix that really is inner-sized, composed onto the panel."""
    from edit_preview import CaptureMatrix
    from modules import astronomy_display, rss
    width, height = safe_area.inner_size()
    capture = CaptureMatrix(width, height)
    frames = []
    swap = capture.swap

    def record_swap(canvas=None):
        inner = capture.snapshot(canvas)
        panel = Image.new('RGB', (64, 32), background)
        panel.paste(inner, (safe_area.left, safe_area.top))
        frames.append(panel)
        return swap(canvas)
    capture.swap = record_swap
    module = rss if module_name == 'rss' else astronomy_display
    with _fast_run(module, module_name == 'rss'):
        test.assertTrue(module.run(capture, _config(safe_area), prepared=prepared))
    return frames


def _differing(a, b):
    return sum(p != q for p, q in zip(a.getdata(), b.getdata()))


class InnerCanvasParityTests(unittest.TestCase):
    """(b) playback == edit preview, (c) both == a real inner-size canvas; no resize anywhere."""

    def assert_surfaces_agree(self, module_name, label, prepared, background):
        for safe_area in INSETS:
            with self.subTest(case=label, safe_area=safe_area):
                published, drawn, resized, inner = scheduled_frames(self, module_name, prepared, safe_area)
                self.assertEqual(inner, safe_area.inner_size())
                self.assertEqual(set(drawn), {inner}, 'module must hand over an inner-size frame')
                self.assertEqual(resized, [], 'no frame may be resized on the panel')
                preview = edit_preview_frames(module_name, prepared, safe_area)
                inner_frames = inner_canvas_frames(self, module_name, prepared, safe_area, background)
                self.assertEqual(len(published), len(preview))
                self.assertEqual(len(published), len(inner_frames))
                for index, (panel, (shown, _), reference) in enumerate(zip(published, preview, inner_frames)):
                    self.assertEqual(_differing(panel, shown), 0, f'playback vs preview frame {index}')
                    self.assertEqual(_differing(panel, reference), 0, f'playback vs inner canvas frame {index}')

    def test_rss_layouts_match_a_real_inner_canvas(self):
        for name, story in rss_cases().items():
            self.assert_surfaces_agree('rss', name, _rss_prepared(story), story.options.background_colour)

    def test_lunar_views_match_a_real_inner_canvas(self):
        for name, card in lunar_cases().items():
            self.assert_surfaces_agree('astronomy', name, _lunar_prepared(card), card.options.background)


class ZeroSafeAreaTests(unittest.TestCase):
    """(a) 0 px output stays byte-identical to origin/main 91f21e6."""

    def test_zero_safe_area_frames_are_byte_identical_to_main(self):
        self.assertEqual(main_0px_digests(), MAIN_0PX_DIGESTS)


class InnerLayoutTests(unittest.TestCase):
    def test_rss_wraps_and_scrolls_on_the_inner_width_without_shrinking(self):
        from modules import rss_renderer as r
        for name, story in rss_cases().items():
            for safe_area in INSETS:
                size = safe_area.inner_size()
                with self.subTest(case=name, size=size):
                    laid_out = r.layout_for_size(story, size)
                    self.assertTrue(all(line.width <= size[0] for line in laid_out.lines))
                    self.assertGreaterEqual(len(laid_out.lines), len(story.lines))
                    # Same fonts, same pitch, same 16 px thumbnail: nothing shrinks.
                    self.assertEqual((laid_out.headline, laid_out.line_pitch, laid_out.thumbnail, laid_out.banner),
                                     (story.headline, story.line_pitch, story.thumbnail, story.banner))
                    frames = list(r.image_frames((story,), size=size))
                    self.assertEqual({image.size for image, _, _ in frames}, {size})
                    self.assertAlmostEqual(sum(delay for _, delay, _ in frames),
                                           r.duration_seconds((story,), size=size))
                    if story.thumbnail:
                        self.assertEqual(frames[0][0].crop((0, 0, 16, 16)).tobytes(),
                                         Image.frombytes('RGB', (16, 16), story.thumbnail.frames[0]).tobytes())
        story = rss_cases()['text-once']
        self.assertLess(r.duration_seconds((story,)), r.duration_seconds((story,), size=(52, 20)),
                        'a narrower canvas wraps into more lines, so the story runs longer')

    def test_lunar_lines_span_the_inner_width_and_keep_fonts_and_moon(self):
        from modules import astronomy_renderer as r
        for name, card in lunar_cases().items():
            for safe_area in INSETS:
                size = safe_area.inner_size()
                with self.subTest(case=name, size=size):
                    laid_out = r.layout_for_size(card, size)
                    for before, after in zip(card.lines, laid_out.lines, strict=True):
                        self.assertEqual(after[0], before[0], 'same glyph pixels (no font change)')
                        # Visible ink ends inside the inner canvas; longer lines scroll.
                        self.assertLessEqual(after[2] + min(after[1], after[4]), size[0])
                    if isinstance(card, r.PreparedMoon):
                        self.assertEqual(laid_out.sprite.size, card.sprite.size, 'moon is never shrunk')
                    frames = list(r.image_frames((card,), now=NOW, size=size))
                    self.assertEqual({image.size for image, _, _ in frames}, {size})
                    self.assertAlmostEqual(sum(delay for _, delay, _ in frames),
                                           r.duration_seconds((card,), now=NOW, size=size))


class TimingEndpointTests(unittest.TestCase):
    """The editor's duration estimate times the canvas the panel really plays on."""

    def setUp(self):
        from tests.test_webapp import WebAppTests
        self.f = WebAppTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.pairing.verify.return_value = True
        self.f.pair_browser()
        config = self.f.store.get()
        device = dict(config['device'], safe_area=SafeArea(top=6, right=6, bottom=6, left=6).as_dict())
        self.f.store.update({'device': device})

    def test_rss_timing_uses_the_inner_canvas(self):
        from modules.rss_renderer import duration_seconds
        host = self.f.app.extensions['rss_host']
        summary = 'The quick brown fox jumps over the lazy dog while the frame hides edges. ' * 3
        feed = f'<rss><channel><item><guid>one</guid><title>Headline</title><description>{summary}</description></item></channel></rss>'
        with patch.object(host.service.client, 'fetch', return_value=feed.encode()):
            host.service.refresh_due()
        ready = host.service.peek()
        self.assertEqual(len(ready.items), 1)
        config = self.f.store.get()
        timing = self.f.paired_request('post', '/api/scroll-timing/rss', json={
            'base_revision': config['revision'], 'settings': config['rss']})
        self.assertEqual(timing.status_code, 200, timing.get_data(as_text=True))
        self.assertEqual(timing.json['duration_seconds'], duration_seconds(ready.stories, size=(52, 20)))
        self.assertGreater(timing.json['duration_seconds'], ready.duration)

    def test_lunar_timing_uses_the_inner_canvas(self):
        from modules.astronomy_renderer import duration_seconds
        # Refresh in the future so the second request reuses the ready snapshot.
        card = dataclasses.replace(lunar_cases()['moon-simple'],
                                   refresh_at=dt.datetime(2100, 1, 1, tzinfo=dt.timezone.utc))
        host = self.f.app.extensions['astronomy_host']
        config = self.f.store.get()
        payload = {'base_revision': config['revision'], 'settings': config['astronomy']}
        with patch.object(host.draft, '_prepare', return_value=(card,)):
            response = self.f.paired_request('post', '/api/scroll-timing/astronomy', json=payload)
            host.draft.wait(threading.Event())
            response = self.f.paired_request('post', '/api/scroll-timing/astronomy', json=payload)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['ready_count'], 1, response.json)
        snapshot = host.draft.peek()
        self.assertEqual(response.json['duration_seconds'],
                         duration_seconds((card,), now=snapshot.prepared_at, size=(52, 20)))
        self.assertGreater(response.json['duration_seconds'], snapshot.duration)


if __name__ == '__main__':
    unittest.main()
