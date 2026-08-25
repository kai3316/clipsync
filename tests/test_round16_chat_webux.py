"""Round 16 — chat + device internet fusion (web UI layer).

Static wiring assertions for fusing internet-paired peers into the chat
panel's "start a chat" target selector, the session list, and the display
labels, plus the one-line privacy note on the Devices page:

  1. chat-panel.js renders an "Internet devices" group for internet-ONLY
     peers and dedups a dual-online peer (LAN + internet) so it appears
     once with a 🌐 badge instead of twice.
  2. Starting a chat from either group goes through the existing
     ClipsyncAPI.chatInvite(peer_id, name) flow (peer_id is the key).
  3. Internet targets render an online dot; offline ones carry the
     "the peer may be offline" hint but stay startable.
  4. Session rows / conversation header prefer the internet alias
     (alias || name || peer_id) and derive the online dot from the live
     internet-pair state as well as the backend's own flag.
  5. device-panel.js's internet-pairing section carries a single-line
     privacy note reusing settings_window.internet_sync_hint.
  6. en / zh-CN key sets identical and every round-16 key present and
     non-empty in both.
  7. A node --check pass over every JS file this round touched (when
     node is available).
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


# ── 1. Internet group + dedup in the chat target selector ───────────────


def test_chat_panel_has_internet_target_group():
    src = _read("components", "chat-panel.js")
    # The internet-ONLY list is a computed over store.internetPairPeers.
    assert "internetDeviceList: function" in src
    assert "this.store.internetPairPeers" in src
    # The group header + subheader are rendered in the device section.
    assert "chat.internet_devices_header" in src
    assert "chat-subheader" in src
    # Empty-state message only when BOTH lists are empty.
    assert "internetDeviceList.length === 0" in src


def test_dual_online_lan_row_carries_internet_badge():
    src = _read("components", "chat-panel.js")
    chunk = src.split("LAN targets")[1]
    assert "d.internet" in chunk
    assert "chat-badge--internet" in chunk
    assert "devices.netpair_also_internet" in chunk
    # The badge mirrors the Devices page's online/offline relay labels.
    assert "devices.netpair_online" in chunk
    assert "devices.netpair_offline" in chunk


def test_internet_group_dedups_lan_present_peers():
    src = _read("components", "chat-panel.js")
    # internetDeviceList skips a peer that is already a LAN chat target.
    chunk = src.split("internetDeviceList: function")[1].split("activeSession: function")[0]
    assert "lanIds[String(p.peer_id)]" in chunk
    assert "continue" in chunk
    # A peer whose paired flag is explicitly false is not a chat target.
    assert "p.paired === false" in chunk
    # deviceList augments a dual peer with the alias-priority name instead
    # of letting it appear twice (under LAN and under the internet group).
    dev = src.split("deviceList: function")[1].split("internetDeviceList: function")[0]
    assert "internet: true" in dev
    assert "name: netPeer.alias || d.name" in dev


# ── 2. Start chat goes through peer_id → chat_invite ────────────────────


def test_start_chat_uses_peer_id_through_existing_flow():
    src = _read("components", "chat-panel.js")
    assert "ClipsyncAPI.chatInvite(device.peer_id, device.name || device.peer_id)" in src
    # The internet-only Start button reuses the same startChat(peer) path.
    assert '@click="startChat(p)"' in src
    # Internet-targeted connecting toast uses the new key.
    assert "chat.internet_connecting" in src
    # Defensive: the existing LAN path is untouched (no new endpoint).
    assert "chatInvite(" in src


def test_internet_peers_fetched_on_mount_defensively():
    src = _read("components", "chat-panel.js")
    # created() fetches the pairing status so the group shows before the
    # Devices page is ever opened, guarded so an old backend is a no-op.
    assert "this.store.fetchInternetPairStatus" in src
    assert "if (this.store.fetchInternetPairStatus)" in src


# ── 3. Online dot + offline hint on internet targets ────────────────────


def test_internet_target_online_dot_and_offline_hint():
    src = _read("components", "chat-panel.js")
    chunk = src.split("Internet-only targets")[1]
    assert "chat-device-row__dot" in chunk
    assert "p.online" in chunk
    assert "chat-session-row__dot--on" in chunk
    assert "chat-session-row__dot--off" in chunk
    # Offline peers stay startable but carry the "may be offline" hint.
    assert "!p.online" in chunk
    assert "chat.internet_offline_hint" in chunk


# ── 4. Session title alias-priority + effective online state ────────────


def test_session_title_prefers_alias():
    src = _read("components", "chat-panel.js")
    assert "net.alias || net.name || fallback || peerId" in src
    assert "chatPeerName(s.peer_id, s.peer_name)" in src
    assert "chatPeerName(activeSession.peer_id, activeSession.peer_name)" in src
    assert "chatPeerName(activeSession.peer_id, activeSession.peer_name)" in src


def test_session_online_dot_uses_effective_state():
    src = _read("components", "chat-panel.js")
    assert "sessionOnline(s)" in src
    chunk = src.split("sessionOnline: function")[1].split("sessionStatusLabel: function")[0]
    assert "internetPeerFor(s.peer_id)" in chunk
    # Reachable when EITHER the LAN channel or the relay channel is up.
    assert "lanOn || netOn" in chunk
    # The session row shows a small 🌐 marker for internet-paired peers.
    assert "internetPeerFor(s.peer_id)" in src
    assert "chat-session-row__net" in src


# ── 5. Device page privacy note (round-16 optional, coordinator add-on) ──


def test_device_panel_privacy_note_reuses_internet_hint():
    src = _read("components", "device-panel.js")
    assert "netpair-privacy" in src
    assert "settings_window.internet_sync_hint" in src
    # The note sits right under the overview row, a single line.
    assert src.index("netpair-overview") < src.index("netpair-privacy")


def test_chat_internet_css_present():
    css = _read("index.html")
    for cls in (
        ".chat-badge--internet",
        ".chat-device-row__dot",
        ".chat-device-row__hint",
        ".chat-subheader",
        ".chat-session-row__net",
        ".netpair-privacy",
    ):
        assert cls in css, f"missing CSS rule: {cls}"


# ── 6. Locale parity ────────────────────────────────────────────────────

_NEW_KEYS = [
    "chat.internet_devices_header",
    "chat.internet_offline_hint",
    "chat.internet_connecting",
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


def test_new_round16_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 7. JS syntax (node --check over every file this round touched) ──────


_TOUCHED_JS = [
    ("components", "chat-panel.js"),
    ("components", "device-panel.js"),
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
