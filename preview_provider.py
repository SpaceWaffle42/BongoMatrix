"""Bounded provider preparation seam for Matrix edit previews."""

from __future__ import annotations

import multiprocessing
import threading


_CANCEL_POLL_SECONDS = 0.005


def _provider_child(provider, settings, credentials, timeout, result_sender):
    """Run provider work in a spawn-safe process without returning secrets."""
    try:
        provider(settings, credentials, timeout)
        result_sender.send(("returned",))
    except BaseException:
        result_sender.send(("failed",))
    finally:
        result_sender.close()


class PreviewProviderCancelled(RuntimeError):
    """Provider preparation was cancelled before a result could be published."""


class PreviewProvider:
    """Fail-closed entry point for provider preparation.

    The pre-cancel check deliberately runs before creating multiprocessing or
    communication resources. Later provider I/O lifecycle slices extend this
    seam without changing that ordering.
    """

    MAX_WORKERS = 2

    def __init__(self):
        self._context = multiprocessing.get_context("spawn")
        self._worker_slots = threading.BoundedSemaphore(self.MAX_WORKERS)
        self._accounting_lock = threading.Lock()
        self._active_children = 0
        self._active_workers = 0

    @property
    def active_child_count(self):
        with self._accounting_lock:
            return self._active_children

    @property
    def active_worker_count(self):
        with self._accounting_lock:
            return self._active_workers

    @property
    def available_worker_slots(self):
        with self._accounting_lock:
            return self.MAX_WORKERS - self._active_workers

    @staticmethod
    def _terminate_and_reap(child):
        if child.is_alive():
            child.terminate()
            child.join(0.1)
        if child.is_alive():
            child.kill()
            child.join(0.1)
        if child.is_alive():
            child.join()

    def prepare(
        self, provider, *, settings, credentials, cancel_event, request_deadline
    ):
        """Prepare provider data, with prompt cancellation of blocked connects."""
        if cancel_event.is_set():
            raise PreviewProviderCancelled("preview provider preparation cancelled")

        if not self._worker_slots.acquire(blocking=False):
            raise RuntimeError("preview provider worker capacity exhausted")

        worker_accounted = False
        child_accounted = False
        child = None
        result_receiver = None
        result_sender = None
        try:
            with self._accounting_lock:
                self._active_workers += 1
                worker_accounted = True

            result_receiver, result_sender = self._context.Pipe(duplex=False)
            child = self._context.Process(
                target=_provider_child,
                args=(
                    provider,
                    settings,
                    credentials,
                    min(3.0, float(request_deadline)),
                    result_sender,
                ),
            )
            child.start()
            result_sender.close()
            result_sender = None
            with self._accounting_lock:
                self._active_children += 1
                child_accounted = True

            while True:
                if cancel_event.is_set():
                    result_receiver.close()
                    result_receiver = None
                    self._terminate_and_reap(child)
                    raise PreviewProviderCancelled(
                        "preview provider preparation cancelled"
                    )
                if result_receiver.poll():
                    result_receiver.recv()
                    child.join()
                    raise NotImplementedError(
                        "provider result handling is not implemented"
                    )
                if not child.is_alive():
                    child.join()
                    raise RuntimeError("preview provider child exited without a result")
                cancel_event.wait(_CANCEL_POLL_SECONDS)
        finally:
            if result_receiver is not None:
                result_receiver.close()
            if result_sender is not None:
                result_sender.close()
            if child is not None:
                self._terminate_and_reap(child)
                if child_accounted:
                    with self._accounting_lock:
                        self._active_children -= 1
                child.close()
            if worker_accounted:
                with self._accounting_lock:
                    self._active_workers -= 1
            self._worker_slots.release()
