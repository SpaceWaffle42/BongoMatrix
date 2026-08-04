"""Decode and re-encode uploaded media in a resource-limited helper process."""

from __future__ import annotations

import json
import math
import os
import pwd
import resource
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

MATRIX_SIZE = (64, 32)
MAX_SOURCE_PIXELS = 40_000_000
MAX_FRAMES = 500
MAX_TOTAL_SOURCE_PIXELS = 500_000_000
MAX_VIDEO_DURATION = 300
MAX_VIDEO_SOURCE_FPS = 120
VIDEO_OUTPUT_FPS = 20
MAX_VIDEO_FRAMES = MAX_VIDEO_DURATION * VIDEO_OUTPUT_FPS
MAX_VIDEO_OUTPUT_BYTES = 48 * 1024 * 1024
FORMAT_BY_EXTENSION = {
    ".gif": "GIF",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
}


def _run_media_tool(command, pass_fds):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            pass_fds=pass_fds,
            timeout=45,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = (error.stderr or error.stdout or "media tool failed").strip()
        raise ValueError(detail[-1000:]) from error


def _probe_mp4(file_object):
    if file_object.writable():
        file_object.flush()
    fd = file_object.fileno()
    os.lseek(fd, 0, os.SEEK_SET)
    result = _run_media_tool(
        [
            "/usr/bin/ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", f"pipe:{fd}",
        ],
        (fd,),
    )
    try:
        metadata = json.loads(result.stdout)
        formats = set(metadata["format"]["format_name"].split(","))
        video = next(
            stream for stream in metadata["streams"] if stream.get("codec_type") == "video"
        )
    except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as error:
        raise ValueError("MP4 does not contain a valid video stream") from error
    if "mp4" not in formats:
        raise ValueError("file content does not match its extension")
    return metadata, video


def _fraction(value):
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (AttributeError, ValueError, ZeroDivisionError):
        return 0


def _sanitize_mp4(source, destination):
    metadata, video = _probe_mp4(source)
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    try:
        duration = float(video.get("duration") or metadata["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("MP4 duration is unavailable") from error
    frame_rate = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if width < 1 or height < 1 or width * height > MAX_SOURCE_PIXELS:
        raise ValueError("video dimensions exceed the upload policy")
    if not math.isfinite(duration) or not 0 < duration <= MAX_VIDEO_DURATION:
        raise ValueError(f"MP4 duration must be between 0 and {MAX_VIDEO_DURATION} seconds")
    if not math.isfinite(frame_rate) or not 0 < frame_rate <= MAX_VIDEO_SOURCE_FPS:
        raise ValueError("MP4 frame rate exceeds the upload policy")

    source_fd, destination_fd = source.fileno(), destination.fileno()
    os.lseek(source_fd, 0, os.SEEK_SET)
    os.lseek(destination_fd, 0, os.SEEK_SET)
    destination.truncate()
    _run_media_tool(
        [
            "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
            "-i", f"/proc/self/fd/{source_fd}",
            "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", (
                "scale=64:32:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=64:32:(ow-iw)/2:(oh-ih)/2:black,fps=20"
            ),
            "-frames:v", str(MAX_VIDEO_FRAMES),
            "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", f"pipe:{destination_fd}",
        ],
        (source_fd, destination_fd),
    )
    destination.flush()
    if os.fstat(destination_fd).st_size > MAX_VIDEO_OUTPUT_BYTES:
        raise ValueError("sanitized MP4 exceeds the output size policy")
    _, optimized = _probe_mp4(destination)
    if (int(optimized.get("width", 0)), int(optimized.get("height", 0))) != MATRIX_SIZE:
        raise ValueError("sanitized MP4 dimensions are invalid")
    if abs(_fraction(optimized.get("avg_frame_rate")) - VIDEO_OUTPUT_FPS) > 0.01:
        raise ValueError("sanitized MP4 frame rate is invalid")


def drop_privileges():
    if os.geteuid() != 0:
        return
    nobody = pwd.getpwnam("nobody")
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    os.setgroups([])
    os.setgid(nobody.pw_gid)
    os.setuid(nobody.pw_uid)


def sanitize(source, destination, filename):
    suffix = Path(filename).suffix.lower()
    if suffix == ".mp4":
        _sanitize_mp4(source, destination)
        return
    expected = FORMAT_BY_EXTENSION.get(suffix)
    if expected is None:
        raise ValueError("unsupported extension")

    with Image.open(source) as image:
        if image.format != expected:
            raise ValueError("file content does not match its extension")
        width, height = image.size
        frame_count = getattr(image, "n_frames", 1)
        if width < 1 or height < 1 or width * height > MAX_SOURCE_PIXELS:
            raise ValueError("image dimensions exceed the upload policy")
        if frame_count < 1 or frame_count > MAX_FRAMES:
            raise ValueError("animation has too many frames")
        if width * height * frame_count > MAX_TOTAL_SOURCE_PIXELS:
            raise ValueError("animation is too large when decoded")
        if expected != "GIF" and frame_count != 1:
            raise ValueError("only GIF uploads may be animated")

        if expected == "GIF":
            frames = []
            durations = []
            for frame in ImageSequence.Iterator(image):
                frame.load()
                optimized = frame.convert("RGBA")
                optimized.thumbnail(MATRIX_SIZE, Image.Resampling.LANCZOS)
                frames.append(optimized)
                durations.append(max(20, min(10_000, int(frame.info.get("duration", 100) or 100))))
            frames[0].save(
                destination,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
                optimize=True,
            )
        else:
            image.seek(0)
            image.load()
            output = image.convert("RGB" if expected == "JPEG" else "RGBA")
            output.thumbnail(MATRIX_SIZE, Image.Resampling.LANCZOS)
            if expected == "JPEG":
                save_options = {"quality": 85, "optimize": True, "progressive": True}
            elif expected == "WEBP":
                save_options = {"quality": 85, "method": 6}
            elif expected == "PNG":
                save_options = {"optimize": True, "compress_level": 9}
            else:
                save_options = {}
            output.save(destination, format=expected, **save_options)

    destination.flush()
    destination.seek(0)
    with Image.open(destination) as verified:
        if verified.format != expected:
            raise ValueError("sanitized output format mismatch")
        verified.verify()


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: media_sanitizer.py SOURCE DESTINATION FILENAME")
    Image.init()
    drop_privileges()
    try:
        source_fd = int(sys.argv[1])
        destination_fd = int(sys.argv[2])
        with os.fdopen(os.dup(source_fd), "rb") as source:
            with os.fdopen(os.dup(destination_fd), "w+b") as destination:
                sanitize(source, destination, sys.argv[3])
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
