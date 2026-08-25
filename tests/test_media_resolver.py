import copy
import fcntl
import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import media_resolver
from config_store import DEFAULT_CONFIG, validate_config
from edit_preview import CaptureMatrix
from media_resolver import MediaResolutionError, MediaResolver
from modules import announcement_board, custom_text, sleep_screen, splash_screen


class MediaResolverTests(unittest.TestCase):
    def test_missing_traversal_directory_and_symlink_media_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            media_root = temporary_root / "media"
            media_root.mkdir()
            outside_root = temporary_root / "outside"
            outside_root.mkdir()
            outside_file = outside_root / "outside.png"
            outside_bytes = b"outside bytes must never be read"
            outside_file.write_bytes(outside_bytes)

            directory_media = media_root / "directory.png"
            directory_media.mkdir()
            in_root_target = media_root / "target.png"
            in_root_target.write_bytes(b"in-root target")
            (media_root / "linked-in-root.png").symlink_to(in_root_target)
            (media_root / "linked-outside.png").symlink_to(outside_file)
            real_built_in = media_root / "real-built-in"
            real_built_in.mkdir()
            (real_built_in / "asset.png").write_bytes(b"built-in target")
            (media_root / "built-in").symlink_to(real_built_in, target_is_directory=True)

            resolver = MediaResolver(media_root)
            descriptors_before = set(os.listdir("/proc/self/fd"))
            rejected = {
                "missing": ("missing.png", "not_found"),
                "absolute": (str(outside_file), "invalid_reference"),
                "parent traversal": ("../outside/outside.png", "invalid_reference"),
                "normalized parent traversal": (
                    "built-in/../linked-outside.png",
                    "invalid_reference",
                ),
                "directory": ("directory.png", "not_regular"),
                "final symlink in root": ("linked-in-root.png", "unsafe_path"),
                "final symlink outside root": ("linked-outside.png", "unsafe_path"),
                "symlinked built-in component": (
                    "built-in/asset.png",
                    "unsafe_path",
                ),
            }

            with patch("os.read", side_effect=AssertionError("resolver read media bytes")):
                for label, (reference, expected_code) in rejected.items():
                    with self.subTest(label=label):
                        with self.assertRaises(MediaResolutionError) as raised:
                            resolver.resolve(reference)
                        self.assertEqual(raised.exception.code, expected_code)
                        self.assertNotIn(str(temporary_root), str(raised.exception))
                        self.assertNotIn(str(outside_file), str(raised.exception))

            self.assertEqual(outside_file.read_bytes(), outside_bytes)
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_open_regular_media_is_confined_and_pinned_without_following_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            media_root = temporary_root / "media"
            media_root.mkdir()
            built_in_root = media_root / "built-in"
            built_in_root.mkdir()
            outside_file = temporary_root / "outside.png"
            outside_bytes = b"outside replacement bytes"
            outside_file.write_bytes(outside_bytes)
            media = {
                "regular.png": b"pinned regular bytes",
                "built-in/allowed.gif": b"pinned built-in bytes",
            }
            for reference, content in media.items():
                media_root.joinpath(reference).write_bytes(content)

            resolver = MediaResolver(media_root)
            descriptors_before = set(os.listdir("/proc/self/fd"))
            real_open = os.open
            open_calls = []

            def recording_open(path, flags, *args, **kwargs):
                open_calls.append((Path(path), flags))
                return real_open(path, flags, *args, **kwargs)

            with patch("os.open", side_effect=recording_open):
                for reference, expected_bytes in media.items():
                    with self.subTest(reference=reference):
                        original_path = media_root.joinpath(reference)
                        original_bytes = original_path.read_bytes()
                        with resolver.resolve(reference) as handle:
                            self.assertIsInstance(
                                handle, media_resolver.PinnedMediaHandle
                            )
                            descriptor = handle.fileno()
                            self.assertGreaterEqual(descriptor, 0)
                            self.assertFalse(handle.closed)
                            self.assertEqual(open_calls[-1][1] & os.O_ACCMODE, os.O_RDONLY)
                            self.assertEqual(
                                fcntl.fcntl(descriptor, fcntl.F_GETFD)
                                & fcntl.FD_CLOEXEC,
                                fcntl.FD_CLOEXEC,
                            )
                            self.assertTrue(
                                Path(f"/proc/self/fd/{descriptor}")
                                .resolve(strict=True)
                                .is_relative_to(media_root.resolve(strict=True))
                            )
                            self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 0)
                            self.assertEqual(
                                os.pread(descriptor, len(expected_bytes), 0),
                                expected_bytes,
                            )
                            self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 0)
                            self.assertEqual(original_path.read_bytes(), original_bytes)

                            original_path.unlink()
                            original_path.symlink_to(outside_file)
                            self.assertEqual(
                                os.pread(descriptor, len(expected_bytes), 0),
                                expected_bytes,
                            )
                            self.assertEqual(outside_file.read_bytes(), outside_bytes)

                        self.assertTrue(handle.closed)
                        handle.close()
                        handle.close()
                        self.assertTrue(handle.closed)
                        with self.assertRaises(OSError):
                            os.fstat(descriptor)
                        self.assertEqual(original_bytes, expected_bytes)
                        original_path.unlink()

            required_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            self.assertEqual(len(open_calls), len(media))
            for opened_path, flags in open_calls:
                self.assertTrue(opened_path.is_relative_to(media_root))
                self.assertEqual(flags & required_flags, required_flags)
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_replace_or_remove_after_validation_cannot_change_pinned_decode_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            media_root = temporary_root / "media"
            media_root.mkdir()
            outside_file = temporary_root / "outside.png"
            outside_bytes = b"outside target bytes"
            outside_file.write_bytes(outside_bytes)
            originals = {
                "replaced.png": b"original atomically replaced bytes",
                "removed.png": b"original unlinked bytes",
                "linked.png": b"original symlink-substituted bytes",
            }
            for reference, content in originals.items():
                media_root.joinpath(reference).write_bytes(content)

            resolver = MediaResolver(media_root)
            descriptors_before = set(os.listdir("/proc/self/fd"))
            handles = {
                reference: resolver.resolve(reference) for reference in originals
            }
            pinned_identities = {
                reference: (
                    os.fstat(handle.fileno()).st_dev,
                    os.fstat(handle.fileno()).st_ino,
                )
                for reference, handle in handles.items()
            }

            try:
                replacement_path = media_root / "replacement.tmp"
                replacement_bytes = b"current replacement bytes"
                replacement_path.write_bytes(replacement_bytes)
                os.replace(replacement_path, media_root / "replaced.png")
                (media_root / "removed.png").unlink()
                (media_root / "linked.png").unlink()
                (media_root / "linked.png").symlink_to(outside_file)

                for reference, expected_bytes in originals.items():
                    with self.subTest(reference=reference):
                        descriptor = handles[reference].fileno()
                        proc_descriptor = f"/proc/self/fd/{descriptor}"
                        descriptor_status = os.stat(proc_descriptor)
                        self.assertEqual(
                            (descriptor_status.st_dev, descriptor_status.st_ino),
                            pinned_identities[reference],
                        )
                        with patch(
                            "os.open",
                            side_effect=AssertionError("pinned handle reopened pathname"),
                        ):
                            self.assertEqual(
                                os.pread(descriptor, len(expected_bytes), 0),
                                expected_bytes,
                            )

                with resolver.resolve("replaced.png") as fresh_handle:
                    self.assertEqual(
                        os.pread(fresh_handle.fileno(), len(replacement_bytes), 0),
                        replacement_bytes,
                    )
                    self.assertNotEqual(
                        (
                            os.fstat(fresh_handle.fileno()).st_dev,
                            os.fstat(fresh_handle.fileno()).st_ino,
                        ),
                        pinned_identities["replaced.png"],
                    )
                with self.assertRaises(MediaResolutionError) as removed:
                    resolver.resolve("removed.png")
                self.assertEqual(removed.exception.code, "not_found")
                with self.assertRaises(MediaResolutionError) as linked:
                    resolver.resolve("linked.png")
                self.assertEqual(linked.exception.code, "unsafe_path")

                self.assertEqual(
                    (media_root / "replaced.png").read_bytes(), replacement_bytes
                )
                self.assertEqual(outside_file.read_bytes(), outside_bytes)
                self.assertFalse(replacement_path.exists())
            finally:
                descriptors = [handle.fileno() for handle in handles.values()]
                for handle in handles.values():
                    handle.close()
                    handle.close()
                    self.assertTrue(handle.closed)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_resolver_close_releases_every_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            (media_root / "built-in").mkdir()
            media = {
                "first.png": b"first regular bytes",
                "second.gif": b"second regular bytes",
                "built-in/third.jpg": b"third built-in bytes",
                "built-in/fourth.webp": b"fourth built-in bytes",
            }
            for reference, content in media.items():
                media_root.joinpath(reference).write_bytes(content)

            resolver = MediaResolver(media_root)
            descriptors_before = set(os.listdir("/proc/self/fd"))
            handles = [resolver.resolve(reference) for reference in media]
            descriptors = [handle.fileno() for handle in handles]
            real_close = os.close
            close_counts = {descriptor: 0 for descriptor in descriptors}

            def recording_close(descriptor):
                if descriptor in close_counts:
                    close_counts[descriptor] += 1
                return real_close(descriptor)

            with patch("os.close", side_effect=recording_close):
                handles[0].close()
                resolver.close()
                resolver.close()
                for handle in handles:
                    handle.close()
                    handle.close()

            self.assertTrue(all(handle.closed for handle in handles))
            self.assertEqual(close_counts, dict.fromkeys(descriptors, 1))
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

            with patch("os.open", side_effect=AssertionError("closed resolver opened media")):
                with self.assertRaises(MediaResolutionError) as raised:
                    resolver.resolve("first.png")
            self.assertEqual(raised.exception.code, "resolver_closed")
            self.assertEqual(str(raised.exception), "media resolver is closed")
            self.assertNotIn(str(media_root), str(raised.exception))

            for reference, content in media.items():
                self.assertEqual(media_root.joinpath(reference).read_bytes(), content)
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_pillow_png_jpeg_webp_gif_decode_uses_pinned_file_object(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            fixtures = {
                "original.png": ("PNG", (3, 2), (241, 17, 31)),
                "original.jpg": ("JPEG", (4, 3), (29, 197, 61)),
                "original.webp": ("WEBP", (5, 4), (13, 47, 229)),
            }
            for reference, (image_format, size, colour) in fixtures.items():
                save_options = {"lossless": True} if image_format == "WEBP" else {}
                Image.new("RGB", size, colour).save(
                    media_root / reference,
                    format=image_format,
                    **save_options,
                )

            gif_frames = [
                Image.new("RGB", (6, 5), (223, 11, 19)),
                Image.new("RGB", (6, 5), (7, 211, 37)),
            ]
            gif_frames[0].save(
                media_root / "original.gif",
                format="GIF",
                save_all=True,
                append_images=gif_frames[1:],
                duration=[40, 90],
                loop=0,
            )
            fixtures["original.gif"] = ("GIF", (6, 5), (223, 11, 19))

            descriptors_before = set(os.listdir("/proc/self/fd"))
            resolver = MediaResolver(media_root)
            handles = {
                reference: resolver.resolve(reference) for reference in fixtures
            }
            try:
                for reference in fixtures:
                    replacement = media_root / f"replacement-{reference}"
                    Image.new("RGB", (1, 1), (0, 0, 0)).save(
                        replacement,
                        format=fixtures[reference][0],
                    )
                    os.replace(replacement, media_root / reference)

                for reference, (expected_format, expected_size, expected_colour) in fixtures.items():
                    with self.subTest(reference=reference):
                        handle = handles[reference]
                        original_descriptor = handle.fileno()

                        with (
                            handle.dup_file() as first_independent,
                            handle.dup_file() as second_independent,
                        ):
                            self.assertEqual(first_independent.tell(), 0)
                            self.assertEqual(second_independent.tell(), 0)
                            first_byte = first_independent.read(1)
                            self.assertTrue(first_byte)
                            self.assertEqual(second_independent.tell(), 0)
                            self.assertEqual(
                                os.lseek(original_descriptor, 0, os.SEEK_CUR), 0
                            )
                            self.assertEqual(second_independent.read(1), first_byte)

                        first_duplicate = handle.dup_file()
                        first_descriptor = first_duplicate.fileno()
                        self.assertEqual(first_duplicate.mode, "rb")
                        self.assertEqual(first_duplicate.tell(), 0)
                        self.assertTrue(first_duplicate.read(1))
                        first_duplicate.close()
                        first_duplicate.close()
                        with self.assertRaises(OSError):
                            os.fstat(first_descriptor)
                        self.assertEqual(handle.fileno(), original_descriptor)
                        os.fstat(original_descriptor)

                        with handle.dup_file() as image_file:
                            duplicate_descriptor = image_file.fileno()
                            self.assertEqual(image_file.tell(), 0)
                            self.assertEqual(
                                fcntl.fcntl(duplicate_descriptor, fcntl.F_GETFD)
                                & fcntl.FD_CLOEXEC,
                                fcntl.FD_CLOEXEC,
                            )
                            with patch(
                                "builtins.open",
                                side_effect=AssertionError(
                                    "Pillow reopened a media pathname"
                                ),
                            ):
                                decoded = Image.open(image_file)
                                self.assertIs(decoded.fp, image_file)
                                self.assertEqual(decoded.format, expected_format)
                                self.assertEqual(decoded.size, expected_size)
                                decoded.load()
                                if expected_format == "GIF":
                                    self.assertEqual(decoded.n_frames, 2)
                                    self.assertEqual(
                                        decoded.convert("RGB").getpixel((0, 0)),
                                        expected_colour,
                                    )
                                    decoded.seek(1)
                                    decoded.load()
                                    self.assertEqual(
                                        decoded.convert("RGB").getpixel((0, 0)),
                                        (7, 211, 37),
                                    )
                                elif expected_format == "JPEG":
                                    actual = decoded.convert("RGB").getpixel((0, 0))
                                    self.assertTrue(
                                        all(
                                            abs(channel - expected) <= 3
                                            for channel, expected in zip(
                                                actual, expected_colour
                                            )
                                        )
                                    )
                                else:
                                    self.assertEqual(
                                        decoded.convert("RGB").getpixel((0, 0)),
                                        expected_colour,
                                    )
                                decoded.close()

                        with self.assertRaises(OSError):
                            os.fstat(duplicate_descriptor)
                        self.assertFalse(handle.closed)
                        self.assertEqual(handle.fileno(), original_descriptor)
                        os.fstat(original_descriptor)
            finally:
                for handle in handles.values():
                    handle.close()
                    handle.close()
                resolver.close()
                resolver.close()

            self.assertTrue(all(handle.closed for handle in handles.values()))
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_ffmpeg_mp4_decode_inherits_only_duplicated_pinned_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            original_path = media_root / "original.mp4"
            replacement_path = media_root / "replacement.mp4"

            def generate_mp4(path, colour, size):
                completed = subprocess.run(
                    (
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={colour}:s={size}:r=1:d=1",
                        "-frames:v",
                        "1",
                        "-c:v",
                        "mpeg4",
                        "-q:v",
                        "2",
                        "-pix_fmt",
                        "yuv420p",
                        "-y",
                        os.fspath(path),
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    "ffmpeg MP4 fixture generation failed",
                )

            generate_mp4(original_path, "red", "4x2")
            generate_mp4(replacement_path, "blue", "6x4")
            original_bytes = original_path.read_bytes()
            replacement_bytes = replacement_path.read_bytes()
            self.assertNotEqual(original_bytes, replacement_bytes)

            descriptors_before = set(os.listdir("/proc/self/fd"))
            resolver = MediaResolver(media_root)
            handle = resolver.resolve("original.mp4")
            original_descriptor = handle.fileno()
            original_identity = os.fstat(original_descriptor)
            unrelated_descriptor = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
            process = None
            child_descriptor = None
            recorded_spawns = []
            real_popen = subprocess.Popen

            try:
                os.replace(replacement_path, original_path)
                original_path.unlink()
                self.assertFalse(original_path.exists())

                os.lseek(original_descriptor, 7, os.SEEK_SET)
                original_offset = os.lseek(original_descriptor, 0, os.SEEK_CUR)
                child_descriptor = handle.dup_fd()
                self.assertNotEqual(child_descriptor, original_descriptor)
                self.assertEqual(os.lseek(child_descriptor, 0, os.SEEK_CUR), 0)
                self.assertEqual(os.read(child_descriptor, 1), original_bytes[:1])
                self.assertEqual(
                    os.lseek(original_descriptor, 0, os.SEEK_CUR), original_offset
                )
                os.lseek(child_descriptor, 0, os.SEEK_SET)
                self.assertEqual(
                    fcntl.fcntl(child_descriptor, fcntl.F_GETFD)
                    & fcntl.FD_CLOEXEC,
                    fcntl.FD_CLOEXEC,
                )
                self.assertEqual(
                    fcntl.fcntl(unrelated_descriptor, fcntl.F_GETFD)
                    & fcntl.FD_CLOEXEC,
                    fcntl.FD_CLOEXEC,
                )

                command = (
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "info",
                    "-i",
                    f"/proc/self/fd/{child_descriptor}",
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                )

                def recording_popen(args, *popen_args, **popen_kwargs):
                    recorded_spawns.append((tuple(args), dict(popen_kwargs)))
                    return real_popen(args, *popen_args, **popen_kwargs)

                with patch("subprocess.Popen", side_effect=recording_popen):
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        close_fds=True,
                        pass_fds=(child_descriptor,),
                    )
                spawned_pid = process.pid
                os.close(child_descriptor)
                closed_child_descriptor = child_descriptor
                child_descriptor = None
                with self.assertRaises(OSError):
                    os.fstat(closed_child_descriptor)

                decoded_bytes, decoder_stderr = process.communicate(timeout=10)
                self.assertEqual(
                    process.returncode,
                    0,
                    "ffmpeg pinned MP4 decode failed",
                )
                process = None

                self.assertEqual(len(recorded_spawns), 1)
                spawned_command, spawned_kwargs = recorded_spawns[0]
                self.assertEqual(spawned_command, command)
                self.assertEqual(
                    spawned_kwargs.get("pass_fds"), (closed_child_descriptor,)
                )
                self.assertIs(spawned_kwargs.get("close_fds"), True)
                self.assertNotIn(unrelated_descriptor, spawned_kwargs["pass_fds"])
                self.assertEqual(len(decoded_bytes), 4 * 2 * 3)
                pixels = tuple(
                    tuple(decoded_bytes[index : index + 3])
                    for index in range(0, len(decoded_bytes), 3)
                )
                self.assertTrue(
                    all(red >= 245 and green <= 10 and blue <= 10 for red, green, blue in pixels)
                )
                self.assertIn(b"Video: mpeg4", decoder_stderr)
                self.assertIn(b"4x2", decoder_stderr)
                self.assertFalse(Path(f"/proc/{spawned_pid}").exists())
                self.assertEqual(
                    (os.fstat(original_descriptor).st_dev, os.fstat(original_descriptor).st_ino),
                    (original_identity.st_dev, original_identity.st_ino),
                )
                self.assertEqual(
                    os.lseek(original_descriptor, 0, os.SEEK_CUR), original_offset
                )
                self.assertEqual(
                    os.pread(original_descriptor, len(original_bytes), 0), original_bytes
                )
                self.assertFalse(handle.closed)
            finally:
                if process is not None:
                    process.kill()
                    process.communicate(timeout=10)
                if child_descriptor is not None:
                    os.close(child_descriptor)
                os.close(unrelated_descriptor)
                handle.close()
                handle.close()
                resolver.close()
                resolver.close()

            self.assertTrue(handle.closed)
            with self.assertRaises(OSError):
                os.fstat(original_descriptor)
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)


    def test_splash_sleep_announcement_and_canvas_use_pinned_handles_after_unlink(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            originals = {
                "splash.gif": ("GIF", (213, 17, 31)),
                "sleep.gif": ("GIF", (19, 211, 37)),
                "announcement.png": ("PNG", (23, 43, 227)),
                "canvas.png": ("PNG", (239, 193, 29)),
            }
            replacements = {
                "splash.gif": (3, 5, 7),
                "sleep.gif": (11, 13, 17),
                "announcement.png": (19, 23, 29),
                "canvas.png": (31, 37, 41),
            }
            for reference, (image_format, colour) in originals.items():
                first = Image.new("RGB", (64, 32), colour)
                if image_format == "GIF":
                    second = Image.new("RGB", (64, 32), tuple(reversed(colour)))
                    first.save(
                        media_root / reference, format=image_format, save_all=True,
                        append_images=[second], duration=[100, 100], loop=0,
                    )
                else:
                    first.save(media_root / reference, format=image_format)

            descriptors_before = set(os.listdir("/proc/self/fd"))
            resolver = MediaResolver(media_root)
            handles = {
                reference: resolver.resolve(reference) for reference in originals
            }
            identities = {
                reference: (os.fstat(handle.fileno()).st_dev, os.fstat(handle.fileno()).st_ino)
                for reference, handle in handles.items()
            }
            for reference, replacement_colour in replacements.items():
                replacement = media_root / f"replacement-{reference}"
                Image.new("RGB", (64, 32), replacement_colour).save(
                    replacement, format=originals[reference][0],
                )
                os.replace(replacement, media_root / reference)
                self.assertNotEqual(
                    (os.stat(media_root / reference).st_dev, os.stat(media_root / reference).st_ino),
                    identities[reference],
                )

            fake_graphics = types.SimpleNamespace(
                Color=lambda red, green, blue: types.SimpleNamespace(
                    red=red, green=green, blue=blue,
                ),
                Font=object,
            )
            fake_rgbmatrix = types.SimpleNamespace(
                RGBMatrix=object, RGBMatrixOptions=object, graphics=fake_graphics,
            )
            previous_rgbmatrix = sys.modules.get("rgbmatrix")
            sys.modules["rgbmatrix"] = fake_rgbmatrix
            try:
                framework = importlib.import_module("matrix_framework")
            finally:
                if previous_rgbmatrix is None:
                    sys.modules.pop("rgbmatrix", None)
                else:
                    sys.modules["rgbmatrix"] = previous_rgbmatrix

            class RecordingMatrix(CaptureMatrix):
                def __init__(self):
                    super().__init__()
                    self.submissions = []

                def swap(self, canvas=None):
                    self.submissions.append(self.snapshot(canvas))
                    return super().swap(canvas)

            class RecordingStop:
                def __init__(self):
                    self.waits = []
                    self.elapsed = 0.0

                def is_set(self):
                    return False

                def wait(self, delay):
                    self.waits.append(delay)
                    self.elapsed += delay
                    return False

            def matrix_with_physical_media_seams():
                matrix = RecordingMatrix()
                matrix.show_gif = types.MethodType(framework.Matrix.show_gif, matrix)
                matrix.media_frames = types.MethodType(framework.Matrix.media_frames, matrix)
                matrix._media_file = lambda _reference: (_ for _ in ()).throw(
                    AssertionError("consumer reopened a media pathname")
                )
                return matrix

            config = copy.deepcopy(DEFAULT_CONFIG)
            config["media"]["splash"] = "splash.gif"
            config["media"]["sleep"] = "sleep.gif"
            announcement = {
                "id": "announcement_pinned", "enabled": True, "name": "Pinned",
                "layout": "full_image_notice", "headline": "PINNED", "message": "",
                "media": "announcement.png",
                "schedule": {
                    "start_date": "2030-01-15", "end_date": "2030-01-15",
                    "start_time": "09:00", "end_time": "17:00",
                    "days": [0, 1, 2, 3, 4, 5, 6],
                },
                "style": {
                    "foreground": "#ffffff", "background": "#000000", "font": "5x7",
                    "alignment": "left", "border": "none", "border_colour": "#ffffff",
                },
                "timing": {"mode": "static", "duration_seconds": 1},
                "transition": {"type": "cut"},
            }
            canvas_module = {
                "id": "custom_canvas_pinned", "name": "Pinned canvas", "type": "canvas",
                "background": "#000000", "frame_count": 1, "fps": 5,
                "playback": "hold", "overrides": [],
                "layers": [{
                    "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                    "properties": {
                        "source": "canvas.png", "x": 0, "y": 0, "width": 64,
                        "height": 32, "rotation": 0, "keep_aspect": False,
                    },
                    "keyframes": [],
                }],
            }
            config["announcement_board"]["announcements"] = [announcement]
            config["custom_modules"] = [canvas_module]
            config["playlist"] = [
                {"module": "announcement_board", "duration": 1, "enabled": True},
                {"module": canvas_module["id"], "duration": 1, "enabled": True},
            ]
            validate_config(config, enforce_save_bounds=True)
            expected_config = copy.deepcopy(config)

            observed = {}
            try:
                with self.subTest(consumer="splash"):
                    matrix, stop = matrix_with_physical_media_seams(), RecordingStop()
                    with patch.object(
                        framework.time, "monotonic", side_effect=lambda: stop.elapsed,
                    ):
                        self.assertTrue(splash_screen.run(
                            matrix, config, duration=0.2, stop_event=stop,
                            pinned_handle=handles["splash.gif"],
                        ))
                    observed["splash"] = (matrix.submissions, stop.waits)
                with self.subTest(consumer="sleep"):
                    matrix, stop = matrix_with_physical_media_seams(), RecordingStop()
                    with patch.object(
                        framework.time, "monotonic", side_effect=lambda: stop.elapsed,
                    ):
                        self.assertTrue(sleep_screen.run(
                            matrix, config, duration=0.2, stop_event=stop,
                            pinned_handle=handles["sleep.gif"],
                        ))
                    observed["sleep"] = (matrix.submissions, stop.waits)
                with self.subTest(consumer="announcement"):
                    matrix, stop = matrix_with_physical_media_seams(), RecordingStop()
                    self.assertTrue(announcement_board.run_preview(
                        matrix, announcement, stop_event=stop,
                        pinned_handles={"announcement.png": handles["announcement.png"]},
                    ))
                    observed["announcement"] = (matrix.submissions, stop.waits)
                with self.subTest(consumer="advanced_canvas"):
                    matrix, stop = matrix_with_physical_media_seams(), RecordingStop()
                    with patch(
                        "modules.custom_text.time.monotonic",
                        side_effect=lambda: stop.elapsed,
                    ):
                        self.assertTrue(custom_text.run(
                            matrix, config, duration=0.2, stop_event=stop,
                            module_config=canvas_module,
                            pinned_handles={"canvas.png": handles["canvas.png"]},
                        ))
                    observed["advanced_canvas"] = (matrix.submissions, stop.waits)

                if len(observed) != 4:
                    return
                self.assertEqual([round(delay, 9) for delay in observed["splash"][1]], [0.1, 0.1])
                self.assertEqual([round(delay, 9) for delay in observed["sleep"][1]], [0.1, 0.1])
                self.assertEqual(observed["announcement"][1], [1.0])
                self.assertEqual(observed["advanced_canvas"][1], [0.2])
                for frames, _delays in observed.values():
                    self.assertTrue(frames)
                    self.assertTrue(all(frame.size == (64, 32) for frame in frames))
                self.assertEqual(
                    observed["splash"][0][0].getpixel((0, 0)), originals["splash.gif"][1]
                )
                self.assertEqual(
                    observed["sleep"][0][0].getpixel((0, 0)), originals["sleep.gif"][1]
                )
                self.assertEqual(
                    observed["announcement"][0][0].getpixel((0, 0)),
                    originals["announcement.png"][1],
                )
                self.assertEqual(
                    observed["advanced_canvas"][0][0].getpixel((0, 0)),
                    originals["canvas.png"][1],
                )
                self.assertEqual(config, expected_config)
                for reference, replacement_colour in replacements.items():
                    with Image.open(media_root / reference) as replacement:
                        self.assertEqual(
                            replacement.convert("RGB").getpixel((0, 0)),
                            replacement_colour,
                        )
                for reference, handle in handles.items():
                    self.assertEqual(
                        (os.fstat(handle.fileno()).st_dev, os.fstat(handle.fileno()).st_ino),
                        identities[reference],
                    )
            finally:
                resolver.close()
                resolver.close()

            self.assertTrue(all(handle.closed for handle in handles.values()))
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)

    def test_cancel_error_and_complete_close_original_and_duplicate_fds_once(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            media_root = temporary_root / "media"
            media_root.mkdir()
            outside = temporary_root / "outside.gif"
            outside_colour = (3, 5, 7)
            Image.new("RGB", (64, 32), outside_colour).save(outside, format="GIF")
            outside_bytes = outside.read_bytes()

            complete_path = media_root / "complete.gif"
            first_colour = (211, 17, 31)
            first = Image.new("RGB", (64, 32), first_colour)
            second = Image.new("RGB", (64, 32), (19, 197, 37))
            first.save(
                complete_path, format="GIF", save_all=True, append_images=[second],
                duration=[10, 10], loop=0,
            )
            (media_root / "error.gif").write_bytes(b"not a decodable image")
            (media_root / "cancel.mp4").write_bytes(b"pinned decoder input")

            descriptors_before = set(os.listdir("/proc/self/fd"))
            threads_before = {thread.ident for thread in threading.enumerate()}
            resolver = MediaResolver(media_root)
            handles = {
                reference: resolver.resolve(reference)
                for reference in ("complete.gif", "error.gif", "cancel.mp4")
            }
            original_descriptors = {
                reference: handle.fileno() for reference, handle in handles.items()
            }
            original_identities = {
                reference: (
                    os.fstat(descriptor).st_dev,
                    os.fstat(descriptor).st_ino,
                )
                for reference, descriptor in original_descriptors.items()
            }
            original_close_counts = dict.fromkeys(handles, 0)
            for reference, handle in handles.items():
                release = handle._on_close

                def record_original_close(closed_handle, *, name=reference, callback=release):
                    original_close_counts[name] += 1
                    callback(closed_handle)

                handle._on_close = record_original_close

            duplicate_files = []
            duplicate_descriptors = {}

            class CountedFile:
                def __init__(self, media_file):
                    self._file = media_file
                    self.descriptor = media_file.fileno()
                    status = os.fstat(self.descriptor)
                    self.identity = (status.st_dev, status.st_ino)
                    self.close_calls = 0

                def __getattr__(self, name):
                    return getattr(self._file, name)

                def close(self):
                    self.close_calls += 1
                    self._file.close()

            class CountedHandle:
                def __init__(self, handle):
                    self._handle = handle

                def dup_file(self):
                    counted = CountedFile(self._handle.dup_file())
                    duplicate_files.append(counted)
                    return counted

                def dup_fd(self):
                    descriptor = self._handle.dup_fd()
                    status = os.fstat(descriptor)
                    duplicate_descriptors[descriptor] = {
                        "identity": (status.st_dev, status.st_ino),
                        "close_calls": 0,
                    }
                    return descriptor

            for reference in handles:
                path = media_root / reference
                path.unlink()
                path.symlink_to(outside)

            fake_graphics = types.SimpleNamespace(
                Color=lambda red, green, blue: types.SimpleNamespace(
                    red=red, green=green, blue=blue,
                ),
                Font=object,
            )
            fake_rgbmatrix = types.SimpleNamespace(
                RGBMatrix=object, RGBMatrixOptions=object, graphics=fake_graphics,
            )
            previous_rgbmatrix = sys.modules.get("rgbmatrix")
            sys.modules["rgbmatrix"] = fake_rgbmatrix
            try:
                framework = importlib.import_module("matrix_framework")
            finally:
                if previous_rgbmatrix is None:
                    sys.modules.pop("rgbmatrix", None)
                else:
                    sys.modules["rgbmatrix"] = previous_rgbmatrix

            class RecordingMatrix(CaptureMatrix):
                def __init__(self):
                    super().__init__()
                    self.submissions = []

                def swap(self, canvas=None):
                    self.submissions.append(self.snapshot(canvas))
                    return super().swap(canvas)

            def production_matrix():
                matrix = RecordingMatrix()
                matrix.show_gif = types.MethodType(framework.Matrix.show_gif, matrix)
                matrix.media_frames = types.MethodType(framework.Matrix.media_frames, matrix)
                matrix._media_file = lambda _reference: (_ for _ in ()).throw(
                    AssertionError("consumer reopened a media pathname")
                )
                return matrix

            processes = []
            child_started = threading.Event()
            real_popen = subprocess.Popen
            real_close = os.close

            def blocking_decoder(_command, *args, **kwargs):
                process = real_popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=kwargs.get("stdin"), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                processes.append(process)
                child_started.set()
                return process

            def recording_close(descriptor):
                ownership = duplicate_descriptors.get(descriptor)
                if ownership is not None:
                    try:
                        status = os.fstat(descriptor)
                    except OSError:
                        status = None
                    if status is not None and (
                        status.st_dev, status.st_ino
                    ) == ownership["identity"]:
                        ownership["close_calls"] += 1
                return real_close(descriptor)

            cancel_stop = threading.Event()
            cancel_result = []
            cancel_errors = []

            def cancel_decode():
                try:
                    cancel_result.append(
                        production_matrix().media_frames(
                            "cancel.mp4", 1, 1, stop_event=cancel_stop,
                            decode_timeout=5,
                            pinned_handle=CountedHandle(handles["cancel.mp4"]),
                        )
                    )
                except BaseException as error:
                    cancel_errors.append(error)

            worker = None
            cancellation_elapsed = None
            try:
                completion_matrix = production_matrix()
                self.assertTrue(
                    completion_matrix.show_gif(
                        "complete.gif", loops=1,
                        pinned_handle=CountedHandle(handles["complete.gif"]),
                    )
                )
                self.assertEqual(len(completion_matrix.submissions), 2)
                self.assertEqual(
                    completion_matrix.submissions[0].getpixel((0, 0)), first_colour
                )

                with self.assertRaises(OSError) as decoder_error:
                    production_matrix().show_gif(
                        "error.gif", loops=1,
                        pinned_handle=CountedHandle(handles["error.gif"]),
                    )
                self.assertNotIn(str(temporary_root), str(decoder_error.exception))

                with (
                    patch.object(framework.subprocess, "Popen", side_effect=blocking_decoder),
                    patch.object(framework.os, "close", side_effect=recording_close),
                ):
                    worker = threading.Thread(
                        target=cancel_decode, name="pinned-media-cancel-decode"
                    )
                    worker.start()
                    self.assertTrue(child_started.wait(1), "decoder child did not start")
                    started = time.monotonic()
                    cancel_stop.set()
                    worker.join(0.25)
                    cancellation_elapsed = time.monotonic() - started
                    self.assertFalse(worker.is_alive(), "cancellation exceeded 250 ms")

                self.assertLessEqual(cancellation_elapsed, 0.25)
                self.assertEqual(cancel_result, [[]])
                self.assertEqual(cancel_errors, [])
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
                self.assertFalse(Path(f"/proc/{processes[0].pid}").exists())
            finally:
                cancel_stop.set()
                if worker is not None:
                    worker.join(1)
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                resolver.close()
                resolver.close()
                for handle in handles.values():
                    handle.close()
                    handle.close()

            self.assertEqual(len(duplicate_files), 2)
            self.assertEqual([item.close_calls for item in duplicate_files], [1, 1])
            self.assertEqual(
                [item.identity for item in duplicate_files],
                [original_identities["complete.gif"], original_identities["error.gif"]],
            )
            for item in duplicate_files:
                self.assertTrue(item._file.closed)
                with self.assertRaises(OSError):
                    os.fstat(item.descriptor)
            self.assertEqual(len(duplicate_descriptors), 1)
            self.assertEqual(
                [item["close_calls"] for item in duplicate_descriptors.values()], [1]
            )
            self.assertEqual(
                [item["identity"] for item in duplicate_descriptors.values()],
                [original_identities["cancel.mp4"]],
            )
            for descriptor in duplicate_descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

            self.assertEqual(
                original_close_counts,
                {"complete.gif": 1, "error.gif": 1, "cancel.mp4": 1},
            )
            for reference, descriptor in original_descriptors.items():
                self.assertTrue(handles[reference].closed)
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertEqual(outside.read_bytes(), outside_bytes)
            descriptors_after = set(os.listdir("/proc/self/fd"))
            fd_delta = descriptors_after.symmetric_difference(descriptors_before)
            thread_delta = {
                thread.ident for thread in threading.enumerate()
            } - threads_before
            self.assertEqual(descriptors_after, descriptors_before)
            self.assertEqual(thread_delta, set())
            self.lifecycle_evidence = {
                "cancellation_ms": round(cancellation_elapsed * 1000, 3),
                "original_close_counts": dict(original_close_counts),
                "duplicate_file_close_counts": [
                    item.close_calls for item in duplicate_files
                ],
                "duplicate_fd_close_counts": [
                    item["close_calls"] for item in duplicate_descriptors.values()
                ],
                "children_reaped": all(process.poll() is not None for process in processes),
                "fd_delta": len(fd_delta),
                "thread_delta": len(thread_delta),
                "outside_bytes_unchanged": outside.read_bytes() == outside_bytes,
            }


if __name__ == "__main__":
    unittest.main()
