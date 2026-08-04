"""Single-owner display scheduler for Flask and the Matrix modules."""

from __future__ import annotations

import datetime as dt
import importlib
import logging
import socket
import threading

from modules.media_player import play_media

LOGGER = logging.getLogger(__name__)
MODULES = {
    "clock_lava": "modules.clock_lava",
    "weather": "modules.weather_app",
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
    def __init__(self, matrix, config_store, ip_address_provider=None):
        self.matrix = matrix
        self.config_store = config_store
        self._shutdown = threading.Event()
        self._reload_requested = threading.Event()
        self._pairing_pause = threading.Event()
        self._display_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread = None
        self._module_stop = None
        self._preview_name = None
        self._media_preview_name = None
        self._pairing_timer = None
        self._pairing_page_timer = None
        self._pairing_pin = None
        self._pairing_ip = None
        self._pairing_show_ip = False
        self._ip_address_provider = ip_address_provider or local_ip_address
        self.current_module = None
        self.last_error = None

    @property
    def running(self):
        with self._lifecycle_lock:
            return bool(self._thread and self._thread.is_alive())

    @property
    def pairing_active(self):
        return self._pairing_pause.is_set()

    def start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._reload_requested.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="matrix-display",
                daemon=True,
            )
            self._thread.start()

    def _interrupt_module(self):
        with self._state_lock:
            if self._module_stop is not None:
                self._module_stop.set()

    def stop(self, timeout=5):
        self._shutdown.set()
        self._interrupt_module()
        self.end_pairing()
        with self._lifecycle_lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        with self._display_lock:
            self.matrix.clear()
            self.matrix.swap()

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
        self._pairing_pause.set()
        self._interrupt_module()
        with self._state_lock:
            if self._pairing_timer is not None:
                self._pairing_timer.cancel()
            if self._pairing_page_timer is not None:
                self._pairing_page_timer.cancel()
            self._pairing_pin = str(pin)
            self._pairing_ip = self._ip_address_provider()
            self._pairing_show_ip = False
            self._pairing_timer = threading.Timer(
                max(1, float(ttl_seconds)), self.end_pairing
            )
            self._pairing_timer.daemon = True
            self._pairing_timer.start()
            self._pairing_page_timer = threading.Timer(3, self._rotate_pairing_page)
            self._pairing_page_timer.daemon = True
            self._pairing_page_timer.start()
        self._draw_pairing_page(show_ip=False)

    def _draw_pairing_page(self, show_ip):
        with self._display_lock:
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

    def _rotate_pairing_page(self):
        with self._state_lock:
            if self._pairing_page_timer is not None:
                self._pairing_page_timer.cancel()
            if not self._pairing_pause.is_set():
                self._pairing_page_timer = None
                return
            self._pairing_show_ip = not self._pairing_show_ip
            show_ip = self._pairing_show_ip
            self._pairing_page_timer = threading.Timer(3, self._rotate_pairing_page)
            self._pairing_page_timer.daemon = True
            self._pairing_page_timer.start()
        self._draw_pairing_page(show_ip=show_ip)

    def end_pairing(self):
        self._pairing_pause.clear()
        with self._state_lock:
            timer = self._pairing_timer
            page_timer = self._pairing_page_timer
            self._pairing_timer = None
            self._pairing_page_timer = None
            self._pairing_pin = None
            self._pairing_ip = None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        if page_timer is not None and page_timer is not threading.current_thread():
            page_timer.cancel()

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
            with self._display_lock:
                options = {
                    "duration": duration,
                    "stop_event": stop_event,
                }
                if custom is not None:
                    options["module_config"] = custom
                return module.run(
                    self.matrix,
                    config,
                    **options,
                )
        except Exception as exc:
            LOGGER.exception("Matrix module %s failed", name)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
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
            self._wait_while_pairing()
            if self._shutdown.is_set():
                break
            self._reload_requested.clear()
            config = self.config_store.get()

            media_preview = self._take_media_preview()
            if media_preview:
                self._execute_media(media_preview, 15)
                continue
            preview = self._take_preview()
            if preview:
                self._execute(preview, config, 15)
                continue

            active = self._is_active(config)
            brightness = (
                config["display"]["day_brightness"]
                if active
                else config["display"]["night_brightness"]
            )
            self.matrix.set_brightness(brightness)


            if not active:
                self._execute("sleep", config, 60)
                continue

            playlist = [item for item in config["playlist"] if item["enabled"]]
            if not playlist:
                self._shutdown.wait(1)
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
