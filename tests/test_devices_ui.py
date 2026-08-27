"""Devices page audit round — web UI layer.

Static wiring assertions for the device-page fixes:

  1. A forgotten (removed) device still advertising on the LAN is not
     re-surfaced as a fresh "Discovered" device — it lives only in the
     Removed archive until restored or purged (backend dedup against
     cfg.removed_peers, not just the known/paired list).
  2. The generate/enter pairing-code controls only render while internet
     sync is on — with the relay off they'd be dead UI (entering a code is
     already rejected by the handler).  The sync-off message + go-to-settings
     CTA is the single prompt shown instead.
  3. An expired pairing request is labeled "Expired", not the misleading
     "Confirmed · waiting" the generic else-branch used to show.
  4. The "No devices found" empty state waits until the Removed archive is
     also empty, so it never overlaps the archive's rows.
  5. The stale header comment now matches the page's real section order.
  6. The dead `paired !== false` filter is gone — every internetPairPeer is
     a confirmed pair (backend status only lists confirmed pairs), so the
     paired-over-internet count is simply the list length.
  7. The "also paired over the internet" 🌐 badge is single-sourced in a
     netpair-badge component (was inlined three times).
  8. Pairing-code copy is single-sourced in _copyToClipboard — both the
     netpair code and the LAN pairing-request code delegate to it.
  9. The connection probe is single-sourced in store.testPeerConnection —
     both the LAN device card's test action and the netpair peer row's test
     delegate to it instead of duplicating the endpoint/toast body.
 10. The local device card no longer offers note editing (the backend
     silently drops a note whose peer_id is the local id, so saving would
     be a lie).
 11. Dead step-guide CSS removed from index.html.
 12. en / zh-CN key sets identical.
 13. Connect rejection is surfaced honestly — the connect action toasts
     "connecting…", never a fake "… successful", and the transport callback
     broadcasts a connect_rejected web event that turns into an error toast.
 14. A node --check pass over every JS file this round touched.
 15. The Mac OS icon is an apple, not a window: the osIcon `win` check came
     before `mac`/`darwin`, and "darwin" contains the substring "win" — so
     every macOS device rendered 🪟. OS labels are friendly brands (macOS,
     Windows, ...) via osLabel + backend friendly_platform_name, never the
     raw kernel name "Darwin".
 16. The device-card "移除" is a full forget matching the context menu's
     "忘记设备" (same confirm + toast, available for every non-local state);
     the old card-only device.remove_confirm_* locale keys are gone.
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


def _read_root(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def _has_node():
    return shutil.which("node") is not None


# ── 1. Removed devices are not re-surfaced as Discovered ────────────────


class _Peer:
    def __init__(self, device_id, name, paired=True):
        self.device_id = device_id
        self.device_name = name
        self.paired = paired
        self.os = ""
        self.notes = ""


class _DevCfg:
    device_id = "self1"
    device_name = "Self"

    def __init__(self, peers=None):
        self.peers = peers or {}
        self.removed_peers = {}


def _removed_peer(device_id, name, removed_at=100.0):
    p = _Peer(device_id, name)
    p.removed_at = removed_at
    p.last_ip = None
    p.last_port = None
    return p


def test_discovered_skips_removed_archive():
    from internal.web.api.devices import get_devices

    cfg = _DevCfg()
    cfg.removed_peers = {"gone": _removed_peer("gone", "Forgotten")}

    # The forgotten device is still broadcasting on the LAN — it must NOT be
    # listed under "Discovered" (that would double-list it against the Removed
    # archive), only in the archive for Restore/Purge.
    data, status = get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {"gone": {"name": "Forgotten"}},
    )
    assert status == 200
    ids = [d["device_id"] for d in data["devices"]]
    assert "gone" not in ids
    assert any(r["device_id"] == "gone" for r in data["removed"])


def test_discovered_not_removed_still_listed():
    from internal.web.api.devices import get_devices

    # Control: a genuinely new device still appears in Discovered — the
    # removed-archive dedup must not over-filter.
    cfg = _DevCfg()
    data, _ = get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {"fresh": {"name": "Fresh One"}},
    )
    ids = [d["device_id"] for d in data["devices"]]
    assert "fresh" in ids


# ── 2. Netpair generate/enter controls need internet sync on ─────────────


def test_netpair_actions_gated_on_sync_on():
    panel = _read("components", "device-panel.js")
    # With the relay off the generate/enter controls would be dead UI; the
    # sync-off message + go-to-settings CTA is the single prompt instead.
    assert '<div v-if="internetSyncEnabled" class="netpair-actions">' in panel


# ── 3. Expired pairing is labeled "Expired" ──────────────────────────────


def test_expired_pairing_shows_expired_not_confirmed_waiting():
    panel = _read("components", "device-panel.js")
    # The waiting badge must distinguish expired from confirmed_waiting; the
    # old generic else-branch mislabeled an expired request "Confirmed".
    assert "pr.status === \\'expired\\' ? \\'pairing.state.expired\\'" in panel
    assert "\\'pairing.state.confirmed_waiting\\'" in panel


# ── 4. Empty state waits for the Removed archive ─────────────────────────


def test_empty_state_waits_for_removed_archive():
    panel = _read("components", "device-panel.js")
    # "No devices found" must not overlap the Removed archive: the empty
    # state only renders when the archive is empty too.
    assert ("allRemoteDevices.length === 0 && pairingRequests.length === 0 "
            "&& removedDevices.length === 0") in panel


# ── 5. Header comment matches reality ────────────────────────────────────


def test_header_comment_matches_section_order():
    panel = _read("components", "device-panel.js")
    header = panel.split("/*")[1].split("*/")[0]
    for expected in (
        "This Device, Pairing Requests, Connected,",
        "Temporary, Paired Offline, Discovered, Removed",
        "pairing block at the bottom",
    ):
        assert expected in header
    # The old order listed Internet pairing directly under This Device.
    assert "This Device, Internet pairing, Connected, Offline" not in panel


# ── 6. Dead paired filter removed ────────────────────────────────────────


def test_netpair_paired_count_is_plain_length():
    panel = _read("components", "device-panel.js")
    cnt = panel.split("netpairPairedCount: function")[1].split("},")[0]
    assert "(this.store.internetPairPeers || []).length" in cnt
    # Every peer in the list is a confirmed pair (backend status only lists
    # confirmed pairs; the store normalizes paired to true for all of them),
    # so the old `p.paired !== false` filter was dead.
    assert "p.paired !== false" not in cnt


# ── 7. 🌐 badge single-sourced in a component ────────────────────────────


def test_netpair_badge_single_sourced_component():
    panel = _read("components", "device-panel.js")
    # The badge markup was inlined in three sections (Connected / Paired
    # Offline / Discovered); it is now one <netpair-badge> component.
    badge_tag = '<netpair-badge :peer="netpairPeerFor(dev.device_id)"></netpair-badge>'
    assert panel.count(badge_tag) == 3
    badge = panel.split("__CLIPSYNC_COMPONENTS__['netpair-badge']")[1]
    badge = badge.split("__CLIPSYNC_COMPONENTS__['device-panel']")[0]
    assert "netpair-card-badge--online" in badge
    assert "netpair-card-badge--offline" in badge
    # The inline duplicate (which re-derived .online in the markup) is gone.
    assert "netpairPeerFor(dev.device_id).online" not in panel


# ── 8. Copy actions single-sourced ───────────────────────────────────────


def test_copy_actions_delegate_to_shared_helper():
    panel = _read("components", "device-panel.js")
    # Both pairing-code copy paths share one clipboard helper.
    assert "_copyToClipboard: function (text)" in panel
    assert panel.count("this._copyToClipboard(code);") == 2
    # The duplicated inline clipboard bodies are gone — exactly one remains.
    assert panel.count("navigator.clipboard.writeText(text).then(done).catch(done)") == 1


# ── 9. Connection probe single-sourced in the store ──────────────────────


def test_probe_single_sourced_in_store():
    store = _read("js", "store.js")
    tp = store.split("testPeerConnection: function (peerId)")[1]
    tp = tp.split("_testErrorReason: function")[0]
    assert "window.ClipsyncAPI.testDeviceConnection(peerId)" in tp
    assert "device.test_channel_ok" in tp
    assert "device.test_channel_fail" in tp


def test_both_call_sites_delegate_to_store_probe():
    card = _read("components", "device-card.js")
    panel = _read("components", "device-panel.js")
    assert "self.store.testPeerConnection(peerId)" in card
    assert "self.store.testPeerConnection(peer.peer_id)" in panel
    # The duplicated endpoint/toast bodies are gone from both components.
    assert "ClipsyncAPI.testDeviceConnection" not in card
    assert "ClipsyncAPI.testDeviceConnection" not in panel
    assert "_deviceTestError" not in card
    assert "_netpairTestError" not in panel


# ── 10. Local device card offers no note editing ─────────────────────────


def test_local_device_note_editor_hidden():
    card = _read("components", "device-card.js")
    # Notes are cross-device memos keyed to a peer; the backend silently
    # drops a note for the local id (it is never in cfg.peers), so the local
    # card must not offer a "save" that never persists.
    assert "!isLocal && !editingNote" in card
    assert "!isLocal && editingNote" in card


# ── 11. Dead netpair step-guide CSS removed ──────────────────────────────


def test_dead_netpair_css_removed():
    html = _read("index.html")
    for dead in (".netpair-empty", ".netpair-step", ".netpair-generate",
                 ".netpair-hint", ".netpair-enter {"):
        assert dead not in html
    # The live controls' styles survive.
    assert ".netpair-actions {" in html
    assert ".netpair-enter__row {" in html


# ── 12. Locale parity ────────────────────────────────────────────────────


def test_locale_key_sets_identical():
    en, zh = _locales()
    assert set(en) == set(zh)


# ── 13. Connect rejection is surfaced honestly (no fake "success") ─────────


def test_connect_toast_is_connecting_not_success():
    card = _read("components", "device-card.js")
    # {ok:true} from connect only means the handshake was *initiated*, so the
    # immediate toast must be "connecting…", never the generic "… successful"
    # that made a rejected connect look like a silent no-op.
    assert "runAction(ClipsyncAPI.connectDevice(peerId), null, self.t('device.connect_started'))" in card
    # The generic success toast stays for the actions that DO complete
    # synchronously (unpair/forget/disconnect).
    assert "self.t('device.action_success'" in card


def test_ws_handles_connect_rejected_broadcast():
    ws = _read("js", "ws.js")
    assert "case 'connect_rejected':" in ws
    assert "store.t('device.connect_rejected', { name: data.name || '' })" in ws
    assert "'error'" in ws.split("case 'connect_rejected':")[1].split("break;")[0]


def test_backend_wires_and_broadcasts_connect_rejected():
    # The transport callback is wired, and its handler broadcasts a web event.
    main = _read_root("src", "main.py")
    assert "set_on_connect_rejected(self._on_connect_rejected)" in main
    assert '"connect_rejected"' in main
    conn = _read_root("internal", "transport", "connection.py")
    assert "set_on_connect_rejected" in conn
    assert "self._notify_connect_rejected(peer_name, peer_id)" in conn


def test_connect_rejected_locale_keys_present():
    en, zh = _locales()
    for key in ("device.connect_started", "device.connect_rejected"):
        assert key in en and key in zh
    assert en["device.connect_rejected"]
    assert zh["device.connect_rejected"]


# ── 14. Mac icon + friendly OS labels ────────────────────────────────────


def test_os_icon_checks_mac_before_win():
    card = _read("components", "device-card.js")
    # "darwin" contains the substring "win" (d-a-r-w-i-n), so the Mac branch
    # must precede the Windows branch or every macOS device renders 🪟.
    icon = card.split("osIcon: function () {")[1].split("osLabel: function () {")[0]
    assert icon.index("os.indexOf('mac')") < icon.index("os.indexOf('win')")
    assert icon.index("os.indexOf('darwin')") < icon.index("os.indexOf('win')")


def test_os_label_maps_darwin_to_macos_and_is_rendered():
    card = _read("components", "device-card.js")
    label = card.split("osLabel: function () {")[1].split("},")[0]
    for brand in ("'macOS'", "'Windows'", "'Linux'", "'Android'", "'iOS'"):
        assert ("return " + brand) in label
    # The card renders the friendly label, not the raw platform string.
    assert "device-card__os\">{{ osLabel }}</span>" in card


def test_local_device_os_is_friendly_name():
    from internal.web.api.devices import get_devices

    cfg = _DevCfg()
    data, status = get_devices(cfg, lambda: [])
    assert status == 200
    local = data["devices"][0]
    # platform.system() ("Darwin") is mapped to the brand ("macOS") so the
    # backend value is already display-ready for the card icon + label.
    assert local["os"] in ("macOS", "Windows", "Linux", "Android", "iOS")
    assert local["os"] != "Darwin"


def test_friendly_platform_name_maps_known_systems():
    from internal.platform import friendly_platform_name

    assert friendly_platform_name("Darwin") == "macOS"
    assert friendly_platform_name("darwin") == "macOS"
    assert friendly_platform_name("Mac OS X") == "macOS"
    assert friendly_platform_name("Windows") == "Windows"
    assert friendly_platform_name("Linux") == "Linux"
    assert friendly_platform_name("Android") == "Android"
    assert friendly_platform_name("iOS") == "iOS"
    # Unknown systems fall through to the raw kernel name.
    assert friendly_platform_name("FreeBSD") == "freebsd"


# ── 15. Card 移除 == context-menu 忘记设备 ───────────────────────────────


def test_card_forget_available_for_every_nonlocal_device():
    card = _read("components", "device-card.js")
    acts = card.split("actions: function () {")[1].split("template:")[0]
    # Forget is offered unconditionally for non-local devices (the method
    # returns early for the local one). The old gate hid it on connected /
    # temporary devices while the context menu's 忘记设备 stayed available
    # for them — now both paths agree.
    assert "acts.push({ key: 'forget', label: this.t('device.remove')" in acts
    assert "!this.isConnected && !this.isTemporary" not in acts


def test_card_forget_matches_context_menu_confirm_and_toast():
    card = _read("components", "device-card.js")
    forget = card.split("if (key === 'forget') {")[1].split("if (key === 'test') {")[0]
    assert "self.t('devices.forget_title')" in forget
    assert "self.t('devices.forget_message', {name: deviceName})" in forget
    assert "self.t('context.device_forgotten')" in forget
    # The old card-only confirm keys are gone from the action path.
    assert "device.remove_confirm_title" not in forget
    assert "device.remove_confirm_msg" not in forget


def test_old_remove_confirm_locale_keys_removed():
    en, zh = _locales()
    for key in ("device.remove_confirm_title", "device.remove_confirm_msg"):
        assert key not in en
        assert key not in zh
    # The unified forget keys (shared with the context menu) are live.
    for key in ("devices.forget_title", "devices.forget_message",
                "context.device_forgotten"):
        assert key in en and key in zh


# ── 16. node --check ─────────────────────────────────────────────────────


def test_node_check_touched_files():
    if not _has_node():
        pytest.skip("node not available")
    for rel in ("js/store.js", "js/ws.js", "components/device-panel.js", "components/device-card.js"):
        subprocess.run(
            ["node", "--check", os.path.join(_STATIC, rel)],
            check=True,
            capture_output=True,
            text=True,
        )
