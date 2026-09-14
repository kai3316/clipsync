"""The liveness check the data-directory lock uses to tell a running legacy
instance from the marker a dead one left behind.

Three tests, one per branch that matters: this process answers for itself, a
pid that a different process now owns must not read as ClipSync (or a stale
marker locks the directory out), and a process that has exited but is still
held open by a shell handle must not either.
"""

import os
import subprocess
import sys
import time

import pytest

from internal.platform.process import pid_running


def test_this_process_is_running():
    assert pid_running(os.getpid())


def test_a_pid_held_by_something_else_is_not_running():
    """A recycled pid must not read as ClipSync, or a stale marker blocks the
    directory until the unrelated process happens to exit."""
    if sys.platform == "win32":
        pytest.skip("no fork on Windows")
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        os.execv("/bin/sleep", ["sleep", "30"])
    try:
        assert pid_running(pid) is False
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


def test_a_live_process_that_is_not_clipsync_is_not_running():
    """Windows, where the rejected pid belongs to whatever the user last ran
    rather than to a fork we control."""
    if sys.platform != "win32":
        pytest.skip("Windows-only process spawn")
    process = subprocess.Popen(["cmd", "/c", "ping -n 20 127.0.0.1 >nul"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is not None:
            time.sleep(0.05)
        assert pid_running(process.pid) is False
    finally:
        process.kill()
        process.wait()
    # Killed, but the Popen handle still holds the process object open, so it
    # still answers OpenProcess and still owns its pid.  Only the exit code
    # says it is gone -- the image-name query cannot, which is the trap this
    # check was written around.
    assert pid_running(process.pid) is False
