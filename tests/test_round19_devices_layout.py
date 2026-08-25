"""Round 19 — device page section reorder (web).

Static wiring assertions for the Devices-tab layout change:
  1. device-panel.js renders the sections top→bottom in the agreed order:
     This Device → Internet pairing → Connected → Paired Offline →
     Discovered → Pairing Requests.
  2. The This Device card sits at the very top (above the internet-pairing
     section and outside the list-loading gate), while the empty / load-failed
     states stay at the bottom of the LAN list.
  3. Round 15/16/17 features are intact: devWithAlias / netpairPeerFor / the
     three device-internet wraps, the local-internet badge, and the round-18
     AI-config device sub-panel remains mounted at the very bottom.
  4. No new locale keys were added — the en / zh-CN key sets stay identical.
  5. A node --check pass over device-panel.js (when node is available).
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# (label, unique template marker) for each device section, top→bottom as the
# round-19 target order dictates. Each marker must appear exactly once in the
# template (checked below) so its index is a reliable ordering signal.
_SECTION_MARKERS = [
    ("this_device", "devices.this_device"),
    ("netpair", "devices.netpair_title"),
    ("connected", "device.connected"),
    ("paired_offline", "device.paired_offline"),
    ("discovered", "device.discovered"),
    ("pairing_requests", "devices.pairing_requests"),
]


# ── 1. Section order ────────────────────────────────────────────────────


def test_device_sections_in_agreed_order():
    src = _read("components", "device-panel.js")
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
    src = _read("components", "device-panel.js")
    for _label, marker in _SECTION_MARKERS:
        assert src.count(marker) == 1, (
            f"section marker {marker!r} must be unique in the template"
        )


# ── 2. This Device on top / loading gate / terminal states ──────────────


def test_this_device_and_netpair_render_above_loading_gate():
    src = _read("components", "device-panel.js")
    # This Device is the first section — it must appear before the internet
    # section AND before the LAN-list loading skeleton.
    assert src.index("devices.this_device") < src.index("device-panel__loading")
    assert src.index("devices.netpair_title") < src.index("device-panel__loading")
    # The loading skeleton gates only the LAN sections below it.
    assert src.index("device-panel__loading") < src.index("devices.pairing_requests")


def test_empty_and_load_failed_states_stay_at_bottom():
    src = _read("components", "device-panel.js")
    # Terminal states render after the last real section (Pairing Requests).
    assert src.index("devices.load_failed") > src.index("devices.pairing_requests")
    assert src.index("devices.no_devices_found") > src.index("devices.pairing_requests")
    # Empty state chains off the load-failed state (v-if → v-else-if).
    failed = src.index("loadFailed || store.devicesLoadFailed")
    empty = src.index('v-else-if="allRemoteDevices.length === 0')
    assert failed < empty


# ── 3. Round 15/16/17/18 features intact ────────────────────────────────


def test_alias_and_netpair_helpers_still_present():
    src = _read("components", "device-panel.js")
    assert "devWithAlias: function" in src
    assert "netpairPeerFor: function" in src
    assert "String(peers[i].peer_id) === String(deviceId)" in src


def test_lan_cards_still_wrapped_in_all_three_sections():
    src = _read("components", "device-panel.js")
    for vfor in (
        "v-for=\"dev in onlineRemoteDevices\"",
        "v-for=\"dev in pairedOfflineDevices\"",
        "v-for=\"dev in discoveredDevices\"",
    ):
        assert vfor in src, vfor
    assert src.count("class=\"device-internet-wrap\"") == 3


def test_local_device_internet_badge_still_wired():
    src = _read("components", "device-panel.js")
    assert "localInternetOnline" in src
    assert "netpair-local-badge" in src
    # The badge must live on the (now top) This Device card.
    assert src.index("netpair-local-badge") > src.index("devices.this_device")
    assert src.index("netpair-local-badge") < src.index("devices.netpair_title")


def test_each_lan_section_keeps_empty_skip_guard():
    src = _read("components", "device-panel.js")
    for guard in (
        "onlineRemoteDevices.length > 0",
        "pairedOfflineDevices.length > 0",
        "discoveredDevices.length > 0",
        "pairingRequests.length > 0",
    ):
        assert guard in src, guard


def test_ai_config_sub_panel_stays_at_bottom():
    src = _read("components", "device-panel.js")
    # The round-18 relocated AI-config device inventories panel remains the
    # last element of the device panel.
    assert src.index("aiconfig-device-panel") > src.index("devices.pairing_requests")


# ── 4. Locale parity (round 19 adds no new copy) ────────────────────────


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_still_identical():
    en, zh = _locales()
    assert set(en) == set(zh)


def test_reordered_section_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
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
    path = os.path.join(_STATIC, "components", "device-panel.js")
    proc = subprocess.run(
        [node, "--check", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_nul_bytes_in_device_panel():
    with open(os.path.join(_STATIC, "components", "device-panel.js"), "rb") as f:
        data = f.read()
    assert b"\x00" not in data
