"""Process-lifecycle primitives that POSIX shorthand cannot express on Windows.

Two things sub-agy needs from the OS are platform-specific:

* *Is this pid still running?*  ``os.kill(pid, 0)`` is the usual probe, but on
  Windows it returns without raising even when the process is gone, so a dead
  supervisor would look alive forever and never release its run slot.
* *Stop this job.*  ``os.killpg``/``os.getpgid`` do not exist on Windows and
  ``signal.SIGKILL`` is not defined there either.

Call sites go through this module instead of touching those directly, so the
platform branch lives in exactly one place.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import IO

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _ERROR_ACCESS_DENIED = 5
    _STILL_ACTIVE = 259  # GetExitCodeProcess sentinel for a live process

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE is 64-bit on this platform; without these the return value would be
    # truncated to a C int and every OpenProcess would look like a failure.
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def pid_alive(pid: int) -> bool:
    """Report whether ``pid`` names a process that has not exited."""
    if os.name == "nt":
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # A protected process we may not open still counts as alive.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            # A process that exited *with* code 259 is misread as alive; that
            # exit code is not one agy or a supervisor ever returns.
            return exit_code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def terminate_tree(pid: int) -> bool:
    """Ask ``pid`` and its children to stop; True if the signal was delivered."""
    return _kill_tree(pid, force=False)


def kill_tree(pid: int) -> bool:
    """Forcibly stop ``pid`` and its children; True if a kill was attempted."""
    return _kill_tree(pid, force=True)


def _kill_tree(pid: int, force: bool) -> bool:
    if os.name == "nt":
        # Windows cannot ask a headless console process to exit politely:
        # ``taskkill`` without /F posts WM_CLOSE, which such a process never
        # sees, so both paths force-kill the tree.  Dropping the supervisor's
        # own cleanup is survivable -- reconcile_state() rebuilds the job from
        # result.json, and /T matters because an orphaned agy would keep
        # consuming quota on a job already reported as cancelled.
        argv = ["taskkill", "/PID", str(pid), "/T", "/F"]
        try:
            result = subprocess.run(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        # Each supervisor is a session leader (start_new_session=True), so the
        # group is exactly the supervisor plus the agy round it hosts.
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def spawn_detached(argv: list[str], stderr_fh: IO[str], cwd: str) -> subprocess.Popen:
    """Spawn a supervisor that keeps running after the caller exits.

    POSIX gets a new session via ``start_new_session``.  Windows needs
    ``DETACHED_PROCESS`` -- ``start_new_session`` there only maps to
    CREATE_NEW_PROCESS_GROUP, which leaves the child attached to the caller's
    console, so closing the terminal would take the supervisor down with it.
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        kwargs["stdin"] = subprocess.DEVNULL  # no console to inherit it from
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        cwd=cwd,
        **kwargs,
    )
