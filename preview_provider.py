"""Bounded provider preparation seam for Matrix edit previews."""

from __future__ import annotations

import multiprocessing


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
        self._active_children = 0
        self._active_workers = 0

    @property
    def active_child_count(self):
        return self._active_children

    @property
    def active_worker_count(self):
        return self._active_workers

    @property
    def available_worker_slots(self):
        return self.MAX_WORKERS - self._active_workers

    def prepare(
        self, provider, *, settings, credentials, cancel_event, request_deadline
    ):
        """Prepare provider data, rejecting pre-cancelled work without resources."""
        if cancel_event.is_set():
            raise PreviewProviderCancelled("preview provider preparation cancelled")
        raise NotImplementedError("provider execution is not implemented")
