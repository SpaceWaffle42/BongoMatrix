import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import media_sanitizer
from media_sanitizer import sanitize


class MediaSanitizerIsolationTests(unittest.TestCase):
    def test_sanitized_output_is_fsynced_before_success(self):
        source = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(source, format="PNG")
        source.seek(0)
        with tempfile.TemporaryFile() as destination, patch(
            "media_sanitizer.os.fsync"
        ) as fsync:
            sanitize(source, destination, "safe.png")

        fsync.assert_called_once()

    def test_resource_limits_apply_even_when_helper_starts_unprivileged(self):
        with (
            patch.object(media_sanitizer.os, "geteuid", return_value=65534),
            patch.object(media_sanitizer.resource, "setrlimit") as setrlimit,
            patch.object(media_sanitizer.pwd, "getpwnam") as getpwnam,
        ):
            media_sanitizer.drop_privileges()

        self.assertEqual(setrlimit.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in setrlimit.call_args_list},
            {media_sanitizer.resource.RLIMIT_CPU, media_sanitizer.resource.RLIMIT_AS},
        )
        getpwnam.assert_not_called()

    def test_resource_limits_never_raise_stricter_inherited_limits(self):
        cpu = media_sanitizer.resource.RLIMIT_CPU
        address_space = media_sanitizer.resource.RLIMIT_AS
        inherited = {
            cpu: (10, 10),
            address_space: (512 * 1024 * 1024, 512 * 1024 * 1024),
        }
        with (
            patch.object(media_sanitizer.os, "geteuid", return_value=65534),
            patch.object(
                media_sanitizer.resource, "getrlimit",
                side_effect=lambda resource_type: inherited[resource_type],
            ),
            patch.object(media_sanitizer.resource, "setrlimit") as setrlimit,
        ):
            media_sanitizer.drop_privileges()

        self.assertEqual(
            [call.args for call in setrlimit.call_args_list],
            [(cpu, inherited[cpu]), (address_space, inherited[address_space])],
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class Mp4SanitizerTests(unittest.TestCase):
    def test_mp4_policy_rejects_excessive_duration_rate_and_dimensions(self):
        base_format = {"format_name": "mov,mp4", "duration": "1"}
        cases = (
            ({"width": 64, "height": 32, "duration": "301", "avg_frame_rate": "20/1"}, "duration"),
            ({"width": 64, "height": 32, "duration": "1", "avg_frame_rate": "121/1"}, "frame rate"),
            ({"width": 10000, "height": 5000, "duration": "1", "avg_frame_rate": "20/1"}, "dimensions"),
        )
        for video, message in cases:
            with self.subTest(message=message), tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as destination:
                with patch.object(media_sanitizer, "_probe_mp4", return_value=({"format": base_format}, video)):
                    with self.assertRaisesRegex(ValueError, message):
                        media_sanitizer._sanitize_mp4(source, destination)

    def test_mp4_is_validated_and_transcoded_for_the_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.mp4"
            output_path = Path(directory) / "optimized.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=128x72:rate=25:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-y", str(source_path),
                ],
                check=True,
            )

            with source_path.open("rb") as source, output_path.open("w+b") as destination:
                sanitize(source, destination, "clip.mp4")

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_streams", "-show_format",
                    "-of", "json", str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = json.loads(probe.stdout)
            video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (56, 32))
            self.assertEqual(video["avg_frame_rate"], "20/1")
            self.assertFalse(any(stream["codec_type"] == "audio" for stream in metadata["streams"]))
            self.assertIn("mp4", metadata["format"]["format_name"])

    def test_mp4_sanitizer_preserves_portrait_content_aspect(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "portrait-source.mp4"
            output_path = Path(directory) / "portrait-output.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=32x64:d=0.2:r=10",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-y", str(source_path),
                ],
                check=True,
            )

            with source_path.open("rb") as source, output_path.open("w+b") as destination:
                sanitize(source, destination, "portrait.mp4")

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "json", str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            video = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((video["width"], video["height"]), (16, 32))


if __name__ == "__main__":
    unittest.main()
