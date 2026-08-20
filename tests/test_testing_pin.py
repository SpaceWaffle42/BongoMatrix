import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from app.testing_pin import load_testing_pin


def load_fifo_in_child(path, results):
    results.put(load_testing_pin(path, "0.3.51-preview"))


class TestingPinLoaderTests(unittest.TestCase):
    TESTING_PIN = "731946"

    def test_loads_exact_secure_pin_before_version_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testing-pin"
            path.write_text(f"{self.TESTING_PIN}\n")
            path.chmod(0o600)

            self.assertEqual(load_testing_pin(path, "0.3.51-preview"), self.TESTING_PIN)

    def test_rejects_missing_malformed_and_insecure_files(self):
        malformed_values = (
            "",
            "12345",
            "1234567",
            "abcdef",
            "123 56",
            f"{self.TESTING_PIN}\r\n",
            f"{self.TESTING_PIN}\n\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testing-pin"
            self.assertIsNone(load_testing_pin(path, "0.3.51-preview"))
            for value in malformed_values:
                with self.subTest(value=repr(value)):
                    path.write_text(value)
                    path.chmod(0o600)
                    self.assertIsNone(load_testing_pin(path, "0.3.51-preview"))
            for mode in (0o400, 0o604, 0o620, 0o640, 0o606):
                with self.subTest(mode=oct(mode)):
                    path.write_text(self.TESTING_PIN)
                    path.chmod(mode)
                    self.assertIsNone(load_testing_pin(path, "0.3.51-preview"))

    def test_rejects_file_not_owned_by_expected_service_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testing-pin"
            path.write_text(self.TESTING_PIN)
            path.chmod(0o600)

            self.assertIsNone(
                load_testing_pin(
                    path,
                    "0.3.51-preview",
                    expected_uid=os.geteuid() + 1,
                )
            )

    def test_rejects_symlinks_hardlinks_and_non_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text(self.TESTING_PIN)
            target.chmod(0o600)

            symlink = root / "testing-pin-symlink"
            symlink.symlink_to(target)
            self.assertIsNone(load_testing_pin(symlink, "0.3.51-preview"))

            hardlink = root / "testing-pin-hardlink"
            hardlink.hardlink_to(target)
            self.assertIsNone(load_testing_pin(hardlink, "0.3.51-preview"))

            non_regular = root / "testing-pin-directory"
            non_regular.mkdir()
            self.assertIsNone(load_testing_pin(non_regular, "0.3.51-preview"))

    def test_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testing-pin"
            os.mkfifo(path, 0o600)
            results = multiprocessing.Queue()
            process = multiprocessing.Process(target=load_fifo_in_child, args=(path, results))
            process.start()
            process.join(0.5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            self.assertFalse(process.is_alive(), "loader blocked while opening a FIFO")
            self.assertEqual(process.exitcode, 0)
            self.assertIsNone(results.get(timeout=1))

    def test_refuses_pin_at_version_one_and_for_unparseable_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testing-pin"
            path.write_text(self.TESTING_PIN)
            path.chmod(0o600)

            for version in ("1.0.0", "1.0.0-preview", "1.2.3", "2.0.0", "invalid"):
                with self.subTest(version=version):
                    self.assertIsNone(load_testing_pin(path, version))


if __name__ == "__main__":
    unittest.main()
