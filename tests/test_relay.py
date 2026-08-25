import os

"""Round 11 — internet relay core (pure logic + transport lifecycle)."""

import json
import threading
import time

import pytest

from internal.transport import relay as R

# ---------------------------------------------------------------- pure crypto

def test_topic_derivation_is_deterministic_and_symmetric():
    t1 = R.derive_topic("aaa", "bbb")
    assert t1 == R.derive_topic("bbb", "aaa")
    assert t1.startswith(R.TOPIC_PREFIX)
    assert len(t1) == len(R.TOPIC_PREFIX) + 24


def test_topic_differs_per_pair_and_hides_single_secret():
    t_ab = R.derive_topic("secret-a", "secret-b")
    t_ac = R.derive_topic("secret-a", "secret-c")
    assert t_ab != t_ac


def test_key_derivation_matches_both_sides():
    k1 = R.derive_key("s1", "s2")
    assert k1 == R.derive_key("s2", "s1")
    assert len(k1) == 32
    assert R.derive_key("s1", "s2") != R.derive_key("s1", "s3")


def test_envelope_roundtrip():
    key = R.derive_key("a", "b")
    frame = b"\x43\x53\x02" + b"payload-bytes"
    blob = R.pack_envelope(frame, key, time.time())
    assert R.open_envelope(blob, key, time.time()) == frame


def test_envelope_tamper_and_wrong_key_rejected():
    key = R.derive_key("a", "b")
    other = R.derive_key("a", "c")
    blob = bytearray(R.pack_envelope(b"hello", key, time.time()))
    blob[-3] ^= 0xFF
    assert R.open_envelope(bytes(blob), key, time.time()) is None
    good = R.pack_envelope(b"hello", key, time.time())
    assert R.open_envelope(good, other, time.time()) is None


def test_envelope_timestamp_window():
    key = R.derive_key("a", "b")
    now = time.time()
    stale = R.pack_envelope(b"x", key, now - R.RELAY_TS_WINDOW - 5)
    future = R.pack_envelope(b"x", key, now + R.RELAY_TS_WINDOW + 5)
    assert R.open_envelope(stale, key, now) is None
    assert R.open_envelope(future, key, now) is None


def test_oversized_frame_refused():
    key = R.derive_key("a", "b")
    with pytest.raises(ValueError):
        R.pack_envelope(b"x" * (R.MAX_RELAY_PAYLOAD + 1), key, time.time())


def test_missing_secrets_raise():
    with pytest.raises(ValueError):
        R.derive_topic("", "b")
    with pytest.raises(ValueError):
        R.derive_key(None, "b")


# ------------------------------------------------------------ probe

def test_probe_relay_endpoint_invalid_never_raises():
    """probe_relay_endpoint (v1.0.75 SSL fix) is a pure connectivity check that
    never raises — clicking 'test connection' must not disturb a live
    RelayTransport session.  Malformed endpoints are refused up front."""
    for bad in ("", "not a url", "wss://", "tcp://", "tcp://:9999"):
        res = R.probe_relay_endpoint(bad, timeout=0.01)
        assert isinstance(res, dict)
        assert res["ok"] is False
        assert "detail" in res


def test_probe_relay_endpoint_reports_connect_failures(monkeypatch):
    def refused(_addr, timeout=...):
        raise OSError("connection refused")

    monkeypatch.setattr(R.socket, "create_connection", refused)
    res = R.probe_relay_endpoint("tcp://broker.example:1883")
    assert res["ok"] is False
    assert res["detail"] == "connection refused"

    def hung(_addr, timeout=...):
        raise TimeoutError("timed out")

    monkeypatch.setattr(R.socket, "create_connection", hung)
    res2 = R.probe_relay_endpoint("tcp://broker.example:1883")
    assert res2["ok"] is False
    assert res2["detail"] == "timeout"


def test_probe_relay_endpoint_reports_tcp_reachable():
    """A plain TCP endpoint that accepts the connection is 'reachable'."""
    import socket
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        res = R.probe_relay_endpoint(f"tcp://127.0.0.1:{port}", timeout=2.0)
    finally:
        listener.close()
    assert res["ok"] is True
    assert res["detail"] == "reachable"
    assert res["latency_ms"] is not None


def test_probe_relay_endpoint_tls_handshake_uses_ca_bundle(monkeypatch):
    """wss:// endpoints must run a TLS handshake against the certifi CA bundle
    (v1.0.75 fix: a default context on macOS / frozen builds can't find the OS
    trust store and would report every wss:// endpoint unreachable)."""
    calls = {}

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeCtx:
        def __init__(self, cafile):
            calls["cafile"] = cafile

        def wrap_socket(self, sock, server_hostname=None):
            calls["server_hostname"] = server_hostname
            return FakeSock()

    def fake_connect(_addr, timeout=...):
        calls["connect"] = True
        return FakeSock()

    monkeypatch.setattr(R.socket, "create_connection", fake_connect)
    monkeypatch.setattr(R.ssl, "create_default_context", FakeCtx)
    monkeypatch.setattr(R, "_ca_bundle_path", lambda: "/tmp/cacert.pem")

    res = R.probe_relay_endpoint("wss://broker.example:8884/mqtt")
    assert res["ok"] is True
    assert res["detail"] == "reachable"
    assert calls["cafile"] == "/tmp/cacert.pem"
    assert calls["server_hostname"] == "broker.example"


# ------------------------------------------------------------- fake client

class FakeClient:
    """paho-shaped stand-in: captures calls, lets tests drive callbacks."""

    def __init__(self):
        self.subscribed = []
        self.published = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.disconnected = False
        self.loop_stopped = False
        self.connect_ok = True

    def ws_set_options(self, path=None):
        self.path = path

    def tls_set(self, **kwargs):
        self.tls_kwargs = kwargs

    def connect(self, host, port, keepalive=60):
        if not self.connect_ok:
            raise OSError("refused")
        self.host, self.port = host, port

    def loop_start(self):
        pass

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, bytes(payload)))

        class Info:
            rc = 0
        return Info()

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        self.loop_stopped = True

    # test helpers -----------------------------------------------------
    def fire_connect(self, index, transport, reason=0):
        transport._connected_on_broker = None
        self.on_connect(self, None, None, reason)

    def fire_message(self, transport, topic, payload):
        class Msg:
            pass
        m = Msg()
        m.topic = topic
        m.payload = payload
        self.on_message(self, None, m)


@pytest.fixture()
def channels():
    return {R.derive_topic("a", "b"): R.derive_key("a", "b")}


def make_transport(channels_dict, brokers=None):
    received = []
    states = []
    clients = []

    def factory():
        c = FakeClient()
        with clients_lock:
            clients.append(c)
        return c

    clients_lock = threading.Lock()

    def wait_for(cond, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with clients_lock:
                snapshot = list(clients)
            if cond(snapshot):
                return True
            time.sleep(0.005)
        return False

    t = R.RelayTransport(
        brokers or ["wss://broker.example:8884/mqtt"],
        get_channels=lambda: dict(channels_dict),
        on_frame=lambda frame, _topic=None: received.append(frame),
        on_state=states.append,
        client_factory=factory,
        sleeper=lambda s: None,
    )
    t._test_wait_clients = lambda n=1: wait_for(lambda cl: len(cl) >= n)
    return t, clients, received, states


# ------------------------------------------------------------ lifecycle

def test_offline_states_and_subscribe_on_connect(channels):
    t, clients, _, states = make_transport(channels)
    t.start()
    assert t.state == R.STATE_CONNECTING
    assert t._test_wait_clients(1)
    topic = next(iter(channels))
    clients[0].fire_connect(0, t)
    assert t.state == R.STATE_ONLINE
    assert clients[0].subscribed == [topic]
    t.stop()
    assert t.state == R.STATE_OFF
    assert clients[0].disconnected


def test_publish_only_when_online(channels):
    t, clients, _, _ = make_transport(channels)
    frame = b"frame-bytes"
    assert t.publish(frame, next(iter(channels)), next(iter(channels.values()))) is False
    t.start()
    assert t._test_wait_clients(1)
    clients[0].fire_connect(0, t)
    topic, key = next(iter(channels.items()))
    deadline = time.time() + 2
    while t.state != R.STATE_ONLINE and time.time() < deadline:
        time.sleep(0.005)
    assert t.publish(frame, topic, key) is True
    sent_topic, blob = clients[0].published[-1]
    assert sent_topic == topic
    assert R.open_envelope(blob, key, time.time()) == frame
    t.stop()


def test_receive_delivers_and_dedupes(channels):
    t, clients, received, _ = make_transport(channels)
    t.start()
    topic, key = next(iter(channels.items()))
    blob = R.pack_envelope(b"inner-frame", key, time.time())
    clients[0].fire_connect(0, t)
    clients[0].fire_message(t, topic, blob)
    clients[0].fire_message(t, topic, blob)          # duplicate dropped
    clients[0].fire_message(t, "unknown/topic", blob)  # not subscribed pair
    assert received == [b"inner-frame"]
    t.stop()


def test_receive_bad_payload_dropped(channels):
    t, clients, received, _ = make_transport(channels)
    t.start()
    topic, key = next(iter(channels.items()))
    clients[0].fire_connect(0, t)
    clients[0].fire_message(t, topic, json.dumps({"v": 99}).encode())
    clients[0].fire_message(t, topic, b"not-json")
    wrong_key_blob = R.pack_envelope(b"x", R.derive_key("q", "z"), time.time())
    clients[0].fire_message(t, topic, wrong_key_blob)
    assert received == []
    t.stop()


def test_failover_to_next_broker():
    ch = {R.derive_topic("a", "b"): R.derive_key("a", "b")}
    brokers = ["bad-scheme-no-host", "wss://first:8884/mqtt"]
    t, clients, _, _ = make_transport(ch, brokers=brokers)
    # first endpoint unparsable -> skipped; the client created belongs to #1
    t.start()
    parsed = [t._parse_endpoint(b) for b in brokers]
    assert parsed[0] is None
    assert parsed[1] is not None
    assert t._test_wait_clients(1)
    clients[0].fire_connect(1, t)
    deadline = time.time() + 2
    while t.state != R.STATE_ONLINE and time.time() < deadline:
        time.sleep(0.005)
    assert t.state == R.STATE_ONLINE
    t.stop()


def test_no_brokers_means_error_state():
    t, clients, _, _ = make_transport({})
    t._brokers = []
    t.start()
    assert t.state == R.STATE_ERROR
    t.stop()


def test_refresh_channels_resubscribes_new_topics():
    ch = {R.derive_topic("a", "b"): R.derive_key("a", "b")}
    t, clients, _, _ = make_transport(ch)
    t.start()
    assert t._test_wait_clients(1)
    clients[0].fire_connect(0, t)
    new_topic = R.derive_topic("c", "d")
    ch[new_topic] = R.derive_key("c", "d")
    t.refresh_channels()
    assert new_topic in clients[0].subscribed
    t.stop()


def test_stop_joins_cleanly_under_contention(channels):
    t, clients, _, _ = make_transport(channels)
    t.start()
    assert t._test_wait_clients(1)
    clients[0].fire_connect(0, t)
    stopper = threading.Thread(target=t.stop)
    stopper.start()
    clients[0].fire_connect(0, t)  # race a callback against stop
    stopper.join(timeout=5)
    assert not stopper.is_alive()


class StrictTlsClient(FakeClient):
    """paho 2.x behaviour: a SECOND tls_set() raises immediately."""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self.tls_calls = 0

    def tls_set(self, **kwargs):
        self.tls_calls += 1
        if self.tls_calls > 1:
            raise ValueError("SSL/TLS has already been configured.")

    def connect(self, host, port, keepalive=60):
        super().connect(host, port, keepalive)
        # fire CONNACK synchronously so the test never waits 10s
        self.on_connect(self, None, None, 0)
        return self


def test_connect_one_does_not_double_configure_tls(channels):
    # Regression: the worker called client.tls_set() a second time after the
    # factory already configured TLS -> paho 2.x raised "already configured",
    # which killed the relay-sync thread on real installs. The TLS belongs in
    # the factory; _connect_one must not touch it.
    calls = []

    def factory():
        c = StrictTlsClient(calls)
        calls.append(c)
        return c

    t = R.RelayTransport(
        ["wss://broker.example:8884/mqtt"],
        get_channels=lambda: dict(channels),
        on_frame=lambda *a: None,
        on_state=lambda s: None,
        client_factory=factory,
        sleeper=lambda s: None,
    )
    assert t._connect_one(0) is True
    assert t.state == R.STATE_ONLINE
    # _connect_one must not configure TLS at all — that's the factory's job
    # (a second tls_set() is what raised on real paho 2.x).
    assert calls[0].tls_calls == 0
    t.stop()


# ══════════════════════════════════════════════════
# merged from test_round11_integration.py
# ══════════════════════════════════════════════════

import types

import pytest

from internal.protocol import codec
from internal.protocol.codec import decode_message, encode_frame

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
    app._relay = attrs.get("_relay")
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
    # Round 14 netpair surface: _relay_channels now folds netpair channels in.
    app._netpair_secrets_all = (
        lambda _a=app: Application._netpair_secrets_all(_a))
    app._netpair_secret_for_topic = (
        lambda topic, _a=app: Application._netpair_secret_for_topic(_a, topic))
    app._on_relay_state = lambda state, _a=app: None
    app._on_relay_frame = (
        lambda frame, _a=app: Application._on_relay_frame(_a, frame))
    return app


def cfg_stub(**over):
    c = types.SimpleNamespace()
    c.internet_sync_enabled = over.get("enabled", True)
    c.relay_secret = over.get("secret", "aa" * 32)
    c.peer_relay_secrets = dict(over.get("secrets", {}))
    c.netpair_secrets = dict(over.get("netpair_secrets", {}))
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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, 'internal', 'web', 'static')


# ══════════════════════════════════════════════════
# split from test_round11_webui.py — settings relay + wiring + locale
# ══════════════════════════════════════════════════

# ── 2. Settings API whitelist + live state ─────────────────────────────


class _SettingsCfg:
    def __init__(self):
        self.device_id = "dev1"
        self.private_key_pem = "KEY"
        self.internet_sync_enabled = False
        self.relay_brokers = ["wss://broker.emqx.io:8884/mqtt"]
        self.relay_secret = ""
        self.peer_relay_secrets = {}


@pytest.fixture()
def sandboxed_persist(monkeypatch, tmp_path):
    """Redirect config persistence into the test sandbox."""
    from internal.web.api import settings as settings_api
    monkeypatch.setattr(
        settings_api, "_config_path", lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(
        settings_api, "save_config", lambda cfg, enc_mgr=None: None,
    )


def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


@pytest.mark.usefixtures("sandboxed_persist")
def test_settings_internet_sync_toggle_roundtrip():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    data, status = update_settings(_body({"internet_sync_enabled": True}), cfg)
    assert status == 200
    assert data["ok"] is True
    assert data["updated"]["internet_sync_enabled"] is True
    assert cfg.internet_sync_enabled is True
    data, status = update_settings(
        _body({"internet_sync_enabled": False}), cfg)
    assert status == 200 and cfg.internet_sync_enabled is False


@pytest.mark.usefixtures("sandboxed_persist")
def test_settings_internet_sync_bad_type_rejected():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    for bad in ("yes", 1, None, [True]):
        data, status = update_settings(
            _body({"internet_sync_enabled": bad}), cfg)
        assert status == 400, bad
        assert cfg.internet_sync_enabled is False


@pytest.mark.usefixtures("sandboxed_persist")
def test_settings_relay_brokers_roundtrip():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    brokers = ["wss://broker.hivemq.com:8884/mqtt",
               "wss://test.mosquitto.org:8081/mqtt"]
    data, status = update_settings(_body({"relay_brokers": brokers}), cfg)
    assert status == 200
    assert cfg.relay_brokers == brokers


@pytest.mark.usefixtures("sandboxed_persist")
def test_settings_relay_brokers_bad_type_rejected():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    original = list(cfg.relay_brokers)
    for bad in ("wss://single.example", 42, {"url": True}, None):
        data, status = update_settings(_body({"relay_brokers": bad}), cfg)
        assert status == 400, bad
        assert cfg.relay_brokers == original


def test_get_settings_exposes_relay_keys_but_never_secrets():
    from internal.web.api.settings import get_settings
    cfg = _SettingsCfg()
    cfg.internet_sync_enabled = True
    cfg.relay_secret = "ab" * 32
    cfg.peer_relay_secrets = {"peer-1": "cd" * 32}
    result, status = get_settings(cfg, get_internet_sync_state=lambda: "online")
    assert status == 200
    s = result["settings"]
    assert s["internet_sync_enabled"] is True
    assert isinstance(s["relay_brokers"], list)
    assert s["internet_sync_state"] == "online"
    # Secrets must never reach any client through this endpoint.
    assert "relay_secret" not in s
    assert "peer_relay_secrets" not in s
    assert s.get("relay_secret") != "ab" * 32


def test_get_settings_state_disabled_off_and_callback_fallback():
    from internal.web.api.settings import get_settings
    cfg = _SettingsCfg()  # internet_sync_enabled = False
    s = get_settings(cfg, get_internet_sync_state=lambda: "online")[0]["settings"]
    assert s["internet_sync_state"] == "off"  # callback ignored when off
    cfg.internet_sync_enabled = True
    s = get_settings(cfg)[0]["settings"]  # no callback -> connecting fallback
    assert s["internet_sync_state"] == "connecting"

    def _boom():
        raise RuntimeError("host gone")

    s = get_settings(cfg, get_internet_sync_state=_boom)[0]["settings"]
    assert s["internet_sync_state"] == "connecting"


def test_routes_threads_state_callback_into_settings_get():
    """The dispatcher passes get_relay_state into GET /api/settings."""
    from internal.web.routes import dispatch
    captured = {}

    def _state():
        return "error"

    def _fake_get_settings(cfg, get_internet_sync_state=None):
        captured["fn"] = get_internet_sync_state
        return {"settings": {}}, 200

    import internal.web.routes as routes_mod
    original = routes_mod.get_settings
    routes_mod.get_settings = _fake_get_settings
    try:
        status, ctype, raw = dispatch(
            "GET", "/api/settings", {}, b"", cfg=object(), history=None,
            sync_mgr=None, get_connected_ids=lambda: [], on_nav_url=None,
            on_forward_file=None, upload_dir=".", get_relay_state=_state,
        )
    finally:
        routes_mod.get_settings = original
    assert status == 200
    assert captured["fn"] is _state
    assert captured["fn"]() == "error"




# ── 4. Static wiring: WS event → store → settings panel ────────────────


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


def test_ws_js_handles_relay_state_event():
    src = _read("js", "ws.js")
    assert "case 'relay_state'" in src
    assert "store.relayState" in src
    # Only the four known states may be written into the store field.
    for state in ("off", "connecting", "online", "error"):
        assert f"'{state}'" in src.split("case 'relay_state'")[1].split("break;")[0]


def test_store_declares_relay_state_field():
    src = _read("js", "store.js")
    assert "relayState:" in src


def test_app_seeds_relay_state_from_settings_snapshot():
    src = _read("js", "app.js")
    assert "internet_sync_state" in src
    assert "store.relayState" in src


def test_settings_panel_consumes_relay_keys():
    panel = _read("components", "settings-panel.js")
    # toggle + broker save go through the standard settings API path
    assert "internet_sync_enabled" in panel
    assert "relay_brokers" in panel
    # live state row reads the store field seeded by ws.js
    assert "effectiveRelayState" in panel
    assert "relayStateKey" in panel
    # empty broker list is refused client-side before any request
    assert "relay_brokers_empty" in panel




# ── 5. Locale parity ───────────────────────────────────────────────────

_NEW_KEYS = [
    "settings_window.internet_sync_title",
    "network.internet_sync",
    "settings_window.internet_sync_state_label",
    "relay.state.off",
    "relay.state.connecting",
    "relay.state.online",
    "relay.state.error",
    "settings_window.relay_brokers_toggle",
    "settings_window.relay_brokers_label",
    "settings_window.relay_brokers_hint",
    "settings_window.save_relay_brokers",
    "settings_window.internet_sync_hint",
    "settings.relay_brokers_saved",
    "settings.relay_brokers_empty",
    "settings.relay_brokers_invalid",
    "devices.sas_label",
    "devices.sas_verify_hint",
]


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_identical():
    en, zh = _locales()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round11_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key

# ══════════════════════════════════════════════════
# split from test_round13_wrapup.py — message buckets + relay envelope
# ══════════════════════════════════════════════════


def test_message_type_buckets_disjoint():
    from internal.protocol.codec import (
        AICONFIG_MSG_TYPES,
        CHAT_MSG_TYPES,
        FILE_TRANSFER_MSG_TYPES,
        PAIRING_MSG_TYPES,
        RELAY_MSG_TYPES,
    )
    buckets = [
        AICONFIG_MSG_TYPES, CHAT_MSG_TYPES, FILE_TRANSFER_MSG_TYPES,
        PAIRING_MSG_TYPES, RELAY_MSG_TYPES,
    ]
    # A frame's msg_type must route to exactly one handler bucket — no frame
    # can be both an aiconfig frame and a relay/chat/pairing/transfer frame.
    for i in range(len(buckets)):
        for j in range(i + 1, len(buckets)):
            overlap = buckets[i] & buckets[j]
            assert not overlap, f"routing bucket overlap: {overlap}"


def test_aiconfig_and_relay_frames_stay_paired_only():
    from internal.protocol.codec import (
        AICONFIG_MSG_TYPES,
        RELAY_MSG_TYPES,
        UNPAIRED_GATE_MSG_TYPES,
    )
    # Both carry real content/identity and must never reach the app router
    # from an unpaired connection (the transport gate drops them; the
    # app-layer handlers re-check pairing as defense in depth).
    assert not (AICONFIG_MSG_TYPES & UNPAIRED_GATE_MSG_TYPES)
    assert not (RELAY_MSG_TYPES & UNPAIRED_GATE_MSG_TYPES)


def test_relay_envelope_carries_standard_frame_by_msg_type():
    """A relay envelope wraps a *standard* ClipSync frame; the inner frame's
    msg_type is what routes it.  A clipboard frame stays a clipboard frame and
    an aiconfig frame stays an aiconfig frame — the relay is only a transport
    and cannot blur the two."""
    from internal.protocol.codec import decode_message, encode_frame
    from internal.transport.relay import (
        derive_key,
        open_envelope,
        pack_envelope,
    )

    key = derive_key("secret-a", "secret-b")
    now = 1_700_000_000.0

    for payload in (
        {"msg_type": "clipboard", "types": ["text"], "content": "hi"},
        {"msg_type": "aiconfig_inv", "device_name": "d", "entries": []},
    ):
        frame = encode_frame(payload, source_device="dev-1")
        env = pack_envelope(frame, key, now)
        inner = open_envelope(env, key, now)
        assert inner is not None
        msg = decode_message(inner)
        assert msg is not None
        assert msg.msg_type == payload["msg_type"]


def test_relay_rejects_stale_or_tampered_envelope():
    from internal.protocol.codec import encode_frame
    from internal.transport.relay import (
        RELAY_TS_WINDOW,
        derive_key,
        open_envelope,
        pack_envelope,
    )

    key = derive_key("secret-a", "secret-b")
    frame = encode_frame({"msg_type": "clipboard", "content": "x"},
                         source_device="dev-1")
    env = pack_envelope(frame, key, 1_700_000_000.0)

    # Envelope from outside the tolerated clock window is dropped.
    assert open_envelope(env, key, 1_700_000_000.0 + RELAY_TS_WINDOW * 2) is None
    # Wrong key cannot be opened.
    assert open_envelope(env, derive_key("secret-a", "secret-c"),
                         1_700_000_000.0) is None


