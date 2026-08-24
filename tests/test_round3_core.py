"""Round-3 core-surface regression tests.

Covers the fixes made in this round:

- codec: a malformed ``msg_type`` (non-string) must drop the FRAME, not raise
  TypeError through every ``in <frozenset>`` router check and tear down the
  transport recv loop; garbage ``timestamp`` / ``image_fmt`` are coerced.
- pairing: a duplicate / late ``pairing_confirm`` must keep PAIRED instead of
  regressing to peer_confirmed (which re-prompts the user); an expired
  confirmation attempt also cancels the lifecycle status.
- nearby_chat: an invite whose frame the transport refuses must tear the
  just-created session down immediately (no phantom "inviting" entry holding
  a session slot) and roll back its rate-limit slot; accepting an invitation
  whose accept-frame fails must NOT commit activation locally; shutdown()
  wakes parked sender threads.
"""

import base64
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import decode_message, encode_frame, encode_message
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.security.pairing import (
    PAIRING_STATUS_CANCELLED,
    PAIRING_STATUS_PAIRED,
    PairingManager,
)
from internal.sync.nearby_chat import ChatManager

import tempfile


# ---------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------

class TestCodecMalformedPayloads:
    def _frame(self, payload: dict) -> bytes:
        return encode_frame(payload, msg_id="deadbeef", source_device="peer-a")

    def test_non_string_msg_type_is_dropped(self):
        # A dict/list msg_type is unhashable: routers do ``msg_type in <set>``
        # which raises TypeError -- inside the recv loop's catch-all that used
        # to tear down the whole connection.  It must be dropped as a bad
        # frame instead.
        for bad in ({}, [1, 2], {"x": 1}):
            data = self._frame({"msg_type": bad, "types": {}})
            assert decode_message(data) is None, f"msg_type={bad!r}"

    def test_non_numeric_timestamp_coerced_to_zero(self):
        for bad in ("oops", None, [1], float("nan"), float("inf")):
            data = self._frame({
                "msg_type": "clipboard",
                "types": {"TEXT": base64.b64encode(b"hi").decode()},
                "timestamp": bad,
            })
            msg = decode_message(data)
            assert msg is not None, f"timestamp={bad!r}"
            assert msg.content.timestamp == 0.0

    def test_bool_timestamp_coerced_to_zero(self):
        data = self._frame({"msg_type": "clipboard", "types": {}, "timestamp": True})
        msg = decode_message(data)
        assert msg is not None and msg.content.timestamp == 0.0

    def test_non_string_image_fmt_tolerated(self):
        data = self._frame({
            "msg_type": "clipboard",
            "types": {},
            "image_fmt": 42,
        })
        msg = decode_message(data)
        assert msg is not None and msg.content.image_fmt == ""

    def test_valid_clipboard_still_decodes(self):
        msg = SyncMessage(
            content=ClipboardContent(
                timestamp=1234.5,
                types={ContentType.TEXT: b"hello"},
            ),
            msg_id="abc123",
            source_device="dev-x",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.msg_type == "clipboard"
        assert decoded.content.types[ContentType.TEXT] == b"hello"
        assert decoded.content.timestamp == 1234.5
        assert decoded.source_device == "dev-x"


# ---------------------------------------------------------------------------
# pairing
# ---------------------------------------------------------------------------

class TestPairingConfirmLifecycle:
    def _mgr(self) -> PairingManager:
        return PairingManager("device-a", "Device A")

    def test_duplicate_peer_confirm_keeps_paired(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-b")
        assert mgr.confirm_pairing("peer-b", code) is True
        # Peer confirms second -> handshake completes.
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_PAIRED
        # A duplicate / late pairing_confirm (reconnect storm re-delivery)
        # must NOT regress the completed handshake to peer_confirmed.
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_PAIRED
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_PAIRED

    def test_expired_confirmation_cancels_lifecycle_status(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-c")
        # Age the request past PAIRING_TIMEOUT (300 s).
        pending_code, _ts = mgr._pending_pairings["peer-c"]
        mgr._pending_pairings["peer-c"] = (pending_code, time.time() - 400)
        assert mgr.confirm_pairing("peer-c", code) is False
        assert mgr.get_pairing_status("peer-c") == PAIRING_STATUS_CANCELLED
        assert mgr.get_pending_pairings() == []

    def test_wrong_code_then_correct_within_window(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-d")
        assert mgr.confirm_pairing("peer-d", "00000000") is False
        assert mgr.confirm_pairing("peer-d", code) is True


# ---------------------------------------------------------------------------
# nearby_chat
# ---------------------------------------------------------------------------

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
                    "device-b", "Device B", "FP1234", refusing_send,
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


# ---------------------------------------------------------------------------
# misc sanity: crafted JSON payloads survive round-trip parsing
# ---------------------------------------------------------------------------

class TestCodecJsonTolerance:
    def test_payload_with_unexpected_extra_fields_decodes(self):
        data = encode_frame({
            "msg_type": "clipboard",
            "types": {"TEXT": base64.b64encode(b"x").decode()},
            "unknown_future_field": {"nested": [1, 2, 3]},
        })
        msg = decode_message(data)
        assert msg is not None
        assert msg.content.types[ContentType.TEXT] == b"x"
        assert msg._raw_payload["unknown_future_field"] == {"nested": [1, 2, 3]}

    def test_json_array_payload_dropped_not_crash(self):
        data = encode_frame([1, 2, 3])
        assert decode_message(data) is None
