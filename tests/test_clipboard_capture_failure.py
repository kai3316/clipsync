"""A clipboard monitor that came up broken must say so, not look healthy.

Every adapter is asked to start and returns immediately; on Windows the
window, the message pump and the clipboard listener are built on a thread of
their own, so a refusal there happened *after* the caller was told the monitor
had started.  The old code logged it and carried on, which left a runtime that
reported itself running, answered every status read as healthy, and captured
nothing -- the app looked fine and simply never synced a local copy, and the
only cure anyone found was quitting and reopening it.

Pinned here: the refusal is recorded where the app can read it, a transient
one is retried before it is believed, and restarting the monitor is possible
without switching sync off.
"""

from __future__ import annotations

import sys

import pytest

from internal.clipboard.clipboard import ClipboardMonitor
from internal.clipboard.format import ClipboardContent
from internal.sync.manager import SyncManager


class BrokenMonitor(ClipboardMonitor):
    """A monitor whose startup was refused, whatever the platform reason."""

    def __init__(self, error="AddClipboardFormatListener refused"):
        self.last_error = error
        self.started = 0
        self.stopped = 0

    def start(self, callback):
        self.started += 1
        self.callback = callback

    def stop(self):
        self.stopped += 1


class HealthyMonitor(BrokenMonitor):
    def __init__(self):
        super().__init__(error=None)


class BareDouble:
    """A stand-in with no health interface at all (older test doubles)."""

    max_poll_interval = 0.0

    def start(self, callback):
        pass

    def stop(self):
        pass


class Reader:
    def read(self):
        return ClipboardContent()


class Writer:
    def write(self, content):
        return True


def make_manager(monitor):
    return SyncManager(
        "device", "name", reader=Reader(), writer=Writer(), monitor=monitor
    )


def test_a_healthy_monitor_reports_no_capture_error():
    assert make_manager(HealthyMonitor()).clipboard_capture_error() == ""


def test_a_broken_monitor_is_named_rather_than_swallowed():
    manager = make_manager(BrokenMonitor("CreateWindowExW failed"))
    assert manager.clipboard_capture_error() == "CreateWindowExW failed"


def test_a_monitor_without_the_interface_is_not_a_fault():
    # Several test doubles predate the interface; guessing at attributes would
    # turn every one of them into a reported fault.
    assert make_manager(BareDouble()).clipboard_capture_error() == ""


def test_restarting_the_monitor_leaves_sync_state_alone():
    monitor = BrokenMonitor()
    manager = make_manager(monitor)
    manager.start()
    enabled = manager._enabled
    manager.restart_monitor()
    # Sync stays where it was: this is a repair, not a pause, and `stop()`
    # would have switched it off and left it that way.
    assert manager._enabled == enabled
    assert monitor.stopped == 1
    assert monitor.started == 2
    # Re-registered with the same callback, or the restart captures nothing.
    assert monitor.callback == manager._on_clipboard_change


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard monitor")
def test_a_refused_listener_is_retried_before_it_is_believed(monkeypatch):
    from internal.clipboard import clipboard_windows as cw

    monitor = cw.WindowsClipboardMonitor()
    monitor._hwnd = 1
    monkeypatch.setattr(cw, "_LISTENER_RETRY_DELAY", 0.0)
    calls = []

    def refused(hwnd):
        calls.append(hwnd)
        return 0

    monkeypatch.setattr(cw.user32, "AddClipboardFormatListener", refused)
    assert monitor._register_listener() is False
    assert len(calls) == cw._LISTENER_ATTEMPTS
    assert monitor.monitor_error() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard monitor")
def test_a_listener_that_succeeds_on_retry_is_not_a_fault(monkeypatch):
    from internal.clipboard import clipboard_windows as cw

    monitor = cw.WindowsClipboardMonitor()
    monitor._hwnd = 1
    monkeypatch.setattr(cw, "_LISTENER_RETRY_DELAY", 0.0)
    results = iter([0, 1])
    monkeypatch.setattr(
        cw.user32, "AddClipboardFormatListener", lambda hwnd: next(results)
    )
    assert monitor._register_listener() is True
    assert monitor.monitor_error() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard monitor")
def test_starting_clears_a_failure_from_a_previous_run(monkeypatch):
    from internal.clipboard import clipboard_windows as cw

    monitor = cw.WindowsClipboardMonitor()
    monitor._failure = "stale"
    monkeypatch.setattr(monitor, "_message_loop", lambda: None)
    # The thread is replaced by a no-op above, so this only has to prove the
    # flag is reset: a restart that kept the old failure would never be able
    # to report itself healthy again.
    monitor.start(lambda: None)
    assert monitor._failure is None
