import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImagePalette

from media_resolver import module_media_references
from modules import custom_text, media_player, sleep_screen, splash_screen
from modules.media_player import gif_media_frames, video_media_frames


class MediaModuleTests(unittest.TestCase):
    def setUp(self):
        self.pinned_handle = Mock()
        patchers = []
        for module in (splash_screen, sleep_screen):
            original = module.run

            def run_with_pin(*args, _original=original, **kwargs):
                kwargs.setdefault("pinned_handle", self.pinned_handle)
                return _original(*args, **kwargs)

            patchers.append(patch.object(module, "run", side_effect=run_with_pin))

        original_custom_run = custom_text.run

        def custom_run_with_pins(*args, **kwargs):
            module = kwargs.get("module_config")
            if module is not None and "pinned_handles" not in kwargs:
                references = module_media_references(
                    {"custom_modules": [module]}, module["id"],
                )
                kwargs["pinned_handles"] = {
                    reference: self.pinned_handle for reference in references
                }
            return original_custom_run(*args, **kwargs)

        patchers.append(patch.object(
            custom_text, "run", side_effect=custom_run_with_pins,
        ))

        original_canvas_frames = custom_text.canvas_timeline_frames

        def canvas_frames_with_pins(matrix, duration, stop_event, module, *args, **kwargs):
            if "pinned_handles" not in kwargs:
                references = module_media_references(
                    {"custom_modules": [module]}, module["id"],
                )
                kwargs["pinned_handles"] = {
                    reference: self.pinned_handle for reference in references
                }
            return original_canvas_frames(
                matrix, duration, stop_event, module, *args, **kwargs,
            )

        patchers.append(patch.object(
            custom_text, "canvas_timeline_frames",
            side_effect=canvas_frames_with_pins,
        ))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_gif_compositing_uses_global_palette_for_background_disposal(self):
        palette = [0, 0, 0, 217, 160, 102] + ([0, 0, 0] * 254)
        source = Mock()
        source.global_palette = ImagePalette.ImagePalette("RGB", palette)
        source.info = {"background": 0}
        source.mode = "RGB"
        source._frame_transparency = None
        source.disposal_method = 2
        source.dispose_extent = (0, 0, 2, 1)
        source.dispose = Image.new("RGB", (2, 1), (217, 160, 102)).im
        observed = []

        def frames(gif):
            yield Image.new("RGB", (2, 1), "white")
            disposal = Image.Image()._new(gif.dispose)
            observed.append(disposal.getpixel((0, 0)))
            yield Image.new("RGB", (2, 1), "white")

        decoded = list(media_player.gif_composited_frames(
            source, image_iterator=frames,
        ))

        self.assertEqual(len(decoded), 2)
        self.assertEqual(observed, [(0, 0, 0)])

    @staticmethod
    def _timed_gif_case(open_seconds, iteration_seconds, copy_seconds):
        class Clock:
            def __init__(self):
                self.elapsed = 0.0

            def advance(self, seconds):
                self.elapsed += seconds

            def __call__(self):
                return self.elapsed

        class Owned:
            def __init__(self, info=None):
                self.info = info or {}
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        clock = Clock()
        gif = Owned()
        source = Owned({"duration": 100})
        candidate = Owned()

        def image_open(_path):
            clock.advance(open_seconds)
            return gif

        def image_iterator(_gif):
            clock.advance(iteration_seconds)
            return iter((source,))

        def frame_copy(_frame):
            clock.advance(copy_seconds)
            return candidate

        frames = list(gif_media_frames(
            "timed.gif", loops=1, max_duration=0.006, monotonic=clock,
            image_open=image_open, image_iterator=image_iterator,
            frame_copy=frame_copy,
        ))
        return frames, clock, gif, candidate

    def test_gif_frame_copy_time_is_charged_to_absolute_duration(self):
        frames, clock, gif, candidate = self._timed_gif_case(
            open_seconds=0.002, iteration_seconds=0.001, copy_seconds=0.002,
        )

        self.assertEqual(len(frames), 1)
        self.assertIs(frames[0][0], candidate)
        self.assertAlmostEqual(frames[0][1], 0.001, places=9)
        self.assertLessEqual(clock.elapsed + frames[0][1], 0.006)
        self.assertEqual(candidate.close_calls, 0)
        self.assertEqual(gif.close_calls, 1)

    def test_gif_frame_copy_exhausting_deadline_closes_candidate_without_yield(self):
        frames, clock, gif, candidate = self._timed_gif_case(0.002, 0.002, 0.003)

        self.assertEqual(frames, [])
        self.assertGreaterEqual(clock.elapsed, 0.006)
        self.assertEqual(candidate.close_calls, 1)
        self.assertEqual(gif.close_calls, 1)

    def test_gif_deadline_cleanup_preserves_candidate_before_source_failure(self):
        class Clock:
            def __init__(self):
                self.elapsed = 0.0

            def advance(self, seconds):
                self.elapsed += seconds

            def __call__(self):
                return self.elapsed

        class Owner:
            def __init__(self, close_error=None, info=None):
                self.close_error = close_error
                self.close_calls = 0
                self.info = info or {}

            def close(self):
                self.close_calls += 1
                if self.close_error is not None:
                    raise self.close_error

        for scenario in ("candidate_only", "source_only", "both"):
            with self.subTest(scenario=scenario):
                clock = Clock()
                source_error = (
                    OSError("deadline-source-close")
                    if scenario in ("source_only", "both") else None
                )
                candidate_error = (
                    OSError("deadline-candidate-close")
                    if scenario in ("candidate_only", "both") else None
                )
                gif = Owner(source_error)
                frame = Owner(info={"duration": 100})
                candidate = Owner(candidate_error)

                def image_open(_path):
                    clock.advance(0.002)
                    return gif

                def image_iterator(_gif):
                    clock.advance(0.002)
                    return iter((frame,))

                def frame_copy(_frame):
                    clock.advance(0.003)
                    return candidate

                expected = candidate_error or source_error
                self.assertIsNotNone(expected)
                with self.assertRaises(OSError) as raised:
                    list(gif_media_frames(
                        "deadline.gif", loops=1, max_duration=0.006,
                        monotonic=clock, image_open=image_open,
                        image_iterator=image_iterator, frame_copy=frame_copy,
                    ))

                self.assertIs(raised.exception, expected)
                self.assertEqual(candidate.close_calls, 1)
                self.assertEqual(gif.close_calls, 1)

        class CleanupBase(BaseException):
            pass

        class GetterOwner:
            def __init__(self, error, info=None):
                self.error = error
                self.info = info or {}
                self.close_lookups = 0

            @property
            def close(self):
                self.close_lookups += 1
                raise self.error

        clock = Clock()
        source_getter_error = CleanupBase("deadline-source-getattr")
        candidate_getter_error = CleanupBase("deadline-candidate-getattr")
        gif = GetterOwner(source_getter_error)
        frame = Owner(info={"duration": 100})
        candidate = GetterOwner(candidate_getter_error)

        def getter_open(_path):
            clock.advance(0.002)
            return gif

        def getter_iterator(_gif):
            clock.advance(0.002)
            return iter((frame,))

        def getter_copy(_frame):
            clock.advance(0.003)
            return candidate

        with self.assertRaises(CleanupBase) as raised:
            list(gif_media_frames(
                "deadline-getter.gif", loops=1, max_duration=0.006,
                monotonic=clock, image_open=getter_open,
                image_iterator=getter_iterator, frame_copy=getter_copy,
            ))
        self.assertIs(raised.exception, candidate_getter_error)
        self.assertEqual(candidate.close_lookups, 1)
        self.assertEqual(gif.close_lookups, 1)

        clock = Clock()
        iterator_primary = RuntimeError("iterator-primary")
        source_getter_error = CleanupBase("source-getattr")
        gif = GetterOwner(source_getter_error)

        def failing_iterator(_gif):
            raise iterator_primary

        with self.assertRaises(RuntimeError) as raised:
            list(gif_media_frames(
                "iterator.gif", loops=1, max_duration=1,
                monotonic=clock, image_open=lambda _path: gif,
                image_iterator=failing_iterator,
            ))
        self.assertIs(raised.exception, iterator_primary)
        self.assertEqual(gif.close_lookups, 1)

    def test_gif_does_not_pull_another_frame_after_hold_consumes_deadline(self):
        class Clock:
            def __init__(self):
                self.elapsed = 0.0

            def advance(self, seconds):
                self.elapsed += seconds

            def __call__(self):
                return self.elapsed

        class Iterator:
            def __init__(self, clock, frame):
                self.clock = clock
                self.frame = frame
                self.next_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                if self.next_calls == 1:
                    self.clock.advance(0.001)
                    return self.frame
                self.clock.advance(0.1)
                raise StopIteration

        clock = Clock()
        gif = Mock()
        frame = Mock(info={"duration": 100})
        candidate = Mock()
        iterator = Iterator(clock, frame)

        def image_open(_path):
            clock.advance(0.002)
            return gif

        def frame_copy(_frame):
            clock.advance(0.002)
            return candidate

        timeline = gif_media_frames(
            "timed.gif", loops=1, max_duration=0.006,
            monotonic=clock, image_open=image_open,
            image_iterator=lambda _gif: iterator, frame_copy=frame_copy,
        )
        yielded, hold = next(timeline)
        clock.advance(hold)

        self.assertIs(yielded, candidate)
        with self.assertRaises(StopIteration):
            next(timeline)
        self.assertEqual(iterator.next_calls, 1)
        self.assertAlmostEqual(clock.elapsed, 0.006)
        gif.close.assert_called_once_with()

    def _assert_gif_blocked_stage_cancels_promptly(self, blocked_stage):
        stop = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        candidate_closed = threading.Event()
        gif = Mock()
        frame = Mock(info={"duration": 100})
        candidate = Mock()
        candidate.close.side_effect = candidate_closed.set
        emitted = []

        class Iterator:
            def __iter__(self):
                return self

            def __next__(self):
                if blocked_stage == "iteration":
                    entered.set()
                    release.wait()
                if getattr(self, "done", False):
                    raise StopIteration
                self.done = True
                return frame

        def frame_copy(_frame):
            if blocked_stage == "copy":
                entered.set()
                release.wait()
            return candidate

        def consume():
            try:
                emitted.extend(gif_media_frames(
                    "blocked.gif", loops=1, stop_event=stop,
                    image_open=lambda _path: gif,
                    image_iterator=lambda _gif: Iterator(),
                    frame_copy=frame_copy,
                ))
            finally:
                returned.set()

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertTrue(entered.wait(0.5))
        stop.set()
        prompt_return = returned.wait(0.2)
        source_closed_before_release = gif.close.called
        release.set()
        self.assertTrue(returned.wait(0.5))
        consumer.join(0.5)

        self.assertTrue(prompt_return)
        self.assertFalse(source_closed_before_release)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(emitted, [])
        self.assertTrue(candidate_closed.wait(0.5))
        candidate.close.assert_called_once_with()
        gif.close.assert_called_once_with()

    def test_gif_cancel_interrupts_blocked_iteration_and_closes_late_candidate(self):
        self._assert_gif_blocked_stage_cancels_promptly("iteration")

    def test_gif_cancel_interrupts_blocked_copy_and_closes_late_candidate(self):
        self._assert_gif_blocked_stage_cancels_promptly("copy")

    def test_gif_deadline_interrupts_blocked_iteration_and_defers_cleanup(self):
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        candidate_closed = threading.Event()
        gif = Mock()
        frame = Mock(info={"duration": 100})
        candidate = Mock()
        candidate.close.side_effect = candidate_closed.set
        emitted = []

        class Iterator:
            def __iter__(self):
                return self

            def __next__(self):
                entered.set()
                release.wait()
                if getattr(self, "done", False):
                    raise StopIteration
                self.done = True
                return frame

        def consume():
            try:
                emitted.extend(gif_media_frames(
                    "blocked.gif", loops=1, max_duration=0.05,
                    monotonic=time.monotonic,
                    image_open=lambda _path: gif,
                    image_iterator=lambda _gif: Iterator(),
                    frame_copy=lambda _frame: candidate,
                ))
            finally:
                returned.set()

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertTrue(entered.wait(0.5))
        self.assertTrue(returned.wait(0.3))
        source_closed_before_release = gif.close.called
        release.set()
        consumer.join(0.5)

        self.assertFalse(source_closed_before_release)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(emitted, [])
        self.assertTrue(candidate_closed.wait(0.5))
        candidate.close.assert_called_once_with()
        gif.close.assert_called_once_with()

    def test_pre_cancelled_video_frames_do_not_construct_decoder(self):
        decoder = Mock(return_value=iter(()))
        stop = threading.Event()
        stop.set()

        self.assertEqual(list(video_media_frames(
            decoder, stop_event=stop,
        )), [])
        decoder.assert_not_called()

    def test_splash_uses_configured_gif(self):
        matrix = Mock()
        matrix.show_gif.return_value = True
        result = splash_screen.run(matrix, {"media": {"splash": "squares_tb.gif"}}, duration=1)
        self.assertTrue(result)
        matrix.show_gif.assert_called_once_with(
            "squares_tb.gif", loops=0, stop_event=None, max_duration=1,
            pinned_handle=self.pinned_handle,
        )

    def test_sleep_uses_configured_gif(self):
        matrix = Mock()
        matrix.show_gif.return_value = True
        stop = threading.Event()
        result = sleep_screen.run(matrix, {"media": {"sleep": "sleep.gif"}}, duration=2, stop_event=stop)
        self.assertTrue(result)
        matrix.show_gif.assert_called_once_with(
            "sleep.gif", loops=0, stop_event=stop, max_duration=2,
            pinned_handle=self.pinned_handle,
        )

    def test_sleep_gif_speed_percentage_is_forwarded_as_playback_rate(self):
        matrix = Mock()
        matrix.show_gif.return_value = True
        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": True, "video_fps": 20, "playback_speed_percent": 250.0,
        }

        result = sleep_screen.run(
            matrix,
            {"media": {"sleep": "sleep.gif"}, "media_layout": {"sleep": settings}},
            duration=2,
        )

        self.assertTrue(result)
        matrix.show_gif.assert_called_once_with(
            "sleep.gif", x=0, y=0, width=64, height=32, keep_aspect=True,
            playback_rate=2.5, loops=0, stop_event=None, max_duration=2,
            pinned_handle=self.pinned_handle,
        )

    def test_mp4_uses_interruptible_video_playback(self):
        matrix = Mock()
        matrix.show_video.return_value = True
        stop = threading.Event()

        result = splash_screen.run(
            matrix,
            {"media": {"splash": "custom.mp4"}},
            duration=8,
            stop_event=stop,
        )

        self.assertTrue(result)
        matrix.show_video.assert_called_once_with(
            "custom.mp4", loops=0, stop_event=stop, max_duration=8,
            pinned_handle=self.pinned_handle,
        )

    def test_media_layout_controls_position_size_aspect_and_video_rate(self):
        matrix = Mock()
        matrix.show_video.return_value = True
        stop = threading.Event()
        config = {
            "media": {"splash": "custom.mp4"},
            "media_layout": {"splash": {
                "x": 2, "y": 1, "width": 60, "height": 30,
                "keep_aspect": False, "video_fps": 24,
            }},
        }

        result = splash_screen.run(matrix, config, duration=8, stop_event=stop)

        self.assertTrue(result)
        matrix.show_video.assert_called_once_with(
            "custom.mp4", x=2, y=1, width=60, height=30,
            keep_aspect=False, fps_override=24, loops=0,
            stop_event=stop, max_duration=8,
            pinned_handle=self.pinned_handle,
        )

    def test_pre_cancelled_static_uploaded_image_is_not_published(self):
        matrix = Mock()
        matrix.show_image.return_value = True
        stop = threading.Event()
        stop.set()
        result = splash_screen.run(
            matrix,
            {"media": {"splash": "custom.png"}},
            duration=8,
            stop_event=stop,
        )
        self.assertFalse(result)
        matrix.show_image.assert_not_called()
        matrix.show_gif.assert_not_called()

    def test_pre_cancelled_static_sleep_image_is_not_published(self):
        matrix = Mock()
        matrix.show_image.return_value = True
        stop = threading.Event()
        stop.set()
        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": True, "video_fps": 20, "playback_speed_percent": 400.0,
        }

        result = sleep_screen.run(
            matrix,
            {"media": {"sleep": "custom.png"}, "media_layout": {"sleep": settings}},
            duration=8,
            stop_event=stop,
        )

        self.assertFalse(result)
        matrix.show_image.assert_not_called()
        matrix.show_gif.assert_not_called()


    def test_advanced_canvas_samples_media_at_speed_without_changing_canvas_timeline(self):
        from modules.custom_text import canvas_timeline_frames

        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.colour_rgb.return_value = Mock(red=0, green=0, blue=0)
        matrix.media_frames.return_value = ["f0", "f1"]
        module = {
            "id": "custom_canvas", "name": "Canvas", "type": "canvas",
            "background": "#000000", "frame_count": 2, "fps": 10,
            "playback": "hold", "overrides": [],
            "layers": [{
                "id": "layer_media", "name": "Media", "type": "media",
                "visible": True, "keyframes": [],
                "properties": {
                    "x": 0, "y": 0, "width": 64, "height": 32,
                    "rotation": 0, "source": "clip.mp4", "keep_aspect": True,
                    "playback_speed_percent": 250.0,
                },
            }],
        }

        frames = list(canvas_timeline_frames(matrix, 0.2, None, module))

        self.assertEqual([delay for _canvas, delay in frames], [0.1, 0.1])
        self.assertEqual(matrix.media_frames.call_args.kwargs["playback_rate"], 2.5)
        self.assertEqual(matrix.draw_image.call_args_list[0].args[0], "f0")
        self.assertEqual(matrix.draw_image.call_args_list[1].args[0], "f1")

    def test_advanced_canvas_caches_every_resolved_inherited_media_identity(self):
        from modules.custom_text import canvas_timeline_frames

        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.colour_rgb.return_value = Mock(red=0, green=0, blue=0)
        matrix.media_frames.side_effect = lambda source, _count, _fps, **options: [
            (source, options["playback_rate"]),
        ]
        module = {
            "id": "custom_canvas", "name": "Canvas", "type": "canvas",
            "background": "#000000", "frame_count": 5, "fps": 10,
            "playback": "hold",
            "layers": [{
                "id": "layer_media", "name": "Media", "type": "media",
                "visible": True,
                "properties": {
                    "x": 0, "y": 0, "width": 64, "height": 32,
                    "rotation": 0, "source": "base.gif", "keep_aspect": True,
                    "playback_speed_percent": 100.0,
                },
                "keyframes": [
                    {"frame": 0, "easing": "linear", "properties": {"source": "first.gif"}},
                    {"frame": 1, "easing": "linear", "properties": {"playback_speed_percent": 100.0}},
                    {"frame": 3, "easing": "linear", "properties": {"playback_speed_percent": 200.0}},
                    {"frame": 4, "easing": "linear", "properties": {"source": "last.gif"}},
                ],
            }],
            "overrides": [{
                "frame": 3, "layer_id": "layer_media",
                "properties": {"source": "override.gif", "playback_speed_percent": 125.0},
            }],
        }

        list(canvas_timeline_frames(matrix, 0.5, None, module))

        decoded = {
            (call.args[0], call.kwargs["playback_rate"])
            for call in matrix.media_frames.call_args_list
        }
        self.assertEqual(decoded, {
            ("first.gif", 1.0),
            ("first.gif", 1.5),
            ("override.gif", 1.25),
            ("last.gif", 2.0),
        })
        self.assertEqual(
            [call.args[0] for call in matrix.draw_image.call_args_list],
            [
                ("first.gif", 1.0),
                ("first.gif", 1.0),
                ("first.gif", 1.5),
                ("override.gif", 1.25),
                ("last.gif", 2.0),
            ],
        )

    def test_advanced_canvas_same_source_at_two_speeds_has_distinct_cache_entries(self):
        from modules.custom_text import canvas_timeline_frames

        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.colour_rgb.return_value = Mock(red=0, green=0, blue=0)
        matrix.media_frames.side_effect = lambda _source, _count, _fps, **options: [options["playback_rate"]]
        base = {"x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                "source": "same.gif", "keep_aspect": True}
        module = {
            "id": "custom_canvas", "name": "Canvas", "type": "canvas",
            "background": "#000000", "frame_count": 1, "fps": 10,
            "playback": "hold", "overrides": [],
            "layers": [
                {"id": "layer_slow", "name": "Slow", "type": "media", "visible": True,
                 "keyframes": [], "properties": {**base, "playback_speed_percent": 50.0}},
                {"id": "layer_fast", "name": "Fast", "type": "media", "visible": True,
                 "keyframes": [], "properties": {**base, "playback_speed_percent": 200.0}},
            ],
        }

        list(canvas_timeline_frames(matrix, 0.1, None, module))

        self.assertEqual([call.kwargs["playback_rate"] for call in matrix.media_frames.call_args_list], [0.5, 2.0])
        self.assertEqual([call.args[0] for call in matrix.draw_image.call_args_list], [0.5, 2.0])

    def test_basic_media_preserves_fit_loops_speed_and_cancellation(self):
        from modules import custom_text

        matrix = Mock()
        matrix.show_gif.return_value = True
        stop = threading.Event()
        module = {
            "id": "custom_party", "name": "Party", "type": "media",
            "source": "party.gif", "fit": "manual", "x": -4, "y": 2,
            "width": 72, "height": 36, "loops": 3,
            "playback_speed_percent": 10.0,
        }

        self.assertTrue(custom_text.run(
            matrix, {}, duration=99, stop_event=stop, module_config=module,
        ))

        matrix.show_gif.assert_called_once_with(
            "party.gif", x=-4, y=2, width=72, height=36, fit="manual",
            loops=3, playback_rate=0.1, stop_event=stop,
            pinned_handle=self.pinned_handle,
        )

    def test_builtin_gif_and_mp4_use_canonical_playback_speed(self):
        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": True, "video_fps": 20,
            "playback_speed_percent": 250.0,
        }
        stop = threading.Event()
        for module, filename, method in (
            (splash_screen, "splash.gif", "show_gif"),
            (sleep_screen, "sleep.mp4", "show_video"),
        ):
            with self.subTest(filename=filename):
                matrix = Mock()
                getattr(matrix, method).return_value = True
                config = {
                    "media": {"splash": filename, "sleep": filename},
                    "media_layout": {"splash": settings, "sleep": settings},
                }
                self.assertTrue(module.run(
                    matrix, config, duration=2, stop_event=stop,
                ))
                call = getattr(matrix, method).call_args
                self.assertEqual(call.kwargs["playback_rate"], 2.5)
                self.assertEqual(call.kwargs["loops"], 0)
                self.assertIs(call.kwargs["stop_event"], stop)


if __name__ == "__main__":
    unittest.main()
