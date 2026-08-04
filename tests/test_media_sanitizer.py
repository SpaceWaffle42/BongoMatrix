import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_sanitizer
from media_sanitizer import sanitize


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
            self.assertEqual((video["width"], video["height"]), (64, 32))
            self.assertEqual(video["avg_frame_rate"], "20/1")
            self.assertFalse(any(stream["codec_type"] == "audio" for stream in metadata["streams"]))
            self.assertIn("mp4", metadata["format"]["format_name"])


if __name__ == "__main__":
    unittest.main()
