import os

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hardware_lock import DisplayOwnershipLock


class DisplayOwnershipLockTests(unittest.TestCase):
    def _trusted_lock(self, root):
        parent = Path(root) / "matrix-display"
        parent.mkdir(mode=0o755)
        os.chmod(parent, 0o755)
        lock_path = parent / "matrix-display.lock"
        lock_path.touch(mode=0o644)
        os.chmod(lock_path, 0o644)
        return parent, lock_path

    def test_second_process_cannot_claim_display_until_owner_releases(self):
        with tempfile.TemporaryDirectory(dir="/root") as directory:
            _parent, lock_path = self._trusted_lock(directory)
            owner = DisplayOwnershipLock(lock_path)
            owner.acquire()
            probe = (
                "from hardware_lock import DisplayOwnershipLock; "
                f"lock=DisplayOwnershipLock({str(lock_path)!r}); "
                "lock.acquire()"
            )
            blocked = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("already owned", blocked.stderr)
            owner.release()
            acquired = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True
            )
            self.assertEqual(acquired.returncode, 0, acquired.stderr)

    def test_rejects_untrusted_lock_nodes_without_mutating_targets(self):
        with tempfile.TemporaryDirectory(dir="/root") as directory:
            parent, lock_path = self._trusted_lock(directory)
            target = parent / "target"
            target.write_text("protected\n", encoding="ascii")
            os.chmod(target, 0o644)
            for attack in ("symlink", "hardlink", "fifo", "wrong-mode", "wrong-owner"):
                with self.subTest(attack=attack):
                    target.write_text("protected\n", encoding="ascii")
                    lock_path.unlink(missing_ok=True)
                    if attack == "symlink":
                        lock_path.symlink_to(target.name)
                    elif attack == "hardlink":
                        os.link(target, lock_path)
                    elif attack == "fifo":
                        os.mkfifo(lock_path, 0o644)
                    elif attack == "wrong-mode":
                        lock_path.touch(mode=0o600)
                        os.chmod(lock_path, 0o600)
                    else:
                        lock_path.touch(mode=0o644)
                        os.chown(lock_path, 65534, 65534)
                    with self.assertRaisesRegex(RuntimeError, "Unsafe display lock"):
                        DisplayOwnershipLock(lock_path).acquire()
                    self.assertEqual(target.read_text(encoding="ascii"), "protected\n")

    def test_rejects_parent_replacement_before_mutating_canonical_inode(self):
        with tempfile.TemporaryDirectory(dir="/root") as directory:
            parent, lock_path = self._trusted_lock(directory)
            original_inode = lock_path.stat().st_ino
            lock = DisplayOwnershipLock(lock_path)
            original_flock = __import__("hardware_lock").fcntl.flock

            def replace_parent(fd, operation):
                if operation & __import__("fcntl").LOCK_EX:
                    moved = parent.with_name("matrix-display-old")
                    parent.rename(moved)
                    replacement, replacement_lock = self._trusted_lock(directory)
                    self.assertNotEqual(replacement_lock.stat().st_ino, original_inode)
                return original_flock(fd, operation)

            __import__("hardware_lock").fcntl.flock = replace_parent
            try:
                with self.assertRaisesRegex(RuntimeError, "Unsafe display lock"):
                    lock.acquire()
            finally:
                __import__("hardware_lock").fcntl.flock = original_flock
            old_lock = Path(directory) / "matrix-display-old" / "matrix-display.lock"
            self.assertEqual(old_lock.stat().st_ino, original_inode)
            self.assertEqual(old_lock.read_bytes(), b"")

    def test_rejects_symlinked_or_writable_parent_ancestry(self):
        with tempfile.TemporaryDirectory(dir="/root") as directory:
            parent, lock_path = self._trusted_lock(directory)
            os.chmod(parent, 0o777)
            with self.assertRaisesRegex(RuntimeError, "Unsafe display lock ancestry"):
                DisplayOwnershipLock(lock_path).acquire()
            os.chmod(parent, 0o755)
            real_parent = parent.with_name("matrix-display-real")
            parent.rename(real_parent)
            parent.symlink_to(real_parent.name, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "Unsafe display lock ancestry"):
                DisplayOwnershipLock(lock_path).acquire()


if __name__ == "__main__":
    unittest.main()
