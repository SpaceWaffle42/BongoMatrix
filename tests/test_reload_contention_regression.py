"""Public reload intent/cancellation must precede renderer settlement."""
import threading
import unittest
from unittest.mock import Mock

from display_controller import DisplayController


class ReloadContentionRegressionTests(unittest.TestCase):
    def test_reload_publishes_intent_before_waiting_for_state_lock(self):
        self._check_contention("_state_lock", check_cancellation=False)

    def test_reload_interrupts_module_before_waiting_for_display_lock(self):
        self._check_contention("_display_lock", check_cancellation=True)

    def _check_contention(self, lock_name, *, check_cancellation):
        controller = DisplayController(Mock(), Mock())
        module_stop = threading.Event()
        draft_stop = threading.Event()
        controller._module_stop = module_stop
        controller._device_draft_stop = draft_stop
        controller._device_draft_pending = (None, Mock())
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def hold_lock():
            with getattr(controller, lock_name):
                entered.set()
                release.wait(1)

        def reload():
            try:
                controller.reload()
            except BaseException as error:
                errors.append(error)

        holder = threading.Thread(target=hold_lock)
        worker = threading.Thread(target=reload)
        holder.start()
        try:
            self.assertTrue(entered.wait(0.5))
            worker.start()
            self.assertTrue(controller._reload_requested.wait(0.25),
                            "public reload waits for a contended lock before publishing intent")
            if check_cancellation:
                self.assertTrue(module_stop.wait(0.25),
                                "reload waits for renderer settlement before interrupting it")
                self.assertTrue(draft_stop.is_set())
        finally:
            release.set()
            holder.join(1)
            if worker.ident is not None:
                worker.join(1)
        self.assertFalse(holder.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(controller._device_draft_pending)
        self.assertTrue(module_stop.is_set())
        self.assertTrue(draft_stop.is_set())
