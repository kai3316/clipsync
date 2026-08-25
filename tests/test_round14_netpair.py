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
    assert codec.NETPAIR_MSG_TYPES == frozenset({"netpair_hello"})
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
