"""Round 5: desktop (customtkinter) failed-text resend UI.

Pins the dashboard side of the round-4 resend backend:

1. ``_chat_text_resendable`` — the pure entry -> show-resend decision used
   by the bubble renderer (module-level, no Tk needed),
2. that decision agrees with what ``ChatManager.resend_text`` would actually
   accept, using a real ChatManager with a fake wire,
3. ``DashboardWindow._chat_do_resend`` — callback wiring, double-click busy
   guard, and failure feedback — exercised on an instance built with
   ``object.__new__`` so NO Tk root is ever created,
4. the Dashboard constructor grew a ``chat_resend_text`` hook,
5. the three new i18n keys exist in BOTH locale dicts (en / zh-CN).
"""

import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.i18n import _EN, _ZH, T
from internal.sync.nearby_chat import ChatManager
from internal.ui.dashboard import DashboardWindow, _chat_text_resendable

PEER = "peer-a"

NEW_I18N_KEYS = (
    "chat.text_failed",
    "chat.resend",
    "chat.err_resend_failed",
)


# ══════════════════════════════════════════════════════════════════
# 1. The renderer's decision function
# ══════════════════════════════════════════════════════════════════

class TestResendableDecision:
    def test_failed_outgoing_text_is_resendable(self):
        assert _chat_text_resendable(
            {"kind": "text", "outgoing": True, "status": "failed"},
        ) is True

    def test_other_statuses_are_not_resendable(self):
        for status in ("pending", "sending", "done", "declined", "cancelled"):
            assert not _chat_text_resendable(
                {"kind": "text", "outgoing": True, "status": status},
            ), status

    def test_incoming_never_resendable_even_if_failed(self):
        assert not _chat_text_resendable(
            {"kind": "text", "outgoing": False, "status": "failed"},
        )

    def test_non_text_kinds_never_resendable(self):
        for kind in ("file", "system"):
            assert not _chat_text_resendable(
                {"kind": kind, "outgoing": True, "status": "failed"},
            ), kind

    def test_missing_kind_defaults_to_text_bubble_semantics(self):
        # The transcript loop renders kind-less entries as text bubbles, so
        # the decision must treat a missing kind the same way.
        assert _chat_text_resendable({"outgoing": True, "status": "failed"}) is True

    def test_garbage_input_is_safe(self):
        assert not _chat_text_resendable({})
        assert not _chat_text_resendable(None)
        assert not _chat_text_resendable("not-a-dict")


# ══════════════════════════════════════════════════════════════════
# 2. Decision agrees with ChatManager.resend_text on real entries
# ══════════════════════════════════════════════════════════════════

def _active_mgr() -> tuple[str, ChatManager]:
    """A ChatManager with one ACTIVE session (incoming invite accepted)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
        PEER, "A1", None,
    )
    sid = mgr.get_sessions()[0]["session_id"]
    assert mgr.accept_invitation(sid, lambda data: True)
    return sid, mgr


class TestDecisionMatchesBackend:
    def test_failed_done_and_incoming_entries(self):
        sid, mgr = _active_mgr()
        try:
            # Outgoing text over a refusing wire → status failed → resendable.
            assert mgr.send_text(sid, "doomed", lambda data: False) is False
            failed = mgr.get_messages(sid)[-1]
            assert failed["status"] == "failed"
            assert _chat_text_resendable(failed) is True

            # Incoming text: renderer hides the button, backend refuses.
            mgr.handle_message(
                "chat_text",
                {"session_id": sid, "text": "from them", "ts": time.time()},
                PEER, "A1", None,
            )
            incoming = mgr.get_messages(sid)[-1]
            assert _chat_text_resendable(incoming) is False
            assert mgr.resend_text(sid, incoming["entry_id"], lambda d: True) is False

            # Delivered outgoing text: button gone after resend succeeds.
            assert mgr.send_text(sid, "fine", lambda data: True) is True
            done = mgr.get_messages(sid)[-1]
            assert done["status"] == "done"
            assert _chat_text_resendable(done) is False
            assert mgr.resend_text(sid, done["entry_id"], lambda d: True) is False
        finally:
            mgr.shutdown()

    def test_button_disappears_after_successful_resend(self):
        sid, mgr = _active_mgr()
        try:
            assert mgr.send_text(sid, "retry me", lambda data: False) is False
            entry = mgr.get_messages(sid)[-1]
            assert _chat_text_resendable(entry) is True
            assert mgr.resend_text(sid, entry["entry_id"], lambda d: True) is True
            refreshed = next(
                e for e in mgr.get_messages(sid)
                if e["entry_id"] == entry["entry_id"]
            )
            assert refreshed["status"] == "done"
            assert _chat_text_resendable(refreshed) is False
        finally:
            mgr.shutdown()


# ══════════════════════════════════════════════════════════════════
# 3. _chat_do_resend wiring + guards (no Tk root involved)
# ══════════════════════════════════════════════════════════════════

def _bare_dashboard(callback) -> tuple[DashboardWindow, list]:
    """A DashboardWindow shell without running __init__ (no Tk objects).

    Only the attributes ``_chat_do_resend`` touches are provided; the hint
    mechanism is replaced by a recorder so the failure-feedback path can be
    observed without a widget tree.
    """
    dash = object.__new__(DashboardWindow)
    dash._chat_resend_text = callback
    dash._chat_resend_busy_id = None
    hints: list[str] = []
    dash._chat_show_hint = hints.append
    return dash, hints


class TestChatDoResend:
    def test_success_invokes_callback_once_and_clears_busy(self):
        calls = []

        def cb(sid, eid):
            calls.append((sid, eid))
            return True

        dash, hints = _bare_dashboard(cb)
        ok = dash._chat_do_resend("sess1", "entry1")
        assert ok is True
        assert calls == [("sess1", "entry1")]
        assert dash._chat_resend_busy_id is None
        assert hints == []

    def test_failure_returns_false_and_shows_hint(self):
        dash, hints = _bare_dashboard(lambda sid, eid: False)
        ok = dash._chat_do_resend("sess1", "entry1")
        assert ok is False
        assert hints == [T("chat.err_resend_failed")]
        assert dash._chat_resend_busy_id is None

    def test_callback_exception_is_swallowed_and_reported(self):
        def boom(sid, eid):
            raise RuntimeError("wire exploded")

        dash, hints = _bare_dashboard(boom)
        assert dash._chat_do_resend("sess1", "entry1") is False
        assert hints == [T("chat.err_resend_failed")]
        assert dash._chat_resend_busy_id is None

    def test_busy_guard_blocks_reentry(self):
        calls = []
        dash, _ = _bare_dashboard(lambda sid, eid: calls.append((sid, eid)))
        dash._chat_resend_busy_id = "other-entry"
        assert dash._chat_do_resend("sess1", "entry1") is False
        assert calls == [], "a second click during an in-flight resend must not send"

    def test_empty_ids_refused_without_callback_call(self):
        called = []
        dash, _ = _bare_dashboard(lambda sid, eid: called.append(1))
        assert dash._chat_do_resend("", "e") is False
        assert dash._chat_do_resend("s", "") is False
        assert called == []

    def test_missing_callback_is_noop(self):
        dash = object.__new__(DashboardWindow)
        dash._chat_resend_text = None
        dash._chat_resend_busy_id = None
        dash._chat_show_hint = lambda *a, **k: None
        assert dash._chat_do_resend("s", "e") is False


# ══════════════════════════════════════════════════════════════════
# 4. Constructor hook + glue shape
# ══════════════════════════════════════════════════════════════════

class TestWiringShape:
    def test_dashboard_takes_chat_resend_text_kwarg(self):
        params = inspect.signature(DashboardWindow.__init__).parameters
        assert "chat_resend_text" in params
        default = params["chat_resend_text"].default
        assert default is None or callable(default)

    def test_hook_arity_matches_host_glue_shape(self):
        # The dashboard hook receives (session_id, entry_id); main.py owns the
        # send_fn closure, mirroring how chat_send_text wraps manager args.
        params = inspect.signature(ChatManager.resend_text).parameters.values()
        names = [p.name for p in params if p.name != "self"]
        assert names[:2] == ["session_id", "entry_id"]
        assert names[2] == "send_fn"


# ══════════════════════════════════════════════════════════════════
# 5. i18n parity
# ══════════════════════════════════════════════════════════════════

class TestI18nParity:
    def test_new_keys_defined_in_both_locales(self):
        for key in NEW_I18N_KEYS:
            assert key in _EN, f"{key} missing from _EN"
            assert key in _ZH, f"{key} missing from _ZH"
            assert _EN[key].strip(), f"{key} has an empty en string"
            assert _ZH[key].strip(), f"{key} has an empty zh-CN string"
            assert _EN[key] != _ZH[key], f"{key}: zh-CN must not reuse en copy"

    def test_keys_resolve_via_T(self):
        for key in NEW_I18N_KEYS:
            value = T(key)
            assert isinstance(value, str) and value.strip()
