import multiprocessing
import os
import threading
import unittest
from unittest.mock import patch

from preview_provider import PreviewProvider, PreviewProviderCancelled


def provider_work_that_must_not_start(_settings, _credentials, _timeout):
    raise AssertionError("provider/DNS work started after cancellation")


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
