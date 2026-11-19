import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Runs in a child: no fcntl, sys.platform "win32", and a fake msvcrt that keeps one lock table.
WINDOWS = r"""
import sys, types
import ctypes, http.server, subprocess, tempfile  # stdlib picks its platform code before the switch
sys.modules["fcntl"] = None
sys.platform = "win32"
held = {}
msvcrt = types.ModuleType("msvcrt")
msvcrt.LK_NBLCK, msvcrt.LK_UNLCK = 2, 0
def locking(fd, mode, size):
    import os
    key = os.fstat(fd).st_ino
    if mode == msvcrt.LK_UNLCK:
        held.pop(key, None)
    elif key in held and held[key] != fd:
        raise PermissionError(13, "Permission denied")
    else:
        held[key] = fd
msvcrt.locking = locking
sys.modules["msvcrt"] = msvcrt
import factory.runtime, factory.decision_inbox, factory.judge, factory.decision_page, factory.board_feed
from factory import locking
path = sys.argv[1]
with open(path, "a") as first, open(path, "a") as second:
    locking.acquire(first)
    try:
        locking.acquire(second, blocking=False)
        raise SystemExit("second holder got the lock")
    except BlockingIOError:
        pass
    locking.release(first)
    locking.acquire(second, blocking=False)
print("ok")
"""


class WindowsLockTest(unittest.TestCase):
    def test_runtime_imports_and_locks_without_fcntl(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", WINDOWS, str(Path(directory) / ".lock")], cwd=ROOT,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr)


if __name__ == "__main__":
    unittest.main()
