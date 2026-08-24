import math
import os
import pwd
import queue
import select
import stat
import subprocess
import sys
import threading
import time
import datetime
import pathlib
from types import SimpleNamespace
try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics
except ModuleNotFoundError as error:
    if error.name != "rgbmatrix":
        raise

    class _UnavailableRGBMatrix:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("RGB Matrix hardware bindings are unavailable")

    RGBMatrix = RGBMatrixOptions = _UnavailableRGBMatrix
    graphics = SimpleNamespace(
        Color=lambda red, green, blue: SimpleNamespace(
            red=red, green=green, blue=blue,
        ),
        Font=_UnavailableRGBMatrix,
    )
from PIL import Image, ImageSequence

from font_catalog import FONT_DIR, font_key
from frame_capture import BdfFont, FrameCapture

root_path = pathlib.Path(__file__).resolve().parent
fonts_path = FONT_DIR
media_path = root_path / "resources" / "media"


def _read_exact_interruptibly(stream, size, stop_event=None, deadline=None):
    """Read one decoder frame while observing cancellation and deadlines."""
    fd = stream.fileno()
    chunks = []
    remaining = size
    idle_deadline = time.monotonic() + 5
    while remaining:
        if stop_event is not None and stop_event.is_set():
            break
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break
        if now >= idle_deadline:
            raise ValueError("MP4 decoder did not produce a frame in time")
        wait_for = min(0.05, idle_deadline - now)
        if deadline is not None:
            wait_for = min(wait_for, max(0, deadline - now))
        readable, _, _ = select.select([fd], [], [], wait_for)
        if not readable:
            continue
        try:
            chunk = os.read(fd, remaining)
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        idle_deadline = time.monotonic() + 5
    return b"".join(chunks)


def _read_ppm_token_interruptibly(stream, stop_event=None, deadline=None):
    """Read one bounded ASCII token from an ffmpeg PPM stream."""
    token = bytearray()
    while True:
        byte = _read_exact_interruptibly(
            stream, 1, stop_event=stop_event, deadline=deadline,
        )
        if not byte:
            return bytes(token) if token else None
        if byte in b" \t\r\n":
            if token:
                return bytes(token)
            continue
        token.extend(byte)
        if len(token) > 32:
            raise ValueError("media decoder produced an invalid frame header")


def _read_ppm_frame_interruptibly(stream, stop_event=None, deadline=None):
    """Read one source-sized RGB frame from ffmpeg's PPM image pipe."""
    magic = _read_ppm_token_interruptibly(stream, stop_event, deadline)
    if magic is None:
        return None
    width_token = _read_ppm_token_interruptibly(stream, stop_event, deadline)
    height_token = _read_ppm_token_interruptibly(stream, stop_event, deadline)
    maximum_token = _read_ppm_token_interruptibly(stream, stop_event, deadline)
    try:
        width = int(width_token or b"")
        height = int(height_token or b"")
        maximum = int(maximum_token or b"")
    except ValueError as error:
        raise ValueError("media decoder produced an invalid frame header") from error
    if magic != b"P6" or maximum != 255 or not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise ValueError("media decoder produced an invalid frame header")
    if width * height > 1_000_000:
        raise ValueError("media decoder frame is too large")
    raw = _read_exact_interruptibly(
        stream, width * height * 3,
        stop_event=stop_event, deadline=deadline,
    )
    if len(raw) != width * height * 3:
        raise ValueError("media could not be decoded for the custom module timeline")
    return Image.frombytes("RGB", (width, height), raw)


def _reap_decoder(process):
    """Collect a killed decoder outside the request path if bounded reaping expires."""
    try:
        process.wait()
    except OSError:
        pass


_DECODER_REAPER_STOP = object()
_decoder_reaper_lock = threading.Lock()
_decoder_reaper_owner = None
# Compatibility/debug mirrors: they always identify the currently reachable owner.
_decoder_reaper_queue = None
_decoder_reaper_thread = None
_decoder_reaper_leases = 0
_decoder_reaper_had_deferred = False


class _DecoderReaperLease:
    """A handoff slot allocated before its decoder child can be spawned."""

    def __init__(self, owner):
        self.owner = owner
        self.process = None
        self.released = False
        self.claimed = False


class _DecoderReaperOwner:
    def __init__(self):
        self.condition = threading.Condition()
        self.leases = []
        self.active_leases = 0
        self.shutdown_requested = False
        self.had_deferred = False
        self.thread = None


def _decoder_reaper_main(owner):
    while True:
        with owner.condition:
            claimed = None
            while claimed is None:
                for lease in owner.leases:
                    if lease.released and not lease.claimed:
                        lease.claimed = True
                        claimed = lease
                        break
                if claimed is not None:
                    break
                if owner.shutdown_requested:
                    return
                owner.condition.wait()
        if claimed.process is not None:
            _reap_decoder(claimed.process)


def _set_decoder_reaper_owner(owner):
    """Update all global owner mirrors while the global lock is held."""
    global _decoder_reaper_owner, _decoder_reaper_queue, _decoder_reaper_thread
    global _decoder_reaper_leases, _decoder_reaper_had_deferred
    _decoder_reaper_owner = owner
    _decoder_reaper_queue = owner
    _decoder_reaper_thread = None if owner is None else owner.thread
    _decoder_reaper_leases = 0 if owner is None else owner.active_leases
    _decoder_reaper_had_deferred = False if owner is None else owner.had_deferred


def _ensure_decoder_reaper():
    """Reserve a handoff slot and verified daemon before decoder Popen."""
    with _decoder_reaper_lock:
        owner = _decoder_reaper_owner
        if owner is not None and not owner.thread.is_alive():
            _set_decoder_reaper_owner(None)
            owner = None
        if owner is not None:
            if owner.shutdown_requested:
                raise RuntimeError("decoder reaper is shutting down")
            lease = _DecoderReaperLease(owner)
            with owner.condition:
                owner.leases.append(lease)
                owner.active_leases += 1
            _set_decoder_reaper_owner(owner)
            return lease

        owner = _DecoderReaperOwner()
        lease = _DecoderReaperLease(owner)
        owner.leases.append(lease)
        owner.active_leases = 1
        reaper = threading.Thread(
            target=_decoder_reaper_main,
            args=(owner,),
            name="matrix-decoder-reaper",
            daemon=True,
        )
        owner.thread = reaper
        reaper.start()
        if not reaper.is_alive():
            raise RuntimeError("decoder reaper failed to start")
        _set_decoder_reaper_owner(owner)
        return lease


def _release_decoder_reaper(lease, deferred_process=None, join_timeout=0.25):
    """Publish into a reserved slot; never detach a worker that may own a child."""
    with _decoder_reaper_lock:
        owner = _decoder_reaper_owner
        if (
            not isinstance(lease, _DecoderReaperLease)
            or lease.owner is not owner
            or lease.released
            or owner.active_leases <= 0
        ):
            raise RuntimeError("decoder reaper lease is not active")
        with owner.condition:
            # Both objects already exist before Popen. This publication performs no
            # queue insertion and cannot strand a spawned child on put() failure.
            lease.process = deferred_process
            lease.released = True
            if deferred_process is not None:
                owner.had_deferred = True
            owner.active_leases -= 1
            final_release = owner.active_leases == 0
            if final_release:
                owner.shutdown_requested = True
            owner.condition.notify_all()
        _set_decoder_reaper_owner(owner)
        reaper = owner.thread
        had_deferred = owner.had_deferred

    if final_release and not had_deferred:
        reaper.join(join_timeout)
        with _decoder_reaper_lock:
            if _decoder_reaper_owner is owner and not reaper.is_alive():
                _set_decoder_reaper_owner(None)


def _reset_decoder_reaper_for_tests(timeout=1):
    """Stop the shared owner without making a live worker unreachable."""
    with _decoder_reaper_lock:
        owner = _decoder_reaper_owner
        if owner is None:
            return
        with owner.condition:
            for lease in owner.leases:
                if not lease.released:
                    lease.released = True
            owner.active_leases = 0
            owner.shutdown_requested = True
            owner.condition.notify_all()
        _set_decoder_reaper_owner(owner)
        reaper = owner.thread
    reaper.join(timeout)
    if reaper.is_alive():
        raise RuntimeError("decoder reaper did not stop")
    with _decoder_reaper_lock:
        if _decoder_reaper_owner is owner:
            _set_decoder_reaper_owner(None)


def _swap_admitted(matrix):
    """Treat swaps without a production admission capability as accepted."""
    admission = getattr(type(matrix), "last_swap_admitted", None)
    return True if admission is None else bool(admission(matrix))


class Matrix:
    def __init__(
        self,
        rows: int = 32,
        cols: int = 64,
        gpio_slowdown: int = 4,
        chain_length: int = 1,
        parallel: int = 1,
        brightness: int = 100,
        hardware_mapping: str = "adafruit-hat-pwm",
        led_rgb_sequence: str = "RGB",
        drop_privileges: bool = False,
        limit_refresh_rate_hz: int = 120,
        pwm_bits: int = 8,
    ):
        options = RGBMatrixOptions()
        options.rows = rows
        options.cols = cols
        options.gpio_slowdown = gpio_slowdown
        options.chain_length = chain_length
        options.parallel = parallel
        options.brightness = brightness
        options.hardware_mapping = hardware_mapping
        options.led_rgb_sequence = led_rgb_sequence
        options.drop_privileges = drop_privileges
        options.limit_refresh_rate_hz = limit_refresh_rate_hz
        options.pwm_bits = pwm_bits

        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()
        self._frame_capture = FrameCapture(self.matrix.width, self.matrix.height)
        self._frame_capture.register(self.canvas)
        self._capture_fonts = {}
        self._publication_guard = None
        self._physical_swap_observer = None
        self.colours = self._load_colours()
        self.loaded_fonts = self._load_fonts()

    def _load_colours(self):
        return {
            "red": graphics.Color(255, 0, 0),
            "blue": graphics.Color(0, 0, 255),
            "green": graphics.Color(0, 255, 0),
            "yellow": graphics.Color(255, 255, 0),
            "orange": graphics.Color(255, 165, 0),
            "cyan": graphics.Color(0, 255, 255),
            "magenta": graphics.Color(255, 0, 255),
            "purple": graphics.Color(120, 81, 169),
            "white": graphics.Color(255, 255, 255),
            "light_grey": graphics.Color(200, 200, 200),
            "black": graphics.Color(0, 0, 0),
            "dark_red": graphics.Color(139, 0, 0),
            "dark_blue": graphics.Color(0, 0, 139),
            "dark_green": graphics.Color(0, 100, 0),
            "gold": graphics.Color(255, 215, 0),
            "sky_blue": graphics.Color(135, 206, 235),
            "pink": graphics.Color(255, 192, 203),
            "brown": graphics.Color(165, 42, 42),
            "navy": graphics.Color(0, 0, 128),
            "teal": graphics.Color(0, 128, 128),
            "vapor_pink": graphics.Color(255, 113, 206),
            "vapor_cyan": graphics.Color(1, 205, 254),
            "vapor_mint": graphics.Color(5, 255, 161),
            "vapor_lilac": graphics.Color(185, 103, 255),
            "vapor_cream": graphics.Color(255, 251, 150),
            "neon_purple": graphics.Color(138, 0, 196),
            "neon_blue": graphics.Color(0, 191, 255),
            "neon_yellow": graphics.Color(255, 179, 0),
            "neon_coral": graphics.Color(255, 92, 92),
            "neon_violet": graphics.Color(126, 61, 153),
            "retro_purple": graphics.Color(178, 82, 161),
            "retro_aqua": graphics.Color(102, 161, 210),
            "retro_deep_purple": graphics.Color(44, 4, 82),
            "retro_slate": graphics.Color(69, 95, 101),
            "retro_soft_grey": graphics.Color(220, 213, 213),
        }

    def _load_fonts(self):
        loaded_fonts = {}

        for path in sorted(fonts_path.glob("*.bdf")):
            font = graphics.Font()
            font.LoadFont(str(path))
            key = font_key(path)
            loaded_fonts[key] = font
            self._capture_fonts[id(font)] = BdfFont(path)

        return loaded_fonts

    def colour(self, name):
        return self.colours[name]

    @staticmethod
    def colour_rgb(value):
        if (
            not isinstance(value, str)
            or len(value) != 7
            or value[0] != "#"
            or any(character not in "0123456789abcdefABCDEF" for character in value[1:])
        ):
            raise ValueError("colour must use #RRGGBB format")
        return graphics.Color(
            int(value[1:3], 16),
            int(value[3:5], 16),
            int(value[5:7], 16),
        )

    def font(self, name):
        try:
            return self.loaded_fonts[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.loaded_fonts))
            raise KeyError(f"Unknown font '{name}'. Available fonts: {available}") from exc

    def clear(self, canvas=None):
        canvas = canvas or self.canvas
        self._frame_capture.clear(canvas)
        canvas.Clear()

    def set_brightness(self, brightness):
        """Set hardware brightness, clamped to the driver's safe range."""
        self.matrix.brightness = max(1, min(100, int(brightness)))

    def fill(self, r, g, b, canvas=None):
        canvas = canvas or self.canvas
        self._frame_capture.fill(canvas, r, g, b)
        canvas.Fill(r, g, b)

    def set_publication_guard(self, guard):
        """Install a final authority check for the owned render invocation."""
        if guard is not None and not callable(guard):
            raise TypeError("publication guard must be callable")
        self._publication_guard = guard

    def set_physical_swap_observer(self, observer):
        """Observe only frames accepted by the production SwapOnVSync seam."""
        if observer is not None and not callable(observer):
            raise TypeError("physical swap observer must be callable")
        self._physical_swap_observer = observer

    def swap(self, canvas=None):
        guard = self._publication_guard
        submitted = self.canvas if canvas is None else canvas
        if guard is not None and not guard():
            self._last_swap_admitted = False
            return submitted
        raw = self._frame_capture.raw_snapshot(submitted)
        returned = self.matrix.SwapOnVSync(submitted)
        revision = self._frame_capture.publish(submitted)
        self._frame_capture.register(returned)
        if canvas is None:
            self.canvas = returned
        observer = getattr(self, "_physical_swap_observer", None)
        if observer is not None:
            observer(raw, revision)
        self._last_swap_admitted = True
        return returned

    def latest_frame_rgb(self):
        """Return immutable RGB bytes from the latest successful physical swap."""
        return self._frame_capture.latest_rgb()

    def last_swap_admitted(self):
        """Report whether the most recent swap crossed the hardware guard."""
        return getattr(self, "_last_swap_admitted", True)

    def latest_frame_png(self):
        """Return the last frame submitted to the physical panel and its revision."""
        return self._frame_capture.png(), self._frame_capture.revision

    def show(self, delay=None):
        """Display the current canvas. delay=None means return immediately."""
        self.swap()
        if not _swap_admitted(self):
            return False

        if delay is None:
            return True

        if delay == 0:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            time.sleep(delay)
        return True

    def pixel(self, x, y, r, g, b, canvas=None):
        canvas = canvas or self.canvas
        if 0 <= x < self.matrix.width and 0 <= y < self.matrix.height:
            self._frame_capture.pixel(canvas, x, y, r, g, b)
            canvas.SetPixel(x, y, r, g, b)

    def draw_line(self, x0, y0, x1, y1, colour, width=1, canvas=None):
        canvas = canvas or self.canvas

        if width <= 1:
            self._frame_capture.line(canvas, x0, y0, x1, y1, colour)
            graphics.DrawLine(canvas, x0, y0, x1, y1, colour)
            return

        offset = width // 2

        if abs(x1 - x0) >= abs(y1 - y0):
            for dy in range(-offset, offset + 1):
                self._frame_capture.line(canvas, x0, y0 + dy, x1, y1 + dy, colour)
                graphics.DrawLine(canvas, x0, y0 + dy, x1, y1 + dy, colour)
        else:
            for dx in range(-offset, offset + 1):
                self._frame_capture.line(canvas, x0 + dx, y0, x1 + dx, y1, colour)
                graphics.DrawLine(canvas, x0 + dx, y0, x1 + dx, y1, colour)

    def draw_circle(self, x, y, radius, colour, width=1, filled=False, canvas=None):
        canvas = canvas or self.canvas

        if filled:
            for r in range(radius, -1, -1):
                self._frame_capture.circle(canvas, x, y, r, colour)
                graphics.DrawCircle(canvas, x, y, r, colour)
            return

        if width <= 1:
            self._frame_capture.circle(canvas, x, y, radius, colour)
            graphics.DrawCircle(canvas, x, y, radius, colour)
            return

        for r in range(radius, max(radius - width, -1), -1):
            self._frame_capture.circle(canvas, x, y, r, colour)
            graphics.DrawCircle(canvas, x, y, r, colour)

    def draw_text(self, font, x, y, colour, text, canvas=None, rotation=0):
        canvas = canvas or self.canvas
        capture_font = self._capture_fonts[id(font)]
        if not rotation % 360:
            self._frame_capture.text(canvas, capture_font, x, y, colour, text)
            return graphics.DrawText(canvas, font, x, y, colour, text)

        advance, points = capture_font.text_pixels(x, y, text)
        if not points:
            return advance
        centre_x = (min(point[0] for point in points) + max(point[0] for point in points)) / 2
        centre_y = (min(point[1] for point in points) + max(point[1] for point in points)) / 2
        angle = math.radians(rotation)
        cosine, sine = math.cos(angle), math.sin(angle)
        seen = set()
        for px, py in points:
            dx, dy = px - centre_x, py - centre_y
            rotated = (
                round(centre_x + dx * cosine - dy * sine),
                round(centre_y + dx * sine + dy * cosine),
            )
            if rotated in seen:
                continue
            seen.add(rotated)
            self.pixel(rotated[0], rotated[1], colour.red, colour.green, colour.blue, canvas=canvas)
        return advance

    def draw_text_with_border(
        self, font, x, y, fill_color, border_color, text, width=1, canvas=None
    ):
        canvas = canvas or self.canvas

        if width < 1:
            width = 1

        for dirx in range(-width, width + 1):
            for diry in range(-width, width + 1):
                if dirx != 0 or diry != 0:
                    self._frame_capture.text(
                        canvas,
                        self._capture_fonts[id(font)],
                        x + dirx,
                        y + diry,
                        border_color,
                        text,
                    )
                    graphics.DrawText(
                        canvas, font, x + dirx, y + diry, border_color, text
                    )

        self._frame_capture.text(
            canvas, self._capture_fonts[id(font)], x, y, fill_color, text
        )
        return graphics.DrawText(canvas, font, x, y, fill_color, text)

    def flash(self, flashes=3, on_time=0.15, off_time=0.15, colour=(255, 255, 255)):
        """Flash the whole display on and off using the RGB."""
        for _ in range(flashes):
            self.fill(*colour)
            self.show(on_time)
            self.clear()
            self.show(off_time)

    def scroll_text(self, text, font, y, colour, speed=0.03, direction="left", loops=1):
        if direction == "left":
            pos = self.matrix.width
            completed = 0
            while loops == 0 or completed < loops:
                self.clear()
                text_len = graphics.DrawText(self.canvas, font, pos, y, colour, text)
                self.swap()
                time.sleep(speed)
                pos -= 1
                if pos + text_len < 0:
                    pos = self.matrix.width
                    completed += 1

        elif direction == "right":
            text_len = graphics.DrawText(self.canvas, font, 0, y, colour, text)
            pos = -text_len
            completed = 0
            while loops == 0 or completed < loops:
                self.clear()
                graphics.DrawText(self.canvas, font, pos, y, colour, text)
                self.swap()
                time.sleep(speed)
                pos += 1
                if pos > self.matrix.width:
                    pos = -text_len
                    completed += 1

    def widget_scroll(self, text, font, x, y, colour, canvas=None, step=1, wrap=True):
        canvas = canvas or self.canvas
        text_len = self.draw_text(font, x, y, colour, text, canvas=canvas)
        x -= step
        if wrap:
            if step >= 0 and x + text_len < 0:
                x = canvas.width
            elif step < 0 and x > canvas.width:
                x = -text_len
        return x, text_len

    def _prepare_image(self, image, width=None, height=None, keep_aspect=True):
        """
        Prepare an image for the matrix.

        width / height:
            - if both given and keep_aspect=False, force exact size
            - if both given and keep_aspect=True, fit inside that box
            - if only one given, scale proportionally
            - if neither given, fit to full matrix
        """
        image = image.convert("RGB")

        if width is None and height is None:
            image = image.copy()
            image.thumbnail(
                (self.matrix.width, self.matrix.height), Image.Resampling.LANCZOS
            )
            return image

        original_width, original_height = image.size

        if width is not None and height is not None:
            if keep_aspect:
                image = image.copy()
                image.thumbnail((width, height), Image.Resampling.LANCZOS)
                return image
            return image.resize((width, height), Image.Resampling.LANCZOS)

        if width is not None:
            ratio = width / original_width
            height = int(original_height * ratio)
            return image.resize((width, height), Image.Resampling.LANCZOS)

        if height is not None:
            ratio = height / original_height
            width = int(original_width * ratio)
            return image.resize((width, height), Image.Resampling.LANCZOS)

        return image

    def _media_file(self, filename):
        """Return a regular file confined to resources/media, or None."""
        if not isinstance(filename, str) or not filename:
            return None
        root = media_path.resolve()
        candidate = (root / filename).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate

    def draw_image(
        self, image, x=0, y=0, width=None, height=None, keep_aspect=True,
        rotation=0, canvas=None,
    ):
        """Resize/rotate an image and mirror the exact canvas submission."""
        canvas = canvas or self.canvas
        prepared = self._prepare_image(
            image, width=width, height=height, keep_aspect=keep_aspect
        )
        if rotation:
            prepared = prepared.rotate(-rotation, expand=False)
        canvas.SetImage(prepared, int(x), int(y))
        self._frame_capture.set_image(canvas, prepared, int(x), int(y))
        return prepared

    def media_frames(
        self, filename, frame_count, fps, stop_event=None, decode_timeout=45,
        preserve_size=False, resolved_path=None, *, pinned_handle=None,
    ):
        """Decode a bounded media timeline while observing cancellation/deadlines."""
        if (
            isinstance(frame_count, bool) or not isinstance(frame_count, int)
            or not 1 <= frame_count <= 120
            or isinstance(fps, bool) or not isinstance(fps, (int, float))
            or not 1 <= fps <= 60
            or isinstance(decode_timeout, bool)
            or not isinstance(decode_timeout, (int, float))
            or not 0 < decode_timeout <= 120
            or not isinstance(preserve_size, bool)
            or resolved_path is not None and not isinstance(resolved_path, (str, os.PathLike))
        ):
            raise ValueError("media timeline bounds are invalid")
        if stop_event is not None and stop_event.is_set():
            return []
        deadline = time.monotonic() + decode_timeout
        path = None
        if pinned_handle is None:
            path = pathlib.Path(resolved_path) if resolved_path is not None else self._media_file(filename)
            if path is None:
                return []
            input_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        else:
            input_fd = pinned_handle.dup_fd()
        if resolved_path is not None or pinned_handle is not None:
            try:
                is_regular = stat.S_ISREG(os.fstat(input_fd).st_mode)
            except OSError:
                os.close(input_fd)
                raise
            if not is_regular:
                os.close(input_fd)
                return []
        identity = {}
        if os.geteuid() == 0:
            nobody = pwd.getpwnam("nobody")
            identity = {"user": nobody.pw_uid, "group": nobody.pw_gid, "extra_groups": []}
        process = None
        reaper_queue = None
        deferred_process = None
        try:
            reaper_queue = _ensure_decoder_reaper()
            command = [
                    "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                    "-i", "pipe:0",
                    "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vf", (
                        f"scale=w='min({self.matrix.width},iw)':h='min({self.matrix.height},ih)':force_original_aspect_ratio=decrease,fps={fps}:eof_action=pass" if preserve_size else
                        f"scale=64:32:force_original_aspect_ratio=decrease,pad=64:32:(ow-iw)/2:(oh-ih)/2,fps={fps}:eof_action=pass"
                    ),
                    "-frames:v", str(frame_count),
                ]
            if preserve_size:
                command.extend(["-f", "image2pipe", "-c:v", "ppm", "pipe:1"])
            else:
                command.extend(["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
            process = subprocess.Popen(
                command,
                stdin=input_fd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                **identity,
            )
            frame_size = self.matrix.width * self.matrix.height * 3
            frames = []
            for _ in range(frame_count):
                if preserve_size:
                    image = _read_ppm_frame_interruptibly(
                        process.stdout, stop_event=stop_event, deadline=deadline,
                    )
                    if stop_event is not None and stop_event.is_set():
                        return []
                    if image is None:
                        break
                    frames.append(image)
                    continue
                raw = _read_exact_interruptibly(
                    process.stdout,
                    frame_size,
                    stop_event=stop_event,
                    deadline=deadline,
                )
                if stop_event is not None and stop_event.is_set():
                    return []
                if not raw:
                    break
                if len(raw) != frame_size:
                    raise ValueError("media could not be decoded for the custom module timeline")
                frames.append(Image.frombytes("RGB", (self.matrix.width, self.matrix.height), raw))
            while True:
                if stop_event is not None and stop_event.is_set():
                    return []
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("media decoder did not finish in time")
                try:
                    return_code = process.wait(
                        timeout=min(0.05, remaining) if stop_event is not None else remaining
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
            if return_code != 0 or not frames:
                raise ValueError("media could not be decoded for the custom module timeline")
            return [frames[index % len(frames)] for index in range(frame_count)]
        finally:
            primary_error = sys.exception()
            cleanup_error = None

            def remember_cleanup_error(error):
                nonlocal cleanup_error
                if cleanup_error is None:
                    cleanup_error = error

            if process is not None and process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception as error:
                    remember_cleanup_error(error)

            if process is not None:
                if reaper_queue is None:
                    remember_cleanup_error(
                        RuntimeError("decoder process has no reaping owner")
                    )
                running = False
                try:
                    running = process.poll() is None
                except ProcessLookupError:
                    pass
                except OSError as error:
                    remember_cleanup_error(error)
                    running = True
                if running:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        remember_cleanup_error(error)
                    cleanup_remaining = deadline - time.monotonic()
                    reap_in_background = cleanup_remaining <= 0
                    if not reap_in_background:
                        try:
                            process.wait(timeout=min(0.25, cleanup_remaining))
                        except ProcessLookupError:
                            pass
                        except subprocess.TimeoutExpired:
                            reap_in_background = True
                        except OSError as error:
                            remember_cleanup_error(error)
                            reap_in_background = True
                    if reap_in_background:
                        deferred_process = process

            try:
                os.close(input_fd)
            except OSError as error:
                remember_cleanup_error(error)

            if reaper_queue is not None:
                try:
                    _release_decoder_reaper(
                        reaper_queue, deferred_process=deferred_process,
                    )
                except Exception as error:
                    remember_cleanup_error(error)

            if primary_error is None and cleanup_error is not None:
                raise cleanup_error

    def show_image(
        self, filename, x=0, y=0, delay=3, width=None, height=None, keep_aspect=True,
        *, pinned_handle=None, stop_event=None,
    ):
        if stop_event is not None and stop_event.is_set():
            return False
        image = self.load_image(
            filename, width=width, height=height, keep_aspect=keep_aspect,
            pinned_handle=pinned_handle,
        )
        if image is None:
            return False
        if stop_event is not None and stop_event.is_set():
            return False

        self.canvas.SetImage(image, x, y)
        if stop_event is not None and stop_event.is_set():
            return False
        self._frame_capture.set_image(self.canvas, image, x, y)
        if stop_event is not None and stop_event.is_set():
            return False
        return self.show(delay)

    def load_image(
        self, filename, width=None, height=None, keep_aspect=True, *, pinned_handle=None,
    ):
        if pinned_handle is not None:
            with pinned_handle.dup_file() as media_file:
                with Image.open(media_file) as image:
                    return self._prepare_image(
                        image, width=width, height=height, keep_aspect=keep_aspect
                    )
        path = self._media_file(filename)
        if path is None:
            return None

        image = Image.open(path)
        return self._prepare_image(
            image, width=width, height=height, keep_aspect=keep_aspect
        )

    def show_gif(
        self,
        filename,
        x=0,
        y=0,
        loops=1,
        width=None,
        height=None,
        keep_aspect=True,
        playback_rate=1.0,
        stop_event=None,
        max_duration=None,
        *,
        pinned_handle=None,
    ):
        """Display an animated GIF, optionally bounded or interruptible."""
        from modules.media_player import gif_media_frames

        # Validate before media lookup to preserve the public fail-fast contract.
        if (
            isinstance(playback_rate, bool)
            or not isinstance(playback_rate, (int, float))
            or not math.isfinite(playback_rate)
            or not 0.25 <= playback_rate <= 4.0
        ):
            raise ValueError("GIF playback rate must be between 0.25 and 4.0")
        if stop_event is not None and stop_event.is_set():
            return False
        image_open = Image.open
        if pinned_handle is None:
            path = self._media_file(filename)
            if path is None:
                return False
        else:
            path = pinned_handle

            def image_open(handle):
                media_file = handle.dup_file()
                try:
                    return Image.open(media_file)
                except BaseException:
                    media_file.close()
                    raise

        def close_owned(value, label):
            active_error = sys.exception()
            try:
                close = getattr(value, "close", None)
                if callable(close):
                    close()
            except BaseException:
                if active_error is None:
                    raise
                try:
                    active_error.add_note(
                        f"suppressed {label} cleanup failure"
                    )
                except BaseException:
                    pass

        shown = False
        timeline = gif_media_frames(
            path, loops=loops, playback_rate=playback_rate,
            stop_event=stop_event, max_duration=max_duration,
            monotonic=time.monotonic, image_open=image_open,
            image_iterator=ImageSequence.Iterator,
        )
        try:
            for image, frame_delay in timeline:
                try:
                    self.clear()
                    self.draw_image(
                        image, x=x, y=y, width=width, height=height,
                        keep_aspect=keep_aspect,
                    )
                    if stop_event is not None and stop_event.is_set():
                        return shown
                    self.swap()
                    if not _swap_admitted(self):
                        return shown
                    shown = True
                    if stop_event is not None:
                        if stop_event.wait(frame_delay):
                            return True
                    else:
                        time.sleep(frame_delay)
                finally:
                    close_owned(image, "GIF candidate")
            return shown
        finally:
            close_owned(timeline, "GIF timeline")

    def _video_frame_timeline(
        self, path, frame_width, frame_height, keep_aspect, stop_event=None,
        deadline=None, *, pinned_handle=None,
    ):
        """Decode one MP4 timeline for shared physical/preview repetition."""
        frame_size = frame_width * frame_height * 3
        identity = {}
        if os.geteuid() == 0:
            nobody = pwd.getpwnam("nobody")
            identity = {
                "user": nobody.pw_uid,
                "group": nobody.pw_gid,
                "extra_groups": [],
            }
        video_filter = f"scale={frame_width}:{frame_height}"
        if keep_aspect:
            video_filter += (
                ":force_original_aspect_ratio=decrease,"
                f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2:black"
            )
        input_fd = (
            pinned_handle.dup_fd() if pinned_handle is not None
            else os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        )
        process = None
        reaper_queue = None
        deferred_process = None
        try:
            reaper_queue = _ensure_decoder_reaper()
            process = subprocess.Popen(
                [
                    "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                    "-i", f"pipe:{input_fd}", "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vf", video_filter,
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                pass_fds=(input_fd,),
                **identity,
            )
            parent_input_fd = input_fd
            # close(2) errors leave numeric-fd ownership ambiguous: Linux can
            # release the descriptor before reporting an error and POSIX does
            # not make EINTR retry-safe.  Consume our ownership before the
            # call, surface any failure, and never retry a potentially reused
            # descriptor number during outer cleanup.
            input_fd = None
            os.close(parent_input_fd)
            while True:
                frame = _read_exact_interruptibly(
                    process.stdout, frame_size, stop_event=stop_event,
                    deadline=deadline,
                )
                if stop_event is not None and stop_event.is_set():
                    return
                if len(frame) != frame_size:
                    break
                yield Image.frombytes("RGB", (frame_width, frame_height), frame)
        finally:
            primary_error = sys.exception()
            cleanup_error = None

            def remember_cleanup_error(error):
                nonlocal cleanup_error
                if cleanup_error is None:
                    cleanup_error = error

            try:
                if process is not None and process.stdout is not None:
                    try:
                        process.stdout.close()
                    except Exception as error:
                        remember_cleanup_error(error)

                if process is not None:
                    if reaper_queue is None:
                        remember_cleanup_error(
                            RuntimeError("decoder process has no reaping owner")
                        )
                    running = False
                    try:
                        running = process.poll() is None
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        remember_cleanup_error(error)
                    if running:
                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass
                        except OSError as error:
                            remember_cleanup_error(error)
                    wait_timeout = 2.0
                    if stop_event is not None:
                        # An interruptible invocation can be cancelled immediately
                        # after any sampled event check. Never enter a synchronous
                        # child wait on its renderer/display-owner path.
                        wait_timeout = 0.0
                    elif deadline is not None:
                        wait_timeout = min(
                            wait_timeout, max(0.0, deadline - time.monotonic())
                        )
                    if wait_timeout <= 0:
                        deferred_process = process
                    else:
                        try:
                            process.wait(timeout=wait_timeout)
                        except ProcessLookupError:
                            pass
                        except subprocess.TimeoutExpired:
                            try:
                                process.kill()
                            except ProcessLookupError:
                                pass
                            except OSError as error:
                                remember_cleanup_error(error)
                            final_wait_timeout = 0.25
                            if deadline is not None:
                                final_wait_timeout = min(
                                    final_wait_timeout,
                                    max(0.0, deadline - time.monotonic()),
                                )
                            if final_wait_timeout <= 0:
                                deferred_process = process
                            else:
                                try:
                                    process.wait(timeout=final_wait_timeout)
                                except ProcessLookupError:
                                    pass
                                except subprocess.TimeoutExpired:
                                    deferred_process = process
                                except OSError as error:
                                    remember_cleanup_error(error)
                                    deferred_process = process
                        except OSError as error:
                            remember_cleanup_error(error)
                            deferred_process = process
            finally:
                if input_fd is not None:
                    try:
                        os.close(input_fd)
                    except OSError as error:
                        remember_cleanup_error(error)
                if reaper_queue is not None:
                    try:
                        _release_decoder_reaper(
                            reaper_queue, deferred_process=deferred_process,
                        )
                    except Exception as error:
                        remember_cleanup_error(error)

            if primary_error is None and cleanup_error is not None:
                raise cleanup_error

    def show_video(
        self,
        filename,
        x=0,
        y=0,
        loops=1,
        fps_override=None,
        width=None,
        height=None,
        keep_aspect=True,
        stop_event=None,
        max_duration=None,
        *,
        pinned_handle=None,
    ):
        """Decode a sanitized MP4 with FFmpeg, bounded and interruptible."""
        from modules.media_player import video_media_frames

        if stop_event is not None and stop_event.is_set():
            return False
        path = None if pinned_handle is not None else self._media_file(filename)
        if (
            path is None and pinned_handle is None
            or pathlib.Path(filename).suffix.lower() != ".mp4"
        ):
            return False

        frame_width = int(width or self.matrix.width)
        frame_height = int(height or self.matrix.height)
        if frame_width < 1 or frame_height < 1:
            return False
        fps = max(1.0, min(60.0, float(fps_override or 20)))
        started = time.monotonic()
        deadline = started + max_duration if max_duration is not None else None

        def decode():
            return self._video_frame_timeline(
                path, frame_width, frame_height, keep_aspect,
                stop_event=stop_event, deadline=deadline,
                pinned_handle=pinned_handle,
            )

        shown = False
        for image, frame_delay in video_media_frames(
            decode, fps=fps, loops=loops, stop_event=stop_event,
            max_duration=max_duration, monotonic=time.monotonic,
        ):
            self.clear()
            self.canvas.SetImage(image, x, y)
            self._frame_capture.set_image(self.canvas, image, x, y)
            if stop_event is not None and stop_event.is_set():
                return shown
            self.swap()
            if not _swap_admitted(self):
                return shown
            shown = True
            if stop_event is not None:
                if stop_event.wait(frame_delay):
                    return shown
            else:
                time.sleep(frame_delay)
        return shown

    def border_test(self):
        """Draw a simple border + diagonal test pattern for panel debugging."""
        self.clear()
        for x in range(self.matrix.width):
            self.pixel(x, 0, 255, 0, 0)
            self.pixel(x, self.matrix.height - 1, 255, 255, 0)

        for y in range(self.matrix.height):
            self.pixel(0, y, 0, 0, 255)
            self.pixel(self.matrix.width - 1, y, 0, 255, 0)

        limit = min(self.matrix.width, self.matrix.height)
        for i in range(limit):
            self.pixel(i, i, 255, 255, 255)
            self.pixel(self.matrix.width - 1 - i, i, 255, 0, 255)

        self.show(2)


if __name__ == "__main__":
    m = Matrix()

    canvas = m.matrix.CreateFrameCanvas()
    icon = m.load_image("image.png", width=10, height=10, keep_aspect=False)
    scroll_text = "Brighton | Partly cloudy | Feels like 17C | Humidity 68%"
    scroll_x = canvas.width

    white = m.colour("retro_soft_grey")
    black = m.colour("black")
    pink = m.colour("vapor_pink")
    cyan = m.colour("vapor_cyan")
    yellow = m.colour("neon_yellow")
    blue = m.colour("retro_aqua")

    font_small = m.font("4x6")
    font_main = m.font("5x7")

    try:
        while True:
            canvas.Clear()
            m.fill(0,50,100,canvas=canvas)

            if icon is not None:
                canvas.SetImage(icon, 0, 0)

            m.draw_text_with_border(
                font_small, 2, 22, white, black, "Now", canvas=canvas
            )
            m.draw_text_with_border(
                font_main, 24, 11, white, black, "18C", canvas=canvas
            )
            m.draw_text_with_border(
                font_small, 46, 7, pink, black, "H 21", canvas=canvas
            )
            m.draw_text_with_border(
                font_small, 46, 15, cyan, black, "L 14", canvas=canvas
            )
            m.draw_line(22, 17, 63, 17, colour=blue, width=1, canvas=canvas)
            m.draw_text_with_border(
                font_small, 24, 24, yellow, black, "Wind 12", canvas=canvas
            )

            scroll_x, _ = m.widget_scroll(
                scroll_text,
                font_small,
                scroll_x,
                31,
                white,
                canvas=canvas,
                step=0.7,
                wrap=True,
            )

            canvas = m.swap(canvas)
            time.sleep(0.03)

    except KeyboardInterrupt:
        canvas.Clear()
        m.swap(canvas)
