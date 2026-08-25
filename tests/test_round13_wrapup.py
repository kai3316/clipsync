"""Round 13 — wrap-up: settings whitelist + config new-key integrity.

Groups covered:
  1. Settings API whitelist: ``ai_config_paths`` present in BOTH
     ``_SAFE_FIELDS`` and ``_MUTABLE_FIELDS``, so GET /api/settings exposes
     it and POST /api/settings can edit the AI-config watch list.  The
     dedicated GET/POST /api/aiconfig/paths endpoints are left intact (dual
     entry is harmless — the panel keeps its dedicated editor).
  2. Config new keys (relay_brokers / peer_relay_secrets / ai_config_paths /
     internet_sync_enabled / relay_secret) survive config save→load AND a
     backup→restore cycle (the backup previously dropped them silently).
  3. Relay × AI-config frame isolation: message-type routing buckets are
     pairwise disjoint, a relay envelope round-trips a standard ClipSync
     frame whose msg_type still routes correctly, and aiconfig/relay frames
     stay gated out of unpaired connections.
  4. Tk classic pairing card: the pending-row unpacking tolerates both 4- and
     5-element tuples (SAS appended by main.py), and the SAS label/hint
     locale keys exist in the desktop i18n tables.
"""

import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════
# 1 — Settings API whitelist for ai_config_paths
# ══════════════════════════════════════════════════════════════════════


def test_settings_whitelist_includes_ai_config_paths():
    from internal.web.api import settings as settings_api
    assert "ai_config_paths" in settings_api._SAFE_FIELDS
    assert "ai_config_paths" in settings_api._MUTABLE_FIELDS


class _SettingsCfg:
    """Minimal config stand-in carrying the fields the handlers touch."""

    def __init__(self):
        self.private_key_pem = "KEY"
        self.internet_sync_enabled = False
        self.relay_brokers = []
        self.relay_secret = ""
        self.peer_relay_secrets = {}
        self.ai_config_paths = []


@pytest.fixture()
def sandboxed_persist(monkeypatch, tmp_path):
    """Redirect config persistence into the test sandbox (no real disk writes)."""
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
def test_ai_config_paths_writable_via_settings_post():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    paths = ["~/ai-configs", "~/.claude"]
    data, status = update_settings(_body({"ai_config_paths": paths}), cfg)
    assert status == 200
    assert data["updated"]["ai_config_paths"] == paths
    assert cfg.ai_config_paths == paths

    # A non-list value is rejected without touching the field.
    data, status = update_settings(_body({"ai_config_paths": "~/x"}), cfg)
    assert status == 400
    assert cfg.ai_config_paths == paths


def test_get_settings_exposes_ai_config_paths():
    from internal.web.api.settings import get_settings
    cfg = _SettingsCfg()
    cfg.ai_config_paths = ["~/ai-configs"]
    result, status = get_settings(cfg)
    assert status == 200
    assert result["settings"]["ai_config_paths"] == ["~/ai-configs"]


@pytest.mark.usefixtures("sandboxed_persist")
def test_dedicated_aiconfig_paths_endpoint_unchanged():
    """The dedicated /api/aiconfig/paths editor still functions (dual entry)."""
    from internal.web.api import aiconfig as aiconfig_api

    class _Mgr:
        def __init__(self):
            self._paths = ["~/orig"]

        def local_summary(self):
            return {"collected_at": 0, "entry_count": 0, "paths": list(self._paths)}

        def set_watch_list(self, paths):
            self._paths = list(paths)
            return {"ok": True, "paths": list(paths)}

        def on_watch_list_changed(self):
            return 0

    aiconfig_api.bind(_Mgr())
    try:
        data, status = aiconfig_api.handle(
            "GET", "/api/aiconfig/paths", {}, b"")
        assert status == 200
        assert data["paths"] == ["~/orig"]

        data, status = aiconfig_api.handle(
            "POST", "/api/aiconfig/paths", {},
            _body({"paths": ["~/new"]}))
        assert status == 200
        assert data["paths"] == ["~/new"]
    finally:
        aiconfig_api.bind(None)


# ══════════════════════════════════════════════════════════════════════
# 2 — Config new keys: save/load + backup→restore integrity
# ══════════════════════════════════════════════════════════════════════


def _point_config_at(tmp_path, monkeypatch, data=None):
    import internal.config.config as config_module
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(data or {}), encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: cfg_path)
    return config_module


def test_config_save_load_roundtrips_new_keys(tmp_path, monkeypatch):
    config_module = _point_config_at(tmp_path, monkeypatch, {})
    cfg = config_module.load()
    cfg.internet_sync_enabled = True
    cfg.relay_brokers = ["wss://broker.emqx.io:8884/mqtt"]
    cfg.relay_secret = "ab" * 32
    cfg.peer_relay_secrets = {"peer-1": "cd" * 32}
    cfg.ai_config_paths = ["~/ai-configs", "~/.claude"]
    config_module.save(cfg)

    cfg2 = config_module.load()
    assert cfg2.internet_sync_enabled is True
    assert cfg2.relay_brokers == cfg.relay_brokers
    assert cfg2.relay_secret == cfg.relay_secret
    assert cfg2.peer_relay_secrets == cfg.peer_relay_secrets
    assert cfg2.ai_config_paths == cfg.ai_config_paths


@pytest.fixture()
def _isolated_favorites(tmp_path, monkeypatch):
    """Point the favorites paths at tmp so tests never touch real user data."""
    import internal.data.backup as backup_mod
    monkeypatch.setattr(
        backup_mod, "_get_favorites_db_path",
        lambda: tmp_path / "favorites.db",
    )
    monkeypatch.setattr(
        backup_mod, "_get_favorites_path",
        lambda: tmp_path / "favorites.json",
    )
    return tmp_path


def test_backup_roundtrips_new_config_keys(tmp_path, _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.clipboard.history import ClipboardHistory
    from internal.config.config import Config

    cfg = Config()
    cfg.internet_sync_enabled = True
    cfg.relay_brokers = ["wss://broker.hivemq.com:8884/mqtt"]
    cfg.relay_secret = "ab" * 32
    cfg.peer_relay_secrets = {"peer-1": "cd" * 32}
    cfg.ai_config_paths = ["~/ai-configs"]

    history = ClipboardHistory(storage_path=str(tmp_path / "h.json"))
    zip_path = backup_mod.create_backup(
        cfg, history, backup_dir=str(tmp_path / "bk"))

    # The archive itself carries the keys.
    with zipfile.ZipFile(zip_path) as zf:
        exported = json.loads(zf.read("config.json").decode("utf-8"))
    assert exported["internet_sync_enabled"] is True
    assert exported["relay_brokers"] == cfg.relay_brokers
    assert exported["peer_relay_secrets"] == {"peer-1": "cd" * 32}
    assert exported["ai_config_paths"] == cfg.ai_config_paths

    fresh = Config()   # defaults everywhere
    result = backup_mod.restore_backup(zip_path, fresh, history)
    assert result["config"] is True
    assert fresh.internet_sync_enabled is True
    assert fresh.relay_brokers == cfg.relay_brokers
    assert fresh.relay_secret == cfg.relay_secret
    assert fresh.peer_relay_secrets == {"peer-1": "cd" * 32}
    assert fresh.ai_config_paths == cfg.ai_config_paths


def test_backup_strlist_nonnull_rule():
    """A hand-edited backup writing null must not set relay_brokers to None."""
    from internal.data.backup import _SKIP, _validate_config_value

    assert _validate_config_value(["a", "b"], ("strlist_nonnull",)) == ["a", "b"]
    assert _validate_config_value(None, ("strlist_nonnull",)) is _SKIP
    assert _validate_config_value("nope", ("strlist_nonnull",)) is _SKIP
    assert _validate_config_value([1], ("strlist_nonnull",)) is _SKIP


# ══════════════════════════════════════════════════════════════════════
# 3 — Relay × AI-config frame isolation
# ══════════════════════════════════════════════════════════════════════


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
    from internal.transport.relay import (
        RELAY_TS_WINDOW,
        derive_key,
        open_envelope,
        pack_envelope,
    )
    from internal.protocol.codec import encode_frame

    key = derive_key("secret-a", "secret-b")
    frame = encode_frame({"msg_type": "clipboard", "content": "x"},
                         source_device="dev-1")
    env = pack_envelope(frame, key, 1_700_000_000.0)

    # Envelope from outside the tolerated clock window is dropped.
    assert open_envelope(env, key, 1_700_000_000.0 + RELAY_TS_WINDOW * 2) is None
    # Wrong key cannot be opened.
    assert open_envelope(env, derive_key("secret-a", "secret-c"),
                         1_700_000_000.0) is None


# ══════════════════════════════════════════════════════════════════════
# 4 — Tk classic pairing card SAS wiring
# ══════════════════════════════════════════════════════════════════════


def test_tk_sas_locale_keys_exist_in_desktop_i18n():
    import internal.i18n as i18n
    for key in ("devices.sas_label", "devices.sas_verify_hint"):
        assert key in i18n._EN, f"missing from _EN: {key}"
        assert key in i18n._ZH, f"missing from _ZH: {key}"
        assert i18n._EN[key].strip(), key
        assert i18n._ZH[key].strip(), key
    # Wording must match the web UI's SAS card so both surfaces agree.
    assert "Security code" in i18n._EN["devices.sas_label"]
    assert "安全代码" in i18n._ZH["devices.sas_label"]
    assert "other device" in i18n._EN["devices.sas_verify_hint"]
    assert "对方设备" in i18n._ZH["devices.sas_verify_hint"]


def test_tk_pending_row_wires_sas_display():
    import inspect
    from internal.ui import dashboard as dash

    # _create_pending_row accepts the SAS as an optional 5th arg…
    sig = inspect.signature(dash.DashboardWindow._create_pending_row)
    params = list(sig.parameters)
    assert "sas" in params
    assert sig.parameters["sas"].default == ""

    # …and renders it (plus a verify hint) when present.
    src = inspect.getsource(dash.DashboardWindow._create_pending_row)
    assert "devices.sas_label" in src
    assert "devices.sas_verify_hint" in src

    # The pending-list unpacking tolerates 4- and 5-element tuples: the 5th
    # element is the SAS appended by main.py._get_pending; expired rows carry
    # only 4.  Guard must not index past a 4-tuple.
    refresh_src = inspect.getsource(dash.DashboardWindow._refresh_devices)
    assert "len(item) > 4" in refresh_src
    assert "item[4] if len(item) > 4 else" in refresh_src


def test_sas_code_shape_via_main_same_source():
    """main.py._pairing_sas uses fingerprint.sas_code — the same derivation
    the web device API surfaces — so the Tk card shows the identical string."""
    import inspect
    import src.main as main_mod
    src = inspect.getsource(main_mod.Application._pairing_sas)
    assert "sas_code" in src
    assert "get_peer_fingerprint" in src
    assert "get_identity().fingerprint" in src
