import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hardware_lock import DisplayOwnershipLock


class DisplayOwnershipLockTests(unittest.TestCase):
    def test_second_process_cannot_claim_display_until_owner_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "matrix-display.lock"
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


if __name__ == "__main__":
    unittest.main()
