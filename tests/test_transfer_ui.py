"""Files page (transfer panel) audit round — web UI layer.

Static wiring assertions for the files-page fixes:

  1. Upload stall guard scales with file size instead of a flat 15s that
     killed slow-but-working multi-megabyte sends.
  2. "Send Folder" copy no longer promises zipping (files are sent
     individually — no zip is made).
  3. A periodic transfer reconcile runs while transfers are active, so an
     outgoing row that missed its initial WS push still appears/completes.
  4. cancel/pause/resume/retry/cancel-all honour the backend's `ok:false`
     instead of mutating state and toasting success.
  5. Transfer splicing lives in ONE place (store.reconcileTransfers); the
     app loader and the panel both delegate to it.
  6. showPhoneQr is single-sourced in the store; the panel delegates.
  7. Dead code removed: transfer.phone_msg + onboarding.err_qr locale keys,
     the old transfer-card__* CSS, and the unused /api/transfer/accept +
     /api/transfer/reject + POST /api/transfer routes.
  8. Internet (relay) pairs stay excluded from transfer targets — the
     web-upload forward path is LAN-only (documented on onlineDevices).
  9. en / zh-CN key sets identical; new keys present and non-empty in both.
  10. A node --check pass over every JS file this round touched.
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


# ── 1. Upload stall guard scales with size ──────────────────────────────


def test_upload_file_uses_size_scaled_stall_guard():
    src = _read("js", "api.js")
    chunk = src.split("uploadFile: function")[1]
    # The abort is armed with the scaled stall budget, not a flat 15s.
    assert "var stallMs = 15000 + Math.ceil(file.size / 1048576) * 5000;" in chunk
    assert "}, stallMs);" in chunk
    assert ", 15000);" not in chunk
    # The comment explains why a fixed deadline is wrong (server reads the
    # whole body before responding; the 128MB body cap bounds the guard).
    assert "reads the WHOLE body" in chunk
    assert "128MB" in chunk


# ── 2. Honest folder-send copy ─────────────────────────────────────────


def test_folder_send_hint_is_honest():
    en, zh = _locales()
    en_hint = en["transfer.send_folder_hint"]
    zh_hint = zh["transfer.send_folder_hint"]
    # No zip is created (files upload individually) — the copy must not
    # promise it in either language.
    assert "zip" not in en_hint.lower()
    assert "压缩" not in zh_hint  # 压缩
    # The panel renders the hint under the folder button.
    assert "t('transfer.send_folder_hint')" in _read("components", "transfer-panel.js")


# ── 3. Periodic transfer reconcile while active ─────────────────────────


def test_transfers_poll_runs_while_active():
    src = _read("js", "app.js")
    # Runs on a 5s interval, gated on focus/visibility AND active transfers,
    # with a single-in-flight guard.
    assert "this._transfersTimer = setInterval" in src
    assert "store.activeTransfers.length === 0" in src
    assert "_transfersPollInFlight" in src
    assert "store.refreshTransfers()" in src
    # Torn down on unmount so a remount never stacks intervals.
    assert "clearInterval(this._transfersTimer)" in src


# ── 4. Transfer controls honour ok:false ────────────────────────────────


def test_transfer_controls_honour_ok_false():
    src = _read("components", "transfer-panel.js")
    for method in ("cancelTransfer", "pauseTransfer", "resumeTransfer", "cancelAllTransfers"):
        assert method + ": function" in src
        chunk = src.split(method + ": function")[1]
        assert "res.ok === false" in chunk, method


def test_cancel_transfer_does_not_mutate_on_failure():
    src = _read("components", "transfer-panel.js")
    cancel = src.split("cancelTransfer: function")[1].split("pauseTransfer: function")[0]
    assert "if (!res || res.ok === false)" in cancel
    assert "transfer.cancel_failed" in cancel
    # The optimistic splice lives AFTER the ok guard.
    assert cancel.index("res.ok === false") < cancel.index("activeTransfers.splice")


def test_retry_uses_dedicated_failure_toast():
    # The API call + ok:false guard + failure toast live in the SHARED store
    # helper, because the history row's context menu ("重新发送") delegates to
    # the same code path as the row's Retry button — one source, no drift.
    store = _read("js", "store.js")
    retry = store.split("retryTransfer: function")[1]
    assert "res.ok === false" in retry
    assert "transfer.retry_failed" in retry
    # The panel keeps only the double-click guard and delegates the rest.
    panel = _read("components", "transfer-panel.js")
    assert "store.retryTransfer(id)" in panel
    assert "retryBusyId" in panel


# ── 5. Single source for transfer reconcile ─────────────────────────────


def test_transfer_reconcile_is_single_sourced():
    store = _read("js", "store.js")
    assert "reconcileTransfers: function (res) {" in store
    assert "refreshTransfers: function () {" in store
    assert "window.ClipsyncAPI.getTransfers()" in store
    # app.js loadTransfers delegates to the store.
    app = _read("js", "app.js")
    assert "return store.refreshTransfers();" in app
    # The panel's refresh is a thin delegate — the old duplicated splice
    # bodies (which fetched the API themselves) are gone from both files.
    panel = _read("components", "transfer-panel.js")
    assert "return this.store.refreshTransfers();" in panel
    # The old duplicated splice bodies are gone from both files (the hydrate
    # helper legitimately fetches the snapshot itself, but it never splices).
    assert "if (res && res.active) {" not in app
    assert "if (res && res.active) {" not in panel


# ── 6. showPhoneQr single-sourced in the store ──────────────────────────


def test_show_phone_qr_delegates_to_store():
    panel = _read("components", "transfer-panel.js")
    chunk = panel.split("showPhoneQr: function")[1]
    assert "self.store.showPhoneQr()" in chunk
    assert "_fetch('POST', '/api/show_qr'" not in chunk
    store = _read("js", "store.js")
    schunk = store.split("showPhoneQr: function")[1]
    # Returns a promise so the panel can manage its busy state around it.
    assert "return this.ensureOverview()" in schunk
    assert "transfer.phone_qr_failed" in schunk


# ── 7. Dead code removed ────────────────────────────────────────────────


def test_dead_transfer_locale_keys_removed_and_parity():
    en, zh = _locales()
    assert "transfer.phone_msg" not in en
    assert "transfer.phone_msg" not in zh
    assert "onboarding.err_qr" not in en
    assert "onboarding.err_qr" not in zh
    # New key present and non-empty in both.
    assert en["transfer.retry_failed"]
    assert zh["transfer.retry_failed"]
    assert set(en) == set(zh)


def test_dead_transfer_card_css_removed():
    html = _read("index.html")
    for dead in (
        "transfer-card__icon",
        "transfer-card__info",
        "transfer-card__name",
        "transfer-card__meta",
        "transfer-card__peer",
        "transfer-card__speed",
        "transfer-card__size",
        "transfer-card__progress-track",
        "transfer-card__progress-fill",
        "transfer-card__pct",
        "transfer-card--done",
    ):
        assert dead not in html, f"dead CSS {dead} still present"
    # The two classes the current panel actually uses survive.
    assert "transfer-card__header" in html
    assert "transfer-card__title" in html


def test_dead_transfer_routes_removed():
    routes = _read_root("internal", "web", "routes.py")
    assert '"/api/transfer/accept"' not in routes
    assert '"/api/transfer/reject"' not in routes
    # POST /api/transfer dispatch (post_transfer) is gone; the GET
    # /api/transfer snapshot endpoint (get_transfers) stays.
    assert "post_transfer(body, cfg, on_forward_file)" not in routes
    assert "get_transfers(on_get_transfers)" in routes
    assert "post_transfer" not in routes.split("internal.web.api.transfer")[1]
    transfer = _read_root("internal", "web", "api", "transfer.py")
    assert "def post_transfer" not in transfer


# ── 8. Internet peers excluded from transfer targets ────────────────────


def test_internet_peers_excluded_from_transfer_targets():
    store = _read("js", "store.js")
    # onlineDevices documents WHY relay pairs are excluded (the web-upload
    # forward path is LAN-only) rather than silently hiding them.
    assert "Internet (relay) pairs are intentionally NOT included" in store
    assert "LAN transport's connected set" in store


# ── 9. Peer-offline hint fires on any drop, not only mid-send ───────────


def test_peer_offline_hint_always_fires_on_drop():
    src = _read("components", "transfer-panel.js")
    watcher = src.split("this._unwatch = this.$watch('onlineDevices'")[1]
    watcher = watcher.split("{ immediate: true }")[0]
    assert "self.peerOffline = true;" in watcher
    # The old `if (self.sending)` gate is gone — the hint must fire even when
    # no send was in flight, so the auto-select guard below actually holds.
    assert "if (self.sending)" not in watcher
    assert "self.targetDevice = '';" in watcher


# ── 10. Locale parity + node --check ────────────────────────────────────


def test_locale_key_sets_identical():
    en, zh = _locales()
    assert set(en) == set(zh)


def test_node_check_touched_files():
    if not _has_node():
        pytest.skip("node not available")
    for rel in ("js/api.js", "js/store.js", "js/app.js", "components/transfer-panel.js"):
        subprocess.run(
            ["node", "--check", os.path.join(_STATIC, rel)],
            check=True,
            capture_output=True,
            text=True,
        )
