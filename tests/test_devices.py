"""Round 15 — internet pairing moved into the Devices page (web UX).

Round 14 put internet pairing inside Settings → Network. Round 15 turns the
Devices page into a real device-management surface:
  1. device-panel.js gains a prominent「互联网配对」section: a relay-status
     overview line, a three-step guide (empty state), generate/enter controls,
     a paired-peer list with status dot + relative last-seen, and rename/unpair
     actions. It also overlays a 🌐 internet badge on the LOCAL device (when the
     relay is online) and on every LAN device card whose device_id is ALSO an
     internet-pair peer.
  2. api.js wraps the two new endpoints (rename / unpair).
  3. store.js normalizes the extended status shape (alias / online / last_seen /
     paired), and folds both `netpair_peer` statuses (paired / unpaired) in;
     ws.js accepts the same vocabulary.
  4. settings-panel.js drops the generate/enter/paired-list UI (moved out) and
     keeps only the toggle + relay status row + a pointer to the Devices tab.
  5. Locales: en / zh-CN key sets identical; every round-15 key present and
     non-empty in both.
  6. A node --check pass over every JS file this round touched.
"""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. Devices page: internet-pairing section ────────────────────────────


def test_device_panel_has_internet_pairing_section():
    src = _read("components", "device-panel.js")
    assert "netpair-section" in src
    assert "devices.netpair_title" in src
    # Overview row reuses the relay-state semantics (dot + key) + count.
    assert "netpair-overview" in src
    assert "relayStateColor" in src
    assert "relayStateKey" in src
    assert "devices.netpair_overview_paired" in src


def test_device_panel_collapsed_behind_toggle():
    src = _read("components", "device-panel.js")
    # The section is collapsed behind a chevron accordion header — the body
    # only renders when expanded, so the page isn't a wall of pairing prompts.
    assert "netpairExpanded" in src
    assert "toggleNetpair" in src
    assert "netpair-section__header" in src
    assert "netpair-section__chevron" in src
    assert "netpair-switch" not in src
    assert 'v-if="netpairExpanded"' in src
    # Expanded by default so the pairing controls are visible without hunting;
    # the count badge shows how many internet peers are paired.
    assert "netpairExpanded: true" in src
    assert "netpairPairedCount" in src
    # When internet sync is off, one compact line + jump-to-settings (not the
    # old multi-step guide).
    assert "internetSyncEnabled" in src
    assert "devices.netpair_sync_off" in src
    assert "devices.netpair_go_settings" in src
    assert "openInternetSyncSettings" in src
    # The old 3-step guide is gone.
    assert "netpair_step1" not in src


def test_device_panel_generate_wiring():
    src = _read("components", "device-panel.js")
    assert "generateNetpairCode" in src
    assert "ClipsyncAPI.generateInternetPair()" in src
    # Generated code grouped ABCD-EFGH-IJKL + copy + regenerate-invalidates-old.
    assert "netpairDisplayCode" in src
    assert ".match(/.{1,4}/g)" in src
    assert "copyNetpairCode" in src
    assert "devices.netpair_generate" in src
    assert "devices.netpair_regenerate" in src
    assert "devices.netpair_code_copied" in src


def test_device_panel_enter_wiring():
    src = _read("components", "device-panel.js")
    assert "confirmNetpairCode" in src
    assert "ClipsyncAPI.enterInternetPair(code)" in src
    assert "netpairCodeInput" in src
    # Auto-ignore spaces/lowercase, cap at 12 alphanumeric chars.
    assert "onNetpairCodeInput" in src
    assert ".toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12)" in src
    # The three inline errors the spec asks for + a generic invalid fallback.
    assert "code.length !== 12" in src
    assert "devices.netpair_error_format" in src
    assert "devices.netpair_error_sync_off" in src
    assert "devices.netpair_error_self" in src
    assert "devices.netpair_error_invalid" in src
    assert "e.status === 400" in src
    # The "paired" toast now lives in the WS handler (store.applyNetpairPeer),
    # not the enter response — one toast per pairing, not two.
    assert "settings_window.netpair_paired_toast" not in src


def test_device_panel_rename_unpair_wiring():
    src = _read("components", "device-panel.js")
    # Rename: prompt → rename endpoint → store local update → refetch.
    assert "renamePeer" in src
    assert "ClipsyncAPI.renameInternetPair(peer.peer_id, name)" in src
    assert "devices.netpair_rename" in src
    assert "devices.netpair_rename_title" in src
    assert "devices.netpair_rename_prompt" in src
    assert "store.setInternetPeerName" in src
    assert "devices.netpair_renamed_toast" in src
    # Unpair: confirm → unpair endpoint → store removal.
    assert "unpairPeer" in src
    assert "ClipsyncAPI.unpairInternetPair(peer.peer_id)" in src
    assert "devices.netpair_unpair" in src
    assert "devices.netpair_unpair_confirm" in src
    assert "store.removeInternetPeer" in src
    assert "devices.netpair_unpaired_toast" in src


def test_device_panel_peer_status_and_last_seen():
    src = _read("components", "device-panel.js")
    # Display name prefers alias, then name.
    assert "peerDisplayName" in src
    assert "peer.alias" in src
    # Status dot + relative "last sync" time (last_seen null → "never synced").
    assert "netpair-peer__dot--online" in src
    assert "netpair-peer__dot--offline" in src
    assert "lastSeenText" in src
    assert "devices.netpair_never_synced" in src
    assert "time.minutes_ago" in src
    assert "peer.online" in src
    # The 30s relative-time refresh keeps the text fresh.
    assert "beforeUnmount" in src
    assert "setInterval" in src
    assert "$forceUpdate" in src


def test_device_panel_loads_state_on_mount():
    src = _read("components", "device-panel.js")
    assert "loadNetpairState" in src
    assert "store.fetchInternetPairStatus()" in src
    assert "mounted" in src


# ── 2. LAN-card + local-device internet badges (round 15 addition) ────────


def test_device_panel_local_internet_badge():
    src = _read("components", "device-panel.js")
    # The local device gets a 🌐「互联网在线」badge only while the relay is
    # online (sync enabled AND relay_state == online).
    assert "localInternetOnline" in src
    assert "internetSyncEnabled && this.effectiveRelayState === 'online'" in src
    assert "netpair-local-badge" in src
    assert "devices.netpair_also_internet" in src


def test_device_panel_lan_card_internet_badge():
    src = _read("components", "device-panel.js")
    # Every LAN device-card is wrapped so an internet-pair peer can overlay a
    # 🌐 badge (online green / offline grey) by matching device_id ↔ peer_id.
    assert "device-internet-wrap" in src
    assert "netpair-card-badge" in src
    assert "netpair-card-badge--online" in src
    assert "netpair-card-badge--offline" in src
    # The cross-reference helper maps a LAN device id to its internet peer.
    assert "netpairPeerFor" in src
    assert "String(peers[i].peer_id) === String(deviceId)" in src
    # Both badge variants carry the one-line hover explanation.
    assert "devices.netpair_also_internet" in src


def test_device_panel_lan_cards_are_wrapped_in_all_sections():
    src = _read("components", "device-panel.js")
    # Connected / Temporary / Paired Offline / Discovered each wrap their cards.
    for vfor in (
        'v-for="dev in connectedSyncDevices"',
        'v-for="dev in temporaryConnectedDevices"',
        'v-for="dev in pairedOfflineDevices"',
        'v-for="dev in discoveredDevices"',
    ):
        assert vfor in src, vfor
    assert src.count('class="device-internet-wrap"') == 4


# ── 3. API / store / ws wiring for the extended contract ─────────────────


def test_api_rename_and_unpair_wrappers():
    src = _read("js", "api.js")
    assert "renameInternetPair" in src
    assert "'/api/internetpair/rename'" in src
    body = src.split("renameInternetPair")[1]
    assert "peer_id: peerId" in body and "name: name" in body
    assert "unpairInternetPair" in src
    assert "'/api/internetpair/unpair'" in src
    body2 = src.split("unpairInternetPair")[1]
    assert "peer_id: peerId" in body2


def test_store_normalizes_extended_status_shape():
    src = _read("js", "store.js")
    chunk = src.split("fetchInternetPairStatus: function")[1].split("applyNetpairPeer")[0]
    # The extended peer shape is normalized defensively (older backend → safe
    # defaults: alias '', online false, last_seen null, paired true).
    assert "alias: p.alias || ''" in chunk
    assert "online: !!p.online" in chunk
    assert "last_seen" in chunk
    assert "p.paired !== false" in chunk
    # generated_code may be null — the stale code is cleared, not kept.
    assert "self.internetPairCode = (res && res.generated_code)" in chunk


def test_store_netpair_peer_handles_paired_and_unpaired():
    src = _read("js", "store.js")
    chunk = src.split("applyNetpairPeer: function")[1].split("applyAiConfigFileResult")[0]
    assert "data.status === 'unpaired'" in chunk
    assert "this.removeInternetPeer(pid)" in chunk
    assert "status: 'paired'" in chunk


def test_store_has_remove_and_rename_helpers():
    src = _read("js", "store.js")
    assert "removeInternetPeer: function" in src
    assert "setInternetPeerName: function" in src
    chunk = src.split("setInternetPeerName: function")[1]
    assert "peer_id" in chunk and "alias" in chunk


def test_ws_accepts_paired_and_unpaired_vocabulary():
    src = _read("js", "ws.js")
    assert "case 'netpair_peer'" in src
    chunk = src.split("case 'netpair_peer'")[1].split("break;")[0]
    assert "data.status === 'paired' || data.status === 'unpaired'" in chunk
    assert "data.peer_id" in chunk
    assert "store.applyNetpairPeer" in chunk


# ── 4. Settings panel simplified ─────────────────────────────────────────


def test_settings_panel_pairing_ui_removed():
    src = _read("components", "settings-panel.js")
    # No guide, no generate/enter, no paired list in the network section.
    assert "netpair-guide" not in src
    assert "generateNetpairCode" not in src
    assert "confirmNetpairCode" not in src
    assert "netpairDisplayCode" not in src
    assert "netpair_steps_title" not in src
    assert "internetPairPeers.length === 0" not in src
    assert "loadNetpairState" not in src


def test_settings_panel_keeps_toggle_and_status_and_devices_pointer():
    src = _read("components", "settings-panel.js")
    # Toggle + status row stay.
    assert "toggleInternetSync" in src
    assert "relayStateColor" in src
    assert "relayStateKey" in src
    assert "relayStateErrorText" in src
    assert "settings_window.netpair_error_detail" in src
    # Pairing management pointer → Devices tab (round 15).
    assert "goToDevicesTab" in src
    assert "store.activeTab = 'devices'" in src
    assert "settings_window.netpair_manage_hint" in src
    assert "settings_window.netpair_manage_cta" in src
    # The search index no longer lists the removed pairing keys.
    assert "netpair_go_pair" not in src
    assert "netpair_generate" not in src


# ── 5. Locale parity ─────────────────────────────────────────────────────

_NEW_KEYS = [
    "settings_window.netpair_manage_hint",
    "settings_window.netpair_manage_cta",
    "devices.netpair_title",
    "devices.netpair_overview_paired",
    "devices.netpair_empty_title",
    "devices.netpair_step1",
    "devices.netpair_step1_done",
    "devices.netpair_go_settings",
    "devices.netpair_step2",
    "devices.netpair_step3",
    "devices.netpair_generate",
    "devices.netpair_regenerate",
    "devices.netpair_code_valid_hint",
    "devices.netpair_enter_title",
    "devices.netpair_confirm",
    "devices.netpair_error_format",
    "devices.netpair_error_self",
    "devices.netpair_error_sync_off",
    "devices.netpair_error_invalid",
    "devices.netpair_paired_list",
    "devices.netpair_online",
    "devices.netpair_offline",
    "devices.netpair_never_synced",
    "devices.netpair_rename",
    "devices.netpair_unpair",
    "devices.netpair_rename_title",
    "devices.netpair_rename_prompt",
    "devices.netpair_unpair_confirm",
    "devices.netpair_unpaired_toast",
    "devices.netpair_renamed_toast",
    "devices.netpair_code_copied",
    "devices.netpair_also_internet",
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


def test_new_round15_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # Interpolated placeholders used by the panel code exist in both.
    for key in ("devices.netpair_overview_paired",):
        assert "{count}" in en[key] and "{count}" in zh[key], key
    for key in (
        "devices.netpair_unpair_confirm",
        "devices.netpair_unpaired_toast",
        "devices.netpair_renamed_toast",
    ):
        assert "{name}" in en[key] and "{name}" in zh[key], key


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 6. JS syntax (node --check over every file this round touched) ────────


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
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"


def test_no_nul_bytes_in_touched_js():
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"


# ══════════════════════════════════════════════════
# merged from test_round14_webux.py (shared scaffold suffixed r14)
# ══════════════════════════════════════════════════

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT_r14 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_r14 = os.path.join(_ROOT_r14, "internal", "web", "static")


def _read_r14(*parts) -> str:
    with open(os.path.join(_STATIC_r14, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. API wrappers ─────────────────────────────────────────────────────


def test_api_internetpair_generate_wrapper():
    src = _read_r14("js", "api.js")
    assert "generateInternetPair" in src
    assert "'/api/internetpair/generate'" in src


def test_api_internetpair_enter_wrapper():
    src = _read_r14("js", "api.js")
    assert "enterInternetPair" in src
    assert "'/api/internetpair/enter'" in src
    body = src.split("enterInternetPair")[1]
    assert "code: code" in body


def test_api_internetpair_status_wrapper():
    src = _read_r14("js", "api.js")
    assert "getInternetPairStatus" in src
    assert "'/api/internetpair/status'" in src


# ── 2. Store state + WS event wiring ────────────────────────────────────


def test_store_declares_internetpair_state():
    src = _read_r14("js", "store.js")
    assert "internetPairPeers: []" in src
    assert "internetPairCode: ''" in src
    assert "fetchInternetPairStatus:" in src
    assert "applyNetpairPeer:" in src


def test_store_fetch_internet_pair_status_defensive():
    src = _read_r14("js", "store.js")
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
    src = _read_r14("js", "store.js")
    chunk = src.split("applyNetpairPeer: function")[1].split("applyAiConfigFileResult")[0]
    # The WS event is an upsert: any prior row for the same peer is dropped
    # before appending the fresh paired row.
    assert "internetPairPeers.filter" in chunk
    assert "status: 'paired'" in chunk
    assert "showToast" in chunk
    assert "netpair_paired_toast" in chunk


def test_ws_handles_netpair_peer_event():
    src = _read_r14("js", "ws.js")
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
    src = _read_r14("components", "settings-panel.js")
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
    src = _read_r14("components", "settings-panel.js")
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

_NEW_KEYS_r14 = [
    "relay.state.initial",
    "settings_window.netpair_paired_toast",
    "settings_window.netpair_error_detail",
    "settings_window.netpair_manage_hint",
    "settings_window.netpair_manage_cta",
]


def _locales_r14():
    with open(os.path.join(_STATIC_r14, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC_r14, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_identical_r14():
    en, zh = _locales_r14()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round14_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales_r14()
    for key in _NEW_KEYS_r14:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # The success toast interpolates the peer name in both languages.
    for key in ("settings_window.netpair_paired_toast",):
        assert "{name}" in en[key], key
        assert "{name}" in zh[key], key


def test_locale_json_files_still_parse_r14():
    en, zh = _locales_r14()
    assert len(en) > 1000 and len(zh) > 1000


# ── 5. JS syntax (node --check over every file this round touched) ──────


_TOUCHED_JS_r14 = [
    ("components", "device-panel.js"),
    ("components", "settings-panel.js"),
    ("js", "api.js"),
    ("js", "store.js"),
    ("js", "ws.js"),
]


def test_touched_js_passes_node_check_r14(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS_r14:
        path = os.path.join(_STATIC_r14, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"


def test_no_nul_bytes_in_touched_js_r14():
    # A stray NUL byte inside a template string survives some editors but is
    # a landmine for others — keep the shipped sources clean.
    for parts in _TOUCHED_JS_r14:
        path = os.path.join(_STATIC_r14, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"


# ══════════════════════════════════════════════════
# merged from test_round19_devices_layout.py (shared scaffold suffixed r19)
# ══════════════════════════════════════════════════

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT_r19 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_r19 = os.path.join(_ROOT_r19, "internal", "web", "static")


def _read_r19(*parts) -> str:
    with open(os.path.join(_STATIC_r19, *parts), encoding="utf-8") as f:
        return f.read()


# (label, unique template marker) for each device section, top→bottom as the
# agreed target order dictates.  Each marker must appear exactly once in the
# template (checked below) so its index is a reliable ordering signal.
# Pairing Requests are PINNED near the top: the pairing codes are
# time-sensitive and must be compared on both devices, so they must never sit
# below the device lists.  Internet pairing sits at the very BOTTOM, collapsed
# behind a toggle (per user request — fewer prompts).
_SECTION_MARKERS = [
    ("this_device", "devices.this_device"),
    ("pairing_requests", "devices.pairing_requests"),
    ("connected", "device.connected"),
    # Chat-only live sessions (connected && !paired) get their own section so
    # they don't masquerade as sync connections.
    ("temporary", "device.temporary_connected"),
    ("paired_offline", "device.paired_offline"),
    ("discovered", "device.discovered"),
    # Forgotten devices are archived, not dropped — manageable here.
    ("removed", "devices.removed_title"),
    ("netpair", "devices.netpair_title"),
]


# ── 1. Section order ────────────────────────────────────────────────────


def test_device_sections_in_agreed_order():
    src = _read_r19("components", "device-panel.js")
    positions = []
    for _label, marker in _SECTION_MARKERS:
        positions.append(src.index(marker))
    for (a_label, _a), (b_label, _b), (pa, pb) in zip(
        _SECTION_MARKERS,
        _SECTION_MARKERS[1:],
        zip(positions, positions[1:], strict=False),
        strict=False,
    ):
        assert pa < pb, (
            f"section '{a_label}' (index {pa}) must render above '{b_label}' (index {pb})"
        )


def test_section_markers_each_appear_once():
    src = _read_r19("components", "device-panel.js")
    for _label, marker in _SECTION_MARKERS:
        assert src.count(marker) == 1, f"section marker {marker!r} must be unique in the template"


# ── 2. This Device on top / loading gate / terminal states ──────────────


def test_this_device_on_top_loading_gates_lan_sections():
    src = _read_r19("components", "device-panel.js")
    # This Device is the first section — above the LAN-list loading skeleton.
    assert src.index("devices.this_device") < src.index("device-panel__loading")
    # The loading skeleton gates only the LAN sections below it.
    assert src.index("device-panel__loading") < src.index("devices.pairing_requests")
    # Internet pairing is at the very bottom (below the LAN lists).
    assert src.index("device-panel__loading") < src.index("devices.netpair_title")
    assert src.index("device.discovered") < src.index("devices.netpair_title")


def test_empty_and_load_failed_states_stay_at_bottom():
    src = _read_r19("components", "device-panel.js")
    # Terminal states render after the last real section (Pairing Requests).
    assert src.index("devices.load_failed") > src.index("devices.pairing_requests")
    assert src.index("devices.no_devices_found") > src.index("devices.pairing_requests")
    # Empty state chains off the load-failed state (v-if → v-else-if).
    failed = src.index("loadFailed || store.devicesLoadFailed")
    empty = src.index('v-else-if="allRemoteDevices.length === 0')
    assert failed < empty


# ── 3. Round 15/16/17/18 features intact ────────────────────────────────


def test_alias_and_netpair_helpers_still_present():
    src = _read_r19("components", "device-panel.js")
    assert "devWithAlias: function" in src
    assert "netpairPeerFor: function" in src
    assert "String(peers[i].peer_id) === String(deviceId)" in src


def test_lan_cards_still_wrapped_in_all_four_sections():
    src = _read_r19("components", "device-panel.js")
    for vfor in (
        'v-for="dev in connectedSyncDevices"',
        'v-for="dev in temporaryConnectedDevices"',
        'v-for="dev in pairedOfflineDevices"',
        'v-for="dev in discoveredDevices"',
    ):
        assert vfor in src, vfor
    assert src.count('class="device-internet-wrap"') == 4


def test_local_device_internet_badge_still_wired():
    src = _read_r19("components", "device-panel.js")
    assert "localInternetOnline" in src
    assert "netpair-local-badge" in src
    # The badge must live on the (now top) This Device card.
    assert src.index("netpair-local-badge") > src.index("devices.this_device")
    assert src.index("netpair-local-badge") < src.index("devices.netpair_title")


def test_each_lan_section_keeps_empty_skip_guard():
    src = _read_r19("components", "device-panel.js")
    for guard in (
        "connectedSyncDevices.length > 0",
        "temporaryConnectedDevices.length > 0",
        "pairedOfflineDevices.length > 0",
        "discoveredDevices.length > 0",
        "pairingRequests.length > 0",
    ):
        assert guard in src, guard


# ── 5. Temporary section + removed-devices archive + collapsibility ──────


def test_temporary_and_removed_sections_wired():
    src = _read_r19("components", "device-panel.js")
    # Temporary bucket = connected && !paired (chat-only sessions).
    assert (
        "return this.allRemoteDevices.filter(function (d) { return d.connected && !d.paired; });"
        in src
    )
    # Removed bucket reads the store archive.
    assert "removedDevices: function ()" in src
    assert "this.store.removedDevices || []" in src


def test_removed_section_restore_and_purge_wiring():
    src = _read_r19("components", "device-panel.js")
    assert "restoreRemovedDevice: function" in src
    assert "purgeRemovedDevice: function" in src
    assert "ClipsyncAPI.restoreDevice(dev.device_id)" in src
    assert "ClipsyncAPI.purgeDevice(dev.device_id)" in src
    # The row shows removal time and the short id.
    assert "removed-device-row__time" in src
    assert "shortId(dev.device_id)" in src


def test_sections_are_collapsible_and_default_open():
    src = _read_r19("components", "device-panel.js")
    # Every section header is now a toggle button sharing the internet-pairing
    # chevron affordance.
    assert src.count("section-header--toggle") >= 7
    assert "toggleSection: function" in src
    # All sections default to expanded.
    for key in ("local", "pairing", "connected", "temporary", "paired", "discovered", "removed"):
        assert f"{key}: true," in src, f"section {key} must default open"


def test_api_restore_and_purge_wrappers():
    src = _read_r19("js", "api.js")
    assert "restoreDevice: function" in src
    assert "purgeDevice: function" in src
    assert "'/api/device/restore'" in src
    assert "'/api/device/purge'" in src


def test_new_removed_locale_keys_present_in_both():
    en, zh = _locales_r19()
    for key in (
        "device.temporary_connected",
        "device.restore",
        "device.purge",
        "devices.removed_title",
        "devices.removed_at",
        "devices.restore_confirm_msg",
        "devices.purge_confirm_msg",
    ):
        assert key in en and en[key], key
        assert key in zh and zh[key], key


def test_ai_config_ui_merged_into_unified_panel():
    # The paired-device AI-config inventories panel no longer lives in the
    # Devices page — the Devices template must not mount it.
    src = _read_r19("components", "device-panel.js")
    assert "aiconfig-device-panel" not in src
    # The refactor then went further: the separate device sub-panel component
    # was deleted and merged INTO aiconfig-panel, which now hosts the peer
    # device bar, the per-device inventories AND the local manager in one file.
    assert not os.path.exists(os.path.join(_STATIC_r19, "components", "aiconfig-device-panel.js"))
    aiconf = _read_r19("components", "aiconfig-panel.js")
    assert "aiconfig-device-panel" not in aiconf
    assert "H.diffCounts" in aiconf  # peer diff badges
    assert "aiconfig.local_title" in aiconf  # local manage sub-view


# ── 4. Locale parity (round 19 adds no new copy) ────────────────────────


def _locales_r19():
    with open(os.path.join(_STATIC_r19, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC_r19, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_still_identical():
    en, zh = _locales_r19()
    assert set(en) == set(zh)


def test_reordered_section_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales_r19()
    for key in (
        "devices.this_device",
        "devices.netpair_title",
        "device.connected",
        "device.paired_offline",
        "device.discovered",
        "devices.pairing_requests",
    ):
        assert key in en and key in zh, key
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key


# ── 5. JS syntax (node --check) ─────────────────────────────────────────


def test_device_panel_passes_node_check(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    path = os.path.join(_STATIC_r19, "components", "device-panel.js")
    proc = subprocess.run(
        [node, "--check", path],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_nul_bytes_in_device_panel():
    with open(os.path.join(_STATIC_r19, "components", "device-panel.js"), "rb") as f:
        data = f.read()
    assert b"\x00" not in data


# ════════════════════════════════════════════════════════════════════════
# 6 — backend: EVERY removed device is archived (Bug B: 点移除后无法找回)
# ════════════════════════════════════════════════════════════════════════


class _StubPairingMgr:
    """Minimal pairing manager: records removals, restores forget them."""

    def __init__(self):
        self.removed = []
        self.rejected = []
        # paired flag the last restore_peer() was called with — restore must
        # never re-pair (see test_restore_does_not_repair_device).
        self.restored_paired = None

    def remove_peer(self, peer_id):
        self.removed.append(peer_id)

    def reject_pairing(self, peer_id):
        self.rejected.append(peer_id)

    def restore_peer(self, peer_id, name, paired=False):
        self.restored_paired = paired
        if peer_id in self.removed:
            self.removed.remove(peer_id)

    def get_known_peers(self):
        return []


class _StubTransportMgr:
    def __init__(self):
        self.disconnected = []
        self.rejected = []
        self.allowed = []
        self.connect_calls = []

    def disconnect_peer(self, peer_id, reject=False):
        self.disconnected.append(peer_id)
        if reject:
            self.rejected.append(peer_id)

    # _on_remove now calls forget_peer (reject) rather than disconnect_peer so
    # a forgotten device can't immediately reconnect — record it the same way.
    def forget_peer(self, peer_id):
        self.disconnected.append(peer_id)

    # Restore lifts the forget-time rejection so the peer can also initiate
    # the pairing from its own side.
    def allow_peer(self, peer_id):
        self.allowed.append(peer_id)

    def connect_to_peer(self, *a, **k):
        self.connect_calls.append((a, k))

    def get_peer_addresses(self):
        return {}

    # _on_connect walks these two before giving up; empty means "nowhere to
    # dial", which is exactly the unreachable path under test.
    def get_resolved_hashes(self):
        return {}

    def get_saved_address(self, peer_id):
        return None


def _remove_app(monkeypatch):
    """Application via __new__ for the remove/restore/purge backend."""
    from internal.config.config import Config
    from src.main import Application

    app = Application.__new__(Application)
    cfg = Config()
    cfg.peers = {}
    cfg.removed_peers = {}
    app.cfg = cfg
    app.pairing_mgr = _StubPairingMgr()
    app.transport_mgr = _StubTransportMgr()
    app.chat_mgr = None
    app._discovered_lock = threading.Lock()
    app._discovered_peers = {}
    app._push_web = lambda *a, **k: None
    monkeypatch.setattr(app, "_save_cfg_encrypted", lambda: None)
    return app


def test_on_remove_archives_paired_peer_for_restore(monkeypatch):
    """A paired peer being removed lands in removed_peers (not just cfg.peers
    being dropped) so the Removed section can offer a Restore action."""
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    app.cfg.peers["peer-1"] = PeerInfo(
        device_id="peer-1",
        device_name="Old Mac",
        public_key_pem="pem",
        paired=True,
        notes="the old one",
        last_ip="192.168.1.20",
        last_port=19990,
    )

    app._on_remove("peer-1")

    assert "peer-1" not in app.cfg.peers
    assert app.pairing_mgr.removed == ["peer-1"]
    assert app.transport_mgr.disconnected == ["peer-1"]
    archived = app.cfg.removed_peers.get("peer-1")
    assert archived is not None, "removed peer must be archived"
    assert archived.device_name == "Old Mac"
    assert archived.paired is True
    assert archived.notes == "the old one"
    assert archived.last_ip == "192.168.1.20"
    assert archived.removed_at > 0

    # The archive is the universal recovery path: restore brings it back.
    assert app._on_restore_remove("peer-1") is True
    assert "peer-1" in app.cfg.peers
    assert "peer-1" not in app.cfg.removed_peers
    assert app.cfg.peers["peer-1"].device_name == "Old Mac"
    assert app.pairing_mgr.removed == []


def test_on_remove_archives_discovered_only_peer(monkeypatch):
    """A bare discovered advertisement (never paired, no cfg.peers row) must
    still be archived — the '点移除后无法找回' bug was that it vanished with no
    trace.  Discovery rows are keyed by the HASHED mDNS id, so the archive
    lookup has to try that form too."""
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    hashed = Discovery._hash_device_id("peer-9")
    app._discovered_peers[hashed] = {
        "name": "Living-room PC",
        "address": "192.168.1.5",
        "port": 19990,
    }

    app._on_remove("peer-9")

    archived = app.cfg.removed_peers.get("peer-9")
    assert archived is not None
    assert archived.device_name == "Living-room PC"
    assert archived.paired is False
    assert archived.last_ip == "192.168.1.5"
    assert archived.removed_at > 0
    assert "peer-9" not in app.cfg.peers
    # …and the hashed discovery row is gone so it does not reappear live.
    assert hashed not in app._discovered_peers

    assert app._on_restore_remove("peer-9") is True
    assert app.cfg.peers["peer-9"].device_name == "Living-room PC"


def test_on_remove_archives_temp_chat_peer(monkeypatch):
    """A peer that only exists as a nearby-chat session (no discovery row, no
    cfg.peers entry) is archived under its session name."""
    from types import SimpleNamespace

    app = _remove_app(monkeypatch)
    app.chat_mgr = SimpleNamespace(
        get_sessions=lambda: [{"peer_id": "peer-7", "peer_name": "ChatBuddy"}],
    )

    app._on_remove("peer-7")

    archived = app.cfg.removed_peers.get("peer-7")
    assert archived is not None
    assert archived.device_name == "ChatBuddy"
    assert archived.paired is False
    assert archived.removed_at > 0


def test_save_cfg_and_peers_prunes_restored_archive_rows(monkeypatch):
    """Once a peer is known again (re-paired / restored), its removed_peers
    row must be dropped — otherwise it lingers in 已移除设备 next to its live
    card.  Discovered-only forgets archive under the hashed mDNS id, so the
    prune must match that form too."""
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    # A stale archive row for a now-known peer…
    from internal.config.config import PeerInfo

    app.cfg.removed_peers["peer-1"] = PeerInfo(
        device_id="peer-1", device_name="Old Mac", removed_at=time.time()
    )
    app.cfg.removed_peers["peer-2"] = PeerInfo(
        device_id="peer-2", device_name="Other", removed_at=time.time()
    )
    # …keyed by the real id in one case and the hashed mDNS id in the other.
    hashed = Discovery._hash_device_id("peer-3")
    app.cfg.removed_peers[hashed] = PeerInfo(
        device_id=hashed, device_name="Hashed", removed_at=time.time()
    )

    app.pairing_mgr = _StubPairingMgr()
    known = []
    for pid, name in (("peer-1", "Old Mac"), ("peer-3", "New Name")):
        from types import SimpleNamespace

        known.append(
            SimpleNamespace(device_id=pid, device_name=name, certificate_pem="", paired=True)
        )
    app.pairing_mgr.get_known_peers = lambda: known

    app._save_cfg_and_peers()

    assert "peer-1" not in app.cfg.removed_peers
    assert hashed not in app.cfg.removed_peers
    assert "peer-2" in app.cfg.removed_peers  # still gone → row stays
    assert "peer-3" in app.cfg.peers
    assert app.cfg.peers["peer-1"].device_name == "Old Mac"


# ════════════════════════════════════════════════════════════════════════
# 7 — restore must not auto-pair, and a rejected device must stay listed
# ════════════════════════════════════════════════════════════════════════


def test_restore_does_not_repair_device(monkeypatch):
    """Restoring from the archive must bring the device back UNPAIRED.

    _on_remove sends `pairing_unpair` to the peer, so the peer has already
    dropped its side of the trust.  Replaying the archived paired=True flag
    re-created one-sided trust the user never consented to ("设备移除再恢复会
    自动配对"), and the follow-up auto-connect fired an unsolicited pairing
    request at the peer.
    """
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    app.cfg.peers["peer-1"] = PeerInfo(
        device_id="peer-1",
        device_name="Old Mac",
        public_key_pem="pem",
        paired=True,
        notes="alias",
        last_ip="192.168.1.20",
        last_port=19990,
    )
    app._on_remove("peer-1")
    assert app.cfg.removed_peers["peer-1"].paired is True  # archive is faithful

    assert app._on_restore_remove("peer-1") is True

    restored = app.cfg.peers["peer-1"]
    assert restored.paired is False, "restore must not re-pair the device"
    assert app.pairing_mgr.restored_paired is False
    # No unsolicited connection: connecting while unpaired is just a pairing
    # request the user did not ask for.
    assert app.transport_mgr.connect_calls == []
    # …but the address survives so the card's Pair button has a target
    # (_on_connect falls back to cfg.peers[...].last_ip).
    assert restored.last_ip == "192.168.1.20"
    assert restored.last_port == 19990
    assert restored.notes == "alias"
    # Archived rows carry a removal timestamp; an active row must not.
    assert restored.removed_at == 0
    # The forget-time rejection is lifted so the peer can start the pairing
    # from its own side instead of being silently refused.
    assert app.transport_mgr.allowed == ["peer-1"]


def test_reject_keeps_device_in_list(monkeypatch):
    """Rejecting a pairing request must not erase the device.

    reject_pairing only clears the pending request and disconnect_peer only
    tears the socket down — neither drops the peer, which survives at
    paired=False/connected=False.  cfg.peers (what the snapshot reads) only
    syncs from pairing_mgr in _save_cfg_and_peers, and the reject path had no
    sync point, so the card vanished ("配对点拒绝设备就会消失在列表里").
    """
    from types import SimpleNamespace

    app = _remove_app(monkeypatch)
    app._send_pairing_msg = lambda *a, **k: None
    # The TLS handshake already registered the requesting peer as
    # known-but-unpaired (connection.py add_peer(..., paired=was_paired)).
    app.pairing_mgr.get_known_peers = lambda: [
        SimpleNamespace(
            device_id="peer-5",
            device_name="Someones Laptop",
            certificate_pem="pem",
            paired=False,
        )
    ]

    assert app._handle_web_device_action("reject", "peer-5") is True

    assert app.pairing_mgr.rejected == ["peer-5"]
    assert app.transport_mgr.rejected == ["peer-5"]
    # The peer is persisted as known-but-unpaired rather than dropped…
    assert "peer-5" in app.cfg.peers
    assert app.cfg.peers["peer-5"].paired is False
    # …and therefore survives in the device snapshot WHILE ON THE NETWORK.
    # Under the live-view policy a rejected peer that is not currently
    # advertising on mDNS is hidden (it will reappear when it broadcasts
    # again).  The test must provide a discovered map, or the snapshot
    # drops the row — matching the "no cached devices" rule.
    from internal.web.api import devices as devices_api

    result, status = devices_api.get_devices(
        app.cfg,
        lambda: [],
        get_discovered=lambda: {"peer-5": {"name": "Someones Laptop"}},
    )
    assert status == 200
    ids = {d["device_id"] for d in result["devices"]}
    assert "peer-5" in ids, "must be listed while on the network"
    # Without the discovered map the row is correctly hidden.
    result2, _ = devices_api.get_devices(app.cfg, lambda: [])
    ids2 = {d["device_id"] for d in result2["devices"]}
    assert "peer-5" not in ids2, "cached offline row must be hidden"


def test_get_devices_lists_known_unpaired_peer():
    """The device page is a LIVE view of the network, not a config dump.

    A paired peer is always listed (it is a trust relationship, not cache).
    An unpaired peer is listed only while it is actually on the network —
    connected, or advertising on mDNS right now.  A row that is none of those
    is stale cache and must not be shown ("不要显示缓存的设备").
    """
    from internal.config.config import Config, PeerInfo
    from internal.transport.discovery import Discovery
    from internal.web.api import devices as devices_api

    cfg = Config()
    cfg.peers = {
        "peer-1": PeerInfo(device_id="peer-1", device_name="Paired Box", paired=True),
        "peer-2": PeerInfo(device_id="peer-2", device_name="Live Unpaired", paired=False),
        "peer-3": PeerInfo(device_id="peer-3", device_name="Cached Ghost", paired=False),
    }
    cfg.removed_peers = {}

    # peer-2 is advertising: discovery keys it by the HASHED id, so the
    # snapshot has to hash each known id to recognise its own peer on the wire.
    result, status = devices_api.get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {
            Discovery._hash_device_id("peer-2"): {"name": "Live Unpaired"},
        },
    )
    assert status == 200
    by_id = {d["device_id"]: d for d in result["devices"]}

    assert "peer-1" in by_id, "a paired peer is always listed, online or not"
    assert by_id["peer-1"]["connected"] is False
    assert by_id["peer-1"]["known"] is True

    assert "peer-2" in by_id, "an unpaired peer on the network stays listed"
    assert by_id["peer-2"]["paired"] is False
    assert by_id["peer-2"]["connected"] is False
    assert by_id["peer-2"]["known"] is True
    # It is listed ONCE — as the known peer, not also as a bare sighting under
    # its hashed id.
    assert Discovery._hash_device_id("peer-2") not in by_id

    assert "peer-3" not in by_id, "cached offline unpaired row must be hidden"
    assert by_id[cfg.device_id]["known"] is True


def test_get_devices_lists_connected_unpaired_peer():
    """A live connection alone justifies a card (temp chat session): it is
    real-time network state, not cache."""
    from internal.config.config import Config, PeerInfo
    from internal.web.api import devices as devices_api

    cfg = Config()
    cfg.peers = {
        "peer-4": PeerInfo(device_id="peer-4", device_name="Chatting Phone", paired=False),
    }
    cfg.removed_peers = {}

    result, _ = devices_api.get_devices(cfg, lambda: ["peer-4"])
    by_id = {d["device_id"]: d for d in result["devices"]}
    assert "peer-4" in by_id
    assert by_id["peer-4"]["connected"] is True
    assert by_id["peer-4"]["encrypted"] is True


def test_get_devices_marks_discovered_peers_not_known():
    """A bare mDNS sighting is genuinely 'Discovered' — known=False keeps it
    distinguishable from the known-but-unpaired rows above."""
    from internal.config.config import Config
    from internal.web.api import devices as devices_api

    cfg = Config()
    cfg.peers = {}
    cfg.removed_peers = {}

    result, _ = devices_api.get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {"hash-abc": {"name": "Kitchen Pi"}},
    )
    by_id = {d["device_id"]: d for d in result["devices"]}
    assert by_id["hash-abc"]["known"] is False
    assert by_id["hash-abc"]["paired"] is False


def test_device_card_badges_known_unpaired_distinctly():
    """A restored/rejected device is offline and not on mDNS, so it must not
    wear the 🔍 Discovered badge."""
    src = _read_r19("components", "device-card.js")
    assert "isKnownUnpaired: function" in src
    assert "return this.isDiscovered && !!this.device.known;" in src
    # statusText prefers the known-unpaired label over the discovered fallback.
    assert src.index("device.not_paired") < src.index("device.discovered")


def test_not_paired_locale_key_in_both_locales():
    en, zh = _locales_r19()
    for loc, name in ((en, "en"), (zh, "zh-CN")):
        assert "device.not_paired" in loc, f"missing in {name}"
        assert loc["device.not_paired"].strip(), f"empty in {name}"


# ════════════════════════════════════════════════════════════════════════
# 8 — "Pair" with nowhere to dial must say WHY, not just "action failed"
# ════════════════════════════════════════════════════════════════════════


def test_on_connect_pushes_unreachable_when_no_address(monkeypatch):
    """A known-but-unpaired peer that is not on mDNS and has no saved address
    has nowhere to connect.  The route only answers {ok:false} — which the card
    renders as a bare "操作失败" — so _on_connect pushes an explanatory event,
    mirroring the connect_rejected push."""
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    pushed = []
    app._push_web = lambda *a, **k: pushed.append((a, k))
    app.cfg.peers["peer-9"] = PeerInfo(
        device_id="peer-9",
        device_name="Ghost PC",
        paired=False,
        last_ip="",
    )

    assert app._on_connect("peer-9") is False
    assert app.transport_mgr.connect_calls == [], "nothing to dial"
    assert pushed, "the failure must be explained over the WS"
    args, _ = pushed[-1]
    assert args[0] == "broadcast"
    assert args[1] == "connect_unreachable"
    assert args[2]["peer_id"] == "peer-9"
    # The name makes the toast readable ("找不到 Ghost PC"), not a hex id.
    assert args[2]["name"] == "Ghost PC"


def test_ws_handles_connect_unreachable():
    src = _read_r19("js", "ws.js")
    assert "case 'connect_unreachable':" in src
    assert "device.connect_unreachable" in src


def test_connect_unreachable_locale_key_in_both_locales():
    en, zh = _locales_r19()
    for loc, name in ((en, "en"), (zh, "zh-CN")):
        key = "device.connect_unreachable"
        assert key in loc, f"missing in {name}"
        # ws.js interpolates {name} — a template without it would drop the
        # only part of the message that identifies the device.
        assert "{name}" in loc[key], f"no {{name}} placeholder in {name}"


# ════════════════════════════════════════════════════════════════════════
# 9 — one physical device must never become two cards (hashed vs real id)
# ════════════════════════════════════════════════════════════════════════


def test_real_peer_id_resolves_hashed_form(monkeypatch):
    """A hashed mDNS id resolves back to the real device_id we already know.

    mDNS advertises hash(device_id), so a discovered-only card carries the
    hash.  Persisting that hash keys a row nothing can ever fold into the real
    one (the hash is one-way) — the same PC then shows twice, once under its
    broadcast name and once under the name from its certificate.
    """
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    assert hashed != real
    app.cfg.peers[real] = PeerInfo(device_id=real, device_name="USER-2024")

    assert app._real_peer_id(hashed) == real
    # A real id is returned untouched, and an unknown hash stays as-is (the
    # discovered-only case still has to be removable).
    assert app._real_peer_id(real) == real
    assert app._real_peer_id("ffffffffffff") == "ffffffffffff"


def test_merge_hashed_peer_rows_collapses_duplicate(monkeypatch):
    """The repair path folds an existing phantom row into the real one.

    Reproduces the observed config exactly: b190acbc219a (= hash of
    abe14d10c140) alongside abe14d10c140, both at the same address.
    """
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    assert hashed == "b190acbc219a", "hash derivation changed — update this test"

    app.cfg.peers[real] = PeerInfo(device_id=real, device_name="USER-20240325OS")
    app.cfg.peers[hashed] = PeerInfo(
        device_id=hashed,
        device_name="pc-zhao-b190",
        last_ip="192.168.31.251",
        last_port=19990,
        notes="zhao's PC",
        paired=True,
    )

    app._merge_hashed_peer_rows()

    assert hashed not in app.cfg.peers, "phantom row must be gone"
    assert real in app.cfg.peers
    # Address and note were possibly all the phantom knew — carried over.
    assert app.cfg.peers[real].last_ip == "192.168.31.251"
    assert app.cfg.peers[real].last_port == 19990
    assert app.cfg.peers[real].notes == "zhao's PC"
    # paired is NOT carried over: trust binds to the certificate CN (the real
    # id), so a paired flag on a hash row is non-functional and copying it
    # would grant a pairing the peer never completed.
    assert app.cfg.peers[real].paired is False


def test_merge_hashed_peer_rows_leaves_normal_config_alone(monkeypatch):
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    app.cfg.peers["aaaaaaaaaaaa"] = PeerInfo(
        device_id="aaaaaaaaaaaa", device_name="A", paired=True, notes="keep"
    )
    app.cfg.peers["bbbbbbbbbbbb"] = PeerInfo(
        device_id="bbbbbbbbbbbb", device_name="B", paired=False
    )

    app._merge_hashed_peer_rows()

    assert set(app.cfg.peers) == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}
    assert app.cfg.peers["aaaaaaaaaaaa"].notes == "keep"
    assert app.cfg.peers["aaaaaaaaaaaa"].paired is True


def test_restore_of_hashed_archive_keys_by_real_id(monkeypatch):
    """Restoring a discovered-only card must not materialise a hash-keyed peer.

    This was the mechanism that created the duplicate: _on_remove archived the
    hashed id, and _on_restore_remove fed it to pairing_mgr.restore_peer.
    """
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    # We know the real id (a handshake resolved it), only the archive is hashed.
    app.transport_mgr.get_resolved_hashes = lambda: {hashed: real}
    app.cfg.removed_peers[hashed] = PeerInfo(
        device_id=hashed,
        device_name="pc-zhao-b190",
        last_ip="192.168.31.251",
        last_port=19990,
        removed_at=1.0,
    )

    assert app._on_restore_remove(hashed) is True

    assert hashed not in app.cfg.peers, "must not re-create the phantom"
    assert real in app.cfg.peers
    assert app.cfg.peers[real].paired is False
    assert app.cfg.peers[real].last_ip == "192.168.31.251"
    assert app.transport_mgr.allowed == [real]


def test_restore_drops_archive_when_real_id_already_known(monkeypatch):
    """A hashed archive whose real id is already live is a duplicate — dropping
    it must not overwrite the live entry with the stale archive."""
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    app.transport_mgr.get_resolved_hashes = lambda: {hashed: real}
    app.cfg.peers[real] = PeerInfo(
        device_id=real, device_name="USER-20240325OS", paired=True, notes="the live one"
    )
    app.cfg.removed_peers[hashed] = PeerInfo(
        device_id=hashed, device_name="pc-zhao-b190", removed_at=1.0
    )

    assert app._on_restore_remove(hashed) is True

    assert hashed not in app.cfg.removed_peers
    assert hashed not in app.cfg.peers
    assert app.cfg.peers[real].paired is True, "live entry must survive"
    assert app.cfg.peers[real].notes == "the live one"


def test_remove_of_hashed_card_archives_under_real_id(monkeypatch):
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    app.transport_mgr.get_resolved_hashes = lambda: {hashed: real}
    app.cfg.peers[real] = PeerInfo(
        device_id=real,
        device_name="USER-20240325OS",
        paired=True,
        last_ip="192.168.31.251",
        last_port=19990,
    )
    app._send_pairing_msg = lambda *a, **k: None
    app._close_chat_for_peer = lambda pid: None

    app._on_remove(hashed)

    assert real not in app.cfg.peers
    assert hashed not in app.cfg.peers
    # Archived under the real id, with the real row's details (not a bare hash).
    assert real in app.cfg.removed_peers
    assert app.cfg.removed_peers[real].device_name == "USER-20240325OS"
    assert app.cfg.removed_peers[real].last_ip == "192.168.31.251"
    assert hashed not in app.cfg.removed_peers


def test_save_cfg_and_peers_preserves_address_and_note(monkeypatch):
    """cfg.peers is rebuilt from pairing_mgr, which tracks neither the note nor
    the address — dropping them blanked the last known address of every OFFLINE
    peer, killing the last_ip fallback the Pair button relies on."""
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)

    class _Known:
        device_id = "abe14d10c140"
        device_name = "USER-20240325OS"
        certificate_pem = "pem"
        paired = True

    app.pairing_mgr.get_known_peers = lambda: [_Known()]
    app.cfg.peers["abe14d10c140"] = PeerInfo(
        device_id="abe14d10c140",
        device_name="USER-20240325OS",
        paired=True,
        notes="desk PC",
        last_ip="192.168.31.251",
        last_port=19990,
    )

    app._save_cfg_and_peers()

    row = app.cfg.peers["abe14d10c140"]
    assert row.notes == "desk PC"
    assert row.last_ip == "192.168.31.251"
    assert row.last_port == 19990


def test_discovery_events_push_a_device_refresh():
    """The page is a live view, so mDNS arrive/leave must reach the web UI.

    Neither callback pushed anything before: an unpaired peer's card is only
    justified by its live presence, so a device appearing or vanishing was
    invisible until some unrelated broadcast happened to refresh the list.
    """
    import inspect

    import src.main as main_mod

    for fn in (main_mod.Application._on_peer_found, main_mod.Application._on_peer_lost):
        src = inspect.getsource(fn)
        assert '_push_web("broadcast_devices")' in src, (
            f"{fn.__name__} does not refresh the device page"
        )


# ════════════════════════════════════════════════════════════════════════
# 12 — chat / test-connection are only offered where a frame can actually go
# ════════════════════════════════════════════════════════════════════════
#
# A paired-but-LAN-offline device used to show 聊天 and 测试连接 buttons that
# could do nothing but fail: both need a live session or a relay path, and a
# plain LAN-paired peer that is simply switched off has neither.  The host now
# publishes `relay_reachable` per device so the card can hide them honestly
# instead of the frontend guessing from `paired`.


def _reach_cfg(**attrs):
    from internal.config.config import Config, PeerInfo

    cfg = Config()
    cfg.peers = {
        "peer-on": PeerInfo(device_id="peer-on", device_name="Desk", paired=True),
        "peer-off": PeerInfo(device_id="peer-off", device_name="Laptop", paired=True),
    }
    cfg.removed_peers = {}
    for k, v in attrs.items():
        setattr(cfg, k, v)
    return cfg


def _reach_rows(cfg, connected=()):
    from internal.web.api import devices as devices_api

    result, _ = devices_api.get_devices(cfg, lambda: list(connected))
    return {d["device_id"]: d for d in result["devices"]}


def test_paired_offline_peer_is_not_relay_reachable_by_default():
    # Internet sync off (the default) → no relay path to anything.
    rows = _reach_rows(_reach_cfg(), connected=["peer-on"])
    assert rows["peer-off"]["paired"] is True
    assert rows["peer-off"]["connected"] is False
    assert rows["peer-off"]["relay_reachable"] is False


def test_internet_paired_peer_is_relay_reachable_while_lan_offline():
    cfg = _reach_cfg(internet_sync_enabled=True, netpair_secrets={"peer-off": "ABCDEFG"})
    rows = _reach_rows(cfg)
    assert rows["peer-off"]["relay_reachable"] is True
    # The peer without a secret stays unreachable — this is per-peer, not a
    # blanket "internet sync is on" flag.
    assert rows["peer-on"]["relay_reachable"] is False


def test_lan_paired_peer_with_an_exchanged_relay_secret_is_reachable():
    cfg = _reach_cfg(internet_sync_enabled=True, peer_relay_secrets={"peer-off": "s3cret"})
    rows = _reach_rows(cfg)
    assert rows["peer-off"]["relay_reachable"] is True


def test_relay_secret_without_pairing_is_not_reachable():
    # Mirrors _relay_publish_to_peer: an unpaired peers row refuses the publish,
    # so the UI must not advertise a path through it.
    from internal.config.config import PeerInfo

    cfg = _reach_cfg(internet_sync_enabled=True, peer_relay_secrets={"peer-off": "s3cret"})
    cfg.peers["peer-off"] = PeerInfo(device_id="peer-off", device_name="Laptop", paired=False)
    rows = _reach_rows(cfg, connected=["peer-off"])  # keep the row listed
    assert rows["peer-off"]["relay_reachable"] is False


def test_internet_sync_off_overrides_every_stored_secret():
    # The secrets survive the toggle, but _relay_publish_to_peer refuses while
    # internet sync is off, so nothing is reachable through the relay.
    cfg = _reach_cfg(
        internet_sync_enabled=False,
        netpair_secrets={"peer-off": "ABCDEFG"},
        peer_relay_secrets={"peer-on": "s3cret"},
    )
    rows = _reach_rows(cfg)
    assert rows["peer-off"]["relay_reachable"] is False
    assert rows["peer-on"]["relay_reachable"] is False


def test_device_card_gates_chat_and_test_on_reachability():
    src = _read_r19("components", "device-card.js")
    assert "canReachNow: function" in src
    assert "return this.hasLiveSession || this.relayReachable;" in src
    assert "relay_reachable" in src
    # Both actions sit behind the single reachability gate — not behind
    # `isPaired`, which was true for an unreachable offline device.  Slice to
    # the next branch, not to the first `}` (that one closes an object literal).
    gate = src.split("if (this.canReachNow) {")[1].split("if (this.isConnected)")[0]
    assert "devices.chat_action" in gate
    assert "device.test_connection" in gate
    assert "isPaired" not in gate


def test_device_context_menu_gates_chat_on_reachability():
    src = _read_r19("components", "context-menu.js")
    assert "deviceCanReachNow: function" in src
    # The menu's 打开聊天 uses the same gate as the card's button.
    assert '!isLocal && deviceCanReachNow" class="context-menu__item"' in src


# 13 — one physical device is ONE row, even when its name changed
# ─────────────────────────────────────────────────────────────────
# Discovery only ever advertises the HASHED device id, while every known row is
# keyed by the real one.  The snapshot used to bridge that with two partial
# mechanisms: rev_resolved (only peers the transport manager resolved a hash
# for, i.e. ones we connected to since start-up) and a device-NAME heuristic.
# A known peer that had been renamed on either side matched neither, so it was
# listed twice — once as the known card and once as a bare "Discovered" card
# with a different device_id.  Known ids are now hashed into seen_ids directly.


def _dedupe_rows(cfg, discovered, **kw):
    from internal.web.api import devices as devices_api

    result, status = devices_api.get_devices(
        cfg, lambda: [], get_discovered=lambda: discovered, **kw
    )
    assert status == 200
    return {d["device_id"]: d for d in result["devices"]}


def _dedupe_cfg(**peers):
    from internal.config.config import Config, PeerInfo

    cfg = Config()
    cfg.peers = {
        pid: PeerInfo(device_id=pid, device_name=spec[0], paired=spec[1])
        for pid, spec in peers.items()
    }
    cfg.removed_peers = {}
    return cfg


def test_renamed_paired_peer_is_not_listed_twice():
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r1": ("Old Stored Name", True)})
    hashed = Discovery._hash_device_id("peer-r1")
    # The device advertises a name that shares nothing with the stored one, so
    # the name heuristic cannot dedupe it — only the hashed id can.
    rows = _dedupe_rows(cfg, {hashed: {"name": "Totally-Different"}})
    assert "peer-r1" in rows
    assert hashed not in rows, "same device listed twice (real id + hashed id)"
    assert len(rows) == 2, "local device + the peer, nothing else"


def test_renamed_unpaired_known_peer_is_not_listed_twice():
    # The unpaired case is the one the Discovered section shows: both rows
    # landed there (paired=False, connected=False) under different ids.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r2": ("Stored Name", False)})
    hashed = Discovery._hash_device_id("peer-r2")
    rows = _dedupe_rows(cfg, {hashed: {"name": "Renamed-Box"}})
    assert "peer-r2" in rows, "on-network unpaired peer keeps its known row"
    assert rows["peer-r2"]["known"] is True
    assert hashed not in rows
    discovered_section = [d for d in rows.values() if not d["paired"] and not d["connected"]]
    assert len(discovered_section) == 1


def test_dedupe_does_not_need_the_resolved_hash_map():
    # Before start-up handshakes there is nothing in get_resolved_hashes(), which
    # is exactly when the duplicate showed up.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r3": ("Box", True)})
    hashed = Discovery._hash_device_id("peer-r3")
    rows = _dedupe_rows(cfg, {hashed: {"name": "Renamed"}}, get_resolved_hashes=lambda: {})
    assert hashed not in rows and "peer-r3" in rows


def test_a_genuinely_different_device_still_gets_its_own_row():
    # The fix must not swallow real neighbours: an unknown hash is still a card.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r4": ("Mine", True)})
    other = Discovery._hash_device_id("some-other-device")
    rows = _dedupe_rows(
        cfg, {Discovery._hash_device_id("peer-r4"): {"name": "Mine"}, other: {"name": "Neighbour"}}
    )
    assert "peer-r4" in rows
    assert other in rows and rows[other]["known"] is False


def test_local_device_hash_is_deduped_too():
    # Discovery filters our own service by hash, but a mirrored/echoed sighting
    # of ourselves must never become a second card for this machine.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg()
    rows = _dedupe_rows(cfg, {Discovery._hash_device_id(cfg.device_id): {"name": "Me"}})
    assert list(rows) == [cfg.device_id]
