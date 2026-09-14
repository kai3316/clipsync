"""Web Nearby-Chat API tests.

Unit-tests ``internal/web/api/chat.py`` handlers with a fake chat_mgr, the
route-level confinement of ``/api/chat/file``, the relay mirror the internet
transport publishes chat frames on, and the reachability flags the chat picker
is built from.  No real sockets are touched.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib

from internal.sync.nearby_chat import ChatManager
from internal.web.api import chat as chat_api
from internal.web.routes import dispatch


class FakeChatManager:
    """Stub matching ChatManager's public API."""

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

    def handle_message(self, msg_type, payload, sender_device_id, sender_fp_short, send_fn):
        return True

    def handle_binary_chunk(self, raw_payload, sender_device_id, send_fn):
        return False

    def mark_peer_disconnected(self, peer_id):
        return None

    def get_sessions(self):
        return [
            {
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
            }
        ]

    def get_messages(self, session_id):
        return [
            {
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
                    os.path.expanduser("~"), "Downloads", "ClipSync", "a.txt"
                ),
                "transfer_id": "tid-1",
            }
        ]

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


def test_get_messages():
    data, status = chat_api.get_messages(FakeChatManager(), {"session_id": ["s1"]})
    assert status == 200
    assert data["messages"][0]["transfer_id"] == "tid-1"


def test_get_chat_devices():
    data, status = chat_api.get_chat_devices(
        lambda: [{"peer_id": "p1", "name": "Bob", "paired": False}],
    )
    assert status == 200
    assert data["devices"][0]["peer_id"] == "p1"


def test_actions_unavailable():
    cm = None
    body = _body({"session_id": "s1", "text": "hi", "transfer_id": "t"})
    assert chat_api.get_sessions(None)[1] == 503
    assert chat_api.get_chat_devices(None)[1] == 503
    assert chat_api.invite(None, _body({"peer_id": "p"}), lambda *a: None)[1] == 503
    assert chat_api.send_text(cm, body, _send_fn_for)[1] == 503
    assert chat_api.send_file(cm, body, _send_fn_for)[1] == 503
    assert chat_api.accept_file(cm, body, _send_fn_for)[1] == 503
    assert chat_api.decline_file(cm, body, _send_fn_for)[1] == 503
    assert chat_api.cancel_file(cm, body)[1] == 503
    assert chat_api.accept_invite(cm, _body({"session_id": "s1"}), _send_fn_for)[1] == 503
    assert chat_api.decline_invite(cm, _body({"session_id": "s1"}), _send_fn_for)[1] == 503
    assert chat_api.close_session(cm, _body({"session_id": "s1"}))[1] == 503
    assert chat_api.mark_read(cm, _body({"session_id": "s1"}))[1] == 503


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
        FailingText(),
        _body({"session_id": "s1", "text": "hi"}),
        _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is False


def test_invite_web_host_refused_dict():
    """#6: the web host distinguishes a refused invite from a connecting one."""
    cm = FakeChatManager()
    data, status = chat_api.invite(
        cm,
        _body({"peer_id": "peer-1"}),
        lambda peer_id, peer_name: {"ok": False, "error": "peer_unreachable"},
    )
    assert status == 400
    assert data["ok"] is False
    assert data["error"] == "peer_unreachable"


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


def test_accept_file_none_and_false_are_distinct():
    """#7 semantics guard: the None sentinel (offer gone → explicit "expired")
    must stay distinct from False (offer exists but not acceptable right now →
    plain {ok:false}).  Callers that test `== False` versus `is None` must not
    conflate the two."""
    seen = {}

    class Probe:
        def __init__(self, ret):
            self.ret = ret

        def accept_file(self, session_id, transfer_id, send_fn):
            seen["ret"] = self.ret
            return self.ret

    # None → "expired" (with the error key so the frontend can toast it).
    data, status = chat_api.accept_file(
        Probe(None),
        _body({"session_id": "s1", "transfer_id": "t"}),
        _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is False
    assert data["error"] == "expired"

    # False → plain {ok:false}, no "expired" label.
    data, status = chat_api.accept_file(
        Probe(False),
        _body({"session_id": "s1", "transfer_id": "t"}),
        _send_fn_for,
    )
    assert status == 200
    assert data["ok"] is False
    assert "error" not in data


# ── /api/chat/file confinement ──────────────────────────────────────


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


def _chat_tmp_file(name: str) -> str:
    import uuid

    from internal.web.routes import _chat_tmp_dir

    return os.path.join(_chat_tmp_dir(), f"{name}-{uuid.uuid4().hex}.txt")


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
        "POST",
        "/api/chat/file",
        {},
        _body({"session_id": "s1", "file_path": "photo.jpg"}),
        object(),
        None,
        None,
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
        "POST",
        "/api/chat/file",
        {},
        _body({"session_id": "s1", "file_path": "../evil.txt"}),
        object(),
        None,
        None,
        **_dispatch_args(
            chat_mgr=FakeChatManager(),
            upload_dir=str(tmp_path),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False


def test_dispatch_post_chat_file_accepts_absolute_chat_tmp_path():
    """#7: purpose=chat uploads pass an absolute path inside the chat temp
    dir; the /api/chat/file route must confine and accept it."""
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
            "POST",
            "/api/chat/file",
            {},
            _body({"session_id": "s1", "file_path": path}),
            object(),
            None,
            None,
            **_dispatch_args(
                chat_mgr=Recorder(),
                chat_send_fn=lambda peer_id: lambda data: True,
            ),
        )
        assert status == 200
        assert seen["path"] == os.path.realpath(path)
        assert json.loads(body)["transfer_id"] == "tid-1"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def test_dispatch_post_chat_file_rejects_absolute_path_outside_tmp(tmp_path):
    """#7: an absolute path that is NOT inside the chat temp dir is refused
    (a token holder must not send arbitrary host files via chat)."""
    path = str(tmp_path / "evil.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("evil")
    status, _ct, body = dispatch(
        "POST",
        "/api/chat/file",
        {},
        _body({"session_id": "s1", "file_path": path}),
        object(),
        None,
        None,
        **_dispatch_args(
            chat_mgr=FakeChatManager(),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False


def test_dispatch_post_chat_file_keeps_received_file_on_failure(tmp_path):
    """#9 guard: the cleanup only targets purpose=chat staging files. A failed
    send of a legacy bare-filename (a real received file) must NOT delete it."""
    recv = tmp_path / "real-received.txt"
    recv.write_text("do not delete", encoding="utf-8")

    class FailingRecorder:
        def send_file(self, session_id, file_path, send_fn):
            return None  # could not start transfer

    status, _ct, body = dispatch(
        "POST",
        "/api/chat/file",
        {},
        _body({"session_id": "s1", "file_path": "real-received.txt"}),
        object(),
        None,
        None,
        **_dispatch_args(
            chat_mgr=FailingRecorder(),
            upload_dir=str(tmp_path),
            chat_send_fn=lambda peer_id: lambda data: True,
        ),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False
    assert recv.exists(), "a real received file must not be cleaned up"


# ── Chat frames over the internet relay ─────────────────────────────

import types

from internal.protocol.codec import (
    decode_message,
    encode_binary_chunk,
    encode_frame,
)
from internal.transport import relay as R  # noqa: N812
from src.main import Application  # noqa: E402


def make_app_stub(**attrs):
    """Application stand-in wired to a recording relay + transport + chat_mgr."""
    app = types.SimpleNamespace()
    device_id = attrs.get("device_id", "a1b2c3d4e5f6")
    peers = {}
    for pid, p in attrs.get("peers", {}).items():
        if isinstance(p, dict):
            peers[pid] = types.SimpleNamespace(
                device_id=pid,
                paired=bool(p.get("paired", True)),
                device_name=p.get("device_name", pid),
            )
        else:
            peers[pid] = types.SimpleNamespace(device_id=pid, paired=bool(p), device_name=pid)
    c = types.SimpleNamespace(
        device_id=device_id,
        device_name=attrs.get("device_name", "DevA"),
        internet_sync_enabled=attrs.get("internet_sync_enabled", True),
        relay_secret=attrs.get("relay_secret", "aa" * 32),
        peer_relay_secrets=dict(attrs.get("peer_relay_secrets", {})),
        netpair_secrets=dict(attrs.get("netpair_secrets", {})),
        relay_brokers=["wss://x:8884/mqtt"],
        peers=peers,
    )
    app.cfg = c
    app._netpair_last_seen = dict(attrs.get("_netpair_last_seen", {}))
    app._netpair_pw = lambda _a=app: Application._netpair_pw(_a)

    class Relay:
        def __init__(self):
            self.published = []
            self.published_qos = []

        def publish(self, frame, topic, key, qos=0):
            self.published.append((frame, topic, key))
            self.published_qos.append(qos)
            return True

    app._relay = Relay()

    sent = []
    broadcast_calls = []
    lan_result = attrs.get("lan_result", True)

    class TM:
        def send_to_peer(self, peer_id, data):
            sent.append((peer_id, data))
            return lan_result

        def broadcast(self, data):
            broadcast_calls.append(data)
            return True

        def get_peer_fingerprint(self, peer_id):
            return "AB:CD:EF:12:34:56:78:90"

        def get_resolved_hashes(self):
            return {}

        def get_connected_peers(self):
            return []

    app.transport_mgr = TM()

    class RecordingChatMgr:
        def __init__(self):
            self.handled = []

        def handle_message(self, msg_type, payload, sender, fp, send_fn):
            self.handled.append((msg_type, payload, sender, fp, send_fn))
            return True

    app.chat_mgr = attrs.get("chat_mgr") or RecordingChatMgr()

    app._ensure_relay_secret = lambda: app.cfg.relay_secret
    app._relay_publish_to_peer = lambda frame, pid, _a=app: Application._relay_publish_to_peer(
        _a, frame, pid
    )
    app._chat_send_fn = lambda pid, _a=app: Application._chat_send_fn(_a, pid)
    app._peer_is_internet_reachable = lambda pid, _a=app: Application._peer_is_internet_reachable(
        _a, pid
    )
    app._on_peer_message = lambda msg, pid=None, _a=app, via_relay=False: (
        Application._on_peer_message(_a, msg, pid, via_relay=via_relay)
    )
    app._on_relay_frame = lambda frame, topic=None, _a=app, **kw: Application._on_relay_frame(
        _a, frame, topic, **kw
    )
    app._sent = sent
    app._broadcast_calls = broadcast_calls
    return app


def _chat_frame(source_device, text="hi", msg_type="chat_text"):
    return encode_frame(
        {
            "msg_type": msg_type,
            "session_id": "0123456789abcdef",
            "text": text,
            "ts": 1.0,
        },
        source_device=source_device,
    )


# ------------------------------------------------------- _relay_publish_to_peer


def test_relay_publish_to_netpair_peer_uses_netpair_channel():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _chat_frame(app.cfg.device_id)
    ok = Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    assert ok is True
    assert app._relay.published == [
        (frame, R.netpair_topic(secret), R.netpair_key(secret)),
    ]


def test_relay_publish_to_lan_enrolled_paired_peer_uses_derive_channel():
    app = make_app_stub(
        peers={"bbbbbbbbbbbb": {"paired": True}},
        peer_relay_secrets={"bbbbbbbbbbbb": "dd" * 32},
    )
    frame = _chat_frame(app.cfg.device_id)
    ok = Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    assert ok is True
    assert app._relay.published == [
        (frame, R.derive_topic("aa" * 32, "dd" * 32), R.derive_key("aa" * 32, "dd" * 32)),
    ]


def test_relay_publish_to_unpaired_lan_peer_noop():
    app = make_app_stub(
        peers={"bbbbbbbbbbbb": {"paired": False}},
        peer_relay_secrets={"bbbbbbbbbbbb": "dd" * 32},
    )
    ok = Application._relay_publish_to_peer(app, _chat_frame(app.cfg.device_id), "bbbbbbbbbbbb")
    assert ok is False and app._relay.published == []


def test_relay_publish_to_peer_internet_sync_off_noop():
    app = make_app_stub(
        internet_sync_enabled=False,
        netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"},
    )
    ok = Application._relay_publish_to_peer(app, _chat_frame(app.cfg.device_id), "bbbbbbbbbbbb")
    assert ok is False and app._relay.published == []


def test_relay_publish_to_peer_publishes_chat_file_chunk_qos1():
    # Chat-file bytes now cross the relay (the whole point of internet-mode
    # file transfer).  Chunks ride QoS 1 so the broker retries them on a
    # reconnect; control frames stay QoS 0.
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"})
    chunk = encode_binary_chunk("a" * 32, 0, 1, b"payload")
    ok = Application._relay_publish_to_peer(app, chunk, "bbbbbbbbbbbb")
    assert ok is True and len(app._relay.published) == 1
    assert app._relay.published_qos == [1]
    frame = app._relay.published[0][0]
    decoded = decode_message(frame)
    assert getattr(decoded, "msg_type", "") == "file_chunk"


# ------------------------------------------------------------- _chat_send_fn


def test_chat_send_fn_lan_fail_relay_mirror_returns_true():
    # Internet-only peer: the LAN send fails, the relay mirror delivers, and
    # the closure still reports delivered (chat's _send_frame tests is True).
    secret = R.generate_netpair_secret()
    app = make_app_stub(lan_result=False, netpair_secrets={"bbbbbbbbbbbb": secret})
    fn = Application._chat_send_fn(app, "bbbbbbbbbbbb")
    frame = _chat_frame(app.cfg.device_id)
    assert fn(frame) is True
    assert app._sent == [("bbbbbbbbbbbb", frame)]
    assert app._relay.published == [
        (frame, R.netpair_topic(secret), R.netpair_key(secret)),
    ]


def test_chat_send_fn_lan_success_skips_relay_to_avoid_duplicate():
    # Dual-connected peer: LAN delivers, so the relay mirror is NOT used —
    # otherwise the same message would be appended twice on the receiver.
    secret = R.generate_netpair_secret()
    app = make_app_stub(lan_result=True, netpair_secrets={"bbbbbbbbbbbb": secret})
    fn = Application._chat_send_fn(app, "bbbbbbbbbbbb")
    frame = _chat_frame(app.cfg.device_id)
    assert fn(frame) is True
    assert app._sent == [("bbbbbbbbbbbb", frame)]
    assert app._relay.published == []


# ----------------------------------------------- relay -> chat_mgr full chain


def test_relay_chat_text_reaches_chat_mgr_with_source_peer():
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": R.generate_netpair_secret()})
    frame = encode_frame(
        {
            "msg_type": "chat_text",
            "session_id": "0123456789abcdef",
            "text": "hello over internet",
            "ts": 123.0,
        },
        source_device="bbbbbbbbbbbb",
    )
    app._on_relay_frame(frame, "some/netpair/topic")
    assert len(app.chat_mgr.handled) == 1
    mt, payload, sender, _fp, send_fn = app.chat_mgr.handled[0]
    assert mt == "chat_text"
    assert sender == "bbbbbbbbbbbb"  # the frame's real device id
    assert payload["text"] == "hello over internet"
    # Replies back to that peer go through a per-peer closure, not broadcast.
    assert send_fn != app.transport_mgr.broadcast


def test_relay_chat_text_establishes_and_updates_real_session(tmp_path):
    # End-to-end through the real ChatManager: a relayed invite establishes a
    # session keyed by the sender's real device id, and a relayed chat_text
    # lands in it — proving no chat_mgr changes were needed.
    dev_a = "aaaaaa000001"
    dev_b = "bbbbbb000002"
    secret = R.generate_netpair_secret()
    app = make_app_stub(device_id=dev_b, netpair_secrets={dev_a: secret})
    cm = ChatManager(dev_b, "DevB", receive_dir=str(tmp_path / "chat"))
    app.chat_mgr = cm
    sid = "0123456789abcdef"
    try:
        invite = encode_frame(
            {
                "msg_type": "chat_invite",
                "session_id": sid,
                "from_name": "DevA",
                "fingerprint_short": "",
                "greeting": "",
            },
            source_device=dev_a,
        )
        app._on_relay_frame(invite, R.netpair_topic(secret))
        sess = next((s for s in cm.get_sessions() if s["session_id"] == sid), None)
        assert sess is not None and sess["peer_id"] == dev_a
        assert cm.accept_invitation(sid, lambda data: True) is True

        text = encode_frame(
            {
                "msg_type": "chat_text",
                "session_id": sid,
                "text": "hi over internet",
                "ts": 1.0,
            },
            source_device=dev_a,
        )
        app._on_relay_frame(text, R.netpair_topic(secret))
        msgs = cm.get_messages(sid)
        assert any(
            e["kind"] == "text" and e["text"] == "hi over internet" and not e["outgoing"]
            for e in msgs
        )
    finally:
        cm.shutdown()


def test_chat_start_session_starts_relay_chat_for_internet_peer(tmp_path):
    # A netpair peer has no LAN address, so _chat_start_session previously
    # bailed with "no address".  Round 16-A: an internet-reachable peer starts
    # the session directly and the invite rides the relay send_fn.
    secret = R.generate_netpair_secret()
    app = make_app_stub(lan_result=False, netpair_secrets={"bbbbbbbbbbbb": secret})
    app._chat_device_address = lambda pid: ("", 0)
    cm = ChatManager(app.cfg.device_id, "DevA", receive_dir=str(tmp_path / "chat"))
    app.chat_mgr = cm
    try:
        sid = Application._chat_start_session(app, "bbbbbbbbbbbb", "DevB", "FP")
        assert sid is not None
        assert any(
            s["session_id"] == sid and s["peer_id"] == "bbbbbbbbbbbb" for s in cm.get_sessions()
        )
        # The invite went out over the netpair relay channel (LAN has no addr).
        assert len(app._relay.published) == 1
        frame, topic, key = app._relay.published[0]
        assert (topic, key) == (R.netpair_topic(secret), R.netpair_key(secret))
        msg = decode_message(frame)
        assert msg._raw_payload["msg_type"] == "chat_invite"
    finally:
        cm.shutdown()


# ══════════════════════════════════════════════════
#  Chat picker reachability flags (_get_chat_devices)
# ══════════════════════════════════════════════════
# The chat tab's target list used to offer "start chat" on a paired device
# that was plainly offline: the invite then spent 15s trying to reach a stale
# last_ip and ended in a timeout toast.  The host now stamps each row with
# what it actually knows (connected / discovered / relay_reachable) and the web
# picker keeps only rows where a frame could really go.


def _chat_dev_app(states, discovered=(), **attrs):
    """make_app_stub + the two inputs _get_chat_devices reads."""
    app = make_app_stub(**attrs)
    app.get_device_states = lambda: [dict(s) for s in states]
    app._snapshot_discovered_peers = lambda: {h: {} for h in discovered}
    return app


def _state(peer_id, **over):
    row = {
        "peer_id": peer_id,
        "name": peer_id,
        "address": "",
        "port": 0,
        "paired": True,
        "fingerprint_short": "",
        "connected": False,
    }
    row.update(over)
    return row


def _rows(app):
    return {r["peer_id"]: r for r in Application._get_chat_devices(app)}


def test_chat_devices_discovered_flag_matches_hashed_mdns_id():
    # Discovery keys peers by the HASHED device id, so a paired peer that is
    # visible right now must be recognised through the hash, not the real id.
    from internal.transport.discovery import Discovery

    real = "bbbbbbbbbbbb"
    app = _chat_dev_app([_state(real)], discovered=[Discovery._hash_device_id(real)])
    assert _rows(app)[real]["discovered"] is True


def test_chat_devices_keeps_the_original_fields():
    # The rows the desktop dashboard consumes are unchanged (flags are additive).
    app = _chat_dev_app(
        [
            _state(
                "bbbbbbbbbbbb",
                name="DevB",
                address="10.0.0.9",
                port=45001,
                fingerprint_short="AB:CD",
            )
        ]
    )
    r = _rows(app)["bbbbbbbbbbbb"]
    assert (r["name"], r["address"], r["port"], r["paired"], r["fingerprint_short"]) == (
        "DevB",
        "10.0.0.9",
        45001,
        True,
        "AB:CD",
    )
