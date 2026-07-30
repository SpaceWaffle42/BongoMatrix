import time
import datetime
import pathlib
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics
from PIL import Image, ImageSequence

try:  # made optional as this can be heavy
    import cv2
except ImportError:
    cv2 = None

root_path = pathlib.Path(__file__).resolve().parent
fonts_path = root_path / "resources" / "fonts"
media_path = root_path / "resources" / "media"


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
            key = path.stem.replace("-", "_")
            loaded_fonts[key] = font

        return loaded_fonts

    def colour(self, name):
        return self.colours[name]

    def font(self, name):
        try:
            return self.loaded_fonts[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.loaded_fonts))
            raise KeyError(f"Unknown font '{name}'. Available fonts: {available}") from exc

    def clear(self, canvas=None):
        canvas = canvas or self.canvas
        canvas.Clear()

    def fill(self, r, g, b, canvas=None):
        canvas = canvas or self.canvas
        canvas.Fill(r, g, b)

    def swap(self, canvas=None):
        if canvas is None:
            self.canvas = self.matrix.SwapOnVSync(self.canvas)
            return self.canvas
        return self.matrix.SwapOnVSync(canvas)

    def show(self, delay=None):
        """Display the current canvas. delay=None means return immediately."""
        self.swap()

        if delay is None:
            return

        if delay == 0:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            time.sleep(delay)

    def pixel(self, x, y, r, g, b, canvas=None):
        canvas = canvas or self.canvas
        if 0 <= x < self.matrix.width and 0 <= y < self.matrix.height:
            canvas.SetPixel(x, y, r, g, b)

    def draw_line(self, x0, y0, x1, y1, colour, width=1, canvas=None):
        canvas = canvas or self.canvas

        if width <= 1:
            graphics.DrawLine(canvas, x0, y0, x1, y1, colour)
            return

        offset = width // 2

        if abs(x1 - x0) >= abs(y1 - y0):
            for dy in range(-offset, offset + 1):
                graphics.DrawLine(canvas, x0, y0 + dy, x1, y1 + dy, colour)
        else:
            for dx in range(-offset, offset + 1):
                graphics.DrawLine(canvas, x0 + dx, y0, x1 + dx, y1, colour)

    def draw_circle(self, x, y, radius, colour, width=1, filled=False, canvas=None):
        canvas = canvas or self.canvas

        if filled:
            for r in range(radius, -1, -1):
                graphics.DrawCircle(canvas, x, y, r, colour)
            return

        if width <= 1:
            graphics.DrawCircle(canvas, x, y, radius, colour)
            return

        for r in range(radius, max(radius - width, -1), -1):
            graphics.DrawCircle(canvas, x, y, r, colour)

    def draw_text(self, font, x, y, colour, text, canvas=None):
        canvas = canvas or self.canvas
        return graphics.DrawText(canvas, font, x, y, colour, text)

    def draw_text_with_border(
        self, font, x, y, fill_color, border_color, text, width=1, canvas=None
    ):
        canvas = canvas or self.canvas

        if width < 1:
            width = 1

        for dirx in range(-width, width + 1):
            for diry in range(-width, width + 1):
                if dirx != 0 or diry != 0:
                    graphics.DrawText(
                        canvas, font, x + dirx, y + diry, border_color, text
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
        text_len = graphics.DrawText(canvas, font, x, y, colour, text)
        x -= step
        if wrap and x + text_len < 0:
            x = canvas.width
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

    def show_image(
        self, filename, x=0, y=0, delay=3, width=None, height=None, keep_aspect=True
    ):
        image = self.load_image(
            filename, width=width, height=height, keep_aspect=keep_aspect
        )
        if image is None:
            return False

        self.canvas.SetImage(image, x, y)
        self.show(delay)
        return True

    def load_image(self, filename, width=None, height=None, keep_aspect=True):
        path = media_path / filename
        if not path.exists():
            return None

        image = Image.open(path)
        return self._prepare_image(
            image, width=width, height=height, keep_aspect=keep_aspect
        )

    def show_gif(
        self, filename, x=0, y=0, loops=1, width=None, height=None, keep_aspect=True
    ):
        """Display an animated GIF."""
        path = media_path / filename
        if not path.exists():
            return False

        gif = Image.open(path)
        completed = 0

        while loops == 0 or completed < loops:
            for frame in ImageSequence.Iterator(gif):
                self.clear()

                image = frame.copy()
                image = self._prepare_image(
                    image, width=width, height=height, keep_aspect=keep_aspect
                )

                self.canvas.SetImage(image, x, y)
                self.swap()

                frame_delay = frame.info.get("duration", 100) / 1000.0
                time.sleep(frame_delay)

            completed += 1

        return True

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
    ):
        """Display a video file."""
        if cv2 is None:
            print("OpenCV is not installed or could not be imported.")
            return False

        path = media_path / filename
        if not path.exists():
            print(f"Video file not found: {path}")
            return False

        completed = 0

        while loops == 0 or completed < loops:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                print(f"Could not open video: {path}")
                print("File could be missing, corrupted, or has invalid MP4 metadata.")
                return False

            fps = fps_override or cap.get(cv2.CAP_PROP_FPS)
            if not fps or fps <= 0:
                fps = 15

            frame_delay = 1.0 / fps

            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(frame)
                image = self._prepare_image(
                    image, width=width, height=height, keep_aspect=keep_aspect
                )

                self.clear()
                self.canvas.SetImage(image, x, y)
                self.swap()
                time.sleep(frame_delay)

            cap.release()
            completed += 1

        return True

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
