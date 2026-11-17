"""Where frames go: the real LED panel, or a recorder for previews and tests."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw


LOCK_PATH = Path(os.environ.get("BONGO_LOCK", "/run/lock/bongo-matrix.lock"))


class PanelBusy(RuntimeError):
    pass


def claim_panel(path: Path = LOCK_PATH):
    """Make this process the panel's only driver. Two drivers on the GPIO pins garble the
    display, so a second ``bongo run`` or ``bongo calibrate`` stops with a clear message.
    The lock is an flock, released by the kernel when the process ends (even on a crash)."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip() or "another process"
        handle.close()
        raise PanelBusy(f"the panel is already being driven by pid {owner}; stop it first "
                        "(sudo systemctl stop bongo-matrix)") from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle  # keep a reference: closing it releases the lock


class HardwarePanel:
    """64x32 HUB75 panel through hzeller/rpi-rgb-led-matrix."""

    def __init__(self, rows=32, cols=64, hardware_mapping="adafruit-hat-pwm",
                 gpio_slowdown=4, brightness=100, chain_length=1, parallel=1, limit_refresh=0):
        self._lock = claim_panel()
        from rgbmatrix import RGBMatrix, RGBMatrixOptions  # only exists on the Pi

        opts = RGBMatrixOptions()
        opts.rows, opts.cols = rows, cols
        opts.chain_length, opts.parallel = chain_length, parallel
        opts.hardware_mapping = hardware_mapping
        opts.gpio_slowdown = gpio_slowdown
        if limit_refresh:   # hold the refresh to a steady rate (0: no limit, the library's own default)
            opts.limit_refresh_rate_hz = int(limit_refresh)
        opts.brightness = brightness
        opts.drop_privileges = False
        self.matrix = RGBMatrix(options=opts)
        self.canvas = self.matrix.CreateFrameCanvas()
        self.width, self.height = self.matrix.width, self.matrix.height

    def show(self, frame: Image.Image) -> None:
        self.canvas.SetImage(frame.convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def clear(self) -> None:
        self.matrix.Clear()

    def set_brightness(self, level: int) -> None:
        self.matrix.brightness = max(1, min(100, int(level)))


class RecordingPanel:
    """Keeps frames in memory; can save an LED-look GIF for review."""

    def __init__(self, width=64, height=32):
        self.width, self.height = width, height
        self.frames: list[Image.Image] = []
        self.brightness: list[int] = []

    def show(self, frame: Image.Image) -> None:
        self.frames.append(frame.copy())

    def clear(self) -> None:
        pass

    def set_brightness(self, level: int) -> None:
        if not self.brightness or self.brightness[-1] != level:
            self.brightness.append(level)

    def save_gif(self, path, fps=30, scale=8) -> None:
        big = [led_look(f, scale) for f in self.frames]
        big[0].save(path, save_all=True, append_images=big[1:], duration=int(1000 / fps), loop=0)


def led_look(frame: Image.Image, scale: int = 8) -> Image.Image:
    """Upscale with round LED dots on a black board, like the physical panel."""
    w, h = frame.size
    out = Image.new("RGB", (w * scale, h * scale), (8, 8, 9))
    draw = ImageDraw.Draw(out)
    px = frame.load()
    pad = max(1, scale // 8)
    for y in range(h):
        for x in range(w):
            draw.ellipse([x * scale + pad, y * scale + pad, (x + 1) * scale - pad - 1,
                          (y + 1) * scale - pad - 1], fill=px[x, y])
    return out
