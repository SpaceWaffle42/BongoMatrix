"""Single-owner display scheduler for Flask and the Matrix modules."""

from __future__ import annotations

import datetime as dt
import importlib
import logging
import socket
import threading
import time

from modules.media_player import play_media

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
        self._preview_name = None
        self._media_preview_name = None
        self._pairing_timer = None
        self._pairing_page_timer = None
        self._pairing_pin = None
        self._pairing_ip = None
        self._pairing_show_ip = False
        self._pairing_generation = 0
        self._ip_address_provider = ip_address_provider or local_ip_address
        self._openweather_secrets = openweather_secrets
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
        self._reload_requested.set()
        self._interrupt_module()

    def preview(self, module_name):
        if module_name not in MODULES and custom_module(self.config_store.get(), module_name) is None:
            raise KeyError(module_name)
        with self._state_lock:
            self._preview_name = module_name
            self._media_preview_name = None
            if self._module_stop is not None:
                self._module_stop.set()
        self.start()

    def preview_media(self, filename):
        if not isinstance(filename, str) or not filename:
            raise ValueError("media filename is required")
        with self._state_lock:
            self._media_preview_name = filename
            self._preview_name = None
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

    def _execute_media(self, filename, duration):
        if self._shutdown.is_set() or self._pairing_pause.is_set():
            return False
        stop_event = threading.Event()
        with self._state_lock:
            if (
                self._shutdown.is_set()
                or self._pairing_pause.is_set()
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
            if self._media_preview_name is not None or self._preview_name is not None:
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
                options["prepared"] = module.prepare(
                    config,
                    stop_event=stop_event,
                    api_key=api_key,
                )
                if stop_event.is_set():
                    return False
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
            config = self.config_store.get()

            media_preview = self._take_media_preview()
            if media_preview:
                self._execute_media(media_preview, 15)
                self._mark_scheduler_healthy()
                continue
            preview = self._take_preview()
            if preview:
                self._execute(preview, config, 15)
                self._mark_scheduler_healthy()
                continue

            active = self._is_active(config)
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
                self._execute("sleep", config, 60)
                self._mark_scheduler_healthy()
                continue

            playlist = [item for item in config["playlist"] if item["enabled"]]
            if not playlist:
                self._shutdown.wait(1)
                self._mark_scheduler_healthy()
                continue

            for item in playlist:
                if (
                    self._shutdown.is_set()
                    or self._reload_requested.is_set()
                    or self._pairing_pause.is_set()
                ):
                    break
                media_preview = self._take_media_preview()
                if media_preview:
                    self._execute_media(media_preview, 15)
                    break
                preview = self._take_preview()
                if preview:
                    self._execute(preview, config, 15)
                    break
                self._execute(item["module"], config, item["duration"])
            self._mark_scheduler_healthy()
