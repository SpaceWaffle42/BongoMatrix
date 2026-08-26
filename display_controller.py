"""Single-owner display scheduler for Flask and the Matrix modules."""

from __future__ import annotations

import base64
import copy
import datetime as dt
import importlib
import io
import logging
import socket
import threading
import time

from PIL import Image

from modules.media_player import play_media
from device_preview import (
    DevicePreviewManager, PreviewAdmissionStatus, PreviewExpiryCause,
)
from preview_provider import (
    PreviewProvider,
    PreviewProviderCancelled,
    PreviewProviderCapacityError,
    PreviewProviderTimeout,
    PreviewProviderUnavailable,
)

LOGGER = logging.getLogger(__name__)
MODULES = {
    "announcement_board": "modules.announcement_board",
    "clock_lava": "modules.clock_lava",
    "weather": "modules.weather_app",
    "openweather_overview": "modules.openweather_overview",
    "openweather_forecast": "modules.openweather_forecast",
    "splash": "modules.splash_screen",
    "sleep": "modules.sleep_screen",
}

# One physical invocation when no enabled playlist row supplies a duration.
# These preserve the existing module wrapper defaults and dispatcher defaults.
MODULE_DEFAULT_DURATIONS = {
    "announcement_board": 15,
    "clock_lava": 300,
    "weather": 20,
    "openweather_overview": 20,
    "openweather_forecast": 30,
    "splash": 8,
    "sleep": 60,
}
ONE_OFF_PREVIEW_DURATION = 15
UNBOUNDED_DEVICE_PREVIEW_SLICE_SECONDS = 3600
OPENWEATHER_PREPARATION_DEADLINE_SECONDS = 5.0


def _provider_image(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _openweather_provider_child(config, api_key, timeout):
    """Prepare and serialize one physical OpenWeather invocation in a child."""
    module_name = config.pop("_openweather_provider_module")
    module = importlib.import_module(f"modules.{module_name}")
    from modules.openweather_common import OpenWeatherClient

    deadline = time.monotonic() + timeout
    prepared = module.prepare(
        config,
        client=OpenWeatherClient(timeout=timeout, deadline=deadline),
        api_key=api_key,
    )
    result = {"error": prepared.error, "cancelled": prepared.cancelled}
    if prepared.error or prepared.cancelled:
        return result
    if module_name == "openweather_overview":
        result.update({
            "weather": vars(prepared.weather),
            "icon": _provider_image(prepared.icon),
        })
    else:
        result.update({
            "periods": [vars(period) for period in prepared.periods],
            "icons": {
                icon_code: _provider_image(image)
                for icon_code, image in prepared.icons.items()
            },
        })
    return result


def _provider_value(value):
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {key: _provider_value(item) for key, item in value}
        return [_provider_value(item) for item in value]
    return value


def _prepared_openweather(module_name, transported):
    prepared = _provider_value(transported)
    if module_name == "openweather_overview":
        from modules.openweather_common import OpenWeatherSnapshot
        from modules.openweather_overview import PreparedOverview

        if prepared["error"] or prepared["cancelled"]:
            return PreparedOverview(
                error=prepared["error"], cancelled=prepared["cancelled"]
            )
        icon = Image.open(io.BytesIO(base64.b64decode(prepared["icon"])))
        icon.load()
        return PreparedOverview(
            weather=OpenWeatherSnapshot(**prepared["weather"]), icon=icon,
        )

    from modules.openweather_common import OpenWeatherForecastPeriod
    from modules.openweather_forecast import PreparedForecast

    if prepared["error"] or prepared["cancelled"]:
        return PreparedForecast(
            error=prepared["error"], cancelled=prepared["cancelled"]
        )
    icons = {}
    for icon_code, encoded in prepared["icons"].items():
        icon = Image.open(io.BytesIO(base64.b64decode(encoded)))
        icon.load()
        icons[icon_code] = icon
    return PreparedForecast(
        periods=[
            OpenWeatherForecastPeriod(**period) for period in prepared["periods"]
        ],
        icons=icons,
    )


def invocation_default_duration(module_name):
    """Return the current physical dispatcher/module invocation default."""
    if module_name.startswith("media:"):
        return ONE_OFF_PREVIEW_DURATION
    return MODULE_DEFAULT_DURATIONS.get(module_name, ONE_OFF_PREVIEW_DURATION)


def custom_module(config, module_name):
    return next(
        (item for item in config.get("custom_modules", []) if item.get("id") == module_name),
        None,
    )


def local_ip_address():
    """Return the LAN address used to reach the outside network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "IP unavailable"
    finally:
        sock.close()


class DisplayController:
    def __init__(
        self, matrix, config_store, ip_address_provider=None, openweather_secrets=None
    ):
        self.matrix = matrix
        self.config_store = config_store
        self._shutdown = threading.Event()
        self._reload_requested = threading.Event()
        self._pairing_pause = threading.Event()
        self._pairing_setup = threading.Event()
        self._display_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread = None
        self._stopping = False
        self._lifecycle_generation = 0
        self._module_stop = None
        self.device_preview_manager = DevicePreviewManager(
            on_expire=self._expire_device_draft,
        )
        self._device_draft_pending = None
        self._device_draft_active_lease = None
        self._device_draft_stop = None
        self._acceptance_transaction = None
        self._acceptance_completed = None
        self._preview_name = None
        self._media_preview_name = None
        self._announcement_preview = None
        self._pairing_timer = None
        self._pairing_page_timer = None
        self._pairing_pin = None
        self._pairing_ip = None
        self._pairing_show_ip = False
        self._pairing_generation = 0
        self._ip_address_provider = ip_address_provider or local_ip_address
        self._openweather_secrets = openweather_secrets
        self._openweather_provider = PreviewProvider()
        self.current_module = None
        self.last_error = None
        self._scheduler_fault_backoff = 0.25
        self._scheduler_next_fault_log = 0.0

    @property
    def running(self):
        with self._lifecycle_lock:
            return bool(self._thread and self._thread.is_alive())

    @property
    def pairing_active(self):
        return self._pairing_pause.is_set()

    def start(self):
        with self._lifecycle_lock:
            if self._stopping or (self._thread and self._thread.is_alive()):
                return False
            self._shutdown.clear()
            self._reload_requested.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="matrix-display",
                daemon=True,
            )
            self._thread.start()
            return True

    def _interrupt_module(self, timeout=None):
        if timeout is None:
            acquired = self._state_lock.acquire()
        else:
            acquired = self._state_lock.acquire(timeout=max(0, timeout))
        if not acquired:
            return False
        try:
            if self._module_stop is not None:
                self._module_stop.set()
            return True
        finally:
            self._state_lock.release()

    def stop(self, timeout=5):
        deadline = time.monotonic() + max(0, float(timeout))
        with self._lifecycle_lock:
            if self._stopping:
                return False
            self._stopping = True
            self._lifecycle_generation += 1
            self._shutdown.set()
            thread = self._thread
        try:
            self._terminate_device_draft(wait=False)
            if not self._interrupt_module(timeout=max(0, deadline - time.monotonic())):
                return False
            pairing_setup_active = self._pairing_setup.is_set()
            if not self.end_pairing(timeout=max(0, deadline - time.monotonic())):
                return False
            if pairing_setup_active:
                return False
            if thread and thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    LOGGER.error("Matrix display thread did not stop within %s seconds", timeout)
                    return False
            self.device_preview_manager.close()
            if not self._display_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                LOGGER.error("Matrix display lock was not released within %s seconds", timeout)
                return False
            try:
                self.matrix.clear()
                self.matrix.swap()
            finally:
                self._display_lock.release()
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
            return True
        finally:
            with self._lifecycle_lock:
                self._stopping = False

    def reload(self):
        self._terminate_device_draft()
        self._reload_requested.set()
        self._interrupt_module()

    def _terminate_device_draft(self, *, wait=True):
        with self._state_lock:
            stop_event = self._device_draft_stop
            self._device_draft_pending = None
            if stop_event is not None:
                stop_event.set()
        if wait:
            with self._display_lock:
                pass
            self.device_preview_manager.close()

    def shutdown_device_drafts(self):
        self._terminate_device_draft()
        self._reload_requested.set()

    def begin_acceptance_transaction(self, publisher, owner, *, generation):
        """Bind one release-only publisher to this exact admitted draft owner."""
        from acceptance_artifacts import AcceptanceArtifactTransaction

        if type(publisher) is not AcceptanceArtifactTransaction:
            raise TypeError("acceptance publisher type is invalid")
        if publisher.owner_generation != generation:
            raise ValueError("acceptance publisher owner is stale")
        with self._state_lock:
            if self._acceptance_transaction is not None:
                raise RuntimeError("an acceptance publisher is already active")
            baseline_frame, baseline_revision = self.matrix.latest_frame_rgb()
            if len(baseline_frame) != 64 * 32 * 3:
                raise ValueError("physical acceptance requires a 64x32 matrix")
            event_authority = object()
            context = {
                "publisher": publisher, "event_authority": event_authority,
                "owner": owner, "generation": generation,
                "baseline_revision": baseline_revision,
                "foreign_rejected": False, "heartbeat_accepted": False,
                "timeout_returned": False, "inactive_probe_pending": False,
                "inactive_sleep_executing": False,
                "draft_executing": False, "draft_execution_lease": None,
            }
            self._acceptance_transaction = context
            try:
                AcceptanceArtifactTransaction._bind_display_controller(
                    publisher, self, event_authority,
                )
            except BaseException:
                self._acceptance_transaction = None
                raise
            self._acceptance_completed = None

        def observe(frame, revision):
            with self._state_lock:
                if self._acceptance_transaction is not context:
                    return
                current = self.device_preview_manager.is_current(owner, generation)
                state = publisher.state
                if state == publisher.INIT:
                    draft_lease = context["draft_execution_lease"]
                    if (
                        not current or not context["draft_executing"]
                        or draft_lease is None
                        or self._device_draft_active_lease is not draft_lease
                    ):
                        return
                    publisher.publish_draft(frame, frame_revision=revision)
                    return
                if state != publisher.DRAFT_PUBLISHED or current:
                    return
                if not all(context[name] for name in (
                    "foreign_rejected", "heartbeat_accepted", "timeout_returned",
                    "inactive_sleep_executing",
                )):
                    return
                publisher._observe("sleep_observed", context["event_authority"])
                publisher.publish_saved_rollback(
                    frame, frame_revision=revision,
                    saved_revision=context["baseline_revision"],
                )
                self._acceptance_completed = {
                    "owner": owner, "generation": generation,
                    "status": publisher.status(),
                }
                self._acceptance_transaction = None
                self.matrix.set_physical_swap_observer(None)

        self.matrix.set_physical_swap_observer(observe)

    def mark_acceptance_result(self, owner, *, generation, result):
        if result != "heartbeat_accepted":
            raise ValueError("acceptance result is invalid")
        with self._state_lock:
            context = self._acceptance_transaction
            if (context is None or context["owner"] != owner
                    or context["generation"] != generation):
                return False
            context[result] = True
            context["publisher"]._observe("heartbeat_accepted", context["event_authority"])
            return True

    def mark_acceptance_foreign_rejection(self, owner, *, generation):
        with self._state_lock:
            context = self._acceptance_transaction
            if (context is None or context["generation"] != generation
                    or context["owner"] == owner):
                return False
            context["foreign_rejected"] = True
            context["publisher"]._observe("foreign_rejected", context["event_authority"])
            return True

    def acceptance_transaction_status(self, owner, *, generation):
        with self._state_lock:
            context = self._acceptance_transaction
            if (context is not None and context["owner"] == owner
                    and context["generation"] == generation):
                return context["publisher"].status()
            completed = self._acceptance_completed
            if (completed is not None and completed["owner"] == owner
                    and completed["generation"] == generation):
                return dict(completed["status"])
            return None

    def submit_device_draft(self, request, *, renderer):
        displaced = self.device_preview_manager.active_lease()
        admission = self.device_preview_manager.submit(
            request, close_displaced=False,
        )
        if admission.status is not PreviewAdmissionStatus.ACCEPTED:
            return admission
        with self._state_lock:
            if self._device_draft_stop is not None:
                self._device_draft_stop.set()
            if self._module_stop is not None:
                self._module_stop.set()
        if displaced is not None:
            with self._display_lock:
                pass
            self.device_preview_manager.finish(displaced)
        with self._state_lock:
            self._device_draft_pending = (admission.lease, renderer)
            if self._module_stop is not None:
                self._module_stop.set()
        self.start()
        return admission

    def cancel_device_drafts_for_device(self, device_id):
        lease = self.device_preview_manager.active_lease()
        if lease is None or lease.owner.device_id != device_id:
            return False
        return self.cancel_device_draft(lease.owner, generation=lease.generation)

    def heartbeat_device_draft(self, owner, *, generation):
        return self.device_preview_manager.heartbeat(owner, generation=generation)

    def _expire_device_draft(self, lease):
        with self._state_lock:
            acceptance = self._acceptance_transaction
            if (
                acceptance is not None
                and acceptance["owner"] == lease.request.owner
                and acceptance["generation"] == lease.request.generation
                and lease.expiry_cause is PreviewExpiryCause.CONTENT
                and acceptance["publisher"].state == acceptance["publisher"].DRAFT_PUBLISHED
            ):
                acceptance["timeout_returned"] = True
                acceptance["inactive_probe_pending"] = True
                acceptance["publisher"]._observe(
                    "timeout_returned", acceptance["event_authority"],
                )
            pending = self._device_draft_pending
            if pending is not None and pending[0].request is lease.request:
                self._device_draft_pending = None
            active = self._device_draft_active_lease
            if active is not None and active.request is lease.request:
                stop_event = self._device_draft_stop
                if stop_event is not None:
                    stop_event.set()
        with self._display_lock:
            pass
        self._reload_requested.set()

    def _acceptance_inactive_probe(self):
        with self._state_lock:
            context = self._acceptance_transaction
            if (
                context is None
                or not context["inactive_probe_pending"]
                or not all(context[name] for name in (
                    "foreign_rejected", "heartbeat_accepted", "timeout_returned",
                ))
            ):
                return None
            return context

    def _claim_acceptance_inactive_probe(self, context):
        with self._state_lock:
            if (
                self._acceptance_transaction is not context
                or not context["inactive_probe_pending"]
            ):
                return False
            context["inactive_probe_pending"] = False
            context["inactive_sleep_executing"] = True
            return True

    def _finish_acceptance_inactive_probe(self, context):
        with self._state_lock:
            if self._acceptance_transaction is context:
                context["inactive_sleep_executing"] = False

    def cancel_device_draft(self, owner, *, generation):
        lease = self.device_preview_manager.cancel(
            owner, generation=generation, close=False,
        )
        if lease is None:
            return False
        with self._state_lock:
            if self._device_draft_stop is not None:
                self._device_draft_stop.set()
            if self._device_draft_pending is not None and self._device_draft_pending[0] is lease:
                self._device_draft_pending = None
        with self._display_lock:
            pass
        self.device_preview_manager.finish(lease)
        self._reload_requested.set()
        return True

    def _execute_pending_device_draft(self):
        with self._state_lock:
            pending = self._device_draft_pending
            self._device_draft_pending = None
            if pending is None:
                return False
            lease, renderer = pending
            stop_event = threading.Event()
            self._device_draft_active_lease = lease
            self._device_draft_stop = stop_event
            self._module_stop = stop_event
        request = lease.request
        guard = lambda: (
            not stop_event.is_set()
            and self.device_preview_manager.is_current(request.owner, request.generation)
        )
        self.current_module = f"device-draft:{request.config.get('module', 'unknown')}"
        try:
            with self._display_lock:
                guard_installed = hasattr(self.matrix, "set_publication_guard")
                if guard_installed:
                    self.matrix.set_publication_guard(guard)
                acceptance_context = None
                try:
                    if not guard():
                        return False
                    with self._state_lock:
                        context = self._acceptance_transaction
                        if (
                            context is not None
                            and context["owner"] == request.owner
                            and context["generation"] == request.generation
                            and context["publisher"].state == context["publisher"].INIT
                        ):
                            context["draft_executing"] = True
                            context["draft_execution_lease"] = lease
                            acceptance_context = context
                    return bool(renderer(self.matrix, request, stop_event=stop_event))
                finally:
                    if acceptance_context is not None:
                        with self._state_lock:
                            if self._acceptance_transaction is acceptance_context:
                                acceptance_context["draft_executing"] = False
                                acceptance_context["draft_execution_lease"] = None
                    if guard_installed:
                        self.matrix.set_publication_guard(None)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            self.current_module = None
            with self._state_lock:
                if self._device_draft_active_lease is lease:
                    self._device_draft_active_lease = None
                if self._device_draft_stop is stop_event:
                    self._device_draft_stop = None
                if self._module_stop is stop_event:
                    self._module_stop = None
            terminal = self.device_preview_manager.cancel(
                request.owner, generation=request.generation, close=False,
            )
            if terminal is not None:
                self.device_preview_manager.finish(terminal)
                self._reload_requested.set()

    def render_device_draft(
        self, matrix, request, *, stop_event, prepared=None,
    ):
        """Render one validated transient request without consulting saved config."""
        module_name = request.config["module"]
        candidate = request.config["candidate"]
        if stop_event.is_set():
            return False
        custom = custom_module(candidate, module_name)
        if module_name in MODULES:
            module = importlib.import_module(MODULES[module_name])
        elif custom is not None:
            module = importlib.import_module("modules.custom_text")
        else:
            raise KeyError(module_name)
        options = {
            "duration": (
                UNBOUNDED_DEVICE_PREVIEW_SLICE_SECONDS
                if request.timeout_seconds is None
                else request.timeout_seconds
            ),
            "stop_event": stop_event,
        }
        if module_name == "clock_lava":
            options["selected_palette"] = request.config.get("selected_lava_palette")
        if module_name in {"splash", "sleep"}:
            filename = candidate["media"][module_name]
            pinned_handle = request.pinned_media.get(filename)
            if pinned_handle is None:
                raise ValueError("built-in device draft media is not pinned")
            options["pinned_handle"] = pinned_handle
        elif custom is not None:
            options["module_config"] = custom
            options["pinned_handles"] = request.pinned_media
        elif module_name == "weather" and prepared is not None:
            class PreparedWeatherClient:
                def fetch(self, *_args, **_kwargs):
                    return prepared
            options["client"] = PreparedWeatherClient()
        elif module_name in {"openweather_overview", "openweather_forecast"}:
            options["prepared"] = prepared
        if stop_event.is_set():
            return False
        rendered = False
        while not stop_event.is_set():
            rendered = module.run(matrix, candidate, **options)
            if request.timeout_seconds is not None or rendered is False:
                return rendered
            if stop_event.is_set() or stop_event.wait(0.01):
                break
        return rendered

    def preview(self, module_name):
        if module_name not in MODULES and custom_module(self.config_store.get(), module_name) is None:
            raise KeyError(module_name)
        with self._state_lock:
            self._preview_name = module_name
            self._media_preview_name = None
            self._announcement_preview = None
            if self._module_stop is not None:
                self._module_stop.set()
        self.start()

    def preview_media(self, filename):
        if not isinstance(filename, str) or not filename:
            raise ValueError("media filename is required")
        with self._state_lock:
            self._media_preview_name = filename
            self._preview_name = None
            self._announcement_preview = None
            if self._module_stop is not None:
                self._module_stop.set()
        self.start()

    def preview_announcement(self, announcement):
        """Queue a validated unsaved announcement as a one-off preview."""
        with self._state_lock:
            self._announcement_preview = copy.deepcopy(announcement)
            self._preview_name = None
            self._media_preview_name = None
            if self._module_stop is not None:
                self._module_stop.set()
        self.start()

    def status(self):
        return {
            "running": self.running,
            "current_module": self.current_module,
            "last_error": self.last_error,
            "pairing_active": self.pairing_active,
        }

    def frame_png(self):
        """Return the exact last frame submitted to the owned physical Matrix."""
        return self.matrix.latest_frame_png()

    def show_pairing_pin(self, pin, ttl_seconds=600):
        self._terminate_device_draft()
        with self._lifecycle_lock:
            if self._shutdown.is_set():
                return
            lifecycle_generation = self._lifecycle_generation
            self._pairing_setup.set()
        with self._state_lock:
            self._pairing_generation += 1
            generation = self._pairing_generation
        try:
            pairing_ip = self._ip_address_provider()
            with self._lifecycle_lock:
                lifecycle_current = (
                    lifecycle_generation == self._lifecycle_generation
                    and not self._shutdown.is_set()
                )
            if not lifecycle_current:
                self.end_pairing(generation)
                return
            self._interrupt_module()
            abort = False
            with self._state_lock:
                if self._shutdown.is_set():
                    abort = True
                elif generation != self._pairing_generation:
                    return
                else:
                    self._pairing_pause.set()
                    if self._pairing_timer is not None:
                        self._pairing_timer.cancel()
                    if self._pairing_page_timer is not None:
                        self._pairing_page_timer.cancel()
                    self._pairing_pin = str(pin)
                    self._pairing_ip = pairing_ip
                    self._pairing_show_ip = False
                    self._pairing_timer = threading.Timer(
                        max(1, float(ttl_seconds)), lambda: self.end_pairing(generation)
                    )
                    self._pairing_timer.daemon = True
                    self._pairing_timer.start()
                    self._pairing_page_timer = threading.Timer(
                        3, lambda: self._rotate_pairing_page(generation)
                    )
                    self._pairing_page_timer.daemon = True
                    self._pairing_page_timer.start()
            if abort:
                self.end_pairing(generation)
                return
            with self._lifecycle_lock:
                lifecycle_current = (
                    lifecycle_generation == self._lifecycle_generation
                    and not self._shutdown.is_set()
                )
            if not lifecycle_current:
                self.end_pairing(generation)
                return
            self._draw_pairing_page(show_ip=False, generation=generation)
        except Exception:
            self.end_pairing(generation)
            raise
        finally:
            self._pairing_setup.clear()

    def _draw_pairing_page(self, show_ip, generation=None):
        with self._display_lock:
            if (
                not self._pairing_pause.is_set()
                or generation is not None
                and generation != self._pairing_generation
            ):
                return
            self.matrix.clear()
            white = self.matrix.colour("white")
            cyan = self.matrix.colour("vapor_cyan")
            black = self.matrix.colour("black")
            if show_ip:
                self.matrix.draw_text_with_border(
                    self.matrix.font("4x6"), 18, 7, cyan, black, "PAIR IP"
                )
                self.matrix.draw_text_with_border(
                    self.matrix.font("4x6"), 8, 17, white, black, self._pairing_ip
                )
                self.matrix.draw_text_with_border(
                    self.matrix.font("4x6"), 14, 27, white, black, "PORT 5000"
                )
            else:
                self.matrix.draw_text_with_border(
                    self.matrix.font("5x7"), 20, 10, cyan, black, "PAIR"
                )
                self.matrix.draw_text_with_border(
                    self.matrix.font("6x10"), 12, 25, white, black, self._pairing_pin
                )
            self.matrix.swap()

    def _rotate_pairing_page(self, generation):
        with self._state_lock:
            if self._pairing_page_timer is not None:
                self._pairing_page_timer.cancel()
            if generation != self._pairing_generation or not self._pairing_pause.is_set():
                self._pairing_page_timer = None
                return
            self._pairing_show_ip = not self._pairing_show_ip
            show_ip = self._pairing_show_ip
            self._pairing_page_timer = threading.Timer(
                3, lambda: self._rotate_pairing_page(generation)
            )
            self._pairing_page_timer.daemon = True
            self._pairing_page_timer.start()
        self._draw_pairing_page(show_ip=show_ip, generation=generation)

    def end_pairing(self, generation=None, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        if deadline is None:
            state_acquired = self._state_lock.acquire()
        else:
            state_acquired = self._state_lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if not state_acquired:
            return False
        try:
            if generation is not None and generation != self._pairing_generation:
                return True
            self._pairing_generation += 1
            self._pairing_pause.clear()
            timer = self._pairing_timer
            page_timer = self._pairing_page_timer
            self._pairing_timer = None
            self._pairing_page_timer = None
            self._pairing_pin = None
            self._pairing_ip = None
        finally:
            self._state_lock.release()
        # A callback can pass its generation check before the state above changes.
        # Waiting on the display lock ensures that any such hardware draw has
        # finished before callers are told pairing has ended.
        if deadline is None:
            synchronized = self._display_lock.acquire()
        else:
            synchronized = self._display_lock.acquire(
                timeout=max(0, deadline - time.monotonic())
            )
        if synchronized:
            self._display_lock.release()
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        if page_timer is not None and page_timer is not threading.current_thread():
            page_timer.cancel()
        return synchronized

    @staticmethod
    def _is_active(config, now=None):
        now = now or dt.datetime.now()
        current = now.time()
        start = dt.time.fromisoformat(config["display"]["active_start"])
        end = dt.time.fromisoformat(config["display"]["active_end"])
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _take_preview(self):
        with self._state_lock:
            name = self._preview_name
            self._preview_name = None
            return name

    def _take_media_preview(self):
        with self._state_lock:
            filename = self._media_preview_name
            self._media_preview_name = None
            return filename

    def _take_announcement_preview(self):
        with self._state_lock:
            announcement = self._announcement_preview
            self._announcement_preview = None
            return announcement

    def _execute_announcement_preview(self, announcement):
        if self._shutdown.is_set() or self._pairing_pause.is_set():
            return False
        stop_event = threading.Event()
        module = importlib.import_module("modules.announcement_board")
        with self._state_lock:
            if (
                self._shutdown.is_set()
                or self._pairing_pause.is_set()
                or self._announcement_preview is not None
                or self._media_preview_name is not None
                or self._preview_name is not None
            ):
                return False
            self._module_stop = stop_event
        self.current_module = "announcement_board:preview"
        self.last_error = None
        try:
            with self._display_lock:
                return module.run_preview(
                    self.matrix,
                    announcement,
                    stop_event=stop_event,
                )
        except Exception as exc:
            LOGGER.exception("Matrix announcement preview failed")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            self.current_module = None
            with self._state_lock:
                if self._module_stop is stop_event:
                    self._module_stop = None

    def _execute_media(self, filename, duration):
        if self._shutdown.is_set() or self._pairing_pause.is_set():
            return False
        stop_event = threading.Event()
        with self._state_lock:
            if (
                self._shutdown.is_set()
                or self._pairing_pause.is_set()
                or self._announcement_preview is not None
                or self._media_preview_name is not None
                or self._preview_name is not None
            ):
                return False
            self._module_stop = stop_event
        self.current_module = f"media:{filename}"
        self.last_error = None
        try:
            with self._display_lock:
                return play_media(
                    self.matrix,
                    filename,
                    duration,
                    stop_event=stop_event,
                )
        except Exception as exc:
            LOGGER.exception("Matrix media preview %s failed", filename)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            self.current_module = None
            with self._state_lock:
                if self._module_stop is stop_event:
                    self._module_stop = None

    def _execute(self, name, config, duration):
        if self._shutdown.is_set() or self._pairing_pause.is_set():
            return False
        custom = custom_module(config, name)
        stop_event = threading.Event()
        with self._state_lock:
            if (
                self._device_draft_pending is not None
                or self._announcement_preview is not None
                or self._media_preview_name is not None
                or self._preview_name is not None
            ):
                return False
        if name in MODULES:
            module = importlib.import_module(MODULES[name])
        elif custom is not None:
            module = importlib.import_module("modules.custom_text")
        else:
            raise KeyError(name)
        with self._state_lock:
            if (
                self._shutdown.is_set()
                or self._pairing_pause.is_set()
                or self._device_draft_pending is not None
                or self._announcement_preview is not None
                or self._media_preview_name is not None
                or self._preview_name is not None
            ):
                return False
            self._module_stop = stop_event
        self.current_module = name
        self.last_error = None
        try:
            options = {
                "duration": duration,
                "stop_event": stop_event,
            }
            if custom is not None:
                options["module_config"] = custom
            if name in {"openweather_overview", "openweather_forecast"}:
                api_key = self._openweather_secrets.get() if self._openweather_secrets else None
                options["api_key"] = api_key
                if not api_key:
                    transported = {"error": "KEY NOT SET", "cancelled": False}
                else:
                    provider_config = copy.deepcopy(config)
                    provider_config["_openweather_provider_module"] = name
                    try:
                        transported = self._openweather_provider.prepare(
                            _openweather_provider_child,
                            settings=provider_config,
                            credentials=api_key,
                            cancel_event=stop_event,
                            request_deadline=OPENWEATHER_PREPARATION_DEADLINE_SECONDS,
                        )
                    except PreviewProviderCancelled:
                        return False
                    except PreviewProviderTimeout:
                        transported = {
                            "error": "OpenWeather request timed out",
                            "cancelled": False,
                        }
                    except (PreviewProviderCapacityError, PreviewProviderUnavailable):
                        transported = {
                            "error": "OpenWeather unavailable",
                            "cancelled": False,
                        }
                if stop_event.is_set():
                    return False
                options["prepared"] = _prepared_openweather(name, transported)
            with self._display_lock:
                return module.run(
                    self.matrix,
                    config,
                    **options,
                )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.current_module = None
            with self._state_lock:
                if self._module_stop is stop_event:
                    self._module_stop = None

    def _wait_while_pairing(self):
        while self._pairing_pause.is_set() and not self._shutdown.is_set():
            self._shutdown.wait(0.25)

    def _run_loop(self):
        while not self._shutdown.is_set():
            try:
                self._run_loop_unchecked()
            except Exception as exc:
                now = time.monotonic()
                if now >= self._scheduler_next_fault_log:
                    LOGGER.exception("Matrix scheduler failed; retrying with backoff")
                    self._scheduler_next_fault_log = now + 30
                self.last_error = f"{type(exc).__name__}: {exc}"
                delay = self._scheduler_fault_backoff
                self._scheduler_fault_backoff = min(delay * 2, 30)
                self._shutdown.wait(delay)

    def _mark_scheduler_healthy(self):
        self._scheduler_fault_backoff = 0.25
        self._scheduler_next_fault_log = 0.0

    def _run_loop_unchecked(self):
        while not self._shutdown.is_set():
            self._wait_while_pairing()
            if self._shutdown.is_set():
                break
            self._reload_requested.clear()
            with self._state_lock:
                device_draft_pending = self._device_draft_pending is not None
            if device_draft_pending:
                self._execute_pending_device_draft()
                self._mark_scheduler_healthy()
                continue
            config = self.config_store.get()

            announcement_preview = self._take_announcement_preview()
            if announcement_preview:
                self._execute_announcement_preview(announcement_preview)
                self._mark_scheduler_healthy()
                continue
            media_preview = self._take_media_preview()
            if media_preview:
                self._execute_media(
                    media_preview, invocation_default_duration(f"media:{media_preview}")
                )
                self._mark_scheduler_healthy()
                continue
            preview = self._take_preview()
            if preview:
                self._execute(preview, config, invocation_default_duration(preview))
                self._mark_scheduler_healthy()
                continue

            acceptance_inactive = self._acceptance_inactive_probe()
            active = False if acceptance_inactive is not None else self._is_active(config)
            brightness = (
                config["display"]["day_brightness"]
                if active
                else config["display"]["night_brightness"]
            )
            with self._display_lock:
                if (
                    self._shutdown.is_set()
                    or self._reload_requested.is_set()
                    or self._pairing_pause.is_set()
                ):
                    continue
                self.matrix.set_brightness(brightness)
            if not active:
                if (
                    acceptance_inactive is not None
                    and not self._claim_acceptance_inactive_probe(acceptance_inactive)
                ):
                    continue
                try:
                    self._execute("sleep", config, invocation_default_duration("sleep"))
                finally:
                    if acceptance_inactive is not None:
                        self._finish_acceptance_inactive_probe(acceptance_inactive)
                self._mark_scheduler_healthy()
                continue

            playlist = [item for item in config["playlist"] if item["enabled"]]
            if not playlist:
                self._shutdown.wait(1)
                self._mark_scheduler_healthy()
                continue

            cycle_rendered = False
            for item in playlist:
                if (
                    self._shutdown.is_set()
                    or self._reload_requested.is_set()
                    or self._pairing_pause.is_set()
                ):
                    break
                with self._state_lock:
                    if self._device_draft_pending is not None:
                        break
                announcement_preview = self._take_announcement_preview()
                if announcement_preview:
                    cycle_rendered = bool(
                        self._execute_announcement_preview(announcement_preview)
                    ) or cycle_rendered
                    break
                media_preview = self._take_media_preview()
                if media_preview:
                    cycle_rendered = bool(
                        self._execute_media(
                            media_preview,
                            invocation_default_duration(f"media:{media_preview}"),
                        )
                    ) or cycle_rendered
                    break
                preview = self._take_preview()
                if preview:
                    cycle_rendered = bool(
                        self._execute(
                            preview, config, invocation_default_duration(preview)
                        )
                    ) or cycle_rendered
                    break
                cycle_rendered = bool(
                    self._execute(item["module"], config, item["duration"])
                ) or cycle_rendered
            if (
                not cycle_rendered
                and not self._shutdown.is_set()
                and not self._reload_requested.is_set()
                and not self._pairing_pause.is_set()
            ):
                self._shutdown.wait(0.25)
            self._mark_scheduler_healthy()
