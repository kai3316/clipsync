"""The file-transfer wire and receive path.

Pins the frames another version must agree with (``file_request`` / ``file_ack``
/ ``file_chunk`` / ``file_chunk_ack`` with ``missing_chunks`` / ``file_complete``
/ ``file_reject``), the chunk and size accounting, the inbound-chunk checks, the
consent gate on a pending offer, stall recovery and resume, duplicate-request
idempotency, and atomic destination naming.
"""


import base64
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from internal.protocol.codec import decode_message
from internal.sync import file_transfer as file_transfer_mod
from internal.sync.file_transfer import (
    CHUNK_SIZE,
    FileTransferManager,
)


@pytest.fixture
def fast_stall_grace(monkeypatch):
    """Shrink the receiver's stall grace so gap tests do not sleep 5 s."""
    monkeypatch.setattr(file_transfer_mod, "STALL_GRACE", 0.1)
    return 0.1


class TestFileTransferManager:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.output_dir = os.path.join(self.tmp_dir, "output")
        self.mgr = FileTransferManager("test-device", self.output_dir)
        self.sent_frames: list[bytes] = []

    def teardown_method(self):
        self.mgr.cleanup_stale_transfers()

    def _broadcast_fn(self, data):
        self.sent_frames.append(data)

    def _create_temp_file(self, name: str, size: int) -> str:
        path = os.path.join(self.tmp_dir, name)
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        return path

    def _decode_sent(self, index: int = 0) -> dict:
        msg = decode_message(self.sent_frames[index])
        assert msg is not None, f"No decodeable msg at index {index}"
        raw = getattr(msg, "_raw_payload", {})
        return raw

    # ------------------------------------------------------------------
    # send_file
    # ------------------------------------------------------------------

    def test_send_file_emits_file_request(self):
        path = self._create_temp_file("hello.txt", 100)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        assert len(tid) == 32  # UUID hex
        assert len(self.sent_frames) == 1
        raw = self._decode_sent(0)
        assert raw["msg_type"] == "file_request"
        assert raw["transfer_id"] == tid
        assert raw["file_name"] == "hello.txt"
        assert raw["file_size"] == 100
        assert raw["mime_type"] == "text/plain"

    def test_send_file_multi_chunk(self):
        """File spanning multiple chunks."""
        size = CHUNK_SIZE * 2 + 500
        path = self._create_temp_file("multi.bin", size)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)
        time.sleep(0.3)

        chunk_msgs = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_chunk"
        ]
        assert len(chunk_msgs) == 3
        assert chunk_msgs[0]["chunk_index"] == 0
        assert chunk_msgs[1]["chunk_index"] == 1
        assert chunk_msgs[2]["chunk_index"] == 2
        assert all(c["total_chunks"] == 3 for c in chunk_msgs)

    # ------------------------------------------------------------------
    # Incoming transfer flow
    # ------------------------------------------------------------------

    def test_handle_file_request_creates_pending_transfer(self):
        # Suppress auto-accept to test pending state
        self.mgr.set_on_transfer_request(lambda *a: None)
        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": "abc123",
                "file_name": "test.pdf",
                "file_size": 5000,
                "mime_type": "application/pdf",
            },
            self._broadcast_fn,
        )

        transfers = self.mgr.get_transfers()
        assert len(transfers) == 1
        t = transfers[0]
        assert t["file_name"] == "test.pdf"
        assert t["file_size"] == 5000
        assert t["direction"] == "down"
        assert t["state"] == "pending"

    # ------------------------------------------------------------------
    # Reject flow
    # ------------------------------------------------------------------

    def test_reject_transfer_sends_file_reject(self):
        # First create a pending incoming transfer.  Without a callback the
        # manager auto-accepts headlessly, which is no longer rejectable.
        self.mgr.set_on_transfer_request(lambda *a: None)
        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": "rej001",
                "file_name": "x.txt",
                "file_size": 10,
                "mime_type": "text/plain",
            },
            self._broadcast_fn,
        )

        self.mgr.reject_transfer("rej001", self._broadcast_fn)

        rejects = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_reject"
        ]
        assert any(r["transfer_id"] == "rej001" for r in rejects)

    # ------------------------------------------------------------------
    # Full receive flow (simulated)
    # ------------------------------------------------------------------

    def test_receive_small_file(self):
        """End-to-end: receive a small file (single chunk)."""
        file_data = b"Hello ClipSync file transfer!"
        tid = "recv001"

        # Step 1: file_request arrives
        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": tid,
                "file_name": "hello.txt",
                "file_size": len(file_data),
                "mime_type": "text/plain",
            },
            self._broadcast_fn,
        )

        # Step 2: user accepts (auto-accept since no callback set)
        # file_ack was already sent above

        # Step 3: file_chunk arrives
        b64_data = base64.b64encode(file_data).decode("ascii")
        self.mgr.handle_message(
            "file_chunk",
            {
                "transfer_id": tid,
                "chunk_index": 0,
                "total_chunks": 1,
                "data": b64_data,
            },
            self._broadcast_fn,
        )
        time.sleep(0.1)

        # Check output file
        output_path = Path(self.output_dir) / "hello.txt"
        assert output_path.exists()
        assert output_path.read_bytes() == file_data

        # Check file_complete was sent
        completes = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_complete"
        ]
        assert any(c["transfer_id"] == tid and c["status"] == "success" for c in completes)

    def test_receive_file_multi_chunk_out_of_order(self):
        """Chunks arrive in reverse order — should still assemble correctly."""
        chunk_size = CHUNK_SIZE
        file_data = os.urandom(chunk_size * 3)
        tid = "recv_oof"

        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": tid,
                "file_name": "out_of_order.bin",
                "file_size": len(file_data),
                "mime_type": "application/octet-stream",
            },
            self._broadcast_fn,
        )

        # Encode all chunks
        chunks = [file_data[i : i + chunk_size] for i in range(0, len(file_data), chunk_size)]

        # Send in reverse order
        for idx in reversed(range(len(chunks))):
            b64 = base64.b64encode(chunks[idx]).decode("ascii")
            self.mgr.handle_message(
                "file_chunk",
                {
                    "transfer_id": tid,
                    "chunk_index": idx,
                    "total_chunks": len(chunks),
                    "data": b64,
                },
                self._broadcast_fn,
            )

        time.sleep(0.1)

        output_path = Path(self.output_dir) / "out_of_order.bin"
        assert output_path.exists()
        assert output_path.read_bytes() == file_data

    def test_receive_file_name_collision(self):
        """When a file with the same name exists, append (1), (2), etc."""
        existing = Path(self.output_dir) / "collision.txt"
        existing.write_text("original")

        file_data = b"new version"
        tid = "collision01"

        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": tid,
                "file_name": "collision.txt",
                "file_size": len(file_data),
                "mime_type": "text/plain",
            },
            self._broadcast_fn,
        )

        b64 = base64.b64encode(file_data).decode("ascii")
        self.mgr.handle_message(
            "file_chunk",
            {"transfer_id": tid, "chunk_index": 0, "total_chunks": 1, "data": b64},
            self._broadcast_fn,
        )
        time.sleep(0.1)

        # Original still there
        assert existing.read_text() == "original"
        # New file with (1) suffix
        renamed = Path(self.output_dir) / "collision (1).txt"
        assert renamed.exists()
        assert renamed.read_bytes() == file_data

    # ------------------------------------------------------------------
    # Size mismatch detection
    # ------------------------------------------------------------------

    def test_size_mismatch_detected(self):
        """If assembled file size != advertised size, report error."""
        tid = "size_mismatch"

        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": tid,
                "file_name": "bad.txt",
                "file_size": 9999,
                "mime_type": "text/plain",
            },
            self._broadcast_fn,
        )

        # Send chunk with only 5 bytes but claim file_size was 9999
        b64 = base64.b64encode(b"hello").decode("ascii")
        self.mgr.handle_message(
            "file_chunk",
            {"transfer_id": tid, "chunk_index": 0, "total_chunks": 1, "data": b64},
            self._broadcast_fn,
        )
        time.sleep(0.1)

        # File should NOT have been saved
        assert not (Path(self.output_dir) / "bad.txt").exists()

        # Error status should have been sent
        completes = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_complete"
        ]
        assert any(c.get("status") == "error_size_mismatch" for c in completes)

    # ------------------------------------------------------------------
    # Invalid chunk data
    # ------------------------------------------------------------------

    def test_invalid_base64_chunk_ignored(self):
        tid = "bad_b64"

        self.mgr.handle_message(
            "file_request",
            {"transfer_id": tid, "file_name": "b64.txt", "file_size": 5, "mime_type": "text/plain"},
            self._broadcast_fn,
        )

        self.mgr.handle_message(
            "file_chunk",
            {
                "transfer_id": tid,
                "chunk_index": 0,
                "total_chunks": 1,
                "data": "!!!not valid base64!!!",
            },
            self._broadcast_fn,
        )

        time.sleep(0.1)
        # Transfer should still be in receiving state, file not created
        assert not (Path(self.output_dir) / "b64.txt").exists()

    # ------------------------------------------------------------------
    # Sender side: file_ack / file_reject / file_complete
    # ------------------------------------------------------------------

    def test_file_reject_cleans_up_outgoing(self):
        complete_calls: list[tuple] = []
        self.mgr.set_on_transfer_complete(
            lambda tid, ok, cancelled, status: complete_calls.append((tid, ok))
        )

        path = self._create_temp_file("reject_me.txt", 100)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        self.mgr.handle_message("file_reject", {"transfer_id": tid}, self._broadcast_fn)

        assert len(complete_calls) == 1
        assert complete_calls[0][0] == tid
        assert complete_calls[0][1] is False

    # ------------------------------------------------------------------
    # Duplicate ACK protection
    # ------------------------------------------------------------------

    def test_duplicate_ack_does_not_resend(self):
        path = self._create_temp_file("dup_ack.bin", CHUNK_SIZE)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        # First ack
        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)
        # Second ack (duplicate)
        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)

        time.sleep(0.3)

        chunk_msgs = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_chunk"
        ]
        assert len(chunk_msgs) == 1  # only one set of chunks

    # ------------------------------------------------------------------
    # Chunk for transfer not in 'receiving' state
    # ------------------------------------------------------------------

    def test_chunk_for_pending_transfer(self):
        """Chunk arriving before ack (pending state) should be ignored."""
        self.mgr.set_on_transfer_request(lambda *a: None)  # suppress auto-accept
        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": "pend",
                "file_name": "p.txt",
                "file_size": 10,
                "mime_type": "text/plain",
            },
            self._broadcast_fn,
        )
        # Transfer is still "pending", chunk should be dropped
        self.mgr.handle_message(
            "file_chunk",
            {"transfer_id": "pend", "chunk_index": 0, "total_chunks": 1, "data": "YQ=="},
            self._broadcast_fn,
        )
        # Should not crash, no file created
        assert not (Path(self.output_dir) / "p.txt").exists()

    # ------------------------------------------------------------------
    # Zero-length file
    # ------------------------------------------------------------------

    def test_send_zero_length_file(self):
        path = self._create_temp_file("empty.txt", 0)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        # Should still have 1 chunk (the code forces total_chunks >= 1)
        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)
        time.sleep(0.2)

        chunk_msgs = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_chunk"
        ]
        assert len(chunk_msgs) == 1
        raw = chunk_msgs[0].get("_raw_data")
        if raw is not None:
            assert raw == b""
        else:
            assert base64.b64decode(chunk_msgs[0]["data"]) == b""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# merged from test_round8_transfer.py
# ══════════════════════════════════════════════════


# ---------------------------------------------------------------------------
# file_transfer: duplicate file_request idempotency
# ---------------------------------------------------------------------------


TID = "a" * 32


class TestDuplicateFileRequest:
    def _make_request(self):
        return {
            "msg_type": "file_request",
            "transfer_id": TID,
            "file_name": "hello.txt",
            "file_size": 10,
            "mime_type": "text/plain",
            "kind": "file",
        }

    def test_duplicate_request_keeps_active_receive_state(self, tmp_path):
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        sends = []
        send_fn = lambda data: sends.append(data) or True  # noqa: E731

        mgr.handle_message("file_request", self._make_request(), send_fn, "peerA")
        first = mgr._transfers[TID]
        assert first["state"] == "receiving"
        assert first.get("temp_fh") is not None
        acks_after_first = sum(1 for d in sends)

        # Replay of the same request (e.g. both legs of a bidirectional
        # connect race delivered the broadcast twice): must NOT re-register,
        # must NOT pop a second ack/dialog.
        mgr.handle_message("file_request", dict(self._make_request()), send_fn, "peerA")
        assert mgr._transfers.get(TID) is first
        assert first.get("temp_fh") is not None
        assert sum(1 for d in sends) == acks_after_first

        mgr.cancel_transfer(TID)
        assert TID not in mgr._transfers
        assert not list(tmp_path.glob(".*.part"))


# ---------------------------------------------------------------------------
# file_transfer: history deletion by id
# ---------------------------------------------------------------------------


class TestDeleteHistoryById:
    """The web UI's transfer-history context menu carries only a transfer_id.

    ``delete_history_item`` matches on dict equality, so it only works for a
    caller holding the very entry object ``get_history`` returned — a remote
    caller cannot.  ``delete_history_by_id`` resolves the id under the lock.
    """

    def _mgr(self, tmp_path, *ids):
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        mgr._history = [{"transfer_id": i, "file_name": i + ".txt"} for i in ids]
        return mgr

    def test_deletes_the_matching_entry_only(self, tmp_path):
        mgr = self._mgr(tmp_path, "a", "b", "c")
        assert mgr.delete_history_by_id("b") is True
        assert [e["transfer_id"] for e in mgr.get_history()] == ["a", "c"]


class TestStalledIncomingTransfer:
    """Receiver-side recovery when the sender's first pass leaves gaps.

    The sender sends every chunk once, then waits ~150 s polling its
    retransmit queue -- it never announces "first pass done".  So the receiver
    must notice the silence.  The old trigger fired finalization when
    ``received >= total - 1``, which meant a transfer missing two or more
    chunks was never finalized at all: no ``file_chunk_ack`` was ever emitted
    and the transfer sat until the stale sweep deleted the .part file.
    """

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.output_dir = os.path.join(self.tmp_dir, "output")
        self.mgr = FileTransferManager("test-device", self.output_dir)
        self.sent_frames: list[bytes] = []

    def teardown_method(self):
        self.mgr.cleanup_stale_transfers()

    def _broadcast_fn(self, data):
        self.sent_frames.append(data)

    def _sent(self) -> list[dict]:
        out = []
        for frame in list(self.sent_frames):
            msg = decode_message(frame)
            if msg is not None:
                out.append(getattr(msg, "_raw_payload", {}))
        return out

    def _of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self._sent() if m.get("msg_type") == msg_type]

    def _wait_for(self, msg_type: str, timeout: float = 5.0) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self._of_type(msg_type)
            if found:
                return found
            time.sleep(0.02)
        return self._of_type(msg_type)

    def _begin(self, tid: str, name: str, total: int):
        self.chunks = [os.urandom(CHUNK_SIZE) for _ in range(total - 1)]
        self.chunks.append(os.urandom(1024))  # short tail chunk
        self.file_data = b"".join(self.chunks)
        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": tid,
                "file_name": name,
                "file_size": len(self.file_data),
                "mime_type": "application/octet-stream",
            },
            self._broadcast_fn,
        )

    def _feed(self, tid: str, idx: int, total: int):
        self.mgr.handle_message(
            "file_chunk",
            {
                "transfer_id": tid,
                "chunk_index": idx,
                "total_chunks": total,
                "data": base64.b64encode(self.chunks[idx]).decode("ascii"),
            },
            self._broadcast_fn,
        )

    def test_two_missing_chunks_are_requested_and_recovered(self, fast_stall_grace):
        tid = "gap2"
        total = 5
        self._begin(tid, "twogaps.bin", total)

        for idx in (0, 1, 4):  # 2 and 3 lost in flight
            self._feed(tid, idx, total)

        acks = self._wait_for("file_chunk_ack")
        assert acks, "stall watchdog never requested the missing chunks"
        assert acks[0]["missing_chunks"] == [2, 3]
        assert not (Path(self.output_dir) / "twogaps.bin").exists()

        for idx in (2, 3):
            self._feed(tid, idx, total)

        deadline = time.time() + 5.0
        out = Path(self.output_dir) / "twogaps.bin"
        while time.time() < deadline and not out.exists():
            time.sleep(0.02)
        assert out.exists()
        assert out.read_bytes() == self.file_data

    def test_resume_re_requests_chunks_dropped_while_paused(self):
        """Chunks arriving during a pause are discarded, so resume must re-ask.

        Nothing on the sender re-sends them on its own -- the resume path used
        to just send file_resume and rely on a retransmit that never came.
        """
        tid = "paused1"
        total = 4
        self._begin(tid, "paused.bin", total)
        self._feed(tid, 0, total)

        assert self.mgr.pause_transfer(tid, self._broadcast_fn) is True
        for idx in (1, 2):  # dropped: receiver is paused
            self._feed(tid, idx, total)
        assert self.mgr.resume_transfer(tid, self._broadcast_fn) is True

        acks = self._of_type("file_chunk_ack")
        assert acks, "resume did not re-request the chunks lost during the pause"
        assert acks[-1]["missing_chunks"] == [1, 2, 3]

        for idx in (1, 2, 3):
            self._feed(tid, idx, total)
        out = Path(self.output_dir) / "paused.bin"
        deadline = time.time() + 5.0
        while time.time() < deadline and not out.exists():
            time.sleep(0.02)
        assert out.read_bytes() == self.file_data

    def test_pause_does_not_delete_the_partial_file(self, fast_stall_grace):
        """Finalization while paused must not burn a round or drop the .part."""
        tid = "paused2"
        total = 4
        self._begin(tid, "keepme.bin", total)
        for idx in (0, 1, 2):
            self._feed(tid, idx, total)
        assert self.mgr.pause_transfer(tid, self._broadcast_fn) is True

        time.sleep(fast_stall_grace * 6)  # long enough to stall, if it counted

        part = Path(self.output_dir) / f".{tid}.part"
        assert part.exists(), "partial file was discarded during a pause"
        with self.mgr._lock:
            assert tid in self.mgr._transfers
            assert self.mgr._transfers[tid].get("_ack_rounds", 0) == 0

    def test_pause_past_the_absolute_cap_is_reaped(self, monkeypatch):
        """An abandoned pause must not pin a thread, an fd and the .part forever."""
        monkeypatch.setattr(file_transfer_mod, "PAUSED_MAX_SECONDS", 0.05)
        tid = "paused4"
        total = 3
        self._begin(tid, "abandoned.bin", total)
        self._feed(tid, 0, total)
        assert self.mgr.pause_transfer(tid, self._broadcast_fn) is True

        time.sleep(0.1)
        self.mgr.cleanup_stale_transfers()
        with self.mgr._lock:
            assert tid not in self.mgr._transfers
        assert not (Path(self.output_dir) / f".{tid}.part").exists()


# ═════════════════════════════════════════════════════════════════════════
# Stage 6 — two same-named files arriving at once must both survive
# ═════════════════════════════════════════════════════════════════════════


class TestDestinationNameIsClaimedAtomically:
    """The old "while dest.exists(): bump counter" was a check followed by an
    unprotected use.  Two transfers of the same name finishing together both
    settled on the same "(1)" path, and one of the two files the user was
    sent was silently lost (POSIX) or the transfer failed as error_disk
    (Windows, where rename onto an existing path raises)."""

    def test_concurrent_claims_never_collide(self, tmp_path):
        import threading

        from internal.sync.file_transfer import _reserve_dest_name

        claimed = []
        lock = threading.Lock()
        start = threading.Event()

        def _claim():
            start.wait(timeout=5)
            got = _reserve_dest_name(tmp_path / "report.pdf")
            with lock:
                claimed.append(got)

        threads = [threading.Thread(target=_claim) for _ in range(8)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)

        assert len(claimed) == 8
        assert len(set(claimed)) == 8, "every concurrent claim must be unique"


