"""Capture-only Matrix adapter and shared preview frame seams."""

from __future__ import annotations

import base64
import copy
import io
import json
import math
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from frame_capture import BdfFont, FrameCapture


class _Canvas:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class CaptureMatrix:
    """Matrix primitive adapter backed by the production framebuffer mirror."""

    def __init__(self, width=64, height=32):
        self.width = int(width)
        self.height = int(height)
        self.canvas = _Canvas(self.width, self.height)
        self.matrix = SimpleNamespace(
            width=self.width, height=self.height, CreateFrameCanvas=self._new_canvas,
        )
        self._capture = FrameCapture(self.width, self.height)
        self._capture.register(self.canvas)

    def _new_canvas(self):
        canvas = _Canvas(self.width, self.height)
        self._capture.register(canvas)
        return canvas

    @staticmethod
    def colour_rgb(value):
        return SimpleNamespace(
            red=int(value[1:3], 16), green=int(value[3:5], 16), blue=int(value[5:7], 16),
        )

    @staticmethod
    def colour(name):
        from module_options import COLOUR_HEX

        return CaptureMatrix.colour_rgb(COLOUR_HEX[name])

    @staticmethod
    def font(name):
        return BdfFont(Path(__file__).parent / "resources" / "fonts" / f"{name}.bdf")

    def clear(self, canvas=None):
        self._capture.clear(canvas or self.canvas)

    def fill(self, red, green, blue, canvas=None):
        self._capture.fill(canvas or self.canvas, red, green, blue)

    def pixel(self, x, y, red, green, blue, canvas=None):
        self._capture.pixel(canvas or self.canvas, x, y, red, green, blue)

    def draw_line(self, x0, y0, x1, y1, colour, width=1, canvas=None):
        self._capture.line(canvas or self.canvas, x0, y0, x1, y1, colour, width=width)

    def draw_circle(self, x, y, radius, colour, width=1, filled=False, canvas=None):
        self._capture.circle(
            canvas or self.canvas, x, y, radius, colour, width=width, filled=filled,
        )

    def draw_image(
        self, image, x=0, y=0, width=None, height=None, keep_aspect=True,
        rotation=0, canvas=None,
    ):
        prepared = image.convert("RGB")
        if width is not None and height is not None:
            if keep_aspect:
                prepared = prepared.copy()
                prepared.thumbnail((width, height))
            else:
                prepared = prepared.resize((width, height))
        if rotation:
            prepared = prepared.rotate(-rotation, expand=False)
        self._capture.set_image(canvas or self.canvas, prepared, x, y)
        return prepared

    def draw_text(self, font, x, y, colour, text, canvas=None, rotation=0):
        canvas = canvas or self.canvas
        if not rotation % 360:
            return self._capture.text(canvas, font, x, y, colour, text)

        advance, points = font.text_pixels(x, y, text)
        if not points:
            return int(x) + advance
        centre_x = (min(px for px, _ in points) + max(px for px, _ in points)) / 2
        centre_y = (min(py for _, py in points) + max(py for _, py in points)) / 2
        angle = math.radians(rotation)
        cosine, sine = math.cos(angle), math.sin(angle)
        for px, py in points:
            dx, dy = px - centre_x, py - centre_y
            self._capture.pixel(
                canvas,
                round(centre_x + dx * cosine - dy * sine),
                round(centre_y + dx * sine + dy * cosine),
                colour.red,
                colour.green,
                colour.blue,
            )
        return int(x) + advance

    def snapshot(self, canvas=None):
        """Return an isolated copy without changing publication or revision state."""
        return self._capture.snapshot(canvas or self.canvas)

    def swap(self, canvas=None):
        """Capture a submitted preview canvas and return a fresh drawing canvas."""
        submitted = canvas or self.canvas
        self.canvas = self._new_canvas()
        return self.canvas


def static_media_frames(
    filename, duration, settings=None, *, render=None, monotonic=time.monotonic,
):
    """Yield one rendered still and its hold clipped to the invocation deadline."""
    geometry = {}
    if settings:
        geometry = {
            "x": settings["x"],
            "y": settings["y"],
            "width": settings["width"],
            "height": settings["height"],
            "keep_aspect": settings["keep_aspect"],
        }
    duration = max(0.0, float(duration))
    if duration == 0:
        return
    if render is None:
        yield filename, duration * 1000, geometry
        return
    started = monotonic()
    if not render(filename, geometry):
        return
    remaining = max(0.0, duration - (monotonic() - started))
    if remaining > 0:
        yield filename, remaining * 1000, geometry


def media_preview_frames(
    matrix, filename, duration, settings=None, *, video_decoder=None,
    stop_event=None,
):
    """Render the shared production animated-media timeline into PNG frames."""
    from PIL import Image, ImageSequence

    from modules.media_player import gif_media_frames, video_media_frames

    settings = settings or {}
    path = Path(filename)
    if not path.is_absolute():
        path = Path(__file__).parent / "resources" / "media" / path
    if path.suffix.lower() == ".gif":
        timeline = gif_media_frames(
            path, loops=0,
            playback_rate=settings.get("gif_speed_percent", 100) / 100.0,
            max_duration=duration, image_open=Image.open,
            image_iterator=ImageSequence.Iterator,
        )
    elif path.suffix.lower() == ".mp4":
        if video_decoder is None:
            video_decoder = getattr(matrix, "_video_frame_timeline", None)
        if video_decoder is None:
            raise ValueError("MP4 preview requires the production video decoder seam")
        timeline = video_media_frames(
            lambda: video_decoder(path, settings, stop_event=stop_event),
            fps=settings.get("video_fps", 20), loops=0,
            stop_event=stop_event, max_duration=duration,
        )
    else:
        raise ValueError("animated preview media must be GIF or MP4")
    for image, delay in timeline:
        matrix.clear()
        matrix.draw_image(
            image,
            x=settings.get("x", 0), y=settings.get("y", 0),
            width=settings.get("width"), height=settings.get("height"),
            keep_aspect=settings.get("keep_aspect", True),
        )
        yield _png_frame(matrix.snapshot(), round(delay * 1000, 9))
        matrix.swap()


def preview_frames(iterator):
    """Consume the same resumable frame iterator as physical wrappers."""
    yield from iterator


def preview_duration(config, module_name, default=None):
    """Resolve the first enabled playlist match or its physical default."""
    for item in config.get("playlist", []):
        if item.get("enabled") and item.get("module") == module_name:
            return item["duration"]
    if default is not None:
        return default
    from display_controller import invocation_default_duration

    return invocation_default_duration(module_name)


def lava_preview_frames(matrix, config):
    """Render one finite Lava invocation through the physical frame seam."""
    from modules import clock_lava

    duration = max(0.0, float(preview_duration(config, "clock_lava")))
    state = clock_lava.prepare_lava_frame_state(matrix, config.get("lava"))
    canvas = matrix.matrix.CreateFrameCanvas()
    temp_canvas = matrix.matrix.CreateFrameCanvas()
    elapsed = 0.0
    while elapsed < duration:
        clock_lava.render_lava_frame(matrix, state, canvas, temp_canvas)
        delay = min(clock_lava.FRAME_DELAY, duration - elapsed)
        yield _png_frame(matrix.snapshot(canvas), round(delay * 1000, 9))
        elapsed += delay
        canvas = matrix.swap(canvas)


def continuous_lava_preview_frames(matrix, config, selected_palette, stop_event=None):
    """Yield continuous physical Lava frames for one inspected palette."""
    from modules import clock_lava

    lava = copy.deepcopy(config["lava"])
    kind = selected_palette["kind"]
    palette_id = selected_palette["id"]
    lava["custom_enabled"] = False
    if kind == "built_in":
        lava["enabled_palettes"] = [palette_id]
        for palette in lava.get("custom_palettes", []):
            palette["enabled"] = False
    elif kind == "custom":
        lava["enabled_palettes"] = []
        matched = False
        for palette in lava.get("custom_palettes", []):
            palette["enabled"] = palette["id"] == palette_id
            if palette["enabled"]:
                palette["colours"] = copy.deepcopy(selected_palette["colours"])
                matched = True
        if not matched:
            raise ValueError("selected custom Lava palette is not in the candidate")
    else:
        raise ValueError("selected Lava palette kind is invalid")

    state = clock_lava.prepare_lava_frame_state(matrix, lava)
    canvas = matrix.matrix.CreateFrameCanvas()
    temp_canvas = matrix.matrix.CreateFrameCanvas()
    while not (stop_event and stop_event.is_set()):
        clock_lava.render_lava_frame(matrix, state, canvas, temp_canvas)
        if stop_event and stop_event.is_set():
            return
        yield _png_frame(matrix.snapshot(canvas), clock_lava.FRAME_DELAY * 1000)
        canvas = matrix.swap(canvas)


def _png_frame(image, delay_ms):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return {
        "png": base64.b64encode(output.getvalue()).decode("ascii"),
        "delay_ms": delay_ms,
    }


def advanced_canvas_preview_frames(
    matrix, module_config, duration, stop_event=None, *, pinned_handles=None,
):
    """Encode the shared Advanced Canvas invocation timeline without sleeping."""
    from modules.custom_text import canvas_timeline_frames

    for canvas, delay in canvas_timeline_frames(
        matrix, duration, stop_event, module_config, pinned_handles=pinned_handles,
    ):
        yield _png_frame(matrix.snapshot(canvas), round(delay * 1000, 9))


def custom_text_preview_frames(matrix, module_config):
    """Yield exact ticker capture frames using physical scroll positions."""
    from modules.custom_text import scroll_positions

    font = matrix.font(module_config["font"])
    foreground = matrix.colour_rgb(module_config["colour"])
    background = matrix.colour_rgb(module_config["background"])
    measure_canvas = matrix.matrix.CreateFrameCanvas()
    matrix.clear(measure_canvas)
    text_width = matrix.draw_text(
        font, 0, module_config["y"], foreground, module_config["text"],
        canvas=measure_canvas,
    )
    canvas = matrix.matrix.CreateFrameCanvas()
    for position in scroll_positions(
        matrix.width, text_width, module_config.get("loops", 1),
        module_config.get("direction", "left"),
    ):
        matrix.fill(background.red, background.green, background.blue, canvas=canvas)
        matrix.draw_text(
            font, position, module_config["y"], foreground, module_config["text"],
            canvas=canvas,
        )
        yield _png_frame(matrix.snapshot(canvas), module_config["speed"] * 1000)
        canvas = matrix.swap(canvas)


def _weather_preview_frames(matrix, iterator):
    for canvas, delay in iterator:
        yield _png_frame(matrix.snapshot(canvas), round(delay * 1000, 9))


def weather_preview_frames(matrix, config, prepared, stop_event=None):
    """Encode the shared prepared Open-Meteo physical frame iterator."""
    from modules.weather_app import weather_frames

    yield from _weather_preview_frames(
        matrix, weather_frames(matrix, config, prepared, stop_event),
    )


def openweather_overview_preview_frames(matrix, config, prepared, stop_event=None):
    """Encode the shared prepared OpenWeather overview frame iterator."""
    from modules.openweather_overview import overview_frames

    yield from _weather_preview_frames(
        matrix, overview_frames(matrix, config, prepared, stop_event),
    )


def openweather_forecast_preview_frames(matrix, config, prepared, stop_event=None):
    """Encode the shared prepared OpenWeather forecast frame iterator."""
    from modules.openweather_forecast import forecast_frames

    yield from _weather_preview_frames(
        matrix, forecast_frames(matrix, config, prepared, stop_event),
    )


def static_announcement_preview_frames(matrix, announcement):
    """Encode the shared physical static-announcement frame sequence."""
    from modules.announcement_board import static_announcement_frames

    for canvas, delay_ms in static_announcement_frames(matrix, announcement):
        yield _png_frame(matrix.snapshot(canvas), delay_ms)


def announcement_preview_frames(
    matrix, announcements, duration=None, stop_event=None, *, pinned_handles=None,
):
    """Encode the complete shared physical announcement invocation."""
    from modules.announcement_board import announcement_frames

    class LogicalClock:
        elapsed = 0.0

        def monotonic(self):
            return self.elapsed

    clock = LogicalClock()
    for canvas, delay_ms in announcement_frames(
        matrix, announcements, duration, stop_event, monotonic=clock.monotonic,
        pinned_handles=pinned_handles,
    ):
        yield _png_frame(matrix.snapshot(canvas), delay_ms)
        clock.elapsed += delay_ms / 1000


class PreviewCapacityError(RuntimeError):
    """Raised when bounded preview-session admission cannot obtain a slot."""


class PreviewInstanceCapacityError(RuntimeError):
    """Raised when a paired device or process has too many live instances."""


class PreviewSessionExpiredError(RuntimeError):
    """Raised when a continuation belongs to an expired preview session."""


class PreviewInstanceRegistry:
    """Own opaque, non-rendering page instances within process-local bounds."""

    MAX_PER_DEVICE = 4
    MAX_TOTAL = 8

    def __init__(self, *, capacity=None):
        self._capacity = capacity or PreviewSessionCapacity()
        self._lock = threading.Condition()
        self._instances = {}
        self._device_counts = {}
        self._generations = {}
        self._closed = False
        self._lifecycle_state = "ACTIVE"
        self._shutdown = threading.Event()
        self._reaper_join_count = 0
        self._reaper = threading.Thread(
            target=self._capacity.run_expiry_reaper,
            name="edit-preview-expiry-reaper",
            daemon=True,
        )
        self._reaper.start()

    @property
    def lifecycle_state(self):
        with self._lock:
            return self._lifecycle_state

    @property
    def reaper_alive(self):
        return self._reaper.is_alive()

    @property
    def reaper_is_daemon(self):
        return self._reaper.daemon

    @property
    def reaper_join_count(self):
        with self._lock:
            return self._reaper_join_count

    @property
    def total_count(self):
        with self._lock:
            return len(self._instances)

    @property
    def counts_by_device(self):
        with self._lock:
            return dict(self._device_counts)

    def count_for_device(self, device_identity):
        with self._lock:
            return self._device_counts.get(device_identity, 0)

    def register(
        self, device_identity, iterator_factory, stop_event=None, *, generation=None,
    ):
        superseded = None
        with self._lock:
            if self._closed:
                raise RuntimeError("preview registry is closed")
            device_count = self._device_counts.get(device_identity, 0)
            if device_count >= self.MAX_PER_DEVICE:
                raise PreviewInstanceCapacityError(
                    "paired device already has 4 live preview instances"
                )
            if len(self._instances) >= self.MAX_TOTAL:
                raise PreviewInstanceCapacityError(
                    "process already has 8 live preview instances"
                )
            if generation is not None:
                authoritative = self._generations.get(device_identity)
                if authoritative is not None:
                    active_generation, active_instance_id = authoritative
                    if generation <= active_generation:
                        raise ValueError("generation must be strictly newer")
                    superseded = self._instances[active_instance_id][1]
            instance_id = secrets.token_hex(16)
            while instance_id in self._instances:
                instance_id = secrets.token_hex(16)
            instance = PreviewInstance(
                instance_id, iterator_factory, self._capacity,
                stop_event=stop_event, on_close=self._unregister,
                registry_shutdown=self._shutdown, generation=generation,
            )
            self._instances[instance_id] = (device_identity, instance)
            self._device_counts[device_identity] = device_count + 1
            if generation is not None:
                self._generations[device_identity] = (generation, instance_id)
        # Never wait for or signal an old page while holding the registry lock;
        # its unregister callback re-enters this registry during deferred cleanup.
        if superseded is not None:
            superseded.close()
        return instance

    def lookup(self, instance_id, device_identity):
        """Return only an instance owned by the requesting paired page identity."""
        with self._lock:
            entry = self._instances.get(instance_id)
            if entry is None or entry[0] != device_identity:
                raise KeyError("unknown preview session")
            return entry[1]

    def _unregister(self, instance_id):
        with self._lock:
            entry = self._instances.pop(instance_id, None)
            if entry is None:
                return
            device_identity, _instance = entry
            authoritative = self._generations.get(device_identity)
            if authoritative is not None and authoritative[1] == instance_id:
                del self._generations[device_identity]
            remaining = self._device_counts[device_identity] - 1
            if remaining:
                self._device_counts[device_identity] = remaining
            else:
                del self._device_counts[device_identity]
            self._lock.notify_all()

    def close(self):
        """Reject new work and close every registered preview resource once."""
        with self._lock:
            if self._lifecycle_state == "CLOSED":
                return
            if self._lifecycle_state == "CLOSING":
                while self._lifecycle_state != "CLOSED":
                    self._lock.wait()
                return
            self._lifecycle_state = "CLOSING"
            self._closed = True
            self._shutdown.set()
            instances = [instance for _device, instance in self._instances.values()]

        # Closures call back into _unregister; never hold the registry lock.
        self._capacity.close()
        for instance in instances:
            instance.close()
        with self._lock:
            while self._instances:
                self._lock.wait()

        # The capacity condition is already awake.  Join only after active page
        # callbacks have released registry/session ownership.
        self._reaper.join(0.25)
        if self._reaper.is_alive():
            raise RuntimeError("preview expiry reaper did not stop within 250 ms")
        with self._lock:
            self._reaper_join_count += 1
            self._lifecycle_state = "CLOSED"
            self._lock.notify_all()


class PreviewInstance:
    """A registered page identity that lazily acquires one renderer session."""

    def __init__(
        self, instance_id, iterator_factory, capacity, *, stop_event=None,
        on_close=None, registry_shutdown=None, generation=None,
    ):
        self.instance_id = instance_id
        self.generation = generation
        self._iterator_factory = iterator_factory
        self._capacity = capacity
        self._stop_event = stop_event
        self._on_close = on_close
        self._registry_shutdown = registry_shutdown
        self._session = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def state(self):
        """Expose renderer ownership without allowing callers to mutate it."""
        with self._lock:
            if self._session is not None:
                return self._session.state
            return "CLOSED" if self._closed else "ACTIVE"

    def admit(self):
        with self._lock:
            if self._closed or (
                self._registry_shutdown is not None
                and self._registry_shutdown.is_set()
            ):
                raise RuntimeError("preview instance is closed")
            if self._session is None:
                self._session = self._capacity.admit(
                    self._iterator_factory,
                    stop_event=self._stop_event,
                    on_close=self._release_registration,
                )
            return self._session

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._stop_event is not None:
                self._stop_event.set()
            session = self._session
        if session is None:
            self._release_registration()
        else:
            session.cancel()

    def _release_registration(self):
        with self._lock:
            if not self._closed:
                self._closed = True
            release, self._on_close = self._on_close, None
        if release is not None:
            release(self.instance_id)


class PreviewSessionCapacity:
    """FIFO admission gate for the two process-local active renderer slots."""

    MAX_ACTIVE = 2
    ACQUIRE_TIMEOUT = 2.0
    IDLE_TTL = 60.0

    def __init__(self, *, monotonic=time.monotonic, wait=None):
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._wait = wait or (lambda condition, timeout: condition.wait(timeout))
        self._active = 0
        self._waiters = deque()
        self._sessions = {}
        self._closed = False

    @property
    def active_count(self):
        with self._condition:
            return self._active

    @property
    def available_slots(self):
        with self._condition:
            return self.MAX_ACTIVE - self._active

    @property
    def waiting_count(self):
        with self._condition:
            return len(self._waiters)

    def admit(self, iterator_factory, stop_event=None, *, on_close=None):
        ticket = object()
        deadline = self._monotonic() + self.ACQUIRE_TIMEOUT
        with self._condition:
            if self._closed:
                raise RuntimeError("preview session capacity is closed")
            self._waiters.append(ticket)
            while (
                not self._closed
                and (self._waiters[0] is not ticket or self._active >= self.MAX_ACTIVE)
            ):
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                    raise PreviewCapacityError(
                        "preview session capacity acquisition timed out after 2 seconds"
                    )
                self._wait(self._condition, remaining)
            if self._closed:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._condition.notify_all()
                raise RuntimeError("preview session capacity is closed")
            self._waiters.popleft()
            self._active += 1

        try:
            def release(session):
                try:
                    self._release(session)
                finally:
                    if on_close is not None:
                        on_close()

            session = PreviewSession(
                iterator_factory(),
                stop_event=stop_event,
                on_close=release,
                on_activity=self._record_activity,
            )
        except BaseException:
            try:
                self._release_reservation()
            finally:
                if on_close is not None:
                    on_close()
            raise

        with self._condition:
            self._sessions[session] = self._monotonic()
            closed = self._closed
            self._condition.notify_all()
        if closed:
            session.cancel()
            raise RuntimeError("preview session capacity is closed")
        return session

    def close(self):
        """Stop admissions and cancel all constructed renderer sessions."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions)
            self._condition.notify_all()
        for session in sessions:
            session.cancel()

    def run_expiry_reaper(self):
        """Expire idle sessions from one interruptible daemon per registry."""
        while True:
            now = self._monotonic()
            with self._condition:
                if self._closed:
                    return
                expired = [
                    session for session, last_activity in self._sessions.items()
                    if session.state == "ACTIVE"
                    and now - last_activity > self.IDLE_TTL
                ]
                if expired:
                    timeout = 0
                else:
                    deadlines = [
                        last_activity + self.IDLE_TTL
                        for session, last_activity in self._sessions.items()
                        if session.state == "ACTIVE"
                    ]
                    timeout = None if not deadlines else max(
                        0.000001, min(deadlines) - now,
                    )
                    self._condition.wait(timeout)
                    continue
            for session in expired:
                session.cancel()

    def reap_expired(self):
        """Synchronously expire sessions idle for more than the 60-second TTL."""
        now = self._monotonic()
        with self._condition:
            expired = [
                session for session, last_activity in self._sessions.items()
                if session.state == "ACTIVE"
                and now - last_activity > self.IDLE_TTL
            ]
        for session in expired:
            session.cancel()
        return len(expired)

    def _record_activity(self, session):
        with self._condition:
            if session in self._sessions and session.state == "ACTIVE":
                self._sessions[session] = self._monotonic()
                self._condition.notify_all()

    def _release(self, session):
        with self._condition:
            if self._sessions.pop(session, None) is None:
                return
            self._active -= 1
            self._condition.notify_all()

    def _release_reservation(self):
        with self._condition:
            if self._active <= 0:
                return
            self._active -= 1
            self._condition.notify_all()


class PreviewSession:
    """Pull one finite iterator in bounded pages without retaining frame history."""

    MAX_FRAMES = 32
    MAX_BYTES = 512 * 1024

    def __init__(
        self, iterator, stop_event=None, *, on_close=None, on_activity=None,
    ):
        self._iterator = iter(iterator)
        self._stop_event = stop_event
        self._page = -1
        self._token = None
        self._lookahead = None
        self._current = []
        self._complete = False
        self._iterator_closed = False
        self._iterator_close_started = False
        self._on_close = on_close
        self._on_activity = on_activity
        self._state_lock = threading.Lock()
        self._page_in_progress = False
        self._state = "ACTIVE"

    @property
    def state(self):
        with self._state_lock:
            return self._state

    @property
    def retained_frame_count(self):
        return len(self._current) + (self._lookahead is not None)

    def restart(self, iterator_factory):
        """Create a new page-zero session only after this cycle is terminal."""
        if not self._complete:
            raise RuntimeError("a finite cycle can restart only after its terminal page")
        return type(self)(iterator_factory())

    def cancel(self):
        """Signal terminal state promptly and defer active-page cleanup."""
        with self._state_lock:
            self._complete = True
            if self._state != "CLOSED":
                self._state = "CLOSING"
            if self._stop_event is not None:
                self._stop_event.set()
            # Clear again on repeated cancellation because an active page may have
            # obtained lookahead after the first cancellation released this lock.
            self._token = None
            self._lookahead = None
            self._current = []
            close_now = not self._page_in_progress
        if close_now:
            self._close_iterator()

    def _close_iterator(self):
        """Release the finite renderer exactly once on cancel or exhaustion."""
        with self._state_lock:
            if self._iterator_close_started:
                return
            self._iterator_close_started = True
        try:
            close = getattr(self._iterator, "close", None)
            if close is not None:
                close()
        finally:
            with self._state_lock:
                self._iterator_closed = True
                self._state = "CLOSED"
            if self._on_close is not None:
                release, self._on_close = self._on_close, None
                release(self)

    def _next_frame(self):
        if self._lookahead is not None:
            frame, self._lookahead = self._lookahead, None
            return frame
        return next(self._iterator)

    def _cancelled_page(self, page_number):
        self.cancel()
        return {
            "page": page_number,
            "frames": [],
            "next_token": None,
            "complete": True,
        }

    def page(self, token=None, *, publish=None):
        """Generate one page and optionally acknowledge its response publication.

        A claimed continuation token is never restored.  If the injected response
        boundary raises, the generated page and any replacement token are
        ambiguous, so the whole session fails closed instead of allowing replay.
        """
        with self._state_lock:
            if self._complete:
                raise StopIteration("an explicit new session is required to restart")
            if self._stop_event is not None and self._stop_event.is_set():
                cancelled_page = self._page + 1
            else:
                cancelled_page = None
            if cancelled_page is None and (
                self._page_in_progress
                or (self._page < 0 and token is not None)
                or (self._page >= 0 and token != self._token)
            ):
                raise ValueError("invalid or already consumed continuation token")
            if cancelled_page is None:
                # Claim and consume the token before iterator work.  The lifecycle
                # lock is released immediately so cancellation can still signal a
                # decoder blocked by the winning page operation.
                self._page_in_progress = True
                self._token = None
                self._page += 1
        if cancelled_page is not None:
            return self._cancelled_page(cancelled_page)

        try:
            page = self._page_after_claim()
            if publish is not None:
                try:
                    publish(page)
                except BaseException:
                    self.cancel()
                    raise
            return page
        finally:
            with self._state_lock:
                self._page_in_progress = False
                close_after_page = self._complete and not self._iterator_close_started
            if close_after_page:
                self._close_iterator()

    def _page_after_claim(self):
        self._current = []
        exhausted = False
        while len(self._current) < self.MAX_FRAMES:
            try:
                frame = self._next_frame()
            except StopIteration:
                exhausted = True
                break
            if self._stop_event is not None and self._stop_event.is_set():
                return self._cancelled_page(self._page)
            candidate = self._current + [frame]
            probe = {
                "page": self._page, "frames": candidate, "next_token": "0" * 64,
                "complete": False,
            }
            if len(json.dumps(probe, separators=(",", ":")).encode()) > self.MAX_BYTES:
                if not self._current:
                    # A valid frame is indivisible: preserve the finite cycle by
                    # publishing a lone oversize frame rather than truncating it.
                    self._current.append(frame)
                else:
                    self._lookahead = frame
                break
            else:
                self._current.append(frame)

        if not exhausted and self._lookahead is None:
            try:
                self._lookahead = next(self._iterator)
            except StopIteration:
                exhausted = True
            if self._stop_event is not None and self._stop_event.is_set():
                return self._cancelled_page(self._page)
        page = None
        with self._state_lock:
            cancelled = self._complete or (
                self._stop_event is not None and self._stop_event.is_set()
            )
            if not cancelled:
                self._complete = exhausted
                self._token = None if exhausted else secrets.token_hex(32)
                page = {
                    "page": self._page,
                    "frames": list(self._current),
                    "next_token": self._token,
                    "complete": exhausted,
                }
                self._current = []
        if cancelled:
            return self._cancelled_page(self._page)
        if exhausted:
            self._close_iterator()
        if self._on_activity is not None:
            self._on_activity(self)
        return page
