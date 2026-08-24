"""Round 11 — web UI layer for internet (relay) sync + pairing SAS.

Covers:
  1. sas_code() — the Short Authentication String shown on both devices
     during pairing (determinism, symmetry, distinctness, format).
  2. The settings API whitelist carrying the new relay keys (round-trip +
     bad-type rejection) and exposing the live internet-sync state while
     never leaking the relay secrets.
  3. GET /api/devices pending pairings surfacing the pairing SAS.
  4. Static wiring assertions: ws.js/store.js handle the WS ``relay_state``
     event into store.relayState; the settings panel consumes it.
  5. Locale files: en / zh-CN key sets identical and containing every new key.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.fingerprint import normalize_fingerprint, sas_code

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


# ── 1. SAS derivation ──────────────────────────────────────────────────


def test_sas_code_is_deterministic():
    a = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
    b = "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00"
    assert sas_code(a, b) == sas_code(a, b)
    assert sas_code(a, b) == sas_code(a, b)  # stable across repeated calls


def test_sas_code_is_symmetric():
    # Both devices must derive the same string from local knowledge alone.
    assert sas_code("fp-alpha", "fp-beta") == sas_code("fp-beta", "fp-alpha")
    assert sas_code("", "xyz") == sas_code("xyz", "")


def test_sas_code_matches_reference_digest():
    # Pin the exact formula: sha256(sorted-normalized concat)[:8] as 4-4 hex.
    import hashlib
    a = normalize_fingerprint("ab:cd:ef")   # ABCDEF
    b = normalize_fingerprint("012345")     # 012345
    lo, hi = sorted([a, b])
    digest = hashlib.sha256((lo + hi).encode("ascii")).hexdigest()
    expected = digest[:8].upper()
    expected = f"{expected[:4]}-{expected[4:]}"
    assert sas_code("ab:cd:ef", "012345") == expected


def test_sas_code_format_and_normalization():
    code = sas_code("AAAA", "BBBB")
    assert len(code) == 9 and code[4] == "-"
    left, right = code.split("-")
    assert len(left) == 4 and len(right) == 4
    assert all(c in "0123456789ABCDEF" for c in left + right)
    # Colon-separated display form hashes identically to bare hex.
    assert sas_code("aa:bb", "cc:dd") == sas_code("AABB", "CCDD")
    assert sas_code("aabb", "ccdd") == sas_code("AA:BB", "ccdD")


def test_sas_code_differs_across_device_pairs():
    seen = {
        sas_code(f"device-{i}", f"peer-{j}")
        for i in range(8) for j in range(8)
    }
    # 64 distinct pairs must not collide on an 8-hex-char code.
    assert len(seen) == 64


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


# ── 3. Devices API surfaces the pairing SAS ────────────────────────────


def _minimal_cfg():
    class _Cfg:
        device_id = "self"
        device_name = "Self"
        peers = {}
    return _Cfg()


def test_devices_pending_pairings_tuple_with_sas():
    from internal.web.api.devices import get_devices
    pending = [("peer-abc", "12345678", "Phone", "pending", "3A2F-91C4")]
    res, status = get_devices(
        _minimal_cfg(), lambda: [], get_pending_pairings=lambda: pending)
    assert status == 200
    row = res["pending_pairings"][0]
    assert row["sas"] == "3A2F-91C4"
    assert row["code"] == "12345678"
    assert row["status"] == "pending"


def test_devices_pending_pairings_without_sas_defaults_empty():
    from internal.web.api.devices import get_devices
    pending = [("peer-abc", "12345678", "Phone", "pending")]
    res, _ = get_devices(
        _minimal_cfg(), lambda: [], get_pending_pairings=lambda: pending)
    assert res["pending_pairings"][0]["sas"] == ""
    # dict-shaped entries carry it under the same key
    res, _ = get_devices(
        _minimal_cfg(), lambda: [],
        get_pending_pairings=lambda: [{"peer_id": "p", "sas": "AAAA-BBBB"}])
    assert res["pending_pairings"][0]["sas"] == "AAAA-BBBB"


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
