import multiprocessing
import os
import threading
import time
import unittest
from collections import deque
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from unittest.mock import patch

from preview_provider import (
    PreviewProvider,
    PreviewProviderCancelled,
    PreviewProviderTimeout,
    PreviewProviderUnavailable,
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
    def setUp(self):
        PreviewProvider._shutdown_reaper_for_tests()
        self.assertEqual(list(PreviewProvider._reaper_jobs), [])
        self.assertEqual(PreviewProvider._active_children, 0)
        self.assertEqual(PreviewProvider._active_workers, 0)
        PreviewProvider()._ensure_reaper()
        reapers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "preview-provider-child-reaper"
        ]
        self.assertEqual(len(reapers), 1)

    def tearDown(self):
        deadline = time.monotonic() + 0.5
        while PreviewProvider._reaper_jobs and time.monotonic() < deadline:
            time.sleep(0.005)
        PreviewProvider._shutdown_reaper_for_tests()
        self.assertEqual(
            [
                thread
                for thread in threading.enumerate()
                if thread.name == "preview-provider-child-reaper"
            ],
            [],
        )

    def test_test_reaper_shutdown_joins_exact_global_owner(self):
        provider = PreviewProvider()
        provider._ensure_reaper()
        reaper = PreviewProvider._reaper_thread
        self.assertTrue(reaper.is_alive())

        PreviewProvider._shutdown_reaper_for_tests()

        self.assertFalse(reaper.is_alive())
        self.assertIsNone(PreviewProvider._reaper_thread)
        self.assertEqual(list(PreviewProvider._reaper_jobs), [])
        self.assertEqual(PreviewProvider._active_children, 0)
        self.assertEqual(PreviewProvider._active_workers, 0)
        self.assertEqual(provider.available_worker_slots, 2)

    def test_reaper_dead_after_start_precedes_pipe_and_child_creation(self):
        context_calls = []
        provider = PreviewProvider()

        class DeadThread:
            @staticmethod
            def start():
                pass

            @staticmethod
            def is_alive():
                return False

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                context_calls.append("pipe")
                raise AssertionError("pipe created without a live reaper")

            @staticmethod
            def Process(**_kwargs):
                context_calls.append("process")
                raise AssertionError("child created without a live reaper")

        PreviewProvider._shutdown_reaper_for_tests()
        with patch.object(provider, "_context", Context()), patch.object(
            threading, "Thread", return_value=DeadThread()
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to start"):
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "must-not-start"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.25,
                )

        self.assertEqual(context_calls, [])
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)
        self.assertEqual(provider._preparations, set())

    def test_shutdown_polls_after_lifecycle_notification_fault(self):
        class FaultingCondition:
            def __init__(self):
                self.inner = threading.Condition()
                self.wait_started = threading.Event()
                self.notify_calls = 0

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def wait(self, timeout=None):
                self.wait_started.set()
                return self.inner.wait(timeout)

            def notify_all(self):
                self.notify_calls += 1
                if self.notify_calls == 1:
                    raise OSError("lifecycle notification unavailable")
                self.inner.notify_all()

        lifecycle = FaultingCondition()
        provider = PreviewProvider()
        provider.__dict__["_lifecycle"] = lifecycle
        preparation = threading.Event()
        with lifecycle:
            provider._preparations.add(preparation)

        shutdown = threading.Thread(target=provider.shutdown)
        shutdown.start()
        self.assertTrue(lifecycle.wait_started.wait(0.25))
        self.assertTrue(preparation.is_set())

        with lifecycle:
            provider._preparations.remove(preparation)
            with self.assertRaisesRegex(OSError, "notification unavailable"):
                lifecycle.notify_all()

        shutdown.join(0.1)
        stranded = shutdown.is_alive()
        if stranded:
            with lifecycle:
                lifecycle.notify_all()
            shutdown.join(0.25)
        self.assertFalse(shutdown.is_alive())
        self.assertFalse(
            stranded,
            "shutdown depended exclusively on the failed notification",
        )
        self.assertEqual(provider._preparations, set())

    def test_provider_failure_precedes_receiver_cleanup_fault(self):
        receiver_error = OSError("receiver close unavailable")

        class Receiver:
            close_calls = 0

            @staticmethod
            def poll():
                return True

            @staticmethod
            def recv():
                return ("raised", "provider failed")

            def close(self):
                self.close_calls += 1
                raise receiver_error

        class Sender:
            @staticmethod
            def close():
                pass

        class Child:
            closed = False

            @staticmethod
            def start():
                pass

            @staticmethod
            def is_alive():
                return False

            @staticmethod
            def join(_timeout=None):
                pass

            def close(self):
                self.closed = True

        receiver = Receiver()
        sender = Sender()
        child = Child()
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                return receiver, sender

            @staticmethod
            def Process(**_kwargs):
                return child

        with patch.object(provider, "_context", Context()):
            with self.assertRaises(PreviewProviderUnavailable) as raised:
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.25,
                )

        self.assertEqual(str(raised.exception), "preview provider preparation failed")
        self.assertEqual(receiver.close_calls, 1)
        self.assertTrue(child.closed)
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)
        self.assertEqual(provider._preparations, set())

    def test_parent_slot_release_fault_hands_off_without_replacing_primary(self):
        pipe_error = OSError("pipe creation failed")

        class FaultingSlots:
            def __init__(self):
                self.inner = threading.BoundedSemaphore(2)
                self.release_calls = 0

            def acquire(self, **kwargs):
                return self.inner.acquire(**kwargs)

            def release(self):
                self.release_calls += 1
                if self.release_calls == 1:
                    raise OSError("slot release unavailable")
                self.inner.release()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                raise pipe_error

        slots = FaultingSlots()
        provider = PreviewProvider()
        with patch.object(provider, "_context", Context()), patch.object(
            PreviewProvider, "_worker_slots", slots
        ):
            with self.assertRaises(OSError) as raised:
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.25,
                )
            self.assertIs(raised.exception, pipe_error)
            deadline = time.monotonic() + 1.0
            while slots.release_calls < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(slots.release_calls, 2)
            self.assertEqual(provider.active_worker_count, 0)
            self.assertEqual(provider.available_worker_slots, 2)
            self.assertEqual(provider._preparations, set())
            self.assertTrue(slots.acquire(blocking=False))
            self.assertTrue(slots.acquire(blocking=False))

    def test_persistent_slot_release_fault_does_not_block_later_child_cleanup(self):
        class PersistentlyFaultingSlots:
            release_calls = 0
            faulting = True

            @classmethod
            def release(cls):
                cls.release_calls += 1
                if cls.faulting:
                    raise OSError("slot release unavailable")

        class DeadChild:
            def __init__(self):
                self.close_calls = 0
                self.closed = threading.Event()

            @staticmethod
            def is_alive():
                return False

            def close(self):
                self.close_calls += 1
                self.closed.set()

        first = DeadChild()
        later = DeadChild()
        first_provider = PreviewProvider()
        later_provider = PreviewProvider()
        with patch.object(
            PreviewProvider, "_worker_slots", PersistentlyFaultingSlots()
        ):
            first_provider._ensure_reaper()
            first_provider._handoff_reaping_child(
                first,
                child_accounted=False,
                worker_accounted=False,
                slot_acquired=True,
            )
            self.assertTrue(first.closed.wait(0.25))
            later_provider._handoff_reaping_child(
                later,
                child_accounted=False,
                worker_accounted=False,
                slot_acquired=False,
            )
            self.assertTrue(
                later.closed.wait(0.25),
                "persistent cleanup fault monopolized the global reaper",
            )
            self.assertEqual(first.close_calls, 1)
            self.assertEqual(later.close_calls, 1)
            self.assertGreater(PersistentlyFaultingSlots.release_calls, 0)
            with PreviewProvider._reaper_condition:
                self.assertTrue(
                    PreviewProvider._reaper_jobs,
                    "failed slot ownership was not durably retained",
                )
            PersistentlyFaultingSlots.faulting = False
            deadline = time.monotonic() + 0.5
            while PreviewProvider._reaper_jobs and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(list(PreviewProvider._reaper_jobs), [])

    def test_reaper_retries_one_shot_child_close_fault(self):
        class DeadChild:
            close_calls = 0
            closed = threading.Event()

            @staticmethod
            def is_alive():
                return False

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise OSError("child handle close unavailable")
                self.closed.set()

        child = DeadChild()
        provider = PreviewProvider()
        provider._ensure_reaper()
        provider._handoff_reaping_child(
            child,
            child_accounted=False,
            worker_accounted=False,
            slot_acquired=False,
        )
        self.assertTrue(child.closed.wait(0.5))
        self.assertEqual(child.close_calls, 2)
        with PreviewProvider._reaper_condition:
            self.assertEqual(list(PreviewProvider._reaper_jobs), [])

    def test_reaper_survives_one_shot_slot_release_fault(self):
        class FaultingSlots:
            def __init__(self):
                self.inner = threading.BoundedSemaphore(2)
                self.release_calls = 0
                self.inner.acquire(blocking=False)

            def acquire(self, **kwargs):
                return self.inner.acquire(**kwargs)

            def release(self):
                self.release_calls += 1
                if self.release_calls == 1:
                    raise OSError("slot release unavailable")
                self.inner.release()

        class DeadChild:
            close_calls = 0

            @staticmethod
            def is_alive():
                return False

            def close(self):
                self.close_calls += 1

        slots = FaultingSlots()
        child = DeadChild()
        provider = PreviewProvider()
        with patch.object(PreviewProvider, "_worker_slots", slots):
            provider._ensure_reaper()
            reaper = PreviewProvider._reaper_thread
            provider._handoff_reaping_child(
                child,
                child_accounted=False,
                worker_accounted=False,
                slot_acquired=True,
            )
            deadline = time.monotonic() + 1.0
            while slots.release_calls < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(slots.release_calls, 2)
            self.assertTrue(reaper.is_alive())
            self.assertEqual(child.close_calls, 1)
            self.assertTrue(slots.acquire(blocking=False))
            self.assertTrue(slots.acquire(blocking=False))

    def test_reaper_notification_fault_does_not_lose_registered_handoff(self):
        jobs = deque()

        class FaultingCondition:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def notify():
                raise OSError("reaper notification unavailable")

        provider = PreviewProvider()
        child = object()
        with patch.object(
            PreviewProvider, "_reaper_jobs", jobs, create=True
        ), patch.object(
            PreviewProvider,
            "_reaper_condition",
            FaultingCondition(),
            create=True,
        ):
            provider._handoff_reaping_child(
                child,
                child_accounted=False,
                worker_accounted=True,
                slot_acquired=True,
            )

        self.assertEqual(
            list(jobs),
            [(provider, child, False, True, True, provider._worker_slots)],
        )

    def test_terminate_fault_still_kills_and_preserves_first_cleanup_error(self):
        terminate_error = OSError("terminate unavailable")
        join_error = OSError("join unavailable")

        class Connection:
            def poll(self):
                return True

            def recv(self):
                return ("returned", (("marker", "must-not-publish"),))

            def close(self):
                pass

        class Child:
            alive = False
            terminate_calls = 0
            join_calls = 0
            kill_calls = 0
            closed = False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminate_calls += 1
                raise terminate_error

            def join(self, _timeout=None):
                self.join_calls += 1
                raise join_error

            def kill(self):
                self.kill_calls += 1
                self.alive = False

            def close(self):
                self.closed = True

        receiver = Connection()
        sender = Connection()
        child = Child()
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                return receiver, sender

            @staticmethod
            def Process(**_kwargs):
                return child

        with patch.object(provider, "_context", Context()):
            with self.assertRaises(OSError) as raised:
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.25,
                )

        self.assertIs(raised.exception, terminate_error)
        self.assertGreaterEqual(child.terminate_calls, 1)
        self.assertEqual(child.join_calls, 2)
        self.assertEqual(child.kill_calls, 1)
        self.assertTrue(child.closed)
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)
        self.assertEqual(provider._preparations, set())

    def test_reaper_start_failure_precedes_pipe_and_child_creation(self):
        context_calls = []
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                context_calls.append("pipe")
                raise AssertionError("pipe created before durable reaper")

            @staticmethod
            def Process(**_kwargs):
                context_calls.append("process")
                raise AssertionError("child created before durable reaper")

        PreviewProvider._shutdown_reaper_for_tests()
        with patch.object(provider, "_context", Context()), patch.object(
            threading.Thread, "start", side_effect=RuntimeError("reaper unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "reaper unavailable"):
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "must-not-start"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.25,
                )

        self.assertEqual(context_calls, [])
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)
        self.assertEqual(provider._preparations, set())
        shutdown = threading.Thread(target=provider.shutdown)
        shutdown.start()
        shutdown.join(0.25)
        self.assertFalse(shutdown.is_alive(), "reaper startup fault stranded shutdown")

    def test_sender_close_fault_attempts_all_cleanup_without_child_underflow(self):
        first_close_error = OSError("first sender close failure")
        second_close_error = OSError("second sender close failure")
        child_can_exit = threading.Event()

        class Receiver:
            closed = False

            def close(self):
                self.closed = True

        class Sender:
            close_calls = 0

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise first_close_error
                raise second_close_error

        class Child:
            alive = False
            closed = False
            terminate_calls = 0
            kill_calls = 0

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive and not child_can_exit.is_set()

            def join(self, _timeout=None):
                pass

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1

            def close(self):
                self.closed = True

        receiver = Receiver()
        sender = Sender()
        child = Child()
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                return receiver, sender

            @staticmethod
            def Process(**_kwargs):
                return child

        with patch.object(provider, "_context", Context()):
            with self.assertRaises(OSError) as raised:
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.02,
                )

        self.assertIs(raised.exception, first_close_error)
        self.assertEqual(sender.close_calls, 2)
        self.assertTrue(receiver.closed)
        self.assertGreaterEqual(child.terminate_calls, 1)
        self.assertGreaterEqual(child.kill_calls, 1)
        self.assertFalse(child.closed)
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 1)
        self.assertEqual(provider.available_worker_slots, 1)
        self.assertEqual(provider._preparations, set())

        shutdown = threading.Thread(target=provider.shutdown)
        shutdown.start()
        shutdown.join(0.25)
        self.assertFalse(shutdown.is_alive(), "cleanup fault stranded shutdown")

        child_can_exit.set()
        deadline = time.monotonic() + 1.0
        while provider.active_worker_count and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(child.closed)
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)

    def test_prepare_hands_off_synchronously_reaped_child_after_close_fault(self):
        class Connection:
            def poll(self):
                return True

            def recv(self):
                return ("returned", (("marker", "ready"),))

            def close(self):
                pass

        class Child:
            close_calls = 0

            def start(self):
                pass

            def is_alive(self):
                return False

            def join(self, timeout=None):
                pass

            def terminate(self):
                pass

            def kill(self):
                pass

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise OSError("synchronous child close unavailable")

        receiver = Connection()
        sender = Connection()
        child = Child()
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                return receiver, sender

            @staticmethod
            def Process(**_kwargs):
                return child

        with patch.object(provider, "_context", Context()):
            with self.assertRaisesRegex(OSError, "synchronous child close unavailable"):
                provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=1,
                )

        deadline = time.monotonic() + 1.0
        while child.close_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(child.close_calls, 2)
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)
        self.assertEqual(list(PreviewProvider._reaper_jobs), [])

    def test_result_is_not_published_when_child_reap_reaches_deadline(self):
        child_can_exit = threading.Event()
        outcome = {}

        class Connection:
            def poll(self):
                return True

            def recv(self):
                return ("returned", (("marker", "late"),))

            def close(self):
                pass

        class Child:
            close_calls = 0

            def start(self):
                pass

            def is_alive(self):
                return not child_can_exit.is_set()

            def join(self, timeout=None):
                child_can_exit.wait(timeout)

            def terminate(self):
                pass

            def kill(self):
                pass

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise OSError("child handle close unavailable")

        receiver = Connection()
        sender = Connection()
        child = Child()
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                return receiver, sender

            @staticmethod
            def Process(**_kwargs):
                return child

        def prepare():
            try:
                outcome["result"] = provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.02,
                )
            except BaseException as error:
                outcome["error"] = error

        with patch.object(provider, "_context", Context()):
            thread = threading.Thread(target=prepare)
            started = time.monotonic()
            thread.start()
            try:
                thread.join(0.10)
                self.assertFalse(thread.is_alive(), "deadline did not settle")
                self.assertLess(time.monotonic() - started, 0.10)
                self.assertIsInstance(outcome.get("error"), PreviewProviderTimeout)
                self.assertNotIn("result", outcome)
                self.assertEqual(provider.active_child_count, 1)
                self.assertEqual(provider.active_worker_count, 1)
                self.assertEqual(child.close_calls, 0)

                child_can_exit.set()
                deadline = time.monotonic() + 1.0
                while provider.active_child_count and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(provider.active_child_count, 0)
                self.assertEqual(provider.active_worker_count, 0)
                self.assertEqual(provider.available_worker_slots, 2)
                self.assertEqual(child.close_calls, 2)
            finally:
                child_can_exit.set()
                thread.join(1.0)

    def test_result_ready_child_handoff_retains_capacity_until_bounded_reap(self):
        child_can_exit = threading.Event()
        child_join_started = threading.Event()
        prepared = {}
        result_ready_at = {}

        class Receiver:
            closed = False

            def poll(self):
                return (
                    "value" in result_ready_at
                    and time.monotonic() >= result_ready_at["value"]
                )

            def recv(self):
                return ("returned", (("marker", "ready"),))

            def close(self):
                self.closed = True

        class Sender:
            closed = False

            def close(self):
                self.closed = True

        class Child:
            def __init__(self):
                self.alive = True
                self.closed = False
                self.joins = []
                self.terminate_calls = 0
                self.kill_calls = 0

            def start(self):
                pass

            def is_alive(self):
                if child_can_exit.is_set():
                    self.alive = False
                return self.alive

            def join(self, timeout=None):
                self.joins.append(timeout)
                child_join_started.set()
                child_can_exit.wait(timeout)
                if child_can_exit.is_set():
                    self.alive = False

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1

            def close(self):
                self.closed = True

        receiver = Receiver()
        sender = Sender()
        child = Child()
        provider = PreviewProvider()

        class Context:
            @staticmethod
            def Pipe(**_kwargs):
                return receiver, sender

            @staticmethod
            def Process(**_kwargs):
                return child

        def run_prepare():
            try:
                prepared["result"] = provider.prepare(
                    provider_returns_recovery_marker,
                    settings={"marker": "ignored"},
                    credentials={"api_key": "server-held"},
                    cancel_event=threading.Event(),
                    request_deadline=0.12,
                )
            except BaseException as error:
                prepared["error"] = error

        thread = threading.Thread(target=run_prepare)
        started = time.monotonic()
        result_ready_at["value"] = started + 0.09
        with patch.object(provider, "_context", Context()):
            thread.start()
            try:
                self.assertTrue(child_join_started.wait(0.14))
                shutdown_thread = threading.Thread(target=provider.shutdown)
                shutdown_thread.start()
                thread.join(0.17)
                shutdown_thread.join(0.17)
                self.assertFalse(thread.is_alive(), "result-ready prepare exceeded deadline")
                self.assertFalse(shutdown_thread.is_alive(), "provider shutdown exceeded deadline")
                self.assertLess(time.monotonic() - started, 0.17)
                self.assertIsInstance(prepared.get("error"), PreviewProviderCancelled)
                self.assertNotIn("result", prepared)
                self.assertNotIn(None, child.joins)
                self.assertTrue(provider.is_closed)
                self.assertEqual(provider.active_child_count, 1)
                self.assertEqual(provider.active_worker_count, 1)
                self.assertEqual(provider.available_worker_slots, 1)
                self.assertFalse(child.closed)

                child_can_exit.set()
                deadline = time.monotonic() + 1.0
                while provider.active_child_count and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(provider.active_child_count, 0)
                self.assertEqual(provider.active_worker_count, 0)
                self.assertEqual(provider.available_worker_slots, 2)
                self.assertTrue(child.closed)
            finally:
                child_can_exit.set()
                thread.join(1.0)

    def test_provider_preserves_bounded_eight_second_cold_start_budget(self):
        provider = PreviewProvider()

        prepared = provider.prepare(
            provider_returns_recovery_marker,
            settings={"marker": "cold-start-budget"},
            credentials={"api_key": "server-held"},
            cancel_event=threading.Event(),
            request_deadline=8.0,
        )

        self.assertEqual(
            prepared,
            (("effective_timeout", 8.0), ("marker", "cold-start-budget")),
        )
        self.assertEqual(provider.active_child_count, 0)
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.available_worker_slots, 2)

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
                1.500,
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

                timeout_thread.join(2.0)
                timeout_elapsed = time.monotonic() - timeout_started_at
                self.assertFalse(timeout_thread.is_alive(), "timeout did not settle")
                self.assertGreaterEqual(timeout_elapsed, 1.400)
                self.assertLess(timeout_elapsed, 1.850)
                self.assertIsInstance(outcomes.get("timeout"), PreviewProviderTimeout)
                self.assertEqual(
                    str(outcomes["timeout"]),
                    "preview provider preparation timed out",
                )
                reap_deadline = time.monotonic() + 1.0
                while not created_children[0]._closed and time.monotonic() < reap_deadline:
                    time.sleep(0.005)
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
                    (("effective_timeout", 8.0), ("marker", "after-timeout")),
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
        provider._ensure_reaper()
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

    def test_shutdown_reaps_all_provider_children(self):
        context = multiprocessing.get_context("spawn")
        connect_started = context.Event()
        connect_completed = context.Event()
        response_read_started = context.Event()
        connect_cancel = threading.Event()
        response_cancel = threading.Event()
        connect_provider = PreviewProvider()
        response_provider = PreviewProvider()
        children_before = {child.pid for child in multiprocessing.active_children()}
        threads_before = {thread.ident for thread in threading.enumerate()}
        descriptors_before = set(os.listdir("/proc/self/fd"))
        response_reader, response_writer = context.Pipe(duplex=False)
        created_pipes = []
        created_children = []
        close_counts = {}
        outcomes = {}
        real_pipe = connect_provider._context.Pipe
        real_process = connect_provider._context.Process
        real_connection_close = Connection.close
        real_process_close = BaseProcess.close

        def tracked_pipe(*args, **kwargs):
            connections = real_pipe(*args, **kwargs)
            created_pipes.extend(connections)
            close_counts.update((id(connection), 0) for connection in connections)
            return connections

        def tracked_process(*args, **kwargs):
            child = real_process(*args, **kwargs)
            created_children.append(child)
            close_counts[id(child)] = 0
            return child

        def tracked_connection_close(connection):
            if id(connection) in close_counts:
                close_counts[id(connection)] += 1
            return real_connection_close(connection)

        def tracked_process_close(child):
            if id(child) in close_counts:
                close_counts[id(child)] += 1
            return real_process_close(child)

        def prepare(name, provider, callable_, settings, cancel_event):
            try:
                provider.prepare(
                    callable_,
                    settings=settings,
                    credentials={"api_key": f"{name}-secret"},
                    cancel_event=cancel_event,
                    request_deadline=3.0,
                )
            except BaseException as error:
                outcomes[name] = error

        connect_thread = threading.Thread(
            target=prepare,
            args=(
                "connect",
                connect_provider,
                provider_blocked_during_connect,
                {"connect_started": connect_started},
                connect_cancel,
            ),
        )
        response_thread = threading.Thread(
            target=prepare,
            args=(
                "response",
                response_provider,
                provider_blocked_during_response_read,
                {
                    "connect_completed": connect_completed,
                    "response_read_started": response_read_started,
                    "response_reader": response_reader,
                },
                response_cancel,
            ),
        )

        with patch.object(
            connect_provider._context, "Pipe", side_effect=tracked_pipe
        ), patch.object(
            connect_provider._context, "Process", side_effect=tracked_process
        ), patch.object(
            Connection, "close", new=tracked_connection_close
        ), patch.object(
            BaseProcess, "close", new=tracked_process_close
        ):
            connect_thread.start()
            response_thread.start()
            try:
                self.assertTrue(connect_started.wait(2.0), outcomes.get("connect"))
                self.assertTrue(
                    response_read_started.wait(2.0), outcomes.get("response")
                )
                self.assertTrue(connect_completed.is_set())
                response_reader.close()

                self.assertEqual(connect_provider.active_child_count, 2)
                self.assertEqual(connect_provider.active_worker_count, 2)
                self.assertEqual(connect_provider.available_worker_slots, 0)
                self.assertEqual(len(created_children), 2)

                shutdown_started = time.monotonic()
                shutdown_threads = [
                    threading.Thread(target=connect_provider.shutdown),
                    threading.Thread(target=response_provider.shutdown),
                ]
                for thread in shutdown_threads:
                    thread.start()
                for thread in shutdown_threads:
                    thread.join(1.0)
                shutdown_elapsed = time.monotonic() - shutdown_started

                self.assertTrue(all(not thread.is_alive() for thread in shutdown_threads))
                self.assertLessEqual(shutdown_elapsed, 0.250)
                connect_thread.join(1.0)
                response_thread.join(1.0)
                self.assertFalse(connect_thread.is_alive())
                self.assertFalse(response_thread.is_alive())
                response_writer.close()
                self.assertIsInstance(outcomes.get("connect"), PreviewProviderCancelled)
                self.assertIsInstance(outcomes.get("response"), PreviewProviderCancelled)

                connect_provider.shutdown()
                response_provider.shutdown()
                process_count = len(created_children)
                pipe_count = len(created_pipes)
                with self.assertRaisesRegex(RuntimeError, "preview provider is shut down"):
                    connect_provider.prepare(
                        provider_work_that_must_not_start,
                        settings={"location": "after-shutdown"},
                        credentials={"api_key": "must-not-spawn"},
                        cancel_event=threading.Event(),
                        request_deadline=3.0,
                    )
                self.assertEqual(len(created_children), process_count)
                self.assertEqual(len(created_pipes), pipe_count)

                self.assertTrue(all(resource.closed for resource in created_pipes))
                self.assertTrue(all(child._closed for child in created_children))
                self.assertTrue(all(count == 1 for count in close_counts.values()))
                self.assertEqual(connect_provider.active_child_count, 0)
                self.assertEqual(connect_provider.active_worker_count, 0)
                self.assertEqual(connect_provider.available_worker_slots, 2)
                self.assertEqual(
                    {child.pid for child in multiprocessing.active_children()},
                    children_before,
                )
                self.assertEqual(
                    {thread.ident for thread in threading.enumerate()}, threads_before
                )
                self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
            finally:
                connect_cancel.set()
                response_cancel.set()
                for provider in (connect_provider, response_provider):
                    shutdown = getattr(provider, "shutdown", None)
                    if shutdown is not None:
                        shutdown()
                if not response_reader.closed:
                    response_reader.close()
                if not response_writer.closed:
                    response_writer.close()
                connect_thread.join(1.0)
                response_thread.join(1.0)
