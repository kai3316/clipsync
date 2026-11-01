"""Whether a process id is running, and whether it is one of ours.

The data-directory lock reads a marker file the legacy application leaves
behind, and it has to tell two situations apart: a legacy ClipSync that is
still running, which owns the directory, and the marker a crashed, killed or
rebooted one left behind, which must not lock every later launch out of the
directory forever.

A bare "is something alive at this pid" does not answer that on either
platform, because pids are reused.  On its own it reads a recycled pid as a
running ClipSync that will never exit, which is indistinguishable from the
bug it is meant to fix -- so where the image name can be read, it is.
"""

import os
import sys

# Both shapes of this application: a frozen PyInstaller build runs as
# clipsync.exe (Windows) or clipsync (macOS, Linux), the source as python
# followed by main.py.
_MARKERS = ("python", "clipsync")


def _matches(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _MARKERS)


def pid_running(pid: int) -> bool:
    """Whether `pid` is a live ClipSync process.

    Anything that cannot be checked at all is reported as running.  The caller
    uses this to decide whether to refuse the data directory, and a refusal the
    user can clear by closing an application is recoverable, where seizing a
    directory out from under a live instance is not.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_running(pid)
    return _posix_pid_running(pid)


def _windows_pid_running(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION rather than the wider mask the
        # legacy single-instance check uses: it is all this needs, and it is
        # granted for more processes.
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # The marker sits in this user's own config directory, so whatever
            # wrote it ran as this user and would have opened for this user.
            # A refusal here means the process is gone, not that it is private.
            return False
        try:
            # Ask whether it has actually exited before asking what it is.  A
            # process that has terminated but still has an open handle -- the
            # shell that launched a killed instance, say -- still opens and
            # still holds its pid, but its image name is no longer readable,
            # so the name check alone would fail safe into "running" and refuse
            # the directory to an instance that is plainly gone.
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            if code.value != 259:  # STILL_ACTIVE
                return False
            size = wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return True  # alive, but unreadable: err on the safe side
            return _matches(os.path.basename(buffer.value))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def _posix_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's process, but it exists
    except OSError:
        return True
    return _posix_name_matches(pid)


def _posix_name_matches(pid: int) -> bool:
    """Confirm the live pid is still ClipSync and not a process that took it.

    Linux answers from /proc.  macOS has no /proc, so it asks `ps` for the
    command name; any failure there reports a match, since the alternative is
    to reclaim a directory that may still be in use.
    """
    try:
        if sys.platform == "linux":
            with open(f"/proc/{pid}/cmdline", "rb") as stream:
                return _matches(stream.read().decode("utf-8", "replace"))
        if sys.platform == "darwin":
            import subprocess

            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            name = (result.stdout or "").strip()
            return True if not name else _matches(name)
    except Exception:
        return True
    return True
