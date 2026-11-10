"""Media: show an image, animated GIF or short video (fit, fill or pixel-art scaling).
Video is decoded once by ffmpeg (if installed) at 20 fps, up to 15 s, then loops like a GIF."""
import functools
import json
import logging
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageSequence

from bongo import media as library
from bongo.module import Module, Style

MAX_FRAMES = 300
VIDEO_FPS = 20


@functools.lru_cache(maxsize=4)
def video_frames(path: str, mtime: float, box: tuple[int, int]) -> tuple:
    """Decode up to MAX_FRAMES frames of ``path``, scaled to fit ``box``, as RGB images.
    Cached by path + mtime, so a looping playlist doesn't decode the file every time."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise OSError("ffmpeg is not installed")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "json", path],
                           capture_output=True, timeout=20, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    k = min(box[0] / stream["width"], box[1] / stream["height"], 1.0)
    w, h = max(2, int(stream["width"] * k) // 2 * 2), max(2, int(stream["height"] * k) // 2 * 2)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an",
                          "-vf", f"fps={VIDEO_FPS},scale={w}:{h}:flags=area", "-frames:v", str(MAX_FRAMES),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, timeout=120, check=True).stdout
    size = w * h * 3
    frames = tuple(Image.frombytes("RGB", (w, h), raw[i:i + size]) for i in range(0, len(raw) - size + 1, size))
    if not frames:
        raise OSError("no video frames")
    return frames


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def locate(file):
    """The file a setting names (``built-in/x.gif`` or ``uploaded/y.mp4``), or None. Paths outside
    the two media folders are never opened (see bongo/media.py)."""
    return library.resolve(file)


class Fit(Style):
    """Whole image visible, letterboxed on the background colour."""
    fps = 20
    defaults = {}

    def target(self, iw, ih):
        """Size to scale an iw x ih image to."""
        w, h = self.ctx.width, self.ctx.height
        k = min(w / iw, h / ih)
        return max(1, round(iw * k)), max(1, round(ih * k))

    resample = Image.LANCZOS

    def resampler(self, old, new):
        """Smooth when shrinking; crisp when enlarging, so small pixel-art GIFs stay sharp."""
        return Image.NEAREST if new[0] >= old[0] and new[1] >= old[1] else self.resample

    def setup(self):
        s = self.settings
        self.bg = rgb(s["background"])
        self.frames, self.ends = [], []
        try:
            path = locate(s["file"])
            if path is None:
                raise OSError("no media")
            if library.is_video(path):
                # decode at up to 4x the canvas so fill/fit still have detail to scale from
                box = (self.ctx.width * 4, self.ctx.height * 4)
                for i, frame in enumerate(video_frames(str(path), path.stat().st_mtime, box)):
                    self.frames.append(self.compose(frame.convert("RGBA"), s["brightness"]))
                    self.ends.append((i + 1) * 1000 // VIDEO_FPS)
                return
            with Image.open(path) as im:
                total = 0
                for i, frame in enumerate(ImageSequence.Iterator(im)):
                    if i >= MAX_FRAMES:
                        break
                    total += frame.info.get("duration") or 100
                    self.frames.append(self.compose(frame.convert("RGBA"), s["brightness"]))
                    self.ends.append(total)
        except Exception as error:
            logging.getLogger(__name__).warning("can't show %r: %s", s["file"], error)
            self.frames, self.ends = [self.placeholder()], [1]

    def compose(self, rgba, brightness):
        w, h = self.ctx.width, self.ctx.height
        size = self.target(*rgba.size)
        if size != rgba.size:
            rgba = rgba.resize(size, self.resampler(rgba.size, size))
        canvas = Image.new("RGBA", (w, h), self.bg + (255,))
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        layer.paste(rgba, ((w - size[0]) // 2, (h - size[1]) // 2))
        out = Image.alpha_composite(canvas, layer).convert("RGB")
        if brightness < 1:
            out = out.point(lambda v: int(v * max(0.0, brightness)))
        return out

    def placeholder(self):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGB", (w, h), self.bg)
        d = ImageDraw.Draw(img)
        dim = (60, 60, 70)
        d.rectangle([1, 1, w - 2, h - 2], outline=dim)
        d.line([(1, 1), (w - 2, h - 2)], fill=dim)
        d.line([(1, h - 2), (w - 2, 1)], fill=dim)
        font, text = self.ctx.fonts("tom-thumb"), "NO MEDIA"
        tw, th = font.width(text), font.ascent
        x, y = (w - tw) // 2, (h + th) // 2
        d.rectangle([x - 1, y - th - 1, x + tw - 1, y + 1], fill=self.bg)
        font.draw(img, x, y, text, (200, 200, 210))
        return img

    def render(self, now):
        t = int(now.timestamp() * 1000 * self.settings["speed"]) % self.ends[-1]
        return self.frames[next(i for i, e in enumerate(self.ends) if t < e)].copy()


class Fill(Fit):
    """Covers the canvas; the overflow is cropped evenly from both sides."""

    def target(self, iw, ih):
        k = max(self.ctx.width / iw, self.ctx.height / ih)
        return max(self.ctx.width, round(iw * k)), max(self.ctx.height, round(ih * k))

    def compose(self, rgba, brightness):
        w, h = self.ctx.width, self.ctx.height
        size = self.target(*rgba.size)
        rgba = rgba.resize(size, self.resampler(rgba.size, size))
        x, y = (rgba.width - w) // 2, (rgba.height - h) // 2
        rgba = rgba.crop((x, y, x + w, y + h))
        out = Image.alpha_composite(Image.new("RGBA", (w, h), self.bg + (255,)), rgba).convert("RGB")
        return out.point(lambda v: int(v * max(0.0, brightness))) if brightness < 1 else out


class Pixel(Fit):
    """Pixel art: whole-number nearest-neighbour scale only, no smoothing."""
    resample = Image.NEAREST

    def target(self, iw, ih):
        w, h = self.ctx.width, self.ctx.height
        k = min(w // iw, h // ih)
        if k >= 1:
            return iw * k, ih * k
        s = min(w / iw, h / ih)  # larger than the canvas: nearest downscale to fit
        return max(1, int(iw * s)), max(1, int(ih * s))


MODULE = Module(
    id="media", name="Media",
    description="Shows an image, animated GIF or short video. Fit letterboxes, fill crops, pixel scales by whole numbers.",
    styles={"fit": Fit, "fill": Fill, "pixel": Pixel},
    defaults=dict(file="built-in/rubik_squares.gif", background="#000000", speed=1.0, brightness=1.0),
    choices={"file": "media"},  # picked from the media library (built-in/ or uploaded/)
)
