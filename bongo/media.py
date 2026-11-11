"""The media library: two folders the Media module (and splash/sleep) can show files from.

    built-in/   resources/media/built-in, shipped with Bongo Matrix (read-only). Only Splash and
                Sleep screens live here: ``splash_*`` files are built-in splash screens and
                ``sleep_*`` files are built-in sleep screens. Anything else is the user's own.
    uploaded/   data/uploads, files added from the dashboard (git-ignored, like v2, so updates
                never touch them; data/ is also where the systemd service may write)

Settings name files as ``built-in/sleep_default.gif`` or ``uploaded/cat.mp4``. Nothing outside these
two folders can be shown, so a setting can't point the panel (or the preview) at other files
on the Pi. Uploads are checked before they are kept: allowed type, size, and that the file
really decodes as an image or a video.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[1]
BUILT_IN = REPO / "resources" / "media" / "built-in"
# data/ is the one folder the systemd service may write to (ReadWritePaths), so uploads live there
UPLOADED = Path(os.environ.get("BONGO_UPLOADS", REPO / "data" / "uploads"))
OLD_UPLOADED = REPO / "resources" / "media" / "uploaded"  # before 11 Nov 2026

IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEOS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
MAX_UPLOAD = 50 * 1024 * 1024        # one file
MAX_LIBRARY = 1024 * 1024 * 1024     # everything in uploaded/
MAX_PIXELS = 16_000_000              # refuse decompression bombs
SCREENS = ("splash", "sleep")        # built-in files are named <screen>_*.ext


def screen_of(name) -> str | None:
    """'splash' or 'sleep' for a built-in screen file (by its prefix), else None."""
    if not isinstance(name, str) or not name.startswith("built-in/"):
        return None
    file = name.split("/", 1)[1]
    return next((s for s in SCREENS if file.startswith(f"{s}_")), None)


def folders() -> dict[str, Path]:
    return {"built-in": BUILT_IN, "uploaded": UPLOADED}


def valid_name(name) -> bool:
    """``folder/file.ext`` with a known folder and a media extension. Doesn't check the file exists."""
    if not isinstance(name, str) or "/" not in name:
        return False
    folder, rest = name.split("/", 1)
    return folder in folders() and Path(rest).suffix.lower() in IMAGES | VIDEOS and ".." not in Path(rest).parts


def resolve(name) -> Path | None:
    """The file a setting names, or None if it's missing or outside the two folders."""
    if not valid_name(name):
        return None
    folder, rest = name.split("/", 1)
    root = folders()[folder].resolve()
    path = (root / rest).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEOS


def library() -> dict[str, list[dict]]:
    """Every file in both folders, for the dashboard's picker."""
    out = {}
    for folder, root in folders().items():
        files = []
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                hidden = any(part.startswith(".") for part in p.relative_to(root).parts)  # half-done uploads
                if p.is_file() and not hidden and p.suffix.lower() in IMAGES | VIDEOS:
                    rel = p.relative_to(root).as_posix()
                    name = f"{folder}/{rel}"
                    if folder == "built-in" and screen_of(name) is None:
                        continue  # only splash_* and sleep_* are built-in
                    files.append({"name": name, "file": rel, "bytes": p.stat().st_size,
                                  "kind": "video" if is_video(p) else "image", "screen": screen_of(name)})
        out[folder] = files
    return out


def used_bytes() -> int:
    return sum(f["bytes"] for f in library()["uploaded"])


def clean_name(filename: str) -> str:
    """A safe, readable file name: ASCII letters, digits, dot, dash and underscore only."""
    stem, suffix = os.path.splitext(os.path.basename(str(filename)))
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:60] or "upload"
    return stem + suffix.lower()


def check_file(path: Path) -> str:
    """Raise ValueError unless ``path`` really is a usable image or video. Returns the kind."""
    if is_video(path):
        if not (shutil.which("ffprobe") and shutil.which("ffmpeg")):
            raise ValueError("videos need ffmpeg on the Matrix (sudo apt install ffmpeg)")
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "json", str(path)],
                               capture_output=True, timeout=20)
        streams = json.loads(probe.stdout or b"{}").get("streams") if probe.returncode == 0 else None
        if not streams:
            raise ValueError("that file isn't a video ffmpeg can play")
        return "video"
    try:
        with Image.open(path) as im:
            if im.width * im.height > MAX_PIXELS:
                raise ValueError(f"that image is too large ({im.width}×{im.height}); keep it under 16 megapixels")
            im.verify()
    except ValueError:
        raise
    except Exception:
        raise ValueError("that file isn't an image Bongo Matrix can read") from None
    return "image"


def adopt_old_uploads(old: Path = OLD_UPLOADED) -> int:
    """Move files from the old uploads folder into UPLOADED (same names, so settings still match)."""
    if not old.is_dir() or old.resolve() == UPLOADED.resolve():
        return 0
    moved = 0
    for path in sorted(old.iterdir()):
        target = UPLOADED / path.name
        if path.is_file() and not path.name.startswith(".") and not target.exists():
            UPLOADED.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), target)
            moved += 1
    return moved


def save_upload(filename: str, stream) -> str:
    """Check an uploaded file and keep it in uploaded/. Returns its setting name (``uploaded/x.gif``)."""
    name = clean_name(filename)
    suffix = Path(name).suffix
    if suffix not in IMAGES | VIDEOS:
        raise ValueError(f"{suffix or 'that file'} isn't supported; use "
                         f"{', '.join(sorted(s.lstrip('.') for s in IMAGES | VIDEOS))}")
    UPLOADED.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=UPLOADED, prefix=".upload-", suffix=suffix)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(1 << 16):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise ValueError(f"files can be up to {MAX_UPLOAD // (1024 * 1024)} MB")
                out.write(chunk)
        if not size:
            raise ValueError("that file is empty")
        if used_bytes() + size > MAX_LIBRARY:
            raise ValueError("the uploaded folder is full (1 GB); delete something first")
        check_file(Path(tmp))
        target, n = UPLOADED / name, 2
        while target.exists():  # never overwrite: cat.gif, cat-2.gif, ...
            target, n = UPLOADED / f"{Path(name).stem}-{n}{suffix}", n + 1
        os.replace(tmp, target)
        os.chmod(target, 0o644)
        return f"uploaded/{target.name}"
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def delete(name: str) -> None:
    """Delete an uploaded file. Built-in files can't be deleted."""
    if not name.startswith("uploaded/"):
        raise PermissionError("built-in files can't be deleted")
    path = resolve(name)
    if path is None:
        raise FileNotFoundError(name)
    path.unlink()
