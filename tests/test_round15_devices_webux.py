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


def test_device_panel_three_step_guide():
    src = _read("components", "device-panel.js")
    # Empty state card + three guided steps (what + why).
    assert "netpair-empty" in src
    assert "devices.netpair_empty_title" in src
    for i in ("1", "2", "3"):
        assert f"devices.netpair_step{i}" in src, f"missing guide step {i}"
    assert "devices.netpair_step1_done" in src
    # Step 1 shows a ✓ when internet sync is already on, else a jump-to-settings link.
    assert "internetSyncEnabled" in src
    assert "devices.netpair_go_settings" in src
    assert "openInternetSyncSettings" in src


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
    assert "devices.netpair_code_valid_hint" in src
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
    # Connected / Paired Offline / Discovered each wrap their cards.
    for vfor in (
        "v-for=\"dev in onlineRemoteDevices\"",
        "v-for=\"dev in pairedOfflineDevices\"",
        "v-for=\"dev in discoveredDevices\"",
    ):
        assert vfor in src, vfor
    assert src.count("class=\"device-internet-wrap\"") == 3


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
