"""Round 16-A — chat over the internet (send-side mirror + relay routing).

Round 14/15 already made the public relay deliver ``CHAT_MSG_TYPES`` frames to
the receive router (``_on_relay_frame`` -> ``_on_peer_message`` -> chat_mgr);
what was missing was the SEND side — chat frames only rode the LAN transport.
This round wires ``_chat_send_fn`` so a frame also reaches an internet-paired
peer over the relay when the LAN send fails, and proves relay-arrived chat
frames reach ``chat_mgr`` attributed to the sender's real device id.

Deliberate deviations from the literal "LAN + relay mirror" design:

  * LAN-first, relay-fallback.  An unconditional relay mirror would deliver
    every chat frame TWICE to a dual-connected peer: chat has no content dedup
    (unlike clipboard history), so every message would append twice.
  * Chat file BYTES (``file_chunk``) are never mirrored — the same
    double-delivery would inflate the receiver's byte counter and fail the
    size check in ``ChatManager._finalize_receive``.
  * Chat frames now carry ``source_device`` (``nearby_chat._send_frame``) so a
    relayed copy can be attributed to the sender — the relay has no connection
    object to infer it from.
"""

import types

import pytest

from internal.protocol.codec import (
    decode_message,
    encode_binary_chunk,
    encode_frame,
)
from internal.sync.nearby_chat import ChatManager
from internal.transport import relay as R

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
            peers[pid] = types.SimpleNamespace(
                device_id=pid, paired=bool(p), device_name=pid)
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

    class Relay:
        def __init__(self):
            self.published = []

        def publish(self, frame, topic, key):
            self.published.append((frame, topic, key))
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
    app._relay_publish_to_peer = (
        lambda frame, pid, _a=app: Application._relay_publish_to_peer(_a, frame, pid))
    app._chat_send_fn = (
        lambda pid, _a=app: Application._chat_send_fn(_a, pid))
    app._peer_is_internet_reachable = (
        lambda pid, _a=app: Application._peer_is_internet_reachable(_a, pid))
    app._on_peer_message = (
        lambda msg, pid=None, _a=app: Application._on_peer_message(_a, msg, pid))
    app._on_relay_frame = (
        lambda frame, topic=None, _a=app, **kw: Application._on_relay_frame(
            _a, frame, topic, **kw))
    app._sent = sent
    app._broadcast_calls = broadcast_calls
    return app


def _chat_frame(source_device, text="hi", msg_type="chat_text"):
    return encode_frame({
        "msg_type": msg_type,
        "session_id": "0123456789abcdef",
        "text": text,
        "ts": 1.0,
    }, source_device=source_device)


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
        (frame,
         R.derive_topic("aa" * 32, "dd" * 32),
         R.derive_key("aa" * 32, "dd" * 32)),
    ]


def test_relay_publish_to_netpair_wins_over_enrolled_single_publish():
    secret = R.generate_netpair_secret()
    app = make_app_stub(
        peers={"bbbbbbbbbbbb": {"paired": True}},
        peer_relay_secrets={"bbbbbbbbbbbb": "dd" * 32},
        netpair_secrets={"bbbbbbbbbbbb": secret},
    )
    frame = _chat_frame(app.cfg.device_id)
    ok = Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    assert ok is True
    assert len(app._relay.published) == 1          # one channel, not two
    assert app._relay.published[0][1:] == (
        R.netpair_topic(secret), R.netpair_key(secret))


def test_relay_publish_to_unknown_peer_noop():
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"})
    ok = Application._relay_publish_to_peer(
        app, _chat_frame(app.cfg.device_id), "ghost")
    assert ok is False and app._relay.published == []


def test_relay_publish_to_unpaired_lan_peer_noop():
    app = make_app_stub(
        peers={"bbbbbbbbbbbb": {"paired": False}},
        peer_relay_secrets={"bbbbbbbbbbbb": "dd" * 32},
    )
    ok = Application._relay_publish_to_peer(
        app, _chat_frame(app.cfg.device_id), "bbbbbbbbbbbb")
    assert ok is False and app._relay.published == []


def test_relay_publish_to_peer_internet_sync_off_noop():
    app = make_app_stub(
        internet_sync_enabled=False,
        netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"},
    )
    ok = Application._relay_publish_to_peer(
        app, _chat_frame(app.cfg.device_id), "bbbbbbbbbbbb")
    assert ok is False and app._relay.published == []


def test_relay_publish_to_peer_relay_absent_noop():
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"})
    app._relay = None
    ok = Application._relay_publish_to_peer(
        app, _chat_frame(app.cfg.device_id), "bbbbbbbbbbbb")
    assert ok is False


def test_relay_publish_to_peer_skips_chat_file_chunk():
    # Binary chat-file bytes must stay LAN-only: mirroring them would
    # double-deliver every chunk to a dual-connected peer and break the
    # receiver's size check.
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"})
    chunk = encode_binary_chunk("a" * 32, 0, 1, b"payload")
    ok = Application._relay_publish_to_peer(app, chunk, "bbbbbbbbbbbb")
    assert ok is False and app._relay.published == []


# ------------------------------------------------------------- _chat_send_fn

def test_chat_send_fn_lan_fail_relay_mirror_returns_true():
    # Internet-only peer: the LAN send fails, the relay mirror delivers, and
    # the closure still reports delivered (chat's _send_frame tests is True).
    secret = R.generate_netpair_secret()
    app = make_app_stub(
        lan_result=False, netpair_secrets={"bbbbbbbbbbbb": secret})
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
    app = make_app_stub(
        lan_result=True, netpair_secrets={"bbbbbbbbbbbb": secret})
    fn = Application._chat_send_fn(app, "bbbbbbbbbbbb")
    frame = _chat_frame(app.cfg.device_id)
    assert fn(frame) is True
    assert app._sent == [("bbbbbbbbbbbb", frame)]
    assert app._relay.published == []


def test_chat_send_fn_both_paths_fail_returns_false():
    app = make_app_stub(lan_result=False)  # no netpair/enrolled peer -> no relay
    fn = Application._chat_send_fn(app, "bbbbbbbbbbbb")
    frame = _chat_frame(app.cfg.device_id)
    assert fn(frame) is False
    assert app._sent == [("bbbbbbbbbbbb", frame)]
    assert app._relay.published == []


def test_chat_send_fn_empty_peer_is_broadcast_and_not_mirrored():
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": "ABCDEFG"})
    fn = Application._chat_send_fn(app, None)
    assert fn == app.transport_mgr.broadcast          # broadcast closure unchanged
    data = encode_frame({
        "msg_type": "chat_invite", "session_id": "0123456789abcdef",
        "from_name": "DevA", "fingerprint_short": "", "greeting": "",
    }, source_device=app.cfg.device_id)
    assert fn(data) is True
    assert app._broadcast_calls == [data]
    assert app._relay.published == []                # broadcast never mirrors


# ----------------------------------------------- relay -> chat_mgr full chain

def test_relay_chat_text_reaches_chat_mgr_with_source_peer():
    app = make_app_stub(
        netpair_secrets={"bbbbbbbbbbbb": R.generate_netpair_secret()})
    frame = encode_frame({
        "msg_type": "chat_text", "session_id": "0123456789abcdef",
        "text": "hello over internet", "ts": 123.0,
    }, source_device="bbbbbbbbbbbb")
    app._on_relay_frame(frame, "some/netpair/topic")
    assert len(app.chat_mgr.handled) == 1
    mt, payload, sender, _fp, send_fn = app.chat_mgr.handled[0]
    assert mt == "chat_text"
    assert sender == "bbbbbbbbbbbb"                  # the frame's real device id
    assert payload["text"] == "hello over internet"
    # Replies back to that peer go through a per-peer closure, not broadcast.
    assert send_fn != app.transport_mgr.broadcast


def test_relay_chat_ping_reaches_chat_mgr_source_peer():
    # chat_ping/chat_pong are chat frames, so they ride the same mirror — the
    # heartbeat continues to work across the relay with no extra wiring.
    app = make_app_stub()
    frame = encode_frame(
        {"msg_type": "chat_ping", "session_id": "0123456789abcdef"},
        source_device="bbbbbbbbbbbb")
    app._on_relay_frame(frame, "some/topic")
    assert len(app.chat_mgr.handled) == 1
    assert app.chat_mgr.handled[0][0] == "chat_ping"
    assert app.chat_mgr.handled[0][2] == "bbbbbbbbbbbb"


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
        invite = encode_frame({
            "msg_type": "chat_invite", "session_id": sid,
            "from_name": "DevA", "fingerprint_short": "", "greeting": "",
        }, source_device=dev_a)
        app._on_relay_frame(invite, R.netpair_topic(secret))
        sess = next((s for s in cm.get_sessions() if s["session_id"] == sid), None)
        assert sess is not None and sess["peer_id"] == dev_a
        assert cm.accept_invitation(sid, lambda data: True) is True

        text = encode_frame({
            "msg_type": "chat_text", "session_id": sid,
            "text": "hi over internet", "ts": 1.0,
        }, source_device=dev_a)
        app._on_relay_frame(text, R.netpair_topic(secret))
        msgs = cm.get_messages(sid)
        assert any(e["kind"] == "text" and e["text"] == "hi over internet"
                   and not e["outgoing"] for e in msgs)
    finally:
        cm.shutdown()


def test_chat_start_session_starts_relay_chat_for_internet_peer(tmp_path):
    # A netpair peer has no LAN address, so _chat_start_session previously
    # bailed with "no address".  Round 16-A: an internet-reachable peer starts
    # the session directly and the invite rides the relay send_fn.
    secret = R.generate_netpair_secret()
    app = make_app_stub(
        lan_result=False, netpair_secrets={"bbbbbbbbbbbb": secret})
    app._chat_device_address = lambda pid: ("", 0)
    cm = ChatManager(app.cfg.device_id, "DevA", receive_dir=str(tmp_path / "chat"))
    app.chat_mgr = cm
    try:
        sid = Application._chat_start_session(
            app, "bbbbbbbbbbbb", "DevB", "FP")
        assert sid is not None
        assert any(s["session_id"] == sid and s["peer_id"] == "bbbbbbbbbbbb"
                   for s in cm.get_sessions())
        # The invite went out over the netpair relay channel (LAN has no addr).
        assert len(app._relay.published) == 1
        frame, topic, key = app._relay.published[0]
        assert (topic, key) == (R.netpair_topic(secret), R.netpair_key(secret))
        msg = decode_message(frame)
        assert msg._raw_payload["msg_type"] == "chat_invite"
    finally:
        cm.shutdown()


def test_chat_start_session_unknown_peer_still_errors(tmp_path):
    # A non-internet, address-less peer must keep failing (not silently start a
    # relay session to a device that can never receive it).
    app = make_app_stub(lan_result=False)
    app._chat_device_address = lambda pid: ("", 0)
    cm = ChatManager(app.cfg.device_id, "DevA", receive_dir=str(tmp_path / "chat"))
    app.chat_mgr = cm
    notified = []
    app.root = types.SimpleNamespace(after=lambda *a: notified.append(a))
    app._notify_info = lambda *a: None
    try:
        sid = Application._chat_start_session(app, "ghost", "Ghost", "FP")
        assert sid is None
        assert cm.get_sessions() == []
        assert app._relay.published == []
        assert notified  # the no-address notification still fired
    finally:
        cm.shutdown()
