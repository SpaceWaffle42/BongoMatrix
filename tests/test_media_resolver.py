import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
