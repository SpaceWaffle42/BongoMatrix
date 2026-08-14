import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import media_resolver
from media_resolver import MediaResolutionError, MediaResolver


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


if __name__ == "__main__":
    unittest.main()
