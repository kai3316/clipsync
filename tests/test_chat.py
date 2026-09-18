"""Nearby-chat service tests — direct send, text/file roundtrips, abuse caps.

Two ChatManagers are wired together through in-memory queues: every frame one
side sends is decoded with the real codec decoder and fed into the other
side's handlers, so the tests exercise the actual wire format without any
sockets.  A real-TLS pair, the typing/resend REST surface and the file-chunk
relay sizing are covered the same way.
"""

import json
import os
import socket
import struct
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import (
    CHAT_MSG_TYPES,
    FILE_TRANSFER_MSG_TYPES,
    PAIRING_MSG_TYPES,
    UNPAIRED_GATE_MSG_TYPES,
    decode_message,
    encode_binary_chunk,
    encode_frame,
)
from internal.security.pairing import PairingManager
from internal.sync.file_transfer import MAX_FILE_SIZE
from internal.sync.nearby_chat import ChatFileTooLargeError, ChatManager
from internal.transport.connection import PeerConnection, PortInUseError, TransportManager
from internal.transport.relay import MAX_RELAY_PAYLOAD
from internal.web.api import chat as chat_api
from internal.web.routes import dispatch

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

        self.a = ChatManager(DEV_A, "Device A", receive_dir=str(dir_a))
        self.b = ChatManager(DEV_B, "Device B", receive_dir=str(dir_b))

        def send_from_b(data: bytes) -> bool:
            """B's outgoing wire: delivers into A."""
            self.frames_b_to_a.append(data)
            return self._deliver(self.a, data, DEV_B, lambda d: self.send_from_a(d))

        def send_from_a(data: bytes) -> bool:
            """A's outgoing wire: delivers into B."""
            self.frames_a_to_b.append(data)
            return self._deliver(self.b, data, DEV_A, lambda d: self.send_from_b(d))

        self.send_from_a = send_from_a
        self.send_from_b = send_from_b

    @staticmethod
    def _deliver(dst: ChatManager, data: bytes, sender_id: str, reply_fn=None) -> bool:
        """Hand *data* to *dst* as if it arrived from *sender_id*.

        *reply_fn* is the wire back to the sender — the receiving side stores
        it and uses it to answer (a chat_accept, a file accept, a done ack), so
        it must be a working transport rather than None: a receiver with no way
        to reply rolls its own receive state back.
        """
        msg = decode_message(data)
        if msg is None:
            return False
        msg_type = getattr(msg, "msg_type", "")
        if msg_type == "file_chunk":
            return dst.handle_binary_chunk(msg._raw_payload, sender_id, reply_fn)
        return dst.handle_message(msg_type, msg._raw_payload, sender_id, FP_A, reply_fn)

    def establish(self) -> str:
        """Open a session; returns the id both sides share.

        Direct send: ``start_session`` births the opener active and the invite
        frame alone opens the peer's side, so there is no accept step between
        the two calls.
        """
        sid = self.a.start_session(DEV_B, "Device B", FP_B, self.send_from_a)
        assert sid, "start_session returned None"
        assert self.a.get_sessions()[0]["status"] == "active", "opener never went active"
        assert _wait_until(
            lambda: any(s["status"] == "active" for s in self.b.get_sessions()),
        ), "B never opened the session"
        assert self.b.get_sessions()[0]["session_id"] == sid, "ids diverged"
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

    def test_invite_opens_both_sides_without_a_consent_step(self):
        """Direct send: the invite frame is the whole handshake.

        Nothing waits on B agreeing, so A is live the moment ``start_session``
        returns and B is live the moment the invite lands — there is no
        intermediate state for a UI to render a dialog in.
        """
        sid = self.pair.establish()
        sess_a = [s for s in self.pair.a.get_sessions() if s["peer_id"] == DEV_B][0]
        sess_b = self.pair.b.get_sessions()[0]
        assert sess_a["status"] == "active"
        assert sess_b["status"] == "active"
        assert sess_a["session_id"] == sid == sess_b["session_id"]
        # A message sent straight after the invite lands, with no accept in
        # between.
        assert self.pair.a.send_text(sid, "first word", self.pair.send_from_a)
        assert _wait_until(
            lambda: any(e["text"] == "first word" for e in self.pair.b.get_messages(sid)),
        )

    def test_crossing_open_converges_on_the_smaller_session_id(self):
        """Two devices opening at once must end up on ONE session id.

        Each side mints a random id, so a naive "always adopt theirs" swaps the
        ids on both sides and leaves the two sessions permanently mismatched —
        each then sends under an id the other has never seen.  Both sides apply
        the same rule (smaller id wins), so they converge without a further
        round trip.

        The two invites are held on the wire and only then released: on a
        synchronous link the second ``start_session`` would simply find the
        first one's session already live and join it, and nothing would race.
        """
        to_b: list[bytes] = []
        to_a: list[bytes] = []

        def hold_for_b(data: bytes) -> bool:
            to_b.append(data)
            return True

        def hold_for_a(data: bytes) -> bool:
            to_a.append(data)
            return True

        sid_a = self.pair.a.start_session(DEV_B, "Device B", FP_B, hold_for_b)
        sid_b = self.pair.b.start_session(DEV_A, "Device A", FP_A, hold_for_a)
        assert sid_a and sid_b and sid_a != sid_b, "expected two independent ids"

        # Release both invites so each side sees the other's id while its own
        # session is already live.
        for data in list(to_b):
            LinkedPair._deliver(self.pair.b, data, DEV_A)
        for data in list(to_a):
            LinkedPair._deliver(self.pair.a, data, DEV_B)

        winner = min(sid_a, sid_b)
        assert self.pair.a.get_sessions()[0]["session_id"] == winner, "A kept the losing id"
        assert self.pair.b.get_sessions()[0]["session_id"] == winner, "B kept the losing id"

    def test_session_id_adoption_carries_the_flood_budget(self):
        """Re-keying a session must not reset its rate-limit buckets.

        Adoption happens whenever a peer re-invites with a smaller id.  If the
        buckets were left under the old id, every re-invite would hand the peer
        a fresh text budget — a flood bypass — and orphan the old buckets.
        """
        chat = ChatManager("x", "X")
        try:
            chat.handle_message(
                "chat_invite",
                {"session_id": "f" * 16, "from_name": "B", "fingerprint_short": "B1"},
                PEER,
                "B1",
                lambda data: True,
            )
            sid = chat.get_sessions()[0]["session_id"]
            for i in range(ChatManager.TEXT_RATE_LIMIT):
                chat.handle_message(
                    "chat_text",
                    {"session_id": sid, "text": f"m{i}", "ts": time.time()},
                    PEER,
                    "B1",
                    lambda data: True,
                )
            used = list(chat._text_times_in[sid])
            assert len(used) == ChatManager.TEXT_RATE_LIMIT

            # A re-invite with a smaller id wins the tiebreak and re-keys it.
            smaller = "0" * 16
            assert smaller < sid
            chat.handle_message(
                "chat_invite",
                {"session_id": smaller, "from_name": "B", "fingerprint_short": "B1"},
                PEER,
                "B1",
                lambda data: True,
            )
            assert chat.get_sessions()[0]["session_id"] == smaller
            assert sid not in chat._text_times_in, "old bucket orphaned, not moved"
            assert list(chat._text_times_in[smaller]) == used, "budget was reset by adoption"
        finally:
            chat.shutdown()

    def test_close_notifies_peer_with_system_entry(self):
        sid = self.pair.establish()
        assert self.pair.a.close_session(sid)
        assert _wait_until(
            lambda: any(
                e["kind"] == "system" and e["text_key"] == "chat.system.session_closed_by_peer"
                for e in self.pair.b.get_messages(sid)
            )
        )


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

    def test_oversized_text_rejected_locally(self):
        long_text = "x" * (ChatManager.MAX_TEXT_LEN + 1)
        assert self.pair.a.send_text(self.sid, long_text, self.pair.send_from_a) is False

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

    def test_invite_flood_from_one_peer_is_rate_limited(self):
        """The bound that applies in both modes.

        With ``open_to_all`` on there is no backlog to cap — every invite opens
        a live session at once — so the per-peer rate limit is what stops one
        device from minting them in a loop.  (With it off the pending-invite
        cap bounds the prompts too; see ``TestApprovalMode``.)
        """
        self.pair.establish()
        accepted = 0
        for i in range(ChatManager.INVITE_RATE_LIMIT + 3):
            if self.pair.b.handle_message(
                "chat_invite",
                {"session_id": f"{i:016x}", "from_name": "A", "fingerprint_short": FP_A},
                DEV_A,
                FP_A,
                self.pair.send_from_b,
            ):
                accepted += 1
        # The establish() above already spent one, and the limit counts the
        # whole window per peer.
        assert accepted == ChatManager.INVITE_RATE_LIMIT - 1
        # And none of it accumulated: one peer still holds exactly one session,
        # however many times it knocked.
        assert len(self.pair.b.get_sessions()) == 1

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
        # Nothing accepts on B: an offer is taken the moment it lands.
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert received["saved_path"]
        assert open(received["saved_path"], "rb").read() == src.read_bytes()  # noqa: SIM115

    def test_collision_rename_avoids_overwrite(self):
        src = self._make_source(2048)
        # Pre-create the destination so the receiver must rename.
        (self.dir_b / "source.bin").write_bytes(b"precious")
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert received["saved_path"] != str(self.dir_b / "source.bin")
        assert open(self.dir_b / "source.bin", "rb").read() == b"precious"  # noqa: SIM115

    def test_offer_that_cannot_be_received_rejects_back_to_the_sender(self):
        """The one path that still answers an offer with a refusal.

        With the accept dialog gone, nothing the *user* does declines a file —
        but a receiver whose receive directory refuses the write must still
        tell the sender, or the sender sits in ``await_accept`` until the
        timeout expires.  A receive dir that cannot be created is that case.
        """
        src = self._make_source(1024)
        # A receive dir that validated at set time, then had the directory
        # replaced by a file underneath it -- so the per-transfer mkdir() fails
        # exactly where a full or unmounted disk would.
        blocked = self.dir_b / "blocked"
        self.pair.b.set_receive_dir(str(blocked))
        blocked.rmdir()
        blocked.write_bytes(b"not a directory")
        tid = self.pair.a.send_file(self.sid, str(src), self.pair.send_from_a)
        assert tid, "send_file returned None"
        assert _wait_until(
            lambda: (
                next(
                    (e for e in self.pair.a.get_messages(self.sid) if e["transfer_id"] == tid),
                    {},
                ).get("status")
                == "declined"
            ),
        ), "the sender was never told the offer could not be received"


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

    def _relay_fn(self, max_message_bytes: int = MAX_RELAY_PAYLOAD):
        # Mirrors main._chat_send_fn's tagging for an internet-only peer: the
        # chunk size it derives from the configured relay limit, and nothing
        # else — the relay cap is on the message, never on the file.
        def fn(data: bytes) -> bool:
            return self.pair.send_from_a(data)

        fn.chunk_size = ChatManager.relay_chunk_for(max_message_bytes)
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

    @pytest.mark.parametrize(
        "max_message_bytes",
        [
            MAX_RELAY_PAYLOAD,  # the public brokers this app ships against
            64 * 1024,  # a free tier's per-message ceiling
        ],
    )
    def test_internet_file_uses_relay_chunk_size_and_preserves_bytes(self, max_message_bytes):
        """A file sent over the relay is chunked to fit the relay's own limit.

        Both limits are exercised because the chunk is derived from the one the
        user configured: the low one is the case the setting exists for, and a
        chunk that only fits a 256 KB broker is dropped in flight by a 64 KB
        one while the sender has already counted it sent.
        """
        chunk = ChatManager.relay_chunk_for(max_message_bytes)
        src = self._source(chunk * 2 + 1234)
        tid = self.pair.a.send_file(self.sid, str(src), self._relay_fn(max_message_bytes))
        assert tid, "send_file returned None"
        assert self._offer_chunk_size() == chunk
        assert _wait_until(lambda: (self._b_entry(tid) or {}).get("status") == "done")
        # Every binary chunk frame must fit inside the relay payload cap.
        for frame in self.pair.frames_a_to_b:
            msg = decode_message(frame)
            if getattr(msg, "msg_type", "") == "file_chunk":
                assert len(frame) <= max_message_bytes, (
                    f"chunk frame {len(frame)}B exceeds the relay's {max_message_bytes}B cap"
                )
        received = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == tid)
        assert open(received["saved_path"], "rb").read() == src.read_bytes()  # noqa: SIM115

    def test_an_oversize_file_is_refused_before_any_offer(self):
        """Only ``MAX_FILE_SIZE`` refuses now — the relay's 5 MiB file cap is gone.

        The relay limits one *message*, not the file, and ``send_file`` already
        cuts chunks to that limit — so a file far past any single envelope still
        crosses (the case above sends three chunks of one).  What is left is the
        app's own ceiling, and it has to raise rather than return None so the UI
        can name the reason instead of reporting a transfer that never started.
        The file is truncated into place: 2 GiB of real bytes is not the point.
        """
        src = self.dir_a / "huge.bin"
        with open(src, "wb") as fh:
            fh.truncate(MAX_FILE_SIZE + 1)
        with pytest.raises(ChatFileTooLargeError):
            self.pair.a.send_file(self.sid, str(src), self._relay_fn())
        # No offer frame should have left the sender.
        assert not any(
            (getattr(decode_message(f), "_raw_payload", {}) or {}).get("msg_type")
            == "chat_file_offer"
            for f in self.pair.frames_a_to_b
        )

    def test_lan_untagged_fn_keeps_default_chunk_size(self):
        # An untagged closure — what ``LanRuntime._chat_send_fn`` hands back for
        # a peer with no relay credential — keeps the default, so the LAN wire
        # format peers already speak is unchanged.  A dual-paired peer is *not*
        # untagged any more: it is sized for the relay even while its LAN link
        # is up, because the chunk size is fixed at offer time and the route is
        # chosen per frame.
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
        # This offer is injected rather than sent by A, so A has no matching
        # send state and would refuse the accept B answers it with.  On the
        # real wire an accept frame goes out over a transport, not into a peer
        # that can shrug at it, so give B a wire that reports success: what
        # this test is about is the chunk_size the offer handler parses, not
        # the round trip (the send_file cases above cover that).
        self.pair.b._latest_send_fn[DEV_A] = lambda data: True
        assert (
            self.pair.b.handle_message(
                "chat_file_offer", payload, DEV_A, FP_A, self.pair.send_from_b
            )
            is True
        )
        entry = next(e for e in self.pair.b.get_messages(self.sid) if e["transfer_id"] == "f" * 32)
        assert entry["status"] == "sending"  # taken on arrival, no accept step
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
            recv2 = self.pair.b._receives.pop(tid, None)
            assert recv2 is not None and recv2["chunk_size"] == ChatManager.CHUNK_SIZE
            # The offer was auto-accepted, so the receive holds an open temp
            # handle; close it rather than dropping the state on the floor (on
            # Windows the directory cleanup would fail with it still open).
            if recv2.get("fh") is not None:
                recv2["fh"].close()


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
        # A working send_fn: the peer stores it and needs it back to answer a
        # file offer with its accept, so None would strand the receive state.
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a",
            "A1",
            lambda data: True,
        )
        return mgr.get_sessions()[0]["session_id"]

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
                # A working send_fn: the auto-accept answers the sender with
                # chat_file_accept, and a send that fails rolls the receive
                # state back (that rollback is the point of the fix), leaving
                # nothing here to stall.
                lambda data: True,
            )
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
    """#4: text flood budgets are per-direction."""

    def _active_session(self, mgr):
        # A working send_fn: the peer stores it and needs it back to answer a
        # file offer with its accept, so None would strand the receive state.
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a",
            "A1",
            lambda data: True,
        )
        return mgr.get_sessions()[0]["session_id"]

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


class TestR6AuditRegressions:
    """Regression tests for the deep-audit adversarial review fixes."""

    def _active_session(self, mgr):
        # A working send_fn: the peer stores it and needs it back to answer a
        # file offer with its accept, so None would strand the receive state.
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a",
            "A1",
            lambda data: True,
        )
        return mgr.get_sessions()[0]["session_id"]

    def test_an_older_peer_that_never_answers_times_out_on_our_side(self, monkeypatch):
        """The accept timeout survives only for version skew.

        A peer running an older build still gates a file behind a dialog, so
        our send waits for a ``chat_file_accept`` that may never come.  The
        sender worker must give up and report it, or the bubble stays pinned on
        "waiting" for the life of the session.
        """
        import tempfile
        from pathlib import Path

        # Shrink the 300s window so the real worker thread times out here.
        monkeypatch.setattr(ChatManager, "TRANSFER_ACCEPT_TIMEOUT", 0.2)
        mgr = ChatManager("x", "X")
        tmp = tempfile.TemporaryDirectory()
        try:
            sid = self._active_session(mgr)
            src = Path(tmp.name) / "x.bin"
            src.write_bytes(b"payload")
            done_events = []
            mgr.set_on_file_done(
                lambda s, tid, ok, path, st: done_events.append((tid, ok, st)),
            )
            # Silence on the wire stands in for the older build's open dialog.
            tid = mgr.send_file(sid, str(src), lambda data: True)
            assert tid and mgr._sends[tid]["entry"].status == "await_accept"
            assert _wait_until(
                lambda: done_events and done_events[0][0] == tid,
                timeout=5,
            ), "the sender never gave up on the silent peer"
            assert done_events[0] == (tid, False, "error_timeout")
            assert mgr._sends.get(tid) is None, "the send state was left behind"
            assert (
                next(e for e in mgr.get_messages(sid) if e["transfer_id"] == tid)["status"]
                == "failed"
            )
        finally:
            mgr.shutdown()
            tmp.cleanup()

    def test_fail_receive_notifies_sender(self):
        """A disk-error on the receive side must send an error chat_file_complete
        so the sender doesn't report a false 'delivered'."""
        mgr = ChatManager("x", "X")
        try:
            sid = self._active_session(mgr)
            sent = []

            def record(data: bytes) -> bool:
                # Must report success, not just append: a falsy return is how
                # the manager learns the wire is dead, and it rolls the receive
                # state back when it thinks the accept never went out.
                sent.append(data)
                return True

            mgr._latest_send_fn["peer-a"] = record
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
            # The sender must receive an error frame (not silence).  It is not
            # necessarily the only frame on this wire -- taking the offer also
            # answers with a chat_file_accept -- so pick out the completion.
            from internal.protocol.codec import decode_message

            completes = [
                payload
                for f in sent
                if (payload := getattr(decode_message(f), "_raw_payload", {}))
                and payload.get("msg_type") == "chat_file_complete"
            ]
            assert completes, "receiver emitted no chat_file_complete error frame"
            assert completes[0]["status"] == "error_disk"
        finally:
            mgr.shutdown()


# ══════════════════════════════════════════════════
# Real TLS, two full stacks on localhost
# ══════════════════════════════════════════════════

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

        # --- 1. invite, both sessions active -------------------------------
        sid = a.chat.start_session(DEV_B, NAME_B, "", a.send_fn_to(DEV_B))
        assert sid, "start_session returned None"
        assert self._status(a, DEV_B) == "active", "opener never went active"
        assert _deadline(lambda: self._status(b, DEV_A) == "active"), (
            "chat invite never opened the session on B over TLS"
        )
        sess_b = next(s for s in b.chat.get_sessions() if s["peer_id"] == DEV_A)
        assert sess_b["session_id"] == sid, "the two sides diverged on the session id"

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
            lambda: (self._entry(b, sid, tid) or {}).get("status") == "done", timeout=20
        ), "file offer never reached B, or B never took it"
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


# ══════════════════════════════════════════════════════════════════
# Codec: wire format + old-peer compatibility
# ══════════════════════════════════════════════════════════════════

PEER = "peer-b"


def _active_mgr() -> ChatManager:
    """A ChatManager with one ACTIVE session (direct send: the invite alone)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "B", "fingerprint_short": "B1"},
        PEER,
        "B1",
        None,
    )
    return mgr


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


# ══════════════════════════════════════════════════════════════════
# Receiver side: snapshot flag, lazy expiry, clear-on-text
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


# ══════════════════════════════════════════════════════════════════
# REST surface: the typing route (auth parity is structural: /api/chat/
# typing sits in the same authenticated dispatch chain as its siblings)
# ══════════════════════════════════════════════════════════════════


class _FakeChat:
    def __init__(self):
        self.calls = []

    def report_typing(self, session_id, typing, send_fn):
        self.calls.append((session_id, typing))
        return True


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


# ══════════════════════════════════════════════════════
# resend_text: a failed message can be re-sent once
# ══════════════════════════════════════════════════════

PEER_RESEND = "peer-a"


def _active_mgr_resend() -> ChatManager:
    """A ChatManager with one ACTIVE session (direct send: the invite alone)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
        PEER_RESEND,
        "A1",
        None,
    )
    return mgr


def _failed_text(mgr: ChatManager, sid: str) -> dict:
    """Send a text over a refusing wire → entry lands as status 'failed'."""
    assert mgr.send_text(sid, "doomed", lambda data: False) is False
    entry = mgr.get_messages(sid)[-1]
    assert entry["status"] == "failed"
    return entry


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


# ══════════════════════════════════════════════════
# Chat invite lifecycle residue
# ══════════════════════════════════════════════════

INVITE_SID = "abcdef0123456789"  # 16 hex chars, as the wire validation demands


def _make_chat(tmpdir: str) -> ChatManager:
    return ChatManager("device-a", "Device A", receive_dir=tmpdir)


def _wire(sink: list):
    """A send_fn that records what it was asked to send, and reports success.

    ``list.append`` is not a drop-in: it returns None, which ``_send_frame``
    reads as a send that never went out.
    """

    def send(data: bytes) -> bool:
        sink.append(data)
        return True

    return send


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
    def test_invite_opens_the_session_even_when_the_ack_cannot_be_sent(self, tmp_path):
        """The invite activates this side, and the ack is best-effort.

        There is nothing to wait for any more, so a chat_accept that never
        reaches the wire must not hold the conversation back: the peer's
        message is already readable here, and the id it echoes is only ever
        needed to settle a crossing open.
        """
        chat = _make_chat(str(tmp_path))
        try:
            sid = _deliver_invite(chat, lambda data: False)
            sess = chat._session_by_sid[sid]
            assert sess.status == "active"
            assert chat.get_sessions()[0]["status"] == "active"
            # And the conversation is usable in that state.
            assert chat.send_text(sid, "hi", lambda data: True) is True
        finally:
            chat.shutdown()


# ══════════════════════════════════════════════════
# The approval switch (``open_to_all`` off)
# ══════════════════════════════════════════════════


class TestApprovalMode:
    """``open_to_all`` off: each conversation and each file waits for the user.

    The switch is the whole of the difference -- the frames, the codec and the
    transfer channel are the ones the rest of this file already drives -- so
    these flip the flag on the manager and check what changes.
    """

    def test_an_invitation_waits_for_this_users_answer(self, tmp_path):
        chat = _make_chat(str(tmp_path))
        chat.set_open_to_all(False)
        try:
            sent: list[bytes] = []
            sid = _deliver_invite(chat, _wire(sent))
            # Nothing answered it: an invitation waiting on a person must not
            # also tell the peer the conversation is already open.
            assert sent == []
            assert chat.get_sessions()[0]["status"] == "invited"

            assert chat.accept_invitation(sid, _wire(sent)) is True
            assert [decode_message(f).msg_type for f in sent] == ["chat_accept"]
            assert chat.get_sessions()[0]["status"] == "active"
        finally:
            chat.shutdown()

    def test_a_refused_invitation_is_told_to_the_peer(self, tmp_path):
        """A refusal the peer never hears about leaves it waiting out its timeout."""
        chat = _make_chat(str(tmp_path))
        chat.set_open_to_all(False)
        try:
            sent: list[bytes] = []
            sid = _deliver_invite(chat, _wire(sent))
            assert chat.decline_invitation(sid, _wire(sent)) is True
            assert [decode_message(f).msg_type for f in sent] == ["chat_decline"]
            assert chat.get_sessions()[0]["status"] == "closed"
        finally:
            chat.shutdown()

    def test_saying_yes_to_one_that_is_already_gone_opens_nothing(self, tmp_path):
        """A stale Accept on a row that was answered elsewhere.

        The prompt sits on screen until the next poll, so the click can arrive
        after the peer gave up.  Committing here would show a live conversation
        the other side is not in, with every message failing.
        """
        chat = _make_chat(str(tmp_path))
        chat.set_open_to_all(False)
        try:
            sid = _deliver_invite(chat, lambda data: True)
            assert chat.close_session(sid) is True
            assert chat.accept_invitation(sid, lambda data: True) is False
            assert chat.get_sessions()[0]["status"] == "closed"
        finally:
            chat.shutdown()

    def test_the_invite_cap_still_bounds_a_flood(self, tmp_path):
        """Waiting for a person means the prompts pile up, so the cap matters.

        It is the one abuse bound the open mode does not need: there an
        invitation is a conversation, and the session limit already applies.
        """
        chat = _make_chat(str(tmp_path))
        chat.set_open_to_all(False)
        try:
            sent: list[bytes] = []
            for index in range(ChatManager.PENDING_INVITE_CAP):
                assert chat.handle_message(
                    "chat_invite",
                    {
                        "msg_type": "chat_invite",
                        "session_id": f"{index:016x}",
                        "from_name": f"Peer {index}",
                        "fingerprint_short": "ABCD1234",
                        "greeting": "",
                    },
                    f"device-{index}",
                    "FP",
                    _wire(sent),
                )
            assert len(chat.get_sessions()) == ChatManager.PENDING_INVITE_CAP

            # One more is refused outright rather than queued behind them.
            chat.handle_message(
                "chat_invite",
                {
                    "msg_type": "chat_invite",
                    "session_id": "ffffffffffffffff",
                    "from_name": "One too many",
                    "fingerprint_short": "ABCD1234",
                    "greeting": "",
                },
                "device-overflow",
                "FP",
                _wire(sent),
            )
            assert len(chat.get_sessions()) == ChatManager.PENDING_INVITE_CAP
            refusal = decode_message(sent[-1])
            assert refusal.msg_type == "chat_decline"
            assert refusal._raw_payload["reason"] == "busy"
        finally:
            chat.shutdown()

    def test_an_offered_file_waits_here_and_starts_on_accept(self, tmp_path):
        pair = LinkedPair(tmp_path / "a", tmp_path / "b")
        try:
            sid = pair.establish()
            # Flipped after the conversation is open: the file rule is read per
            # offer, so this is the switch that starts holding them.
            pair.b.set_open_to_all(False)
            src = tmp_path / "a" / "source.bin"
            src.write_bytes(bytes(range(256)) * 8)

            tid = pair.a.send_file(sid, str(src), pair.send_from_a)
            assert tid, "send_file returned None"
            entry = next(
                (e for e in pair.b.get_messages(sid) if e["transfer_id"] == tid),
                None,
            )
            assert entry is not None and entry["status"] == "await_accept"
            # Nothing has moved while it waits for the answer.
            assert not entry.get("saved_path")

            assert pair.b.accept_file(sid, tid, pair.send_from_b) is True
            assert _wait_until(
                lambda: (
                    next(
                        (e for e in pair.b.get_messages(sid) if e["transfer_id"] == tid),
                        {},
                    ).get("status")
                    == "done"
                ),
            ), "the file never arrived after being accepted"
        finally:
            pair.close()

    def test_the_switch_leaves_a_live_conversation_alone(self, tmp_path):
        """Flipping it mid-conversation must not tear down what is open.

        The rule decides whether the *next* invitation is a prompt; a session
        already running was admitted under the rule in force when it opened,
        and closing it would end a conversation the user is in the middle of.
        """
        chat = _make_chat(str(tmp_path))
        try:
            sid = _deliver_invite(chat, lambda data: True)
            assert chat.get_sessions()[0]["status"] == "active"

            chat.set_open_to_all(False)

            assert chat.get_sessions()[0]["status"] == "active"
            assert chat.send_text(sid, "still here", lambda data: True) is True
        finally:
            chat.shutdown()

    def test_both_sides_asking_at_once_settle_on_one_conversation(self, tmp_path):
        """Two prompts for the same pair must not stay two prompts.

        Both users asked at the same moment, so both sides hold a session the
        other knows nothing about and each has to drop one.  There is no prompt
        to answer here -- the tiebreak is what keeps the two from sitting on
        each other's invitations -- so the SMALLER id wins on both sides.
        "Adopt theirs" would swap the ids and leave the two permanently
        disagreeing about which conversation this is.
        """
        pair = LinkedPair(tmp_path / "a", tmp_path / "b")
        pair.a.set_open_to_all(False)
        pair.b.set_open_to_all(False)
        try:
            # Both wires are held, so neither invitation is read until both are
            # on the wire -- the crossing open.
            a_out: list[bytes] = []
            b_out: list[bytes] = []

            def wire_a(data: bytes) -> bool:
                a_out.append(data)
                return True

            def wire_b(data: bytes) -> bool:
                b_out.append(data)
                return True

            sid_a = pair.a.start_session(DEV_B, "Device B", FP_B, wire_a)
            sid_b = pair.b.start_session(DEV_A, "Device A", FP_A, wire_b)
            assert sid_a and sid_b and sid_a != sid_b, "expected two independent ids"

            # Drain in per-direction order, which is what a TCP link gives you:
            # each side reads the other's invitation before the answer to it.
            for _ in range(8):
                batch_a, a_out[:] = list(a_out), []
                batch_b, b_out[:] = list(b_out), []
                if not batch_a and not batch_b:
                    break
                for data in batch_a:
                    LinkedPair._deliver(pair.b, data, DEV_A, wire_b)
                for data in batch_b:
                    LinkedPair._deliver(pair.a, data, DEV_B, wire_a)
            assert not a_out and not b_out, "the two sides are still talking past each other"

            winner = min(sid_a, sid_b)
            assert [s["session_id"] for s in pair.a.get_sessions()] == [winner], "A kept a loser"
            assert [s["session_id"] for s in pair.b.get_sessions()] == [winner], "B kept a loser"
            assert pair.a.get_sessions()[0]["status"] == "active"
            assert pair.b.get_sessions()[0]["status"] == "active"
        finally:
            pair.close()
