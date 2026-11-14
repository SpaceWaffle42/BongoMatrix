"""The dashboard's Animator: frames drawn in the browser become a GIF in the media library, and the
drawing itself is kept as a project, a small text file that opens again with its layers.

A frame is the canvas's pixels as a list of ``"#rrggbb"`` (or None for the background), row by
row, plus how long it shows. A GIF is drawn at the canvas's size, one pixel per LED.

A project (``js/animator-project.js`` describes the file) holds the layers of every frame. Yours are
kept in ``data/animator``; the ones Bongo Matrix ships are in ``resources/animator``. ``flatten`` turns
a project into pictures with the same sums the Animator's preview does, so modules can draw a sprite
straight from the file it was drawn in (``bongo/sprites.py``): no exported images to keep in step.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence, UnidentifiedImageError

from bongo import media

MAX_FRAMES = 300
MAX_SIDE = 128                    # a canvas is 1 to 128 pixels each way
MAX_PIXELS = 300 * 64 * 32        # width x height x frames: 300 frames of the panel, fewer of a bigger canvas
MAX_LAYERS = 8
MAX_COLOURS = 65536               # in one project
MAX_PROJECT = 4 * 1024 * 1024     # one project file
MAX_PROJECTS = 500                # of your own, in data/animator
TOKEN = re.compile(r"(?:([1-9][0-9]{0,5})\*)?(\.|[0-9a-z]{1,4})")   # a run in a cel: "12*a", "a" or "."
FORMAT = "bongo-animation"
HEX = re.compile(r"#[0-9a-fA-F]{6}")
BUILT_IN = media.REPO / "resources" / "animator"
PREVIEW = "uploaded/.animator/preview.gif"   # "Show on Matrix" without saving: hidden from the library


def colour(value, background):
    if value is None:
        return background
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError(f"colours look like #ff8800, not {str(value)[:20]!r}")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def encode_gif(frames: list[tuple[Image.Image, int]], loop: bool = True) -> bytes:
    """One GIF from RGB pictures of one size and how long each shows: repeated frames merged into one
    longer one, and one shared palette for the whole animation, so a colour never shifts from frame to
    frame. Up to 256 colours are kept exactly; with more, 224 are picked by median cut plus the 32 colours
    those serve worst (error x how often), and every pixel takes the nearest. No dithering."""
    if not frames:
        raise ValueError("an animation needs at least one frame")
    size = frames[0][0].size
    merged: list[list] = []
    for img, ms in frames:
        if img.size != size:
            raise ValueError("every frame must be the same size")
        data = np.asarray(img.convert("RGB"), dtype=np.uint32)
        packed = (data[..., 0] << 16) | (data[..., 1] << 8) | data[..., 2]
        if merged and np.array_equal(merged[-1][0], packed):
            merged[-1][1] = min(655_350, merged[-1][1] + ms)   # the longest a GIF frame can show (11 minutes)
        else:
            merged.append([packed, ms])
    colours, where, counts = np.unique(np.stack([m[0] for m in merged]), return_inverse=True, return_counts=True)
    where = where.reshape(len(merged), size[1], size[0])
    rgb = np.stack([(colours >> 16) & 255, (colours >> 8) & 255, colours & 255], axis=1).astype(np.int32)
    if len(colours) <= 256:   # few enough to keep every one exactly
        palette, index = rgb, np.arange(len(colours))
    else:
        # the median cut sees each colour as often as it's used (scaled down, so what it reads stays small)
        times = np.maximum(1, counts * 65536 // int(counts.sum()))
        sample = Image.fromarray(np.repeat(rgb, times, axis=0).astype(np.uint8).reshape(1, -1, 3), "RGB")
        cut = sample.quantize(224, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        base = np.asarray(cut.getpalette(), dtype=np.int32)[:224 * 3].reshape(-1, 3)
        error = ((rgb - base[_nearest(rgb, base)]) ** 2).sum(1) * counts
        worst = np.argsort(-error, kind="stable")[:32]
        palette = np.concatenate([base, rgb[worst[error[worst] > 0]]])
        index = _nearest(rgb, palette)
    flat = [int(v) for v in palette.reshape(-1)]
    pictures, times = [], []
    for k in range(len(merged)):
        data = index[where[k]].astype(np.uint8).tobytes()
        if pictures and pictures[-1].tobytes() == data:   # two frames the palette made the same: one longer frame (Pillow would add them uncapped)
            times[-1] = min(655_350, times[-1] + merged[k][1])
            continue
        img = Image.frombytes("P", size, data)
        img.putpalette(flat + [0] * (768 - len(flat)))
        pictures.append(img)
        times.append(merged[k][1])
    out = io.BytesIO()
    pictures[0].save(out, "GIF", save_all=True, append_images=pictures[1:], duration=times,
                     optimize=False, disposal=1, **({"loop": 0} if loop else {}))
    return out.getvalue()


def _nearest(colours: np.ndarray, palette: np.ndarray) -> np.ndarray:
    """For each colour, which palette entry is closest (an exact match always wins). In slices, to stay small."""
    out = np.empty(len(colours), dtype=np.int64)
    for start in range(0, len(colours), 4096):
        part = colours[start:start + 4096]
        out[start:start + 4096] = ((part[:, None, :] - palette[None, :, :]) ** 2).sum(2).argmin(1)
    return out


def budget(width: int, height: int, frames: int) -> None:
    """Keep an animation small enough for a Pi Zero to draw, save and send: a bigger canvas gets fewer frames."""
    if frames > MAX_FRAMES:
        raise ValueError(f"up to {MAX_FRAMES} frames")
    if width * height * frames > MAX_PIXELS:
        raise ValueError(f"at {width}x{height} an animation can have up to {MAX_PIXELS // (width * height)} frames")


def _ms(value, k: int) -> int:
    """How long frame k shows, as a whole number of ms (rounded as the Animator rounds it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > 1e9:
        raise ValueError(f"frame {k + 1} needs a time in ms")
    return math.floor(value + 0.5)


def side(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_SIDE:
        raise ValueError(f"the {name} is 1 to {MAX_SIDE} pixels")
    return value


def gif(frames: list, width: int, height: int, background: str = "#000000", loop: str = "loop") -> bytes:
    """The frames as an animated GIF (``loop``: loop, once or pingpong)."""
    if not isinstance(frames, list) or not frames:
        raise ValueError("an animation needs at least one frame")
    budget(width, height, len(frames))
    bg = colour(background, (0, 0, 0))
    pictures = []
    for k, f in enumerate(frames):
        px = f.get("px") if isinstance(f, dict) else None
        if not isinstance(px, list) or len(px) != width * height:
            raise ValueError(f"frame {k + 1} isn't {width}x{height}")
        img = Image.new("RGB", (width, height))
        img.putdata([colour(v, bg) for v in px])
        pictures.append((img, max(20, min(10_000, _ms(f.get("ms", 100), k)))))
    if loop == "pingpong" and len(pictures) > 2:
        pictures += pictures[-2:0:-1]
    return encode_gif(pictures, loop=loop != "once")


def frames_from(data: bytes, width: int, height: int) -> list:
    """A GIF, PNG or WebP (still or animated) as Animator frames: each frame fitted inside the panel,
    centred, transparent and black pixels left as background, its own timing kept (up to ``MAX_FRAMES``)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("that isn't a picture the Animator can read (try a GIF, PNG or WebP)") from None
    frames = []
    most = min(MAX_FRAMES, MAX_PIXELS // (width * height))   # a bigger canvas takes fewer frames
    for k, frame in enumerate(ImageSequence.Iterator(img)):
        if k >= most:
            break
        rgba = frame.convert("RGBA")
        scale = min(width / rgba.width, height / rgba.height)
        size = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
        if size != rgba.size:   # shrinking smooths; growing a small sprite keeps its hard pixels
            rgba = rgba.resize(size, Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.NEAREST)
        canvas = Image.new("RGBA", (width, height))
        canvas.paste(rgba, ((width - size[0]) // 2, (height - size[1]) // 2))
        rgba_px = canvas.tobytes()   # (getdata is deprecated in newer Pillow)
        px = [None if rgba_px[i + 3] < 128 or not any(rgba_px[i:i + 3]) else "#%02x%02x%02x" % tuple(rgba_px[i:i + 3])
              for i in range(0, len(rgba_px), 4)]   # black is an LED that's off: the background
        ms = int(frame.info.get("duration") or img.info.get("duration") or 100)
        frames.append({"px": px, "ms": max(20, min(10_000, ms))})
    return frames


def save(name: str, data: bytes) -> str:
    """Into the media library as ``uploaded/<name>.gif`` (never over another file). A name a built-in screen
    already has (``splash_tetris_squares``) gets ``my_`` in front: an upload with a built-in file's name is
    taken for a copy of it, and Splash and Sleep would play the built-in one, not this."""
    stem = media.clean_name(f"{name or 'animation'}.gif")
    if media.resolve(f"built-in/{stem}"):
        stem = f"my_{stem}"
    return media.save_upload(stem, io.BytesIO(data))


def save_preview(data: bytes) -> str:
    path = media.UPLOADED / ".animator" / "preview.gif"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return PREVIEW


# -- projects ---------------------------------------------------------------------------------------
def projects_dir() -> Path:
    """Where your own projects are kept: beside the uploads, in data/ (the folder the service may write)."""
    return media.UPLOADED.parent / "animator"


def clean_project_name(name) -> str:
    """A project's name is its file name: lower-case letters, digits, dash and underscore."""
    stem = re.sub(r"[^a-z0-9_-]+", "_", str(name).strip().lower()).strip("_-")[:40].strip("_-")
    if not stem:
        raise ValueError("give it a name (letters, digits, - and _)")
    return stem


def _cel(text, palette: list, count: int) -> list:
    """One layer's pixels in one frame, from its runs: ``12*a`` is twelve of palette colour a (base 36), ``.`` empty."""
    px: list = []
    for token in str(text).split(" "):
        if not token:
            continue
        match = TOKEN.fullmatch(token)
        if match is None:
            raise ValueError("a layer has pixels the file doesn't describe")
        n, name = int(match[1] or 1), match[2]
        index = -1 if name == "." else int(name, 36)
        if index >= len(palette):
            raise ValueError("a layer uses a colour the file doesn't list")
        if len(px) + n > count:
            raise ValueError("a layer has pixels that don't fit its size")
        px.extend([None if index < 0 else palette[index]] * n)
    if len(px) != count:
        raise ValueError("a layer has too few pixels for its size")
    return px


def check_project(data) -> dict:
    """A project file, checked all the way through (every layer of every frame is read). Returns what to
    keep: the known keys only, so a saved file never carries anything else."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("that isn't an Animator project")
    if data.get("version") != 1 or isinstance(data.get("version"), bool):
        raise ValueError("that project was made by a newer Animator")
    width, height = side(data.get("width"), "width"), side(data.get("height"), "height")
    palette = data.get("palette")
    if not isinstance(palette, list) or len(palette) > MAX_COLOURS or not all(isinstance(c, str) and HEX.fullmatch(c) for c in palette):
        raise ValueError("its colours look wrong (they are #rrggbb)")
    layers = data.get("layers")
    if not isinstance(layers, list) or not 1 <= len(layers) <= MAX_LAYERS or not all(isinstance(l, dict) for l in layers):
        raise ValueError(f"a project has 1 to {MAX_LAYERS} layers")
    kept_layers = []
    for k, layer in enumerate(layers):
        opacity = layer.get("opacity", 1)
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not 0 <= opacity <= 1:
            raise ValueError("a layer's opacity is between 0 and 1")
        if layer.get("mode", "normal") not in ("normal", "light"):
            raise ValueError("a layer is normal or adds light")
        kept_layers.append({"name": str(layer.get("name") or f"Layer {k + 1}")[:24], "on": layer.get("on") is not False,
                            "opacity": int(opacity) if opacity in (0, 1) else float(opacity), "mode": layer.get("mode", "normal"),
                            "shared": layer.get("shared") is True})
    frames = data.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError(f"a project has 1 to {MAX_FRAMES} frames")
    budget(width, height, len(frames))
    kept_frames = []
    for k, frame in enumerate(frames):
        cels = frame.get("cels") if isinstance(frame, dict) else None
        if not isinstance(cels, list) or len(cels) != len(layers) or not all(isinstance(c, str) for c in cels):
            raise ValueError(f"frame {k + 1} doesn't have every layer")
        cels = ["=" if k and kept_layers[j]["shared"] else cel for j, cel in enumerate(cels)]   # a shared layer is frame 1's, on every frame
        for cel in cels:
            if cel == "=":
                if k == 0:
                    raise ValueError("the first frame can't repeat one before it")
            else:
                _cel(cel, palette, width * height)
        kept_frames.append({"ms": max(20, min(5000, _ms(frame.get("ms", 90), k))), "cels": cels})
    return {"format": FORMAT, "version": 1, "name": clean_project_name(data.get("name", "animation")),
            "width": width, "height": height,
            "background": data["background"].lower() if isinstance(data.get("background"), str) and HEX.fullmatch(data["background"]) else "#000000",
            "loop": data.get("loop") if data.get("loop") in ("loop", "pingpong", "once") else "loop",
            "palette": [c.lower() for c in palette], "layers": kept_layers, "frames": kept_frames}


def dumps(project: dict) -> str:
    """The file's text: a line per frame, so a change to one frame is one changed line."""
    head = {k: v for k, v in project.items() if k != "frames"}
    frames = ",\n".join(json.dumps(f, separators=(",", ":")) for f in project["frames"])
    return json.dumps(head, separators=(",", ":"))[:-1] + ',"frames":[\n' + frames + "\n]}\n"


def _path(name: str) -> Path | None:
    """A project's file: yours if you've saved one by that name, else the built-in one."""
    stem = clean_project_name(name)
    for folder in (projects_dir(), BUILT_IN):
        if (folder / f"{stem}.json").is_file():
            return folder / f"{stem}.json"
    return None


HEAD = re.compile(r'"width"\s*:\s*(\d+)\s*,\s*"height"\s*:\s*(\d+)')


def _own(folder: Path) -> list[Path]:
    """The project files in a folder: named as a project is named (nothing hidden, nothing half-saved)."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.json") if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", p.stem) and not p.stem.endswith(("_", "-")))


BADGE = re.compile(r"logo(_simple)?_(blue|red|tan|pink|yellow|green)")


def projects() -> list[dict]:
    """Every project that can be opened, yours first: name, size and whether it's built in. A built-in one
    you've saved your own version of is listed once, as yours."""
    found: dict[str, dict] = {}
    for folder, built_in in ((BUILT_IN, True), (projects_dir(), False)):
        for path in _own(folder):
            if built_in and BADGE.fullmatch(path.stem):
                continue   # the logo in each of its six colours: used by the hub, not twelve more things to open
            try:
                with open(path, encoding="utf-8") as f:
                    size = HEAD.search(f.read(2048))      # the size is near the top of a file the Animator wrote
                if size is None:                          # written by hand, or by something else: read it all
                    whole = json.loads(path.read_text(encoding="utf-8"))
                    size = (None, whole["width"], whole["height"])
                found[path.stem] = {"name": path.stem, "width": int(size[1]), "height": int(size[2]),
                                    "built_in": built_in, "replaces_built_in": not built_in and path.stem in found,
                                    "bytes": path.stat().st_size}
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                continue   # not one of ours: leave it alone
    return sorted(found.values(), key=lambda p: (p["built_in"], p["name"]))


def load_project(name: str) -> dict:
    path = _path(name)
    if path is None:
        raise FileNotFoundError(name)
    return read_project(path)


def read_project(path: Path) -> dict:
    """The project in a file, checked. It is called what its file is called, whatever name is written inside."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except RecursionError:
        raise ValueError("that isn't an Animator project") from None
    return check_project({**data, "name": path.stem} if isinstance(data, dict) else data)


def save_project(data) -> str:
    """Keep a project in data/animator under its own name (saving again replaces it). Returns the name."""
    project = check_project(data)
    text = dumps(project)
    if len(text.encode()) > MAX_PROJECT:
        raise ValueError(f"that project is bigger than {MAX_PROJECT // (1024 * 1024)} MB; use fewer frames or layers")
    folder = projects_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{project['name']}.json"
    if not target.exists() and len(_own(folder)) >= MAX_PROJECTS:
        raise ValueError(f"there are already {MAX_PROJECTS} saved drawings; delete one you don't need first")
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".saving-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())   # whole on the card before it takes the old one's place: a pulled plug loses nothing
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return project["name"]


def delete_project(name: str) -> None:
    """Delete one of your projects. Built-in ones can't be deleted (deleting your version brings it back)."""
    path = projects_dir() / f"{clean_project_name(name)}.json"
    if not path.is_file():
        if (BUILT_IN / path.name).is_file():
            raise PermissionError("built-in projects can't be deleted")
        raise FileNotFoundError(name)
    path.unlink()


def _byte(value: float) -> int:
    whole = math.floor(value)   # as JavaScript's Math.round (halves go up), so both draw the same
    return max(0, min(255, whole + (value - whole >= 0.5)))


def flatten(project: dict) -> list[tuple[Image.Image, int]]:
    """A checked project as pictures: each frame's shown layers laid over each other, bottom first, on a
    see-through canvas (RGBA), and how long it shows. A see-through layer blends with what is under it,
    or with the project's background colour where nothing is; a light layer adds its colours. The same
    sums as ``compose`` in js/animator-tools.js, so the panel shows what the Animator showed."""
    width, height, count = project["width"], project["height"], project["width"] * project["height"]
    palette = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in project["palette"]]
    background = tuple(int(project["background"][i:i + 2], 16) for i in (1, 3, 5))
    layers, before, out = project["layers"], None, []
    for frame in project["frames"]:
        cels = [before[k] if before and (cel == "=" or layers[k]["shared"]) else _cel(cel, palette, count)
                for k, cel in enumerate(frame["cels"])]
        before = cels
        px: list = [None] * count
        for layer, cel in zip(layers, cels):
            opacity = layer["opacity"]
            if not layer["on"] or opacity <= 0:
                continue
            light = layer["mode"] == "light"
            for i, c in enumerate(cel):
                if c is None:
                    continue
                if opacity >= 1 and not light:
                    px[i] = c
                    continue
                a = px[i] or background
                mixed = tuple(_byte(a[j] + c[j] * opacity) if light else _byte(a[j] + (c[j] - a[j]) * opacity) for j in range(3))
                if px[i] is not None or mixed != background:   # too faint to show on nothing: stays empty
                    px[i] = mixed
        img = Image.new("RGBA", (width, height))
        img.putdata([(0, 0, 0, 0) if c is None else (*c, 255) for c in px])
        out.append((img, frame["ms"]))
    return out
