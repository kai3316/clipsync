"""Receive-side regressions: a clip whose clipboard write fails is not lost.

The review's 中-3: when the receiving machine's clipboard was momentarily held
(Office, a clipboard manager), `handle_remote_message` returned False *before*
writing history, so the peer's clip was absent from history, unacked, and —
with the old delivery ledger — eventually marked 未送达 and never retried.

Pinned here: the row is still recorded, the outcome is retryable so the sender
keeps the frame, the redelivery applies the clipboard without a duplicate
history row, and a crossed local copy keeps the local clipboard while still
recording the remote clip.
"""

from __future__ import annotations

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.sync.manager import SyncManager


class FakeMonitor:
    max_poll_interval = 0.0

    def __init__(self):
        self.suppressed = 0.0

    def suppress_for(self, seconds):
        self.suppressed = seconds


class FakeReader:
    def read(self):
        return ClipboardContent()


class FakeWriter:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def write(self, content):
        self.calls += 1
        return self.ok


class FakeHistory:
    def __init__(self):
        self.rows = []

    def add(self, content, source_app=None):
        self.rows.append(content)
        return f"h{len(self.rows)}"


def manager(writer_ok=True):
    writer = FakeWriter(writer_ok)
    history = FakeHistory()
    manager = SyncManager(
        "local",
        "Local",
        reader=FakeReader(),
        writer=writer,
        monitor=FakeMonitor(),
        history=history,
        sync_debounce=0.5,
        retry_enabled=False,
    )
    errors = []
    manager.set_on_write_error(lambda: errors.append(1))
    return manager, writer, history, errors


def remote_msg(text="hello", msg_id="m1"):
    return SyncMessage(
        ClipboardContent({ContentType.TEXT: text.encode()}), msg_id, "peer"
    )


def test_a_failed_clipboard_write_still_records_the_row():
    sync, writer, history, errors = manager(writer_ok=False)

    outcome = sync.handle_remote_message_ex(remote_msg())

    assert outcome["accepted"] is False
    assert outcome["retryable"] is True
    assert outcome["history"] is True
    assert outcome["clipboard"] is False
    assert len(history.rows) == 1
    assert errors == [1]
    # The old bool contract is the ack signal: no receipt, so the sender keeps
    # the frame (the delivery ledger requeues an unacked send).
    assert sync.handle_remote_message(remote_msg("second", "m2")) is False


def test_the_redelivery_applies_the_clipboard_without_a_second_history_row():
    sync, writer, history, _errors = manager(writer_ok=False)
    first = sync.handle_remote_message_ex(remote_msg())
    assert first["retryable"] is True

    writer.ok = True
    second = sync.handle_remote_message_ex(remote_msg())

    assert second["accepted"] is True
    assert second["clipboard"] is True
    assert second["history"] is True
    assert len(history.rows) == 1  # the row written by the failed attempt
    assert writer.calls == 2


def test_a_crossed_local_copy_keeps_the_local_clipboard_but_keeps_the_row():
    sync, writer, history, _errors = manager(writer_ok=True)
    # What a local capture in flight looks like to the receive path.
    sync._local_capture_active = 1

    outcome = sync.handle_remote_message_ex(remote_msg())

    assert outcome["accepted"] is True  # it did arrive; settle the sender
    assert outcome["clipboard"] is False  # ... but the local copy wins
    assert outcome["history"] is True
    assert len(history.rows) == 1
    assert writer.calls == 0


def test_a_duplicate_delivery_is_accepted_so_a_lost_receipt_does_not_loop():
    sync, _writer, history, _errors = manager()

    first = sync.handle_remote_message_ex(remote_msg())
    second = sync.handle_remote_message_ex(remote_msg())

    assert first["accepted"] is True and first["clipboard"] is True
    assert second["accepted"] is True
    assert second["reason"] == "duplicate"
    assert second["clipboard"] is False
    assert len(history.rows) == 1
