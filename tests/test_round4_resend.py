"""Round 4: failed-text one-click resend + defer unification of file-done.

Two contracts are pinned here:

1. ``ChatManager.resend_text`` — a failed outgoing chat text can be
   re-transmitted from the bubble's ⟳ button; only that session's own
   FAILED texts qualify, the retry re-charges (and on failure rolls back)
   the per-session flood budget, and success flips the SAME entry to
   ``done``.
2. Defer unification — ``_fail_transfers_for_session`` never fires
   ``_on_file_done`` while the chat lock is held anymore; every caller
   (close_session, mark_peer_disconnected, remote chat_close,
   _drop_session_locked) collects tuples and fires them after release.
   Callbacks still happen with unchanged arguments.

REST surface covered: POST /api/chat/resend handler + route dispatch.
"""

import inspect
import json
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.nearby_chat import ChatManager
from internal.web.api import chat as chat_api
from internal.web.routes import dispatch


PEER = "peer-a"


def _active_mgr() -> ChatManager:
    """A ChatManager with one ACTIVE session (incoming invite accepted)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
        PEER, "A1", None,
    )
    sid = mgr.get_sessions()[0]["session_id"]
    # Honest-accept: activation only commits when the ack frame goes out.
    assert mgr.accept_invitation(sid, lambda data: True)
    return mgr


def _offer(mgr: ChatManager, sid: str, tid: str) -> None:
    """Put one incoming file offer in flight for the session."""
    mgr.handle_message(
        "chat_file_offer",
        {"session_id": sid, "transfer_id": tid,
         "file_name": "x.bin", "file_size": 100, "mime": ""},
        PEER, "A1", None,
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
        import threading

        from internal.protocol.codec import decode_message

        mgr = _active_mgr()
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
            assert len([m for m in delivered
                        if getattr(m, "msg_type", "") == "chat_text"]) == 1
        finally:
            mgr.shutdown()

    def test_resend_rejects_non_failed_entries(self):
        mgr = _active_mgr()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            # Incoming text: never resendlable.
            mgr.handle_message(
                "chat_text",
                {"session_id": sid, "text": "from them", "ts": time.time()},
                PEER, "A1", None,
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
        mgr = _active_mgr()
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
        mgr = _active_mgr()
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

class _FakeChat:
    """Minimal chat_mgr for handler/dispatch tests."""

    def __init__(self):
        self.calls = []
        self.sessions = [{
            "session_id": "s1", "peer_id": "p1", "peer_name": "Alice",
            "status": "active",
        }]

    def get_sessions(self):
        return self.sessions

    def resend_text(self, session_id, entry_id, send_fn):
        self.calls.append((session_id, entry_id, send_fn))
        return self._ok


def test_api_resend_ok():
    cm = _FakeChat()
    cm._ok = True
    body = json.dumps({"session_id": "s1", "entry_id": "e9"}).encode()
    data, status = chat_api.resend_text(cm, body, lambda pid: lambda d: True)
    assert status == 200 and data == {"ok": True}
    assert cm.calls[0][0] == "s1" and cm.calls[0][1] == "e9"
    assert cm.calls[0][2] is not None, "send_fn must be built for the peer"


def test_api_resend_refused_maps_to_ok_false():
    cm = _FakeChat()
    cm._ok = False
    body = json.dumps({"session_id": "s1", "entry_id": "e9"}).encode()
    data, status = chat_api.resend_text(cm, body, None)
    assert status == 200 and data == {"ok": False}


def test_api_resend_validation():
    cm = _FakeChat()
    bad = [
        b"",                                     # empty body
        b"not json",                             # invalid json
        json.dumps({"session_id": "s1"}).encode(),   # missing entry_id
        json.dumps({"entry_id": "e9"}).encode(),     # missing session_id
    ]
    for raw in bad:
        data, status = chat_api.resend_text(cm, raw, None)
        assert status == 400, raw
    assert cm.calls == [], "validation failures must not reach the manager"


def test_api_resend_unavailable():
    data, status = chat_api.resend_text(None, b"{}", None)
    assert status == 503 and data == {"error": "chat unavailable"}


def test_dispatch_post_chat_resend():
    cm = _FakeChat()
    cm._ok = True
    status, _ct, body_b = dispatch(
        "POST", "/api/chat/resend", {},
        json.dumps({"session_id": "s1", "entry_id": "e9"}).encode(),
        object(), None, None,
        get_connected_ids=lambda: [], on_nav_url=None, on_forward_file=None,
        upload_dir=".", chat_mgr=cm,
        chat_send_fn=lambda peer_id: lambda data: True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert cm.calls[0][0] == "s1"


def test_dispatch_post_chat_resend_unknown_route_shape():
    status, _ct, body_b = dispatch(
        "GET", "/api/chat/resend", {}, b"", object(), None, None,
        get_connected_ids=lambda: [], on_nav_url=None, on_forward_file=None,
        upload_dir=".", chat_mgr=_FakeChat(),
    )
    assert status == 404


# ══════════════════════════════════════════════════════════════════
# Defer unification: file-done callbacks fire OUTSIDE the lock
# ══════════════════════════════════════════════════════════════════

class TestDeferFileDone:
    def _probe(self, mgr: ChatManager, sink: list):
        def cb(session_id, transfer_id, ok, path, status):
            sink.append({
                "args": (session_id, transfer_id, ok, path, status),
                # RLock._is_owned(): True when the callback ran under the
                # chat lock — which is exactly what defer forbids.
                "in_lock": bool(mgr._lock._is_owned()),
            })
        mgr.set_on_file_done(cb)

    def test_close_session_fires_outside_lock(self):
        mgr = _active_mgr()
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
        mgr = _active_mgr()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "b" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            mgr.mark_peer_disconnected(PEER)
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
        mgr = _active_mgr()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "c" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            assert mgr.handle_message(
                "chat_close", {"session_id": sid}, PEER, "FP", None,
            ) is True
            assert len(fired) == 1
            assert fired[0]["args"][4] == "peer_offline"
            assert fired[0]["in_lock"] is False
        finally:
            mgr.shutdown()

    def test_drop_session_locked_collects_without_firing(self):
        mgr = _active_mgr()
        try:
            sid = mgr.get_sessions()[0]["session_id"]
            tid = "d" * 32
            _offer(mgr, sid, tid)
            fired = []
            self._probe(mgr, fired)
            session = mgr._sessions[PEER]
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
            assert mgr.start_session(PEER, "A", "FP", lambda data: False) is None
            assert mgr.get_sessions() == []
        finally:
            mgr.shutdown()


# ══════════════════════════════════════════════════════════════════
# i18n parity for the new web copy
# ══════════════════════════════════════════════════════════════════

def test_web_locales_have_resend_keys_in_both_languages():
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "internal", "web", "static", "locales",
    )
    needed = {
        "chat.text_failed", "chat.resend", "chat.err_resend_failed",
    }
    for name in ("en.json", "zh-CN.json"):
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            data = json.load(fh)
        missing = needed - set(data)
        assert not missing, f"{name} missing {sorted(missing)}"
