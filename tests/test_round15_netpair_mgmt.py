"""Round 15 — internet pairing management (device page): per-peer aliases,
online / last-seen status, rename + one-sided unpair.

Everything in Round 14 (generate/enter) is untouched; this layer adds the
device-management surface on top:

  * ``cfg.netpair_aliases`` — local, persisted per-peer friendly names.
  * ``GET /api/internetpair/status`` now returns {peer_id, name, alias,
    online, last_seen, paired} where online = a frame arrived within the last
    90s and last_seen is the epoch-seconds of that frame (or None).
  * ``POST /api/internetpair/rename`` {peer_id, name} — set/clear the alias.
  * ``POST /api/internetpair/unpair`` {peer_id} — one-sided break; removes the
    secret + alias + runtime state and unsubscribes the relay channel.

Handlers are exercised through the make_app_stub pattern (mirroring
tests/test_round14_netpair.py): no app/tkinter/transport stack boots here.
"""

import json
import types
import zipfile

import pytest

from internal.protocol.codec import decode_message, encode_frame
from internal.transport import relay as R

from src.main import Application  # noqa: E402


# ------------------------------------------------------------------ config

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from internal.config import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg_mod, "_config_path",
                        lambda: tmp_path / "config.json")
    yield cfg_mod


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

def make_app_stub(**attrs):
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
    app = make_app_stub(
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
    app = make_app_stub(
        netpair_secrets={"peer-1": "ABCDEFG"},
        _netpair_last_seen={"peer-1": 1000.0},
    )
    data, _ = app._netpair_status(now=1090.0)   # exactly 90s -> still online
    assert data["peers"][0]["online"] is True
    data, _ = app._netpair_status(now=1090.1)   # just past -> offline
    assert data["peers"][0]["online"] is False


def test_status_no_last_seen_is_offline_null():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"})
    data, _ = app._netpair_status(now=500.0)
    p = data["peers"][0]
    assert p["online"] is False
    assert p["last_seen"] is None
    # alias unset -> empty string (frontend falls back to name)
    assert p["alias"] == ""
    # name falls back to LAN peer device_name when hello never arrived
    app2 = make_app_stub(
        netpair_secrets={"peer-1": "ABCDEFG"},
        peers={"peer-1": {"paired": True, "device_name": "DevB"}},
    )
    data, _ = app2._netpair_status(now=500.0)
    assert data["peers"][0]["name"] == "DevB"


# ----------------------------------------------- last_seen refresh points

def test_last_seen_refreshes_on_clipboard_frame():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"},
                        _netpair_last_seen={})
    app._on_relay_frame(_clipboard_frame("peer-1"), "some/topic", now=1234.0)
    assert app._netpair_last_seen == {"peer-1": 1234.0}


def test_last_seen_refreshes_on_netpair_hello():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"},
                        _netpair_last_seen={})
    hello = encode_frame({"msg_type": "netpair_hello", "peer_id": "x",
                          "device_name": "DevB", "ts": 1.0},
                         source_device="peer-1")
    app._on_relay_frame(hello, R.netpair_topic("ABCDEFG"), now=999.0)
    # a hello from a confirmed peer counts as "seen"
    assert app._netpair_last_seen.get("peer-1") == 999.0


def test_self_frame_does_not_update_last_seen():
    app = make_app_stub(device_id="a1b2c3d4e5f6",
                        netpair_secrets={"a1b2c3d4e5f6": "ABCDEFG"})
    app._on_relay_frame(_clipboard_frame(app.cfg.device_id),
                        "some/topic", now=1234.0)
    assert app._netpair_last_seen == {}


# ------------------------------------------------------------- rename

def test_rename_sets_alias_and_persists():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"},
                        netpair_aliases={})
    data, status = app._netpair_rename("peer-1", "客厅电脑")
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_aliases == {"peer-1": "客厅电脑"}
    assert app._saved["n"] == 1


def test_rename_empty_name_clears_alias():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"},
                        netpair_aliases={"peer-1": "Old"})
    data, status = app._netpair_rename("peer-1", "   ")
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_aliases == {}
    assert app._saved["n"] == 1


def test_rename_rejects_unknown_peer_and_bad_name():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"},
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
    app = make_app_stub(
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
    app = make_app_stub(
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
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"})
    data, status = app._netpair_unpair("ghost")
    assert status == 400
    assert app.cfg.netpair_secrets == {"peer-1": "ABCDEFG"}
    assert app._relay.refreshed == 0
    assert app._saved["n"] == 0


def test_unpair_is_local_only_no_hello_sent():
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"})
    data, status = app._netpair_unpair("peer-1")
    assert status == 200
    assert app._relay.published == []  # nothing is broadcast to the peer


# ------------------------------------------------------------------ REST

def test_api_rename_unpair_routes():
    from internal.web.api import internetpair as api
    app = make_app_stub(netpair_secrets={"peer-1": "ABCDEFG"})
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
def _isolated_favorites(tmp_path, monkeypatch):
    import internal.data.backup as backup_mod
    monkeypatch.setattr(backup_mod, "_get_favorites_db_path",
                        lambda: tmp_path / "favorites.db")
    monkeypatch.setattr(backup_mod, "_get_favorites_path",
                        lambda: tmp_path / "favorites.json")
    return tmp_path


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
