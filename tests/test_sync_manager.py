"""Tests for SyncManager — dedup, loop prevention, throttle, enable/disable."""

import sys
import os
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.sync.manager import SyncManager, SYNC_DEBOUNCE, DEDUP_RING_SIZE


class MockClipboardMonitor:
    def __init__(self):
        self._callback = None
        self._running = False
        self.suppress_until = 0.0

    def start(self, callback):
        self._callback = callback
        self._running = True

    def stop(self):
        self._running = False
        self._callback = None

    def suppress_for(self, duration_seconds):
        """Mirror the real monitor: drop callbacks until the window passes."""
        self.suppress_until = time.time() + duration_seconds

    def fire(self):
        if self._callback and time.time() >= self.suppress_until:
            self._callback()


class MockClipboardReader:
    def __init__(self):
        self.content = ClipboardContent()

    def read(self) -> ClipboardContent:
        return self.content


class MockClipboardWriter:
    def __init__(self):
        self.last_written: ClipboardContent | None = None
        self.write_count = 0

    def write(self, content: ClipboardContent):
        self.last_written = content
        self.write_count += 1


class TestSyncManager:
    def setup_method(self):
        self.monitor = MockClipboardMonitor()
        self.reader = MockClipboardReader()
        self.writer = MockClipboardWriter()
        self.sent: list[SyncMessage] = []
        # Use dependency injection — bypasses platform-specific factories
        self.mgr = SyncManager(
            "test-device", "Test Device",
            reader=self.reader,
            writer=self.writer,
            monitor=self.monitor,
        )
        self.mgr.on_send = lambda msg: self.sent.append(msg)

    def teardown_method(self):
        self.mgr.stop()

    def test_local_change_broadcasts(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"hello"},
        )
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.5)  # exceed SYNC_DEBOUNCE for timer to fire

        assert len(self.sent) == 1
        assert self.sent[0].source_device == "test-device"
        assert self.sent[0].content.types[ContentType.TEXT] == b"hello"

    def test_dedup_identical_content(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"dup"},
        )
        self.mgr.start()
        self.monitor.fire()
        self.monitor.fire()  # second fire cancels timer, restarts — coalesced
        time.sleep(0.5)  # exceed SYNC_DEBOUNCE

        assert len(self.sent) == 1

    def test_different_content_sends_both(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"first"},
        )
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.5)  # exceed SYNC_DEBOUNCE for first timer

        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"second"},
        )
        self.monitor.fire()
        time.sleep(0.5)  # exceed SYNC_DEBOUNCE for second timer

        assert len(self.sent) == 2

    def test_throttle_rapid_changes(self):
        self.mgr.start()

        # Fire 5 changes as fast as possible — coalescing timer keeps resetting
        for i in range(5):
            self.reader.content = ClipboardContent(
                types={ContentType.TEXT: f"rapid {i}".encode()},
            )
            self.monitor.fire()

        time.sleep(0.5)  # exceed SYNC_DEBOUNCE to let final timer fire
        # Rapid changes coalesce into a single send (only the last one)
        assert len(self.sent) == 1, f"Expected coalescing to 1, got {len(self.sent)}"
        assert self.sent[0].content.types[ContentType.TEXT] == b"rapid 4"

    def test_disabled_does_not_broadcast(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"should not send"},
        )
        self.mgr.set_enabled(False)
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.1)

        assert len(self.sent) == 0

    def test_disabled_does_not_receive(self):
        self.mgr.set_enabled(False)
        self.mgr.start()

        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"remote"}),
            msg_id="remote-1",
            source_device="peer",
        )
        self.mgr.handle_remote_message(msg)
        assert self.writer.write_count == 0

    def test_remote_message_writes_locally(self):
        msg = SyncMessage(
            content=ClipboardContent(
                types={ContentType.TEXT: b"from peer", ContentType.HTML: b"<p>peer</p>"},
            ),
            msg_id="r1",
            source_device="peer-device",
        )
        self.mgr.start()
        self.mgr.handle_remote_message(msg)

        assert self.writer.write_count == 1
        assert self.writer.last_written is not None
        assert self.writer.last_written.types[ContentType.TEXT] == b"from peer"
        assert self.writer.last_written.types[ContentType.HTML] == b"<p>peer</p>"

    def test_loop_prevention(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"loop-test"},
        )
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.5)  # exceed SYNC_DEBOUNCE

        assert len(self.sent) == 1
        write_count_before = self.writer.write_count

        # Simulate the remote reflecting this back
        reflected = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"loop-test"}),
            msg_id="reflected",
            source_device="peer",
        )
        self.mgr.handle_remote_message(reflected)
        assert self.writer.write_count == write_count_before, "Should not write reflected content"

    def test_empty_content_ignored(self):
        self.reader.content = ClipboardContent()  # empty
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.1)
        assert len(self.sent) == 0

    def test_dedup_ring_limits(self):
        self.mgr.start()
        # Send many different clips
        for i in range(DEDUP_RING_SIZE + 10):
            self.reader.content = ClipboardContent(
                types={ContentType.TEXT: f"clip-{i}".encode()},
            )
            self.monitor.fire()
            time.sleep(0.5)  # exceed SYNC_DEBOUNCE with margin for timer jitter

        # All should have been sent (each is different)
        assert len(self.sent) == DEDUP_RING_SIZE + 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
