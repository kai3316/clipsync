"""Round 14 — internet pairing code + UX guidance (web settings layer).

Static wiring assertions for the guided internet-sync block:
  1. api.js wraps the three internet-pairing endpoints against the agreed
     contract paths (generate / enter / status).
  2. store.js holds the paired-over-internet list + this device's generated
     code, refreshes from GET /api/internetpair/status defensively (older
     backend → empty state, no throw), and folds the WS ``netpair_peer``
     event into the list with a toast.
  3. ws.js handles the ``netpair_peer`` event (known status vocabulary only).
  4. settings-panel.js renders a persistent three-step guide, generate/enter
     controls, a paired-device list, an actionable relay-error state, an
     "initial" state for off-but-enabled, and a "go pair" action when enabled
     with no peers; the search index picks the new keys up.
  5. Locales: en / zh-CN key sets identical and every round-14 key present
     and non-empty in both.
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
    # Only the known status vocabulary reaches the store.
    assert "data.status === 'paired'" in chunk
    assert "store.applyNetpairPeer" in chunk
    # Malformed payloads without a peer id are dropped before the store call.
    assert "data.peer_id" in chunk


# ── 3. Settings panel guided block ──────────────────────────────────────


def test_settings_panel_three_step_guide():
    src = _read("components", "settings-panel.js")
    # Persistent guide block, focusable, with the three step rows.
    assert "netpair-guide" in src
    assert 'ref="netpairGuide"' in src
    assert "netpair-guide__steps" in src
    for i in ("1", "2", "3"):
        assert f"netpair_step{i}" in src, f"missing guide step {i}"
    assert "netpair_steps_title" in src


def test_settings_panel_generate_wiring():
    src = _read("components", "settings-panel.js")
    assert "generateNetpairCode" in src
    assert "ClipsyncAPI.generateInternetPair()" in src
    # Generated code is displayed grouped (ABCD-EFGH-IJKL) + copyable.
    assert "netpairDisplayCode" in src
    assert ".match(/.{1,4}/g)" in src
    assert "copyNetpairCode" in src
    assert "netpair_regenerate" in src  # regenerate invalidates the old code


def test_settings_panel_enter_wiring():
    src = _read("components", "settings-panel.js")
    assert "confirmNetpairCode" in src
    assert "ClipsyncAPI.enterInternetPair(code)" in src
    assert "netpairCodeInput" in src
    # Auto-ignore spaces/lowercase, cap at 12 alphanumeric chars.
    assert "onNetpairCodeInput" in src
    assert ".toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12)" in src
    # Illegal code surfaces an explicit inline error.
    assert "code.length !== 12" in src
    assert "netpair_invalid_code" in src
    assert "e.status === 400" in src
    # Success toasts "paired <name>" and refreshes the list.
    assert "netpair_paired_toast" in src
    assert "fetchInternetPairStatus()" in src


def test_settings_panel_paired_list():
    src = _read("components", "settings-panel.js")
    assert "netpair_paired_title" in src
    assert "netpair_empty" in src
    assert "internetPairPeers.length === 0" in src
    assert 'v-for="peer in internetPairPeers"' in src
    # Status badge gated on the peer's 'paired' status (single quotes are
    # escaped inside the JS template string).
    assert "peer.status" in src
    assert "ui.paired" in src


def test_settings_panel_status_branches():
    src = _read("components", "settings-panel.js")
    # off-but-enabled renders as the initial (pre-connecting) state, never a
    # bare "Off".
    assert "relayDisplayState" in src
    assert "internetSyncEnabled && state === 'off') return 'initial'" in src
    assert "relay.state.initial" in src
    # relay error is always explained with actionable text.
    assert "relayStateErrorText" in src
    assert "settings_window.netpair_error_detail" in src
    assert "effectiveRelayState === 'error'" in src
    # Enabled with no peers shows a "go pair" action that scrolls to the guide.
    assert "showUnpairedHint" in src
    assert "netpair_go_pair" in src
    assert "focusNetpairGuide" in src


def test_settings_panel_search_index_updated():
    src = _read("components", "settings-panel.js")
    # The network search bucket picks up the new copy so the header search box
    # can find the guided block.
    for key in (
        "relay.state.initial",
        "settings_window.netpair_steps_title",
        "settings_window.netpair_step1",
        "settings_window.netpair_step2",
        "settings_window.netpair_step3",
        "settings_window.netpair_generate",
        "settings_window.netpair_enter_title",
        "settings_window.netpair_error_detail",
        "settings_window.netpair_go_pair",
    ):
        assert key in src, f"search index missing key: {key}"


def test_settings_panel_load_netpair_state_wired():
    src = _read("components", "settings-panel.js")
    # Refresh on panel open AND on switching to the network section.
    assert src.count("this.loadNetpairState()") == 2
    assert "fetchInternetPairStatus()" in src


# ── 4. Locale parity ────────────────────────────────────────────────────

_NEW_KEYS = [
    "relay.state.initial",
    "settings_window.netpair_steps_title",
    "settings_window.netpair_step1",
    "settings_window.netpair_step2",
    "settings_window.netpair_step3",
    "settings_window.netpair_generate",
    "settings_window.netpair_regenerate",
    "settings_window.netpair_generate_hint",
    "settings_window.netpair_enter_title",
    "settings_window.netpair_enter_placeholder",
    "settings_window.netpair_confirm",
    "settings_window.netpair_invalid_code",
    "settings_window.netpair_paired_title",
    "settings_window.netpair_empty",
    "settings_window.netpair_paired_toast",
    "settings_window.netpair_error_detail",
    "settings_window.netpair_go_pair",
    "settings_window.netpair_code_copied",
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
