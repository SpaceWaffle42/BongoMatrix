import multiprocessing
import os
import threading
import time
import unittest
from unittest.mock import patch

from preview_provider import (
    PreviewProvider,
    PreviewProviderCancelled,
    PreviewProviderTimeout,
)


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


def provider_blocked_until_timeout(settings, _credentials, _timeout):
    settings["started"].set()
    time.sleep(60.0)


def provider_blocked_until_cancelled(settings, _credentials, _timeout):
    settings["started"].set()
    time.sleep(60.0)


def provider_returns_recovery_marker(settings, credentials, timeout):
    return {
        "marker": settings["marker"],
        "effective_timeout": timeout,
        "api_key": credentials["api_key"],
    }


class PreviewProviderTests(unittest.TestCase):
    def test_timeout_and_cancel_release_worker_slot(self):
        context = multiprocessing.get_context("spawn")
        timeout_started = context.Event()
        cancel_started = context.Event()
        cancellation = threading.Event()
        timeout_cancel = threading.Event()
        provider = PreviewProvider()
        overflow_provider = PreviewProvider()
        children_before = {child.pid for child in multiprocessing.active_children()}
        threads_before = {thread.ident for thread in threading.enumerate()}
        descriptors_before = set(os.listdir("/proc/self/fd"))
        created_pipes = []
        created_children = []
        outcomes = {}
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

        def run(name, callable_, started, cancel_event, deadline):
            try:
                provider.prepare(
                    callable_,
                    settings={"started": started},
                    credentials={"api_key": f"{name}-secret"},
                    cancel_event=cancel_event,
                    request_deadline=deadline,
                )
            except BaseException as error:
                outcomes[name] = error

        timeout_thread = threading.Thread(
            target=run,
            args=(
                "timeout",
                provider_blocked_until_timeout,
                timeout_started,
                timeout_cancel,
                0.300,
            ),
        )
        cancel_thread = threading.Thread(
            target=run,
            args=(
                "cancel",
                provider_blocked_until_cancelled,
                cancel_started,
                cancellation,
                3.0,
            ),
        )

        with patch.object(provider._context, "Pipe", side_effect=tracked_pipe), patch.object(
            provider._context, "Process", side_effect=tracked_process
        ):
            timeout_started_at = time.monotonic()
            timeout_thread.start()
            try:
                for started, name in ((timeout_started, "timeout"),):
                    if not started.wait(2.0):
                        if name in outcomes:
                            raise outcomes[name]
                        self.fail(f"{name} provider never started")
                cancel_thread.start()
                if not cancel_started.wait(2.0):
                    if "cancel" in outcomes:
                        raise outcomes["cancel"]
                    self.fail("cancel provider never started")

                self.assertEqual(provider.active_child_count, 2)
                self.assertEqual(provider.active_worker_count, 2)
                self.assertEqual(provider.available_worker_slots, 0)
                children_at_capacity = len(created_children)
                with self.assertRaisesRegex(
                    RuntimeError, "preview provider worker capacity exhausted"
                ):
                    overflow_provider.prepare(
                        provider_returns_recovery_marker,
                        settings={"marker": "over-admitted"},
                        credentials={"api_key": "capacity-secret"},
                        cancel_event=threading.Event(),
                        request_deadline=3.0,
                    )
                self.assertEqual(len(created_children), children_at_capacity)

                timeout_thread.join(1.0)
                timeout_elapsed = time.monotonic() - timeout_started_at
                self.assertFalse(timeout_thread.is_alive(), "timeout did not settle")
                self.assertGreaterEqual(timeout_elapsed, 0.275)
                self.assertLess(timeout_elapsed, 0.650)
                self.assertIsInstance(outcomes.get("timeout"), PreviewProviderTimeout)
                self.assertEqual(
                    str(outcomes["timeout"]),
                    "preview provider preparation timed out",
                )
                self.assertTrue(created_children[0]._closed)
                self.assertEqual(provider.active_child_count, 1)
                self.assertEqual(provider.active_worker_count, 1)
                self.assertEqual(provider.available_worker_slots, 1)

                timeout_recovery = provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "after-timeout"},
                    credentials={"api_key": "timeout-recovery-secret"},
                    cancel_event=threading.Event(),
                    request_deadline=8.0,
                )
                self.assertEqual(
                    timeout_recovery,
                    (("effective_timeout", 3.0), ("marker", "after-timeout")),
                )
                self.assertTrue(created_children[-1]._closed)
                self.assertEqual(provider.active_worker_count, 1)

                cancelled_at = time.monotonic()
                cancellation.set()
                cancel_thread.join(1.0)
                cancellation_latency = time.monotonic() - cancelled_at
                self.assertFalse(cancel_thread.is_alive(), "cancellation did not settle")
                self.assertLessEqual(cancellation_latency, 0.250)
                self.assertIsInstance(outcomes.get("cancel"), PreviewProviderCancelled)
                self.assertEqual(
                    str(outcomes["cancel"]),
                    "preview provider preparation cancelled",
                )
                self.assertTrue(created_children[1]._closed)
                self.assertEqual(provider.active_child_count, 0)
                self.assertEqual(provider.active_worker_count, 0)
                self.assertEqual(provider.available_worker_slots, 2)

                cancel_recovery = provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "after-cancel"},
                    credentials={"api_key": "cancel-recovery-secret"},
                    cancel_event=threading.Event(),
                    request_deadline=3.0,
                )
                self.assertEqual(
                    cancel_recovery,
                    (("effective_timeout", 3.0), ("marker", "after-cancel")),
                )
                self.assertLessEqual(provider.active_child_count, provider.MAX_WORKERS)
                self.assertTrue(all(connection.closed for connection in created_pipes))
                self.assertTrue(all(child._closed for child in created_children))
                self.assertEqual(provider.active_child_count, 0)
                self.assertEqual(provider.active_worker_count, 0)
                self.assertEqual(provider.available_worker_slots, 2)
                self.assertEqual(
                    {child.pid for child in multiprocessing.active_children()},
                    children_before,
                )
                self.assertEqual(
                    {thread.ident for thread in threading.enumerate()}, threads_before
                )
                self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
            finally:
                timeout_cancel.set()
                cancellation.set()
                timeout_thread.join(1.0)
                cancel_thread.join(1.0)

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
