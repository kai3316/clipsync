"""Round 14 — internet pairing code + UX guidance (web layer).

Round 15 moved the pairing management UI out of Settings → Network and into
the Devices page; this module keeps the parts of round 14 that still hold and
asserts the migration:
  1. api.js wraps the internet-pairing endpoints against the agreed contract
     paths (generate / enter / status; round 15 adds rename / unpair).
  2. store.js holds the paired-over-internet list + this device's generated
     code, refreshes from GET /api/internetpair/status defensively (older
     backend → empty state, no throw), and folds the WS ``netpair_peer``
     event (paired / unpaired) into the list with a toast.
  3. ws.js handles the ``netpair_peer`` event (known status vocabulary only).
  4. settings-panel.js keeps the toggle + relay status row (initial/error
     states) and a pointer to the Devices page — pairing management itself
     lives on the Devices page now (see test_round15_devices_webux.py).
  5. Locales: en / zh-CN key sets identical and the retained round-14 keys
     present and non-empty in both.
  6. A node --check pass over every JS file this round touched (when node
     is available).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. API wrappers ─────────────────────────────────────────────────────


def test_api_internetpair_generate_wrapper():
    src = _read("js", "api.js")
    assert "generateInternetPair" in src
    assert "'/api/internetpair/generate'" in src


def test_api_internetpair_enter_wrapper():
    src = _read("js", "api.js")
    assert "enterInternetPair" in src
    assert "'/api/internetpair/enter'" in src
    body = src.split("enterInternetPair")[1]
    assert "code: code" in body


def test_api_internetpair_status_wrapper():
    src = _read("js", "api.js")
    assert "getInternetPairStatus" in src
    assert "'/api/internetpair/status'" in src


# ── 2. Store state + WS event wiring ────────────────────────────────────


def test_store_declares_internetpair_state():
    src = _read("js", "store.js")
    assert "internetPairPeers: []" in src
    assert "internetPairCode: ''" in src
    assert "fetchInternetPairStatus:" in src
    assert "applyNetpairPeer:" in src


def test_store_fetch_internet_pair_status_defensive():
    src = _read("js", "store.js")
    chunk = src.split("fetchInternetPairStatus: function")[1].split("applyNetpairPeer")[0]
    # Missing API / older backend must settle to a resolved false — never throw.
    assert "getInternetPairStatus" in chunk
    assert "return Promise.resolve(false)" in chunk
    # Peers array is normalized (guarded + name fallback to id).
    assert "Array.isArray(res.peers)" in chunk
    assert "p.peer_id === undefined" in chunk
    assert "p.name || String(p.peer_id)" in chunk
    # generated_code from the status snapshot becomes internetPairCode.
    assert "res.generated_code" in chunk
    assert "internetPairCode" in chunk
    # Failure keeps whatever was loaded and signals false for the empty state.
    assert "return false" in chunk


def test_store_apply_netpair_peer_upserts_and_toasts():
    src = _read("js", "store.js")
    chunk = src.split("applyNetpairPeer: function")[1].split("applyAiConfigFileResult")[0]
    # The WS event is an upsert: any prior row for the same peer is dropped
    # before appending the fresh paired row.
    assert "internetPairPeers.filter" in chunk
    assert "status: 'paired'" in chunk
    assert "showToast" in chunk
    assert "netpair_paired_toast" in chunk


def test_ws_handles_netpair_peer_event():
    src = _read("js", "ws.js")
    assert "case 'netpair_peer'" in src
    chunk = src.split("case 'netpair_peer'")[1].split("break;")[0]
    # Only the known status vocabulary reaches the store (round 15: unpaired
    # too — the WS now folds both directions of a netpair change).
    assert "data.status === 'paired' || data.status === 'unpaired'" in chunk
    assert "store.applyNetpairPeer" in chunk
    # Malformed payloads without a peer id are dropped before the store call.
    assert "data.peer_id" in chunk


# ── 3. Settings panel simplified (round 15) ─────────────────────────────


def test_settings_panel_keeps_toggle_and_status():
    # The network section keeps the toggle, the relay status row (with the
    # initial off-but-enabled state and the actionable error text).
    src = _read("components", "settings-panel.js")
    assert "toggleInternetSync" in src
    assert "relayDisplayState" in src
    assert "internetSyncEnabled && state === 'off') return 'initial'" in src
    assert "relay.state.initial" in src
    assert "relayStateErrorText" in src
    assert "settings_window.netpair_error_detail" in src
    assert "effectiveRelayState === 'error'" in src


def test_settings_panel_pairing_management_moved_to_devices():
    # Round 15 moved pairing out of Settings. The generate/enter/list UI is
    # gone; a single pointer jumps to the Devices page.
    src = _read("components", "settings-panel.js")
    assert "goToDevicesTab" in src
    assert "netpair_manage_hint" in src
    assert "netpair_manage_cta" in src
    assert "netpair-guide" not in src
    assert "generateNetpairCode" not in src
    assert "confirmNetpairCode" not in src
    assert "netpair_paired_title" not in src
    assert "loadNetpairState" not in src
    # The network search bucket drops the removed pairing keys and keeps the
    # relay/status + manage-pointer keys.
    assert "relay.state.initial" in src
    assert "settings_window.netpair_error_detail" in src
    assert "settings_window.netpair_manage_hint" in src
    assert "settings_window.netpair_manage_cta" in src
    assert "netpair_steps_title" not in src
    assert "netpair_go_pair" not in src


# ── 4. Locale parity ────────────────────────────────────────────────────

_NEW_KEYS = [
    "relay.state.initial",
    "settings_window.netpair_paired_toast",
    "settings_window.netpair_error_detail",
    "settings_window.netpair_manage_hint",
    "settings_window.netpair_manage_cta",
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


def test_new_round14_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # The success toast interpolates the peer name in both languages.
    for key in ("settings_window.netpair_paired_toast",):
        assert "{name}" in en[key], key
        assert "{name}" in zh[key], key


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 5. JS syntax (node --check over every file this round touched) ──────


_TOUCHED_JS = [
    ("components", "device-panel.js"),
    ("components", "settings-panel.js"),
    ("js", "api.js"),
    ("js", "store.js"),
    ("js", "ws.js"),
]


def test_touched_js_passes_node_check(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


def test_no_nul_bytes_in_touched_js():
    # A stray NUL byte inside a template string survives some editors but is
    # a landmine for others — keep the shipped sources clean.
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"
