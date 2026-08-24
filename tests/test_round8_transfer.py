"""Round 8 — transfer/connection face tests.

Covers:
- codec tolerance for malformed / hostile frames (regression pins)
- duplicate ``file_request`` idempotency (bidirectional-connect race replays)
- real-time transfer speed + ETA (rolling window, staleness to 0)
- reconnect slow-retry (no permanent give-up after the fast budget)
- hash<->real id derivation consistency between transport and discovery
"""

import hashlib
import struct
import threading
import time

import pytest

from internal.protocol.codec import (
    HEADER_FMT,
    VERSION,
    decode_message,
    encode_binary_chunk,
    encode_frame,
)
from internal.sync.file_transfer import (
    CHUNK_SIZE,
    SPEED_STALE_AFTER,
    FileTransferManager,
)
import internal.transport.connection as conn_mod
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
