"""Overview page audit round — web UI layer.

Static wiring assertions for the overview-page fixes:

  1. toggleSync mirrors the server's `_clear_pause_state()` — an explicit
     sync toggle clears a pending timed pause locally, so the "⏸ N min left"
     row never lingers under a fresh toggle.
  2. The overview's connected count is paired-only, matching the frontend's
     connectedCount and keeping the ring's connected/paired-offline math
     consistent (chat-only / mid-pairing connections excluded).
  3. The 'store.overview' watch is immediate, so the animated stat counters
     seed on mount / tab re-entry instead of sitting at 0 until the first
     polling push arrives.
  4. Dead overview state removed: recentActivity / transferBytes /
     overview.loading (frontend) and recent_activity / transfer_bytes
     (backend) are gone from _get_overview_data.
  5. mobileUrl is protocol-aware (http vs https) and copyUrl delegates to
     it — one source for the phone-connect link.
  6. The Transfers stat card leads with the ACTIVE transfer count and shows
     the completed count as the subtitle (desktop-dashboard parity), not the
     reverse.
  7. Dead overview.* web-locale keys removed from both languages; the key
     sets stay identical.
  8. beforeUnmount cancels in-flight counter animation frames.
  9. A node --check pass over every JS file this round touched.
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


# ── 1. toggleSync clears a pending timed pause (server parity) ──────────


def test_toggle_sync_clears_timed_pause():
    src = _read("components", "overview-panel.js")
    toggle = src.split("toggleSync: function")[1].split("pauseSync: function")[0]
    # The local cache mirrors the server's _clear_pause_state() so the
    # "⏸ N min left" row disappears immediately after an explicit toggle.
    assert "self.store.mergeSettings({ timed_pause_until: 0 });" in toggle
    assert "self.nowTick = Date.now();" in toggle
    # The clear happens in the SUCCESS path, after the ok:false guard (a
    # failed request must not flip the toggle state or clear the pause).
    assert toggle.index("res.ok === false") < toggle.index("timed_pause_until: 0")


# ── 2. Overview connected count is paired-only ──────────────────────────


def test_overview_connected_count_filters_to_paired():
    main = _read_root("src", "main.py")
    ov = main.split("def _get_overview_data")[1].split("def _handle_web_device_action")[0]
    # The raw connection list is filtered down to paired peer ids so the
    # overview's connected_count agrees with the frontend's paired-only
    # connectedCount (chat-only / mid-pairing connections excluded).
    assert 'paired_ids = {getattr(p, "device_id", "") for p in paired}' in ov
    assert "connected = [pid for pid in connected if pid in paired_ids]" in ov
    assert '"connected_count": len(connected),' in ov
    # The connected-device chips come from the same filtered set.
    assert "if pid in paired_ids" in ov


def test_frontend_connected_count_documents_paired_agreement():
    store = _read("js", "store.js")
    cc = store.split("connectedCount: computed")[1]
    assert "agreeing with the web" in cc
    assert "backend filters to paired" in cc


# ── 3. Overview watch is immediate ──────────────────────────────────────


def test_overview_watch_seeds_stats_immediately():
    src = _read("components", "overview-panel.js")
    watch = src.split("'store.overview': {")[1].split("handler: function (o) {")[0]
    assert "immediate: true" in watch


# ── 4. Dead overview state removed ──────────────────────────────────────


def test_dead_frontend_overview_state_removed():
    store = _read("js", "store.js")
    ov = store.split("overview: {")[1].split("pairingRequests: [")[0]
    assert "recentActivity" not in ov
    assert "loading" not in ov
    # The writes in fetchOverview are gone too.
    assert "self.overview.recentActivity" not in store
    assert "self.overview.transferBytes" not in store
    assert "this.overview.loading" not in store


def test_dead_backend_overview_fields_removed():
    main = _read_root("src", "main.py")
    ov = main.split("def _get_overview_data")[1].split("def _handle_web_device_action")[0]
    assert "transfer_bytes" not in ov
    assert "recent_activity" not in ov


# ── 5. mobileUrl protocol-aware; copyUrl single-sourced ─────────────────


def test_mobile_url_follows_serving_scheme():
    store = _read("js", "store.js")
    mu = store.split("mobileUrl: computed")[1]
    assert "window.location.protocol === 'https:'" in mu
    assert "? 'https' : 'http'" in mu


def test_copy_url_delegates_to_store_mobile_url():
    src = _read("components", "overview-panel.js")
    cu = src.split("copyUrl: function")[1].split("_fallbackCopy: function")[0]
    assert "var url = this.store.mobileUrl;" in cu
    # The old duplicated protocol-choosing builder is gone from the panel.
    assert "window.location.protocol" not in cu


# ── 6. Transfers card leads with the active count (desktop parity) ──────


def test_transfers_card_leads_with_active_count():
    src = _read("components", "overview-panel.js")
    tpl = src.split("template:")[1]
    # Primary value = active transfer count (matching the desktop dashboard's
    # Transfers stat), completed moves to the subtitle.
    assert "stats.transfers || 0 }}</span>" in tpl
    assert "stats.completed || 0 }} {{ t(" in tpl
    assert "overview.completed" in tpl
    # And the active count is the PRIMARY metric (rendered before completed).
    assert tpl.index("stats.transfers || 0") < tpl.index("stats.completed || 0")


# ── 7. Dead overview locale keys removed; parity intact ─────────────────


def test_dead_overview_locale_keys_removed():
    en, zh = _locales()
    for dead in (
        "overview.activity",
        "overview.browsing",
        "overview.connection",
        "overview.copied",
        "overview.discovery_status",
        "overview.items",
        "overview.none_active",
        "overview.scan_hint",
        "overview.send_hint",
        "overview.settings",
        "overview.this_device_label",
        "overview.title",
        "overview.visibility_status",
        "overview.active",
    ):
        assert dead not in en, f"{dead} still present in en.json"
        assert dead not in zh, f"{dead} still present in zh-CN.json"
    # The completed key the Transfers card now shows survives and is non-empty.
    assert en["overview.completed"]
    assert zh["overview.completed"]


def test_overview_locale_key_sets_identical():
    en, zh = _locales()
    assert set(en) == set(zh)


# ── 8. beforeUnmount cancels animation frames ───────────────────────────


def test_before_unmount_cancels_animation_frames():
    src = _read("components", "overview-panel.js")
    unmount = src.split("beforeUnmount: function")[1].split("methods: {")[0]
    assert "cancelAnimationFrame(anims[key])" in unmount
    assert "this._animTimers = null;" in unmount


# ── 9. Locale parity + node --check ─────────────────────────────────────


def test_node_check_touched_files():
    if not _has_node():
        pytest.skip("node not available")
    for rel in ("js/store.js", "components/overview-panel.js"):
        subprocess.run(
            ["node", "--check", os.path.join(_STATIC, rel)],
            check=True,
            capture_output=True,
            text=True,
        )
