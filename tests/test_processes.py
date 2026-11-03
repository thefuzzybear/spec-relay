import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from ub_execute.processes import held_lock, is_locked


class ProcessLockTest(unittest.TestCase):
    def test_lock_visibility_and_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "worker.lock"
            self.assertFalse(is_locked(path))
            with held_lock(path):
                self.assertTrue(is_locked(path))
                code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            self.assertFalse(is_locked(path))
            result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
