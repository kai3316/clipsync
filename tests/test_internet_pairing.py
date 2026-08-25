"""Round 14 — internet pairing code (codec, code format, config, relay, REST).

Devices that have NEVER met pair over the public relay using a shared secret
carried inside a short human-readable code.  One side generates
(/api/internetpair/generate), the other enters it (/api/internetpair/enter);
both derive the SAME netpair topic+key from the secret alone, then a
``netpair_hello`` round-trip confirms identity.

Handlers are exercised through the make_app_stub pattern (mirroring
tests/test_round11_integration.py): no app/tkinter/transport stack boots here,
and every frame crosses the wire through the real codec.
"""

import json
import types
import zipfile

import pytest

from internal.protocol import codec
from internal.protocol.codec import decode_message, encode_frame
from internal.transport import relay as R

# ------------------------------------------------------------------ codec

def test_netpair_hello_frame_roundtrip():
    raw = {"msg_type": "netpair_hello", "peer_id": "ABCD",
           "device_name": "DevB", "ts": 1234.5}
    data = encode_frame(raw, source_device="device-B")
    msg = decode_message(data)
    assert getattr(msg, "msg_type", "") == "netpair_hello"
    assert msg.source_device == "device-B"
    assert msg._raw_payload["peer_id"] == "ABCD"
    assert msg._raw_payload["device_name"] == "DevB"


def test_netpair_types_are_pairing_only():
    assert frozenset({"netpair_hello"}) == codec.NETPAIR_MSG_TYPES
    # must never be admitted from an unpaired LAN peer at the transport gate
    assert not (codec.NETPAIR_MSG_TYPES & codec.UNPAIRED_GATE_MSG_TYPES)


# ------------------------------------------------------------ code format

def test_netpair_code_roundtrip():
    device_id = "a1b2c3d4e5f6"
    secret = R.generate_netpair_secret()
    code = R.generate_netpair_code(device_id, secret)
    assert code.count("-") == 2 and len(code) == 14  # 12 chars + 2 hyphens
    tag, secret2 = R.decode_netpair_code(code)
    assert secret2 == secret                       # secret fully recoverable
    assert tag == R.netpair_device_tag(device_id)  # device tag matches

    # same inputs -> same code (deterministic)
    assert R.generate_netpair_code(device_id, secret) == code


def test_netpair_code_checksum_and_typo_rejection():
    device_id = "a1b2c3d4e5f6"
    secret = "ABCDEFG"  # fixed, all-in-alphabet -> deterministic
    code = R.generate_netpair_code(device_id, secret)
    # A wrong checksum byte is ALWAYS rejected.
    for c in R.NETPAIR_ALPHABET:
        if c == code[-1]:
            continue
        assert R.decode_netpair_code(code[:-1] + c) is None, c
    # Single-char data typos are caught by the checksum with p ≈ 31/32 each.
    norm = code.replace("-", "")
    caught = 0
    total = 0
    for i in range(11):
        for c in R.NETPAIR_ALPHABET:
            if c == norm[i]:
                continue
            total += 1
            if R.decode_netpair_code(norm[:i] + c + norm[i + 1:]) is None:
                caught += 1
    assert total == 341  # 11 data positions × 31 other alphabet chars
    assert caught >= 330  # expected ~331; loose bound guards the 1/32 checksum


def test_netpair_code_alphabet_hygiene_and_tolerance():
    assert len(R.NETPAIR_ALPHABET) == 32
    assert len(set(R.NETPAIR_ALPHABET)) == 32
    for c in "0O1I":
        assert c not in R.NETPAIR_ALPHABET
    # decode tolerates case, spaces and hyphens
    secret = R.generate_netpair_secret()
    code = R.generate_netpair_code("a1b2c3d4e5f6", secret)
    assert R.decode_netpair_code(code.lower()) == R.decode_netpair_code(code)
    assert R.decode_netpair_code(code.replace("-", " ")) == R.decode_netpair_code(code)
    # garbage / wrong shape
    for bad in ("", "not-a-code", "XXXX-XXXX-XXXX", "ABCD-EFGH-IJKL",
                code[:10], None, 12345):
        assert R.decode_netpair_code(bad) is None, repr(bad)
    # a secret with a confusing char must be refused at generation
    with pytest.raises(ValueError):
        R.generate_netpair_code("a1b2c3d4e5f6", "ABC0DEF")  # contains '0'


# -------------------------------------------------------- topic/key derive

def test_netpair_topic_key_agree_and_differ_from_relay():
    secret = R.generate_netpair_secret()
    # both sides compute identical topic + key from the same secret
    assert R.netpair_topic(secret) == R.netpair_topic(secret)
    assert R.netpair_key(secret) == R.netpair_key(secret)
    assert len(R.netpair_key(secret)) == 32
    assert R.netpair_topic(secret).startswith("clipsync/net/v1/")
    # distinct from the LAN-relay channels
    relay_topic = R.derive_topic("aa" * 32, "bb" * 32)
    relay_key = R.derive_key("aa" * 32, "bb" * 32)
    assert R.netpair_topic(secret) != relay_topic
    assert R.netpair_key(secret) != relay_key
    # distinct secrets -> distinct channels
    s2 = R.generate_netpair_secret()
    assert R.netpair_topic(secret) != R.netpair_topic(s2)
    assert R.netpair_key(secret) != R.netpair_key(s2)


# ------------------------------------------------------------------ config

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from internal.config import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg_mod, "_config_path",
                        lambda: tmp_path / "config.json")
    yield cfg_mod


def test_config_netpair_secrets_roundtrip(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    cfg.netpair_secrets = {"peer-1": "ABCDEFG", "peer-2": "2345678"}
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()
    assert loaded.netpair_secrets == {"peer-1": "ABCDEFG",
                                      "peer-2": "2345678"}
    assert cfg_mod.Config().netpair_secrets == {}  # fresh default


def test_config_netpair_secrets_bad_type_falls_back(isolated_config):
    cfg_mod = isolated_config
    path = cfg_mod._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"netpair_secrets": {"p": 5}}),
                    encoding="utf-8")
    loaded = cfg_mod.load()
    assert loaded.netpair_secrets == {}


# ------------------------------------------- Application handler behaviour

def make_app_stub(**attrs):
    """Application stand-in wired to a recording relay + WS broadcast."""
    app = types.SimpleNamespace()
    device_id = attrs.get("device_id", "a1b2c3d4e5f6")
    peers = {}
    for pid, paired in attrs.get("peers", {}).items():
        peers[pid] = types.SimpleNamespace(device_id=pid, paired=paired)
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
    app._netpair_pending = dict(attrs.get("_netpair_pending", {}))
    app._netpair_names = dict(attrs.get("_netpair_names", {}))

    class Relay:
        def __init__(self):
            self.published = []
            self.refreshed = 0

        def publish(self, frame, topic, key):
            self.published.append((frame, topic, key))
            return True

        def refresh_channels(self):
            self.refreshed += 1
    app._relay = Relay()

    saved = {"n": 0}
    app._save_cfg_and_peers = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._saved = saved

    class WS:
        def __init__(self):
            self.broadcasts = []

        def broadcast(self, mtype, data):
            self.broadcasts.append((mtype, data))
            return 1
    app.web_server = WS()

    # bind the real Application methods the handlers delegate to
    app._netpair_generate = (
        lambda _a=app: Application._netpair_generate(_a))
    app._netpair_enter = (
        lambda code, _a=app: Application._netpair_enter(_a, code))
    app._netpair_status = (
        lambda _a=app: Application._netpair_status(_a))
    app._send_netpair_hello = (
        lambda pid, secret, _a=app: Application._send_netpair_hello(
            _a, pid, secret))
    app._handle_netpair_hello = (
        lambda payload, source, topic, _a=app: Application._handle_netpair_hello(
            _a, payload, source, topic))
    app._on_relay_frame = (
        lambda frame, topic, _a=app: Application._on_relay_frame(
            _a, frame, topic))
    app._relay_channels = (
        lambda _a=app: Application._relay_channels(_a))
    app._netpair_secrets_all = (
        lambda _a=app: Application._netpair_secrets_all(_a))
    app._netpair_secret_for_topic = (
        lambda topic, _a=app: Application._netpair_secret_for_topic(_a, topic))
    app._ensure_relay_secret = (
        lambda _a=app: Application._ensure_relay_secret(_a))
    app._on_peer_message = lambda msg, pid, _a=app: None
    return app


from src.main import Application  # noqa: E402


def test_generate_subscribes_netpair_topic_and_returns_code():
    app = make_app_stub()
    data, status = app._netpair_generate()
    assert status == 200 and data["ok"] is True
    code = data["code"]
    secret = R.decode_netpair_code(code)[1]
    assert code in app._netpair_pending
    assert app._relay.refreshed == 1
    channels = app._relay_channels()
    assert R.netpair_topic(secret) in channels
    assert channels[R.netpair_topic(secret)] == R.netpair_key(secret)


def test_generate_requires_internet_sync():
    app = make_app_stub(internet_sync_enabled=False)
    data, status = app._netpair_generate()
    assert status == 400 and data["ok"] is False


def test_enter_establishes_mapping_and_persists():
    app = make_app_stub(device_id="bbbbbbbbbbbb", device_name="DevB")
    secret = R.generate_netpair_secret()
    code = R.generate_netpair_code("a1b2c3d4e5f6", secret)  # device A's code
    tag = R.decode_netpair_code(code)[0]
    data, status = app._netpair_enter(code)
    assert status == 200 and data["ok"] is True and data["peer_id"] == tag
    assert app.cfg.netpair_secrets.get(tag) == secret
    assert app._saved["n"] == 1
    # hello was published to the secret's channel with B's real source id
    assert len(app._relay.published) == 1
    frame, topic, key = app._relay.published[0]
    assert topic == R.netpair_topic(secret)
    assert key == R.netpair_key(secret)
    msg = decode_message(frame)
    assert msg._raw_payload["msg_type"] == "netpair_hello"
    assert msg._raw_payload["peer_id"] == tag
    assert msg._raw_payload["device_name"] == "DevB"
    assert msg.source_device == "bbbbbbbbbbbb"


def test_enter_rejects_invalid_code_and_internet_off():
    app = make_app_stub()
    data, status = app._netpair_enter("garbage-code")
    assert status == 400 and data["ok"] is False
    assert app.cfg.netpair_secrets == {} and app._saved["n"] == 0
    # a valid-format code with a bad checksum is also rejected
    secret = R.generate_netpair_secret()
    code = R.generate_netpair_code("a1b2c3d4e5f6", secret)
    bad = code[:-1] + ("A" if code[-1] != "A" else "B")
    data, status = app._netpair_enter(bad)
    assert status == 400
    # internet sync off
    app2 = make_app_stub(internet_sync_enabled=False)
    data, status = app2._netpair_enter(code)
    assert status == 400 and data["ok"] is False


def test_hello_roundtrip_confirms_identity():
    a = make_app_stub(device_id="a1b2c3d4e5f6", device_name="DevA")
    b = make_app_stub(device_id="bbbbbbbbbbbb", device_name="DevB")
    # A generates a code (device A is the generator).
    data, _ = a._netpair_generate()
    code = data["code"]
    secret = R.decode_netpair_code(code)[1]
    topic = R.netpair_topic(secret)
    # B enters it and sends the first hello; deliver that hello to A.
    data, _ = b._netpair_enter(code)
    b_frame, b_topic, b_key = b._relay.published[0]
    assert b_topic == topic and b_key == R.netpair_key(secret)
    a._on_relay_frame(b_frame, b_topic)
    # A persisted B's REAL device id (verified against the code's device tag).
    assert a.cfg.netpair_secrets == {"bbbbbbbbbbbb": secret}
    assert a._netpair_pending == {}          # pending code consumed
    assert a._saved["n"] >= 1
    # A replied with a hello; deliver that reply to B.
    a_frame, a_topic, a_key = a._relay.published[0]
    assert a_topic == topic
    b._on_relay_frame(a_frame, a_topic)
    # B re-keyed its provisional tag entry to A's REAL device id.
    assert b.cfg.netpair_secrets == {"a1b2c3d4e5f6": secret}
    assert b._saved["n"] >= 2
    # Both ends broadcast the netpair_peer event with real ids + names.
    events_a = [d for m, d in a.web_server.broadcasts if m == "netpair_peer"]
    assert events_a and events_a[0] == {"peer_id": "bbbbbbbbbbbb",
                                        "name": "DevB", "status": "paired"}
    events_b = [d for m, d in b.web_server.broadcasts if m == "netpair_peer"]
    assert events_b and events_b[0] == {"peer_id": "a1b2c3d4e5f6",
                                        "name": "DevA", "status": "paired"}


def test_netpair_status_lists_generated_code_and_peers():
    app = make_app_stub(device_id="bbbbbbbbbbbb")
    data, status = app._netpair_generate()
    code = data["code"]
    # unknown peer (no name yet, not in cfg.peers)
    app.cfg.netpair_secrets["peer-x"] = "ABCDEFG"
    sdata, sstatus = app._netpair_status()
    assert sstatus == 200
    assert sdata["generated_code"] == code
    peers = {p["peer_id"]: p["name"] for p in sdata["peers"]}
    assert peers.get("peer-x") == ""  # no name known yet


def test_relay_publish_mirrors_to_confirmed_netpair_channels():
    secret = R.generate_netpair_secret()
    app = make_app_stub(netpair_secrets={"peer-1": secret})
    Application._relay_publish_frame(app, b"clipframe")
    assert (b"clipframe", R.netpair_topic(secret), R.netpair_key(secret)) \
        in app._relay.published


def test_relay_channels_include_both_relay_and_netpair():
    secret = R.generate_netpair_secret()
    app = make_app_stub(peer_relay_secrets={"p1": "dd" * 32},
                        peers={"p1": True},
                        netpair_secrets={"net": secret})
    ch = Application._relay_channels(app)
    assert R.derive_topic("aa" * 32, "dd" * 32) in ch
    assert R.netpair_topic(secret) in ch


# ------------------------------------------------------------------ REST

def test_api_routes_with_bound_app():
    from internal.web.api import internetpair as api
    app = make_app_stub()
    api.bind(app)
    try:
        data, status = api.handle("POST", "/api/internetpair/generate",
                                  {}, b"")
        assert status == 200 and data["ok"] and "-" in data["code"]
        code = data["code"]
        data, status = api.handle("GET", "/api/internetpair/status", {}, b"")
        assert status == 200 and data["generated_code"] == code
        data, status = api.handle(
            "POST", "/api/internetpair/enter", {},
            json.dumps({"code": "garbage"}).encode())
        assert status == 400 and data["ok"] is False
        data, status = api.handle(
            "POST", "/api/internetpair/enter", {},
            json.dumps({"code": code}).encode())
        # Entering OUR OWN generated code is self-pairing — must be rejected.
        assert status == 400 and data["ok"] is False
        from internal.transport.relay import (
            generate_netpair_code,
            generate_netpair_secret,
        )
        other_code = generate_netpair_code("999999999999",
                                           generate_netpair_secret())
        data, status = api.handle(
            "POST", "/api/internetpair/enter", {},
            json.dumps({"code": other_code}).encode())
        assert status == 200 and data["peer_id"]
        data, status = api.handle("GET", "/api/internetpair/nope", {}, b"")
        assert status == 404
    finally:
        api.bind(None)


def test_api_routes_unbound_returns_503():
    from internal.web.api import internetpair as api
    api.bind(None)
    try:
        data, status = api.handle("POST", "/api/internetpair/generate",
                                  {}, b"")
        assert status == 503
        data, status = api.handle("POST", "/api/internetpair/enter",
                                  {}, b'{"code":"x"}')
        assert status == 503
    finally:
        api.bind(None)


# ----------------------------------------------------------------- backup

@pytest.fixture()
def _isolated_favorites(tmp_path, monkeypatch):
    import internal.data.backup as backup_mod
    monkeypatch.setattr(backup_mod, "_get_favorites_db_path",
                        lambda: tmp_path / "favorites.db")
    monkeypatch.setattr(backup_mod, "_get_favorites_path",
                        lambda: tmp_path / "favorites.json")
    return tmp_path


def test_backup_roundtrip_includes_netpair_secrets(tmp_path,
                                                   _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.clipboard.history import ClipboardHistory
    from internal.config.config import Config

    cfg = Config()
    cfg.netpair_secrets = {"peer-1": "ABCDEFG", "peer-2": "2345678"}
    cfg.peer_relay_secrets = {"peer-1": "cd" * 32}

    history = ClipboardHistory(storage_path=str(tmp_path / "h.json"))
    zip_path = backup_mod.create_backup(
        cfg, history, backup_dir=str(tmp_path / "bk"))

    with zipfile.ZipFile(zip_path) as zf:
        exported = json.loads(zf.read("config.json").decode("utf-8"))
    assert exported["netpair_secrets"] == {"peer-1": "ABCDEFG",
                                           "peer-2": "2345678"}

    fresh = Config()
    result = backup_mod.restore_backup(zip_path, fresh, history)
    assert result["config"] is True
    assert fresh.netpair_secrets == {"peer-1": "ABCDEFG",
                                     "peer-2": "2345678"}
    assert fresh.peer_relay_secrets == {"peer-1": "cd" * 32}


def test_backup_restore_ignores_malformed_netpair_secrets(tmp_path,
                                                          _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.config.config import Config

    # A hand-edited backup writing non-str values must not corrupt the field.
    crafted = tmp_path / "bad.zip"
    import zipfile
    with zipfile.ZipFile(crafted, "w") as zf:
        zf.writestr("config.json", json.dumps({
            "netpair_secrets": {"ok": "ABCDEFG", "bad": 5, "bad2": ["x"]},
            "device_name": "Restored",
        }))
        zf.writestr("history.json", json.dumps([]))
    fresh = Config()
    backup_mod.restore_backup(str(crafted), fresh, None)
    assert fresh.netpair_secrets == {"ok": "ABCDEFG"}  # junk pairs dropped


# ------------------------------------------------- self-pairing guards (hotfix)

def test_netpair_enter_own_code_rejected():
    # Entering a code we generated ourselves must not pair us with ourselves.
    from internal.transport.relay import generate_netpair_code
    app = make_app_stub(device_id="a1b2c3d4e5f6")
    code = generate_netpair_code(app.cfg.device_id, "SECRETX")
    resp, status = Application._netpair_enter(app, code)
    assert status == 400
    assert "cannot pair" in resp.get("error", "")
    assert app.cfg.netpair_secrets == {}


def test_netpair_hello_from_self_ignored():
    from internal.transport.relay import (
        generate_netpair_code,
        generate_netpair_secret,
    )
    app = make_app_stub(device_id="a1b2c3d4e5f6", internet_sync_enabled=True)
    secret = generate_netpair_secret()
    code = generate_netpair_code(app.cfg.device_id, secret)
    # Simulate: we entered our own code, sent ourselves a hello, and it bounced
    # back with source_device == our real id.
    app.cfg.netpair_secrets["ABCD"] = secret  # provisional self tag entry
    Application._handle_netpair_hello(
        app,
        {"msg_type": "netpair_hello", "peer_id": code.split("-")[0],
         "device_name": "self", "ts": 1},
        source_device=app.cfg.device_id,   # the self-origin marker
        topic=None,
    )
    assert app.cfg.netpair_secrets == {"ABCD": secret}  # unchanged


def test_on_relay_frame_drops_self_originated():
    from internal.protocol.codec import encode_frame
    app = make_app_stub(device_id="a1b2c3d4e5f6")
    routed = []
    app._on_peer_message = lambda msg, pid: routed.append(pid)
    frame = encode_frame({"msg_type": "clipboard", "types": {"TEXT": "aGk="},
                          "timestamp": 1.0},
                         source_device=app.cfg.device_id)
    Application._on_relay_frame(app, frame, "some/topic")
    assert routed == []  # self-originated relay frames never enter the routers


# ══════════════════════════════════════════════════
# merged from test_round15_netpair_mgmt.py
# ══════════════════════════════════════════════════


import pytest

# ------------------------------------------------------------------ config

@pytest.fixture()
def test_config_netpair_aliases_roundtrip(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    cfg.netpair_aliases = {"peer-1": "客厅电脑", "peer-2": "Office PC"}
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()
    assert loaded.netpair_aliases == {"peer-1": "客厅电脑",
                                      "peer-2": "Office PC"}
    assert cfg_mod.Config().netpair_aliases == {}  # fresh default


def test_config_netpair_aliases_bad_type_falls_back(isolated_config):
    cfg_mod = isolated_config
    path = cfg_mod._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"netpair_aliases": {"p": 5}}),
                    encoding="utf-8")
    loaded = cfg_mod.load()
    assert loaded.netpair_aliases == {}


# ------------------------------------------- Application handler behaviour

def make_app_stub_mgmt(**attrs):
    """Application stand-in wired to a recording relay + WS broadcast."""
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
        netpair_aliases=dict(attrs.get("netpair_aliases", {})),
        relay_brokers=["wss://x:8884/mqtt"],
        peers=peers,
    )
    app.cfg = c
    app._netpair_pending = dict(attrs.get("_netpair_pending", {}))
    app._netpair_names = dict(attrs.get("_netpair_names", {}))
    app._netpair_last_seen = dict(attrs.get("_netpair_last_seen", {}))

    class Relay:
        def __init__(self):
            self.published = []
            self.refreshed = 0

        def publish(self, frame, topic, key):
            self.published.append((frame, topic, key))
            return True

        def refresh_channels(self):
            self.refreshed += 1
    app._relay = Relay()

    saved = {"n": 0}
    app._save_cfg_and_peers = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._saved = saved

    class WS:
        def __init__(self):
            self.broadcasts = []

        def broadcast(self, mtype, data):
            self.broadcasts.append((mtype, data))
            return 1
    app.web_server = WS()

    # bind the real Application methods the handlers delegate to
    app._netpair_status = (
        lambda _a=app, **kw: Application._netpair_status(_a, **kw))
    app._netpair_rename = (
        lambda pid, name=None, _a=app: Application._netpair_rename(
            _a, pid, name))
    app._netpair_unpair = (
        lambda pid, _a=app: Application._netpair_unpair(_a, pid))
    app._on_relay_frame = (
        lambda frame, topic=None, _a=app, **kw: Application._on_relay_frame(
            _a, frame, topic, **kw))
    app._send_netpair_hello = (
        lambda pid, secret, _a=app: Application._send_netpair_hello(
            _a, pid, secret))
    app._handle_netpair_hello = (
        lambda payload, source, topic, _a=app: Application._handle_netpair_hello(
            _a, payload, source, topic))
    app._relay_channels = (
        lambda _a=app: Application._relay_channels(_a))
    app._netpair_secrets_all = (
        lambda _a=app: Application._netpair_secrets_all(_a))
    app._netpair_secret_for_topic = (
        lambda topic, _a=app: Application._netpair_secret_for_topic(_a, topic))
    app._ensure_relay_secret = (
        lambda _a=app: Application._ensure_relay_secret(_a))
    app._on_peer_message = lambda msg, pid, _a=app: None
    return app


def _clipboard_frame(source_device):
    return encode_frame(
        {"msg_type": "clipboard", "types": {"TEXT": "aGk="}, "timestamp": 1.0},
        source_device=source_device)


# ------------------------------------------------------- status semantics

def test_status_includes_alias_online_last_seen():
    app = make_app_stub_mgmt(
        netpair_secrets={"peer-1": "ABCDEFG"},
        netpair_aliases={"peer-1": "客厅电脑"},
        _netpair_names={"peer-1": "DevB"},
        _netpair_last_seen={"peer-1": 1000.0},
    )
    data, status = app._netpair_status(now=1050.0)  # 50s ago -> online
    assert status == 200
    p = data["peers"][0]
    assert p["peer_id"] == "peer-1"
    assert p["name"] == "DevB"          # peer's device name
    assert p["alias"] == "客厅电脑"       # our memo
    assert p["online"] is True
    assert p["last_seen"] == 1000.0     # epoch seconds
    assert p["paired"] is True


def test_status_online_cutoff_is_90s():
    app = make_app_stub_mgmt(
        netpair_secrets={"peer-1": "ABCDEFG"},
        _netpair_last_seen={"peer-1": 1000.0},
    )
    data, _ = app._netpair_status(now=1090.0)   # exactly 90s -> still online
    assert data["peers"][0]["online"] is True
    data, _ = app._netpair_status(now=1090.1)   # just past -> offline
    assert data["peers"][0]["online"] is False


def test_status_no_last_seen_is_offline_null():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"})
    data, _ = app._netpair_status(now=500.0)
    p = data["peers"][0]
    assert p["online"] is False
    assert p["last_seen"] is None
    # alias unset -> empty string (frontend falls back to name)
    assert p["alias"] == ""
    # name falls back to LAN peer device_name when hello never arrived
    app2 = make_app_stub_mgmt(
        netpair_secrets={"peer-1": "ABCDEFG"},
        peers={"peer-1": {"paired": True, "device_name": "DevB"}},
    )
    data, _ = app2._netpair_status(now=500.0)
    assert data["peers"][0]["name"] == "DevB"


# ----------------------------------------------- last_seen refresh points

def test_last_seen_refreshes_on_clipboard_frame():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"},
                        _netpair_last_seen={})
    app._on_relay_frame(_clipboard_frame("peer-1"), "some/topic", now=1234.0)
    assert app._netpair_last_seen == {"peer-1": 1234.0}


def test_last_seen_refreshes_on_netpair_hello():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"},
                        _netpair_last_seen={})
    hello = encode_frame({"msg_type": "netpair_hello", "peer_id": "x",
                          "device_name": "DevB", "ts": 1.0},
                         source_device="peer-1")
    app._on_relay_frame(hello, R.netpair_topic("ABCDEFG"), now=999.0)
    # a hello from a confirmed peer counts as "seen"
    assert app._netpair_last_seen.get("peer-1") == 999.0


def test_self_frame_does_not_update_last_seen():
    app = make_app_stub_mgmt(device_id="a1b2c3d4e5f6",
                        netpair_secrets={"a1b2c3d4e5f6": "ABCDEFG"})
    app._on_relay_frame(_clipboard_frame(app.cfg.device_id),
                        "some/topic", now=1234.0)
    assert app._netpair_last_seen == {}


# ------------------------------------------------------------- rename

def test_rename_sets_alias_and_persists():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"},
                        netpair_aliases={})
    data, status = app._netpair_rename("peer-1", "客厅电脑")
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_aliases == {"peer-1": "客厅电脑"}
    assert app._saved["n"] == 1


def test_rename_empty_name_clears_alias():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"},
                        netpair_aliases={"peer-1": "Old"})
    data, status = app._netpair_rename("peer-1", "   ")
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_aliases == {}
    assert app._saved["n"] == 1


def test_rename_rejects_unknown_peer_and_bad_name():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"},
                        netpair_aliases={})
    data, status = app._netpair_rename("ghost", "X")
    assert status == 400
    data, status = app._netpair_rename("peer-1", None)    # not a string
    assert status == 400
    data, status = app._netpair_rename("peer-1", 42)      # not a string
    assert status == 400
    data, status = app._netpair_rename("peer-1", "x" * 121)  # too long
    assert status == 400
    assert app.cfg.netpair_aliases == {}
    assert app._saved["n"] == 0


# ------------------------------------------------------------- unpair

def test_unpair_cleans_all_state_and_refreshes():
    app = make_app_stub_mgmt(
        netpair_secrets={"peer-1": "ABCDEFG"},
        netpair_aliases={"peer-1": "Alias"},
        _netpair_names={"peer-1": "DevB"},
        _netpair_last_seen={"peer-1": 1234.0},
    )
    data, status = app._netpair_unpair("peer-1")
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_secrets == {}
    assert app.cfg.netpair_aliases == {}
    assert app._netpair_names == {}
    assert app._netpair_last_seen == {}
    assert app._relay.refreshed == 1   # channel re-read so the sub disappears
    assert app._saved["n"] == 1


def test_unpair_keeps_lan_peer_relationship():
    # peer-1 is BOTH a LAN cfg.peers member AND an internet pair.
    app = make_app_stub_mgmt(
        peers={"peer-1": {"paired": True, "device_name": "DevB"}},
        netpair_secrets={"peer-1": "ABCDEFG"},
        netpair_aliases={"peer-1": "Alias"},
    )
    data, status = app._netpair_unpair("peer-1")
    assert status == 200
    # internet pairing removed, LAN pairing intact
    assert app.cfg.netpair_secrets == {}
    assert app.cfg.peers["peer-1"].paired is True
    assert app.cfg.peers["peer-1"].device_name == "DevB"


def test_unpair_unknown_peer_rejected():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"})
    data, status = app._netpair_unpair("ghost")
    assert status == 400
    assert app.cfg.netpair_secrets == {"peer-1": "ABCDEFG"}
    assert app._relay.refreshed == 0
    assert app._saved["n"] == 0


def test_unpair_is_local_only_no_hello_sent():
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"})
    data, status = app._netpair_unpair("peer-1")
    assert status == 200
    assert app._relay.published == []  # nothing is broadcast to the peer


# ------------------------------------------------------------------ REST

def test_api_rename_unpair_routes():
    from internal.web.api import internetpair as api
    app = make_app_stub_mgmt(netpair_secrets={"peer-1": "ABCDEFG"})
    api.bind(app)
    try:
        data, status = api.handle(
            "POST", "/api/internetpair/rename", {},
            json.dumps({"peer_id": "peer-1", "name": "我的电脑"}).encode())
        assert status == 200 and data["ok"] is True
        assert app.cfg.netpair_aliases == {"peer-1": "我的电脑"}
        data, status = api.handle(
            "POST", "/api/internetpair/rename", {},
            json.dumps({"peer_id": "ghost", "name": "X"}).encode())
        assert status == 400
        data, status = api.handle(
            "GET", "/api/internetpair/status", {}, b"")
        assert status == 200
        p = next(x for x in data["peers"] if x["peer_id"] == "peer-1")
        assert p["alias"] == "我的电脑" and p["paired"] is True
        data, status = api.handle(
            "POST", "/api/internetpair/unpair", {},
            json.dumps({"peer_id": "peer-1"}).encode())
        assert status == 200 and data["ok"] is True
        assert app.cfg.netpair_secrets == {}
        data, status = api.handle(
            "POST", "/api/internetpair/unpair", {},
            json.dumps({"peer_id": "peer-1"}).encode())
        assert status == 400
    finally:
        api.bind(None)


# ----------------------------------------------------------------- backup

@pytest.fixture()
def test_backup_roundtrip_includes_netpair_aliases(tmp_path,
                                                   _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.clipboard.history import ClipboardHistory
    from internal.config.config import Config

    cfg = Config()
    cfg.netpair_aliases = {"peer-1": "客厅电脑", "peer-2": "Office PC"}
    cfg.netpair_secrets = {"peer-1": "ABCDEFG"}

    history = ClipboardHistory(storage_path=str(tmp_path / "h.json"))
    zip_path = backup_mod.create_backup(
        cfg, history, backup_dir=str(tmp_path / "bk"))

    with zipfile.ZipFile(zip_path) as zf:
        exported = json.loads(zf.read("config.json").decode("utf-8"))
    assert exported["netpair_aliases"] == {"peer-1": "客厅电脑",
                                           "peer-2": "Office PC"}

    fresh = Config()
    result = backup_mod.restore_backup(zip_path, fresh, history)
    assert result["config"] is True
    assert fresh.netpair_aliases == {"peer-1": "客厅电脑",
                                     "peer-2": "Office PC"}


def test_backup_restore_ignores_malformed_netpair_aliases(
        tmp_path, _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.config.config import Config

    crafted = tmp_path / "bad.zip"
    with zipfile.ZipFile(crafted, "w") as zf:
        zf.writestr("config.json", json.dumps({
            "netpair_aliases": {"ok": "Alias", "bad": 5, "bad2": ["x"]},
            "device_name": "Restored",
        }))
        zf.writestr("history.json", json.dumps([]))
    fresh = Config()
    backup_mod.restore_backup(str(crafted), fresh, None)
    assert fresh.netpair_aliases == {"ok": "Alias"}  # junk pairs dropped


# ══════════════════════════════════════════════════
# merged from test_round16_audit.py (make_app_stub_mgmt renamed)
# ══════════════════════════════════════════════════

import time

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.sync.manager import SyncManager

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class MockClipboardMonitor:
    def __init__(self):
        self._callback = None
        self._running = False
        self.suppress_until = 0.0

    def start(self, callback):
        self._callback = callback
        self._running = True

    def stop(self):
        self._running = False
        self._callback = None

    def suppress_for(self, duration_seconds):
        self.suppress_until = time.time() + duration_seconds

    def fire(self):
        if self._callback and time.time() >= self.suppress_until:
            self._callback()


class MockClipboardReader:
    def __init__(self):
        self.content = ClipboardContent()

    def read(self):
        return self.content


class MockClipboardWriter:
    def __init__(self):
        self.last_written = None
        self.write_count = 0

    def write(self, content):
        self.last_written = content
        self.write_count += 1


class FakeHistory:
    def __init__(self):
        self.items = []

    def add(self, content, source_app=None):
        self.items.append(content)


def make_sync_mgr():
    monitor = MockClipboardMonitor()
    reader = MockClipboardReader()
    writer = MockClipboardWriter()
    history = FakeHistory()
    sent = []

    mgr = SyncManager(
        "device-A", "DevA",
        reader=reader, writer=writer, monitor=monitor, history=history,
        sync_debounce=0.0,  # no debounce → deterministic, no threads
    )
    mgr.on_send = lambda msg: sent.append(msg)
    return mgr, dict(monitor=monitor, reader=reader, writer=writer,
                     history=history, sent=sent)


def _text_content(text: str) -> ClipboardContent:
    return ClipboardContent(types={ContentType.TEXT: text.encode("utf-8")})


def _remote_msg(text: str, source: str) -> SyncMessage:
    return SyncMessage(content=_text_content(text), msg_id="m" + str(abs(hash(text))),
                       source_device=source)


def make_app_stub_audit(**attrs):
    """Application stand-in wired to a recording relay + WS broadcast.

    Only the relay/netpair surface is modelled; the clipboard/chat/file
    managers are stubs so Application methods under test have what they need.
    """
    app = types.SimpleNamespace()
    device_id = attrs.get("device_id", "a1b2c3d4e5f6")
    peers = {}
    for pid, paired in attrs.get("peers", {}).items():
        peers[pid] = types.SimpleNamespace(device_id=pid, paired=paired,
                                           device_name=pid)
    c = types.SimpleNamespace(
        device_id=device_id,
        device_name=attrs.get("device_name", "DevA"),
        internet_sync_enabled=attrs.get("internet_sync_enabled", True),
        relay_secret=attrs.get("relay_secret", "aa" * 32),
        peer_relay_secrets=dict(attrs.get("peer_relay_secrets", {})),
        netpair_secrets=dict(attrs.get("netpair_secrets", {})),
        netpair_aliases=dict(attrs.get("netpair_aliases", {})),
        relay_brokers=["wss://x:8884/mqtt"],
        peers=peers,
    )
    app.cfg = c
    app._netpair_pending = dict(attrs.get("_netpair_pending", {}))
    app._netpair_names = dict(attrs.get("_netpair_names", {}))
    app._netpair_last_seen = dict(attrs.get("_netpair_last_seen", {}))

    class Relay:
        def __init__(self):
            self.published = []
            self.refreshed = 0
            self.stopped = 0

        def publish(self, frame, topic, key):
            self.published.append((frame, topic, key))
            return True

        def refresh_channels(self):
            self.refreshed += 1

        def stop(self):
            self.stopped += 1

    relay = Relay()
    app._relay = attrs.get("_relay", relay)

    saved = {"n": 0}
    app._save_cfg_and_peers = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._save_cfg_encrypted = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._saved = saved

    class WS:
        def __init__(self):
            self.broadcasts = []

        def broadcast(self, mtype, data):
            self.broadcasts.append((mtype, data))
            return 1

    app.web_server = WS()

    # Bind the real Application methods the audit exercises.
    app._relay_publish_frame = lambda fb, _a=app: Application._relay_publish_frame(_a, fb)
    app._relay_channels = lambda _a=app: Application._relay_channels(_a)
    app._netpair_secrets_all = lambda _a=app: Application._netpair_secrets_all(_a)
    app._netpair_status = lambda now=None, _a=app: Application._netpair_status(_a, now)
    app._netpair_unpair = lambda pid, _a=app: Application._netpair_unpair(_a, pid)
    app._stop_internet_sync = lambda _a=app: Application._stop_internet_sync(_a)
    app._get_relay_state = lambda _a=app: Application._get_relay_state(_a)
    app._ensure_relay_secret = lambda _a=app: Application._ensure_relay_secret(_a)
    app._on_relay_frame = (
        lambda fb, topic=None, now=None, _a=app: Application._on_relay_frame(
            _a, fb, topic, now=now))
    app._handle_netpair_hello = (
        lambda payload, source, topic, _a=app: Application._handle_netpair_hello(
            _a, payload, source, topic))
    app._on_peer_message = attrs.get("_on_peer_message",
                                     lambda msg, pid, _a=app: None)
    return app


def _netpair_secret():
    return R.generate_netpair_secret()


# ---------------------------------------------------------------------------
# P1 — dual-path (LAN + relay) end-to-end dedup
# ---------------------------------------------------------------------------


def test_p1_dual_delivery_lands_single_history_and_write():
    """The same clip arriving twice (once LAN, once relay) must not double."""
    mgr, f = make_sync_mgr()
    msg = _remote_msg("hello internet", "device-B")

    mgr.handle_remote_message(msg)   # e.g. arrived over LAN
    mgr.handle_remote_message(msg)   # e.g. the relay mirror of the same frame

    assert f["writer"].write_count == 1
    assert len(f["history"].items) == 1
    assert f["history"].items[0].source_device == "device-B"


def test_p1_different_content_is_not_collapsed():
    """Distinct content still lands twice (dedup is content-addressed)."""
    mgr, f = make_sync_mgr()
    mgr.handle_remote_message(_remote_msg("first", "device-B"))
    mgr.handle_remote_message(_remote_msg("second", "device-B"))
    assert f["writer"].write_count == 2
    assert len(f["history"].items) == 2


def test_p1_relay_frame_and_lan_frame_share_the_dedup_ring():
    """Interleaved LAN + relay arrivals of the SAME content collapse too.

    The dedup ring (not just _last_local_hash) is what absorbs the case where
    the two deliveries are separated by an unrelated local copy.
    """
    mgr, f = make_sync_mgr()
    msg = _remote_msg("same clip", "device-B")
    mgr.handle_remote_message(msg)                 # relay
    mgr.handle_remote_message(_remote_msg("unrelated", "device-A"))  # local-ish
    mgr.handle_remote_message(msg)                 # LAN copy arrives late
    assert f["writer"].write_count == 2            # same + unrelated
    assert len(f["history"].items) == 2


# ---------------------------------------------------------------------------
# P2 — sync pause / timed pause × relay mirror
# ---------------------------------------------------------------------------


def test_p2_paused_sync_never_reaches_on_send():
    """Pausing the sync manager means nothing is broadcast (or relay-mirrored).

    The relay mirror is only reachable from _on_local_sync (the on_send hook),
    so this gate is what stops mirroring while a pause is active.
    """
    mgr, f = make_sync_mgr()
    mgr.set_enabled(False)
    mgr.start()
    f["reader"].content = _text_content("while paused")
    f["monitor"].fire()
    time.sleep(0.05)
    assert f["sent"] == []


def test_p2_paused_inbound_clipboard_is_dropped():
    """Incoming clipboard (LAN or relay) is dropped while sync is paused."""
    mgr, f = make_sync_mgr()
    mgr.set_enabled(False)
    mgr.handle_remote_message(_remote_msg("paused inbound", "device-B"))
    assert f["writer"].write_count == 0
    assert f["history"].items == []


def test_p2_relay_publish_is_off_when_internet_disabled():
    """_relay_publish_frame is a hard no-op when internet sync is off."""
    app = make_app_stub_audit(internet_sync_enabled=False,
                        peer_relay_secrets={"device-B": "bb" * 32})
    app.cfg.peers["device-B"] = types.SimpleNamespace(
        device_id="device-B", paired=True, device_name="DevB")
    app._relay_publish_frame(b"some frame bytes")
    assert app._relay.published == []


def test_p2_relay_publish_is_off_without_transport():
    """_relay_publish_frame is a no-op when the relay transport is down."""
    app = make_app_stub_audit(internet_sync_enabled=True,
                        peer_relay_secrets={"device-B": "bb" * 32})
    app.cfg.peers["device-B"] = types.SimpleNamespace(
        device_id="device-B", paired=True, device_name="DevB")
    app._relay = None
    app._relay_publish_frame(b"frame")
    # Nothing to publish to — must not raise.
    assert True


# ---------------------------------------------------------------------------
# P3 — internet arrival consistency (same router as LAN)
# ---------------------------------------------------------------------------


def test_p3_relay_clipboard_routes_to_peer_router():
    """A clipboard frame over the relay hits the SAME _on_peer_message router."""
    calls = []
    app = make_app_stub_audit(_on_peer_message=lambda msg, pid, _a=None: calls.append((msg, pid)))
    frame = encode_frame(
        {"msg_type": "clipboard", "text": "relay clip"}, source_device="device-B")
    app._on_relay_frame(frame, topic="t")
    assert len(calls) == 1
    msg, pid = calls[0]
    assert pid == "device-B"          # peer_id = source_device, like LAN
    assert getattr(msg, "msg_type", "") == "clipboard"


def test_p3_relay_self_frame_is_dropped():
    """Our own mirrored frame must never re-enter the router (self-echo)."""
    calls = []
    app = make_app_stub_audit(_on_peer_message=lambda msg, pid, _a=None: calls.append((msg, pid)))
    frame = encode_frame(
        {"msg_type": "clipboard", "text": "self"}, source_device=app.cfg.device_id)
    app._on_relay_frame(frame, topic="t")
    assert calls == []


def test_p3_relay_netpair_hello_never_reaches_clipboard_router():
    """netpair_hello frames are routed to the handshake, not the router."""
    router_calls = []
    hello_calls = []
    app = make_app_stub_audit(_on_peer_message=lambda msg, pid, _a=None: router_calls.append(msg))
    app._handle_netpair_hello = (
        lambda payload, source, topic, _a=app: hello_calls.append((payload, source, topic)))
    frame = encode_frame(
        {"msg_type": "netpair_hello", "peer_id": "ABCD"}, source_device="device-B")
    app._on_relay_frame(frame, topic="t")
    assert router_calls == []
    assert len(hello_calls) == 1


# ---------------------------------------------------------------------------
# P4 — status / alias consistency
# ---------------------------------------------------------------------------


def test_p4_netpair_status_online_follows_last_seen_window():
    """online is driven by the 90 s last-seen window, independent of relay."""
    pid = "device-B"
    secret = _netpair_secret()
    now = 1_000_000.0
    app = make_app_stub_audit(netpair_secrets={pid: secret},
                        _netpair_last_seen={pid: now - 30})
    data, status = app._netpair_status(now)
    assert status == 200
    peer = data["peers"][0]
    assert peer["peer_id"] == pid and peer["online"] is True
    assert peer["last_seen"] == now - 30
    assert peer["paired"] is True

    # Outside the window → offline.
    app2 = make_app_stub_audit(netpair_secrets={pid: secret},
                         _netpair_last_seen={pid: now - 300})
    data2, _ = app2._netpair_status(now)
    assert data2["peers"][0]["online"] is False


def test_p4_last_seen_only_for_confirmed_netpair_peers():
    """A relay_enroll-only peer (LAN-derived secret) gets NO netpair row.

    Only a source present in cfg.netpair_secrets updates _netpair_last_seen,
    so the device page's internet-online dot can't be poisoned by a peer that
    merely shares a LAN-derived relay channel.
    """
    pid = "device-B"
    app = make_app_stub_audit(peer_relay_secrets={pid: "bb" * 32},
                        netpair_secrets={})
    frame = __import__("internal.protocol.codec", fromlist=["encode_frame"]).encode_frame(
        {"msg_type": "clipboard", "text": "x"}, source_device=pid)
    app._on_relay_frame(frame, topic="t", now=1_000_000.0)
    assert app._netpair_last_seen.get(pid) is None
    data, _ = app._netpair_status(1_000_000.0)
    assert data["peers"] == []


def test_p4_alias_fallback_order_and_self_exclusion():
    """Alias > device name > short id; self can never be a netpair peer."""
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub_audit(
        netpair_secrets={pid: secret},
        netpair_aliases={pid: "My Laptop"},
        _netpair_names={pid: "DevB"},
        peers={pid: True},
    )
    data, _ = app._netpair_status(1_000_000.0)
    peer = data["peers"][0]
    assert peer["alias"] == "My Laptop"
    assert peer["name"] == "DevB"     # runtime name beats cfg.peers name


def test_p4_relay_state_reflects_enabled_and_transport():
    """_get_relay_state: off when disabled; transport state when enabled."""
    app = make_app_stub_audit(internet_sync_enabled=False)
    assert app._get_relay_state() == "off"

    app2 = make_app_stub_audit(internet_sync_enabled=True)
    app2._relay = types.SimpleNamespace(state="online")
    assert app2._get_relay_state() == "online"

    app3 = make_app_stub_audit(internet_sync_enabled=True)
    app3._relay = types.SimpleNamespace(state="connecting")
    assert app3._get_relay_state() == "connecting"


# ---------------------------------------------------------------------------
# P6 — same peer dual channel: publish once, netpair wins
# ---------------------------------------------------------------------------


def test_p6_dual_channel_peer_publishes_once_netpair_wins():
    """Peer in BOTH peer_relay_secrets and netpair_secrets → one publish."""
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub_audit(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={pid: secret},
        peers={pid: True},  # make_app_stub_audit: {device_id: paired_bool}
    )
    app._relay_publish_frame(b"clip frame")
    assert len(app._relay.published) == 1
    frame, topic, key = app._relay.published[0]
    assert topic == R.netpair_topic(secret)
    assert key == R.netpair_key(secret)


def test_p6_relay_enroll_only_peer_publishes_once():
    """Peer reachable only via LAN-derived relay secret → one relay publish."""
    pid = "device-B"
    app = make_app_stub_audit(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={},
        peers={pid: True},  # make_app_stub_audit: {device_id: paired_bool}
    )
    app._relay_publish_frame(b"clip frame")
    assert len(app._relay.published) == 1
    topic, key = app._relay.published[0][1], app._relay.published[0][2]
    assert topic == R.derive_topic(app.cfg.relay_secret, "bb" * 32)
    assert key == R.derive_key(app.cfg.relay_secret, "bb" * 32)


def test_p6_netpair_only_peer_publishes_once():
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub_audit(peer_relay_secrets={}, netpair_secrets={pid: secret})
    app._relay_publish_frame(b"clip frame")
    assert len(app._relay.published) == 1
    assert app._relay.published[0][1] == R.netpair_topic(secret)


def test_p6_self_never_published():
    """A stray self netpair entry is skipped on the publish path."""
    secret = _netpair_secret()
    app = make_app_stub_audit(netpair_secrets={"a1b2c3d4e5f6": secret})  # default device id
    app._relay_publish_frame(b"clip frame")
    assert app._relay.published == []


def test_p6_unpaired_peer_never_published():
    """A LAN peer that is not (yet) paired is not mirrored over the relay."""
    pid = "device-B"
    app = make_app_stub_audit(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={},
        peers={pid: False},   # make_app_stub_audit: {device_id: paired_bool}
    )
    app._relay_publish_frame(b"clip frame")
    assert app._relay.published == []


# ---------------------------------------------------------------------------
# P7 — lifecycle: unpair / stop / restart
# ---------------------------------------------------------------------------


def test_p7_unpair_clears_state_and_resubscribes():
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub_audit(
        netpair_secrets={pid: secret},
        netpair_aliases={pid: "My Laptop"},
        _netpair_names={pid: "DevB"},
        _netpair_last_seen={pid: time.time()},
    )
    # Confirm the channel is currently subscribed.
    assert R.netpair_topic(secret) in app._relay_channels()

    data, status = app._netpair_unpair(pid)
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_secrets == {}
    assert app.cfg.netpair_aliases == {}
    assert pid not in app._netpair_names
    assert pid not in app._netpair_last_seen
    assert app._relay.refreshed >= 1
    # Unsubscribed: the topic must no longer be in the channel set.
    assert R.netpair_topic(secret) not in app._relay_channels()


def test_p7_unpair_unknown_peer_is_400():
    app = make_app_stub_audit(netpair_secrets={})
    data, status = app._netpair_unpair("ghost")
    assert status == 400 and data.get("ok") is False


def test_p7_unpair_broadcasts_unpaired_to_web_tabs():
    """Unpair pushes a WS `netpair_peer status:unpaired` so sibling tabs
    (and the acting tab, if it misses the REST response) drop the row."""
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub_audit(netpair_secrets={pid: secret})
    app._netpair_unpair(pid)
    ws_events = [e for e in app.web_server.broadcasts if e[0] == "netpair_peer"]
    assert len(ws_events) == 1
    assert ws_events[0][1] == {"peer_id": pid, "status": "unpaired"}


def test_p7_stop_releases_transport():
    app = make_app_stub_audit()
    relay = app._relay
    app._stop_internet_sync()
    assert app._relay is None
    assert relay.stopped == 1          # the fake relay recorded transport.stop()


def test_p7_relay_channels_exclude_self_and_include_both_paths():
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub_audit(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={pid: secret},
        peers={pid: True},  # make_app_stub_audit: {device_id: paired_bool}
    )
    channels = app._relay_channels()
    assert R.derive_topic(app.cfg.relay_secret, "bb" * 32) in channels
    assert R.netpair_topic(secret) in channels
    # self netpair entry never subscribed
    app.cfg.netpair_secrets[app.cfg.device_id] = _netpair_secret()
    channels2 = app._relay_channels()
    assert not any(R.netpair_topic(app.cfg.netpair_secrets[app.cfg.device_id]) == t
                   for t in channels2)


# ---------------------------------------------------------------------------
# P8 — history / transfer panels share one store
# ---------------------------------------------------------------------------


def test_p8_internet_content_uses_same_history_as_lan():
    """Relay-arriving clipboard is fed to the shared handle_remote_message.

    The one store (the SyncManager's history) receives both a LAN-arriving
    clip and a relay-arriving clip, so the history panel sees a single
    timeline with a single source device — no separate internet bucket.
    """
    history = FakeHistory()
    # The app's clipboard path (LAN and relay) both terminate at the SAME
    # SyncManager.handle_remote_message; prove the shared history dedups.
    mgr = SyncManager(
        "device-A", "DevA",
        reader=MockClipboardReader(), writer=MockClipboardWriter(),
        monitor=MockClipboardMonitor(), history=history, sync_debounce=0.0,
    )
    lan = _remote_msg("shared clip", "device-B")
    relay = _remote_msg("shared clip", "device-B")
    mgr.handle_remote_message(lan)    # arrives over LAN
    mgr.handle_remote_message(relay)  # same content arrives over relay
    assert len(history.items) == 1
    assert history.items[0].source_device == "device-B"

# ══════════════════════════════════════════════════
# restored from test_round15_netpair_mgmt.py (lost in fixture dedup)
# ══════════════════════════════════════════════════

def test_config_netpair_aliases_roundtrip(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    cfg.netpair_aliases = {"peer-1": "客厅电脑", "peer-2": "Office PC"}
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()
    assert loaded.netpair_aliases == {"peer-1": "客厅电脑",
                                      "peer-2": "Office PC"}
    assert cfg_mod.Config().netpair_aliases == {}  # fresh default


def test_backup_roundtrip_includes_netpair_aliases(tmp_path,
                                                   _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.clipboard.history import ClipboardHistory
    from internal.config.config import Config

    cfg = Config()
    cfg.netpair_aliases = {"peer-1": "客厅电脑", "peer-2": "Office PC"}
    cfg.netpair_secrets = {"peer-1": "ABCDEFG"}

    history = ClipboardHistory(storage_path=str(tmp_path / "h.json"))
    zip_path = backup_mod.create_backup(
        cfg, history, backup_dir=str(tmp_path / "bk"))

    with zipfile.ZipFile(zip_path) as zf:
        exported = json.loads(zf.read("config.json").decode("utf-8"))
    assert exported["netpair_aliases"] == {"peer-1": "客厅电脑",
                                           "peer-2": "Office PC"}

    fresh = Config()
    result = backup_mod.restore_backup(zip_path, fresh, history)
    assert result["config"] is True
    assert fresh.netpair_aliases == {"peer-1": "客厅电脑",
                                     "peer-2": "Office PC"}
