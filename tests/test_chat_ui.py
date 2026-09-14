"""Chat, at the two seams that are contracts.

The rest of this file used to hold static wiring assertions over the shipped
JavaScript (markup, CSS, class names, locale keys, test-double signatures);
those are gone.

What is left is what a real ``ChatManager`` does with a resend — a failed
outgoing text is resendable, an incoming one is not (the renderer hides the
button and the backend refuses the call), and a delivered one is not — and the
status vocabulary a session row is labelled from, mapped from the wire status
the peer sends.
"""

import time

from internal.sync.nearby_chat import ChatManager
from internal.ui.dashboard import _chat_status_key, _chat_text_resendable

PEER = "peer-a"


def _active_mgr() -> tuple[str, ChatManager]:
    """A ChatManager with one ACTIVE session (incoming invite accepted)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
        PEER,
        "A1",
        None,
    )
    sid = mgr.get_sessions()[0]["session_id"]
    assert mgr.accept_invitation(sid, lambda data: True)
    return sid, mgr


def test_failed_done_and_incoming_entries():
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
            PEER,
            "A1",
            None,
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


def test_chat_status_key_mapping():
    """The wire statuses a peer sends, mapped to the keys the row is labelled
    with."""
    assert _chat_status_key("inviting") == "chat.status.inviting"
    assert _chat_status_key("invited") == "chat.status.pending"
    assert _chat_status_key("active", online=True) == "chat.status.connected"
    assert _chat_status_key("active", online=False) == "chat.status.offline"
    assert _chat_status_key("declined_remote") == "chat.status.declined"
    assert _chat_status_key("closed") == "chat.status.closed"
