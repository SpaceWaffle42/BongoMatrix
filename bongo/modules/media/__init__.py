"""Media: show an image or animated GIF (fit, fill or pixel-art scaling). Video is not supported."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageSequence

from bongo.module import Module, Style

ROOT = Path(__file__).parents[3] / "resources" / "media"
MAX_FRAMES = 300


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def locate(file):
    """Resolve a setting to an existing file, or None (missing, unsafe, or not a file)."""
    p = Path(str(file)).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    root = ROOT.resolve()
    p = (root / p).resolve()
    return p if p.is_relative_to(root) and p.is_file() else None


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
            if path is None or path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
                raise OSError("no media")
            with Image.open(path) as im:
                total = 0
                for i, frame in enumerate(ImageSequence.Iterator(im)):
                    if i >= MAX_FRAMES:
                        break
                    total += frame.info.get("duration") or 100
                    self.frames.append(self.compose(frame.convert("RGBA"), s["brightness"]))
                    self.ends.append(total)
        except Exception:
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
    description="Shows an image or animated GIF. Fit letterboxes, fill crops, pixel scales by whole numbers.",
    styles={"fit": Fit, "fill": Fill, "pixel": Pixel},
    defaults=dict(file="built-in/rubik_squares.gif", background="#000000", speed=1.0, brightness=1.0),
)
