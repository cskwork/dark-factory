"""Advisory whole-file locks on POSIX (fcntl.flock) and Windows (msvcrt.locking)."""
import os
import sys
import time

if sys.platform == "win32":
    import ctypes
    import msvcrt
else:
    import fcntl

POLL_SECONDS = 0.05


def acquire(stream, *, blocking=True):
    """Lock an open file; raise BlockingIOError when blocking=False and another holder has it."""
    if sys.platform != "win32":
        fcntl.flock(stream, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    # msvcrt locks a byte range from the current position; byte 0 stands for the whole file.
    # LK_LOCK gives up after ten one-second retries, so a blocking wait polls LK_NBLCK instead.
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if not blocking:
                raise BlockingIOError(*error.args) from error
        time.sleep(POLL_SECONDS)


def release(stream):
    if sys.platform != "win32":
        fcntl.flock(stream, fcntl.LOCK_UN)
        return
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def pid_alive(pid):
    """True while a process with this id exists; never signals it (os.kill(pid, 0) terminates on Windows)."""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return False
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, owned by someone else
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True
