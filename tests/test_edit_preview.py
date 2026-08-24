import base64
import copy
import datetime
import gc
import io
import json
import importlib
import os
import random
import sys
import tempfile
import threading
import time
import types
import unittest
import weakref
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from PIL import Image, ImageSequence

from config_store import DEFAULT_CONFIG, validate_config
import frame_capture as frame_capture_module
from edit_preview import (
    CaptureMatrix, PreviewSession, custom_text_preview_frames, preview_frames,
    static_announcement_preview_frames, static_media_frames,
)
from frame_capture import BdfFont, FrameCapture
from modules.media_player import play_media
from modules import clock_lava, custom_text
import edit_preview


class Colour:
    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue


class EditPreviewTests(unittest.TestCase):
    def test_zero_selected_lava_preview_is_neutral_and_matches_physical_renderer(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 15, 9, 30, tzinfo=tz)

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "enabled_palettes": [], "custom_enabled": False,
            "custom_palettes": [], "show_time": True, "show_date": True,
        })

        def rendered_pixels(show_time, show_date):
            config = copy.deepcopy(candidate)
            config["lava"].update({"show_time": show_time, "show_date": show_date})
            frame = next(edit_preview.lava_preview_frames(CaptureMatrix(), config))
            with Image.open(io.BytesIO(base64.b64decode(frame["png"]))) as image:
                return tuple(image.convert("RGB").get_flattened_data()), frame["delay_ms"]

        with patch("modules.clock_lava.datetime.datetime", FixedDateTime):
            visible, delay = rendered_pixels(True, True)
            time_hidden, _ = rendered_pixels(False, True)
            date_hidden, _ = rendered_pixels(True, False)
            text_hidden, _ = rendered_pixels(False, False)

        self.assertIn((0, 0, 0), visible)
        self.assertNotEqual(visible, time_hidden)
        self.assertNotEqual(visible, date_hidden)
        self.assertEqual(set(text_hidden), {(0, 0, 0)})
        self.assertEqual(delay, 30)

    def test_page_supersede_race_drops_old_generation_output(self):
        class CommitBarrierIterator:
            def __init__(self, name, *, block_on_call=None):
                self.name = name
                self.block_on_call = block_on_call
                self.calls = 0
                self.active = False
                self.commit_barrier = threading.Event()
                self.release_commit = threading.Event()
                self.close_calls = 0
                self.concurrent_close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.calls += 1
                self.active = True
                try:
                    frame = {
                        "png": f"{self.name}-frame-{self.calls}",
                        "delay_ms": self.calls,
                    }
                    if self.calls == self.block_on_call:
                        self.commit_barrier.set()
                        if not self.release_commit.wait(2):
                            raise AssertionError("generation commit barrier was not released")
                    return frame
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise ValueError("generator already executing")
                self.close_calls += 1

        capacity = edit_preview.PreviewSessionCapacity()
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        old_stop = threading.Event()
        new_stop = threading.Event()
        old_iterator = CommitBarrierIterator("old", block_on_call=3)
        new_iterator = CommitBarrierIterator("new")
        old_instance = registry.register(
            "paired-device", lambda: old_iterator,
            stop_event=old_stop, generation=7,
        )
        old_session = old_instance.admit()
        old_session.MAX_FRAMES = 1
        old_first = old_session.page()
        old_token = old_first["next_token"]
        old_result = {}
        old_failures = []

        def page_old_generation():
            try:
                old_result.update(old_session.page(old_token))
            except BaseException as error:
                old_failures.append(error)

        old_thread = threading.Thread(
            target=page_old_generation, name="superseded-preview-page",
        )
        old_thread.start()
        self.assertTrue(
            old_iterator.commit_barrier.wait(1),
            "old generation did not reach pre-commit barrier",
        )

        supersede_started = time.monotonic()
        new_instance = registry.register(
            "paired-device", lambda: new_iterator,
            stop_event=new_stop, generation=8,
        )
        supersede_elapsed = time.monotonic() - supersede_started
        self.assertLess(supersede_elapsed, 0.25)
        self.assertTrue(old_stop.is_set())
        self.assertFalse(new_stop.is_set())
        self.assertEqual(capacity.active_count, 1)
        self.assertEqual(registry.total_count, 2)

        new_session = new_instance.admit()
        new_session.MAX_FRAMES = 1
        with self.assertRaisesRegex(
            ValueError, "invalid or already consumed continuation token",
        ):
            new_session.page(old_token)
        new_first = new_session.page()
        self.assertEqual(
            new_first["frames"], [{"png": "new-frame-1", "delay_ms": 1}],
        )
        self.assertFalse(new_first["complete"])
        self.assertIsNotNone(new_first["next_token"])
        with self.assertRaisesRegex(ValueError, "generation must be strictly newer"):
            registry.register(
                "paired-device", lambda: iter(()), generation=8,
            )
        with self.assertRaisesRegex(ValueError, "generation must be strictly newer"):
            registry.register(
                "paired-device", lambda: iter(()), generation=6,
            )

        released = time.monotonic()
        old_iterator.release_commit.set()
        old_thread.join(0.25)
        settle_elapsed = time.monotonic() - released

        self.assertLess(settle_elapsed, 0.25)
        self.assertFalse(old_thread.is_alive())
        self.assertEqual(old_failures, [])
        self.assertEqual(old_result, {
            "page": 1, "frames": [], "next_token": None, "complete": True,
        })
        self.assertEqual(old_iterator.calls, 3)
        self.assertEqual(old_iterator.concurrent_close_calls, 0)
        self.assertEqual(old_iterator.close_calls, 1)
        self.assertEqual(old_session.retained_frame_count, 0)
        self.assertEqual(registry.total_count, 1)
        self.assertEqual(capacity.active_count, 1)
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            old_session.page(old_token)

        new_second = new_session.page(new_first["next_token"])
        self.assertEqual(
            new_second["frames"], [{"png": "new-frame-2", "delay_ms": 2}],
        )
        self.assertFalse(new_stop.is_set())
        self.assertEqual(new_iterator.close_calls, 0)
        new_instance.close()
        new_instance.close()
        self.assertEqual(new_iterator.close_calls, 1)
        self.assertEqual(new_iterator.concurrent_close_calls, 0)
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})
        self.assertNotIn(old_thread, threading.enumerate())

    def test_page_cancel_race_drops_uncommitted_output_and_closes(self):
        class CommitBarrierIterator:
            def __init__(self, iterator, cancelled):
                self.iterator = iterator
                self.cancelled = cancelled
                self.calls = 0
                self.active = False
                self.commit_barrier = threading.Event()
                self.release_commit = threading.Event()
                self.close_calls = 0
                self.concurrent_close_calls = 0
                self.advanced_after_cancel = 0

            def __iter__(self):
                return self

            def __next__(self):
                if self.cancelled.is_set():
                    self.advanced_after_cancel += 1
                self.calls += 1
                self.active = True
                try:
                    frame = next(self.iterator)
                    # Page zero consumes calls 1 and 2.  The continuation consumes
                    # retained call 2, then pauses with valid call 3 generated as
                    # its lookahead but not yet committed to a page response.
                    if self.calls == 3:
                        self.commit_barrier.set()
                        if not self.release_commit.wait(2):
                            raise AssertionError("page commit barrier was not released")
                    return frame
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise ValueError("generator already executing")
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False,
            "show_date": False,
            "blur_radius": 0,
            "blob_count": 2,
        })
        cancelled = threading.Event()
        matrix = CaptureMatrix()
        lava = edit_preview.continuous_lava_preview_frames(
            matrix, candidate, {"kind": "built_in", "id": "fire_burst"},
            stop_event=cancelled,
        )
        iterator = CommitBarrierIterator(lava, cancelled)
        capacity = edit_preview.PreviewSessionCapacity()
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        instance = registry.register(
            "paired-device", lambda: iterator, stop_event=cancelled,
        )
        session = instance.admit()
        session.MAX_FRAMES = 1
        first = session.page()
        stale_token = first["next_token"]
        result = {}
        failures = []

        def page_in_background():
            try:
                result.update(session.page(stale_token))
            except BaseException as error:
                failures.append(error)

        cancel_elapsed = []

        def cancel_in_background():
            started = time.monotonic()
            session.cancel()
            session.cancel()
            cancel_elapsed.append(time.monotonic() - started)

        page_thread = threading.Thread(target=page_in_background, name="raced-page")
        cancel_thread = threading.Thread(target=cancel_in_background, name="raced-cancel")
        page_thread.start()
        self.assertTrue(
            iterator.commit_barrier.wait(1), "page did not reach pre-commit barrier",
        )
        cancel_thread.start()
        cancel_thread.join(0.25)
        try:
            self.assertFalse(
                cancel_thread.is_alive(), "cancel waited on the page-operation lock",
            )
            self.assertTrue(cancelled.is_set())
            self.assertLess(cancel_elapsed[0], 0.25)
            # Ownership cannot be released while the page still executes; cleanup
            # is deferred to that page's unwind instead of closing it concurrently.
            self.assertEqual(capacity.active_count, 1)
            self.assertEqual(registry.total_count, 1)
        finally:
            released = time.monotonic()
            iterator.release_commit.set()
            page_thread.join(0.25)
            cancel_thread.join(0.25)
            settle_elapsed = time.monotonic() - released
            if page_thread.is_alive():
                iterator.release_commit.set()
                page_thread.join(1)

        self.assertLess(settle_elapsed, 0.25)
        self.assertFalse(page_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result, {
            "page": 1, "frames": [], "next_token": None, "complete": True,
        })
        self.assertEqual(iterator.calls, 3)
        self.assertEqual(iterator.advanced_after_cancel, 0)
        self.assertEqual(iterator.concurrent_close_calls, 0)
        self.assertEqual(iterator.close_calls, 1)
        self.assertEqual(session.retained_frame_count, 0)
        self.assertIsNone(getattr(lava, "gi_frame"))
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            session.page(stale_token)
        self.assertEqual(iterator.calls, 3)
        session.cancel()
        self.assertEqual(iterator.close_calls, 1)

    def test_simultaneous_page_requests_allow_exactly_one_token_claim(self):
        class CoordinatedIterator:
            def __init__(self):
                self.calls = 0
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()
                self.continuation_entered = threading.Event()
                self.release_continuation = threading.Event()

            def __iter__(self):
                return self

            def __next__(self):
                with self.lock:
                    self.calls += 1
                    call = self.calls
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    # Page zero consumes call 1 and retains call 2 as lookahead.
                    # The first continuation caller consumes that lookahead, then
                    # pauses on call 3 so its simultaneous peer can try the token.
                    if call == 3:
                        self.continuation_entered.set()
                        if not self.release_continuation.wait(2):
                            raise AssertionError("continuation barrier was not released")
                    return {"png": f"frame-{call}", "delay_ms": call}
                finally:
                    with self.lock:
                        self.active -= 1

        iterator = CoordinatedIterator()
        session = PreviewSession(iterator)
        session.MAX_FRAMES = 1
        first = session.page()
        token = first["next_token"]
        self.assertEqual(first["frames"], [{"png": "frame-1", "delay_ms": 1}])

        launch = threading.Barrier(3)
        outcome_before_release = threading.Event()
        results = []
        failures = []

        def request_page():
            launch.wait()
            try:
                results.append(session.page(token))
            except BaseException as error:
                failures.append(error)
            finally:
                outcome_before_release.set()

        threads = [threading.Thread(target=request_page) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            launch.wait()
            self.assertTrue(iterator.continuation_entered.wait(1))
            loser_failed_before_winner_advanced = outcome_before_release.wait(1)
        finally:
            iterator.release_continuation.set()
            for thread in threads:
                thread.join(2)

        self.assertTrue(loser_failed_before_winner_advanced)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertEqual(
            str(failures[0]), "invalid or already consumed continuation token"
        )
        winning_page = results[0]
        self.assertEqual(winning_page["page"], 1)
        self.assertEqual(
            winning_page["frames"], [{"png": "frame-2", "delay_ms": 2}]
        )
        self.assertFalse(winning_page["complete"])
        self.assertIsNotNone(winning_page["next_token"])
        self.assertEqual(iterator.calls, 3)
        self.assertEqual(iterator.max_active, 1)

        next_token = winning_page["next_token"]
        later_page = session.page(next_token)
        self.assertEqual(later_page["page"], 2)
        self.assertEqual(
            later_page["frames"], [{"png": "frame-3", "delay_ms": 3}]
        )
        self.assertEqual(iterator.calls, 4)
        with self.assertRaisesRegex(
            ValueError, "invalid or already consumed continuation token"
        ):
            session.page(next_token)
        self.assertEqual(iterator.calls, 4)

    def test_registry_shutdown_closes_every_iterator_and_decoder(self):
        class CountingIterator:
            def __init__(self, iterator, on_close=None):
                self.iterator = iter(iterator)
                self.on_close = on_close
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.iterator)

            def close(self):
                self.close_calls += 1
                close = getattr(self.iterator, "close", None)
                if close is not None:
                    close()
                if self.on_close is not None:
                    self.on_close()

        class BlockedDecoder:
            def __init__(self):
                self.started = threading.Event()
                self.stop_observed = threading.Event()
                self.emergency_release = threading.Event()
                self.close_calls = 0
                self.stop_event = None
                self.read_fd, self.write_fd = os.pipe()

            def __call__(self, _path, _settings, *, stop_event=None):
                self.stop_event = stop_event
                return self

            def __iter__(self):
                return self

            def __next__(self):
                self.started.set()
                while not self.emergency_release.wait(0.01):
                    if self.stop_event is not None and self.stop_event.is_set():
                        self.stop_observed.set()
                        raise StopIteration
                raise AssertionError("registry shutdown required emergency decoder release")

            def close(self):
                self.close_calls += 1
                os.close(self.read_fd)
                os.close(self.write_fd)

        capacity = edit_preview.PreviewSessionCapacity()
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        lava_stop = threading.Event()
        media_stop = threading.Event()
        decoder = BlockedDecoder()
        lava_iterator = None
        page_result = {}
        page_failures = []
        page_thread = None
        non_admitted_factory_calls = []
        non_admitted_stop = threading.Event()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False, "show_date": False, "blur_radius": 0,
            "blob_count": 2,
        })
        lava_matrix = CaptureMatrix()
        media_matrix = CaptureMatrix()

        def lava_factory():
            nonlocal lava_iterator
            lava_iterator = CountingIterator(
                edit_preview.continuous_lava_preview_frames(
                    lava_matrix, candidate,
                    {"kind": "built_in", "id": "fire_burst"},
                    stop_event=lava_stop,
                ),
                # A close callback that re-enters the registry proves shutdown
                # never invokes resource callbacks while holding its lock.
                on_close=lambda: registry.total_count,
            )
            return lava_iterator

        lava_instance = registry.register(
            "paired-device", lava_factory, stop_event=lava_stop,
        )
        media_instance = registry.register(
            "paired-device",
            lambda: edit_preview.media_preview_frames(
                media_matrix, "blocked.mp4", 10,
                settings={
                    "x": 0, "y": 0, "width": 64, "height": 32,
                    "keep_aspect": True, "video_fps": 20,
                },
                video_decoder=decoder, stop_event=media_stop,
            ),
            stop_event=media_stop,
        )
        non_admitted = registry.register(
            "paired-device",
            lambda: non_admitted_factory_calls.append(True) or iter(()),
            stop_event=non_admitted_stop,
        )
        lava_session = lava_instance.admit()
        media_session = media_instance.admit()
        lava_page = lava_session.page()
        self.assertFalse(lava_page["complete"])
        stale_lava_token = lava_page["next_token"]

        def page_media():
            try:
                page_result.update(media_session.page())
            except BaseException as error:
                page_failures.append(error)

        page_thread = threading.Thread(target=page_media, name="registry-blocked-media-page")
        page_thread.start()
        try:
            self.assertTrue(decoder.started.wait(1), "media decoder did not block")
            self.assertEqual(registry.total_count, 3)
            self.assertEqual(capacity.active_count, 2)

            started = time.monotonic()
            registry.close()
            page_thread.join(0.25)
            elapsed = time.monotonic() - started
        finally:
            decoder.emergency_release.set()
            if page_thread is not None:
                page_thread.join(1)

        self.assertLess(elapsed, 0.25)
        self.assertFalse(page_thread.is_alive(), "registry shutdown cleanup hung")
        self.assertEqual(page_failures, [])
        self.assertEqual(page_result["frames"], [])
        self.assertTrue(page_result["complete"])
        self.assertIsNone(page_result["next_token"])
        self.assertTrue(lava_stop.is_set())
        self.assertTrue(media_stop.is_set())
        self.assertTrue(non_admitted_stop.is_set())
        self.assertTrue(decoder.stop_observed.is_set())
        self.assertEqual(lava_iterator.close_calls, 1)
        self.assertEqual(decoder.close_calls, 1)
        self.assertEqual(non_admitted_factory_calls, [])
        for descriptor in (decoder.read_fd, decoder.write_fd):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.waiting_count, 0)
        self.assertNotIn(page_thread, threading.enumerate())
        self.assertEqual(lava_session.retained_frame_count, 0)
        self.assertEqual(media_session.retained_frame_count, 0)

        registry.close()
        lava_instance.close()
        media_instance.close()
        non_admitted.close()
        self.assertEqual(lava_iterator.close_calls, 1)
        self.assertEqual(decoder.close_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "preview registry is closed"):
            registry.register("late-device", lambda: iter(()))
        with self.assertRaisesRegex(RuntimeError, "preview instance is closed"):
            non_admitted.admit()
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            lava_session.page(stale_lava_token)

    def test_registry_bounds_instances_per_device_and_globally(self):
        factories = []

        def finite_renderer(name):
            factories.append(name)
            return iter(({"png": name, "delay_ms": 30},))

        capacity = edit_preview.PreviewSessionCapacity()
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        instances = []
        try:
            device_a = [
                registry.register("paired-device-a", lambda index=index: finite_renderer(f"a-{index}"))
                for index in range(4)
            ]
            instances.extend(device_a)
            self.assertEqual(registry.total_count, 4)
            self.assertEqual(registry.count_for_device("paired-device-a"), 4)
            with self.assertRaisesRegex(
                edit_preview.PreviewInstanceCapacityError,
                "paired device already has 4 live preview instances",
            ):
                registry.register(
                    "paired-device-a", lambda: finite_renderer("ghost-device"),
                )
            self.assertEqual(registry.total_count, 4)
            self.assertEqual(factories, [])

            other_devices = []
            for name in ("b", "c", "d", "e"):
                instance = registry.register(
                    f"paired-device-{name}",
                    lambda name=name: finite_renderer(name),
                )
                other_devices.append(instance)
                instances.append(instance)
            self.assertEqual(registry.total_count, 8)
            self.assertEqual(registry.count_for_device("paired-device-b"), 1)
            self.assertEqual(registry.count_for_device("paired-device-a"), 4)
            self.assertEqual(factories, [])

            ids = [instance.instance_id for instance in instances]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(len(instance_id) == 32 for instance_id in ids))
            self.assertTrue(all(
                all(fragment not in instance_id for fragment in ("paired", "device", "a-"))
                for instance_id in ids
            ))
            with self.assertRaisesRegex(
                edit_preview.PreviewInstanceCapacityError,
                "process already has 8 live preview instances",
            ):
                registry.register(
                    "paired-device-f", lambda: finite_renderer("ghost-global"),
                )
            self.assertEqual(registry.total_count, 8)
            self.assertEqual(registry.count_for_device("paired-device-f"), 0)
            self.assertEqual(factories, [])

            first_session = device_a[0].admit()
            second_session = other_devices[0].admit()
            self.assertEqual(capacity.active_count, 2)
            self.assertEqual(factories, ["a-0", "b"])
            self.assertIs(device_a[0].admit(), first_session)
            self.assertEqual(factories, ["a-0", "b"])

            first_page = first_session.page()
            self.assertTrue(first_page["complete"])
            self.assertEqual(registry.total_count, 7)
            self.assertEqual(registry.count_for_device("paired-device-a"), 3)
            first_session.cancel()
            first_session.cancel()
            self.assertEqual(registry.total_count, 7)

            replacement = registry.register(
                "paired-device-a", lambda: finite_renderer("replacement"),
            )
            instances.append(replacement)
            self.assertEqual(registry.total_count, 8)
            self.assertEqual(registry.count_for_device("paired-device-a"), 4)

            second_session.cancel()
            second_session.cancel()
            self.assertEqual(registry.total_count, 7)
            self.assertEqual(registry.count_for_device("paired-device-b"), 0)
            self.assertEqual(capacity.active_count, 0)
        finally:
            for instance in instances:
                instance.close()
                instance.close()

        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})
        self.assertEqual(capacity.active_count, 0)

        failed = registry.register(
            "paired-device-failure",
            lambda: (_ for _ in ()).throw(RuntimeError("renderer construction failed")),
        )
        with self.assertRaisesRegex(RuntimeError, "renderer construction failed"):
            failed.admit()
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.count_for_device("paired-device-failure"), 0)
        self.assertEqual(capacity.active_count, 0)
        self.assertNotIn("ghost-device", factories)
        self.assertNotIn("ghost-global", factories)

    def test_two_active_sessions_saturate_capacity_and_queue_times_out(self):
        class Clock:
            def __init__(self):
                self.now = 100.0
                self.waits = []

            def monotonic(self):
                return self.now

            def wait(self, condition, timeout):
                self.waits.append(timeout)
                self.now += timeout
                return False

        closed = []

        def renderer(name):
            try:
                for frame_number in range(100):
                    yield {"png": f"{name}-{frame_number}", "delay_ms": 30}
            finally:
                closed.append(name)

        clock = Clock()
        capacity = edit_preview.PreviewSessionCapacity(
            monotonic=clock.monotonic, wait=clock.wait,
        )
        first = capacity.admit(lambda: renderer("first"))
        second = capacity.admit(lambda: renderer("second"))

        first_page = first.page()
        second_page = second.page()
        self.assertFalse(first_page["complete"])
        self.assertFalse(second_page["complete"])
        self.assertEqual(capacity.active_count, 2)
        self.assertEqual(capacity.available_slots, 0)

        third_factory_calls = []
        with self.assertRaisesRegex(
            edit_preview.PreviewCapacityError,
            r"preview session capacity acquisition timed out after 2 seconds",
        ):
            capacity.admit(
                lambda: third_factory_calls.append(True) or renderer("timed-out"),
            )

        self.assertEqual(clock.waits, [2.0])
        self.assertEqual(third_factory_calls, [])
        self.assertEqual(capacity.active_count, 2)
        self.assertEqual(capacity.waiting_count, 0)
        self.assertFalse(first_page["complete"])
        self.assertFalse(second_page["complete"])

        first.cancel()
        first.cancel()
        self.assertEqual(capacity.active_count, 1)
        recovered = capacity.admit(lambda: renderer("recovered"))
        self.assertEqual(capacity.active_count, 2)
        self.assertFalse(recovered.page()["complete"])

        second.cancel()
        recovered.cancel()
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)
        self.assertEqual(capacity.waiting_count, 0)
        self.assertCountEqual(closed, ["first", "second", "recovered"])
        self.assertNotIn("timed-out", closed)

    def test_response_failure_after_token_claim_closes_without_replay(self):
        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

        class CountingIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.calls = 0
                self.active = False
                self.close_calls = 0
                self.concurrent_close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.calls += 1
                self.active = True
                try:
                    return next(self.iterator)
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise ValueError("generator already executing")
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False, "show_date": False, "blur_radius": 0,
            "blob_count": 2,
        })
        stop_event = threading.Event()
        lava = edit_preview.continuous_lava_preview_frames(
            CaptureMatrix(), candidate,
            {"kind": "built_in", "id": "fire_burst"},
            stop_event=stop_event,
        )
        iterator = CountingIterator(lava)
        clock = Clock()
        capacity = edit_preview.PreviewSessionCapacity(monotonic=clock.monotonic)
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        instance = registry.register(
            "paired-device", lambda: iterator, stop_event=stop_event,
        )
        session = instance.admit()
        session.MAX_FRAMES = 1
        acknowledged_pages = []
        first = session.page(
            publish=lambda page: acknowledged_pages.append(
                json.dumps(page, separators=(",", ":")),
            ),
        )
        self.assertEqual(len(acknowledged_pages), 1)
        claimed_token = first["next_token"]
        failed_pages = []

        def fail_after_serialization(page):
            json.dumps(page, separators=(",", ":"))
            failed_pages.append(copy.deepcopy(page))
            raise OSError("deterministic response delivery failure")

        with self.assertRaisesRegex(OSError, "deterministic response delivery failure"):
            session.page(claimed_token, publish=fail_after_serialization)

        self.assertEqual(len(failed_pages), 1)
        replacement_token = failed_pages[0]["next_token"]
        self.assertIsNotNone(replacement_token)
        self.assertNotEqual(replacement_token, claimed_token)
        self.assertEqual(failed_pages[0]["page"], 1)
        self.assertEqual(iterator.calls, 3)
        self.assertTrue(stop_event.is_set())
        self.assertEqual(session.state, "CLOSED")
        self.assertEqual(session.retained_frame_count, 0)
        self.assertEqual(iterator.concurrent_close_calls, 0)
        self.assertEqual(iterator.close_calls, 1)
        self.assertIsNone(lava.gi_frame)
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})

        for token in (claimed_token, replacement_token):
            with self.assertRaisesRegex(
                StopIteration, "explicit new session is required to restart",
            ):
                session.page(token)
        self.assertEqual(iterator.calls, 3)
        clock.now += capacity.IDLE_TTL + 1
        self.assertEqual(capacity.reap_expired(), 0)

        session.cancel()
        session.cancel()
        instance.close()
        self.assertEqual(iterator.close_calls, 1)

        replacement = registry.register(
            "paired-device", lambda: iter((
                {"png": "replacement", "delay_ms": 1},
            )),
        )
        replacement_page = replacement.admit().page()
        self.assertEqual(
            replacement_page["frames"],
            [{"png": "replacement", "delay_ms": 1}],
        )
        self.assertTrue(replacement_page["complete"])
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(registry.total_count, 0)

    def test_close_during_page_is_idempotent_and_joins_reaper(self):
        class CommitBarrierIterator:
            def __init__(self):
                self.calls = 0
                self.active = False
                self.blocked = threading.Event()
                self.release = threading.Event()
                self.close_calls = 0
                self.concurrent_close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.calls += 1
                self.active = True
                try:
                    if self.calls == 3:
                        self.blocked.set()
                        if not self.release.wait(2):
                            raise AssertionError("close-during-page barrier was not released")
                    return {"png": f"frame-{self.calls}", "delay_ms": 1}
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise ValueError("iterator closed while executing")
                self.close_calls += 1

        capacity = edit_preview.PreviewSessionCapacity()
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        stop_event = threading.Event()
        iterator = CommitBarrierIterator()
        instance = registry.register(
            "paired-device", lambda: iterator, stop_event=stop_event,
        )
        session = instance.admit()
        session.MAX_FRAMES = 1
        first = session.page()
        stale_token = first["next_token"]
        page_result = {}
        page_failures = []
        close_failures = []

        def page():
            try:
                page_result.update(session.page(stale_token))
            except BaseException as error:
                page_failures.append(error)

        def close_registry():
            try:
                registry.close()
            except BaseException as error:
                close_failures.append(error)

        page_thread = threading.Thread(target=page, name="close-active-preview-page")
        close_threads = [
            threading.Thread(target=close_registry, name=f"preview-close-{index}")
            for index in range(3)
        ]
        page_thread.start()
        self.assertTrue(iterator.blocked.wait(1), "page did not reach commit barrier")
        for thread in close_threads:
            thread.start()
        try:
            deadline = time.monotonic() + 1
            while not stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue(stop_event.is_set())
            self.assertEqual(registry.lifecycle_state, "CLOSING")
            self.assertEqual(session.state, "CLOSING")
            self.assertTrue(registry.reaper_is_daemon)
            self.assertEqual(registry.reaper_join_count, 0)
            self.assertTrue(all(thread.is_alive() for thread in close_threads))
            self.assertEqual(iterator.close_calls, 0)
            self.assertEqual(iterator.concurrent_close_calls, 0)
            self.assertEqual(capacity.active_count, 1)
            self.assertEqual(registry.total_count, 1)
        finally:
            settled_started = time.monotonic()
            iterator.release.set()
            page_thread.join(0.25)
            for thread in close_threads:
                thread.join(0.25)
            settled_elapsed = time.monotonic() - settled_started
            if page_thread.is_alive() or any(thread.is_alive() for thread in close_threads):
                session.cancel()
                iterator.release.set()
                page_thread.join(1)
                for thread in close_threads:
                    thread.join(1)

        self.assertLess(settled_elapsed, 0.25)
        self.assertFalse(page_thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in close_threads))
        self.assertEqual(page_failures, [])
        self.assertEqual(close_failures, [])
        self.assertEqual(page_result, {
            "page": 1, "frames": [], "next_token": None, "complete": True,
        })
        self.assertEqual(iterator.concurrent_close_calls, 0)
        self.assertEqual(iterator.close_calls, 1)
        self.assertEqual(registry.lifecycle_state, "CLOSED")
        self.assertFalse(registry.reaper_alive)
        self.assertEqual(registry.reaper_join_count, 1)
        self.assertEqual(session.state, "CLOSED")
        self.assertEqual(session.retained_frame_count, 0)
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.waiting_count, 0)
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})

        registry.close()
        registry.close()
        instance.close()
        session.cancel()
        self.assertEqual(registry.reaper_join_count, 1)
        self.assertEqual(iterator.close_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "preview registry is closed"):
            registry.register("late-device", lambda: iter(()))
        with self.assertRaisesRegex(RuntimeError, "preview instance is closed"):
            instance.admit()
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            session.page(stale_token)

    def test_expiry_during_page_marks_closing_and_releases_after_page_returns(self):
        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

        class CommitBarrierIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.calls = 0
                self.active = False
                self.commit_barrier = threading.Event()
                self.release_commit = threading.Event()
                self.close_calls = 0
                self.concurrent_close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.calls += 1
                self.active = True
                try:
                    frame = next(self.iterator)
                    if self.calls == 3:
                        self.commit_barrier.set()
                        if not self.release_commit.wait(2):
                            raise AssertionError("expiry commit barrier was not released")
                    return frame
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise ValueError("generator already executing")
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False, "show_date": False, "blur_radius": 0,
            "blob_count": 2,
        })
        clock = Clock()
        capacity = edit_preview.PreviewSessionCapacity(monotonic=clock.monotonic)
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        expired_stop = threading.Event()
        control_stop = threading.Event()
        lava = edit_preview.continuous_lava_preview_frames(
            CaptureMatrix(), candidate,
            {"kind": "built_in", "id": "fire_burst"},
            stop_event=expired_stop,
        )
        iterator = CommitBarrierIterator(lava)
        expired_instance = registry.register(
            "expired-device", lambda: iterator, stop_event=expired_stop,
        )
        control_instance = registry.register(
            "control-device", lambda: iter((
                {"png": "control-1", "delay_ms": 1},
                {"png": "control-2", "delay_ms": 1},
                {"png": "control-3", "delay_ms": 1},
            )), stop_event=control_stop,
        )
        waiter_factory_calls = []
        waiter_instance = registry.register(
            "waiting-device",
            lambda: waiter_factory_calls.append(True) or iter((
                {"png": "waiter", "delay_ms": 1},
            )),
        )
        expired_session = expired_instance.admit()
        control_session = control_instance.admit()
        expired_session.MAX_FRAMES = control_session.MAX_FRAMES = 1
        first = expired_session.page()
        stale_token = first["next_token"]
        control_first = control_session.page()
        page_result = {}
        page_failures = []
        waiter_result = []
        waiter_failures = []

        def blocked_page():
            try:
                page_result.update(expired_session.page(stale_token))
            except BaseException as error:
                page_failures.append(error)

        def wait_for_slot():
            try:
                waiter_result.append(waiter_instance.admit())
            except BaseException as error:
                waiter_failures.append(error)

        page_thread = threading.Thread(target=blocked_page, name="expiring-preview-page")
        waiter_thread = threading.Thread(target=wait_for_slot, name="expiry-slot-waiter")
        page_thread.start()
        self.assertTrue(iterator.commit_barrier.wait(1))
        clock.now += capacity.IDLE_TTL + 0.001
        control_second = control_session.page(control_first["next_token"])
        control_calls_before_reap = control_second["frames"]
        expiry_deadline = time.monotonic() + 0.25
        while expired_session.state == "ACTIVE" and time.monotonic() < expiry_deadline:
            time.sleep(0.001)
        self.assertEqual(expired_session.state, "CLOSING")
        waiter_thread.start()
        deadline = time.monotonic() + 1
        while capacity.waiting_count != 1 and time.monotonic() < deadline:
            time.sleep(0.001)

        try:
            self.assertEqual(capacity.waiting_count, 1)
            reaped_started = time.monotonic()
            self.assertEqual(capacity.reap_expired(), 0)
            reaped_elapsed = time.monotonic() - reaped_started
            self.assertLess(reaped_elapsed, 0.25)
            self.assertEqual(expired_session.state, "CLOSING")
            self.assertEqual(expired_instance.state, "CLOSING")
            self.assertTrue(expired_stop.is_set())
            self.assertIsNone(expired_session._token)
            self.assertEqual(iterator.close_calls, 0)
            self.assertEqual(iterator.concurrent_close_calls, 0)
            self.assertEqual(capacity.reap_expired(), 0)
            expired_session.cancel()
            capacity._record_activity(expired_session)
            self.assertEqual(expired_session.state, "CLOSING")
            self.assertEqual(iterator.close_calls, 0)
            self.assertEqual(capacity.active_count, 2)
            self.assertEqual(registry.total_count, 3)
            self.assertTrue(waiter_thread.is_alive())
            self.assertEqual(waiter_factory_calls, [])
            self.assertFalse(control_stop.is_set())
            self.assertEqual(control_second["frames"], control_calls_before_reap)
        finally:
            released = time.monotonic()
            iterator.release_commit.set()
            page_thread.join(0.25)
            waiter_thread.join(0.25)
            settle_elapsed = time.monotonic() - released
            if page_thread.is_alive() or waiter_thread.is_alive():
                expired_session.cancel()
                control_session.cancel()
                iterator.release_commit.set()
                page_thread.join(1)
                waiter_thread.join(1)

        self.assertLess(settle_elapsed, 0.25)
        self.assertFalse(page_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(page_failures, [])
        self.assertEqual(waiter_failures, [])
        self.assertEqual(page_result, {
            "page": 1, "frames": [], "next_token": None, "complete": True,
        })
        self.assertEqual(iterator.calls, 3)
        self.assertEqual(iterator.concurrent_close_calls, 0)
        self.assertEqual(iterator.close_calls, 1)
        self.assertIsNone(lava.gi_frame)
        self.assertEqual(expired_session.state, "CLOSED")
        self.assertEqual(expired_instance.state, "CLOSED")
        self.assertEqual(expired_session.retained_frame_count, 0)
        self.assertEqual(capacity.active_count, 2)
        self.assertEqual(registry.total_count, 2)
        self.assertEqual(len(waiter_result), 1)
        self.assertEqual(waiter_factory_calls, [True])
        self.assertEqual(capacity.reap_expired(), 0)
        expired_session.cancel()
        capacity._record_activity(expired_session)
        self.assertEqual(expired_session.state, "CLOSED")
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            expired_session.page(stale_token)

        waiter_result[0].cancel()
        control_session.cancel()
        control_instance.close()
        waiter_instance.close()
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})

    def test_abandoned_session_expires_without_another_api_request(self):
        class Clock:
            def __init__(self):
                self.now = 100.0
                self.capacity = None

            def monotonic(self):
                return self.now

            def wake(self):
                if self.capacity is None:
                    raise AssertionError("clock capacity was not connected")
                with self.capacity._condition:
                    self.capacity._condition.notify_all()

            def advance(self, seconds):
                self.now += seconds
                # Advancing the injected monotonic clock is the test's only wake;
                # no page/reap/cancel/close call drives expiry.
                self.wake()

        class CloseCountingIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.next_calls = 0
                self.close_calls = 0
                self.close_started = threading.Event()
                self.release_close = threading.Event()

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return next(self.iterator)

            def close(self):
                self.close_calls += 1
                self.close_started.set()
                if not self.release_close.wait(2):
                    raise AssertionError("abandoned iterator close was not released")
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False, "show_date": False, "blur_radius": 0,
            "blob_count": 2,
        })
        clock = Clock()
        capacity = edit_preview.PreviewSessionCapacity(monotonic=clock.monotonic)
        clock.capacity = capacity
        reapers_before = [
            thread for thread in threading.enumerate()
            if thread.name == "edit-preview-expiry-reaper"
        ]
        registry = edit_preview.PreviewInstanceRegistry(capacity=capacity)
        abandoned_stop = threading.Event()
        control_stop = threading.Event()
        lava = edit_preview.continuous_lava_preview_frames(
            CaptureMatrix(), candidate,
            {"kind": "built_in", "id": "fire_burst"},
            stop_event=abandoned_stop,
        )
        iterator = CloseCountingIterator(lava)
        waiter_factory_calls = []
        waiter_result = []
        waiter_failures = []
        waiter_thread = None

        try:
            abandoned_instance = registry.register(
                "abandoned-device", lambda: iterator, stop_event=abandoned_stop,
            )
            control_instance = registry.register(
                "control-device", lambda: iter((
                    {"png": "control-1", "delay_ms": 1},
                    {"png": "control-2", "delay_ms": 1},
                    {"png": "control-3", "delay_ms": 1},
                )), stop_event=control_stop,
            )
            waiter_instance = registry.register(
                "waiting-device",
                lambda: waiter_factory_calls.append(True) or iter((
                    {"png": "waiting", "delay_ms": 1},
                )),
            )
            abandoned_session = abandoned_instance.admit()
            control_session = control_instance.admit()
            abandoned_session.MAX_FRAMES = control_session.MAX_FRAMES = 1
            abandoned_page = abandoned_session.page()
            stale_token = abandoned_page["next_token"]
            control_page = control_session.page()
            self.assertFalse(abandoned_page["complete"])
            self.assertEqual(abandoned_session.retained_frame_count, 1)
            self.assertIsNotNone(stale_token)

            named_reapers = [
                thread for thread in threading.enumerate()
                if thread.name == "edit-preview-expiry-reaper"
                and thread not in reapers_before
            ]
            self.assertEqual(named_reapers, [registry._reaper])
            self.assertTrue(registry.reaper_is_daemon)
            self.assertFalse(any(
                thread.name.startswith("edit-preview-session-")
                for thread in threading.enumerate()
            ))

            clock.advance(capacity.IDLE_TTL - 1)
            control_page = control_session.page(control_page["next_token"])

            def wait_for_slot():
                try:
                    waiter_result.append(waiter_instance.admit())
                except BaseException as error:
                    waiter_failures.append(error)

            waiter_thread = threading.Thread(
                target=wait_for_slot, name="abandoned-expiry-slot-waiter",
            )
            waiter_thread.start()
            deadline = time.monotonic() + 1
            while capacity.waiting_count != 1 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(capacity.waiting_count, 1)

            # The documented contract is strictly after 60 seconds.  Activity on
            # the control session above must also move its nearest deadline.
            clock.advance(1)
            time.sleep(0.01)
            self.assertEqual(abandoned_session.state, "ACTIVE")
            self.assertFalse(abandoned_stop.is_set())
            self.assertTrue(waiter_thread.is_alive())
            self.assertFalse(control_stop.is_set())

            clock.advance(0.001)
            self.assertTrue(iterator.close_started.wait(0.25))
            self.assertEqual(abandoned_session.state, "CLOSING")
            self.assertEqual(abandoned_instance.state, "CLOSING")
            self.assertTrue(abandoned_stop.is_set())
            self.assertIsNone(abandoned_session._token)
            self.assertEqual(abandoned_session.retained_frame_count, 0)
            self.assertEqual(iterator.close_calls, 1)
            self.assertTrue(waiter_thread.is_alive())
            clock.wake()
            time.sleep(0.01)
            self.assertEqual(iterator.close_calls, 1)
            iterator.release_close.set()
            expiry_deadline = time.monotonic() + 0.25
            while abandoned_session.state != "CLOSED" and time.monotonic() < expiry_deadline:
                time.sleep(0.001)
            waiter_thread.join(0.25)

            self.assertEqual(abandoned_session.state, "CLOSED")
            self.assertEqual(abandoned_instance.state, "CLOSED")
            self.assertEqual(iterator.close_calls, 1)
            self.assertIsNone(getattr(lava, "gi_frame"))
            self.assertFalse(waiter_thread.is_alive())
            self.assertEqual(waiter_failures, [])
            self.assertEqual(len(waiter_result), 1)
            self.assertEqual(waiter_factory_calls, [True])
            self.assertEqual(capacity.active_count, 2)
            self.assertEqual(capacity.waiting_count, 0)
            self.assertEqual(registry.total_count, 2)
            self.assertEqual(registry.counts_by_device, {
                "control-device": 1, "waiting-device": 1,
            })
            self.assertFalse(control_stop.is_set())

            with self.assertRaisesRegex(
                StopIteration, "explicit new session is required to restart",
            ):
                abandoned_session.page(stale_token)
            calls_after_expiry = iterator.next_calls
            clock.advance(1)
            time.sleep(0.01)
            self.assertEqual(iterator.next_calls, calls_after_expiry)
            self.assertEqual(iterator.close_calls, 1)
        finally:
            iterator.release_close.set()
            registry.close()
            if waiter_thread is not None:
                waiter_thread.join(1)

        registry.close()
        self.assertEqual(registry.lifecycle_state, "CLOSED")
        self.assertFalse(registry.reaper_alive)
        self.assertEqual(registry.reaper_join_count, 1)
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)
        self.assertEqual(capacity.waiting_count, 0)
        self.assertEqual(registry.total_count, 0)
        self.assertEqual(registry.counts_by_device, {})
        self.assertNotIn(registry._reaper, threading.enumerate())

    def test_expired_session_closes_iterator_and_releases_slot(self):
        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.swaps = 0

            def swap(self, canvas=None):
                self.swaps += 1
                return super().swap(canvas)

        class CloseCountingIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return next(self.iterator)

            def close(self):
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False,
            "show_date": False,
            "blur_radius": 0,
            "blob_count": 2,
        })
        clock = Clock()
        capacity = edit_preview.PreviewSessionCapacity(monotonic=clock.monotonic)
        expired_stop = threading.Event()
        active_stop = threading.Event()
        expired_matrix = RecordingMatrix()
        active_matrix = RecordingMatrix()
        expired_lava = edit_preview.continuous_lava_preview_frames(
            expired_matrix, candidate,
            {"kind": "built_in", "id": "fire_burst"},
            stop_event=expired_stop,
        )
        active_lava = edit_preview.continuous_lava_preview_frames(
            active_matrix, candidate,
            {"kind": "built_in", "id": "fire_burst"},
            stop_event=active_stop,
        )
        expired_iterator = CloseCountingIterator(expired_lava)
        active_iterator = CloseCountingIterator(active_lava)
        expired = capacity.admit(
            lambda: expired_iterator, stop_event=expired_stop,
        )
        active = capacity.admit(lambda: active_iterator, stop_event=active_stop)
        expired.MAX_FRAMES = active.MAX_FRAMES = 2

        expired_page = expired.page()
        active_page = active.page()
        stale_token = expired_page["next_token"]
        expired_before_reap = (
            expired_iterator.next_calls, expired_matrix.swaps,
        )
        self.assertFalse(expired_page["complete"])
        self.assertIsNotNone(stale_token)
        self.assertEqual(expired.retained_frame_count, 1)
        self.assertEqual(capacity.active_count, 2)

        clock.now += capacity.IDLE_TTL
        self.assertEqual(capacity.reap_expired(), 0)
        active_page = active.page(active_page["next_token"])
        active_before_reap = (active_iterator.next_calls, active_matrix.swaps)
        clock.now += 0.001

        self.assertEqual(capacity.reap_expired(), 1)
        self.assertTrue(expired_stop.is_set())
        self.assertFalse(active_stop.is_set())
        self.assertEqual(expired_iterator.close_calls, 1)
        self.assertEqual(expired.retained_frame_count, 0)
        self.assertIsNone(getattr(expired_lava, "gi_frame"))
        self.assertEqual(capacity.active_count, 1)
        self.assertEqual(capacity.available_slots, 1)
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            expired.page(stale_token)
        self.assertEqual(
            (expired_iterator.next_calls, expired_matrix.swaps),
            expired_before_reap,
        )

        replacement_stop = threading.Event()
        replacement_lava = edit_preview.continuous_lava_preview_frames(
            RecordingMatrix(), candidate,
            {"kind": "built_in", "id": "fire_burst"},
            stop_event=replacement_stop,
        )
        replacement_iterator = CloseCountingIterator(replacement_lava)
        replacement = capacity.admit(
            lambda: replacement_iterator, stop_event=replacement_stop,
        )
        self.assertEqual(capacity.active_count, 2)
        self.assertEqual(capacity.reap_expired(), 0)
        expired.cancel()
        self.assertEqual(expired_iterator.close_calls, 1)
        self.assertEqual(capacity.active_count, 2)
        self.assertEqual(
            (active_iterator.next_calls, active_matrix.swaps), active_before_reap,
        )

        active.cancel()
        replacement.cancel()
        self.assertEqual(active_iterator.close_calls, 1)
        self.assertEqual(replacement_iterator.close_calls, 1)
        self.assertEqual(capacity.active_count, 0)
        self.assertEqual(capacity.available_slots, 2)

    def test_cancel_before_first_frame_publishes_nothing(self):
        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class GuardedIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                raise AssertionError("a pre-cancelled session advanced its renderer")

            def close(self):
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"]["show_time"] = False
        candidate["lava"]["show_date"] = False
        matrix = RecordingMatrix()
        lava = edit_preview.continuous_lava_preview_frames(
            matrix, candidate, {"kind": "built_in", "id": "fire_burst"},
        )
        guarded = GuardedIterator(lava)
        cancelled = threading.Event()
        cancelled.set()
        session = PreviewSession(guarded, stop_event=cancelled)

        page = session.page()
        session.cancel()
        session.cancel()

        self.assertEqual(page["frames"], [])
        self.assertTrue(page["complete"])
        self.assertIsNone(page["next_token"])
        self.assertEqual(guarded.next_calls, 0)
        self.assertEqual(guarded.close_calls, 1)
        self.assertEqual(matrix.submissions, [])
        self.assertEqual(session.retained_frame_count, 0)
        self.assertIsNone(getattr(lava, "gi_frame"))
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            session.page(page["next_token"])

    def test_cancel_between_pages_closes_iterator_and_invalidates_token(self):
        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.swaps = 0
                self.submissions = []

            def swap(self, canvas=None):
                self.swaps += 1
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class CloseCountingIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return next(self.iterator)

            def close(self):
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False,
            "show_date": False,
            "blur_radius": 0,
            "blob_count": 2,
        })
        original_candidate = copy.deepcopy(candidate)
        matrix = RecordingMatrix()
        cancelled = threading.Event()
        lava = edit_preview.continuous_lava_preview_frames(
            matrix, candidate, {"kind": "built_in", "id": "fire_burst"},
            stop_event=cancelled,
        )
        guarded = CloseCountingIterator(lava)
        session = PreviewSession(guarded, stop_event=cancelled)
        session.MAX_FRAMES = 2

        with patch.object(
            clock_lava, "render_lava_frame", wraps=clock_lava.render_lava_frame,
        ) as render:
            page = session.page()
            token = page["next_token"]
            before_cancel = {
                "next_calls": guarded.next_calls,
                "renders": render.call_count,
                "swaps": matrix.swaps,
                "submissions": [image.tobytes() for image in matrix.submissions],
                "renderer": matrix.snapshot().tobytes(),
            }

            self.assertEqual(page["page"], 0)
            self.assertEqual(len(page["frames"]), 2)
            self.assertFalse(page["complete"])
            self.assertIsNotNone(token)
            self.assertEqual(session.retained_frame_count, 1)

            session.cancel()
            self.assertTrue(cancelled.is_set())
            self.assertEqual(guarded.close_calls, 1)
            self.assertEqual(session.retained_frame_count, 0)
            self.assertIsNone(getattr(lava, "gi_frame"))

            for stale_token in (token, None):
                with self.subTest(token=stale_token):
                    with self.assertRaisesRegex(
                        StopIteration,
                        "explicit new session is required to restart",
                    ):
                        session.page(stale_token)

            session.cancel()
            self.assertEqual(guarded.close_calls, 1)
            self.assertEqual(
                {
                    "next_calls": guarded.next_calls,
                    "renders": render.call_count,
                    "swaps": matrix.swaps,
                    "submissions": [image.tobytes() for image in matrix.submissions],
                    "renderer": matrix.snapshot().tobytes(),
                },
                before_cancel,
            )

        self.assertEqual(candidate, original_candidate)

    def test_cancel_after_decode_before_publication_publishes_nothing(self):
        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class DecodeBarrierIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.decoded = threading.Event()
                self.release = threading.Event()
                self.close_requested = False
                self.next_calls = 0
                self.close_calls = 0
                self.active = False

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                self.active = True
                try:
                    frame = next(self.iterator)
                    self.decoded.set()
                    if not self.release.wait(1):
                        raise AssertionError("decode barrier was not released")
                    if self.close_requested:
                        self.iterator.close()
                    return frame
                finally:
                    self.active = False

            def close(self):
                self.close_calls += 1
                self.close_requested = True
                if not self.active:
                    self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False,
            "show_date": False,
            "blur_radius": 0,
            "blob_count": 2,
        })
        matrix = RecordingMatrix()
        cancelled = threading.Event()
        lava = edit_preview.continuous_lava_preview_frames(
            matrix, candidate, {"kind": "built_in", "id": "fire_burst"},
            stop_event=cancelled,
        )
        guarded = DecodeBarrierIterator(lava)
        session = PreviewSession(guarded, stop_event=cancelled)
        result = {}
        failure = []

        def page_in_background():
            try:
                result.update(session.page())
            except BaseException as exc:
                failure.append(exc)

        page_thread = threading.Thread(target=page_in_background)
        page_thread.start()
        self.assertTrue(guarded.decoded.wait(1), "production decode did not reach barrier")
        session.cancel()
        self.assertTrue(cancelled.is_set())
        guarded.release.set()
        page_thread.join(0.25)

        self.assertFalse(page_thread.is_alive(), "cancellation was not acknowledged promptly")
        self.assertEqual(failure, [])
        self.assertEqual(result["frames"], [])
        self.assertTrue(result["complete"])
        self.assertIsNone(result["next_token"])
        self.assertEqual(guarded.next_calls, 1)
        self.assertEqual(guarded.close_calls, 1)
        self.assertEqual(matrix.submissions, [])
        self.assertEqual(session.retained_frame_count, 0)
        self.assertIsNone(getattr(lava, "gi_frame"))
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            session.page()

    def test_cancel_interrupts_blocked_gif_frame_copy_and_releases_capacity(self):
        entered = threading.Event()
        release = threading.Event()
        stop = threading.Event()
        result = {}

        class Frame:
            info = {"duration": 100}

            def copy(self):
                entered.set()
                release.wait()
                return Image.new("RGB", (2, 2), "red")

        opened = MagicMock()
        opened.__enter__.return_value = opened
        capacity = edit_preview.PreviewSessionCapacity()
        with (
            patch.object(Image, "open", return_value=opened),
            patch.object(ImageSequence, "Iterator", return_value=iter([Frame()])),
        ):
            session = capacity.admit(
                lambda: edit_preview.media_preview_frames(
                    CaptureMatrix(), "blocked.gif", 10, stop_event=stop,
                ),
                stop_event=stop,
            )
            thread = threading.Thread(target=lambda: result.update(session.page()))
            thread.start()
            self.assertTrue(entered.wait(1), "GIF frame copy did not block")
            session.cancel()
            thread.join(0.15)
            prompt = not thread.is_alive()
            state = session.state
            active = capacity.active_count
            release.set()
            thread.join(1)

        self.assertTrue(prompt, "GIF frame copy blocked page cancellation")
        self.assertEqual(state, "CLOSED")
        self.assertEqual(active, 0)
        self.assertEqual(result.get("frames"), [])

    def test_pinned_mp4_preserves_fixed_signature_decoder_compatibility(self):
        calls = []
        pinned = Mock()

        def legacy_decoder(path, settings, stop_event=None):
            calls.append((path, settings, stop_event))
            return iter(())

        stop = threading.Event()
        frames = list(edit_preview.media_preview_frames(
            CaptureMatrix(), "legacy.mp4", 1,
            video_decoder=legacy_decoder, stop_event=stop, pinned_handle=pinned,
        ))

        self.assertEqual(frames, [])
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][2], stop)

    def test_preview_decode_completion_cancel_race_closes_result_once(self):
        real_event = threading.Event
        checked = real_event()
        release = real_event()
        stop = real_event()
        returned = real_event()
        closed = real_event()
        event_count = [0]

        class Value:
            close_calls = 0

            def close(self):
                self.close_calls += 1
                closed.set()

        value = Value()

        class GatedAbandonedEvent:
            def __init__(self):
                self._set = real_event()

            def is_set(self):
                if threading.current_thread().name == "matrix-preview-pillow":
                    checked.set()
                    release.wait()
                    return False
                return self._set.is_set()

            def set(self):
                self._set.set()

        def event_factory():
            event_count[0] += 1
            return real_event() if event_count[0] == 1 else GatedAbandonedEvent()

        def decode():
            if event_count[0] == 1:  # Lock-based implementation: pause before transfer.
                checked.set()
                release.wait()
            return value

        threading_proxy = types.SimpleNamespace(
            Event=event_factory, Lock=threading.Lock, Thread=threading.Thread,
        )

        def call_decode():
            try:
                edit_preview._preview_decode(decode, stop, None, time.monotonic)
            except edit_preview._PreviewDecodeCancelled:
                pass
            finally:
                returned.set()

        with patch.object(edit_preview, "threading", threading_proxy):
            caller = threading.Thread(target=call_decode)
            caller.start()
            self.assertTrue(checked.wait(1), "worker did not reach transfer boundary")
            stop.set()
            self.assertTrue(returned.wait(0.15), "cancel did not return promptly")
            release.set()
            caller.join(1)
            self.assertTrue(closed.wait(1), "abandoned result was not closed")

        self.assertEqual(value.close_calls, 1)

    def test_preview_pillow_workers_are_globally_bounded(self):
        release = threading.Event()
        fifth_worker = threading.Event()
        lock = threading.Lock()
        entered = [0]
        stops = [threading.Event() for _ in range(5)]
        callers = []

        def blocked_decode():
            with lock:
                entered[0] += 1
                if entered[0] >= 5:
                    fifth_worker.set()
            release.wait()
            return Image.new("RGB", (1, 1))

        def call_decode(stop):
            try:
                edit_preview._preview_decode(
                    blocked_decode, stop, None, time.monotonic,
                )
            except edit_preview._PreviewDecodeCancelled:
                pass

        for stop in stops:
            caller = threading.Thread(target=call_decode, args=(stop,))
            callers.append(caller)
            caller.start()
        try:
            self.assertFalse(
                fifth_worker.wait(0.15),
                "a fifth pathological Pillow worker was admitted",
            )
        finally:
            for stop in stops:
                stop.set()
            for caller in callers:
                caller.join(0.25)
            release.set()
            for caller in callers:
                caller.join(1)

        self.assertLessEqual(entered[0], 4)
        self.assertFalse(any(caller.is_alive() for caller in callers))

    def test_cancel_interrupts_blocked_pinned_static_preview_decode(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(encoded, format="PNG")
        original_open = Image.open
        entered = threading.Event()
        release = threading.Event()
        stop = threading.Event()
        pinned = Mock()
        duplicate = io.BytesIO(encoded.getvalue())
        pinned.dup_file.side_effect = lambda: duplicate

        def blocked_open(source):
            entered.set()
            release.wait()
            return original_open(source)

        session = PreviewSession(edit_preview.media_preview_frames(
            CaptureMatrix(), "blocked.png", 10,
            stop_event=stop, pinned_handle=pinned,
        ), stop_event=stop)
        result = {}
        thread = threading.Thread(target=lambda: result.update(session.page()))
        with patch.object(Image, "open", side_effect=blocked_open):
            thread.start()
            self.assertTrue(entered.wait(1), "static decode did not block")
            self.assertTrue(duplicate.closed, "pinned duplicate remained owned by Pillow")
            session.cancel()
            thread.join(0.15)
            prompt = not thread.is_alive()
            release.set()
            thread.join(1)

        self.assertTrue(prompt, "static preview cancellation waited for Pillow")
        self.assertEqual(result.get("frames"), [])
        self.assertEqual(session.state, "CLOSED")

    def test_cancel_interrupts_blocked_pinned_gif_preview_decode(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(encoded, format="GIF")
        original_open = Image.open
        entered = threading.Event()
        release = threading.Event()
        stop = threading.Event()
        pinned = Mock()
        duplicate = io.BytesIO(encoded.getvalue())
        pinned.dup_file.side_effect = lambda: duplicate

        def blocked_open(source):
            entered.set()
            release.wait()
            return original_open(source)

        session = PreviewSession(edit_preview.media_preview_frames(
            CaptureMatrix(), "blocked.gif", 10,
            stop_event=stop, pinned_handle=pinned,
        ), stop_event=stop)
        result = {}
        thread = threading.Thread(target=lambda: result.update(session.page()))
        with patch.object(Image, "open", side_effect=blocked_open):
            thread.start()
            self.assertTrue(entered.wait(1), "GIF decode did not block")
            self.assertTrue(duplicate.closed, "pinned duplicate remained owned by Pillow")
            session.cancel()
            thread.join(0.15)
            prompt = not thread.is_alive()
            release.set()
            thread.join(1)

        self.assertTrue(prompt, "GIF preview cancellation waited for Pillow")
        self.assertEqual(result.get("frames"), [])
        self.assertEqual(session.state, "CLOSED")

    def test_media_preview_passes_monotonic_and_absolute_video_deadline(self):
        clock = Mock(return_value=10.0)
        decoder_options = []

        def decoder(_path, _settings, **options):
            decoder_options.append(options)
            return iter(())

        list(edit_preview.media_preview_frames(
            CaptureMatrix(), "clip.mp4", 3,
            video_decoder=decoder, stop_event=threading.Event(), monotonic=clock,
        ))

        self.assertEqual(len(decoder_options), 1)
        self.assertIs(decoder_options[0]["stop_event"].is_set(), False)
        self.assertEqual(decoder_options[0]["deadline"], 13.0)

    def test_media_preview_gif_duration_uses_monotonic_decode_time(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(encoded, format="GIF")
        pinned = Mock()
        pinned.dup_file.side_effect = lambda: io.BytesIO(encoded.getvalue())
        now = [10.0]
        original_open = Image.open

        def clock():
            return now[0]

        def consume_five_ms(source):
            now[0] = 10.005
            return original_open(source)

        with patch.object(Image, "open", side_effect=consume_five_ms):
            frames = list(edit_preview.media_preview_frames(
                CaptureMatrix(), "slow.gif", 0.006,
                pinned_handle=pinned, monotonic=clock,
            ))

        self.assertGreaterEqual(len(frames), 1)
        self.assertAlmostEqual(frames[0]["delay_ms"], 1.0, places=6)
        self.assertAlmostEqual(
            sum(frame["delay_ms"] for frame in frames), 6.0, places=6,
        )

    def test_pre_cancelled_gif_preview_skips_pinned_decode(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(encoded, format="GIF")
        pinned = Mock()
        pinned.dup_file.side_effect = lambda: io.BytesIO(encoded.getvalue())
        stop = threading.Event()
        stop.set()

        frames = list(edit_preview.media_preview_frames(
            CaptureMatrix(), "cancelled.gif", 1,
            stop_event=stop, pinned_handle=pinned,
        ))

        self.assertEqual(frames, [])
        pinned.dup_file.assert_not_called()

    def test_gif_preview_cancelled_during_pinned_decode_draws_nothing(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(encoded, format="GIF")
        pinned = Mock()
        stop = threading.Event()
        matrix = Mock()

        def cancel_during_duplication():
            stop.set()
            return io.BytesIO(encoded.getvalue())

        pinned.dup_file.side_effect = cancel_during_duplication
        frames = list(edit_preview.media_preview_frames(
            matrix, "cancelled.gif", 1,
            stop_event=stop, pinned_handle=pinned,
        ))

        self.assertEqual(frames, [])
        pinned.dup_file.assert_called_once_with()
        matrix.clear.assert_not_called()
        matrix.draw_image.assert_not_called()
        matrix.snapshot.assert_not_called()
        matrix.swap.assert_not_called()

    def test_gif_preview_cancelled_during_draw_yields_nothing(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(encoded, format="GIF")
        pinned = Mock()
        pinned.dup_file.side_effect = lambda: io.BytesIO(encoded.getvalue())
        stop = threading.Event()
        matrix = Mock()
        matrix.draw_image.side_effect = lambda *_args, **_kwargs: stop.set()

        frames = list(edit_preview.media_preview_frames(
            matrix, "cancelled.gif", 1,
            stop_event=stop, pinned_handle=pinned,
        ))

        self.assertEqual(frames, [])
        matrix.clear.assert_called_once_with()
        matrix.draw_image.assert_called_once()
        matrix.snapshot.assert_not_called()
        matrix.swap.assert_not_called()

    def test_gif_preview_cancelled_while_yielded_does_not_swap_on_resume(self):
        encoded = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(encoded, format="GIF")
        pinned = Mock()
        pinned.dup_file.side_effect = lambda: io.BytesIO(encoded.getvalue())
        stop = threading.Event()
        matrix = Mock()
        iterator = edit_preview.media_preview_frames(
            matrix, "cancelled.gif", 1,
            stop_event=stop, pinned_handle=pinned,
        )

        first = next(iterator)
        self.assertIn("png", first)
        stop.set()
        with self.assertRaises(StopIteration):
            next(iterator)

        matrix.swap.assert_not_called()

    def test_pre_cancelled_static_preview_skips_pinned_decode(self):
        pinned = Mock()
        stop = threading.Event()
        stop.set()

        frames = list(edit_preview.media_preview_frames(
            CaptureMatrix(), "cancelled.png", 1,
            stop_event=stop, pinned_handle=pinned,
        ))

        self.assertEqual(frames, [])
        pinned.dup_file.assert_not_called()

    def test_static_preview_cancelled_during_decode_draws_nothing(self):
        matrix = Mock()
        pinned = Mock()
        media_file = MagicMock()
        media_file.__enter__.return_value.read.return_value = b"pinned-static"
        pinned.dup_file.return_value = media_file
        opened = MagicMock()
        stop = threading.Event()

        def cancel_during_copy():
            stop.set()
            return Image.new("RGB", (2, 2), "blue")

        opened.__enter__.return_value.copy.side_effect = cancel_during_copy
        with patch.object(Image, "open", return_value=opened):
            frames = list(edit_preview.media_preview_frames(
                matrix, "cancelled.png", 1,
                stop_event=stop, pinned_handle=pinned,
            ))

        self.assertEqual(frames, [])
        matrix.clear.assert_not_called()
        matrix.draw_image.assert_not_called()
        matrix.snapshot.assert_not_called()
        matrix.swap.assert_not_called()

    def test_static_preview_cancelled_during_draw_yields_nothing(self):
        matrix = Mock()
        stop = threading.Event()
        opened = MagicMock()
        opened.__enter__.return_value.copy.return_value = Image.new(
            "RGB", (2, 2), "green"
        )
        matrix.draw_image.side_effect = lambda *_args, **_kwargs: stop.set()

        with patch.object(Image, "open", return_value=opened):
            frames = list(edit_preview.media_preview_frames(
                matrix, "cancelled.png", 1, stop_event=stop,
            ))

        self.assertEqual(frames, [])
        matrix.clear.assert_called_once_with()
        matrix.draw_image.assert_called_once()
        matrix.snapshot.assert_not_called()
        matrix.swap.assert_not_called()

    def test_media_decode_cancel_closes_decoder_within_250_ms(self):
        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class BlockedDecoder:
            def __init__(self):
                self.started = threading.Event()
                self.stop_observed = threading.Event()
                self.released = threading.Event()
                self.close_calls = 0
                self.read_fd, self.write_fd = os.pipe()
                self.stop_event = None

            def __call__(self, _path, _settings, *, stop_event=None):
                self.stop_event = stop_event
                return self

            def __iter__(self):
                return self

            def __next__(self):
                self.started.set()
                while not self.released.wait(0.01):
                    if self.stop_event is not None and self.stop_event.is_set():
                        self.stop_observed.set()
                        raise StopIteration
                raise AssertionError("blocked decoder required emergency release")

            def close(self):
                self.close_calls += 1
                os.close(self.read_fd)
                os.close(self.write_fd)

        matrix = RecordingMatrix()
        decoder = BlockedDecoder()
        cancelled = threading.Event()
        result = {}
        failure = []
        session = PreviewSession(
            edit_preview.media_preview_frames(
                matrix, "blocked.mp4", 10,
                settings={
                    "x": 0, "y": 0, "width": 64, "height": 32,
                    "keep_aspect": True, "video_fps": 20,
                },
                video_decoder=decoder,
                stop_event=cancelled,
            ),
            stop_event=cancelled,
        )

        def page_in_background():
            try:
                result.update(session.page())
            except BaseException as exc:
                failure.append(exc)

        page_thread = threading.Thread(
            target=page_in_background, name="blocked-preview-page",
        )
        page_thread.start()
        try:
            self.assertTrue(decoder.started.wait(1), "media decode did not block")
            started = time.monotonic()
            session.cancel()
            page_thread.join(0.25)
            elapsed = time.monotonic() - started
        finally:
            decoder.released.set()
            page_thread.join(1)

        self.assertLess(elapsed, 0.25)
        self.assertFalse(page_thread.is_alive(), "preview page did not settle within 250 ms")
        self.assertTrue(cancelled.is_set())
        self.assertTrue(decoder.stop_observed.is_set())
        self.assertEqual(failure, [])
        self.assertEqual(result["frames"], [])
        self.assertTrue(result["complete"])
        self.assertIsNone(result["next_token"])
        self.assertEqual(matrix.submissions, [])
        self.assertEqual(session.retained_frame_count, 0)
        self.assertEqual(decoder.close_calls, 1)
        for descriptor in (decoder.read_fd, decoder.write_fd):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertNotIn(page_thread, threading.enumerate())
        session.cancel()
        self.assertEqual(decoder.close_calls, 1)
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            session.page()

    def test_duplicate_disabled_and_missing_playlist_rows_choose_documented_duration(self):
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["custom_modules"] = [
            {"id": "custom_simple", "type": "text_scroll"},
            {"id": "custom_canvas", "type": "advanced_canvas"},
        ]
        candidate["playlist"] = [
            {"module": "weather", "duration": 91, "enabled": False},
            {"module": "weather", "duration": 0, "enabled": True},
            {"module": "weather", "duration": 92, "enabled": True},
            {"module": "clock_lava", "duration": 93, "enabled": False},
            {"module": "custom_simple", "duration": 7, "enabled": True},
            {"module": "custom_simple", "duration": 8, "enabled": True},
            {"module": "announcement_board", "duration": 9, "enabled": True},
            {"module": "media:example.gif", "duration": 10, "enabled": False},
        ]
        original = copy.deepcopy(candidate)

        expected = {
            # First enabled duplicate wins and zero remains a real physical duration.
            "weather": 0,
            "custom_simple": 7,
            "announcement_board": 9,
            # Disabled-only and missing rows use current physical module defaults.
            "clock_lava": 300,
            "openweather_overview": 20,
            "openweather_forecast": 30,
            "custom_canvas": 15,
            "splash": 8,
            "sleep": 60,
            "media:example.gif": 15,
        }
        for module_name, duration in expected.items():
            with self.subTest(module=module_name):
                self.assertEqual(
                    edit_preview.preview_duration(candidate, module_name), duration,
                )

        self.assertEqual(candidate, original)
        self.assertFalse(candidate["playlist"][0]["enabled"])
        self.assertTrue(candidate["playlist"][1]["enabled"])
        self.assertTrue(candidate["playlist"][2]["enabled"])
        self.assertFalse(candidate["playlist"][-1]["enabled"])

    def test_lava_preview_pages_only_selected_palette_until_cancelled(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 15, 9, 30, tzinfo=tz)

        cases = (
            (
                "disabled built-in",
                {"kind": "built_in", "id": "fire_burst"},
                {"#000000", "#ff0000", "#ffff00"},
            ),
            (
                "disabled custom with edited colours",
                {
                    "kind": "custom", "id": "palette_inspected",
                    "colours": ["#102030", "#405060", "#90a0b0"],
                },
                {"#102030", "#405060", "#90a0b0"},
            ),
        )
        for label, descriptor, expected_colours in cases:
            with self.subTest(case=label):
                candidate = copy.deepcopy(DEFAULT_CONFIG)
                candidate["lava"].update({
                    "enabled_palettes": ["ocean_wave"],
                    "custom_enabled": False,
                    "custom_palettes": [{
                        "id": "palette_inspected", "name": "Inspected",
                        "colours": ["#102030", "#405060", "#90a0b0"],
                        "enabled": False,
                    }],
                    "blur_radius": 0,
                    "show_time": False,
                    "show_date": False,
                    "blob_count": 3,
                    "blob_speed": 0.5,
                })
                original = copy.deepcopy(candidate)

                with patch.object(frame_capture_module, "Image", Image), \
                     patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
                     patch("modules.clock_lava.random.uniform", side_effect=random.Random(17).uniform):
                    iterator = edit_preview.continuous_lava_preview_frames(
                        CaptureMatrix(), candidate, descriptor,
                    )
                    session = PreviewSession(iterator)
                    session.MAX_FRAMES = 2
                    first = session.page()
                    second = session.page(first["next_token"])

                pages = (first, second)
                frames = [frame for page in pages for frame in page["frames"]]
                self.assertEqual([page["page"] for page in pages], [0, 1])
                self.assertTrue(all(not page["complete"] for page in pages))
                self.assertTrue(all(page["next_token"] for page in pages))
                self.assertEqual([frame["delay_ms"] for frame in frames], [30] * 4)
                decoded = [
                    Image.open(io.BytesIO(base64.b64decode(frame["png"]))).convert("RGB")
                    for frame in frames
                ]
                self.assertTrue(all(image.size == (64, 32) for image in decoded))
                self.assertEqual(len({image.tobytes() for image in decoded}), len(decoded))
                observed = {
                    "#%02x%02x%02x" % image.getpixel((x, y))
                    for image in decoded
                    for y in range(image.height)
                    for x in range(image.width)
                }
                self.assertTrue(observed <= expected_colours)
                self.assertGreaterEqual(len(observed), 2)
                self.assertEqual(candidate, original)

                token = second["next_token"]
                session.cancel()
                with self.assertRaises(StopIteration):
                    session.page(token)
                self.assertIsNone(getattr(iterator, "gi_frame"))
                self.assertEqual(session.retained_frame_count, 0)

    def test_lava_playlist_duration_and_default_preserve_physical_wrapper_semantics(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 15, 9, 30, tzinfo=tz)

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["lava"].update({
            "enabled_palettes": [],
            "custom_enabled": True,
            "custom_colours": ["#001122", "#ff5500", "#ffee00"],
            "show_time": False,
            "show_date": False,
            "text_colour": "#ffffff",
            "border_colour": "#000000",
            "blob_count": 2,
        })
        config["playlist"] = [
            {"module": "clock_lava", "duration": 0.05, "enabled": True},
            {"module": "clock_lava", "duration": 0.09, "enabled": True},
        ]
        original_lava = copy.deepcopy(config["lava"])
        original_playlist = copy.deepcopy(config["playlist"])

        with patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
             patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: (low + high) / 2):
            frames = list(edit_preview.lava_preview_frames(CaptureMatrix(), config))

        self.assertEqual([frame["delay_ms"] for frame in frames], [30, 20])
        self.assertEqual(config["lava"], original_lava)
        self.assertEqual(config["playlist"], original_playlist)

        disabled = copy.deepcopy(config)
        disabled["playlist"] = [
            {"module": "clock_lava", "duration": 1, "enabled": False},
        ]
        self.assertEqual(
            edit_preview.preview_duration(disabled, "clock_lava"), 300,
        )
        self.assertFalse(disabled["playlist"][0]["enabled"])

        with patch.object(clock_lava, "FRAME_DELAY", 100), \
             patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
             patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: (low + high) / 2):
            default_frames = list(edit_preview.lava_preview_frames(CaptureMatrix(), disabled))
        self.assertEqual([frame["delay_ms"] for frame in default_frames], [100000, 100000, 100000])
        self.assertFalse(disabled["playlist"][0]["enabled"])

        physical_matrix = CaptureMatrix()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(clock_lava, "render_lava_frame", wraps=clock_lava.render_lava_frame) as shared, \
             patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
             patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: (low + high) / 2), \
             patch("modules.clock_lava.time.time", side_effect=[0, 0]):
            clock_lava.clock(
                duration=1, matrix=physical_matrix, stop_event=stop,
                lava_config=config["lava"],
            )
        self.assertEqual(shared.call_count, 1)
        self.assertEqual(clock_lava.clock.__defaults__, (300, None, None, None))
        self.assertEqual(clock_lava.run.__defaults__, (300, None))

    def test_finite_cycle_can_restart_only_after_terminal_page(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        custom_options = {
            "type": "text_scroll",
            "text": "RESTART",
            "font": "5x7",
            "colour": "#ffffff",
            "background": "#001122",
            "y": 20,
            "direction": "left",
            "speed": 0.05,
            "loops": 1,
        }
        announcement = {
            "id": "announcement_restart",
            "enabled": True,
            "name": "Restart lifecycle",
            "layout": "text_notice",
            "headline": "Welcome",
            "message": "Terminal page required",
            "media": None,
            "schedule": {
                "start_date": "2030-01-15",
                "end_date": "2030-01-15",
                "start_time": "09:00",
                "end_time": "17:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
            },
            "style": {
                "foreground": "#ffffff",
                "background": "#001122",
                "font": "5x7",
                "alignment": "left",
                "border": "none",
                "border_colour": "#ffffff",
            },
            "timing": {"mode": "static", "duration_seconds": 1.2},
            "transition": {"type": "fade", "duration_seconds": 0.4},
        }
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["announcement_board"]["announcements"] = [announcement]
        validate_config(validated, enforce_save_bounds=True)
        announcement = validated["announcement_board"]["announcements"][0]

        first = Image.new("RGB", (2, 2), "red")
        second = Image.new("RGB", (2, 2), "blue")
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "restart.gif"
            first.save(
                media_path, format="GIF", save_all=True, append_images=[second],
                duration=[100, 200], loop=0,
            )
            cases = {
                "custom_text": lambda: custom_text_preview_frames(
                    CaptureMatrix(), custom_options,
                ),
                "announcement": lambda: static_announcement_preview_frames(
                    CaptureMatrix(), announcement,
                ),
                "media": lambda: edit_preview.media_preview_frames(
                    CaptureMatrix(), media_path, 0.32,
                    settings={
                        "x": 0, "y": 0, "width": 64, "height": 32,
                        "keep_aspect": True, "gif_speed_percent": 200,
                    },
                ),
            }

            for name, iterator_factory in cases.items():
                with self.subTest(iterator=name):
                    session = PreviewSession(iterator_factory())
                    session.MAX_FRAMES = 2
                    first_page = session.page()
                    self.assertFalse(first_page["complete"])
                    first_token = first_page["next_token"]

                    with self.assertRaises(RuntimeError):
                        session.restart(iterator_factory)
                    with self.assertRaises(ValueError):
                        session.page("out-of-order-token")

                    pages = [first_page]
                    token = first_token
                    while True:
                        page = session.page(token)
                        pages.append(page)
                        if page["complete"]:
                            break
                        if len(pages) == 2:
                            with self.assertRaises(ValueError):
                                session.page(first_token)
                        token = page["next_token"]

                    self.assertEqual(
                        [page["page"] for page in pages], list(range(len(pages))),
                    )
                    continuation_tokens = [
                        page["next_token"] for page in pages[:-1]
                    ]
                    self.assertEqual(
                        len(continuation_tokens), len(set(continuation_tokens)),
                    )
                    self.assertIsNone(pages[-1]["next_token"])
                    with self.assertRaises(StopIteration):
                        session.page(token)

                    restarted = session.restart(iterator_factory)
                    restarted.MAX_FRAMES = 2
                    restarted_pages = []
                    restarted_token = None
                    while True:
                        restarted_page = restarted.page(restarted_token)
                        restarted_pages.append(restarted_page)
                        if restarted_page["complete"]:
                            break
                        restarted_token = restarted_page["next_token"]

                    self.assertIsNot(restarted, session)
                    self.assertEqual(restarted_pages[0]["page"], 0)
                    self.assertEqual(
                        restarted_pages[0]["frames"][0], first_page["frames"][0],
                    )
                    self.assertEqual(
                        [frame for page in restarted_pages for frame in page["frames"]],
                        [frame for page in pages for frame in page["frames"]],
                    )
                    self.assertTrue(restarted_pages[-1]["complete"])

    def test_media_timing_and_loops_complete_before_terminal_page(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        first = Image.new("RGB", (2, 2), "red")
        second = Image.new("RGB", (2, 2), "blue")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.gif"
            first.save(
                path, format="GIF", save_all=True, append_images=[second],
                duration=[100, 200], loop=0,
            )

            fake_graphics = types.SimpleNamespace(
                Color=lambda *rgb: types.SimpleNamespace(
                    red=rgb[0], green=rgb[1], blue=rgb[2],
                ),
                Font=Mock,
            )
            fake_rgb = types.SimpleNamespace(
                RGBMatrix=Mock,
                RGBMatrixOptions=Mock,
                graphics=fake_graphics,
            )
            previous = sys.modules.get("rgbmatrix")
            sys.modules["rgbmatrix"] = fake_rgb
            try:
                framework = importlib.import_module("matrix_framework")
            finally:
                if previous is None:
                    sys.modules.pop("rgbmatrix", None)
                else:
                    sys.modules["rgbmatrix"] = previous

            class Canvas:
                def SetImage(self, image, x, y):
                    pass

                def Clear(self):
                    pass

            class PhysicalMatrix(framework.Matrix):
                def __init__(self):
                    self.matrix = types.SimpleNamespace(width=64, height=32)
                    self.canvas = Canvas()
                    self._frame_capture = FrameCapture(64, 32)
                    self._frame_capture.register(self.canvas)
                    self.submissions = []

                def _media_file(self, filename):
                    return path

                def swap(self, canvas=None):
                    self.submissions.append(self._frame_capture.snapshot(self.canvas))
                    return self.canvas

            class AdvancingStop:
                def __init__(self):
                    self.elapsed = 0.0
                    self.waits = []

                def is_set(self):
                    return False

                def wait(self, delay):
                    self.waits.append(delay)
                    self.elapsed += delay
                    return False

            physical = PhysicalMatrix()
            stop = AdvancingStop()
            with patch.object(
                framework.time, "monotonic", side_effect=lambda: stop.elapsed,
            ), patch.object(
                framework.Image, "open", side_effect=Image.open,
            ), patch.object(
                framework.ImageSequence, "Iterator", side_effect=ImageSequence.Iterator,
            ):
                self.assertTrue(
                    play_media(
                        physical, str(path), 0.32, stop_event=stop,
                        settings={
                            "x": 0, "y": 0, "width": 64, "height": 32,
                            "keep_aspect": True, "gif_speed_percent": 200,
                            "video_fps": 20,
                        },
                    )
                )

            session = PreviewSession(
                edit_preview.media_preview_frames(
                    CaptureMatrix(), str(path), 0.32,
                    settings={
                        "x": 0, "y": 0, "width": 64, "height": 32,
                        "keep_aspect": True, "gif_speed_percent": 200,
                        "video_fps": 20,
                    },
                )
            )
            session.MAX_FRAMES = 2
            pages = []
            token = None
            while True:
                page = session.page(token)
                pages.append(page)
                if page["complete"]:
                    break
                token = page["next_token"]

        frames = [frame for page in pages for frame in page["frames"]]
        preview_images = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
            for frame in frames
        ]
        self.assertEqual([page["page"] for page in pages], [0, 1, 2])
        self.assertTrue(all(not page["complete"] for page in pages[:-1]))
        self.assertTrue(pages[-1]["complete"])
        self.assertIsNone(pages[-1]["next_token"])
        self.assertEqual([frame["delay_ms"] for frame in frames], [50, 100, 50, 100, 20])
        self.assertAlmostEqual(sum(stop.waits), 0.32)
        self.assertEqual(
            [image.tobytes() for image in preview_images],
            [image.tobytes() for image in physical.submissions],
        )
        self.assertEqual(
            [image.getpixel((0, 0)) for image in preview_images],
            [(255, 0, 0), (0, 0, 255), (255, 0, 0), (0, 0, 255), (255, 0, 0)],
        )
        with self.assertRaises(StopIteration):
            session.page()

    def test_gif_and_mp4_speed_repeat_and_final_delay_match_physical_invocation(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        fake_graphics = types.SimpleNamespace(
            Color=lambda *rgb: types.SimpleNamespace(
                red=rgb[0], green=rgb[1], blue=rgb[2],
            ),
            Font=Mock,
        )
        fake_rgb = types.SimpleNamespace(
            RGBMatrix=Mock, RGBMatrixOptions=Mock, graphics=fake_graphics,
        )
        previous = sys.modules.get("rgbmatrix")
        sys.modules["rgbmatrix"] = fake_rgb
        try:
            framework = importlib.import_module("matrix_framework")
        finally:
            if previous is None:
                sys.modules.pop("rgbmatrix", None)
            else:
                sys.modules["rgbmatrix"] = previous

        class Canvas:
            def SetImage(self, image, x, y):
                pass

            def Clear(self):
                pass

        class PhysicalMatrix(framework.Matrix):
            def __init__(self, paths, video_frames):
                self.paths = paths
                self.video_frames = video_frames
                self.video_decode_calls = 0
                self.matrix = types.SimpleNamespace(width=64, height=32)
                self.canvas = Canvas()
                self._frame_capture = FrameCapture(64, 32)
                self._frame_capture.register(self.canvas)
                self.submissions = []

            def _media_file(self, filename):
                return self.paths[filename]

            def _video_frame_timeline(self, *args, **kwargs):
                self.video_decode_calls += 1
                return iter(frame.copy() for frame in self.video_frames)

            def swap(self, canvas=None):
                self.submissions.append(self._frame_capture.snapshot(self.canvas))
                return self.canvas

        class AdvancingStop:
            def __init__(self):
                self.elapsed = 0.0
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                self.elapsed += delay
                return False

        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": False, "gif_speed_percent": 200,
            "video_fps": 20,
        }
        red = Image.new("RGB", (64, 32), "red")
        blue = Image.new("RGB", (64, 32), "blue")
        with tempfile.TemporaryDirectory() as directory:
            gif_path = Path(directory) / "timeline.gif"
            mp4_path = Path(directory) / "timeline.mp4"
            red.save(
                gif_path, format="GIF", save_all=True, append_images=[blue],
                duration=[100, 200], loop=0,
            )
            mp4_path.write_bytes(b"deterministic injected decoder input")

            for filename, duration, expected_delays, decoded in (
                ("timeline.gif", 0.32, [50, 100, 50, 100, 20], []),
                ("timeline.mp4", 0.23, [50, 50, 50, 50, 30], [red, blue]),
            ):
                with self.subTest(media=filename):
                    physical = PhysicalMatrix(
                        {filename: gif_path if filename.endswith(".gif") else mp4_path},
                        decoded,
                    )
                    stop = AdvancingStop()
                    playback_patches = [
                        patch.object(
                            framework.time, "monotonic",
                            side_effect=lambda: stop.elapsed,
                        ),
                        patch.object(framework.Image, "open", side_effect=Image.open),
                        patch.object(
                            framework.ImageSequence, "Iterator",
                            side_effect=ImageSequence.Iterator,
                        ),
                    ]
                    if filename.endswith(".mp4"):
                        playback_patches.append(patch.object(
                            framework.subprocess, "Popen",
                            side_effect=AssertionError(
                                "shared injected video decoder seam was not consumed"
                            ),
                        ))
                    with playback_patches[0], playback_patches[1], playback_patches[2]:
                        if len(playback_patches) == 4:
                            playback_patches[3].start()
                        try:
                            self.assertTrue(play_media(
                                physical, filename, duration, stop_event=stop,
                                settings=settings,
                            ))
                        finally:
                            if len(playback_patches) == 4:
                                playback_patches[3].stop()

                    preview = CaptureMatrix()
                    preview_decode_calls = [0]

                    def preview_video_decoder(*_args, **_kwargs):
                        preview_decode_calls[0] += 1
                        return iter(frame.copy() for frame in decoded)

                    session = PreviewSession(edit_preview.media_preview_frames(
                        preview,
                        gif_path if filename.endswith(".gif") else mp4_path,
                        duration,
                        settings=settings,
                        video_decoder=(
                            None if filename.endswith(".gif") else
                            preview_video_decoder
                        ),
                    ))
                    session.MAX_FRAMES = 2
                    pages = []
                    token = None
                    while True:
                        page = session.page(token)
                        pages.append(page)
                        if page["complete"]:
                            break
                        token = page["next_token"]

                    frames = [frame for page in pages for frame in page["frames"]]
                    preview_images = [
                        Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
                        for frame in frames
                    ]
                    self.assertEqual(
                        [frame["delay_ms"] for frame in frames], expected_delays,
                    )
                    self.assertEqual(
                        [round(delay * 1000, 9) for delay in stop.waits],
                        expected_delays,
                    )
                    self.assertAlmostEqual(sum(stop.waits), duration)
                    if filename.endswith(".mp4"):
                        self.assertEqual(physical.video_decode_calls, 3)
                        self.assertEqual(preview_decode_calls[0], 3)
                    self.assertEqual(
                        [image.tobytes() for image in preview_images],
                        [image.tobytes() for image in physical.submissions],
                    )
                    self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                    self.assertTrue(pages[-1]["complete"])
                    self.assertIsNone(pages[-1]["next_token"])
                    with self.assertRaises(StopIteration):
                        session.page()

    def test_advanced_canvas_hold_repeat_and_ping_pong_match_physical_invocation(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        media_frames = [
            Image.new("RGB", (64, 32), colour)
            for colour in ("#220000", "#002200", "#000022")
        ]
        layer = {
            "id": "layer_media", "name": "Media", "type": "media", "visible": True,
            "properties": {
                "source": "timeline.gif", "x": 0, "y": 0, "width": 64,
                "height": 32, "rotation": 0, "keep_aspect": False,
            },
            "keyframes": [],
        }
        keyframed = {
            "id": "layer_keyframed", "name": "Keyframed", "type": "rectangle",
            "visible": True,
            "properties": {
                "x": 0, "y": 0, "width": 2, "height": 2, "rotation": 0,
                "colour": "#ff0000", "filled": True,
            },
            "keyframes": [
                {"frame": 0, "easing": "linear", "properties": {"x": 0, "colour": "#ff0000"}},
                {"frame": 2, "easing": "linear", "properties": {"x": 4, "colour": "#0000ff"}},
            ],
        }

        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []
                self.decode_calls = []

            def media_frames(self, source, frame_count, fps, **kwargs):
                self.decode_calls.append((source, frame_count, fps, kwargs))
                return [image.copy() for image in media_frames]

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class AdvancingStop:
            def __init__(self):
                self.elapsed = 0.0
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                self.elapsed += delay
                return False

        for playback, duration, expected_indices in (
            ("hold", 1.1, [0, 1, 2, 2, 2]),
            ("loop", 1.85, [0, 1, 2, 0, 1, 2, 0, 1]),
            ("ping_pong", 2.1, [0, 1, 2, 1, 0, 1, 2, 1, 0]),
        ):
            with self.subTest(playback=playback):
                module = {
                    "id": "custom_canvas_timeline", "name": "Canvas timeline",
                    "type": "canvas", "background": "#000000", "frame_count": 3,
                    "fps": 4, "playback": playback, "layers": [layer, keyframed],
                    "overrides": [{
                        "frame": 1, "layer_id": "layer_keyframed",
                        "properties": {"y": 3},
                    }],
                }
                candidate = copy.deepcopy(DEFAULT_CONFIG)
                candidate["custom_modules"] = [copy.deepcopy(module)]
                candidate["playlist"] = [{
                    "module": module["id"], "duration": duration, "enabled": True,
                }]
                validate_config(candidate, enforce_save_bounds=True)
                module = candidate["custom_modules"][0]

                physical = RecordingMatrix()
                stop = AdvancingStop()
                with patch(
                    "modules.custom_text.time.monotonic", side_effect=lambda: stop.elapsed,
                ):
                    self.assertTrue(custom_text.run(
                        physical, candidate, duration=duration, stop_event=stop,
                        module_config=module,
                    ))

                preview = RecordingMatrix()
                session = PreviewSession(edit_preview.advanced_canvas_preview_frames(
                    preview, module, duration,
                ))
                session.MAX_FRAMES = 2
                pages = []
                token = None
                while True:
                    page = session.page(token)
                    pages.append(page)
                    if page["complete"]:
                        break
                    token = page["next_token"]

                frames = [frame for page in pages for frame in page["frames"]]
                preview_images = [
                    Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
                    for frame in frames
                ]
                expected_delays = [250] * (len(expected_indices) - 1) + [
                    round((duration - 0.25 * (len(expected_indices) - 1)) * 1000, 9)
                ]
                self.assertEqual([frame["delay_ms"] for frame in frames], expected_delays)
                self.assertEqual(
                    [round(delay * 1000, 9) for delay in stop.waits], expected_delays,
                )
                self.assertAlmostEqual(sum(stop.waits), duration)
                self.assertEqual(
                    [image.tobytes() for image in preview_images],
                    [image.tobytes() for image in physical.submissions],
                )
                signatures = [preview_images[index].tobytes() for index in range(3)]
                self.assertEqual(
                    [signatures.index(image.tobytes()) for image in preview_images],
                    expected_indices,
                )
                self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                self.assertTrue(pages[-1]["complete"])
                self.assertIsNone(pages[-1]["next_token"])
                self.assertEqual(len(physical.decode_calls), 1)
                self.assertEqual(len(preview.decode_calls), 1)
                self.assertEqual(physical.decode_calls[0][:3], ("timeline.gif", 3, 4))
                with self.assertRaises(StopIteration):
                    session.page()

        self.assertEqual(custom_text.run.__defaults__, (15, None, None))

    def test_capture_adapter_matches_physical_capture_for_64_by_32_primitives(self):
        font_path = Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf"
        font = BdfFont(font_path)
        colour = Colour(255, 0, 0)
        image = Image.new("RGB", (2, 2), (0, 255, 0))

        physical = FrameCapture(64, 32)
        canvas = object()
        physical.register(canvas)
        physical.fill(canvas, 1, 2, 3)
        physical.pixel(canvas, 0, 0, 4, 5, 6)
        physical.pixel(canvas, 63, 31, 7, 8, 9)
        physical.pixel(canvas, 64, 32, 255, 255, 255)
        physical.line(canvas, -2, 4, 4, 4, colour, width=2)
        physical.circle(canvas, 10, 10, 3, colour, filled=True)
        physical.set_image(canvas, image, 63, 30)
        expected_advance = physical.text(canvas, font, 1, 20, colour, "!")
        advance, points = font.text_pixels(24, 20, "!")
        centre_x = (min(x for x, _ in points) + max(x for x, _ in points)) / 2
        centre_y = (min(y for _, y in points) + max(y for _, y in points)) / 2
        for x, y in points:
            physical.pixel(
                canvas,
                round(centre_x - (y - centre_y)),
                round(centre_y + (x - centre_x)),
                colour.red,
                colour.green,
                colour.blue,
            )
        physical.publish(canvas)
        expected = Image.open(physical.png()).copy()

        preview = CaptureMatrix(64, 32)
        preview.fill(1, 2, 3)
        preview.pixel(0, 0, 4, 5, 6)
        preview.pixel(63, 31, 7, 8, 9)
        preview.pixel(64, 32, 255, 255, 255)
        preview.draw_line(-2, 4, 4, 4, colour, width=2)
        preview.draw_circle(10, 10, 3, colour, filled=True)
        preview.draw_image(image, x=63, y=30, keep_aspect=False)
        actual_advance = preview.draw_text(font, 1, 20, colour, "!")
        preview.draw_text(font, 24, 20, colour, "!", rotation=90)

        self.assertEqual(actual_advance, expected_advance)
        self.assertEqual(preview.snapshot().tobytes(), expected.tobytes())
        self.assertEqual(preview.snapshot().getpixel((0, 0)), (4, 5, 6))
        self.assertEqual(preview.snapshot().getpixel((63, 31)), (0, 255, 0))

    def test_physical_wrappers_and_preview_consume_the_same_frame_iterators(self):
        class FakeMatrix:
            def __init__(self):
                self.shown = []

            def show_image(self, filename, delay=None, *, stop_event=None, **geometry):
                self.shown.append((filename, delay, geometry))
                return True

        class FakeStopEvent:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return False

        filename = "resources/media/still.png"
        settings = {"x": 2, "y": 3, "width": 20, "height": 10, "keep_aspect": True}
        expected = list(preview_frames(static_media_frames(filename, 1.25, settings)))
        matrix = FakeMatrix()
        stop_event = FakeStopEvent()

        with patch("modules.media_player.time.monotonic", return_value=0.0):
            rendered = play_media(
                matrix, filename, 1.25, stop_event=stop_event, settings=settings,
            )

        self.assertEqual(expected, [(filename, 1250.0, settings)])
        self.assertTrue(rendered)
        self.assertEqual(matrix.shown, [(filename, None, settings)])
        self.assertEqual(stop_event.waits, [expected[0][1] / 1000])

    def test_weather_variants_preserve_direction_loop_count_and_forecast_cards(self):
        from modules.openweather_common import (
            OpenWeatherForecastPeriod, OpenWeatherSnapshot,
        )
        from modules.openweather_forecast import PreparedForecast
        from modules.openweather_overview import PreparedOverview
        from modules.weather_app import WeatherSnapshot
        from modules import openweather_forecast, openweather_overview, weather_app

        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.positions = []
                self.labels = []
                self.measurements = []
                self.submissions = []

            def colour(self, name):
                colours = {
                    "retro_soft_grey": "#dcd5d5", "black": "#000000",
                    "vapor_pink": "#ff71ce", "vapor_cyan": "#01cdfe",
                    "neon_yellow": "#ffb300", "retro_aqua": "#00ffff",
                }
                return self.colour_rgb(colours[name])

            def draw_text_with_border(
                self, font, x, y, fill, border, text, width=1, canvas=None,
            ):
                for dx in range(-width, width + 1):
                    for dy in range(-width, width + 1):
                        if dx or dy:
                            self.draw_text(font, x + dx, y + dy, border, text, canvas=canvas)
                self.labels.append(text)
                return self.draw_text(font, x, y, fill, text, canvas=canvas)

            def draw_text(self, font, x, y, colour, text, canvas=None, rotation=0):
                result = super().draw_text(font, x, y, colour, text, canvas=canvas, rotation=rotation)
                if x == -10000:
                    self.measurements.append(result - x)
                return result

            def widget_scroll(
                self, text, font, x, y, colour, canvas=None, step=1, wrap=True,
            ):
                self.positions.append(x)
                text_end = self.draw_text(font, x, y, colour, text, canvas=canvas)
                return x - step, text_end - x

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class RecordingStop:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return False

        weather = WeatherSnapshot(
            location="Hove", status="Clear", temperature=16, feels_like=15,
            high=18, low=12, humidity=70,
        )
        current = OpenWeatherSnapshot(
            location="Hove", status="Cloudy", temperature=17, feels_like=16,
            high=19, low=13, humidity=68, icon_code="04d",
        )
        periods = tuple(
            OpenWeatherForecastPeriod(
                label=f"Tue {hour}:00", status=status, temperature=temperature,
                feels_like=temperature - 1, high=temperature + 2,
                low=temperature - 3, icon_code=icon_code,
            )
            for hour, status, temperature, icon_code in (
                ("15", "Rain", 18, "10d"),
                ("18", "Clear", 16, "01n"),
            )
        )
        icon = Image.new("RGB", (20, 20), "blue")

        class PreparedWeatherClient:
            def fetch(self, *_args, **_kwargs):
                return weather

        cases = (
            (
                "weather", weather_app.run, edit_preview.weather_preview_frames,
                {"weather": {"scroll_direction": "right", "scroll_speed": 0.075,
                             "scroll_loops": 2}}, weather,
                {"client": PreparedWeatherClient()},
            ),
            (
                "overview", openweather_overview.run,
                edit_preview.openweather_overview_preview_frames,
                {"weather": {"location": "Hove"},
                 "openweather_overview": {"scroll_direction": "left",
                     "scroll_speed": 0.05, "scroll_loops": 2}},
                PreparedOverview(weather=current, icon=icon), {"prepared": PreparedOverview(weather=current, icon=icon)},
            ),
            (
                "forecast", openweather_forecast.run,
                edit_preview.openweather_forecast_preview_frames,
                {"weather": {"location": "Hove"},
                 "openweather_forecast": {"scroll_direction": "right",
                     "scroll_speed": 0.025, "scroll_loops": 2}},
                PreparedForecast(
                    periods=periods,
                    icons={period.icon_code: icon for period in periods},
                ),
                {"prepared": PreparedForecast(
                    periods=periods,
                    icons={period.icon_code: icon for period in periods},
                )},
            ),
        )

        for name, physical_run, preview_factory, config, prepared, physical_options in cases:
            with self.subTest(variant=name):
                physical = RecordingMatrix()
                stop = RecordingStop()
                self.assertTrue(physical_run(
                    physical, config, duration=999, stop_event=stop,
                    **physical_options,
                ))

                preview = RecordingMatrix()
                session = PreviewSession(preview_factory(preview, config, prepared))
                session.MAX_FRAMES = 7
                pages = []
                token = None
                while True:
                    page = session.page(token)
                    pages.append(page)
                    if page["complete"]:
                        break
                    token = page["next_token"]

                frames = [frame for page in pages for frame in page["frames"]]
                preview_images = [
                    Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
                    for frame in frames
                ]
                direction = config[
                    "weather" if name == "weather" else f"openweather_{name}"
                ]["scroll_direction"]
                loops = config[
                    "weather" if name == "weather" else f"openweather_{name}"
                ]["scroll_loops"]
                expected_positions = []
                for text_width in preview.measurements:
                    one_loop = (
                        list(range(-text_width, preview.width + 1))
                        if direction == "right"
                        else list(range(preview.width, -text_width - 1, -1))
                    )
                    expected_positions.extend(one_loop * loops)

                self.assertEqual(preview.positions, expected_positions)
                self.assertEqual(physical.positions, expected_positions)
                self.assertEqual(len(frames), len(expected_positions))
                self.assertEqual(
                    [image.tobytes() for image in preview_images],
                    [image.tobytes() for image in physical.submissions],
                )
                self.assertEqual(
                    [frame["delay_ms"] for frame in frames],
                    [delay * 1000 for delay in stop.waits],
                )
                self.assertTrue(pages[-1]["complete"])
                self.assertIsNone(pages[-1]["next_token"])
                self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                if name == "forecast":
                    rendered_labels = [
                        label for label in physical.labels if label.startswith("Tue ")
                    ]
                    expected_labels = []
                    for period, text_width in zip(periods, preview.measurements):
                        card_frames = (preview.width + text_width + 1) * loops
                        expected_labels.extend([period.label] * card_frames)
                    self.assertEqual(rendered_labels, expected_labels)

        self.assertEqual(weather_app.run.__defaults__, (20, None, None))
        self.assertEqual(openweather_overview.run.__defaults__, (20, None, None, None, None))
        self.assertEqual(openweather_forecast.run.__defaults__, (30, None, None, None, None))

    def test_static_hold_delay_is_clipped_to_invocation_deadline(self):
        class Clock:
            def __init__(self):
                self.elapsed = 0.0

            def monotonic(self):
                return self.elapsed

        class PhysicalCaptureMatrix(CaptureMatrix):
            def __init__(self, path, clock):
                super().__init__()
                self.path = path
                self.clock = clock
                self.submissions = []

            def show_image(self, filename, delay=None, *, stop_event=None, **geometry):
                self.clock.elapsed += 0.03
                with Image.open(self.path) as image:
                    self.clear()
                    self.draw_image(image, **geometry)
                self.submissions.append(self.snapshot())
                return True

        class AdvancingStop:
            def __init__(self, clock):
                self.clock = clock
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                self.clock.elapsed += delay
                return False

        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "still.png"
            Image.new("RGB", (64, 32), "purple").save(path)

            physical_clock = Clock()
            physical = PhysicalCaptureMatrix(path, physical_clock)
            stop = AdvancingStop(physical_clock)
            with patch(
                "modules.media_player.time.monotonic", side_effect=physical_clock.monotonic,
            ):
                self.assertTrue(play_media(
                    physical, str(path), 0.05, stop_event=stop, settings=settings,
                ))

            preview_clock = Clock()
            preview = PhysicalCaptureMatrix(path, preview_clock)
            frames = list(static_media_frames(
                str(path), 0.05, settings,
                render=lambda filename, geometry: preview.show_image(
                    filename, delay=None, **geometry,
                ),
                monotonic=preview_clock.monotonic,
            ))

            self.assertEqual(len(frames), 1)
            self.assertAlmostEqual(frames[0][1], 20.0)
            self.assertEqual(stop.waits, [frames[0][1] / 1000])
            self.assertAlmostEqual(sum(stop.waits), 0.02)
            self.assertEqual(
                physical.submissions[0].tobytes(), preview.submissions[0].tobytes(),
            )

            exhausted = PhysicalCaptureMatrix(path, Clock())
            self.assertEqual(
                list(static_media_frames(
                    str(path), 0, settings,
                    render=lambda filename, geometry: exhausted.show_image(
                        filename, delay=None, **geometry,
                    ),
                )),
                [],
            )
            self.assertEqual(exhausted.submissions, [])

    def test_announcement_static_scroll_transition_and_final_delay_match_physical_invocation(self):
        import modules.announcement_board as announcement_board

        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        class RecordingCaptureMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class AdvancingStop:
            def __init__(self):
                self.elapsed = 0.0
                self.waits = []
                self.cancelled = False

            def is_set(self):
                return self.cancelled

            def wait(self, delay):
                self.waits.append(delay)
                self.elapsed += delay
                return self.cancelled

        def item(item_id, headline, timing, transition):
            return {
                "id": item_id,
                "enabled": True,
                "name": headline,
                "layout": "text_notice",
                "headline": headline,
                "message": "",
                "media": None,
                "schedule": {
                    "start_date": "2030-01-15",
                    "end_date": "2030-01-15",
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "days": [0, 1, 2, 3, 4, 5, 6],
                },
                "style": {
                    "foreground": "#ffffff",
                    "background": "#001122",
                    "font": "5x7",
                    "alignment": "left",
                    "border": "none",
                    "border_colour": "#ffffff",
                },
                "timing": timing,
                "transition": transition,
            }

        announcements = [
            item(
                "announcement_static", "HELD",
                {"mode": "static", "duration_seconds": 1}, {"type": "cut"},
            ),
            item(
                "announcement_scroll", "A",
                {"mode": "scroll", "loops": 2, "frame_delay": 0.01},
                {"type": "cut"},
            ),
            item(
                "announcement_fade", "FINAL",
                {"mode": "static", "duration_seconds": 1},
                {"type": "fade", "duration_seconds": 0.4},
            ),
        ]
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["announcement_board"]["announcements"] = announcements
        validate_config(validated, enforce_save_bounds=True)
        announcements = validated["announcement_board"]["announcements"]

        physical = RecordingCaptureMatrix()
        stop = AdvancingStop()
        now = datetime.datetime(2030, 1, 15, 12, 0)
        with patch("modules.announcement_board.time.monotonic", side_effect=lambda: stop.elapsed):
            self.assertTrue(announcement_board.run(
                physical,
                {"announcement_board": {"announcements": announcements}},
                duration=3,
                stop_event=stop,
                now_provider=lambda: now,
            ))

        preview = CaptureMatrix()
        frames = list(edit_preview.announcement_preview_frames(
            preview, announcements, duration=3,
        ))
        preview_images = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
            for frame in frames
        ]
        physical_bytes = [image.tobytes() for image in physical.submissions]
        preview_bytes = [image.tobytes() for image in preview_images]
        preview_delays = [frame["delay_ms"] for frame in frames]
        physical_delays = [delay * 1000 for delay in stop.waits]

        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        text_width = font.text_pixels(0, 20, "A")[0]
        scroll_frames_per_loop = 64 + text_width + 1
        transition_start = 1 + scroll_frames_per_loop * 2
        self.assertEqual(len(frames), transition_start + 5)
        self.assertEqual(preview_bytes, physical_bytes)
        self.assertEqual(preview_delays, physical_delays)
        self.assertEqual(preview_delays[:1], [1000])
        self.assertEqual(
            preview_delays[1:transition_start], [10] * (scroll_frames_per_loop * 2),
        )
        self.assertEqual(
            [round(delay, 9) for delay in preview_delays[transition_start:]],
            [100, 100, 100, 100, 200],
        )
        self.assertAlmostEqual(sum(preview_delays), 3000)
        self.assertEqual(
            preview_bytes[1], preview_bytes[1 + scroll_frames_per_loop],
            "each saved leftward traversal must reset to the exact first position",
        )
        self.assertEqual(
            preview_bytes[-1], preview_bytes[-2],
            "the terminal page must be the one explicit hold submission",
        )
        self.assertEqual(len(physical.submissions), 146)

        cancelled = RecordingCaptureMatrix()
        cancelled_stop = AdvancingStop()
        cancelled_stop.cancelled = True
        self.assertFalse(announcement_board.run(
            cancelled,
            {"announcement_board": {"announcements": announcements}},
            duration=3,
            stop_event=cancelled_stop,
            now_provider=lambda: now,
        ))
        self.assertEqual(cancelled.submissions, [])
        self.assertEqual(announcement_board.run_preview.__defaults__, (None,))

    def test_announcement_transition_and_hold_complete_before_terminal_page(self):
        import modules.announcement_board as announcement_board

        class RecordingCaptureMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class RecordingStopEvent:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return False

        announcement = {
            "id": "announcement_preview_fade",
            "enabled": True,
            "name": "Preview fade",
            "layout": "text_notice",
            "headline": "Welcome",
            "message": "Front desk closes at 5 PM",
            "media": None,
            "schedule": {
                "start_date": "2030-01-15",
                "end_date": "2030-01-15",
                "start_time": "09:00",
                "end_time": "17:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
            },
            "style": {
                "foreground": "#ffffff",
                "background": "#001122",
                "font": "5x7",
                "alignment": "left",
                "border": "none",
                "border_colour": "#ffffff",
            },
            "timing": {"mode": "static", "duration_seconds": 1.2},
            "transition": {"type": "fade", "duration_seconds": 0.4},
        }
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["announcement_board"]["announcements"] = [announcement]
        validate_config(validated, enforce_save_bounds=True)
        announcement = validated["announcement_board"]["announcements"][0]

        physical = RecordingCaptureMatrix()
        stop_event = RecordingStopEvent()
        self.assertTrue(
            announcement_board.run_preview(physical, announcement, stop_event=stop_event)
        )

        preview_matrix = CaptureMatrix()
        session = PreviewSession(
            static_announcement_preview_frames(preview_matrix, announcement)
        )
        session.MAX_FRAMES = int(4)
        transition_page = session.page()
        self.assertFalse(transition_page["complete"])
        self.assertIsNotNone(transition_page["next_token"])
        terminal_page = session.page(transition_page["next_token"])

        pages = [transition_page, terminal_page]
        frames = [frame for page in pages for frame in page["frames"]]
        preview_images = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
            for frame in frames
        ]
        self.assertEqual([len(page["frames"]) for page in pages], [4, 1])
        self.assertTrue(terminal_page["complete"])
        self.assertIsNone(terminal_page["next_token"])
        self.assertEqual([page["page"] for page in pages], [0, 1])
        self.assertTrue(all(len(page["frames"]) <= session.MAX_FRAMES for page in pages))
        self.assertEqual(
            [image.tobytes() for image in preview_images],
            [image.tobytes() for image in physical.submissions],
        )
        self.assertEqual(
            [frame["delay_ms"] for frame in frames],
            [delay * 1000 for delay in stop_event.waits],
        )
        self.assertEqual([frame["delay_ms"] for frame in frames], [100, 100, 100, 100, 1200])
        self.assertEqual(sum(frame["delay_ms"] for frame in frames), 1600)
        self.assertEqual(
            preview_images[-1].tobytes(), preview_images[-2].tobytes(),
            "the single hold frame must retain the completed transition image",
        )
        with self.assertRaises(StopIteration):
            session.page()

    def test_long_left_ticker_pages_complete_every_configured_loop_and_reset(self):
        options = {
            "type": "text_scroll",
            "text": "A LONG LEFT TICKER",
            "font": "5x7",
            "colour": "#ffcc00",
            "background": "#001122",
            "y": 20,
            "direction": "left",
            "speed": 0.05,
            "loops": 2,
        }
        session = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        pages = []
        token = None
        while True:
            page = session.page(token)
            pages.append(page)
            encoded = json.dumps(page, separators=(",", ":")).encode()
            self.assertLessEqual(len(page["frames"]), 32)
            self.assertLessEqual(len(encoded), 512 * 1024)
            self.assertLessEqual(session.retained_frame_count, 33)
            if page["complete"]:
                break
            token = page["next_token"]

        frames = [frame for page in pages for frame in page["frames"]]
        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        text_width = font.text_pixels(0, options["y"], options["text"])[0]
        frames_per_loop = 64 + text_width + 1
        self.assertEqual(len(frames), frames_per_loop * options["loops"])
        self.assertTrue(all(len(page["frames"]) == 32 for page in pages[:-1]))
        self.assertEqual([page["page"] for page in pages], list(range(len(pages))))
        self.assertTrue(all(not page["complete"] for page in pages[:-1]))
        self.assertIsNone(pages[-1]["next_token"])
        self.assertEqual(sum(frame["delay_ms"] for frame in frames), len(frames) * 50)

        def image(frame):
            return Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()

        first = image(frames[0])
        reset = image(frames[frames_per_loop])
        last = image(frames[-1])
        self.assertEqual(first.tobytes(), reset.tobytes())
        self.assertEqual(first.getpixel((0, 0)), (0, 17, 34))
        self.assertEqual(last.getbbox(), (0, 0, 64, 32))
        with self.assertRaises(StopIteration):
            session.page()

        restarted = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        restarted_first = image(restarted.page()["frames"][0])
        self.assertEqual(restarted_first.tobytes(), first.tobytes())

    def test_long_right_ticker_pages_complete_every_configured_loop_and_reset(self):
        options = {
            "type": "text_scroll",
            "text": "A LONG RIGHT TICKER",
            "font": "5x7",
            "colour": "#ffcc00",
            "background": "#001122",
            "y": 20,
            "direction": "right",
            "speed": 0.05,
            "loops": 2,
        }
        session = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        pages = []
        token = None
        while True:
            page = session.page(token)
            pages.append(page)
            encoded = json.dumps(page, separators=(",", ":")).encode()
            self.assertLessEqual(len(page["frames"]), 32)
            self.assertLessEqual(len(encoded), 512 * 1024)
            self.assertLessEqual(session.retained_frame_count, 33)
            if page["complete"]:
                break
            token = page["next_token"]

        frames = [frame for page in pages for frame in page["frames"]]
        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        text_width = font.text_pixels(0, options["y"], options["text"])[0]
        frames_per_loop = 64 + text_width + 1
        self.assertEqual(len(frames), frames_per_loop * options["loops"])
        self.assertTrue(all(len(page["frames"]) == 32 for page in pages[:-1]))
        self.assertEqual([page["page"] for page in pages], list(range(len(pages))))
        self.assertTrue(all(not page["complete"] for page in pages[:-1]))
        self.assertIsNone(pages[-1]["next_token"])
        self.assertEqual(sum(frame["delay_ms"] for frame in frames), len(frames) * 50)

        def image(frame):
            return Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()

        first = image(frames[0])
        reset = image(frames[frames_per_loop])
        last = image(frames[-1])
        left_options = dict(options, direction="left", loops=1)
        left_frames = list(custom_text_preview_frames(CaptureMatrix(), left_options))
        self.assertEqual(first.tobytes(), reset.tobytes())
        self.assertEqual(first.getpixel((0, 0)), (0, 17, 34))
        self.assertEqual(last.getbbox(), (0, 0, 64, 32))
        self.assertEqual(
            image(frames[text_width]).tobytes(), image(left_frames[64]).tobytes()
        )
        with self.assertRaises(StopIteration):
            session.page()

        restarted = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        restarted_first = image(restarted.page()["frames"][0])
        self.assertEqual(restarted_first.tobytes(), first.tobytes())

    def test_page_frame_and_byte_bounds_do_not_truncate_cycle(self):
        class ClosingIterator:
            def __init__(self, iterator):
                self.iterator = iter(iterator)
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.iterator)

            def close(self):
                self.close_calls += 1
                close = getattr(self.iterator, "close", None)
                if close is not None:
                    close()

        options = {
            "type": "text_scroll",
            "text": "W" * 24,
            "font": "helvR12",
            "colour": "#ffffff",
            "background": "#001122",
            "y": 20,
            "direction": "left",
            "speed": 0.037,
            "loops": 2,
        }
        with patch.object(frame_capture_module, "Image", Image):
            expected = list(custom_text_preview_frames(CaptureMatrix(), options))

        self.assertGreater(len(expected), 32)

        def serialized(page):
            return json.dumps(page, separators=(",", ":")).encode("utf-8")

        # A one-frame nonterminal response is the byte-bound unit because the
        # actual response contract includes JSON, UTF-8, base64, and the token.
        token = "0" * 64
        largest_single = max(
            len(serialized({
                "page": page_number,
                "frames": [frame],
                "next_token": token,
                "complete": False,
            }))
            for page_number, frame in enumerate(expected)
        )
        smallest_pair = min(
            len(serialized({
                "page": page_number,
                "frames": expected[index:index + 2],
                "next_token": token,
                "complete": False,
            }))
            for page_number, index in enumerate(range(len(expected) - 1))
        )
        self.assertLess(largest_single, smallest_pair)

        policies = (
            ("frame", 3, 10 * 1024 * 1024, 3, False),
            ("byte", len(expected), smallest_pair - 1, 1, False),
            # Complete-cycle preservation wins when one valid frame cannot fit:
            # that frame is returned alone as the documented oversize exception.
            ("single_oversize", len(expected), 1, 1, True),
        )
        for name, max_frames, max_bytes, expected_page_max, oversize in policies:
            with self.subTest(bound=name), patch.object(frame_capture_module, "Image", Image):
                guarded = ClosingIterator(
                    custom_text_preview_frames(CaptureMatrix(), options)
                )
                session = PreviewSession(guarded)
                session.MAX_FRAMES = max_frames
                session.MAX_BYTES = max_bytes
                pages = []
                token = None
                consumed_tokens = []
                while True:
                    page = session.page(token)
                    pages.append(page)
                    self.assertLessEqual(len(page["frames"]), expected_page_max)
                    page_bytes = len(serialized(page))
                    if oversize and page_bytes > max_bytes:
                        self.assertEqual(len(page["frames"]), 1)
                    else:
                        self.assertLessEqual(page_bytes, max_bytes)
                    self.assertLessEqual(session.retained_frame_count, 1)
                    if page["complete"]:
                        self.assertIsNone(page["next_token"])
                        break
                    self.assertIsNotNone(page["next_token"])
                    consumed_tokens.append(page["next_token"])
                    token = page["next_token"]

                actual = [frame for page in pages for frame in page["frames"]]
                self.assertEqual(actual, expected)
                self.assertEqual(
                    [frame["delay_ms"] for frame in actual],
                    [frame["delay_ms"] for frame in expected],
                )
                self.assertEqual(
                    [page["page"] for page in pages], list(range(len(pages)))
                )
                self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                self.assertTrue(pages[-1]["complete"])
                self.assertEqual(guarded.close_calls, 1)
                self.assertEqual(session.retained_frame_count, 0)
                with self.assertRaises(StopIteration):
                    session.page(token)
                if consumed_tokens:
                    with self.assertRaises(StopIteration):
                        session.page(consumed_tokens[0])

    def test_maximum_width_custom_text_ten_loops_completes_without_history_growth(self):
        class WeakFrame(dict):
            pass

        options = {
            "type": "text_scroll",
            "text": "@" * 200,
            "font": "helvR12",
            "colour": "#ffffff",
            "background": "#000000",
            "y": 31,
            "direction": "left",
            "speed": 0.01,
            "loops": 10,
        }
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["custom_modules"] = [{
            "id": "custom_maximum", "name": "Maximum ticker", "x": 0, **options,
        }]
        validated["playlist"] = [{
            "module": "custom_maximum", "duration": 1, "enabled": True,
        }]
        validate_config(validated, enforce_save_bounds=True)

        font = BdfFont(
            Path(__file__).parents[1] / "resources" / "fonts" / f"{options['font']}.bdf"
        )
        text_width = font.text_pixels(0, options["y"], options["text"])[0]
        frames_per_loop = 64 + text_width + 1
        expected_frames = frames_per_loop * options["loops"]
        source = (
            WeakFrame(frame)
            for frame in custom_text_preview_frames(CaptureMatrix(), options)
        )
        session = PreviewSession(source)
        token = None
        page_number = 0
        frame_count = 0
        delay_total = 0
        tokens = []
        first_loop = []
        prior_frame_refs = []

        while True:
            page = session.page(token)
            gc.collect()
            self.assertTrue(all(reference() is None for reference in prior_frame_refs))
            self.assertEqual(page["page"], page_number)
            self.assertLessEqual(len(page["frames"]), 32)
            self.assertLessEqual(
                len(json.dumps(page, separators=(",", ":")).encode()), 512 * 1024,
            )
            self.assertLessEqual(session.retained_frame_count, 33)

            for frame in page["frames"]:
                loop_offset = frame_count % frames_per_loop
                signature = frame["png"]
                if frame_count < frames_per_loop:
                    first_loop.append(signature)
                else:
                    self.assertEqual(signature, first_loop[loop_offset])
                frame_count += 1
                delay_total += frame["delay_ms"]

            if page["complete"]:
                self.assertIsNone(page["next_token"])
                terminal_frame_refs = [weakref.ref(frame) for frame in page["frames"]]
                del frame
                del page
                gc.collect()
                self.assertTrue(
                    all(reference() is None for reference in terminal_frame_refs)
                )
                break
            self.assertIsInstance(page["next_token"], str)
            self.assertNotIn(page["next_token"], tokens)
            tokens.append(page["next_token"])
            token = page["next_token"]
            prior_frame_refs = [weakref.ref(frame) for frame in page["frames"]]
            del frame
            del page
            page_number += 1

        self.assertEqual(frame_count, expected_frames)
        self.assertEqual(delay_total, expected_frames * 10)
        self.assertGreater(len(set(first_loop)), 64)
        self.assertEqual(len(tokens), page_number)
        with self.assertRaises(StopIteration):
            session.page()


if __name__ == "__main__":
    unittest.main()
