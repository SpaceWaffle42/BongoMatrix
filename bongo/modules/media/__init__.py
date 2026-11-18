"""Media: show an image, animated GIF or short video (fit, fill or pixel-art scaling).
Video is decoded by ffmpeg (if installed) at 20 fps, up to 15 s, then loops like a GIF."""
import json
import logging
import shutil
import subprocess
import threading

from PIL import Image, ImageDraw, ImageSequence

from bongo import media as library
from bongo.module import Module, Style

MAX_FRAMES = library.MAX_FRAMES
VIDEO_FPS = 20
KEPT = 6            # the last few files' finished frames: up to 300 x canvas x 3 bytes each (1.8 MB at 64 x 32, 11 MB for six)
lock = threading.Lock()   # guards `ready` and `loading` only: never held while a file decodes
ready: dict = {}    # (file, changed, size, style, canvas, background, brightness) -> (frames, ends, moving, plays)
loading: dict = {}  # the same keys -> a lock held while that one picture is decoded


def video_size(path: str) -> tuple[int, int]:
    """The size a video is shown at: ffmpeg turns a phone's portrait video upright as it decodes, and
    stretches pixels that aren't square (SAR), so the stored width and height alone would squash it."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise OSError("ffmpeg is not installed")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
                            "-of", "json", path], capture_output=True, timeout=20, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    w, h = float(stream["width"]), float(stream["height"])
    try:
        num, den = (int(v) for v in str(stream.get("sample_aspect_ratio", "1:1")).split(":"))
        if num > 0 and den > 0:
            w = w * num / den
    except ValueError:
        pass
    turns = [stream.get("tags", {}).get("rotate")] + [d.get("rotation") for d in stream.get("side_data_list", [])]
    for turn in turns:   # older ffmpeg tags it "rotate", newer gives a display matrix's "rotation"
        try:
            if turn is not None and round(float(turn)) % 180 == 90:
                w, h = h, w
                break
        except ValueError:
            pass
    return max(1, round(w)), max(1, round(h))


def video_frames(path: str, size: tuple[int, int]):
    """Up to MAX_FRAMES frames of ``path`` at ``size``, as RGB images, one at a time: decoded at the size
    they're shown, so only one small frame is ever held here (a Pi Zero has 512 MB)."""
    w, h = size
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an",
                             "-vf", f"fps={VIDEO_FPS},scale={w}:{h}:flags=area,setsar=1", "-frames:v", str(MAX_FRAMES),
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stop = threading.Timer(120, proc.kill)   # a file ffmpeg gets stuck on
    stop.start()
    try:
        while len(raw := proc.stdout.read(w * h * 3)) == w * h * 3:
            yield Image.frombytes("RGB", (w, h), raw)
    finally:
        stop.cancel()
        proc.kill()
        proc.wait()
        proc.stdout.close()


def plays(im) -> int:
    """How many times the file asks to be played (0: for ever). A GIF without a loop count plays once, as
    a browser plays it (the Animator's "Plays: Once"); a GIF's count is repeats after the first play."""
    loop = im.info.get("loop")
    if im.format == "GIF":
        return 1 if loop is None else (0 if loop == 0 else loop + 1)
    return loop if isinstance(loop, int) and loop > 0 else 0


def keep(key, kept):
    """Keep a file's finished frames as the newest, dropping the oldest past KEPT."""
    with lock:
        ready.pop(key, None)
        ready[key] = kept
        while len(ready) > KEPT:
            del ready[next(iter(ready))]


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
        self.moving = False   # a GIF or video (a GIF can be one long frame); a still never "plays through"
        try:
            path = locate(s["file"])
            if path is None:
                raise OSError("no media")
            # a playlist comes back to the same file every pass: decode and scale it once, not each time
            # (a changed file has a new time or size, so an upload under the same name is read afresh)
            stat = path.stat()
            key = (str(path), stat.st_mtime, stat.st_size, type(self), self.ctx.width, self.ctx.height, self.bg, s["brightness"])
            with lock:   # the panel and the dashboard's previews both come here
                kept = ready.get(key)
                busy = loading.setdefault(key, threading.Lock()) if kept is None else None
            if busy is not None:
                # decoded under this picture's own lock, so a big file never holds up other screens
                with busy:
                    with lock:
                        kept = ready.get(key)   # another screen may have just decoded it
                    try:
                        if kept is None:
                            self.load(path)
                            kept = (tuple(self.frames), tuple(self.ends), self.moving, self.plays)
                        keep(key, kept)   # kept before its lock goes, so no screen arriving now decodes it again
                    finally:
                        with lock:
                            loading.pop(key, None)
            else:
                keep(key, kept)
            self.frames, self.ends, self.moving, self.plays = kept
        except Exception as error:
            logging.getLogger(__name__).warning("can't show %r: %s", s["file"], error)
            self.frames, self.ends, self.moving, self.plays = [self.placeholder()], [1], False, 0

    plays = 0         # how many passes the file asks for (0: for ever)
    looping = False   # set by the player: loop whatever the file asks (Sleep re-shows it in whole passes)

    def load(self, path):
        """Decode ``path`` into this style's finished frames."""
        s = self.settings
        if library.is_video(path):
            shown = video_size(str(path))
            size = self.target(*shown)
            if size[0] > shown[0] or size[1] > shown[1]:
                size = shown   # a small video: decoded as it is, enlarged below like a small GIF
            for i, frame in enumerate(video_frames(str(path), size)):
                self.frames.append(self.compose(frame.convert("RGBA"), s["brightness"]))
                self.ends.append((i + 1) * 1000 // VIDEO_FPS)
            if not self.frames:
                raise OSError("no video frames")
            self.moving = True
            return
        with Image.open(path) as im:
            total, turn, self.plays = 0, library.orientation(im), plays(im)
            most = library.frames_played(im.width, im.height, library.frame_count(im))
            try:
                for i, frame in enumerate(ImageSequence.Iterator(im)):
                    if i >= most:
                        break
                    rgba = library.rgba(frame, turn)   # decodes the frame: a WebP frame's time is read then
                    total += max(10, int(frame.info.get("duration") or 100))   # as schedule.gif_pass counts it
                    self.frames.append(self.compose(rgba, s["brightness"]))
                    self.ends.append(total)
            except library.CUT_OFF as error:
                if not self.frames:
                    raise
                # a cut-off download: play the frames that came through, as a browser does
                logging.getLogger(__name__).info("%s: %d frames, the rest won't decode (%s)", path.name, len(self.frames), error)
        self.moving = len(self.frames) > 1   # a one-frame GIF is a still

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

    seen = None      # the time at the last frame drawn
    began = None     # when this screen came on: an animation starts at its first frame
    played = 0.0     # seconds played, for a splash that plays once
    JUMP = 1.0       # a step bigger than this (or backwards) is the clock being set (NTP steps over 0.4 s), not time passing

    def follow(self, before) -> None:
        """Back to back with the same file and settings: a looping animation carries on where it was rather
        than jumping back to its first frame. One that plays once (a splash) or a set number of times
        starts from the beginning, as ever."""
        if (type(before) is not type(self) or before.settings != self.settings or before.began is None
                or self.once or before.once or not self.moving
                or any(s.plays and not s.looping for s in (self, before))):   # (both screens looping)
            return
        self.began = before.began

    def render(self, now):
        stamp = now.timestamp()
        if self.began is None:
            self.began = stamp
        if self.once:   # counted frame by frame, so the clock being set at boot can't cut a splash short
            if self.seen is not None:
                step = stamp - self.seen
                self.played += step if 0 <= step <= self.JUMP else 1 / self.fps
            self.seen = stamp
            elapsed = self.played
        else:           # a pure function of the time, so shared dashboard previews stay steady
            elapsed = stamp - self.began
        t = int(elapsed * 1000 * self.settings["speed"])
        if self.once and self.moving and t >= self.ends[-1]:
            self.finished = True               # played through once: hand over, on its last frame
            return self.frames[-1].copy()
        if self.plays and not self.looping and t >= self.plays * self.ends[-1]:
            return self.frames[-1].copy()      # played as many times as the file asks: it stays on its last frame
        t %= self.ends[-1]
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
    defaults=dict(file="built-in/splash_loading_squares.gif", background="#000000", speed=1.0, brightness=1.0),
    choices={"file": "media"},  # picked from the media library (built-in/ or uploaded/)
    basic=("file",),
    limits={"speed": (0, 8), "brightness": (0, 1)},
)
