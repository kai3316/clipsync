"""Round 11 — internet-relay integration (codec, config, main.py wiring).

The Application-level handlers are exercised through lightweight fakes so no
real app/tkinter/transport stack boots here.
"""

import json
import types

import pytest

from internal.protocol import codec
from internal.protocol.codec import encode_frame, decode_message
from internal.transport import relay as R


# ------------------------------------------------------------------ codec

def test_relay_enroll_frame_roundtrip():
    raw = {"msg_type": "relay_enroll", "relay_secret": "ab" * 32}
    data = encode_frame(raw, source_device="device-A")
    msg = decode_message(data)
    assert getattr(msg, "msg_type", "") == "relay_enroll"
    assert msg._raw_payload["relay_secret"] == "ab" * 32
    assert msg.source_device == "device-A"


def test_relay_enroll_is_paired_only():
    # must NOT be admitted from unpaired peers at the transport gate
    assert "relay_enroll" in codec.RELAY_MSG_TYPES
    assert "relay_enroll" not in codec.UNPAIRED_GATE_MSG_TYPES


# ------------------------------------------------------------------ config

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Point the config module at a throwaway directory."""
    from internal.config import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg_mod, "_config_path",
                        lambda: tmp_path / "config.json")
    yield cfg_mod


def test_config_relay_keys_roundtrip(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    cfg.internet_sync_enabled = True
    cfg.relay_brokers = ["wss://b1:8084/mqtt"]
    cfg.relay_secret = "cd" * 32
    cfg.peer_relay_secrets = {"peer-1": "ef" * 32}
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()
    assert loaded.internet_sync_enabled is True
    assert loaded.relay_brokers == ["wss://b1:8084/mqtt"]
    assert loaded.relay_secret == "cd" * 32
    assert loaded.peer_relay_secrets == {"peer-1": "ef" * 32}


def test_config_relay_bad_types_fall_back_to_defaults(isolated_config):
    cfg_mod = isolated_config
    path = cfg_mod._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    base = json.loads(json.dumps({
        "internet_sync_enabled": "yes",       # wrong type
        "relay_brokers": "not-a-list",        # wrong type
        "relay_secret": 123,                  # wrong type
        "peer_relay_secrets": {"p": 5},       # non-str value
    }))
    path.write_text(json.dumps(base), encoding="utf-8")
    loaded = cfg_mod.load()
    assert loaded.internet_sync_enabled is False
    assert isinstance(loaded.relay_brokers, list) and len(loaded.relay_brokers) == 3
    assert loaded.relay_secret == ""
    assert loaded.peer_relay_secrets == {}


# ------------------------------------------- Application handler behaviour

class FakePeers(dict):
    pass


def make_app_stub(**attrs):
    app = types.SimpleNamespace()
    cfg_opts = {k: attrs[k] for k in ("enabled", "secret", "secrets", "peers")
                if k in attrs}
    app.cfg = attrs.get("cfg") or cfg_stub(**cfg_opts)
    app._relay = attrs.get("_relay", None)
    with_relay = attrs.get("with_relay", False)
    app.peers_sent = []
    app.frames_published = []

    class T:
        def send_to_peer(self, pid, data):
            app.peers_sent.append((pid, data))

        def broadcast(self, data):
            pass
    app.transport_mgr = T()

    if attrs.get("with_relay"):
        class Relay:
            def __init__(self):
                self.published = []
                self.refreshed = 0

            def publish(self, frame, topic, key):
                self.published.append((frame, topic, key))

            def refresh_channels(self):
                self.refreshed += 1
        app._relay = Relay()
    saved = {"n": 0}

    app._save_cfg_and_peers = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._saved = saved
    # bind the real Application methods the handlers delegate to
    app._send_relay_enroll = (
        lambda pid, _a=app: Application._send_relay_enroll(_a, pid))
    app._ensure_relay_secret = (
        lambda _a=app: Application._ensure_relay_secret(_a))
    app._relay_channels = (
        lambda _a=app: Application._relay_channels(_a))
    app._on_relay_state = lambda state, _a=app: None
    app._on_relay_frame = (
        lambda frame, _a=app: Application._on_relay_frame(_a, frame))
    return app


def cfg_stub(**over):
    c = types.SimpleNamespace()
    c.internet_sync_enabled = over.get("enabled", True)
    c.relay_secret = over.get("secret", "aa" * 32)
    c.peer_relay_secrets = dict(over.get("secrets", {}))
    c.relay_brokers = ["wss://x:8884/mqtt"]

    peers = {}
    for pid, paired in over.get("peers", {"p1": True}).items():
        peers[pid] = types.SimpleNamespace(device_id=pid, paired=paired)
    c.peers = peers
    return c


from src.main import Application  # noqa: E402


def test_handle_relay_enroll_stores_and_replies():
    app = make_app_stub(with_relay=True)
    secret = "bb" * 32
    Application._handle_relay_enroll(
        app, {"relay_secret": secret}, "p1")
    assert app.cfg.peer_relay_secrets["p1"] == secret
    assert app._saved["n"] == 1
    assert app._relay.refreshed == 1
    # reply carries OUR secret to the same peer
    assert len(app.peers_sent) == 1
    pid, data = app.peers_sent[0]
    assert pid == "p1"
    msg = decode_message(data)
    assert msg._raw_payload["relay_secret"] == "aa" * 32


def test_handle_relay_enroll_rejects_garbage():
    app = make_app_stub()
    for bad in ({}, {"relay_secret": 5}, {"relay_secret": "zz" * 32},
                {"relay_secret": "ab" * 31}):
        Application._handle_relay_enroll(app, bad, "p1")
    assert app.peers_sent == []
    assert app.cfg.peer_relay_secrets == {}
    assert app._saved["n"] == 0


def test_handle_relay_enroll_ignores_unpaired_and_missing_peer():
    app = make_app_stub()
    app.cfg.peers = {}
    Application._handle_relay_enroll(app, {"relay_secret": "cc" * 32}, "ghost")
    assert app.peers_sent == []


def test_relay_publish_skips_when_disabled_or_unpaired():
    app = make_app_stub(enabled=False, with_relay=True,
                        secrets={"p1": "dd" * 32})
    Application._relay_publish_frame(app, b"frame")
    assert app._relay.published == []

    app2 = make_app_stub(enabled=True, with_relay=True,
                         secrets={"p1": "dd" * 32},
                         peers={"p1": False})   # paired=False
    Application._relay_publish_frame(app2, b"frame")
    assert app2._relay.published == []


def test_relay_publish_targets_each_enrolled_pair():
    app = make_app_stub(enabled=True, with_relay=True,
                        secrets={"p1": "dd" * 32, "p2": "ee" * 32},
                        peers={"p1": True, "p2": True, "p3": True})
    Application._relay_publish_frame(app, b"frame-bytes")
    got = {(topic, key) for _, topic, key in app._relay.published}
    expected = {
        (R.derive_topic("aa" * 32, "dd" * 32),
         R.derive_key("aa" * 32, "dd" * 32)),
        (R.derive_topic("aa" * 32, "ee" * 32),
         R.derive_key("aa" * 32, "ee" * 32)),
    }
    # p3 is paired but never enrolled -> no channel for it
    assert got == expected
    assert all(frame == b"frame-bytes" for frame, _, _ in app._relay.published)


def test_relay_channels_match_derivation_and_skip_unknowns():
    app = make_app_stub(secrets={"p1": "dd" * 32}, peers={"p1": True})
    ch = Application._relay_channels(app)
    assert ch == {R.derive_topic("aa" * 32, "dd" * 32): R.derive_key("aa" * 32, "dd" * 32)}

    app2 = make_app_stub(secret="", secrets={})
    ch2 = Application._relay_channels(app2)
    assert ch2 == {}
    assert len(app2.cfg.relay_secret) == 64          # lazily generated
    assert app2._saved["n"] == 1                     # …and persisted


def test_start_internet_sync_enrolls_paired_peers_only(monkeypatch):
    started = {}

    class FakeTransport:
        def __init__(self, brokers, get_channels, on_frame, on_state,
                     client_factory=None, sleeper=None):
            started["args"] = (brokers, get_channels, on_frame, on_state)

        def start(self):
            started["started"] = True

        def stop(self):
            started["stopped"] = True

    from internal.transport import relay as R
    monkeypatch.setattr(R, "RelayTransport", FakeTransport)

    app = make_app_stub(peers={"p1": True, "off": False})
    app.cfg.peers["ghost"] = types.SimpleNamespace(device_id="g", paired=False)
    Application._start_internet_sync(app)
    assert started.get("started") is True
    sent_ids = [pid for pid, _ in app.peers_sent]
    assert sent_ids == ["p1"]
    assert app._relay is not None


def test_stop_internet_sync_stops_transport():
    app = make_app_stub(with_relay=True)
    stopped = []
    app._relay.stop = lambda: stopped.append(1)
    Application._stop_internet_sync(app)
    assert stopped == [1]
    assert app._relay is None
    Application._stop_internet_sync(app)  # idempotent
