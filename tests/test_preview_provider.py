import multiprocessing
import os
import threading
import time
import unittest
from unittest.mock import patch

from preview_provider import PreviewProvider, PreviewProviderCancelled


def provider_work_that_must_not_start(_settings, _credentials, _timeout):
    raise AssertionError("provider/DNS work started after cancellation")


def provider_blocked_during_connect(settings, _credentials, _timeout):
    settings["connect_started"].set()
    time.sleep(60.0)


def provider_blocked_during_response_read(settings, _credentials, _timeout):
    settings["connect_completed"].set()
    settings["response_read_started"].set()
    settings["response_reader"].recv_bytes()


def provider_returns_prepared_weather(settings, credentials, _timeout):
    settings["provider_returned"].set()
    return {
        "temperature_c": 21,
        "conditions": ["clear", "dry"],
        "api_key": credentials["api_key"],
    }


class PreviewProviderTests(unittest.TestCase):
    def test_cancel_before_dns_starts_no_child(self):
        cancellation = threading.Event()
        cancellation.set()
        provider = PreviewProvider()
        children_before = {child.pid for child in multiprocessing.active_children()}
        threads_before = {thread.ident for thread in threading.enumerate()}
        descriptors_before = set(os.listdir("/proc/self/fd"))

        with patch.object(
            multiprocessing.context.SpawnContext,
            "Process",
            side_effect=AssertionError("provider child was created"),
        ) as process:
            with self.assertRaisesRegex(
                PreviewProviderCancelled,
                "preview provider preparation cancelled",
            ):
                provider.prepare(
                    provider_work_that_must_not_start,
                    settings={"location": "cancelled-before-dns"},
                    credentials={"api_key": "server-held"},
                    cancel_event=cancellation,
                    request_deadline=3.0,
                )

        self.assertEqual(process.call_count, 0)
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()},
            children_before,
        )
        self.assertEqual(
            {thread.ident for thread in threading.enumerate()},
            threads_before,
        )
        self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_cancel_during_connect_terminates_child_within_250_ms(self):
        context = multiprocessing.get_context("spawn")
        connect_started = context.Event()
        cancellation = threading.Event()
        provider = PreviewProvider()
        children_before = {child.pid for child in multiprocessing.active_children()}
        threads_before = {thread.ident for thread in threading.enumerate()}
        descriptors_before = set(os.listdir("/proc/self/fd"))
        outcome = {}

        def prepare():
            try:
                provider.prepare(
                    provider_blocked_during_connect,
                    settings={
                        "location": "blocked-during-connect",
                        "connect_started": connect_started,
                    },
                    credentials={"api_key": "server-held"},
                    cancel_event=cancellation,
                    request_deadline=3.0,
                )
            except BaseException as error:
                outcome["error"] = error

        prepare_thread = threading.Thread(target=prepare)
        prepare_thread.start()
        try:
            if not connect_started.wait(2.0):
                if "error" in outcome:
                    raise outcome["error"]
                self.fail("provider never entered connect")
            self.assertEqual(provider.active_child_count, 1)
            self.assertEqual(provider.active_worker_count, 1)
            self.assertEqual(provider.available_worker_slots, 1)

            cancelled_at = time.monotonic()
            cancellation.set()
            prepare_thread.join(1.0)
            cancellation_latency = time.monotonic() - cancelled_at

            self.assertFalse(prepare_thread.is_alive(), "prepare did not settle")
            self.assertLessEqual(cancellation_latency, 0.250)
            self.assertIsInstance(outcome.get("error"), PreviewProviderCancelled)
            self.assertEqual(
                str(outcome["error"]),
                "preview provider preparation cancelled",
            )
            self.assertEqual(provider.active_child_count, 0)
            self.assertEqual(provider.active_worker_count, 0)
            self.assertEqual(provider.available_worker_slots, 2)
            self.assertEqual(
                {child.pid for child in multiprocessing.active_children()},
                children_before,
            )
            self.assertEqual(
                {thread.ident for thread in threading.enumerate()},
                threads_before,
            )
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
        finally:
            cancellation.set()
            prepare_thread.join(1.0)

    def test_cancel_during_response_read_terminates_child_within_250_ms(self):
        context = multiprocessing.get_context("spawn")
        connect_completed = context.Event()
        response_read_started = context.Event()
        cancellation = threading.Event()
        provider = PreviewProvider()
        children_before = {child.pid for child in multiprocessing.active_children()}
        threads_before = {thread.ident for thread in threading.enumerate()}
        descriptors_before = set(os.listdir("/proc/self/fd"))
        response_reader, response_writer = context.Pipe(duplex=False)
        outcome = {}

        def prepare():
            try:
                provider.prepare(
                    provider_blocked_during_response_read,
                    settings={
                        "location": "blocked-during-response-read",
                        "connect_completed": connect_completed,
                        "response_read_started": response_read_started,
                        "response_reader": response_reader,
                    },
                    credentials={"api_key": "server-held"},
                    cancel_event=cancellation,
                    request_deadline=3.0,
                )
            except BaseException as error:
                outcome["error"] = error

        prepare_thread = threading.Thread(target=prepare)
        prepare_thread.start()
        try:
            if not response_read_started.wait(2.0):
                if "error" in outcome:
                    raise outcome["error"]
                self.fail("provider never entered response read")
            self.assertTrue(connect_completed.is_set())
            self.assertEqual(provider.active_child_count, 1)
            self.assertEqual(provider.active_worker_count, 1)
            self.assertEqual(provider.available_worker_slots, 1)

            response_reader.close()
            cancelled_at = time.monotonic()
            cancellation.set()
            prepare_thread.join(1.0)
            cancellation_latency = time.monotonic() - cancelled_at
            response_writer.close()

            self.assertFalse(prepare_thread.is_alive(), "prepare did not settle")
            self.assertLessEqual(cancellation_latency, 0.250)
            self.assertIsInstance(outcome.get("error"), PreviewProviderCancelled)
            self.assertEqual(
                str(outcome["error"]),
                "preview provider preparation cancelled",
            )
            self.assertTrue(response_reader.closed)
            self.assertTrue(response_writer.closed)
            self.assertEqual(provider.active_child_count, 0)
            self.assertEqual(provider.active_worker_count, 0)
            self.assertEqual(provider.available_worker_slots, 2)
            self.assertEqual(
                {child.pid for child in multiprocessing.active_children()},
                children_before,
            )
            self.assertEqual(
                {thread.ident for thread in threading.enumerate()},
                threads_before,
            )
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
        finally:
            cancellation.set()
            response_reader.close()
            response_writer.close()
            prepare_thread.join(1.0)

    def test_cancel_after_provider_return_before_renderer_publication_drops_result(self):
        context = multiprocessing.get_context("spawn")
        provider_returned = context.Event()
        cancellation = threading.Event()
        publication_reached = threading.Event()
        release_publication = threading.Event()
        provider = PreviewProvider()
        children_before = {child.pid for child in multiprocessing.active_children()}
        threads_before = {thread.ident for thread in threading.enumerate()}
        descriptors_before = set(os.listdir("/proc/self/fd"))
        created_pipes = []
        created_children = []
        outcome = {}
        real_pipe = provider._context.Pipe
        real_process = provider._context.Process

        def tracked_pipe(*args, **kwargs):
            connections = real_pipe(*args, **kwargs)
            created_pipes.extend(connections)
            return connections

        def tracked_process(*args, **kwargs):
            child = real_process(*args, **kwargs)
            created_children.append(child)
            return child

        def before_publication(prepared):
            self.assertEqual(
                prepared,
                (
                    ("conditions", ("clear", "dry")),
                    ("temperature_c", 21),
                ),
            )
            with self.assertRaises(TypeError):
                prepared[0] = ("temperature_c", 99)
            publication_reached.set()
            self.assertTrue(release_publication.wait(1.0))

        def prepare():
            try:
                outcome["result"] = provider.prepare(
                    provider_returns_prepared_weather,
                    settings={
                        "location": "provider-returned",
                        "provider_returned": provider_returned,
                    },
                    credentials={"api_key": "server-held-secret"},
                    cancel_event=cancellation,
                    request_deadline=3.0,
                )
            except BaseException as error:
                outcome["error"] = error

        provider._before_publication = before_publication
        with patch.object(provider._context, "Pipe", side_effect=tracked_pipe), patch.object(
            provider._context, "Process", side_effect=tracked_process
        ):
            prepare_thread = threading.Thread(target=prepare)
            prepare_thread.start()
            try:
                if not publication_reached.wait(2.0):
                    if "error" in outcome:
                        raise outcome["error"]
                    self.fail("parent never reached the pre-publication barrier")
                self.assertTrue(provider_returned.is_set())
                self.assertEqual(len(created_children), 1)
                self.assertFalse(created_children[0].is_alive())

                cancelled_at = time.monotonic()
                cancellation.set()
                release_publication.set()
                prepare_thread.join(1.0)
                cancellation_latency = time.monotonic() - cancelled_at

                self.assertFalse(prepare_thread.is_alive(), "prepare did not settle")
                self.assertLessEqual(cancellation_latency, 0.250)
                self.assertIsInstance(outcome.get("error"), PreviewProviderCancelled)
                self.assertEqual(
                    str(outcome["error"]),
                    "preview provider preparation cancelled",
                )
                self.assertNotIn("result", outcome)
                self.assertTrue(all(connection.closed for connection in created_pipes))
                self.assertTrue(created_children[0]._closed)
                self.assertEqual(provider.active_child_count, 0)
                self.assertEqual(provider.active_worker_count, 0)
                self.assertEqual(provider.available_worker_slots, 2)
                self.assertEqual(
                    {child.pid for child in multiprocessing.active_children()},
                    children_before,
                )
                self.assertEqual(
                    {thread.ident for thread in threading.enumerate()},
                    threads_before,
                )
                self.assertEqual(
                    set(os.listdir("/proc/self/fd")), descriptors_before
                )
            finally:
                cancellation.set()
                release_publication.set()
                prepare_thread.join(1.0)
