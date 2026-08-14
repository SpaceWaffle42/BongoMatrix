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
