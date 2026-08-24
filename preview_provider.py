"""Bounded provider preparation seam for Matrix edit previews."""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Mapping


_CANCEL_POLL_SECONDS = 0.005
_REAP_JOIN_SECONDS = 0.05
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


class PreviewProviderCapacityError(RuntimeError):
    """No bounded provider worker is available for this preparation."""


class PreviewProviderUnavailable(RuntimeError):
    """Provider preparation failed without exposing private child details."""


class PreviewProvider:
    """Fail-closed entry point for provider preparation.

    The pre-cancel check deliberately runs before creating multiprocessing or
    communication resources. Later provider I/O lifecycle slices extend this
    seam without changing that ordering.
    """

    MAX_WORKERS = 2
    MAX_REQUEST_SECONDS = 8.0
    _worker_slots = threading.BoundedSemaphore(MAX_WORKERS)
    _accounting_lock = threading.Lock()
    _active_children = 0
    _active_workers = 0

    def __init__(self):
        self._context = multiprocessing.get_context("spawn")
        self._lifecycle = threading.Condition()
        self._shutdown = False
        self._preparations = set()

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

    @property
    def is_closed(self):
        """Return whether this provider permanently rejects new preparation."""
        with self._lifecycle:
            return self._shutdown

    @staticmethod
    def _terminate_and_reap(child, deadline=None):
        def join_within_budget():
            timeout = _REAP_JOIN_SECONDS
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - time.monotonic()))
            child.join(timeout)

        if child.is_alive():
            child.terminate()
            join_within_budget()
        if child.is_alive():
            child.kill()
            join_within_budget()
        return not child.is_alive()

    def _handoff_reaping_child(self, child):
        """Retain capacity ownership until a stubborn child is really gone."""
        def reap():
            while not self._terminate_and_reap(child):
                time.sleep(_CANCEL_POLL_SECONDS)
            child.close()
            with type(self)._accounting_lock:
                type(self)._active_children -= 1
                type(self)._active_workers -= 1
            self._worker_slots.release()

        threading.Thread(
            target=reap,
            name="preview-provider-child-reaper",
            daemon=True,
        ).start()

    def _before_publication(self, _prepared):
        """Parent-side seam immediately before a prepared result is returned."""

    def shutdown(self):
        """Stop admitting work and wait for this instance's owners to reap it."""
        with self._lifecycle:
            self._shutdown = True
            for shutdown_event in self._preparations:
                shutdown_event.set()
            while self._preparations:
                self._lifecycle.wait()

    def prepare(
        self, provider, *, settings, credentials, cancel_event, request_deadline
    ):
        """Prepare provider data, with prompt cancellation of blocked connects."""
        if cancel_event.is_set():
            raise PreviewProviderCancelled("preview provider preparation cancelled")

        effective_timeout = min(self.MAX_REQUEST_SECONDS, float(request_deadline))
        deadline = time.monotonic() + effective_timeout
        shutdown_event = threading.Event()
        with self._lifecycle:
            if self._shutdown:
                raise RuntimeError("preview provider is shut down")
            self._preparations.add(shutdown_event)

        slot_acquired = False
        worker_accounted = False
        child_accounted = False
        child = None
        result_receiver = None
        result_sender = None
        try:
            if not self._worker_slots.acquire(blocking=False):
                raise PreviewProviderCapacityError(
                    "preview provider worker capacity exhausted"
                )
            slot_acquired = True
            if cancel_event.is_set() or shutdown_event.is_set():
                raise PreviewProviderCancelled(
                    "preview provider preparation cancelled"
                )

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
                if cancel_event.is_set() or shutdown_event.is_set():
                    result_receiver.close()
                    result_receiver = None
                    raise PreviewProviderCancelled(
                        "preview provider preparation cancelled"
                    )
                if time.monotonic() >= deadline:
                    result_receiver.close()
                    result_receiver = None
                    raise PreviewProviderTimeout(
                        "preview provider preparation timed out"
                    )
                if result_receiver.poll():
                    message = result_receiver.recv()
                    result_receiver.close()
                    result_receiver = None
                    if not self._terminate_and_reap(child, deadline):
                        self._handoff_reaping_child(child)
                        child = None
                        child_accounted = False
                        worker_accounted = False
                        slot_acquired = False
                    else:
                        if child_accounted:
                            with type(self)._accounting_lock:
                                type(self)._active_children -= 1
                            child_accounted = False
                    if cancel_event.is_set() or shutdown_event.is_set():
                        raise PreviewProviderCancelled(
                            "preview provider preparation cancelled"
                        )
                    if message[0] != "returned":
                        raise PreviewProviderUnavailable(
                            "preview provider preparation failed"
                        )
                    prepared = message[1]
                    self._before_publication(prepared)
                    if cancel_event.is_set() or shutdown_event.is_set():
                        raise PreviewProviderCancelled(
                            "preview provider preparation cancelled"
                        )
                    return prepared
                if not child.is_alive():
                    child.join(0)
                    raise PreviewProviderUnavailable(
                        "preview provider child exited without a result"
                    )
                cancel_event.wait(_CANCEL_POLL_SECONDS)
        finally:
            if result_receiver is not None:
                result_receiver.close()
            if result_sender is not None:
                result_sender.close()
            if child is not None:
                if not self._terminate_and_reap(child, deadline):
                    self._handoff_reaping_child(child)
                    child = None
                    child_accounted = False
                    worker_accounted = False
                    slot_acquired = False
                else:
                    if child_accounted:
                        with type(self)._accounting_lock:
                            type(self)._active_children -= 1
                    child.close()
            if worker_accounted:
                with type(self)._accounting_lock:
                    type(self)._active_workers -= 1
            if slot_acquired:
                self._worker_slots.release()
            with self._lifecycle:
                self._preparations.remove(shutdown_event)
                self._lifecycle.notify_all()
