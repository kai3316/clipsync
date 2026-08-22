"""Web Nearby-Chat API tests.

Unit-tests ``internal/web/api/chat.py`` handlers with a fake chat_mgr whose
public method signatures are checked against the real ``ChatManager`` (drift
guard), plus route-dispatch smoke tests through ``internal.web.routes.dispatch``.

No real sockets are touched.
"""

import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.nearby_chat import ChatManager
from internal.web.api import chat as chat_api
from internal.web.routes import dispatch


# ── Fake chat manager (signature drift guard mirrors test_chat_i18n) ──

class FakeChatManager:
    """Stub matching ChatManager's public API (signatures checked below)."""

    def __init__(self):
        self._sent = []

    def start_session(self, peer_id, peer_name, fingerprint_short, send_fn):
        return "session-new"

    def accept_invitation(self, session_id, send_fn):
        return True

    def decline_invitation(self, session_id, send_fn, reason=""):
        return True

    def close_session(self, session_id, notify_peer=True):
        return True

    def send_text(self, session_id, text, send_fn):
        self._sent.append((session_id, text))
        return True

    def send_file(self, session_id, file_path, send_fn):
        return "transfer-1"

    def accept_file(self, session_id, transfer_id, send_fn):
        return True

    def decline_file(self, session_id, transfer_id, send_fn):
        return True

    def cancel_file(self, session_id, entry_id):
        return True

    def mark_session_read(self, session_id):
        return None

    def set_own_fingerprint(self, fingerprint):
        return None

    def set_receive_dir(self, path):
        return None

    def shutdown(self):
        return None

    def handle_message(self, msg_type, payload, sender_device_id,
                       sender_fp_short, send_fn):
        return True

    def handle_binary_chunk(self, raw_payload, sender_device_id, send_fn):
        return False

    def mark_peer_disconnected(self, peer_id):
        return None

    def get_sessions(self):
        return [{
            "session_id": "s1",
            "peer_id": "peer-1",
            "peer_name": "Alice",
            "fingerprint_short": "ABCDEF12",
            "status": "active",
            "created_ts": 0.0,
            "last_activity_ts": 0.0,
            "unread": 0,
            "online": True,
            "last_preview": "hi",
        }]

    def get_messages(self, session_id):
        return [{
            "entry_id": "e1",
            "kind": "file",
            "outgoing": False,
            "ts": 0.0,
            "text": "",
            "text_key": "",
            "fmt": {},
            "file_name": "a.txt",
            "file_size": 3,
            "mime": "",
            "status": "done",
            "fraction": 1.0,
            "saved_path": os.path.join(
                os.path.expanduser("~"), "Downloads", "ClipSync", "a.txt"),
            "transfer_id": "tid-1",
        }]

    def set_on_incoming_invite(self, cb):
        return None

    def set_on_invite_response(self, cb):
        return None

    def set_on_sessions_changed(self, cb):
        return None

    def set_on_message(self, cb):
        return None

    def set_on_file_progress(self, cb):
        return None

    def set_on_file_done(self, cb):
        return None


_FAKE_PUBLIC = {
    name for name in dir(FakeChatManager)
    if not name.startswith("_") and callable(getattr(FakeChatManager, name))
}


def _params_of(fn):
    return [p for p in inspect.signature(fn).parameters.values()
            if p.name != "self"]


def test_fake_matches_real_chat_manager_signatures():
    for name in sorted(_FAKE_PUBLIC):
        real_fn = getattr(ChatManager, name, None)
        assert real_fn is not None, (
            f"FakeChatManager.{name} has no real ChatManager counterpart"
        )
        real_params = _params_of(real_fn)
        fake_params = _params_of(getattr(FakeChatManager, name))
        assert [p.name for p in real_params] == [p.name for p in fake_params], (
            f"{name}: parameter names drifted"
        )
        assert [p.default for p in real_params] == [p.default for p in fake_params], (
            f"{name}: parameter defaults drifted"
        )


# ── Handler helpers ─────────────────────────────────────────────────

def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _send_fn_for(peer_id):
    return lambda data: True


# ── Handler unit tests ──────────────────────────────────────────────

def test_get_sessions():
    data, status = chat_api.get_sessions(FakeChatManager())
    assert status == 200
    assert data["sessions"][0]["session_id"] == "s1"


def test_get_sessions_unavailable():
    data, status = chat_api.get_sessions(None)
    assert status == 503
    assert data["error"] == "chat unavailable"


def test_get_messages():
    data, status = chat_api.get_messages(FakeChatManager(), {"session_id": ["s1"]})
    assert status == 200
    assert data["messages"][0]["transfer_id"] == "tid-1"


def test_get_messages_requires_session_id():
    data, status = chat_api.get_messages(FakeChatManager(), {})
    assert status == 400


def test_get_chat_devices():
    data, status = chat_api.get_chat_devices(
        lambda: [{"peer_id": "p1", "name": "Bob", "paired": False}],
    )
    assert status == 200
    assert data["devices"][0]["peer_id"] == "p1"


def test_get_chat_devices_unavailable():
    data, status = chat_api.get_chat_devices(None)
    assert status == 503


def test_invite():
    cm = FakeChatManager()
    calls = {}

    def _start(peer_id, peer_name):
        calls["peer_id"] = peer_id
        return "sess-new"

    data, status = chat_api.invite(
        cm, _body({"peer_id": "peer-1", "peer_name": "Alice"}), _start,
    )
    assert status == 200
    assert data["session_id"] == "sess-new"
    assert calls["peer_id"] == "peer-1"


def test_invite_connecting():
    cm = FakeChatManager()
    data, status = chat_api.invite(
        cm, _body({"peer_id": "peer-1"}), lambda peer_id, peer_name: None,
    )
    assert status == 200
    assert data["connecting"] is True
    assert data["session_id"] is None


def test_invite_requires_peer_id():
    data, status = chat_api.invite(FakeChatManager(), _body({}), lambda *a: None)
    assert status == 400


def test_invite_unavailable():
    data, status = chat_api.invite(None, _body({"peer_id": "p"}), lambda *a: None)
    assert status == 503


def test_send_text():
    cm = FakeChatManager()
    data, status = chat_api.send_text(
        cm, _body({"session_id": "s1", "text": "hi"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is True
    assert cm._sent == [("s1", "hi")]


def test_send_text_missing_fields():
    data, status = chat_api.send_text(
        FakeChatManager(), _body({"session_id": "s1"}), _send_fn_for,
    )
    assert status == 400


def test_send_file():
    cm = FakeChatManager()
    data, status = chat_api.send_file(
        cm, _body({"session_id": "s1", "file_path": "C:/x/a.txt"}), _send_fn_for,
    )
    assert status == 200
    assert data["transfer_id"] == "transfer-1"


def test_accept_file():
    data, status = chat_api.accept_file(
        FakeChatManager(),
        _body({"session_id": "s1", "transfer_id": "tid-1"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is True


def test_decline_file():
    data, status = chat_api.decline_file(
        FakeChatManager(),
        _body({"session_id": "s1", "transfer_id": "tid-1"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is True


def test_cancel_file():
    data, status = chat_api.cancel_file(
        FakeChatManager(), _body({"session_id": "s1", "transfer_id": "tid-1"}),
    )
    assert status == 200
    assert data["ok"] is True


def test_accept_invite():
    data, status = chat_api.accept_invite(
        FakeChatManager(), _body({"session_id": "s1"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is True


def test_decline_invite():
    data, status = chat_api.decline_invite(
        FakeChatManager(), _body({"session_id": "s1"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is True


def test_close_session():
    data, status = chat_api.close_session(
        FakeChatManager(), _body({"session_id": "s1"}),
    )
    assert status == 200
    assert data["ok"] is True


def test_mark_read():
    data, status = chat_api.mark_read(
        FakeChatManager(), _body({"session_id": "s1"}),
    )
    assert status == 200
    assert data["ok"] is True


def test_actions_unavailable():
    cm = None
    body = _body({"session_id": "s1", "text": "hi", "transfer_id": "t"})
    assert chat_api.send_text(cm, body, _send_fn_for)[1] == 503
    assert chat_api.send_file(cm, body, _send_fn_for)[1] == 503
    assert chat_api.accept_file(cm, body, _send_fn_for)[1] == 503
    assert chat_api.decline_file(cm, body, _send_fn_for)[1] == 503
    assert chat_api.cancel_file(cm, body)[1] == 503
    assert chat_api.accept_invite(cm, _body({"session_id": "s1"}), _send_fn_for)[1] == 503
    assert chat_api.decline_invite(cm, _body({"session_id": "s1"}), _send_fn_for)[1] == 503
    assert chat_api.close_session(cm, _body({"session_id": "s1"}))[1] == 503
    assert chat_api.mark_read(cm, _body({"session_id": "s1"}))[1] == 503


def test_find_saved_path():
    cm = FakeChatManager()
    path = chat_api.find_saved_path(cm, "tid-1")
    assert path and path.endswith("a.txt")
    assert chat_api.find_saved_path(cm, "unknown-tid") is None
    assert chat_api.find_saved_path(None, "tid-1") is None


# ── Route-dispatch smoke tests ──────────────────────────────────────

def _dispatch_args(**kw):
    """Build keyword args for dispatch() with sensible stubs."""
    args = {
        "get_connected_ids": lambda: [],
        "on_nav_url": None,
        "on_forward_file": None,
        "upload_dir": ".",
    }
    args.update(kw)
    return args


def test_dispatch_get_chat_sessions():
    status, _ct, body = dispatch(
        "GET", "/api/chat/sessions", {}, b"", object(), None, None,
        **_dispatch_args(chat_mgr=FakeChatManager()),
    )
    assert status == 200
    assert json.loads(body)["sessions"][0]["session_id"] == "s1"


def test_dispatch_get_chat_sessions_unavailable():
    status, _ct, body = dispatch(
        "GET", "/api/chat/sessions", {}, b"", object(), None, None,
        **_dispatch_args(chat_mgr=None),
    )
    assert status == 503
    assert json.loads(body)["error"] == "chat unavailable"


def test_dispatch_get_chat_devices():
    status, _ct, body = dispatch(
        "GET", "/api/chat/devices", {}, b"", object(), None, None,
        **_dispatch_args(get_chat_devices=lambda: [{"peer_id": "p1"}]),
    )
    assert status == 200
    assert json.loads(body)["devices"][0]["peer_id"] == "p1"


def test_dispatch_get_chat_messages():
    status, _ct, body = dispatch(
        "GET", "/api/chat/messages", {"session_id": ["s1"]}, b"",
        object(), None, None, **_dispatch_args(chat_mgr=FakeChatManager()),
    )
    assert status == 200
    assert json.loads(body)["messages"][0]["transfer_id"] == "tid-1"


def test_dispatch_post_chat_text():
    status, _ct, body = dispatch(
        "POST", "/api/chat/text", {}, _body({"session_id": "s1", "text": "hi"}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=FakeChatManager(),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_dispatch_post_chat_invite_connecting():
    status, _ct, body = dispatch(
        "POST", "/api/chat/invite", {},
        _body({"peer_id": "peer-1", "peer_name": "Alice"}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=FakeChatManager(),
            chat_start_session=lambda peer_id, peer_name: None,
        ),
    )
    assert status == 200
    assert json.loads(body)["connecting"] is True


def test_dispatch_post_chat_close():
    status, _ct, body = dispatch(
        "POST", "/api/chat/close", {}, _body({"session_id": "s1"}),
        object(), None, None, **_dispatch_args(chat_mgr=FakeChatManager()),
    )
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_dispatch_unknown_chat_route_404():
    status, _ct, body = dispatch(
        "GET", "/api/chat/nope", {}, b"", object(), None, None,
        **_dispatch_args(chat_mgr=FakeChatManager()),
    )
    assert status == 404


def test_dispatch_post_chat_file_resolves_basename(tmp_path):
    """The web UI uploads via /api/upload and gets a bare filename back; the
    chat/file route must resolve it against the upload dir or the stat in
    ChatManager.send_file fails against the CWD."""
    seen = {}

    class Recorder:
        def send_file(self, session_id, file_path, send_fn):
            seen["path"] = file_path
            return "tid-1"

    status, _ct, body = dispatch(
        "POST", "/api/chat/file", {},
        _body({"session_id": "s1", "file_path": "photo.jpg"}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=Recorder(),
            upload_dir=str(tmp_path),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 200
    assert seen["path"] == str(tmp_path / "photo.jpg")
    assert json.loads(body)["transfer_id"] == "tid-1"


def test_dispatch_post_chat_file_rejects_traversal(tmp_path):
    status, _ct, body = dispatch(
        "POST", "/api/chat/file", {},
        _body({"session_id": "s1", "file_path": "../evil.txt"}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=FakeChatManager(),
            upload_dir=str(tmp_path),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False


# ── Regression tests for the web-chat audit fixes ────────────────────

def test_send_text_failure_reported_as_ok_false():
    """#1: a failed send_text surfaces {ok: false} so the frontend keeps the
    draft instead of clearing it and pretending the message went out."""

    class FailingText:
        def get_sessions(self):
            return [{"session_id": "s1", "peer_id": "peer-1"}]

        def send_text(self, session_id, text, send_fn):
            return False

    data, status = chat_api.send_text(
        FailingText(), _body({"session_id": "s1", "text": "hi"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is False


def test_invite_web_host_refused_dict():
    """#6: the web host distinguishes a refused invite from a connecting one."""
    cm = FakeChatManager()
    data, status = chat_api.invite(
        cm, _body({"peer_id": "peer-1"}),
        lambda peer_id, peer_name: {"ok": False, "error": "peer_unreachable"},
    )
    assert status == 400
    assert data["ok"] is False
    assert data["error"] == "peer_unreachable"


def test_invite_web_host_connecting_dict():
    cm = FakeChatManager()
    data, status = chat_api.invite(
        cm, _body({"peer_id": "peer-1"}),
        lambda peer_id, peer_name: {"connecting": True},
    )
    assert status == 200
    assert data["connecting"] is True
    assert data["session_id"] is None


def test_invite_web_host_session_id_dict():
    cm = FakeChatManager()
    data, status = chat_api.invite(
        cm, _body({"peer_id": "peer-1"}),
        lambda peer_id, peer_name: {"session_id": "sess-web"},
    )
    assert status == 200
    assert data["session_id"] == "sess-web"


def test_chat_manager_set_receive_dir_updates_root():
    """#2: ChatManager.set_receive_dir is the live receive-dir hook main.py
    now calls; assert it actually moves _receive_dir (and is idempotent)."""
    import tempfile
    from pathlib import Path
    cm = ChatManager("dev", "Dev", receive_dir="")
    try:
        assert cm._receive_dir is None
        with tempfile.TemporaryDirectory() as td:
            cm.set_receive_dir(td)
            assert cm._receive_dir == Path(td)
        with tempfile.TemporaryDirectory() as td2:
            cm.set_receive_dir(td2)
            assert cm._receive_dir == Path(td2)
    finally:
        cm.shutdown()


def _chat_tmp_file(name: str) -> str:
    from internal.web.routes import _chat_tmp_dir
    import uuid
    return os.path.join(_chat_tmp_dir(), f"{name}-{uuid.uuid4().hex}.txt")


def test_dispatch_post_chat_file_accepts_absolute_chat_tmp_path():
    """#7: purpose=chat uploads pass an absolute path inside the chat temp
    dir; the /api/chat/file route must confine and accept it."""
    from internal.web.routes import _chat_tmp_dir
    path = _chat_tmp_file("chat-ok")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello")
        seen = {}

        class Recorder:
            def send_file(self, session_id, file_path, send_fn):
                seen["path"] = file_path
                return "tid-1"

        status, _ct, body = dispatch(
            "POST", "/api/chat/file", {},
            _body({"session_id": "s1", "file_path": path}),
            object(), None, None,
            **_dispatch_args(
                chat_mgr=Recorder(),
                chat_send_fn=lambda peer_id: lambda data: True,
            ),
        )
        assert status == 200
        assert seen["path"] == os.path.realpath(path)
        assert json.loads(body)["transfer_id"] == "tid-1"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_dispatch_post_chat_file_rejects_absolute_path_outside_tmp(tmp_path):
    """#7: an absolute path that is NOT inside the chat temp dir is refused
    (a token holder must not send arbitrary host files via chat)."""
    path = str(tmp_path / "evil.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("evil")
    status, _ct, body = dispatch(
        "POST", "/api/chat/file", {},
        _body({"session_id": "s1", "file_path": path}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=FakeChatManager(),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False


def test_dispatch_post_chat_file_cleans_up_staging_on_send_failure():
    """#9: a purpose=chat staging file is removed when the send cannot start,
    so a failed chat send leaves no orphaned upload copy."""
    path = _chat_tmp_file("chat-cleanup")
    with open(path, "w", encoding="utf-8") as f:
        f.write("hello")
    assert os.path.exists(path)

    class FailingRecorder:
        def send_file(self, session_id, file_path, send_fn):
            return None  # could not start transfer

    status, _ct, body = dispatch(
        "POST", "/api/chat/file", {},
        _body({"session_id": "s1", "file_path": path}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=FailingRecorder(),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False
    assert not os.path.exists(path), "staging file should be cleaned up"


def test_dispatch_post_chat_file_keeps_received_file_on_failure(tmp_path):
    """#9 guard: the cleanup only targets purpose=chat staging files. A failed
    send of a legacy bare-filename (a real received file) must NOT delete it."""
    recv = tmp_path / "real-received.txt"
    recv.write_text("do not delete", encoding="utf-8")

    class FailingRecorder:
        def send_file(self, session_id, file_path, send_fn):
            return None  # could not start transfer

    status, _ct, body = dispatch(
        "POST", "/api/chat/file", {},
        _body({"session_id": "s1", "file_path": "real-received.txt"}),
        object(), None, None,
        **_dispatch_args(
            chat_mgr=FailingRecorder(),
            upload_dir=str(tmp_path),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False
    assert recv.exists(), "a real received file must not be cleaned up"


# ── #4: accept-race window → explicit "expired" error ────────────────────

def test_chat_manager_accept_file_returns_none_when_offer_gone():
    """#4: ChatManager.accept_file returns None (not False) when the offer is
    already gone — it was swept by the stale-receive reaper while the UI still
    showed its Accept button.  None stays falsy so truthiness callers still
    treat it as a failed accept."""
    from internal.sync.nearby_chat import ChatManager
    mgr = ChatManager("dev", "Dev")
    try:
        assert mgr.accept_file("sess", "0" * 32, lambda data: True) is None
    finally:
        mgr.shutdown()


def test_accept_file_expired_offer_returns_explicit_error():
    """#4: the API handler translates the None sentinel into an explicit
    {ok: false, error: "expired"} so the frontend can toast "offer expired"
    instead of a generic failure."""

    class ExpiredManager:
        def accept_file(self, session_id, transfer_id, send_fn):
            return None  # offer gone — the widened accept-race case

    data, status = chat_api.accept_file(
        ExpiredManager(),
        _body({"session_id": "s1", "transfer_id": "tid-1"}), _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is False
    assert data["error"] == "expired"
