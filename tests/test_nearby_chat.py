"""Nearby-chat service tests — consent gating, text/file roundtrips, abuse caps.

Two ChatManagers are wired together through in-memory queues: every frame one
side sends is decoded with the real codec decoder and fed into the other
side's handlers, so the tests exercise the actual wire format without any
sockets.
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import (
    CHAT_MSG_TYPES,
    UNPAIRED_GATE_MSG_TYPES,
    decode_message,
    encode_binary_chunk,
    encode_frame,
)
from internal.sync.nearby_chat import ChatManager
from internal.transport.connection import PeerConnection, TransportManager

DEV_A = "device-aaaa"
DEV_B = "device-bbbb"
FP_A = "AB:CD:EF:12:34:56:78:90:aa:bb:cc:dd:ee:ff:01:02"
FP_B = "11:22:33:44:55:66:77:88:99:00:aa:bb:cc:dd:ee:ff"


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    """Poll *predicate* to a deadline instead of sleeping fixed amounts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class LinkedPair:
    """Two ChatManagers whose send_fns deliver straight into each other."""

    def __init__(self, dir_a, dir_b):
        self.frames_a_to_b: list[bytes] = []
        self.frames_b_to_a: list[bytes] = []
        self.invites_on_b: list[dict] = []
        self.responses_on_a: list[tuple] = []

        self.a = ChatManager(DEV_A, "Device A", receive_dir=str(dir_a))
        self.b = ChatManager(DEV_B, "Device B", receive_dir=str(dir_b))

        def send_from_b(data: bytes) -> bool:
            """B's outgoing wire: delivers into A."""
            self.frames_b_to_a.append(data)
            return self._deliver(self.a, data, DEV_B)

        def send_from_a(data: bytes) -> bool:
            """A's outgoing wire: delivers into B."""
            self.frames_a_to_b.append(data)
            return self._deliver(self.b, data, DEV_A)

        self.send_from_a = send_from_a
        self.send_from_b = send_from_b
        self.b.set_on_incoming_invite(self.invites_on_b.append)
        self.a.set_on_invite_response(
            lambda sid, pid, ok: self.responses_on_a.append((sid, pid, ok)),
        )

    @staticmethod
    def _deliver(dst: ChatManager, data: bytes, sender_id: str) -> bool:
        msg = decode_message(data)
        if msg is None:
            return False
        msg_type = getattr(msg, "msg_type", "")
        if msg_type == "file_chunk":
            return dst.handle_binary_chunk(msg._raw_payload, sender_id, None)
        return dst.handle_message(msg_type, msg._raw_payload, sender_id, FP_A, None)

    def establish(self) -> str:
        """Invite → accept; returns the session id both sides share."""
        sid = self.a.start_session(DEV_B, "Device B", FP_B, self.send_from_a)
        assert sid, "start_session returned None"
        assert _wait_until(lambda: len(self.invites_on_b) == 1), "invite not seen on B"
        assert self.b.accept_invitation(sid, self.send_from_b)
        assert _wait_until(
            lambda: any(s["status"] == "active" for s in self.a.get_sessions()),
        ), "A never went active"
        return sid

    def close(self):
        self.a.shutdown()
        self.b.shutdown()


class TestInviteLifecycle:
    def setup_method(self):
        # Dirs are only needed once files flow; create throwaway ones here.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        base = __import__("pathlib").Path(self._tmp.name)
        self.pair = LinkedPair(base / "a", base / "b")

    def teardown_method(self):
        self.pair.close()
        self._tmp.cleanup()

    def test_invite_accept_activates_both_sides(self):
        sid = self.pair.establish()
        sess_a = [s for s in self.pair.a.get_sessions() if s["peer_id"] == DEV_B][0]
        sess_b = self.pair.b.get_sessions()[0]
        assert sess_a["status"] == "active"
        assert sess_b["status"] == "active"
        assert sess_a["session_id"] == sid == sess_b["session_id"]
        assert self.pair.responses_on_a == [(sid, DEV_B, True)]

    def test_decline_marks_remote_declined(self):
        sid = self.pair.a.start_session(DEV_B, "Device B", FP_B, self.pair.send_from_a)
        assert _wait_until(lambda: len(self.pair.invites_on_b) == 1)
        assert self.pair.b.decline_invitation(sid, self.pair.send_from_b)
        assert _wait_until(
            lambda: self.pair.a.get_sessions()[0]["status"] == "declined_remote",
        )
        assert self.pair.responses_on_a[-1] == (sid, DEV_B, False)

    def test_close_notifies_peer_with_system_entry(self):
        sid = self.pair.establish()
        assert self.pair.a.close_session(sid)
        assert _wait_until(lambda: any(
            e["kind"] == "system" and e["text_key"] == "chat.system.session_closed_by_peer"
            for e in self.pair.b.get_messages(sid)
        ))

    def test_duplicate_invite_over_active_session_is_idempotent(self):
        sid = self.pair.establish()
        stranger_sid = "ffffffffffffffff"
        ok = self.pair.b.handle_message(
            "chat_invite",
            {"session_id": stranger_sid, "from_name": "A", "fingerprint_short": FP_A},
            DEV_A,
            FP_A,
            self.pair.send_from_b,
        )
        assert ok is True
        # The existing session stays canonical and untouched.
        assert [s["session_id"] for s in self.pair.a.get_sessions()] == [sid]

    def test_mutual_invite_converges_to_single_session(self):
        # Both sides must be 'inviting' simultaneously, so capture frames
        # first and replay them afterwards (synchronous inline delivery
        # would resolve the second invite as a normal incoming one).
        captured: list[tuple[str, bytes]] = []

        def capture_a(data: bytes) -> bool:
            captured.append(("a", data))
            return True

        def capture_b(data: bytes) -> bool:
            captured.append(("b", data))
            return True

        sid_a = self.pair.a.start_session(DEV_B, "Device B", FP_B, capture_a)
        sid_b = self.pair.b.start_session(DEV_A, "Device A", FP_A, capture_b)
        winner = min(sid_a, sid_b)

        # Pump until the wire goes quiet (bounded rounds).
        for _ in range(len(captured) + 8):
            if not captured:
                break
            origin, data = captured.pop(0)
            if origin == "a":
                self.pair._deliver(self.pair.b, data, DEV_A)
            else:
                self.pair._deliver(self.pair.a, data, DEV_B)

        sessions_a = [s for s in self.pair.a.get_sessions() if s["status"] == "active"]
        sessions_b = [s for s in self.pair.b.get_sessions() if s["status"] == "active"]
        assert len(sessions_a) == 1 and len(sessions_b) == 1
        assert sessions_a[0]["session_id"] == winner == sessions_b[0]["session_id"]


class TestTextMessaging:
    def setup_method(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.pair = LinkedPair(base / "a", base / "b")
        self.sid = self.pair.establish()

    def teardown_method(self):
        self.pair.close()
        self._tmp.cleanup()

    def test_text_roundtrip_and_unread_count(self):
        assert self.pair.a.send_text(self.sid, "hello nearby", self.pair.send_from_a)
        assert _wait_until(lambda: any(
            e["text"] == "hello nearby" for e in self.pair.b.get_messages(self.sid)
        ))
        sess_b = self.pair.b.get_sessions()[0]
        assert sess_b["unread"] == 1
        assert sess_b["last_preview"].startswith("hello nearby")
        self.pair.b.mark_session_read(self.sid)
        assert self.pair.b.get_sessions()[0]["unread"] == 0

    def test_text_from_unknown_peer_is_dropped(self):
        before = len(self.pair.b.get_messages(self.sid))
        ok = self.pair.b.handle_message(
            "chat_text",
            {"session_id": "0000000000000000", "text": "sneak", "ts": time.time()},
            "stranger-1",
            FP_A,
            self.pair.send_from_b,
        )
        assert ok is True  # consumed by the chat router...
        # ...but nothing was appended to the real conversation.
        assert len(self.pair.b.get_messages(self.sid)) == before

    def test_text_with_mismatched_session_id_still_delivered(self):
        """A text tagged with a stale session_id from a KNOWN peer must land
        via the by-peer fallback (a lost chat_accept after a re-invite would
        otherwise blackout one direction)."""
        before = len(self.pair.b.get_messages(self.sid))
        ok = self.pair.b.handle_message(
            "chat_text",
            {"session_id": "ffffffffffffffff", "text": "still lands", "ts": time.time()},
            DEV_A,
            FP_A,
            self.pair.send_from_b,
        )
        assert ok is True
        assert any(e["text"] == "still lands" for e in self.pair.b.get_messages(self.sid))
        assert len(self.pair.b.get_messages(self.sid)) == before + 1

    def test_oversized_text_rejected_locally(self):
        long_text = "x" * (ChatManager.MAX_TEXT_LEN + 1)
        assert self.pair.a.send_text(self.sid, long_text, self.pair.send_from_a) is False

    def test_empty_text_rejected(self):
        assert self.pair.a.send_text(self.sid, "   ", self.pair.send_from_a) is False

    def test_text_marked_failed_when_transport_refuses(self):
        # A send_fn that drops the frame must surface a "failed" entry in the
        # transcript instead of pretending the message was delivered.
        drop = lambda data: False  # noqa: E731
        assert self.pair.a.send_text(self.sid, "will not arrive", drop) is False
        entry = next(
            e for e in self.pair.a.get_messages(self.sid)
            if e["text"] == "will not arrive"
        )
        assert entry["status"] == "failed"


class TestAbuseCaps:
    def setup_method(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.pair = LinkedPair(base / "a", base / "b")

    def teardown_method(self):
        self.pair.close()
        self._tmp.cleanup()

    def test_pending_invite_cap_auto_declines(self):
        # Distinct senders: one live session per peer, so the cap needs
        # several peers knocking at once.
        senders = [(f"peer-{i}", f"{i:016x}") for i in range(ChatManager.PENDING_INVITE_CAP + 2)]
        for sender_id, sid in senders:
            self.pair.b.handle_message(
                "chat_invite",
                {"session_id": sid, "from_name": sender_id, "fingerprint_short": FP_A},
                sender_id,
                FP_A,
                self.pair.send_from_b,
            )
        invited = [s for s in self.pair.b.get_sessions() if s["status"] == "invited"]
        assert len(invited) == ChatManager.PENDING_INVITE_CAP

    def test_outgoing_invite_rate_limit(self):
        # No real delivery: the send closure swallows frames, so every
        # attempt burns exactly one unit of the per-peer invite budget.
        drop = lambda data: True  # noqa: E731
        sent = 0
        for _ in range(ChatManager.INVITE_RATE_LIMIT + 2):
            sid = self.pair.a.start_session(DEV_B, "B", FP_B, drop)
            if sid is None:
                break
            sent += 1
            assert self.pair.a.close_session(sid, notify_peer=False)
        assert sent == ChatManager.INVITE_RATE_LIMIT

    def test_traversal_file_name_is_sanitized(self):
        sid = self.pair.establish()
        self.pair.b.handle_message(
            "chat_file_offer",
            {
                "session_id": sid,
                "transfer_id": "c" * 32,
                "file_name": "..\\..\\windows\\evil.txt",
                "file_size": 10,
                "mime": "",
            },
            DEV_A,
            FP_A,
            self.pair.send_from_b,
        )
        msgs = self.pair.b.get_messages(sid)
        assert msgs, "offer should still create an entry for the user to see"
        assert ".." not in msgs[-1]["file_name"]
        assert "\\" not in msgs[-1]["file_name"]

    def test_oversized_offer_rejected_without_entry(self):
        sid = self.pair.establish()
        from internal.sync.file_transfer import MAX_FILE_SIZE
        self.pair.b.handle_message(
            "chat_file_offer",
            {
                "session_id": sid,
                "transfer_id": "d" * 32,
                "file_name": "big.bin",
                "file_size": MAX_FILE_SIZE + 1,
                "mime": "",
            },
            DEV_A,
            FP_A,
            self.pair.send_from_b,
        )
        assert all(e["kind"] != "file" for e in self.pair.b.get_messages(sid))


class TestFileTransfer:
    def setup_method(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.dir_a, self.dir_b = base / "a", base / "b"
        self.pair = LinkedPair(self.dir_a, self.dir_b)
        self.sid = self.pair.establish()

    def teardown_method(self):
        self.pair.close()
        self._tmp.cleanup()

    def _make_source(self, size: int):
        from pathlib import Path
        src = self.dir_a / "source.bin"
        src.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
        return src

    def _b_entry(self, tid):
        msgs = self.pair.b.get_messages(self.sid)
        return next((e for e in msgs if e["transfer_id"] == tid), None)

    def test_file_roundtrip_preserves_bytes(self):
        src = self._make_source(ChatManager.CHUNK_SIZE * 2 + 1234)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert tid, "send_file returned None"
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        assert self.pair.b.accept_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(
            e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid
        )
        assert received["saved_path"]
        assert open(received["saved_path"], "rb").read() == src.read_bytes()

    def test_collision_rename_avoids_overwrite(self):
        src = self._make_source(2048)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        # Pre-create the destination so the receiver must rename.
        (self.dir_b / "source.bin").write_bytes(b"precious")
        assert self.pair.b.accept_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(
            e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid
        )
        assert received["saved_path"] != str(self.dir_b / "source.bin")
        assert open(self.dir_b / "source.bin", "rb").read() == b"precious"

    def test_decline_file_marks_entry_declined(self):
        src = self._make_source(1024)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        assert self.pair.b.decline_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(
            lambda: next(
                e for e in self.pair.a.get_messages(self.sid) if e["transfer_id"] == tid
            )["status"] == "declined",
        )

    def test_empty_file_transfers_via_completion_frame(self):
        src = self._make_source(0)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert tid
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        assert self.pair.b.accept_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(
            e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid
        )
        assert open(received["saved_path"], "rb").read() == b""

    def test_unknown_transfer_id_returns_false_for_chat_router(self):
        assert self.pair.b.handle_message("chat_text", {"session_id": "x" * 16}, DEV_A, FP_A, None)
        # A foreign chunk id must fall through (False) to clipboard transfers.
        assert self.pair.b.handle_binary_chunk(
            {"transfer_id": "e" * 32, "chunk_index": 0, "total_chunks": 1, "_raw_data": b""},
            DEV_A,
            None,
        ) is False

    def test_outgoing_file_cap(self):
        from pathlib import Path
        sent = []
        for i in range(ChatManager.MAX_CONCURRENT_OUTGOING_FILES + 1):
            src = Path(self.dir_a) / f"f{i}.bin"
            src.write_bytes(b"x" * 512)
            tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
            if tid is None:
                break
            sent.append(tid)
        assert len(sent) == ChatManager.MAX_CONCURRENT_OUTGOING_FILES


class TestDisconnectAndSnapshots:
    def setup_method(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.pair = LinkedPair(base / "a", base / "b")
        self.sid = self.pair.establish()

    def teardown_method(self):
        self.pair.close()
        self._tmp.cleanup()

    def test_mark_peer_disconnected_appends_system_entry(self):
        self.pair.b.mark_peer_disconnected(DEV_A)
        sess = self.pair.b.get_sessions()[0]
        assert sess["online"] is False
        assert any(
            e["text_key"] == "chat.system.peer_offline"
            for e in self.pair.b.get_messages(sess["session_id"])
        )

    def test_snapshot_shapes_are_stable(self):
        self.pair.a.send_text(self.sid, "shape check", self.pair.send_from_a)
        assert _wait_until(lambda: self.pair.b.get_sessions())
        sess = self.pair.b.get_sessions()[0]
        assert set(sess) >= {
            "session_id", "peer_id", "peer_name", "fingerprint_short",
            "status", "last_activity_ts", "unread", "online", "last_preview",
        }
        entry = self.pair.b.get_messages(sess["session_id"])[-1]
        assert set(entry) >= {
            "entry_id", "kind", "outgoing", "ts", "text", "text_key", "fmt",
            "file_name", "file_size", "mime", "status", "fraction",
            "saved_path", "transfer_id",
        }

    def test_handle_binary_chunk_ignores_foreign_ids(self):
        assert self.pair.b.handle_binary_chunk({}, DEV_A, None) is False
        assert self.pair.b.handle_binary_chunk(None, DEV_A, None) is False


class _ScriptedSocket:
    """Socket stub that replays pre-baked frames then EOF."""

    def __init__(self, frames):
        self._buf = b"".join(
            struct.pack(">I", len(f)) + f for f in frames
        )
        self._pos = 0

    def settimeout(self, timeout):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass

    def sendall(self, data):
        pass

    def recv(self, n):
        if self._pos >= len(self._buf):
            return b""
        chunk = self._buf[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _UnpairedPairingMgr:
    """Pairing manager that reports every peer as unpaired."""

    def is_peer_paired(self, peer_id):
        return False


class TestTransportGate:
    """The unpaired gate must admit chat frames AND chat file bytes while
    still blocking clipboard-transfer initiation."""

    def test_unpaired_gate_admits_chat_and_chunk_bytes_only(self):
        frames = [
            encode_frame({"msg_type": "file_request", "transfer_id": "d" * 32}),
            encode_frame({"msg_type": "chat_invite", "session_id": "c" * 16}),
            encode_binary_chunk("a" * 32, 0, 1, b"bytes"),
        ]
        conn = PeerConnection(
            "peer-1", "Peer One", _ScriptedSocket(frames),
            pairing_mgr=_UnpairedPairingMgr(),
        )
        received = []
        conn.set_on_message(lambda msg, pid: received.append(getattr(msg, "msg_type", "")))
        try:
            conn.start()
            assert _wait_until(lambda: len(received) >= 2), f"got {received}"
        finally:
            conn.stop()
        assert "chat_invite" in received
        assert "file_chunk" in received
        assert "file_request" not in received

    def test_unpaired_gate_constant_shape(self):
        assert "file_chunk" in UNPAIRED_GATE_MSG_TYPES
        assert "file_request" not in UNPAIRED_GATE_MSG_TYPES
        assert "file_ack" not in UNPAIRED_GATE_MSG_TYPES
        assert "clipboard" not in UNPAIRED_GATE_MSG_TYPES
        assert all(t in UNPAIRED_GATE_MSG_TYPES for t in CHAT_MSG_TYPES)


class TestFingerprintAccessor:
    def test_get_peer_fingerprint_reads_private_field(self):
        tm = TransportManager("dev-a", "Device A", 19999, _UnpairedPairingMgr())
        conn = PeerConnection("peer-1", "Peer One", _ScriptedSocket([]))
        conn._peer_fingerprint = "AB:CD:EF:01"
        with tm._lock:
            tm._peers["peer-1"] = conn
        assert tm.get_peer_fingerprint("peer-1") == "AB:CD:EF:01"
        assert tm.get_peer_fingerprint("ghost") == ""


class TestReceiveExpiry:
    def test_ignored_offer_expires_and_releases_cap(self):
        mgr = ChatManager("x", "X")
        try:
            mgr.handle_message(
                "chat_invite",
                {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
                "peer-a", "A1", None,
            )
            sid = mgr.get_sessions()[0]["session_id"]
            mgr.accept_invitation(sid, None)
            mgr.handle_message(
                "chat_file_offer",
                {"session_id": sid, "transfer_id": "b" * 32,
                 "file_name": "x.bin", "file_size": 100, "mime": ""},
                "peer-a", "A1", None,
            )
            assert mgr.get_messages(sid)[-1]["status"] == "await_accept"
            # Age the offer past the timeout and sweep.
            mgr._receives["b" * 32]["entry"].ts -= (ChatManager.INVITE_ACCEPT_TIMEOUT + 10)
            with mgr._lock:
                mgr._expire_stale_receives()
            assert "b" * 32 not in mgr._receives
            assert mgr.get_messages(sid)[-1]["status"] == "declined"
        finally:
            mgr.shutdown()


class TestOfflineSendGuard:
    def setup_method(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.pair = LinkedPair(base / "a", base / "b")
        self.sid = self.pair.establish()

    def teardown_method(self):
        self.pair.close()
        self._tmp.cleanup()

    def test_sends_blocked_while_offline_then_resume_after_ping(self):
        self.pair.b.mark_peer_disconnected(DEV_A)
        assert self.pair.b.get_sessions()[0]["online"] is False
        assert self.pair.b.send_text(self.sid, "nope", self.pair.send_from_b) is False
        from pathlib import Path
        src = Path(self._tmp.name) / "f.bin"
        src.write_bytes(b"data" * 64)
        assert self.pair.b.send_file(self.sid, str(src), self.pair.send_from_b) is None
        # Peer comes back (a ping from A restores liveness).
        self.pair.b.handle_message(
            "chat_ping", {"session_id": self.sid}, DEV_A, FP_A, self.pair.send_from_b,
        )
        assert self.pair.b.get_sessions()[0]["online"] is True
        assert self.pair.b.send_text(self.sid, "hi again", self.pair.send_from_b) is True


class TestStalledTransferSweep:
    """#2: transfers that stop making progress must be failed and their
    ``.part`` temp files removed instead of leaking forever."""

    def _active_session(self, mgr):
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a", "A1", None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, None)
        return sid

    def test_stalled_receive_is_failed_and_temp_file_removed(self):
        import tempfile
        mgr = ChatManager("x", "X")
        tmp = tempfile.TemporaryDirectory()
        tid = "b" * 32
        try:
            mgr.set_receive_dir(tmp.name)
            sid = self._active_session(mgr)
            mgr.handle_message(
                "chat_file_offer",
                {"session_id": sid, "transfer_id": tid,
                 "file_name": "x.bin", "file_size": 100, "mime": ""},
                "peer-a", "A1", None,
            )
            # Accepting opens the temp file; the wire ack itself is not needed.
            # Use a working send_fn so the accept succeeds and the receive
            # state persists (a failed accept now rolls the state back —
            # that's the point of the rollback fix).
            mgr.accept_file(sid, tid, lambda data: True)
            temp_path = mgr._receives[tid]["temp_path"]
            assert temp_path is not None and temp_path.exists()
            # Age the receive past the stall timeout so the sweep fails it.
            mgr._receives[tid]["last_progress_mono"] = (
                time.monotonic() - (ChatManager.TRANSFER_STALL_TIMEOUT + 10)
            )
            fired: list = []
            with mgr._lock:
                done = mgr._expire_stale_transfers(fired)
            assert tid not in mgr._receives
            assert not temp_path.exists()
            assert mgr.get_messages(sid)[-1]["kind"] == "system"
            assert any(d[0] == sid and d[1] == tid for d in done)
            assert any(m[0] == sid for m in fired)
        finally:
            mgr.shutdown()
            tmp.cleanup()


class TestTextRateBudget:
    """#4: text flood budgets are per-direction and failed sends don't burn slots."""

    def _active_session(self, mgr):
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a", "A1", None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, None)
        return sid

    def test_incoming_flood_does_not_consume_outgoing_budget(self):
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            # Flood the INCOMING budget up to its cap.
            for i in range(ChatManager.TEXT_RATE_LIMIT):
                mgr.handle_message(
                    "chat_text",
                    {"session_id": sid, "text": f"in-{i}", "ts": time.time()},
                    "peer-a", "A1", None,
                )
            # The 31st incoming text is consumed by the router but dropped.
            mgr.handle_message(
                "chat_text",
                {"session_id": sid, "text": "flooded-out", "ts": time.time()},
                "peer-a", "A1", None,
            )
            assert not any(e["text"] == "flooded-out" for e in mgr.get_messages(sid))
            # The OUTGOING budget is untouched, so our own send still works.
            assert mgr.send_text(sid, "still here", lambda data: True) is True
            assert any(e["text"] == "still here" for e in mgr.get_messages(sid))
        finally:
            mgr.shutdown()

    def test_failed_send_rolls_back_rate_limit_slot(self):
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            drop = lambda data: False  # noqa: E731
            # Send TEXT_RATE_LIMIT + 1 texts that all fail to transmit.  Without
            # the rollback the first 30 failures would exhaust the whole budget.
            for i in range(ChatManager.TEXT_RATE_LIMIT + 1):
                assert mgr.send_text(sid, f"fail-{i}", drop) is False
            # The failed sends must not have burned any outgoing slots.
            assert mgr.send_text(sid, "real", lambda data: True) is True
        finally:
            mgr.shutdown()


class TestR6AuditRegressions:
    """Regression tests for the deep-audit adversarial review fixes."""

    def _active_session(self, mgr):
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a", "A1", None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, None)
        return sid

    def test_offline_gate_ignores_stalled_transfer(self):
        """A transfer with stale progress must not pin the peer 'online'."""
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            mgr.handle_message(
                "chat_file_offer",
                {"session_id": sid, "transfer_id": "a" * 32,
                 "file_name": "x.bin", "file_size": 100, "mime": ""},
                "peer-a", "A1", None,
            )
            sess = mgr._sessions["peer-a"]
            # Stale progress -> NOT an active transfer -> offline detection
            # is allowed to proceed instead of waiting out the stall window.
            mgr._receives["a" * 32]["last_progress_mono"] = (
                time.monotonic() - (ChatManager.TRANSFER_STALL_TIMEOUT + 10)
            )
            with mgr._lock:
                assert mgr._session_has_active_transfer(sess) is False
            # Fresh progress -> still counts as in-flight.
            mgr._receives["a" * 32]["last_progress_mono"] = time.monotonic()
            with mgr._lock:
                assert mgr._session_has_active_transfer(sess) is True
        finally:
            mgr.shutdown()

    def test_expired_offer_fires_done_callback(self):
        """An offer the user never answered must notify the UI (declined) so
        the Accept/Decline card stops offering dead actions."""
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            done_events = []
            mgr.set_on_file_done(
                lambda s, tid, ok, path, st: done_events.append((tid, ok, st)),
            )
            mgr.handle_message(
                "chat_file_offer",
                {"session_id": sid, "transfer_id": "b" * 32,
                 "file_name": "x.bin", "file_size": 100, "mime": ""},
                "peer-a", "A1", None,
            )
            mgr._receives["b" * 32]["entry"].ts -= (
                ChatManager.INVITE_ACCEPT_TIMEOUT + 10
            )
            with mgr._lock:
                mgr._expire_stale_receives()
            assert "b" * 32 not in mgr._receives
            assert done_events == [("b" * 32, False, "declined")]
        finally:
            mgr.shutdown()

    def test_fail_receive_notifies_sender(self):
        """A disk-error on the receive side must send an error chat_file_complete
        so the sender doesn't report a false 'delivered'."""
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            sent = []
            mgr._latest_send_fn["peer-a"] = sent.append
            mgr.handle_message(
                "chat_file_offer",
                {"session_id": sid, "transfer_id": "c" * 32,
                 "file_name": "x.bin", "file_size": 100, "mime": ""},
                "peer-a", "A1", None,
            )
            with mgr._lock:
                mgr._fail_receive_locked("c" * 32, "error_disk")
            # The sender must receive an error frame (not silence).
            assert sent, "receiver emitted no chat_file_complete error frame"
            import json
            from internal.protocol.codec import decode_message
            decoded = decode_message(sent[0])
            assert decoded._raw_payload["status"] == "error_disk"
        finally:
            mgr.shutdown()
