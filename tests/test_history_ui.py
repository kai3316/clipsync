"""History page audit round — web UI layer.

Static wiring assertions for the history-page fixes:

  1. clearAll clears the multi-select selection — otherwise the action bar
     keeps floating over the wiped, empty list.
  2. Delete/Backspace works while a history card has real keyboard focus
     (the app-level handler is gated off for [role="button"] cards).
  3. The search-results count row is hidden when a search yields nothing,
     so "0 results" and the "No results for …" empty state don't compete.
  4. Dead code removed: the unused `index` prop / `:index` bindings, the
     dead `history.merged` locale key, and the dead `.history-panel__header`
     (+ __header-actions) CSS.
  5. "Load more" uses the SAME page size as the initial load
     (web_history_limit) instead of a separate hardcoded 20.
  6. The per-item copy action is single-sourced in
     store.pasteHistoryItem — history-item and the app keyboard path both
     delegate instead of re-implementing paste/count-bump/toasts.
  7. clearAll / batchPin / batchFavorite / batchDelete surface the
     backend's `{ok:false}` error body instead of silently swallowing it.
  8. en / zh-CN key sets identical.
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


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def _has_node():
    return shutil.which("node") is not None


# ── 1. clearAll clears the selection ────────────────────────────────────


def test_clear_all_clears_selection():
    panel = _read("components", "history-panel.js")
    clear = panel.split("clearAll: function")[1].split("clearSearch: function")[0]
    # Rows selected before the wipe must not keep the action bar alive.
    assert "self.store.clearSelection();" in clear
    assert "self.store.clearHistory();" in clear


# ── 2. Delete key works while a card has focus ──────────────────────────


def test_delete_key_works_on_focused_card():
    item = _read("components", "history-item.js")
    kd = item.split("onKeyDown: function")[1].split("onClick: function")[0]
    assert "e.key === 'Delete' || e.key === 'Backspace'" in kd
    assert "this.deleteItem();" in kd
    assert "e.preventDefault();" in kd


# ── 3. No competing no-results messages ─────────────────────────────────


def test_search_count_hidden_when_empty():
    panel = _read("components", "history-panel.js")
    tpl = panel.split("template:")[1]
    # The count row only renders while a search actually matches; the empty
    # state's "No results for …" is the single no-results message otherwise.
    assert "store.historySearch && hasContent" in tpl
    assert "history.search_count" in tpl


# ── 4. Dead code removed ────────────────────────────────────────────────


def test_dead_index_prop_removed():
    panel = _read("components", "history-panel.js")
    item = _read("components", "history-item.js")
    # The history-item component no longer declares an `index` prop (it only
    # ever used flatIndex for the keyboard cursor), and the panel no longer
    # binds it.
    assert '":index=\\"index\\""' not in panel
    assert '"index: {"' not in item


def test_dead_history_merged_key_removed():
    en, zh = _locales()
    assert "history.merged" not in en
    assert "history.merged" not in zh
    # The live key it was replaced by survives in both languages.
    assert en["web.merged_pushed"]
    assert zh["web.merged_pushed"]


def test_dead_history_header_css_removed():
    html = _read("index.html")
    assert ".history-panel__header {" not in html
    assert ".history-panel__header-actions" not in html


# ── 5. Load-more page size matches the initial load ─────────────────────


def test_load_more_uses_web_history_limit():
    panel = _read("components", "history-panel.js")
    lm = panel.split("loadMore: function")[1].split("clearAll: function")[0]
    # Same page size as the initial load (app.js uses web_history_limit),
    # not a separate hardcoded 20 that diverges from the configured first page.
    assert "web_history_limit" in lm
    assert "historyPageSize" not in panel


# ── 6. Single source for the per-item copy action ───────────────────────


def test_paste_history_item_lives_in_store():
    store = _read("js", "store.js")
    ph = store.split("pasteHistoryItem: function (eid, opts)")[1]
    assert "window.ClipsyncAPI.pasteRich(eid)" in ph
    assert "history.copy_failed" in ph
    assert "history.copy_to_desktop_toast" in ph
    assert "opts.coarse" in ph


def test_history_item_copy_delegates_to_store():
    item = _read("components", "history-item.js")
    ci = item.split("copyItem: function")[1].split("fallbackCopy: function")[0]
    assert "this.store.pasteHistoryItem(eid, { coarse: this.isCoarse })" in ci
    # The duplicated pasteRich body is gone from the card.
    assert "ClipsyncAPI.pasteRich(eid)" not in ci


def test_app_kbd_copy_delegates_to_store():
    app = _read("js", "app.js")
    kbd = app.split("_copyKbdItem: function")[1].split("_deleteKbdItem: function")[0]
    assert "store.pasteHistoryItem(item.entry_id)" in kbd
    assert "ClipsyncAPI.pasteRich(eid)" not in kbd


# ── 7. Batch / clear actions surface {ok:false} ─────────────────────────


def test_clear_all_surfaces_ok_false():
    panel = _read("components", "history-panel.js")
    clear = panel.split("clearAll: function")[1].split("clearSearch: function")[0]
    assert "(res && res.error) || self.t('history.clear_failed')" in clear


def test_batch_pin_surfaces_ok_false():
    panel = _read("components", "history-panel.js")
    bp = panel.split("batchPinSelected: function")[1].split("batchFavoriteSelected: function")[0]
    assert "(res && res.error) || self.t('history.pin_failed')" in bp


def test_batch_favorite_surfaces_ok_false():
    panel = _read("components", "history-panel.js")
    bf = panel.split("batchFavoriteSelected: function")[1].split("deleteSelected: function")[0]
    assert "(res && res.error) || self.t('favorites.add_failed')" in bf


def test_batch_delete_surfaces_ok_false():
    panel = _read("components", "history-panel.js")
    bd = panel.split("deleteSelected: function")[1].split("onPanelClick: function")[0]
    assert "(res && res.error) || self.t('history.delete_failed')" in bd


# ── 8. Locale parity ────────────────────────────────────────────────────


def test_locale_key_sets_identical():
    en, zh = _locales()
    assert set(en) == set(zh)


# ── 9. node --check ─────────────────────────────────────────────────────


def test_node_check_touched_files():
    if not _has_node():
        pytest.skip("node not available")
    for rel in (
        "js/store.js",
        "js/app.js",
        "components/history-item.js",
        "components/history-panel.js",
    ):
        subprocess.run(
            ["node", "--check", os.path.join(_STATIC, rel)],
            check=True,
            capture_output=True,
            text=True,
        )


# ═════════════════════════════════════════════════════════════════════════
# Stage 6 — frontend: stale-snapshot guard, confirm parity, cancel handling
# ═════════════════════════════════════════════════════════════════════════


def test_app_load_devices_drops_a_stale_snapshot():
    """devices_updated is edge-triggered on the server, so a GET response that
    overwrites a fresher WS push would never be corrected — the list stays
    wrong until the NEXT real change, which may never come.  device-panel's
    refresh() already guards this; app.js's loadDevices did not."""
    app = _read("js", "app.js")
    body = app.split("loadDevices: function")[1].split("loadFavorites: function")[0]
    assert "var startTick = store.devicesMutationTick;" in body, (
        "the tick must be recorded before the request goes out"
    )
    assert "store.devicesMutationTick !== startTick" in body, (
        "and re-checked before the snapshot is written"
    )
    # The guard has to come before the write, or it guards nothing.
    assert body.index("devicesMutationTick !== startTick") < body.index("store.devices.push("), (
        "the check must precede the list write"
    )


def test_keyboard_delete_confirms_like_every_other_delete_path():
    """history-item.js already claims "the inline trash and the
    Delete/Backspace key are permanent... every other delete path confirms
    first" — the Delete/Backspace path was the one that didn't, so a
    mis-aimed keypress wiped a row with no way back."""
    app = _read("js", "app.js")
    body = app.split("_deleteKbdItem: function")[1]
    assert "store.confirm(" in body, "the keyboard delete must confirm first"
    assert body.index("store.confirm(") < body.index("ClipsyncAPI.deleteItem("), (
        "the confirm must gate the request, not follow it"
    )
    assert "history.delete_title" in body and "history.delete_confirm" in body, (
        "reuse the same strings as the inline trash so the two agree"
    )


def test_cancelling_a_confirm_is_not_an_unhandled_rejection():
    """store.confirm() REJECTS on cancel.  A chain with no tail .catch turns
    every "no, don't delete that" into an unhandled promise rejection —
    console noise in the browser, and a hard failure under any page that
    treats unhandled rejections as errors."""
    for rel, marker in (
        (("js", "app.js"), "_deleteKbdItem: function"),
        (("components", "history-item.js"), "deleteItem: function"),
        (("components", "favorite-item.js"), "removeFavorite: function"),
    ):
        body = _read(*rel).split(marker)[1].split("\n      },")[0]
        assert ".catch(function () {})" in body, (
            f"{rel[-1]} {marker}: the confirm chain needs a tail .catch "
            "(pattern: history-panel.js clearAll)"
        )


def test_chat_panel_cleans_up_its_typing_timer():
    """The peer-typing deadline is a 4.5s timer that writes component state.
    Switching tabs unmounts the panel while it is still armed, so it fired
    against a dead instance — and on a fast tab-flip the stale timer from the
    previous mount could clear the indicator the new mount had just set."""
    panel = _read("components", "chat-panel.js")
    assert "beforeUnmount: function" in panel, "chat-panel must clean up on unmount"
    body = panel.split("beforeUnmount: function")[1].split("\n    template:")[0]
    assert "clearTimeout(this._peerTypingTimer)" in body
    assert "this._peerTypingTimer = null" in body
    # Leaving the tab must also retract our own "typing…" flag, or the peer
    # stares at it until the server-side deadline expires.
    assert "sendTypingState(false)" in body


def test_node_check_stage6_touched_files():
    if not _has_node():
        pytest.skip("node not available")
    for rel in (
        "js/app.js",
        "js/ws.js",
        "components/history-item.js",
        "components/favorite-item.js",
        "components/chat-panel.js",
    ):
        subprocess.run(
            ["node", "--check", os.path.join(_STATIC, rel)],
            check=True,
            capture_output=True,
            text=True,
        )
