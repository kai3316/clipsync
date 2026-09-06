"""Nearby-chat service tests — consent gating, text/file roundtrips, abuse caps.

Two ChatManagers are wired together through in-memory queues: every frame one
side sends is decoded with the real codec decoder and fed into the other
side's handlers, so the tests exercise the actual wire format without any
sockets.
"""

import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import (
    CHAT_MSG_TYPES,
    UNPAIRED_GATE_MSG_TYPES,
    decode_message,
    encode_binary_chunk,
    encode_frame,
)
from internal.sync.nearby_chat import ChatFileTooLargeError, ChatManager
from internal.transport.connection import PeerConnection, TransportManager
from internal.transport.relay import MAX_RELAY_PAYLOAD

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
        assert _wait_until(
            lambda: any(
                e["kind"] == "system" and e["text_key"] == "chat.system.session_closed_by_peer"
                for e in self.pair.b.get_messages(sid)
            )
        )

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
        assert _wait_until(
            lambda: any(e["text"] == "hello nearby" for e in self.pair.b.get_messages(self.sid))
        )
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
        # Dropped (unknown peer, no active session) -> the host must NOT
        # send a relay_ack "delivered" receipt for it...
        assert ok is False
        # ...and nothing was appended to the real conversation.
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
            e for e in self.pair.a.get_messages(self.sid) if e["text"] == "will not arrive"
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
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert received["saved_path"]
        assert open(received["saved_path"], "rb").read() == src.read_bytes()  # noqa: SIM115

    def test_collision_rename_avoids_overwrite(self):
        src = self._make_source(2048)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        # Pre-create the destination so the receiver must rename.
        (self.dir_b / "source.bin").write_bytes(b"precious")
        assert self.pair.b.accept_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert received["saved_path"] != str(self.dir_b / "source.bin")
        assert open(self.dir_b / "source.bin", "rb").read() == b"precious"  # noqa: SIM115

    def test_decline_file_marks_entry_declined(self):
        src = self._make_source(1024)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        assert self.pair.b.decline_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(
            lambda: (
                next(e for e in self.pair.a.get_messages(self.sid) if e["transfer_id"] == tid)[
                    "status"
                ]
                == "declined"
            ),
        )

    def test_empty_file_transfers_via_completion_frame(self):
        src = self._make_source(0)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert tid
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        assert self.pair.b.accept_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert open(received["saved_path"], "rb").read() == b""  # noqa: SIM115

    def test_unknown_transfer_id_returns_false_for_chat_router(self):
        # No active session for this peer -> dropped, so no relay_ack is sent.
        assert (
            self.pair.b.handle_message("chat_text", {"session_id": "x" * 16}, DEV_A, FP_A, None)
            is False
        )
        # A foreign chunk id must fall through (False) to clipboard transfers.
        assert (
            self.pair.b.handle_binary_chunk(
                {"transfer_id": "e" * 32, "chunk_index": 0, "total_chunks": 1, "_raw_data": b""},
                DEV_A,
                None,
            )
            is False
        )

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


class TestInternetRelayFileTransfers:
    """Internet-only peers ship chat files through the relay.

    The send_fn carries the relay chunk size + file cap as function
    attributes (tagged by main._chat_send_fn for peers with no live LAN
    connection).  Chunks must fit inside MAX_RELAY_PAYLOAD and the receiver
    must seek with the advertised chunk size.
    """

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

    def _relay_fn(self):
        # Mirrors main._chat_send_fn's tagging for an internet-only peer.
        def fn(data: bytes) -> bool:
            return self.pair.send_from_a(data)

        fn.chunk_size = ChatManager.RELAY_CHUNK_SIZE
        fn.internet_cap = ChatManager.RELAY_FILE_CAP
        return fn

    def _source(self, size: int):
        src = self.dir_a / "source.bin"
        src.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
        return src

    def _b_entry(self, tid):
        msgs = self.pair.b.get_messages(self.sid)
        return next((e for e in msgs if e["transfer_id"] == tid), None)

    def _offer_chunk_size(self) -> int:
        for frame in self.pair.frames_a_to_b:
            msg = decode_message(frame)
            payload = getattr(msg, "_raw_payload", None)
            if payload and payload.get("msg_type") == "chat_file_offer":
                return int(payload.get("chunk_size") or 0)
        raise AssertionError("no chat_file_offer frame captured")

    def test_internet_file_uses_relay_chunk_size_and_preserves_bytes(self):
        src = self._source(ChatManager.RELAY_CHUNK_SIZE * 2 + 1234)
        tid = self.pair.a.send_file(self.sid, str(src), self._relay_fn())
        assert tid, "send_file returned None"
        assert self._offer_chunk_size() == ChatManager.RELAY_CHUNK_SIZE
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "await_accept")
        assert self.pair.b.accept_file(self.sid, tid, self.pair.send_from_b)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        # Every binary chunk frame must fit inside the relay payload cap.
        for frame in self.pair.frames_a_to_b:
            msg = decode_message(frame)
            if getattr(msg, "msg_type", "") == "file_chunk":
                assert len(frame) <= MAX_RELAY_PAYLOAD, (
                    f"chunk frame {len(frame)}B exceeds relay cap"
                )
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert open(received["saved_path"], "rb").read() == src.read_bytes()  # noqa: SIM115

    def test_internet_file_cap_refused(self):
        src = self._source(ChatManager.RELAY_FILE_CAP + 1)
        try:
            self.pair.a.send_file(self.sid, str(src), self._relay_fn())
        except ChatFileTooLargeError:
            pass
        else:
            raise AssertionError("expected ChatFileTooLargeError")
        # No offer frame should have left the sender.
        assert not any(
            (getattr(decode_message(f), "_raw_payload", {}) or {}).get("msg_type")
            == "chat_file_offer"
            for f in self.pair.frames_a_to_b
        )

    def test_lan_untagged_fn_keeps_default_chunk_size(self):
        # A dual/LAN peer's send_fn carries no tag → LAN wire format unchanged.
        src = self._source(ChatManager.CHUNK_SIZE + 1234)
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert tid
        assert self._offer_chunk_size() == ChatManager.CHUNK_SIZE

    def test_bogus_chunk_size_in_offer_falls_back_to_default(self):
        # A corrupted/attacker-advertised chunk_size must not be trusted for
        # seeks; the receiver falls back to CHUNK_SIZE when parsing the offer.
        import math

        payload = {
            "msg_type": "chat_file_offer",
            "session_id": self.sid,
            "transfer_id": "f" * 32,
            "file_name": "bogus.bin",
            "file_size": 1024,
            "chunk_size": "not-a-number",
            "mime": "",
        }
        assert (
            self.pair.b.handle_message(
                "chat_file_offer", payload, DEV_A, FP_A, self.pair.send_from_b
            )
            is True
        )
        entry = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == "f" * 32)
        assert entry["status"] == "await_accept"
        recv = self.pair.b._receives.get("f" * 32)
        assert recv is not None
        assert recv["chunk_size"] == ChatManager.CHUNK_SIZE
        assert recv["total_chunks"] == math.ceil(1024 / ChatManager.CHUNK_SIZE)
        # Zero / negative / oversized values are treated the same way.  Each
        # iteration needs a FRESH transfer_id: a second offer for an id already
        # in _receives is rejected as a duplicate, which would make this loop
        # test the dedup guard instead of the chunk_size fallback.  The slot is
        # freed afterwards so a later iteration isn't rejected by the
        # MAX_CONCURRENT_INCOMING_FILES guard either.
        for i, bogus in enumerate((0, -7, ChatManager.CHUNK_SIZE + 1)):
            tid = (f"{i:02x}").ljust(32, "e")[:32]  # unique per iteration
            recv2_payload = dict(payload, chunk_size=bogus, transfer_id=tid)
            assert (
                self.pair.b.handle_message(
                    "chat_file_offer", recv2_payload, DEV_A, FP_A, self.pair.send_from_b
                )
                is True
            )
            recv2 = self.pair.b._receives.get(tid)
            assert recv2 is not None and recv2["chunk_size"] == ChatManager.CHUNK_SIZE
            self.pair.b._receives.pop(tid, None)


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
            "session_id",
            "peer_id",
            "peer_name",
            "fingerprint_short",
            "status",
            "last_activity_ts",
            "unread",
            "online",
            "last_preview",
        }
        entry = self.pair.b.get_messages(sess["session_id"])[-1]
        assert set(entry) >= {
            "entry_id",
            "kind",
            "outgoing",
            "ts",
            "text",
            "text_key",
            "fmt",
            "file_name",
            "file_size",
            "mime",
            "status",
            "fraction",
            "saved_path",
            "transfer_id",
        }

    def test_handle_binary_chunk_ignores_foreign_ids(self):
        assert self.pair.b.handle_binary_chunk({}, DEV_A, None) is False
        assert self.pair.b.handle_binary_chunk(None, DEV_A, None) is False


class _ScriptedSocket:
    """Socket stub that replays pre-baked frames then EOF."""

    def __init__(self, frames):
        self._buf = b"".join(struct.pack(">I", len(f)) + f for f in frames)
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
        chunk = self._buf[self._pos : self._pos + n]
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
            "peer-1",
            "Peer One",
            _ScriptedSocket(frames),
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
                "peer-a",
                "A1",
                None,
            )
            sid = mgr.get_sessions()[0]["session_id"]
            # A working channel: since the honest-accept fix, activation only
            # commits when the chat_accept frame actually goes out.
            mgr.accept_invitation(sid, lambda data: True)
            mgr.handle_message(
                "chat_file_offer",
                {
                    "session_id": sid,
                    "transfer_id": "b" * 32,
                    "file_name": "x.bin",
                    "file_size": 100,
                    "mime": "",
                },
                "peer-a",
                "A1",
                None,
            )
            assert mgr.get_messages(sid)[-1]["status"] == "await_accept"
            # Age the offer past the timeout and sweep.
            mgr._receives["b" * 32]["entry"].ts -= ChatManager.INVITE_ACCEPT_TIMEOUT + 10
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
            "chat_ping",
            {"session_id": self.sid},
            DEV_A,
            FP_A,
            self.pair.send_from_b,
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
            "peer-a",
            "A1",
            None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, lambda data: True)  # honest-accept fix: ack must go out
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
                {
                    "session_id": sid,
                    "transfer_id": tid,
                    "file_name": "x.bin",
                    "file_size": 100,
                    "mime": "",
                },
                "peer-a",
                "A1",
                None,
            )
            # Accepting opens the temp file; the wire ack itself is not needed.
            # Use a working send_fn so the accept succeeds and the receive
            # state persists (a failed accept now rolls the state back —
            # that's the point of the rollback fix).
            mgr.accept_file(sid, tid, lambda data: True)
            temp_path = mgr._receives[tid]["temp_path"]
            assert temp_path is not None and temp_path.exists()
            # Age the receive past the stall timeout so the sweep fails it.
            mgr._receives[tid]["last_progress_mono"] = time.monotonic() - (
                ChatManager.TRANSFER_STALL_TIMEOUT + 10
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
            "peer-a",
            "A1",
            None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, lambda data: True)  # honest-accept fix: ack must go out
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
                    "peer-a",
                    "A1",
                    None,
                )
            # The 31st incoming text is consumed by the router but dropped.
            mgr.handle_message(
                "chat_text",
                {"session_id": sid, "text": "flooded-out", "ts": time.time()},
                "peer-a",
                "A1",
                None,
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
            "peer-a",
            "A1",
            None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, lambda data: True)  # honest-accept fix: ack must go out
        return sid

    def test_offline_gate_ignores_stalled_transfer(self):
        """A transfer with stale progress must not pin the peer 'online'."""
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            mgr.handle_message(
                "chat_file_offer",
                {
                    "session_id": sid,
                    "transfer_id": "a" * 32,
                    "file_name": "x.bin",
                    "file_size": 100,
                    "mime": "",
                },
                "peer-a",
                "A1",
                None,
            )
            sess = mgr._sessions["peer-a"]
            # Stale progress -> NOT an active transfer -> offline detection
            # is allowed to proceed instead of waiting out the stall window.
            mgr._receives["a" * 32]["last_progress_mono"] = time.monotonic() - (
                ChatManager.TRANSFER_STALL_TIMEOUT + 10
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
                {
                    "session_id": sid,
                    "transfer_id": "b" * 32,
                    "file_name": "x.bin",
                    "file_size": 100,
                    "mime": "",
                },
                "peer-a",
                "A1",
                None,
            )
            mgr._receives["b" * 32]["entry"].ts -= ChatManager.INVITE_ACCEPT_TIMEOUT + 10
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
                {
                    "session_id": sid,
                    "transfer_id": "c" * 32,
                    "file_name": "x.bin",
                    "file_size": 100,
                    "mime": "",
                },
                "peer-a",
                "A1",
                None,
            )
            with mgr._lock:
                mgr._fail_receive_locked("c" * 32, "error_disk")
            # The sender must receive an error frame (not silence).
            assert sent, "receiver emitted no chat_file_complete error frame"

            from internal.protocol.codec import decode_message

            decoded = decode_message(sent[0])
            assert decoded._raw_payload["status"] == "error_disk"
        finally:
            mgr.shutdown()


# ══════════════════════════════════════════════════
# merged from test_nearby_chat_e2e.py
# ══════════════════════════════════════════════════

import os
import socket
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.pairing import PairingManager  # noqa: E402
from internal.sync.nearby_chat import ChatManager  # noqa: E402
from internal.transport.connection import PortInUseError  # noqa: E402

NAME_A = "Device A"
NAME_B = "Device B"

# ~1.5 MB source file: comfortably more than one 256 KB chat chunk.
FILE_BYTES = 1500 * 1024


def _free_port() -> int:
    """Return a currently-free ephemeral TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _deadline(pred, timeout: float = 10.0, interval: float = 0.02) -> bool:
    """Poll *pred* to a deadline instead of sleeping a fixed amount."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class _Stack:
    """One full transport + chat stack on its own ephemeral localhost port.

    ``route`` implements the host router contract: decoded ``CHAT_MSG_TYPES``
    frames go to ``handle_message``, ``file_chunk`` frames to
    ``handle_binary_chunk`` (chat right-of-first-refusal), everything else is
    ignored.  ``send_fn_to`` is a closure that pushes bytes to one peer over
    the transport, i.e. over the real TLS connection.
    """

    def __init__(self, device_id: str, device_name: str, receive_dir):
        self.pairing = PairingManager(device_id, device_name)
        self.pairing.load_or_create_identity("", "")
        self.chat = ChatManager(device_id, device_name, receive_dir=str(receive_dir))
        self.chat.set_own_fingerprint(self.pairing.get_identity().fingerprint)
        self.transport = TransportManager(device_id, device_name, _free_port(), self.pairing)
        self.transport.set_on_peer_message(self._route)
        self.incoming_invites: list[dict] = []
        self.chat.set_on_incoming_invite(self.incoming_invites.append)

    # ---- transport → chat wiring (mirrors the host router contract) --------

    def _route(self, msg, peer_id):
        send_fn = lambda data, p=peer_id: self.transport.send_to_peer(p, data)  # noqa: E731
        fp = self.transport.get_peer_fingerprint(peer_id)
        fp_short = self.chat.shorten_fingerprint(fp)
        msg_type = getattr(msg, "msg_type", "")
        if msg_type == "file_chunk":
            return self.chat.handle_binary_chunk(msg._raw_payload, peer_id, send_fn)
        if msg_type in CHAT_MSG_TYPES:
            return self.chat.handle_message(
                msg_type,
                msg._raw_payload,
                peer_id,
                fp_short,
                send_fn,
            )
        return False

    def send_fn_to(self, peer_id):
        return lambda data, p=peer_id: self.transport.send_to_peer(p, data)  # noqa: E731

    @property
    def port(self) -> int:
        return self.transport._port

    def start(self):
        """Bind the server, retrying through any ephemeral-port race."""
        for _ in range(25):
            try:
                self.transport.start_server()
                return
            except PortInUseError:
                self.transport._port = _free_port()
        pytest.skip("could not bind an ephemeral localhost port for the E2E test")

    def stop(self):
        try:
            self.chat.shutdown()
        finally:
            self.transport.stop_server()


class TestNearbyChatE2E:
    """Real-TLS nearby-chat roundtrips between two unpaired devices."""

    @pytest.fixture
    def rig(self, tmp_path):
        dir_a = tmp_path / "receive_a"
        dir_b = tmp_path / "receive_b"
        a = _Stack(DEV_A, NAME_A, dir_a)
        b = _Stack(DEV_B, NAME_B, dir_b)
        try:
            a.start()
            b.start()
        except OSError as e:
            a.stop()
            b.stop()
            pytest.skip(f"environment cannot bind localhost sockets: {e}")

        # A dials B directly over 127.0.0.1 (no mDNS in this test).
        a.transport.connect_to_peer(DEV_B, NAME_B, "127.0.0.1", b.port)
        assert _deadline(
            lambda: DEV_B in a.transport.get_connected_peers(),
            timeout=10,
        ), "A never connected to B"
        assert _deadline(
            lambda: DEV_A in b.transport.get_connected_peers(),
            timeout=10,
        ), "B never saw A as connected"

        rig = SimpleNamespace(a=a, b=b, dir_a=dir_a, dir_b=dir_b)
        yield rig
        a.stop()
        b.stop()

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _status(stack: _Stack, peer_id: str) -> str | None:
        for s in stack.chat.get_sessions():
            if s["peer_id"] == peer_id:
                return s["status"]
        return None

    @staticmethod
    def _entry(stack: _Stack, sid: str, tid: str) -> dict | None:
        for e in stack.chat.get_messages(sid):
            if e["transfer_id"] == tid:
                return e
        return None

    # ---- tests -------------------------------------------------------------

    def test_unpaired_tls_connection_establishes(self, rig):
        assert DEV_B in rig.a.transport.get_connected_peers()
        assert DEV_A in rig.b.transport.get_connected_peers()
        # Both devices know each other's certs but never paired.
        assert not rig.a.pairing.is_peer_paired(DEV_B)
        assert not rig.b.pairing.is_peer_paired(DEV_A)

    def test_chat_invite_text_file_roundtrip_over_tls(self, rig):
        a, b = rig.a, rig.b

        # --- 1. invite → accept, both sessions active ---------------------
        sid = a.chat.start_session(DEV_B, NAME_B, "", a.send_fn_to(DEV_B))
        assert sid, "start_session returned None"
        assert _deadline(lambda: len(b.incoming_invites) == 1), (
            "chat invite never reached B over TLS"
        )
        invite = b.incoming_invites[0]
        assert invite["peer_id"] == DEV_A
        assert invite["session_id"] == sid
        assert b.chat.accept_invitation(invite["session_id"], b.send_fn_to(DEV_A))
        assert _deadline(lambda: self._status(a, DEV_B) == "active"), (
            "A session never became active"
        )
        assert _deadline(lambda: self._status(b, DEV_A) == "active"), (
            "B session never became active"
        )

        # --- 2. text roundtrip ---------------------------------------------
        assert a.chat.send_text(sid, "hello over TLS", a.send_fn_to(DEV_B))
        assert _deadline(
            lambda: any(
                e["kind"] == "text" and e["text"] == "hello over TLS"
                for e in b.chat.get_messages(sid)
            )
        ), "chat text never reached B"

        # --- 3. multi-chunk file roundtrip ---------------------------------
        payload = (bytes(range(256)) * (FILE_BYTES // 256)) + b"e2e-tail"
        src = rig.dir_a / "payload.bin"
        src.write_bytes(payload)

        tid = a.chat.send_file(sid, str(src), a.send_fn_to(DEV_B))
        assert tid, "send_file returned None"
        assert _deadline(
            lambda: (self._entry(b, sid, tid) or {}).get("status") == "await_accept"
        ), "file offer never reached B"
        assert b.chat.accept_file(sid, tid, b.send_fn_to(DEV_A))
        assert _deadline(
            lambda: (self._entry(b, sid, tid) or {}).get("status") == "done", timeout=20
        ), "B never finalized the received file"
        saved = (self._entry(b, sid, tid) or {}).get("saved_path")
        assert saved, "B did not record a saved path"
        assert os.path.getsize(saved) == len(payload)
        with open(saved, "rb") as fh:
            assert fh.read() == payload, "received file bytes differ from source"

        # Sender converges to "done" once the receiver's completion ack lands.
        assert _deadline(
            lambda: (self._entry(a, sid, tid) or {}).get("status") == "done", timeout=10
        ), "A never marked the send done"

        # --- 4. never paired, anywhere, at any point -----------------------
        assert not a.pairing.is_peer_paired(DEV_B)
        assert not b.pairing.is_peer_paired(DEV_A)


# ══════════════════════════════════════════════════
# merged from test_round9_chat.py
# ══════════════════════════════════════════════════

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import (
    FILE_TRANSFER_MSG_TYPES,
    PAIRING_MSG_TYPES,
)
from internal.sync.nearby_chat import ChatManager
from internal.web.api import chat as chat_api
from internal.web.routes import dispatch

PEER = "peer-b"


def _active_mgr() -> ChatManager:
    """A ChatManager with one ACTIVE session (incoming invite accepted)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "B", "fingerprint_short": "B1"},
        PEER,
        "B1",
        None,
    )
    sid = mgr.get_sessions()[0]["session_id"]
    assert mgr.accept_invitation(sid, lambda data: True)
    return mgr


# ══════════════════════════════════════════════════════════════════
# Codec: wire format + old-peer compatibility
# ══════════════════════════════════════════════════════════════════


class TestCodecChatTyping:
    def test_roundtrip(self):
        payload = {"msg_type": "chat_typing", "session_id": "a" * 16, "typing": True}
        msg = decode_message(encode_frame(payload))
        assert msg is not None
        assert msg.msg_type == "chat_typing"
        assert msg._raw_payload["session_id"] == "a" * 16
        assert msg._raw_payload["typing"] is True

    def test_admitted_by_gate_and_router_sets(self):
        # New versions must let the frame through the unpaired gate and route
        # it to ChatManager.handle_message (which keys off CHAT_MSG_TYPES).
        assert "chat_typing" in CHAT_MSG_TYPES
        assert "chat_typing" in UNPAIRED_GATE_MSG_TYPES

    def test_old_peer_tolerance_empty_clipboard_payload(self):
        """A legacy device that doesn't know the type must not crash or sync.

        Old gate: drops unknown types (connection kept).  Old router: falls
        through to clipboard handling — safe because the decoded content has
        NO types, which SyncManager discards via content.is_empty().
        """
        payload = {"msg_type": "chat_typing", "session_id": "a" * 16, "typing": True}
        msg = decode_message(encode_frame(payload))
        assert msg.content.is_empty(), "legacy peers rely on empty content dropping"
        # Not mistaken for a file-transfer / pairing type by an old router.
        assert msg.msg_type not in FILE_TRANSFER_MSG_TYPES
        assert msg.msg_type not in PAIRING_MSG_TYPES
        # A string msg_type keeps every legacy `in <frozenset>` router happy.


# ══════════════════════════════════════════════════════════════════
# Sender side: report_typing throttle
# ══════════════════════════════════════════════════════════════════


class TestReportTyping:
    def setup_method(self):
        self.sent = []

        def wire(data: bytes) -> bool:
            self.sent.append(decode_message(data))
            return True

        self.wire = wire
        self.mgr = _active_mgr()
        self.sid = self.mgr.get_sessions()[0]["session_id"]

    def teardown_method(self):
        self.mgr.shutdown()

    def _typing_frames(self):
        return [m for m in self.sent if getattr(m, "msg_type", "") == "chat_typing"]

    def test_first_report_sends_frame(self):
        assert self.mgr.report_typing(self.sid, True, self.wire) is True
        frames = self._typing_frames()
        assert len(frames) == 1
        assert frames[0]._raw_payload == {
            "msg_type": "chat_typing",
            "session_id": self.sid,
            "typing": True,
        }

    def test_duplicate_same_state_within_throttle_suppressed(self):
        assert self.mgr.report_typing(self.sid, True, self.wire) is True
        # Immediate duplicates are suppressed (no frame, returns False).
        assert self.mgr.report_typing(self.sid, True, self.wire) is False
        assert self.mgr.report_typing(self.sid, True, self.wire) is False
        assert len(self._typing_frames()) == 1  # only the first went out

    def test_state_change_bypasses_throttle_immediately(self):
        self.mgr.report_typing(self.sid, True, self.wire)
        assert self.mgr.report_typing(self.sid, False, self.wire) is True
        states = [m._raw_payload["typing"] for m in self._typing_frames()]
        assert states == [True, False]

    def test_keepalive_after_throttle_window(self):
        self.mgr.report_typing(self.sid, True, self.wire)
        # Age the bookkeeping past TYPING_THROTTLE without sleeping.
        rec = self.mgr._typing_out[self.sid]
        self.mgr._typing_out[self.sid] = (rec[0], rec[1] - 3.0)
        assert self.mgr.report_typing(self.sid, True, self.wire) is True
        assert len(self._typing_frames()) == 2

    def test_refuses_unknown_or_inactive_session(self):
        assert self.mgr.report_typing("0" * 16, True, self.wire) is False
        # Close the session: no longer active -> refused.
        assert self.mgr.close_session(self.sid)
        assert self.mgr.report_typing(self.sid, True, self.wire) is False
        assert all(getattr(m, "msg_type", "") != "chat_typing" for m in self.sent)


# ══════════════════════════════════════════════════════════════════
# Receiver side: snapshot flag, lazy expiry, clear-on-text, no push storm
# ══════════════════════════════════════════════════════════════════


class TestReceiveTyping:
    def setup_method(self):
        self.mgr = _active_mgr()
        self.sid = self.mgr.get_sessions()[0]["session_id"]
        self.pushes = []
        self.mgr.set_on_sessions_changed(lambda: self.pushes.append(1))

    def teardown_method(self):
        self.mgr.shutdown()

    def _snapshot(self):
        return [s for s in self.mgr.get_sessions() if s["session_id"] == self.sid][0]

    def _send_typing(self, typing):
        self.mgr.handle_message(
            "chat_typing",
            {"session_id": self.sid, "typing": typing},
            PEER,
            "B1",
            None,
        )

    def test_flag_visible_in_snapshot_and_flips_fire_once(self):
        assert self._snapshot()["peer_typing"] is False
        self._send_typing(True)
        assert self._snapshot()["peer_typing"] is True
        assert len(self.pushes) == 1  # visible flip
        # Keep-alives refresh the deadline but must NOT re-push.
        self._send_typing(True)
        self._send_typing(True)
        assert len(self.pushes) == 1
        # Explicit stop clears and pushes once more.
        self._send_typing(False)
        assert self._snapshot()["peer_typing"] is False
        assert len(self.pushes) == 2

    def test_lazy_timeout_self_clears_without_any_thread(self):
        self._send_typing(True)
        assert self._snapshot()["peer_typing"] is True
        # Simulate the deadline passing: to_dict compares lazily, so no
        # sweeper thread is needed to make the flag disappear.
        sess = self.mgr._session_by_sid[self.sid]
        sess.peer_typing_until_mono = time.monotonic() - 0.01
        assert self._snapshot()["peer_typing"] is False

    def test_incoming_text_clears_indicator_immediately(self):
        self._send_typing(True)
        assert self._snapshot()["peer_typing"] is True
        self.mgr.handle_message(
            "chat_text",
            {"session_id": self.sid, "text": "hi", "ts": time.time()},
            PEER,
            "B1",
            None,
        )
        assert self._snapshot()["peer_typing"] is False

    def test_unknown_peer_cannot_raise_flag(self):
        before = len(self.pushes)
        self.mgr.handle_message(
            "chat_typing",
            {"session_id": "0" * 16, "typing": True},
            "stranger-dev",
            "S1",
            None,
        )
        assert self._snapshot()["peer_typing"] is False
        assert len(self.pushes) == before

    def test_stale_sid_from_known_peer_falls_back_to_their_session(self):
        # Mirrors chat_text's by-peer fallback: session-id adoption after a
        # re-invite can desync the two sides, and a typing frame must not be
        # lost to it (same resolution order as text/ping/close).
        self.mgr.handle_message(
            "chat_typing",
            {"session_id": "0" * 16, "typing": True},
            PEER,
            "B1",
            None,
        )
        assert self._snapshot()["peer_typing"] is True


# ══════════════════════════════════════════════════════════════════
# End-to-end over the real codec (two managers wired in memory)
# ══════════════════════════════════════════════════════════════════


class TestEndToEndPair:
    def test_typing_flows_between_two_managers(self):
        import tempfile

        with tempfile.TemporaryDirectory():
            # Two managers wired through the real codec: every frame one side
            # sends is decoded and fed into the other side's handlers.
            mgr_a = ChatManager("dev-a", "A")
            mgr_b = ChatManager("dev-b", "B")

            def deliver_b(data: bytes) -> bool:
                msg = decode_message(data)
                return mgr_b.handle_message(
                    getattr(msg, "msg_type", ""),
                    msg._raw_payload,
                    "dev-a",
                    "FP",
                    None,
                )

            def deliver_a(data: bytes) -> bool:
                msg = decode_message(data)
                return mgr_a.handle_message(
                    getattr(msg, "msg_type", ""),
                    msg._raw_payload,
                    "dev-b",
                    "FP",
                    None,
                )

            try:
                sid = mgr_a.start_session("dev-b", "B", "FP", deliver_b)
                assert mgr_b.accept_invitation(sid, deliver_a)
                assert any(s["status"] == "active" for s in mgr_a.get_sessions()), (
                    "pair never activated"
                )

                # A types -> B sees the flag.
                assert mgr_a.report_typing(sid, True, deliver_b) is True
                sess_b = [s for s in mgr_b.get_sessions() if s["session_id"] == sid][0]
                assert sess_b["peer_typing"] is True

                # B's view lives only while refreshed: expire it artificially,
                # then A's text arrives and would clear it anyway.
                mgr_b._session_by_sid[sid].peer_typing_until_mono = time.monotonic() - 0.01
                assert mgr_a.send_text(sid, "hello", deliver_b) is True
                sess_b = [s for s in mgr_b.get_sessions() if s["session_id"] == sid][0]
                assert sess_b["peer_typing"] is False
                assert sess_b["last_preview"].startswith("hello")
            finally:
                mgr_a.shutdown()
                mgr_b.shutdown()


# ══════════════════════════════════════════════════════════════════
# REST surface: handler + route dispatch (auth parity is structural:
# /api/chat/typing sits in the same authenticated dispatch chain as
# its sibling /api/chat/* routes)
# ══════════════════════════════════════════════════════════════════


class _FakeChat:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def report_typing(self, session_id, typing, send_fn):
        self.calls.append((session_id, typing))
        return self.ok


class TestSetTypingApi:
    def test_unavailable_manager_is_503(self):
        data, status = chat_api.set_typing(None, b"{}", None)
        assert status == 503

    def test_invalid_json_is_400(self):
        data, status = chat_api.set_typing(_FakeChat(), b"not json{", None)
        assert status == 400
        assert data["error"] == "invalid json"

    def test_missing_session_id_is_400(self):
        data, status = chat_api.set_typing(_FakeChat(), b'{"typing": true}', None)
        assert status == 400
        assert "session_id required" in data["error"]

    def test_happy_path_parses_args(self):
        fake = _FakeChat(ok=True)
        data, status = chat_api.set_typing(fake, b'{"session_id": "s1"}', None)
        assert status == 200 and data == {"ok": True}
        assert fake.calls == [("s1", True)]  # typing defaults to True

        data, status = chat_api.set_typing(
            fake,
            json.dumps({"session_id": "s2", "typing": False}).encode(),
            None,
        )
        assert status == 200 and data == {"ok": True}
        assert fake.calls[-1] == ("s2", False)


def test_dispatch_post_chat_typing():
    cm = _FakeChat()
    status, _ct, body_b = dispatch(
        "POST",
        "/api/chat/typing",
        {},
        json.dumps({"session_id": "s1", "typing": True}).encode(),
        object(),
        None,
        None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        chat_mgr=cm,
        chat_send_fn=lambda peer_id: lambda data: True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert cm.calls == [("s1", True)]


def test_dispatch_get_chat_typing_is_404():
    status, _ct, _body = dispatch(
        "GET",
        "/api/chat/typing",
        {},
        b"",
        object(),
        None,
        None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        chat_mgr=_FakeChat(),
    )
    assert status == 404


# ══════════════════════════════════════════════════════════════════
# Robustness: non-string text is refused cleanly (was an AttributeError
# caught one layer up as a generic failure)
# ══════════════════════════════════════════════════════════════════


def test_send_text_refuses_non_string():
    mgr = _active_mgr()
    try:
        sid = mgr.get_sessions()[0]["session_id"]
        assert mgr.send_text(sid, 12345, lambda data: True) is False
        assert mgr.send_text(sid, None, lambda data: True) is False
        assert mgr.get_messages(sid) == []
    finally:
        mgr.shutdown()


# ══════════════════════════════════════════════════
# merged from test_round4_resend.py (PEER/_FakeChat/_active_mgr renamed)
# ══════════════════════════════════════════════════

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.nearby_chat import ChatManager

PEER_RESEND = "peer-a"


def _active_mgr_resend() -> ChatManager:
    """A ChatManager with one ACTIVE session (incoming invite accepted)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
        PEER_RESEND,
        "A1",
        None,
    )
    sid = mgr.get_sessions()[0]["session_id"]
    # Honest-accept: activation only commits when the ack frame goes out.
    assert mgr.accept_invitation(sid, lambda data: True)
    return mgr


def _offer(mgr: ChatManager, sid: str, tid: str) -> None:
    """Put one incoming file offer in flight for the session."""
    mgr.handle_message(
        "chat_file_offer",
        {"session_id": sid, "transfer_id": tid, "file_name": "x.bin", "file_size": 100, "mime": ""},
        PEER_RESEND,
        "A1",
        None,
    )


def _failed_text(mgr: ChatManager, sid: str) -> dict:
    """Send a text over a refusing wire → entry lands as status 'failed'."""
    assert mgr.send_text(sid, "doomed", lambda data: False) is False
    entry = mgr.get_messages(sid)[-1]
    assert entry["status"] == "failed"
    return entry


# ══════════════════════════════════════════════════════════════════
# resend_text API
# ══════════════════════════════════════════════════════════════════


class TestResendText:
    def test_resend_success_flips_entry_and_delivers(self):

        from internal.protocol.codec import decode_message

        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            entry = _failed_text(mgr, sid)

            delivered = []

            def peer_wire(data: bytes) -> bool:
                delivered.append(decode_message(data))
                return True

            assert mgr.resend_text(sid, entry["entry_id"], peer_wire) is True
            # Same entry object, flipped to done — not a duplicate row.
            msgs = mgr.get_messages(sid)
            assert [m["entry_id"] for m in msgs].count(entry["entry_id"]) == 1
            resent = [m for m in msgs if m["entry_id"] == entry["entry_id"]][0]
            assert resent["status"] == "done"
            # The peer got ONE chat_text with the original text + timestamp.
            texts = [m for m in delivered if getattr(m, "msg_type", "") == "chat_text"]
            assert len(texts) == 1
            payload = texts[0]._raw_payload
            assert payload["text"] == "doomed"
            assert payload["session_id"] == sid
            assert payload["ts"] == entry["ts"]

            # A done entry can't be resent again (no double-send).
            assert mgr.resend_text(sid, entry["entry_id"], peer_wire) is False
            assert len([m for m in delivered if getattr(m, "msg_type", "") == "chat_text"]) == 1
        finally:
            mgr.shutdown()

    def test_resend_rejects_non_failed_entries(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            # Incoming text: never resendlable.
            mgr.handle_message(
                "chat_text",
                {"session_id": sid, "text": "from them", "ts": time.time()},
                PEER_RESEND,
                "A1",
                None,
            )
            incoming = mgr.get_messages(sid)[-1]
            assert mgr.resend_text(sid, incoming["entry_id"], lambda d: True) is False
            # Delivered outgoing text: refused.
            assert mgr.send_text(sid, "fine", lambda data: True) is True
            done = mgr.get_messages(sid)[-1]
            assert mgr.resend_text(sid, done["entry_id"], lambda d: True) is False
        finally:
            mgr.shutdown()

    def test_resend_unknown_session_or_entry(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            entry = _failed_text(mgr, sid)
            assert mgr.resend_text("nonesuch", entry["entry_id"], lambda d: True) is False
            assert mgr.resend_text(sid, "nonesuch", lambda d: True) is False
            # Closed session: refused even with a valid failed entry.
            mgr.close_session(sid)
            assert mgr.resend_text(sid, entry["entry_id"], lambda d: True) is False
        finally:
            mgr.shutdown()

    def test_resend_rate_limited_and_slot_rollback(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            e1 = _failed_text(mgr, sid)
            # Stuff the OUTGOING budget to its cap: retries must be refused
            # exactly like fresh sends.
            now = time.monotonic()
            dq = mgr._text_times_out[sid]
            dq.clear()
            dq.extend([now] * ChatManager.TEXT_RATE_LIMIT)
            assert mgr.resend_text(sid, e1["entry_id"], lambda d: True) is False
            assert len(dq) == ChatManager.TEXT_RATE_LIMIT, (
                "a refused-by-budget resend must not consume a slot",
            )
            # Free the budget: a failing retry rolls ITS slot back too.
            dq.clear()
            assert mgr.resend_text(sid, e1["entry_id"], lambda d: False) is False
            assert len(dq) == 0
            # And a succeeding retry keeps its slot charged.
            assert mgr.resend_text(sid, e1["entry_id"], lambda d: True) is True
            assert len(dq) == 1
            assert mgr.get_messages(sid)[-1]["status"] == "done"
        finally:
            mgr.shutdown()

    def test_resend_signature_matches_send_style(self):
        """send_fn is the last parameter, mirroring send_text/accept_file so
        the web host can build per-peer closures the same way."""
        params = list(
            inspect.signature(ChatManager.resend_text).parameters.values(),
        )
        assert [p.name for p in params] == ["self", "session_id", "entry_id", "send_fn"]


# ══════════════════════════════════════════════════════════════════
# REST endpoint + route dispatch
# ══════════════════════════════════════════════════════════════════


class _FakeChatResend:
    """Minimal chat_mgr for handler/dispatch tests."""

    def __init__(self):
        self.calls = []
        self.sessions = [
            {
                "session_id": "s1",
                "peer_id": "p1",
                "peer_name": "Alice",
                "status": "active",
            }
        ]

    def get_sessions(self):
        return self.sessions

    def resend_text(self, session_id, entry_id, send_fn):
        self.calls.append((session_id, entry_id, send_fn))
        return self._ok


def test_api_resend_ok():
    cm = _FakeChatResend()
    cm._ok = True
    body = json.dumps({"session_id": "s1", "entry_id": "e9"}).encode()
    data, status = chat_api.resend_text(cm, body, lambda pid: lambda d: True)
    assert status == 200 and data == {"ok": True}
    assert cm.calls[0][0] == "s1" and cm.calls[0][1] == "e9"
    assert cm.calls[0][2] is not None, "send_fn must be built for the peer"


def test_api_resend_refused_maps_to_ok_false():
    cm = _FakeChatResend()
    cm._ok = False
    body = json.dumps({"session_id": "s1", "entry_id": "e9"}).encode()
    data, status = chat_api.resend_text(cm, body, None)
    assert status == 200 and data == {"ok": False}


def test_api_resend_validation():
    cm = _FakeChatResend()
    bad = [
        b"",  # empty body
        b"not json",  # invalid json
        json.dumps({"session_id": "s1"}).encode(),  # missing entry_id
        json.dumps({"entry_id": "e9"}).encode(),  # missing session_id
    ]
    for raw in bad:
        data, status = chat_api.resend_text(cm, raw, None)
        assert status == 400, raw
    assert cm.calls == [], "validation failures must not reach the manager"


def test_api_resend_unavailable():
    data, status = chat_api.resend_text(None, b"{}", None)
    assert status == 503 and data == {"error": "chat unavailable"}


def test_dispatch_post_chat_resend():
    cm = _FakeChatResend()
    cm._ok = True
    status, _ct, body_b = dispatch(
        "POST",
        "/api/chat/resend",
        {},
        json.dumps({"session_id": "s1", "entry_id": "e9"}).encode(),
        object(),
        None,
        None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        chat_mgr=cm,
        chat_send_fn=lambda peer_id: lambda data: True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert cm.calls[0][0] == "s1"


def test_dispatch_post_chat_resend_unknown_route_shape():
    status, _ct, body_b = dispatch(
        "GET",
        "/api/chat/resend",
        {},
        b"",
        object(),
        None,
        None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        chat_mgr=_FakeChatResend(),
    )
    assert status == 404


# ══════════════════════════════════════════════════════════════════
# Defer unification: file-done callbacks fire OUTSIDE the lock
# ══════════════════════════════════════════════════════════════════


class TestDeferFileDone:
    def _probe(self, mgr: ChatManager, sink: list):
        def cb(session_id, transfer_id, ok, path, status):
            sink.append(
                {
                    "args": (session_id, transfer_id, ok, path, status),
                    # RLock._is_owned(): True when the callback ran under the
                    # chat lock — which is exactly what defer forbids.
                    "in_lock": bool(mgr._lock._is_owned()),
                }
            )

        mgr.set_on_file_done(cb)

    def test_close_session_fires_outside_lock(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "a" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            assert mgr.close_session(sid) is True
            assert len(fired) == 1
            call = fired[0]
            assert call["args"] == (sid, tid, False, "", "cancelled")
            assert call["in_lock"] is False, (
                "_on_file_done must fire after close_session releases the lock",
            )
            assert mgr.get_messages(sid)[0]["status"] == "failed"
            assert tid not in mgr._receives
        finally:
            mgr.shutdown()

    def test_mark_peer_disconnected_fires_outside_lock(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "b" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            mgr.mark_peer_disconnected(PEER_RESEND)
            assert len(fired) == 1
            assert fired[0]["args"] == (sid, tid, False, "", "peer_offline")
            assert fired[0]["in_lock"] is False
            # System offline notice still lands after the transfers fail.
            keys = [e["text_key"] for e in mgr.get_messages(sid)]
            assert "chat.system.peer_offline" in keys
            assert mgr.get_sessions()[0]["online"] is False
        finally:
            mgr.shutdown()

    def test_remote_close_frame_fires_outside_lock(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "c" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            assert (
                mgr.handle_message(
                    "chat_close",
                    {"session_id": sid},
                    PEER_RESEND,
                    "FP",
                    None,
                )
                is True
            )
            assert len(fired) == 1
            assert fired[0]["args"][4] == "peer_offline"
            assert fired[0]["in_lock"] is False
        finally:
            mgr.shutdown()

    def test_drop_session_locked_collects_without_firing(self):
        mgr = _active_mgr_resend()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "d" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            session = mgr._sessions[PEER_RESEND]
            collected: list = []
            with mgr._lock:
                mgr._drop_session_locked(session, collected)
            assert collected == [(sid, tid, "cancelled")]
            assert fired == [], "locked helper must never fire inline"
        finally:
            mgr.shutdown()

    def test_start_session_refused_still_tears_down(self):
        mgr = ChatManager("x", "X")
        try:
            # Transport refuses every frame: invite is discarded, no phantom
            # inviting session, no crash from the new deferred-fire path.
            assert mgr.start_session(PEER_RESEND, "A", "FP", lambda data: False) is None
            assert mgr.get_sessions() == []
        finally:
            mgr.shutdown()


# ══════════════════════════════════════════════════════════════════
# i18n parity for the new web copy
# ══════════════════════════════════════════════════════════════════


def test_web_locales_have_resend_keys_in_both_languages():
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "internal",
        "web",
        "static",
        "locales",
    )
    needed = {
        "chat.text_failed",
        "chat.resend",
        "chat.err_resend_failed",
    }
    for name in ("en.json", "zh-CN.json"):
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            data = json.load(fh)
        missing = needed - set(data)
        assert not missing, f"{name} missing {sorted(missing)}"


# ══════════════════════════════════════════════════
# split from test_round3_core.py — chat invite lifecycle residue
# ══════════════════════════════════════════════════

INVITE_SID = "abcdef0123456789"  # 16 hex chars, as the wire validation demands


def _make_chat(tmpdir: str) -> ChatManager:
    return ChatManager("device-a", "Device A", receive_dir=tmpdir)


def _deliver_invite(mgr: ChatManager, send_fn) -> str:
    """Feed an incoming chat_invite into *mgr*; return its session id."""
    payload = {
        "msg_type": "chat_invite",
        "session_id": INVITE_SID,
        "from_name": "Device B",
        "fingerprint_short": "ABCD1234",
        "greeting": "",
    }
    assert mgr.handle_message("chat_invite", payload, "device-b", "FP", send_fn)
    return INVITE_SID


class TestChatInviteLifecycleResidue:
    def test_refused_invite_leaves_no_phantom_session(self, tmp_path):
        """The transport refusing the invite frame must tear the session down
        immediately instead of pinning a ghost 'inviting' entry (and one of
        the MAX_SESSIONS slots) for the whole 300 s accept timeout."""
        chat = _make_chat(str(tmp_path))
        try:

            def refusing_send(data: bytes) -> bool:
                return False

            for _ in range(6):  # more than INVITE_RATE_LIMIT attempts
                sid = chat.start_session(
                    "device-b",
                    "Device B",
                    "FP1234",
                    refusing_send,
                )
                assert sid is None
                assert chat.get_sessions() == []
            # The invite-rate timestamps were rolled back too: none may linger.
            dq = chat._invite_times_out.get("device-b")
            assert not dq
        finally:
            chat.shutdown()

    def test_accept_failure_keeps_session_invited(self, tmp_path):
        """If the chat_accept frame never goes out, this side must stay in
        'invited' (unread kept) rather than showing an active conversation
        the peer knows nothing about."""
        chat = _make_chat(str(tmp_path))
        try:
            sent_frames: list[bytes] = []

            def working_send(data: bytes) -> bool:
                sent_frames.append(data)
                return True

            sid = _deliver_invite(chat, working_send)
            assert chat._session_by_sid[sid].status == "invited"

            def refusing_send(data: bytes) -> bool:
                return False

            assert chat.accept_invitation(sid, refusing_send) is False
            sess = chat._session_by_sid[sid]
            assert sess.status == "invited"
            assert sess.unread == 1  # unread marker survives the failed accept

            # Retry with a working transport -> activates for real.
            assert chat.accept_invitation(sid, working_send) is True
            sess = chat._session_by_sid[sid]
            assert sess.status == "active"
            assert sess.unread == 0
            assert any(b"chat_accept" in f for f in sent_frames)
        finally:
            chat.shutdown()

    def test_shutdown_wakes_parked_sender_threads(self, tmp_path):
        """shutdown() must set the accept/complete events so a sender thread
        parked in its wait exits promptly instead of sleeping out up to
        INVITE_ACCEPT_TIMEOUT + COMPLETION_WAIT_TIMEOUT."""
        chat = _make_chat(str(tmp_path))
        accept_event = threading.Event()
        complete_event = threading.Event()
        chat._sends["t" * 32] = {
            "accept_event": accept_event,
            "complete_event": complete_event,
        }
        chat.shutdown()
        assert accept_event.is_set()
        assert complete_event.is_set()
