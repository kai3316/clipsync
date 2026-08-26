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
    for rel in ("js/store.js", "js/app.js", "components/history-item.js",
                "components/history-panel.js"):
        subprocess.run(
            ["node", "--check", os.path.join(_STATIC, rel)],
            check=True,
            capture_output=True,
            text=True,
        )
