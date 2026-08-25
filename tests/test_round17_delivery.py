"""Round 17 — internet delivery ("已送达" receipt + offline retransmission).

The relay path is QoS0 best-effort: no receipt, offline is loss.  Round 17 adds
a delivery ledger (sent → ack → delivered, or → failed on ack timeout), a
persisted offline queue flushed on reconnect / peer-active / timer, and a
``relay_ack`` receipt published back on the same encrypted relay channel when a
clipboard frame lands in the receiver's history.

Handlers are exercised through the make_app_stub pattern (mirroring
tests/test_round14_netpair.py / test_round16_chat_internet.py): no app /
tkinter / transport stack boots here, and every frame crosses the wire through
the real codec.
"""

import json
import threading
import time
import types
from collections import OrderedDict

import pytest

from internal.protocol import codec
from internal.protocol.codec import decode_message, encode_frame
from internal.transport import relay as R

from src.main import Application  # noqa: E402


# ------------------------------------------------------------------ codec

def test_relay_ack_frame_roundtrip():
    raw = {"msg_type": "relay_ack", "msg_id": "abc123", "ts": 1234.5}
    data = encode_frame(raw, source_device="device-B")
    msg = decode_message(data)
    assert getattr(msg, "msg_type", "") == "relay_ack"
    assert msg.source_device == "device-B"
    assert msg._raw_payload["msg_id"] == "abc123"
    assert msg._raw_payload["ts"] == 1234.5


def test_relay_ack_is_paired_only():
    assert "relay_ack" in codec.RELAY_MSG_TYPES
    # must never be admitted from an unpaired LAN peer at the transport gate
    assert not (codec.RELAY_MSG_TYPES & codec.UNPAIRED_GATE_MSG_TYPES)


# ------------------------------------------------------------------ stub

def _clipboard_frame(device_id, text="hello"):
    import base64 as _b
    return encode_frame({
        "msg_type": "clipboard",
        "types": {"TEXT": _b.b64encode(text.encode("utf-8")).decode("ascii")},
        "timestamp": 1.0,
    }, source_device=device_id)


def _chat_frame(device_id, text="hi", msg_type="chat_text",
                session_id="0123456789abcdef"):
    return encode_frame({
        "msg_type": msg_type,
        "session_id": session_id,
        "text": text,
        "ts": 1.0,
    }, source_device=device_id)


def make_app_stub(**attrs):
    """Application stand-in wired to a recording relay + WS + delivery state.

    Built with ``object.__new__(Application)`` so every Application class method
    (including the delivery internals that call each other) is available
    without binding each one — only the attributes those code paths touch are
    set here.
    """
    app = object.__new__(Application)
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
    app.cfg = types.SimpleNamespace(
        device_id=device_id,
        device_name=attrs.get("device_name", "DevA"),
        internet_sync_enabled=attrs.get("internet_sync_enabled", True),
        relay_secret=attrs.get("relay_secret", "aa" * 32),
        peer_relay_secrets=dict(attrs.get("peer_relay_secrets", {})),
        netpair_secrets=dict(attrs.get("netpair_secrets", {})),
        relay_brokers=["wss://x:8884/mqtt"],
        peers=peers,
        plain_text_only=False,
    )

    relay_ok = attrs.get("relay_ok", True)

    class Relay:
        def __init__(self):
            self.published = []

        def publish(self, frame, topic, key):
            self.published.append((frame, topic, key))
            return relay_ok

        def refresh_channels(self):
            pass
    app._relay = Relay()

    class WS:
        def __init__(self):
            self.broadcasts = []

        def broadcast(self, mtype, data):
            self.broadcasts.append((mtype, data))
            return 1
    app.web_server = WS()

    class SyncMgr:
        def __init__(self, accepted):
            self.accepted = accepted
            self.calls = 0

        def handle_remote_message(self, msg):
            self.calls += 1
            return self.accepted
    app.sync_mgr = SyncMgr(attrs.get("remote_accepted", True))

    class ChatMgr:
        def __init__(self, accepted):
            self.accepted = accepted
            self.handled = []

        def handle_message(self, msg_type, payload, sender, fp, send_fn):
            self.handled.append((msg_type, payload, sender))
            return self.accepted
    app.chat_mgr = ChatMgr(attrs.get("chat_accepted", True))

    class TM:
        def get_peer_fingerprint(self, peer_id):
            return "AB:CD:EF:12:34:56:78:90"

        def send_to_peer(self, peer_id, data):
            return False

        def broadcast(self, data):
            return True
    app.transport_mgr = TM()

    # Round 17 delivery state + round-15 netpair runtime state.
    app._delivery_ledger = {}
    app._delivery_queue = {}
    app._delivery_lock = threading.RLock()
    app._delivery_thread = None
    app._delivery_stop_evt = threading.Event()
    app._netpair_last_seen = {}
    app._netpair_names = {}
    app._netpair_pending = {}
    return app


def _delivery_events(app, status=None):
    evs = [d for m, d in app.web_server.broadcasts if m == "internet_delivery"]
    if status is not None:
        evs = [d for d in evs if d.get("status") == status]
    return evs


# ------------------------------------------------ ledger sent → ack → delivered

def test_publish_records_sent_and_ack_marks_delivered():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id

    # ledger row is "sent" with an ack deadline; WS event carried content_hash
    led = app._delivery_ledger["bbbbbbbbbbbb"][msg_id]
    assert led["status"] == "sent"
    assert led["deadline"] > time.time()
    assert led["content_hash"]
    ev = _delivery_events(app, "sent")
    assert ev and ev[0]["msg_id"] == msg_id
    assert ev[0]["content_hash"] == led["content_hash"]

    # ack arrives → delivered
    Application._handle_relay_ack(app, {"msg_id": msg_id, "ts": time.time()},
                                  "bbbbbbbbbbbb")
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "delivered"
    ev = _delivery_events(app, "delivered")
    assert ev and ev[0]["msg_id"] == msg_id


def test_ack_via_on_peer_message_routes_relay_ack():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    ack = encode_frame({"msg_type": "relay_ack", "msg_id": msg_id,
                        "ts": time.time()}, source_device="bbbbbbbbbbbb")
    app._on_peer_message(decode_message(ack), "bbbbbbbbbbbb")
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "delivered"


def test_ack_unknown_msg_id_ignored():
    app = make_app_stub()
    app._delivery_ledger["peer-x"] = OrderedDict()
    app._delivery_ledger["peer-x"]["known"] = {
        "msg_id": "known", "content_hash": "h", "ts": time.time(),
        "status": "sent", "deadline": time.time() + 15, "preview": "x",
    }
    Application._handle_relay_ack(app, {"msg_id": "ghost", "ts": 1.0}, "peer-x")
    assert app._delivery_ledger["peer-x"]["known"]["status"] == "sent"
    assert _delivery_events(app) == []


# ------------------------------------------------------ timeout → failed

def test_ack_timeout_marks_failed():
    app = make_app_stub()
    secret = R.generate_netpair_secret()
    app.cfg.netpair_secrets = {"peer-x": secret}
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    # inject an expired ack window
    app._delivery_ledger["peer-x"][msg_id]["deadline"] = time.time() - 1.0
    app._delivery_scan_expired()
    assert app._delivery_ledger["peer-x"][msg_id]["status"] == "failed"
    assert _delivery_events(app, "failed")


def test_same_content_delivered_not_marked_failed():
    # content-level ack fallback: a re-send of already-delivered content must
    # not be reported failed when its own ack never comes back.
    app = make_app_stub()
    app.cfg.netpair_secrets = {"peer-x": "ABCDEFG"}
    f1 = _clipboard_frame(app.cfg.device_id, text="same text")
    f2 = _clipboard_frame(app.cfg.device_id, text="same text")
    app._relay_publish_frame(f1)
    m1 = decode_message(f1).msg_id
    app._relay_publish_frame(f2)
    m2 = decode_message(f2).msg_id
    assert m1 != m2
    h = app._delivery_ledger["peer-x"][m1]["content_hash"]
    assert app._delivery_ledger["peer-x"][m2]["content_hash"] == h
    # first copy is acked (delivered), second times out
    Application._handle_relay_ack(app, {"msg_id": m1, "ts": time.time()}, "peer-x")
    app._delivery_ledger["peer-x"][m2]["deadline"] = time.time() - 1.0
    app._delivery_scan_expired()
    assert app._delivery_ledger["peer-x"][m2]["status"] == "delivered"
    assert not _delivery_events(app, "failed")


# ------------------------------------------------------------ ack sending

def test_received_clipboard_sends_ack_over_relay():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = encode_frame({
        "msg_type": "clipboard", "types": {"TEXT": "aGVsbG8="},
        "timestamp": 1.0,
    }, source_device="bbbbbbbbbbbb")
    app._on_peer_message(decode_message(frame), "bbbbbbbbbbbb")
    assert app.sync_mgr.calls == 1
    assert len(app._relay.published) == 1
    ack_frame, topic, key = app._relay.published[0]
    assert (topic, key) == (R.netpair_topic(secret), R.netpair_key(secret))
    ack = decode_message(ack_frame)
    assert ack._raw_payload["msg_type"] == "relay_ack"
    assert ack._raw_payload["msg_id"] == decode_message(frame).msg_id
    assert ack.source_device == app.cfg.device_id


def test_rejected_clipboard_sends_no_ack():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret},
                        remote_accepted=False)
    frame = encode_frame({
        "msg_type": "clipboard", "types": {"TEXT": "aGVsbG8="},
        "timestamp": 1.0,
    }, source_device="bbbbbbbbbbbb")
    app._on_peer_message(decode_message(frame), "bbbbbbbbbbbb")
    assert app.sync_mgr.calls == 1
    assert app._relay.published == []  # rejected → no receipt


def test_ack_not_sent_for_unknown_peer():
    # A peer with no relay channel must not produce an ack.
    app = make_app_stub()  # no netpair / relay-enroll peers
    frame = encode_frame({
        "msg_type": "clipboard", "types": {"TEXT": "aGVsbG8="},
        "timestamp": 1.0,
    }, source_device="ghost")
    app._on_peer_message(decode_message(frame), "ghost")
    assert app._relay.published == []


def test_ack_not_sent_for_unpaired_lan_peer():
    app = make_app_stub(peers={"bbbbbbbbbbbb": {"paired": False}},
                        peer_relay_secrets={"bbbbbbbbbbbb": "dd" * 32})
    frame = encode_frame({
        "msg_type": "clipboard", "types": {"TEXT": "aGVsbG8="},
        "timestamp": 1.0,
    }, source_device="bbbbbbbbbbbb")
    app._on_peer_message(decode_message(frame), "bbbbbbbbbbbb")
    assert app._relay.published == []


# -------------------------------------------- offline queue + persistence

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from internal.config import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_config_dir", lambda: tmp_path)
    yield cfg_mod


def test_offline_publish_enqueues_and_persists(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    # ledger row is queued + WS "queued"
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "queued"
    assert app._delivery_queue["bbbbbbbbbbbb"][msg_id]["retries"] == 0
    assert _delivery_events(app, "queued")
    # persisted atomically
    path = tmp_path / "relay_pending.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["peers"]["bbbbbbbbbbbb"][msg_id]["frame_b64"]


def test_queue_roundtrip_on_restart(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app1 = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _clipboard_frame(app1.cfg.device_id)
    app1._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id

    # "crash": a brand-new app loads the same file on _delivery_start
    app2 = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    app2._delivery_start()
    try:
        assert msg_id in app2._delivery_queue["bbbbbbbbbbbb"]
        assert app2._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "queued"
        assert app2._delivery_queue["bbbbbbbbbbbb"][msg_id]["frame_b64"]
    finally:
        app2._delivery_stop()


def test_online_enqueue_does_not_persist(isolated_config, tmp_path):
    # when the relay is online the frame is tracked as sent, never queued
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=True)
    app._relay_publish_frame(_clipboard_frame(app.cfg.device_id))
    assert app._delivery_queue == {}
    assert not (tmp_path / "relay_pending.json").exists()


# --------------------------------------------- retransmission triggers

def test_relay_online_trigger_flushes_queue(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    assert msg_id in app._delivery_queue["bbbbbbbbbbbb"]
    # relay comes online → flush: switch the stub relay to online behaviour
    app._relay.published.clear()
    app._relay.publish = lambda f, t, k: (app._relay.published.append(
        (f, t, k)) or True)
    Application._on_relay_state(app, "online")
    assert msg_id not in app._delivery_queue.get("bbbbbbbbbbbb", {})
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "sent"
    assert _delivery_events(app, "sent")
    # the queue file was rewritten without the flushed row
    data = json.loads((tmp_path / "relay_pending.json").read_text(encoding="utf-8"))
    assert not data.get("peers", {}).get("bbbbbbbbbbbb", {}).get(msg_id)


def test_peer_active_trigger_flushes_queue(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    assert msg_id in app._delivery_queue["bbbbbbbbbbbb"]
    # a frame from the peer arrives (active signal) while relay now online
    app._relay.published.clear()
    app._relay.publish = lambda f, t, k: (app._relay.published.append(
        (f, t, k)) or True)
    incoming = encode_frame({
        "msg_type": "clipboard", "types": {"TEXT": "aGVsbG8="},
        "timestamp": 2.0,
    }, source_device="bbbbbbbbbbbb")
    app._on_peer_message(decode_message(incoming), "bbbbbbbbbbbb")
    assert msg_id not in app._delivery_queue.get("bbbbbbbbbbbb", {})
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "sent"


def test_timer_retry_flushes_queue(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    app._relay.published.clear()
    app._relay.publish = lambda f, t, k: (app._relay.published.append(
        (f, t, k)) or True)
    app._delivery_retry_queue()
    assert msg_id not in app._delivery_queue.get("bbbbbbbbbbbb", {})
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "sent"


def test_retry_limit_marks_failed_and_removes(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    for _ in range(5):
        app._delivery_retry_peer("bbbbbbbbbbbb")
    assert "bbbbbbbbbbbb" not in app._delivery_queue or \
        msg_id not in app._delivery_queue.get("bbbbbbbbbbbb", {})
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "failed"
    assert _delivery_events(app, "failed")
    # queue file no longer contains the exhausted row
    data = json.loads((tmp_path / "relay_pending.json").read_text(encoding="utf-8"))
    peers = data.get("peers", {})
    assert not peers.get("bbbbbbbbbbbb", {}).get(msg_id)


# ---------------------------------------------------------------- REST

def test_delivery_status_shape_and_content_hash():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _clipboard_frame(app.cfg.device_id)
    app._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    data = app._delivery_status("bbbbbbbbbbbb")
    assert data["pending"] == 0
    assert len(data["sends"]) == 1
    row = data["sends"][0]
    assert row["msg_id"] == msg_id
    assert row["status"] == "sent"
    assert row["preview"] == "hello"
    assert row["content_hash"] == app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["content_hash"]
    assert isinstance(row["ts"], (int, float))
    # aggregate view without peer_id
    agg = app._delivery_status()
    assert agg["pending"] == 0 and len(agg["sends"]) == 1


def test_delivery_counts_per_peer_badges(isolated_config, tmp_path):
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    app._relay_publish_frame(_clipboard_frame(app.cfg.device_id))
    counts = app._delivery_counts()
    assert counts["peers"].get("bbbbbbbbbbbb") == 1


def test_api_routes_with_bound_app(isolated_config, tmp_path):
    from internal.web.api import internetdelivery as api
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    app._relay_publish_frame(_clipboard_frame(app.cfg.device_id))
    api.bind(app)
    try:
        data, status = api.handle(
            "GET", "/api/internetdelivery", {"peer_id": ["bbbbbbbbbbbb"]}, b"")
        assert status == 200 and data["pending"] == 1
        assert data["sends"][0]["content_hash"]
        data, status = api.handle("GET", "/api/internetdelivery/counts", {}, b"")
        assert status == 200 and data["peers"].get("bbbbbbbbbbbb") == 1
        data, status = api.handle("GET", "/api/internetdelivery/nope", {}, b"")
        assert status == 404
    finally:
        api.bind(None)


def test_api_unbound_returns_503():
    from internal.web.api import internetdelivery as api
    api.bind(None)
    try:
        data, status = api.handle("GET", "/api/internetdelivery", {}, b"")
        assert status == 503
    finally:
        api.bind(None)


# ------------------------------------------- chat delivery over the relay

def test_relay_chat_frame_ledgered_sent():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _chat_frame(app.cfg.device_id, text="hello chat")
    ok = Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    assert ok is True
    msg_id = decode_message(frame).msg_id
    led = app._delivery_ledger["bbbbbbbbbbbb"][msg_id]
    assert led["status"] == "sent"
    assert led["kind"] == "chat_text"
    assert led["session_id"] == "0123456789abcdef"
    assert led["preview"] == "hello chat"
    assert led["content_hash"] == ""
    # WS event carries chat matching fields
    ev = _delivery_events(app, "sent")
    assert ev and ev[0]["kind"] == "chat_text"
    assert ev[0]["session_id"] == "0123456789abcdef"


def test_relay_chat_ack_marks_delivered():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _chat_frame(app.cfg.device_id)
    Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    msg_id = decode_message(frame).msg_id
    Application._handle_relay_ack(app, {"msg_id": msg_id, "ts": time.time()},
                                  "bbbbbbbbbbbb")
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "delivered"
    assert _delivery_events(app, "delivered")[0]["kind"] == "chat_text"


def test_relay_chat_timeout_marks_failed():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    frame = _chat_frame(app.cfg.device_id)
    Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    msg_id = decode_message(frame).msg_id
    app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["deadline"] = time.time() - 1.0
    app._delivery_scan_expired()
    assert app._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "failed"


def test_relay_chat_received_ack_is_published():
    # Receiver side: a relayed chat frame the chat layer accepts earns a
    # relay_ack back over the same netpair channel.
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"a1b2c3d4e5f6": secret})
    frame = _chat_frame("a1b2c3d4e5f6", text="over internet")
    app._on_peer_message(decode_message(frame), "a1b2c3d4e5f6")
    assert app.chat_mgr.handled and app.chat_mgr.handled[0][0] == "chat_text"
    assert len(app._relay.published) == 1
    ack_frame, topic, key = app._relay.published[0]
    assert (topic, key) == (R.netpair_topic(secret), R.netpair_key(secret))
    ack = decode_message(ack_frame)
    assert ack._raw_payload["msg_type"] == "relay_ack"
    assert ack._raw_payload["msg_id"] == decode_message(frame).msg_id
    assert ack.source_device == app.cfg.device_id


def test_relay_chat_no_ack_when_rejected():
    app = make_app_stub(chat_accepted=False,
                        netpair_secrets={"a1b2c3d4e5f6": "ABCDEFG"})
    frame = _chat_frame("a1b2c3d4e5f6")
    app._on_peer_message(decode_message(frame), "a1b2c3d4e5f6")
    assert app.chat_mgr.handled and app._relay.published == []


def test_relay_chat_offline_not_queued(isolated_config, tmp_path):
    # Chat gets confirmation but NO offline queue — that stays clipboard-only.
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret}, relay_ok=False)
    frame = _chat_frame(app.cfg.device_id)
    ok = Application._relay_publish_to_peer(app, frame, "bbbbbbbbbbbb")
    assert ok is False
    assert app._delivery_queue == {}
    assert app._delivery_ledger == {}


def test_delivery_status_includes_chat_kind_and_session():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"bbbbbbbbbbbb": secret})
    Application._relay_publish_to_peer(
        app, _chat_frame(app.cfg.device_id), "bbbbbbbbbbbb")
    data = app._delivery_status("bbbbbbbbbbbb")
    assert data["pending"] == 0
    row = data["sends"][0]
    assert row["kind"] == "chat_text"
    assert row["session_id"] == "0123456789abcdef"
    assert row["content_hash"] == ""


def test_full_chat_delivery_chain_over_relay(isolated_config, tmp_path):
    """A relay-sends chat_text → B's chat layer accepts → B acks → A delivered."""
    secret = R.generate_netpair_secret()
    a = make_app_stub(device_id="a1b2c3d4e5f6", device_name="DevA",
                      netpair_secrets={"bbbbbbbbbbbb": secret})
    b = make_app_stub(device_id="bbbbbbbbbbbb", device_name="DevB",
                      netpair_secrets={"a1b2c3d4e5f6": secret})
    frame = _chat_frame(a.cfg.device_id, text="hi over relay")
    Application._relay_publish_to_peer(a, frame, "bbbbbbbbbbbb")
    msg_id = decode_message(frame).msg_id
    assert a._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "sent"
    # B receives the chat frame over its netpair channel.
    pub = a._relay.published[0]
    b._on_relay_frame(pub[0], pub[1])
    assert len(b._relay.published) == 1
    ack_frame, ack_topic, ack_key = b._relay.published[0]
    ack = decode_message(ack_frame)
    assert ack._raw_payload["msg_type"] == "relay_ack"
    assert ack._raw_payload["msg_id"] == msg_id
    # B's ack reaches A → delivered.
    a._on_relay_frame(ack_frame, ack_topic)
    assert a._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "delivered"


def test_chat_entry_msg_id_matches_frame(tmp_path):
    # Round 17: the outgoing ChatEntry carries the same frame msg_id the relay
    # ledger / internet_delivery events use, so the frontend can match a
    # receipt to the exact bubble.
    from internal.sync.nearby_chat import ChatManager
    dev_b = "bbbbbbbbbbbb"
    secret = R.generate_netpair_secret()
    app = make_app_stub(device_id=dev_b,
                        netpair_secrets={"a1b2c3d4e5f6": secret})
    cm = ChatManager(dev_b, "DevB", receive_dir=str(tmp_path / "chat"))
    app.chat_mgr = cm
    try:
        sid = "0123456789abcdef"
        invite = encode_frame({
            "msg_type": "chat_invite", "session_id": sid,
            "from_name": "DevA", "fingerprint_short": "", "greeting": "",
        }, source_device="a1b2c3d4e5f6")
        app._on_relay_frame(invite, R.netpair_topic(secret))
        assert cm.accept_invitation(sid, lambda data: True) is True
        send_fn = Application._chat_send_fn(app, "a1b2c3d4e5f6")
        app._relay.published.clear()
        ok = cm.send_text(sid, "hello there", send_fn)
        assert ok is True
        assert len(app._relay.published) == 1
        msg_id = decode_message(app._relay.published[0][0]).msg_id
        entries = cm.get_messages(sid)
        entry = next(e for e in entries if e.get("kind") == "text"
                     and e.get("outgoing"))
        assert entry["msg_id"] == msg_id
        assert app._delivery_ledger["a1b2c3d4e5f6"][msg_id]["status"] == "sent"
    finally:
        cm.shutdown()


# ------------------------------------------------------------ full chain

def test_full_delivery_chain_over_relay(isolated_config, tmp_path):
    """A publishes (relay online) → B receives clipboard → B acks → A delivered."""
    secret = R.generate_netpair_secret()
    a = make_app_stub(device_id="a1b2c3d4e5f6", device_name="DevA",
                      netpair_secrets={"bbbbbbbbbbbb": secret})
    b = make_app_stub(device_id="bbbbbbbbbbbb", device_name="DevB",
                      netpair_secrets={"a1b2c3d4e5f6": secret})
    # A mirrors a clipboard frame to B over the netpair channel.
    frame = _clipboard_frame(a.cfg.device_id, text="over the internet")
    a._relay_publish_frame(frame)
    msg_id = decode_message(frame).msg_id
    assert a._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "sent"
    # Deliver A's mirror to B; B accepts and publishes a relay_ack.
    pub = a._relay.published[0]
    b._on_relay_frame(pub[0], pub[1])
    assert len(b._relay.published) == 1
    ack_frame, ack_topic, ack_key = b._relay.published[0]
    ack = decode_message(ack_frame)
    assert ack._raw_payload["msg_type"] == "relay_ack"
    assert ack._raw_payload["msg_id"] == msg_id
    # Deliver B's ack to A over the same channel → delivered.
    a._on_relay_frame(ack_frame, ack_topic)
    assert a._delivery_ledger["bbbbbbbbbbbb"][msg_id]["status"] == "delivered"
