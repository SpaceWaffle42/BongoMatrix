import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from secret_store import SecretDurabilityError, SecretStore


class SecretStoreTests(unittest.TestCase):
    def test_malformed_owner_only_secret_is_reported_as_storage_integrity_failure(self):
        for payload in (b"\xff\xfe", b"short"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "secrets" / "openweather-api-key"
                path.parent.mkdir(mode=0o700)
                path.write_bytes(payload)
                path.chmod(0o600)
                store = SecretStore(path)

                with self.assertRaisesRegex(RuntimeError, "Stored API key is invalid"):
                    store.get()

    def test_openweather_key_round_trips_in_owner_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "openweather-api-key"
            store = SecretStore(path)

            self.assertFalse(store.configured())
            store.set("test_key_1234567890")

            self.assertTrue(store.configured())
            self.assertEqual(store.get(), "test_key_1234567890")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("test_key_1234567890", str(store.status()))

    def test_openweather_key_read_rejects_non_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "openweather-api-key"
            store = SecretStore(path)
            store.set("test_key_1234567890")
            path.chmod(0o644)

            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                store.get()

    def test_openweather_key_can_be_removed_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "openweather-api-key"
            store = SecretStore(path)
            store.set("test_key_1234567890")

            store.clear()

            self.assertFalse(path.exists())
            self.assertFalse(store.configured())

    def test_openweather_key_rejects_a_symlinked_data_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (root / "data").symlink_to(outside, target_is_directory=True)
            store = SecretStore(root / "data" / "secrets" / "openweather-api-key")

            with self.assertRaisesRegex(RuntimeError, "unsafe secret root"):
                store.set("test_key_1234567890")

            self.assertFalse((outside / "secrets" / "openweather-api-key").exists())

    def test_set_reports_committed_state_when_directory_fsync_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "openweather-api-key"
            store = SecretStore(path)
            store.set("test_key_1234567890")

            with patch("secret_store.os.fsync", side_effect=[None, OSError("fsync failed")]):
                with self.assertRaises(SecretDurabilityError) as raised:
                    store.set("replacement_key_1234567890")

            self.assertTrue(raised.exception.configured)
            self.assertEqual(store.get(), "replacement_key_1234567890")

    def test_clear_reports_committed_state_when_directory_fsync_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "openweather-api-key"
            store = SecretStore(path)
            store.set("test_key_1234567890")

            with patch("secret_store.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaises(SecretDurabilityError) as raised:
                    store.clear()

            self.assertFalse(raised.exception.configured)
            self.assertIsNone(store.get())


if __name__ == "__main__":
    unittest.main()
