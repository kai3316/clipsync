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
    assert "settings_window.netpair_paired_toast" in src


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
        "v-for=\"dev in connectedSyncDevices\"",
        "v-for=\"dev in temporaryConnectedDevices\"",
        "v-for=\"dev in pairedOfflineDevices\"",
        "v-for=\"dev in discoveredDevices\"",
    ):
        assert vfor in src, vfor
    assert src.count("class=\"device-internet-wrap\"") == 4


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
    for key in ("devices.netpair_unpair_confirm",
                "devices.netpair_unpaired_toast",
                "devices.netpair_renamed_toast"):
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


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
            _SECTION_MARKERS, _SECTION_MARKERS[1:],
            zip(positions, positions[1:])):
        assert pa < pb, (
            f"section '{a_label}' (index {pa}) must render above "
            f"'{b_label}' (index {pb})"
        )


def test_section_markers_each_appear_once():
    src = _read_r19("components", "device-panel.js")
    for _label, marker in _SECTION_MARKERS:
        assert src.count(marker) == 1, (
            f"section marker {marker!r} must be unique in the template"
        )


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
        "v-for=\"dev in connectedSyncDevices\"",
        "v-for=\"dev in temporaryConnectedDevices\"",
        "v-for=\"dev in pairedOfflineDevices\"",
        "v-for=\"dev in discoveredDevices\"",
    ):
        assert vfor in src, vfor
    assert src.count("class=\"device-internet-wrap\"") == 4


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
    assert "return this.allRemoteDevices.filter(function (d) { return d.connected && !d.paired; });" in src
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
    for key in ("local", "pairing", "connected", "temporary", "paired",
                "discovered", "removed"):
        assert f"{key}: true," in src, f"section {key} must default open"


def test_api_restore_and_purge_wrappers():
    src = _read_r19("js", "api.js")
    assert "restoreDevice: function" in src
    assert "purgeDevice: function" in src
    assert "'/api/device/restore'" in src
    assert "'/api/device/purge'" in src


def test_new_removed_locale_keys_present_in_both():
    en, zh = _locales_r19()
    for key in ("device.temporary_connected", "device.restore", "device.purge",
                "devices.removed_title", "devices.removed_at",
                "devices.restore_confirm_msg", "devices.purge_confirm_msg"):
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
    assert not os.path.exists(os.path.join(
        _STATIC_r19, "components", "aiconfig-device-panel.js"))
    aiconf = _read_r19("components", "aiconfig-panel.js")
    assert "aiconfig-device-panel" not in aiconf
    assert "H.diffCounts" in aiconf          # peer diff badges
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
    for key in ("devices.this_device", "devices.netpair_title", "device.connected",
                "device.paired_offline", "device.discovered",
                "devices.pairing_requests"):
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_nul_bytes_in_device_panel():
    with open(os.path.join(_STATIC_r19, "components", "device-panel.js"), "rb") as f:
        data = f.read()
    assert b"\x00" not in data
