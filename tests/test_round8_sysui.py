"""Round-8 system-UI tests.

Covers:
1. Dashboard history lazy-render: a stale "show more" button reference
   (already destroyed by a list rebuild) must not abort the batch render.
2. Backup/restore now carries global-hotkey bindings (hotkeys +
   hotkeys_enabled) with strict validation on the way back in.
3. Settings search: text-index collection, per-panel match counting.
4. i18n: the new settings-search keys exist in BOTH locales.
"""

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk

from internal.config.config import Config
from internal.clipboard.history import ClipboardHistory
from internal.data import backup as backup_mod
from internal.i18n import LOCALES
from internal.ui.dashboard import DashboardWindow
from internal.ui.settings_window import SettingsWindow


# ── helpers ───────────────────────────────────────────────────────────


class _DeadButton:
    """Mimics a Tk widget whose .destroy() raises after being destroyed."""

    def __init__(self):
        self.destroyed = False

    def winfo_exists(self):
        return not self.destroyed

    def destroy(self):
        if self.destroyed:
            raise tk.TclError("bad window path name")
        self.destroyed = True


def _bare_dashboard() -> DashboardWindow:
    """A DashboardWindow shell without running __init__ (no Tk objects)."""
    dash = object.__new__(DashboardWindow)
    dash._window = None          # makes _render_card_chunk bail out early
    dash._root = None
    dash._history_shown = 0
    dash._history_entries = []
    dash._history_peer_map = {}
    return dash


class _FakeWidget:
    """Minimal widget stand-in for the settings text-index collector."""

    def __init__(self, text=None, children=()):
        self._text = text
        self._children = list(children)

    def winfo_children(self):
        return self._children

    def cget(self, name):
        if name == "text" and self._text is not None:
            return self._text
        raise ValueError(f"unknown option {name!r}")


# ── 1. History batch render vs stale more-button ──────────────────────


class TestHistoryMoreButtonGuard:
    def test_stale_more_button_does_not_abort_batch(self):
        """After _refresh_history_list wipes the scroll frame the attribute
        still references the dead button; clicking/refreshing used to hit
        destroy() twice (TclError on several stock Tk builds), aborting the
        whole render and leaving the panel blank."""
        dash = _bare_dashboard()
        dead = _DeadButton()
        dead.destroyed = True           # already gone with the wiped frame
        dash._history_more_btn = dead
        dash._history_entries = [{"entry_id": i} for i in range(25)]

        dash._show_history_batch()      # must not raise

        assert dash._history_more_btn is None

    def test_live_more_button_still_destroyed(self):
        dash = _bare_dashboard()
        live = _DeadButton()
        dash._history_more_btn = live
        dash._history_entries = [{}]

        dash._show_history_batch()

        assert live.destroyed
        assert dash._history_more_btn is None


# ── 2. Backup carries hotkey bindings ─────────────────────────────────


@pytest.fixture()
def _isolated_favorites(tmp_path, monkeypatch):
    """Point the favorites paths at tmp so tests never touch real user data."""
    monkeypatch.setattr(
        backup_mod, "_get_favorites_db_path",
        lambda: tmp_path / "favorites.db",
    )
    monkeypatch.setattr(
        backup_mod, "_get_favorites_path",
        lambda: tmp_path / "favorites.json",
    )
    return tmp_path


class TestBackupHotkeys:
    def test_hotkeys_roundtrip(self, tmp_path, _isolated_favorites):
        cfg = Config()
        cfg.hotkeys = {"quick_paste": "Ctrl+^", "paste_1": "Ctrl+Alt+1"}
        cfg.hotkeys_enabled = True
        history = ClipboardHistory(storage_path=str(tmp_path / "h.json"))

        zip_path = backup_mod.create_backup(
            cfg, history, backup_dir=str(tmp_path / "bk"))

        fresh = Config()   # defaults everywhere
        result = backup_mod.restore_backup(zip_path, fresh, history)

        assert result["config"] is True
        assert fresh.hotkeys == {
            "quick_paste": "Ctrl+^", "paste_1": "Ctrl+Alt+1"}
        assert fresh.hotkeys_enabled is True

    def test_restore_drops_non_string_pairs(self, tmp_path,
                                            _isolated_favorites):
        """A dirty in-memory hotkey map must not bake junk into the archive:
        creation keeps only well-formed str→str pairs (json.dumps would
        otherwise stringify an int key), and restore-side validation
        re-checks every pair against the same rule."""
        cfg = Config()
        cfg.hotkeys = {"paste_1": "Ctrl+1", 42: "junk", "bad": 7}
        history = ClipboardHistory(storage_path=str(tmp_path / "h.json"))

        zip_path = backup_mod.create_backup(
            cfg, history, backup_dir=str(tmp_path / "bk"))
        with zipfile.ZipFile(zip_path) as zf:
            exported = json.loads(zf.read("config.json").decode("utf-8"))
        # Int-keyed pair filtered at export; non-str VALUE pair too.
        assert exported["hotkeys"] == {"paste_1": "Ctrl+1"}

        fresh = Config()
        backup_mod.restore_backup(zip_path, fresh, history)
        assert fresh.hotkeys == {"paste_1": "Ctrl+1"}

    def test_validate_strdict_rule(self):
        from internal.data.backup import _SKIP, _validate_config_value

        ok = _validate_config_value(
            {"a": "Ctrl+1", 1: "x", "b": 2}, ("strdict",))
        assert ok == {"a": "Ctrl+1"}

        assert _validate_config_value(["nope"], ("strdict",)) is _SKIP
        assert _validate_config_value("nope", ("strdict",)) is _SKIP
        assert _validate_config_value({}, ("strdict",)) == {}


# ── 3. Settings search matching logic ─────────────────────────────────


def _bare_settings() -> SettingsWindow:
    return object.__new__(SettingsWindow)


class TestSettingsSearchCounts:
    def _indexed(self):
        sw = _bare_settings()
        sw._panel_texts = {
            "network": ["🌐  Network", "TCP Port", "Service Type"],
            "appearance": ["🎨  Appearance", "Theme", "Dark"],
            "advanced": ["⚙  Advanced", "历史记录上限", "Language"],
        }
        return sw

    def test_empty_query_matches_nothing(self):
        sw = self._indexed()
        assert sw._search_counts("") == {}
        assert sw._search_counts("   ") == {}

    def test_case_insensitive_substring(self):
        sw = self._indexed()
        counts = sw._search_counts("port")
        assert counts == {"network": 1}     # only the "TCP Port" label

    def test_only_matching_panels_listed(self):
        sw = self._indexed()
        counts = sw._search_counts("dark")
        assert counts == {"appearance": 1}  # only the "Dark" label

    def test_unicode_query(self):
        sw = self._indexed()
        assert sw._search_counts("历史") == {"advanced": 1}

    def test_no_match_returns_empty(self):
        sw = self._indexed()
        assert sw._search_counts("zzzz") == {}

    def test_collector_walks_tree_and_skips_textless(self):
        tree = _FakeWidget(children=[
            _FakeWidget(text="TCP Port"),
            _FakeWidget(),                      # no text attr → skipped
            _FakeWidget(children=[
                _FakeWidget(text="  "),         # blank → skipped
                _FakeWidget(text="Theme"),
            ]),
        ])
        texts = SettingsWindow._collect_widget_texts(tree)
        assert texts == ["TCP Port", "Theme"]


# ── 4. i18n completeness ──────────────────────────────────────────────


class TestSettingsSearchI18n:
    @pytest.mark.parametrize("key", [
        "settings_window.search_placeholder",
        "settings_window.search_matches",
        "settings_window.search_no_matches",
    ])
    def test_key_present_in_both_locales(self, key):
        for locale in ("en", "zh-CN"):
            assert key in LOCALES[locale], f"{key} missing in {locale}"
