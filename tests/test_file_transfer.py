"""Tests for FileTransferManager — chunked transfer, state machine, edge cases."""

import base64
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import decode_message, encode_frame
from internal.sync.file_transfer import (
    CHUNK_SIZE,
    FileTransferManager,
    _guess_mime_type,
    _safe_remove,
)


class TestMimeType:
    def test_known_extensions(self):
        assert _guess_mime_type("test.png") == "image/png"
        assert _guess_mime_type("test.jpg") == "image/jpeg"
        assert _guess_mime_type("test.pdf") == "application/pdf"
        assert _guess_mime_type("test.txt") == "text/plain"
        assert _guess_mime_type("test.py") == "text/x-python"
        assert _guess_mime_type("test.json") == "application/json"
        assert _guess_mime_type("test.zip") == "application/zip"

    def test_unknown_extension(self):
        assert _guess_mime_type("test.xyzzy") == "application/octet-stream"

    def test_no_extension(self):
        assert _guess_mime_type("Makefile") == "application/octet-stream"

    def test_case_insensitive(self):
        assert _guess_mime_type("test.PNG") == "image/png"
        assert _guess_mime_type("TEST.JPG") == "image/jpeg"


class TestSafeRemove:
    def test_remove_existing(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"data")
            path = Path(f.name)
        assert path.exists()
        _safe_remove(path)
        assert not path.exists()

    def test_remove_nonexistent(self):
        path = Path(tempfile.gettempdir()) / "nonexistent_clipsync_test_file"
        _safe_remove(path)  # should not raise

    def test_remove_directory(self, tmp_path):
        d = tmp_path / "testdir"
        d.mkdir()
        # _safe_remove uses Path.unlink which fails on directories
        _safe_remove(d)  # should not raise


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

    def test_send_file_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            self.mgr.send_file("/nonexistent/path.xyz", self._broadcast_fn)

    def test_send_file_small_single_chunk(self):
        """File smaller than CHUNK_SIZE should produce exactly 1 chunk."""
        path = self._create_temp_file("small.bin", 100)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        # Ack the transfer to start chunk sending
        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)

        # Wait for all chunks + complete message
        time.sleep(0.3)

        # Find the file_chunk message
        chunks = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
        ]
        chunk_msgs = [c for c in chunks if c.get("msg_type") == "file_chunk"]
        assert len(chunk_msgs) == 1
        assert chunk_msgs[0]["chunk_index"] == 0
        assert chunk_msgs[0]["total_chunks"] == 1
        # Accept both binary (_raw_data) and legacy base64 (data) formats
        raw = chunk_msgs[0].get("_raw_data")
        if raw is not None:
            assert len(raw) == 100
        else:
            assert len(base64.b64decode(chunk_msgs[0]["data"])) == 100

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

    def test_send_file_progress_callback(self):
        progress_values: list[float] = []
        self.mgr.set_on_transfer_progress(lambda tid, p: progress_values.append(p))

        path = self._create_temp_file("prog.bin", CHUNK_SIZE * 2)
        tid = self.mgr.send_file(path, self._broadcast_fn)
        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)
        time.sleep(0.3)

        assert len(progress_values) >= 2
        assert progress_values[-1] >= 1.0

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

    def test_auto_accept_when_no_callback(self):
        """Without a transfer_request callback, files are auto-accepted."""
        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": "auto001",
                "file_name": "auto.txt",
                "file_size": 42,
                "mime_type": "text/plain",
            },
            self._broadcast_fn,
        )

        # file_ack should have been sent
        acks = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_ack"
        ]
        assert len(acks) == 1
        assert acks[0]["transfer_id"] == "auto001"

    def test_transfer_request_callback(self):
        callback_calls: list[tuple] = []

        def on_request(tid, fname, fsize, mime, send_fn):
            callback_calls.append((tid, fname, fsize, mime))
            self.mgr.accept_transfer(tid, send_fn)

        self.mgr.set_on_transfer_request(on_request)

        def send_fn(data):
            self.sent_frames.append(data)

        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": "cb001",
                "file_name": "callback.txt",
                "file_size": 100,
                "mime_type": "text/plain",
            },
            send_fn,
        )

        assert len(callback_calls) == 1
        assert callback_calls[0][0] == "cb001"
        assert callback_calls[0][1] == "callback.txt"
        assert callback_calls[0][2] == 100

    # ------------------------------------------------------------------
    # Reject flow
    # ------------------------------------------------------------------

    def test_reject_transfer_sends_file_reject(self):
        # First create a pending incoming transfer
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": "rej001", "file_name": "x.txt", "file_size": 10, "mime_type": "text/plain"},
            self._broadcast_fn,
        )

        self.mgr.reject_transfer("rej001", self._broadcast_fn)

        rejects = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_reject"
        ]
        assert any(r["transfer_id"] == "rej001" for r in rejects)

    def test_reject_unknown_transfer_does_not_crash(self):
        # Should not raise
        self.mgr.reject_transfer("nonexistent_id", self._broadcast_fn)

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
        assert any(
            c["transfer_id"] == tid and c["status"] == "success"
            for c in completes
        )

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
        chunks = [
            file_data[i:i + chunk_size]
            for i in range(0, len(file_data), chunk_size)
        ]

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
            {"transfer_id": tid, "file_name": "collision.txt", "file_size": len(file_data), "mime_type": "text/plain"},
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

    def test_receive_completion_callbacks(self):
        progress_vals: list[float] = []
        complete_args: list[tuple] = []
        received_args: list[tuple] = []

        self.mgr.set_on_transfer_progress(lambda tid, p: progress_vals.append(p))
        self.mgr.set_on_transfer_complete(lambda tid, ok, cancelled, status: complete_args.append((tid, ok)))
        self.mgr.set_on_file_received(lambda tid, path, name: received_args.append((tid, path, name)))

        file_data = b"callback test data"
        tid = "cb_test"

        self.mgr.handle_message(
            "file_request",
            {"transfer_id": tid, "file_name": "cb.txt", "file_size": len(file_data), "mime_type": "text/plain"},
            self._broadcast_fn,
        )

        b64 = base64.b64encode(file_data).decode("ascii")
        self.mgr.handle_message(
            "file_chunk",
            {"transfer_id": tid, "chunk_index": 0, "total_chunks": 1, "data": b64},
            self._broadcast_fn,
        )
        time.sleep(0.1)

        assert len(progress_vals) >= 1, "progress callback should fire"
        assert len(complete_args) == 1
        assert complete_args[0][0] == tid
        assert complete_args[0][1] is True
        assert len(received_args) == 1
        assert received_args[0][0] == tid
        assert "cb.txt" in received_args[0][1]

    # ------------------------------------------------------------------
    # Size mismatch detection
    # ------------------------------------------------------------------

    def test_size_mismatch_detected(self):
        """If assembled file size != advertised size, report error."""
        tid = "size_mismatch"

        self.mgr.handle_message(
            "file_request",
            {"transfer_id": tid, "file_name": "bad.txt", "file_size": 9999, "mime_type": "text/plain"},
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

    def test_missing_chunk_detected(self):
        """Missing chunks trigger file_chunk_ack retransmit request, not immediate failure."""
        tid = "missing_chunk"

        self.mgr.handle_message(
            "file_request",
            {
                "transfer_id": tid, "file_name": "gap.bin",
                "file_size": CHUNK_SIZE * 2, "mime_type": "application/octet-stream",
            },
            self._broadcast_fn,
        )

        for idx in [0, 2]:
            b64 = base64.b64encode(b"0123456789").decode("ascii")
            self.mgr.handle_message(
                "file_chunk",
                {"transfer_id": tid, "chunk_index": idx, "total_chunks": 2, "data": b64},
                self._broadcast_fn,
            )

        time.sleep(0.1)

        # File should NOT have been saved
        assert not (Path(self.output_dir) / "gap.bin").exists()

        # Should have sent file_chunk_ack with missing chunks instead of immediate error
        ack_msgs = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_chunk_ack"
        ]
        assert len(ack_msgs) >= 1
        assert ack_msgs[0].get("missing_chunks") == [1]

        # Verify no immediate file_complete error
        completes = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_complete"
        ]
        assert not any(c.get("status") == "error_missing_chunks" for c in completes)

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
        self.mgr.set_on_transfer_complete(lambda tid, ok, cancelled, status: complete_calls.append((tid, ok)))

        path = self._create_temp_file("reject_me.txt", 100)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        self.mgr.handle_message("file_reject", {"transfer_id": tid}, self._broadcast_fn)

        assert len(complete_calls) == 1
        assert complete_calls[0][0] == tid
        assert complete_calls[0][1] is False

    def test_file_complete_success(self):
        complete_calls: list[tuple] = []
        self.mgr.set_on_transfer_complete(lambda tid, ok, cancelled, status: complete_calls.append((tid, ok)))

        path = self._create_temp_file("ok.txt", 50)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        self.mgr.handle_message(
            "file_complete",
            {"transfer_id": tid, "status": "success"},
            self._broadcast_fn,
        )

        assert len(complete_calls) == 1
        assert complete_calls[0][1] is True

    def test_file_complete_error_status(self):
        complete_calls: list[tuple] = []
        self.mgr.set_on_transfer_complete(lambda tid, ok, cancelled, status: complete_calls.append((tid, ok)))

        path = self._create_temp_file("err.txt", 50)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        self.mgr.handle_message(
            "file_complete",
            {"transfer_id": tid, "status": "error_disk"},
            self._broadcast_fn,
        )

        assert len(complete_calls) == 1
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
    # get_transfers snapshot
    # ------------------------------------------------------------------

    def test_get_transfers_empty(self):
        assert self.mgr.get_transfers() == []

    def test_get_transfers_outgoing(self):
        path = self._create_temp_file("snap.txt", 50)
        self.mgr.send_file(path, self._broadcast_fn)

        transfers = self.mgr.get_transfers()
        assert len(transfers) == 1
        assert transfers[0]["direction"] == "up"
        assert transfers[0]["state"] == "awaiting_ack"
        assert transfers[0]["file_name"] == "snap.txt"
        assert transfers[0]["file_size"] == 50

    def test_get_transfers_incoming(self):
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": "snap_in", "file_name": "incoming.png", "file_size": 200, "mime_type": "image/png"},
            self._broadcast_fn,
        )
        transfers = self.mgr.get_transfers()
        assert len(transfers) == 1
        assert transfers[0]["direction"] == "down"

    # ------------------------------------------------------------------
    # Stale transfer cleanup
    # ------------------------------------------------------------------

    def test_cleanup_stale_transfers(self):
        # This requires manipulating start_time directly
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": "stale001", "file_name": "old.txt", "file_size": 10, "mime_type": "text/plain"},
            self._broadcast_fn,
        )

        # Artificially age the transfer
        with self.mgr._lock:
            if "stale001" in self.mgr._transfers:
                self.mgr._transfers["stale001"]["start_time"] = 0  # epoch
                self.mgr._transfers["stale001"]["_last_activity"] = 0  # epoch

        complete_calls: list[tuple] = []
        self.mgr.set_on_transfer_complete(lambda tid, ok, cancelled, status: complete_calls.append((tid, ok)))

        self.mgr.cleanup_stale_transfers()

        assert len(self.mgr.get_transfers()) == 0
        assert any(tid == "stale001" for tid, ok in complete_calls if not ok)

    # ------------------------------------------------------------------
    # Unknown message type
    # ------------------------------------------------------------------

    def test_unknown_msg_type_does_not_crash(self):
        self.mgr.handle_message("bogus_type", {}, self._broadcast_fn)
        # Should not raise

    # ------------------------------------------------------------------
    # Accept transfer with temp file error
    # ------------------------------------------------------------------

    def test_accept_transfer_disk_error(self):
        # Create a manager then point its output to a path where writing will fail.
        # Use a file path as if it were a directory — parent of temp file will be a file.
        bad_dir = os.path.join(self.tmp_dir, "not_a_dir")
        with open(bad_dir, "w") as f:
            f.write("block")
        bad_mgr = FileTransferManager("test-device", self.output_dir)
        bad_mgr._output_dir = Path(bad_dir)  # bypass mkdir in __init__
        bad_mgr.handle_message(
            "file_request",
            {"transfer_id": "diskerr", "file_name": "f.txt", "file_size": 10, "mime_type": "text/plain"},
            self._broadcast_fn,
        )
        # accept_transfer will fail to open temp file and send file_reject
        rejects = [
            self._decode_sent(i)
            for i in range(len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_reject"
        ]
        assert any(r["transfer_id"] == "diskerr" for r in rejects)

    # ------------------------------------------------------------------
    # accept_transfer edge cases
    # ------------------------------------------------------------------

    def test_accept_nonexistent_transfer(self):
        self.mgr.accept_transfer("nope", self._broadcast_fn)
        # Should not raise

    def test_accept_already_receiving_transfer(self):
        """Accepting a transfer that's already in 'receiving' state is a no-op."""
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": "twice", "file_name": "t.txt", "file_size": 10, "mime_type": "text/plain"},
            self._broadcast_fn,
        )
        # Already auto-accepted above, try accepting again
        sent_before = len(self.sent_frames)
        self.mgr.accept_transfer("twice", self._broadcast_fn)
        # No additional file_ack should be sent
        acks_after = [
            self._decode_sent(i)
            for i in range(sent_before, len(self.sent_frames))
            if self._decode_sent(i).get("msg_type") == "file_ack"
        ]
        assert len(acks_after) == 0

    # ------------------------------------------------------------------
    # Sender failure during chunk read
    # ------------------------------------------------------------------

    def test_send_chunks_file_disappears(self):
        """If the file is deleted after ack, sender should report failure."""
        path = self._create_temp_file("ghost.bin", CHUNK_SIZE)
        tid = self.mgr.send_file(path, self._broadcast_fn)

        # Delete the file before acking
        os.unlink(path)

        complete_calls: list[tuple] = []
        self.mgr.set_on_transfer_complete(lambda tid, ok, cancelled, status: complete_calls.append((tid, ok)))

        self.mgr.handle_message("file_ack", {"transfer_id": tid}, self._broadcast_fn)
        time.sleep(0.2)

        assert any(t == tid and not ok for t, ok in complete_calls)

    # ------------------------------------------------------------------
    # Chunk for unknown transfer
    # ------------------------------------------------------------------

    def test_chunk_for_unknown_transfer(self):
        self.mgr.handle_message(
            "file_chunk",
            {"transfer_id": "ghost", "chunk_index": 0, "total_chunks": 1, "data": "YQ=="},
            self._broadcast_fn,
        )
        # Should not raise

    # ------------------------------------------------------------------
    # file_ack for non-outgoing or unknown
    # ------------------------------------------------------------------

    def test_ack_for_incoming_transfer(self):
        """An ack for an incoming transfer should be a no-op."""
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": "inc", "file_name": "i.txt", "file_size": 10, "mime_type": "text/plain"},
            self._broadcast_fn,
        )
        sent_before = len(self.sent_frames)
        self.mgr.handle_message("file_ack", {"transfer_id": "inc"}, self._broadcast_fn)
        # Should not crash, no extra messages
        assert len(self.sent_frames) == sent_before + 0

    # ------------------------------------------------------------------
    # Chunk for transfer not in 'receiving' state
    # ------------------------------------------------------------------

    def test_chunk_for_pending_transfer(self):
        """Chunk arriving before ack (pending state) should be ignored."""
        self.mgr.set_on_transfer_request(lambda *a: None)  # suppress auto-accept
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": "pend", "file_name": "p.txt", "file_size": 10, "mime_type": "text/plain"},
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

    def test_receive_zero_length_file(self):
        tid = "zero_len"
        self.mgr.handle_message(
            "file_request",
            {"transfer_id": tid, "file_name": "empty.txt", "file_size": 0, "mime_type": "text/plain"},
            self._broadcast_fn,
        )

        b64 = base64.b64encode(b"").decode("ascii")
        self.mgr.handle_message(
            "file_chunk",
            {"transfer_id": tid, "chunk_index": 0, "total_chunks": 1, "data": b64},
            self._broadcast_fn,
        )
        time.sleep(0.1)

        output = Path(self.output_dir) / "empty.txt"
        assert output.exists()
        assert output.stat().st_size == 0

    # ------------------------------------------------------------------
    # Send-as-frame encoding
    # ------------------------------------------------------------------

    def test_send_as_frame_produces_valid_frame(self):
        frame = encode_frame({"msg_type": "test", "key": "value"})
        # Frame should have 4-byte length prefix + magic + version + header + payload
        assert len(frame) >= 10
        msg = decode_message(frame)
        assert msg is not None
        raw = getattr(msg, "_raw_payload", {})
        assert raw["msg_type"] == "test"
        assert raw["key"] == "value"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# merged from test_round8_transfer.py
# ══════════════════════════════════════════════════

import hashlib
import struct
import threading

import pytest

import internal.transport.connection as conn_mod
from internal.protocol.codec import (
    HEADER_FMT,
    VERSION,
    encode_binary_chunk,
)
from internal.sync.file_transfer import (
    SPEED_STALE_AFTER,
)
from internal.transport.connection import (
    MAX_RECONNECT_ATTEMPTS,
    MAX_RECONNECT_BACKOFF,
    MIN_RECONNECT_DELAY,
    TransportManager,
    peer_id_hash,
)

# ---------------------------------------------------------------------------
# codec: malformed-frame tolerance
# ---------------------------------------------------------------------------


def _wrap_payload(payload_bytes: bytes, msg_id: str = "abcd") -> bytes:
    """Hand-roll a wire frame with a non-object JSON body."""
    buf = bytearray()
    buf += struct.pack(HEADER_FMT, 0x4353, VERSION, len(payload_bytes))
    buf += struct.pack(">I", len(msg_id.encode()))
    buf += msg_id.encode()
    buf += b"\x01d"  # source_device: len 1, "d"
    buf += payload_bytes
    return bytes(buf)


class TestCodecMalformedTolerance:
    def test_empty_and_short_inputs_return_none(self):
        assert decode_message(b"") is None
        assert decode_message(b"\x43\x53\x02") is None

    def test_json_array_payload_is_dropped_not_crash(self):
        frame = _wrap_payload(b"[1,2,3]")
        assert decode_message(frame) is None

    def test_non_string_msg_type_is_dropped(self):
        frame = encode_frame({"msg_type": {"evil": 1}, "types": {}})
        assert decode_message(frame) is None

    def test_bad_base64_type_skipped_message_survives(self):
        frame = encode_frame({
            "msg_type": "clipboard",
            "types": {"TEXT": "!!!not-base64!!!"},
            "timestamp": 1.0,
        })
        msg = decode_message(frame)
        assert msg is not None
        assert msg.msg_type == "clipboard"
        assert msg.content.types == {}

    def test_binary_chunk_roundtrip(self):
        frame = encode_binary_chunk("f" * 32, 3, 9, b"hello-chunk")
        msg = decode_message(frame)
        assert msg is not None
        assert msg.msg_type == "file_chunk"
        assert msg._raw_payload["chunk_index"] == 3
        assert msg._raw_payload["total_chunks"] == 9
        assert msg._raw_payload["_raw_data"] == b"hello-chunk"

    def test_truncated_binary_frame_returns_none(self):
        frame = encode_binary_chunk("f" * 32, 0, 1, b"x" * 64)
        assert decode_message(frame[:20]) is None
        # magic present but header claims more data than provided
        assert decode_message(frame[:-10]) is None

    def test_wrong_binary_magic_falls_through_to_json_path(self):
        data = b"ZZZZ" + b"\x00" * 40
        assert decode_message(data) is None


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

    def test_new_request_after_completion_registers_normally(self, tmp_path):
        """Same id arriving once the old entry is gone must still register."""
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        mgr.handle_message("file_request", self._make_request(), lambda d: True, "peerA")
        # Simulate completion cleanup.
        mgr._transfers.pop(TID)
        mgr.handle_message("file_request", dict(self._make_request()), lambda d: True, "peerA")
        assert TID in mgr._transfers


# ---------------------------------------------------------------------------
# file_transfer: real-time speed + ETA
# ---------------------------------------------------------------------------


class TestRealtimeSpeed:
    def _wait_for_speed(self, mgr, deadline=5.0):
        end = time.time() + deadline
        while time.time() < end:
            rows = mgr.get_transfers()
            if rows and rows[0]["speed_bytes_per_sec"] > 0:
                return rows[0]
            time.sleep(0.02)
        return None

    def test_outgoing_speed_positive_then_stale_zero(self, tmp_path):
        src = tmp_path / "send.bin"
        src.write_bytes(b"\x01" * (CHUNK_SIZE * 4 + 123))
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))

        tid = mgr.send_file(str(src), lambda data: True)
        thread = threading.Thread(
            target=mgr._send_chunks, args=(tid, lambda data: time.sleep(0.08)),
            daemon=True,
        )
        thread.start()
        try:
            row = self._wait_for_speed(mgr)
            assert row is not None, "speed never became positive while sending"
            assert row["eta_seconds"] > 0
        finally:
            mgr.cancel_transfer(tid)
            thread.join(timeout=5)
        assert tid not in mgr._transfers

    def test_incoming_speed_positive_then_stale_zero(self, tmp_path):
        total_chunks = 10
        payload_req = {
            "msg_type": "file_request",
            "transfer_id": TID,
            "file_name": "in.bin",
            "file_size": CHUNK_SIZE * total_chunks,
            "mime_type": "application/octet-stream",
            "kind": "file",
        }
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        mgr.handle_message("file_request", payload_req, lambda d: True, "peerA")
        mgr.accept_transfer(TID, lambda d: True)

        for i in range(3):
            chunk = {
                "msg_type": "file_chunk",
                "transfer_id": TID,
                "chunk_index": i,
                "total_chunks": total_chunks,
                "_raw_data": b"\x02" * CHUNK_SIZE,
            }
            time.sleep(0.12)
            mgr.handle_message("file_chunk", chunk, lambda d: True, "peerA")

        row = self._wait_for_speed(mgr)
        assert row is not None, "incoming speed never became positive"
        assert row["progress"] > 0 and row["progress"] < 1.0

        # Simulate a stall: rewrite every sample timestamp into the past so
        # no real progress happened recently => speed must read 0 (and ETA 0)
        # instead of showing a stale number.
        t = mgr._transfers[TID]
        old = time.monotonic() - (SPEED_STALE_AFTER + 1.0)
        t["_rate_samples"] = type(t["_rate_samples"])(
            (old, b) for _, b in t["_rate_samples"]
        )
        row = mgr.get_transfers()[0]
        assert row["speed_bytes_per_sec"] == 0.0
        assert row["eta_seconds"] == 0.0

        mgr.cancel_transfer(TID)

    def test_awaiting_ack_reports_zero_speed(self, tmp_path):
        src = tmp_path / "idle.bin"
        src.write_bytes(b"\x03" * 128)
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        tid = mgr.send_file(str(src), lambda data: True)
        row = mgr.get_transfers()[0]
        assert row["state"] == "awaiting_ack"
        assert row["speed_bytes_per_sec"] == 0.0
        mgr.cancel_transfer(tid)


# ---------------------------------------------------------------------------
# transport: reconnect slow retry (no permanent give-up)
# ---------------------------------------------------------------------------


class _StubPairing:
    def is_peer_paired(self, peer_id):
        return True

    def get_identity(self):  # pragma: no cover - unused here
        raise AssertionError("not expected in reconnect scheduling tests")


class _FakeTimer:
    instances: list = []

    def __init__(self, interval, fn, args=(), kwargs=None):
        self.interval = interval
        self.fn = fn
        self.args = args
        self.daemon = None
        self.started = False
        self.canceled = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.canceled = True


@pytest.fixture()
def fake_timer(monkeypatch):
    _FakeTimer.instances = []
    monkeypatch.setattr(conn_mod.threading, "Timer", _FakeTimer)
    return _FakeTimer


class TestSlowRetry:
    def _make_tm(self):
        tm = TransportManager("dev-self", "Self", 0, _StubPairing())
        tm._running = True
        return tm

    def test_fast_phase_backoff_schedule(self, fake_timer):
        tm = self._make_tm()
        tm._peer_addresses["p"] = ("Peer", "127.0.0.1", 5)
        tm._schedule_reconnect("p")
        assert fake_timer.instances[-1].interval == MIN_RECONNECT_DELAY
        tm._schedule_reconnect("p")
        assert fake_timer.instances[-1].interval == max(
            MIN_RECONNECT_DELAY, min(2 ** 1, MAX_RECONNECT_BACKOFF))

    def test_no_permanent_giveup_past_max_attempts(self, fake_timer):
        tm = self._make_tm()
        tm._peer_addresses["p"] = ("Peer", "127.0.0.1", 5)
        for _ in range(MAX_RECONNECT_ATTEMPTS + 5):
            tm._schedule_reconnect("p")
        # Address retained: recovery no longer depends on an mDNS re-announce.
        assert "p" in tm._peer_addresses
        # Slow-retry interval is the fixed max backoff.
        assert fake_timer.instances[-1].interval == MAX_RECONNECT_BACKOFF
        assert fake_timer.instances[-1].started
        # Display caps attempts at the fast budget (UI shows N/N, not N+5/N).
        state = tm.get_reconnect_states()["p"]
        assert state["attempts"] == MAX_RECONNECT_ATTEMPTS
        assert state["max_attempts"] == MAX_RECONNECT_ATTEMPTS

    def test_success_clears_attempts(self, fake_timer):
        tm = self._make_tm()
        tm._peer_addresses["q"] = ("Q", "127.0.0.1", 6)
        for _ in range(MAX_RECONNECT_ATTEMPTS + 2):
            tm._schedule_reconnect("q")
        with tm._lock:
            tm._reconnect_attempts.pop("q", None)
        assert tm.get_reconnect_states() == {}

    def test_inactive_transport_does_not_schedule(self, fake_timer):
        tm = TransportManager("dev-self", "Self", 0, _StubPairing())  # _running False
        tm._peer_addresses["p"] = ("Peer", "127.0.0.1", 5)
        tm._schedule_reconnect("p")
        assert fake_timer.instances == []


# ---------------------------------------------------------------------------
# transport/discovery: hashed-id formula linkage
# ---------------------------------------------------------------------------


class TestPeerIdHashLinkage:
    @pytest.mark.parametrize("device_id", [
        "0123456789abcdef",
        "device-with-dash-and-digits-42",
        "unicode-ü-идентификатор",
        "x",
    ])
    def test_matches_discovery_formula(self, device_id):
        from internal.transport.discovery import Discovery

        assert peer_id_hash(device_id) == Discovery._hash_device_id(device_id)

    def test_is_sha256_prefix(self):
        assert peer_id_hash("abc") == hashlib.sha256(b"abc").hexdigest()[:12]
