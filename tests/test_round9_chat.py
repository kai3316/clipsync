"""Round 9: end-to-end chat typing indicator.

Pinned contracts:

1. Wire — ``chat_typing`` ``{session_id, typing}`` rides the existing JSON
   frame format: it decodes like any other chat frame, is a member of
   ``CHAT_MSG_TYPES`` (so both the unpaired transport gate and the host
   router admit it), and is backward compatible with older peers (the
   decoded content carries no clipboard types, so a legacy device that
   falls through to clipboard handling drops it via ``content.is_empty()``).
2. Sender — ``ChatManager.report_typing`` suppresses duplicate same-state
   frames within ``TYPING_THROTTLE`` (2s); state changes always go out;
   unknown/inactive sessions are refused.
3. Receiver — the flag lands on the session snapshot as ``peer_typing``,
   expires lazily ``TYPING_TIMEOUT`` (4s) after the last frame, is cleared
   early by an incoming text, and ``_on_sessions_changed`` fires only on a
   VISIBLE flip (keep-alives must not produce a WS push every 2s).
4. REST — ``POST /api/chat/typing`` behaves like its neighbor routes
   (503 without a ChatManager, 400 on bad bodies, dispatch wired).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import (
    CHAT_MSG_TYPES,
    FILE_TRANSFER_MSG_TYPES,
    PAIRING_MSG_TYPES,
    UNPAIRED_GATE_MSG_TYPES,
    decode_message,
    encode_frame,
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
        PEER, "B1", None,
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
            "msg_type": "chat_typing", "session_id": self.sid, "typing": True,
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
            "chat_typing", {"session_id": self.sid, "typing": typing}, PEER, "B1", None,
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
            "chat_text", {"session_id": self.sid, "text": "hi", "ts": time.time()},
            PEER, "B1", None,
        )
        assert self._snapshot()["peer_typing"] is False

    def test_unknown_peer_cannot_raise_flag(self):
        before = len(self.pushes)
        self.mgr.handle_message(
            "chat_typing", {"session_id": "0" * 16, "typing": True},
            "stranger-dev", "S1", None,
        )
        assert self._snapshot()["peer_typing"] is False
        assert len(self.pushes) == before

    def test_stale_sid_from_known_peer_falls_back_to_their_session(self):
        # Mirrors chat_text's by-peer fallback: session-id adoption after a
        # re-invite can desync the two sides, and a typing frame must not be
        # lost to it (same resolution order as text/ping/close).
        self.mgr.handle_message(
            "chat_typing", {"session_id": "0" * 16, "typing": True}, PEER, "B1", None,
        )
        assert self._snapshot()["peer_typing"] is True


# ══════════════════════════════════════════════════════════════════
# End-to-end over the real codec (two managers wired in memory)
# ══════════════════════════════════════════════════════════════════

class TestEndToEndPair:
    def test_typing_flows_between_two_managers(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # Two managers wired through the real codec: every frame one side
            # sends is decoded and fed into the other side's handlers.
            mgr_a = ChatManager("dev-a", "A")
            mgr_b = ChatManager("dev-b", "B")

            def deliver_b(data: bytes) -> bool:
                msg = decode_message(data)
                return mgr_b.handle_message(
                    getattr(msg, "msg_type", ""), msg._raw_payload, "dev-a", "FP", None,
                )

            def deliver_a(data: bytes) -> bool:
                msg = decode_message(data)
                return mgr_a.handle_message(
                    getattr(msg, "msg_type", ""), msg._raw_payload, "dev-b", "FP", None,
                )
            try:
                sid = mgr_a.start_session("dev-b", "B", "FP", deliver_b)
                assert mgr_b.accept_invitation(sid, deliver_a)
                assert any(
                    s["status"] == "active" for s in mgr_a.get_sessions()
                ), "pair never activated"

                # A types -> B sees the flag.
                assert mgr_a.report_typing(sid, True, deliver_b) is True
                sess_b = [s for s in mgr_b.get_sessions() if s["session_id"] == sid][0]
                assert sess_b["peer_typing"] is True

                # B's view lives only while refreshed: expire it artificially,
                # then A's text arrives and would clear it anyway.
                mgr_b._session_by_sid[sid].peer_typing_until_mono = \
                    time.monotonic() - 0.01
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
            fake, json.dumps({"session_id": "s2", "typing": False}).encode(), None,
        )
        assert status == 200 and data == {"ok": True}
        assert fake.calls[-1] == ("s2", False)


def test_dispatch_post_chat_typing():
    cm = _FakeChat()
    status, _ct, body_b = dispatch(
        "POST", "/api/chat/typing", {},
        json.dumps({"session_id": "s1", "typing": True}).encode(),
        object(), None, None,
        get_connected_ids=lambda: [], on_nav_url=None, on_forward_file=None,
        upload_dir=".", chat_mgr=cm,
        chat_send_fn=lambda peer_id: lambda data: True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert cm.calls == [("s1", True)]


def test_dispatch_get_chat_typing_is_404():
    status, _ct, _body = dispatch(
        "GET", "/api/chat/typing", {}, b"", object(), None, None,
        get_connected_ids=lambda: [], on_nav_url=None, on_forward_file=None,
        upload_dir=".", chat_mgr=_FakeChat(),
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
