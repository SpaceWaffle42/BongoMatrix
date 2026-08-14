"""Bounded provider preparation seam for Matrix edit previews."""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Mapping


_CANCEL_POLL_SECONDS = 0.005
_SENSITIVE_RESULT_KEYS = frozenset(
    {"api_key", "credential", "credentials", "password", "secret", "token"}
)


def _sanitize_provider_result(value):
    """Copy provider data into a secret-free, transitively immutable value."""
    if isinstance(value, Mapping):
        sanitized = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("provider result keys must be strings")
            if key.casefold() in _SENSITIVE_RESULT_KEYS:
                continue
            sanitized.append((key, _sanitize_provider_result(item)))
        return tuple(sorted(sanitized))
    if isinstance(value, (list, tuple)):
        return tuple(_sanitize_provider_result(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("provider result contains an unsupported value")


def _provider_child(provider, settings, credentials, timeout, result_sender):
    """Run provider work in a spawn-safe process without returning secrets."""
    try:
        result = provider(settings, credentials, timeout)
        result_sender.send(("returned", _sanitize_provider_result(result)))
    except BaseException:
        result_sender.send(("failed",))
    finally:
        result_sender.close()


class PreviewProviderCancelled(RuntimeError):
    """Provider preparation was cancelled before a result could be published."""


class PreviewProviderTimeout(RuntimeError):
    """Provider preparation exceeded its effective hard deadline."""


class PreviewProvider:
    """Fail-closed entry point for provider preparation.

    The pre-cancel check deliberately runs before creating multiprocessing or
    communication resources. Later provider I/O lifecycle slices extend this
    seam without changing that ordering.
    """

    MAX_WORKERS = 2
    _worker_slots = threading.BoundedSemaphore(MAX_WORKERS)
    _accounting_lock = threading.Lock()
    _active_children = 0
    _active_workers = 0

    def __init__(self):
        self._context = multiprocessing.get_context("spawn")

    @property
    def active_child_count(self):
        with type(self)._accounting_lock:
            return type(self)._active_children

    @property
    def active_worker_count(self):
        with type(self)._accounting_lock:
            return type(self)._active_workers

    @property
    def available_worker_slots(self):
        with type(self)._accounting_lock:
            return self.MAX_WORKERS - type(self)._active_workers

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

    def _before_publication(self, _prepared):
        """Parent-side seam immediately before a prepared result is returned."""

    def prepare(
        self, provider, *, settings, credentials, cancel_event, request_deadline
    ):
        """Prepare provider data, with prompt cancellation of blocked connects."""
        if cancel_event.is_set():
            raise PreviewProviderCancelled("preview provider preparation cancelled")

        if not self._worker_slots.acquire(blocking=False):
            raise RuntimeError("preview provider worker capacity exhausted")

        effective_timeout = min(3.0, float(request_deadline))
        deadline = time.monotonic() + effective_timeout
        worker_accounted = False
        child_accounted = False
        child = None
        result_receiver = None
        result_sender = None
        try:
            with type(self)._accounting_lock:
                type(self)._active_workers += 1
                worker_accounted = True

            result_receiver, result_sender = self._context.Pipe(duplex=False)
            child = self._context.Process(
                target=_provider_child,
                args=(
                    provider,
                    settings,
                    credentials,
                    effective_timeout,
                    result_sender,
                ),
            )
            child.start()
            result_sender.close()
            result_sender = None
            with type(self)._accounting_lock:
                type(self)._active_children += 1
                child_accounted = True

            while True:
                if cancel_event.is_set():
                    result_receiver.close()
                    result_receiver = None
                    self._terminate_and_reap(child)
                    raise PreviewProviderCancelled(
                        "preview provider preparation cancelled"
                    )
                if time.monotonic() >= deadline:
                    result_receiver.close()
                    result_receiver = None
                    self._terminate_and_reap(child)
                    raise PreviewProviderTimeout(
                        "preview provider preparation timed out"
                    )
                if result_receiver.poll():
                    message = result_receiver.recv()
                    result_receiver.close()
                    result_receiver = None
                    if cancel_event.is_set():
                        self._terminate_and_reap(child)
                        raise PreviewProviderCancelled(
                            "preview provider preparation cancelled"
                        )
                    child.join()
                    if message[0] != "returned":
                        raise RuntimeError("preview provider preparation failed")
                    prepared = message[1]
                    self._before_publication(prepared)
                    if cancel_event.is_set():
                        raise PreviewProviderCancelled(
                            "preview provider preparation cancelled"
                        )
                    return prepared
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
                    with type(self)._accounting_lock:
                        type(self)._active_children -= 1
                child.close()
            if worker_accounted:
                with type(self)._accounting_lock:
                    type(self)._active_workers -= 1
            self._worker_slots.release()
