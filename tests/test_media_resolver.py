import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
