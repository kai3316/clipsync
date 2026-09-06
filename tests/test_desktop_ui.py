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

from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config
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
    dash._window = None  # makes _render_card_chunk bail out early
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
        dead.destroyed = True  # already gone with the wiped frame
        dash._history_more_btn = dead
        dash._history_entries = [{"entry_id": i} for i in range(25)]

        dash._show_history_batch()  # must not raise

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
        backup_mod,
        "_get_favorites_db_path",
        lambda: tmp_path / "favorites.db",
    )
    monkeypatch.setattr(
        backup_mod,
        "_get_favorites_path",
        lambda: tmp_path / "favorites.json",
    )
    return tmp_path


class TestBackupHotkeys:
    def test_hotkeys_roundtrip(self, tmp_path, _isolated_favorites):
        cfg = Config()
        cfg.hotkeys = {"paste_1": "Ctrl+Alt+1"}
        cfg.hotkeys_enabled = True
        history = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))

        zip_path = backup_mod.create_backup(cfg, history, backup_dir=str(tmp_path / "bk"))

        fresh = Config()  # defaults everywhere
        result = backup_mod.restore_backup(zip_path, fresh, history)

        assert result["config"] is True
        assert fresh.hotkeys == {"paste_1": "Ctrl+Alt+1"}
        assert fresh.hotkeys_enabled is True

    def test_restore_drops_non_string_pairs(self, tmp_path, _isolated_favorites):
        """A dirty in-memory hotkey map must not bake junk into the archive:
        creation keeps only well-formed str→str pairs (json.dumps would
        otherwise stringify an int key), and restore-side validation
        re-checks every pair against the same rule."""
        cfg = Config()
        cfg.hotkeys = {"paste_1": "Ctrl+1", 42: "junk", "bad": 7}
        history = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))

        zip_path = backup_mod.create_backup(cfg, history, backup_dir=str(tmp_path / "bk"))
        with zipfile.ZipFile(zip_path) as zf:
            exported = json.loads(zf.read("config.json").decode("utf-8"))
        # Int-keyed pair filtered at export; non-str VALUE pair too.
        assert exported["hotkeys"] == {"paste_1": "Ctrl+1"}

        fresh = Config()
        backup_mod.restore_backup(zip_path, fresh, history)
        assert fresh.hotkeys == {"paste_1": "Ctrl+1"}

    def test_validate_strdict_rule(self):
        from internal.data.backup import _SKIP, _validate_config_value

        ok = _validate_config_value({"a": "Ctrl+1", 1: "x", "b": 2}, ("strdict",))
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
        assert counts == {"network": 1}  # only the "TCP Port" label

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
        tree = _FakeWidget(
            children=[
                _FakeWidget(text="TCP Port"),
                _FakeWidget(),  # no text attr → skipped
                _FakeWidget(
                    children=[
                        _FakeWidget(text="  "),  # blank → skipped
                        _FakeWidget(text="Theme"),
                    ]
                ),
            ]
        )
        texts = SettingsWindow._collect_widget_texts(tree)
        assert texts == ["TCP Port", "Theme"]


# ── 4. i18n completeness ──────────────────────────────────────────────


class TestSettingsSearchI18n:
    @pytest.mark.parametrize(
        "key",
        [
            "settings_window.search_placeholder",
            "settings_window.search_matches",
            "settings_window.search_no_matches",
        ],
    )
    def test_key_present_in_both_locales(self, key):
        for locale in ("en", "zh-CN"):
            assert key in LOCALES[locale], f"{key} missing in {locale}"


# ══════════════════════════════════════════════════
# merged from test_desktop_fixes.py
# ══════════════════════════════════════════════════

import os
import re
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═════════════════════════════════════════════════════════════════════════
# #1 (P0) — webview dialogs run their blocking wait off the Tk main thread
# ═════════════════════════════════════════════════════════════════════════


class _FakeRoot:
    """Minimal Tk-root stand-in: executes ``after(0, ...)`` callbacks inline."""

    def __init__(self):
        self.delivered = []

    def after(self, delay, fn, *args):
        if delay == 0:
            fn(*args)
        return 1


def test_web_dialog_async_runs_blocking_wait_off_main_thread():
    """``_web_dialog_async`` must not block the caller while the dialog wait
    is still pending — that wait happens on a worker thread, and only the
    result hop comes back (via ``root.after(0, ...)``)."""
    from src.main import Application

    app = Application.__new__(Application)
    app.root = _FakeRoot()

    release = threading.Event()

    def _blocking_dialog(dialog_type, **kwargs):
        # Mirrors DialogManager.show(): blocks up to 120s for a human.
        assert release.wait(timeout=5), "worker should be released by the test"
        return {"action": "accept"}

    app._web_dialog = _blocking_dialog  # type: ignore[attr-defined]

    results = []
    app._web_dialog_async("transfer_request", results.append, title="t")

    # Returned immediately — the dialog wait is still blocked on a worker.
    assert results == []

    release.set()
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.01)
    assert results == [{"action": "accept"}]


def test_webview_dialog_paths_use_async_not_blocking():
    """The webview branches of transfer-request / peer-pick / cert-retrust /
    send-URL must route through ``_web_dialog_async`` — a direct
    ``_web_dialog`` call from the main thread would stall hotkeys, tray
    polling and timers for up to two minutes."""
    import inspect

    from src.main import Application

    blocking_re = re.compile(r"self\._web_dialog\((?!async)")
    for method in (
        Application._show_transfer_request_dialog,
        Application._pick_peer_then,
        Application._ask_retrust_choice,
        Application._do_send_url,
    ):
        src = inspect.getsource(method)
        assert "self._web_dialog_async(" in src, f"{method.__name__} must use the async web dialog"
        assert not blocking_re.search(src), (
            f"{method.__name__} must not call blocking _web_dialog on the main thread"
        )


# ═════════════════════════════════════════════════════════════════════════
# #2 (P1) — config parse failures degrade per-field, not whole-identity
# ═════════════════════════════════════════════════════════════════════════


def _point_config_at(tmp_path, monkeypatch, data) -> Path:
    import internal.config.config as config_module

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: cfg_path)
    return cfg_path


def test_config_load_skips_invalid_field_types(tmp_path, monkeypatch):
    """A field with the wrong type is skipped with the default kept, while
    other valid fields still load — the whole identity must not be reset."""
    import internal.config.config as config_module

    _point_config_at(
        tmp_path,
        monkeypatch,
        {
            "device_name": "Valid Host",
            "port": "abc",  # wrong type -> skipped, default kept
            "sync_enabled": "yes",  # wrong type -> skipped, default kept
            "history_max_entries": 42,  # valid -> applied
        },
    )
    cfg = config_module.load()
    assert cfg.device_name == "Valid Host"
    assert cfg.port == 19990
    assert cfg.sync_enabled is True
    assert cfg.history_max_entries == 42


def test_config_load_corrupt_archives_file(tmp_path, monkeypatch):
    """An unparseable config is preserved as config.json.corrupt-<stamp>
    before degrading to defaults, so the identity is recoverable."""
    import internal.config.config as config_module

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text("{ definitely not json", encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: cfg_path)

    cfg = config_module.load()
    assert cfg.port == 19990
    assert not cfg_path.exists()  # original moved aside
    archived = list(cfg_dir.glob("config.json.corrupt-*"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "{ definitely not json"


def test_hotkey_parse_shortcut_rejects_non_string():
    """Config hotkeys are untrusted input: a non-string shortcut must raise
    ValueError (so it is skipped) instead of raising AttributeError."""
    import internal.system.hotkey as hotkey_module

    mgr = hotkey_module.HotkeyManager()
    for bad in (123, None, ["Ctrl+V"], 3.14, {"a": "b"}):
        with pytest.raises(ValueError):
            mgr._parse_shortcut(bad)  # type: ignore[arg-type]
    mods, vk = mgr._parse_shortcut("Ctrl+1")
    assert mods & hotkey_module.MOD_CONTROL
    # VK semantics are platform-specific: Windows/Linux use ord('1'), macOS
    # uses the Carbon kVK code (kVK_ANSI_1 = 18).  Assert against the platform
    # the manager detected rather than hardcoding the Windows value.
    expected_vk = hotkey_module._MAC_DIGIT_VK["1"] if mgr._platform == "macos" else ord("1")
    assert vk == expected_vk


# ═════════════════════════════════════════════════════════════════════════
# #4 (P4) — backup restore applies peers (and drops pinned public keys)
# ═════════════════════════════════════════════════════════════════════════


def test_backup_restore_applies_peers():
    from internal.config.config import Config
    from internal.data.backup import _apply_config

    cfg = Config()
    _apply_config(
        {
            "peers": [
                {
                    "device_id": "peer1",
                    "device_name": "One",
                    "paired": True,
                    "public_key_pem": "SENSITIVE",
                    "notes": "lab",
                },
                {"device_id": "peer2", "device_name": "Two", "paired": False},
            ]
        },
        cfg,
    )

    assert "peer1" in cfg.peers
    assert cfg.peers["peer1"].device_name == "One"
    assert cfg.peers["peer1"].paired is True
    assert cfg.peers["peer1"].notes == "lab"
    # public_key_pem is deliberately NOT carried over — it is re-exchanged
    # on reconnect, and importing a stale pin would cause cert lockouts.
    assert cfg.peers["peer1"].public_key_pem == ""
    assert cfg.peers["peer2"].device_name == "Two"
    assert cfg.peers["peer2"].paired is False


def test_backup_restore_skips_malformed_peers():
    from internal.config.config import Config
    from internal.data.backup import _apply_config

    cfg = Config()
    _apply_config(
        {
            "peers": [
                "not-a-dict",
                {"device_id": "", "device_name": "Empty"},  # empty id -> skip
                {"device_id": "ok-id", "device_name": 123},  # bad name -> skip
                {"device_id": "good", "device_name": "Good", "paired": True},
            ]
        },
        cfg,
    )

    assert set(cfg.peers) == {"good"}
    assert cfg.peers["good"].paired is True


# ═════════════════════════════════════════════════════════════════════════
# #5 (P5) — export / backup writes are atomic
# ═════════════════════════════════════════════════════════════════════════


class _StubHistory:
    """Minimal ClipboardHistory stand-in exposing ``get_all`` (the only API
    the export path uses)."""

    def __init__(self, entries=None):
        self._entries = entries if entries is not None else []

    def get_all(self):
        return list(self._entries)


def test_export_history_json_is_atomic(tmp_path):
    from internal.data.export import export_history_json

    hist = _StubHistory(
        [
            {
                "timestamp": 1.0,
                "content_type": "TEXT",
                "text_preview": "hi",
                "types": {"TEXT": "aGk="},
                "source_device": "",
                "pinned": False,
                "paste_count": 0,
            }
        ]
    )
    out = tmp_path / "history.json"
    n = export_history_json(hist, str(out))
    assert n == 1
    assert out.exists()
    assert not (tmp_path / "history.json.part").exists()
    assert json.loads(out.read_text(encoding="utf-8"))[0]["text_preview"] == "hi"


def test_export_history_csv_is_atomic(tmp_path):
    from internal.data.export import export_history_csv

    hist = _StubHistory(
        [
            {
                "timestamp": 1.0,
                "content_type": "TEXT",
                "text_preview": "hello",
                "source_device": "",
                "pinned": False,
                "paste_count": 0,
            }
        ]
    )
    out = tmp_path / "history.csv"
    n = export_history_csv(hist, str(out))
    assert n == 1
    assert out.exists()
    assert not (tmp_path / "history.csv.part").exists()
    assert "hello" in out.read_text(encoding="utf-8")


def test_create_backup_is_atomic_and_valid(tmp_path, monkeypatch):
    import internal.data.backup as backup
    from internal.config.config import Config, PeerInfo

    monkeypatch.setattr(backup, "_get_favorites_path", lambda: tmp_path / "cfg" / "favorites.json")
    monkeypatch.setattr(backup, "_get_favorites_db_path", lambda: tmp_path / "cfg" / "favorites.db")

    cfg = Config()
    cfg.peers["p1"] = PeerInfo(
        device_id="p1", device_name="One", paired=True, public_key_pem="SECRET"
    )

    backups_dir = tmp_path / "backups"
    zip_path = backup.create_backup(cfg, _StubHistory(), backup_dir=str(backups_dir))

    # No .part leftover from an interrupted write.
    assert not list(backups_dir.glob("*.part"))
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "config.json" in names
        assert "history.json" in names
        cfg_data = json.loads(zf.read("config.json"))
        assert cfg_data["peers"][0]["device_id"] == "p1"
        assert cfg_data["peers"][0]["paired"] is True
        # public_key_pem is excluded from the backup peer payload.
        assert "public_key_pem" not in cfg_data["peers"][0]
        # history.json round-trips (create_backup validates it before zipping).
        json.loads(zf.read("history.json"))


def test_create_backup_aborts_when_history_export_invalid(tmp_path, monkeypatch):
    """create_backup validates history.json before packaging and refuses to
    ship a backup whose history cannot be restored."""
    import internal.data.backup as backup

    class _BrokenHistory:
        def get_all(self):
            raise RuntimeError("history read failed")

    monkeypatch.setattr(backup, "_get_favorites_path", lambda: tmp_path / "cfg" / "favorites.json")
    monkeypatch.setattr(backup, "_get_favorites_db_path", lambda: tmp_path / "cfg" / "favorites.db")

    from internal.config.config import Config

    backups_dir = tmp_path / "backups"
    with pytest.raises(RuntimeError):
        backup.create_backup(Config(), _BrokenHistory(), backup_dir=str(backups_dir))
    # No partial archive left behind.
    assert not list(backups_dir.glob("*.zip"))
    assert not list(backups_dir.glob("*.part"))


# ═════════════════════════════════════════════════════════════════════════
# P3 — port-in-use message teaches the platform's native command
# ═════════════════════════════════════════════════════════════════════════


def test_port_in_use_msg_is_platform_aware():
    import internal.i18n as i18n

    msg = i18n.T("ui.port_in_use_msg", port=19990)
    if sys.platform.startswith("win"):
        assert "netstat -ano | findstr :19990" in msg
        assert "lsof" not in msg
    else:
        assert "lsof -i :19990" in msg


# ═════════════════════════════════════════════════════════════════════════
# Stage 5 — main-program / platform lifecycle
# ═════════════════════════════════════════════════════════════════════════


def _make_stale(path, age_seconds):
    """Backdate a file/dir mtime so the sweep sees it as abandoned."""
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def _sweeper():
    """A stand-in ``self`` carrying only what _sweep_stale_temp reads."""
    import types

    from src.main import Application

    return types.SimpleNamespace(
        _TEMP_SWEEP_AGE=Application._TEMP_SWEEP_AGE,
    ), Application._sweep_stale_temp


def test_temp_sweep_removes_abandoned_update_downloads(tmp_path, monkeypatch):
    """An update download that was never installed is tens of MB of installer
    parked in %TEMP% forever.  Anything older than a day is nobody's."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    stale = tmp_path / "clipsync_update_old"
    stale.mkdir()
    (stale / "ClipSync-Setup.exe").write_bytes(b"x" * 16)
    _make_stale(stale, 48 * 3600)

    fresh = tmp_path / "clipsync_update_now"
    fresh.mkdir()
    (fresh / "ClipSync-Setup.exe").write_bytes(b"x" * 16)

    other = tmp_path / "someone_elses_dir"
    other.mkdir()
    _make_stale(other, 48 * 3600)

    self_, sweep = _sweeper()
    sweep(self_)

    assert not stale.exists(), "an abandoned update download must be swept"
    assert fresh.exists(), "a download from this session must survive"
    assert other.exists(), "the sweep must only touch our own prefixes"


def test_temp_sweep_never_touches_the_restart_dir(tmp_path, monkeypatch):
    """A frozen restart hands clipsync_restart/<hex> to a child process whose
    live PyInstaller _MEI extraction sits inside it.  Age proves nothing there
    — a long-running child keeps an old-looking directory in use — so this
    directory is off-limits to housekeeping, full stop."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    restart = tmp_path / "clipsync_restart"
    (restart / "deadbeef").mkdir(parents=True)
    (restart / "deadbeef" / "_MEI123").mkdir()
    _make_stale(restart / "deadbeef", 90 * 24 * 3600)
    _make_stale(restart, 90 * 24 * 3600)

    self_, sweep = _sweeper()
    sweep(self_)

    assert (restart / "deadbeef" / "_MEI123").exists()


def test_temp_sweep_clears_stale_chat_uploads_only(tmp_path, monkeypatch):
    """Chat attachments are staged in temp and read by the transfer; nothing
    deletes them afterwards.  Sweep old files, leave fresh ones (a send may
    still be reading) and leave directories alone."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    chat = tmp_path / "clipsync_chat_uploads"
    chat.mkdir()
    old_file = chat / "photo.png"
    old_file.write_bytes(b"old")
    _make_stale(old_file, 48 * 3600)
    new_file = chat / "sending-right-now.bin"
    new_file.write_bytes(b"new")
    sub = chat / "a-directory"
    sub.mkdir()
    _make_stale(sub, 48 * 3600)

    self_, sweep = _sweeper()
    sweep(self_)

    assert not old_file.exists()
    assert new_file.exists()
    assert sub.exists()


def test_temp_sweep_survives_a_missing_temp_dir(tmp_path, monkeypatch):
    """Housekeeping is best effort: an unreadable %TEMP% must not raise out of
    the background thread."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "does-not-exist"))
    self_, sweep = _sweeper()
    sweep(self_)  # must not raise


def test_temp_sweep_runs_off_the_main_thread():
    """The scan walks every entry in %TEMP% — thousands on an old machine —
    so it must never sit between startup and the tray appearing."""
    import inspect

    from src.main import Application

    src = inspect.getsource(Application._start_threads)
    assert "_sweep_stale_temp" in src, "startup must schedule the sweep"
    assert "threading.Thread(target=self._sweep_stale_temp" in src, (
        "the sweep must be started on its own thread, not called inline"
    )


def test_pid_alive_accepts_a_source_run_outside_a_clipsync_folder(monkeypatch):
    """The single-instance guard must recognise a running source checkout.

    A dev/source install is commonly in a folder named something else
    entirely (this repo's is "copyboard"), so argv reads
    "python .../src/main.py" with the word clipsync nowhere in it.  Demanding
    "clipsync" reported that live instance as dead, which let a second one
    start and clear the first one's lock file."""
    import pathlib

    import src.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "linux")
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        pathlib.Path,
        "read_bytes",
        lambda self: b"/usr/bin/python3\x00/home/u/Desktop/copyboard/src/main.py\x00",
    )
    assert main_mod._pid_alive(4242) is True


def test_pid_alive_still_recognises_a_frozen_run(monkeypatch):
    import pathlib

    import src.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "linux")
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: b"/opt/clipsync/clipsync\x00")
    assert main_mod._pid_alive(4242) is True


def test_pid_alive_rejects_an_unrelated_process_reusing_the_pid(monkeypatch):
    """PID reuse is the whole reason this reads cmdline at all."""
    import pathlib

    import src.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "linux")
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: b"/usr/bin/vim\x00notes.txt\x00")
    assert main_mod._pid_alive(4242) is False


def test_netpair_sweep_waits_until_cfg_exists():
    """``_netpair_drop_provisional`` reads self.cfg.netpair_secrets, and
    __init__ runs long before load_config() assigns self.cfg — calling it
    there swept nothing, every launch, silently."""
    import inspect

    from src.main import Application

    init_src = inspect.getsource(Application.__init__)
    load_src = inspect.getsource(Application.load_config)

    assert "self._netpair_drop_provisional()" not in init_src, (
        "the sweep must not run in __init__, where self.cfg is still None"
    )
    assert "self._netpair_drop_provisional()" in load_src
    assert load_src.index("self.cfg = load()") < load_src.index(
        "self._netpair_drop_provisional()"
    ), "the sweep must run after cfg is loaded"


def test_shutdown_releases_the_lock_even_when_the_final_save_fails(monkeypatch):
    """A failed save on the way out (disk full, unserialisable peer row) used
    to skip the rest of teardown — including _remove_lock() — so the next
    launch refused to start with "ClipSync is already running"."""
    import types

    import src.main as main_mod
    from src.main import Application

    unlocked = []
    monkeypatch.setattr(main_mod, "_remove_lock", lambda: unlocked.append(True))

    app = Application.__new__(Application)
    app._shutting_down = False
    app._stop_updater = threading.Event()
    app._stop_internet_sync = lambda: None
    app._clear_pause_state = lambda: None
    app.sync_mgr = types.SimpleNamespace(stop=lambda: None)
    app.chat_mgr = None
    app.discovery = types.SimpleNamespace(stop=lambda: None)
    app.transport_mgr = types.SimpleNamespace(stop_server=lambda: None)
    app.webview_win = None
    app._push_web = lambda *a, **k: None
    app.web_server = None
    app._pairing_notify_timers = {}
    app._skip_save_on_shutdown = False
    app.hotkey_mgr = None
    app._tray_proc = None
    app.root = None

    def _boom():
        raise RuntimeError("disk full")

    app._merge_hashed_peer_rows = _boom

    app.shutdown()  # must not raise

    assert unlocked == [True], "the single-instance lock must always be released"


def test_systray_without_pystray_reports_instead_of_crashing(monkeypatch):
    """On a headless/Wayland box the module-level pystray import fails and the
    name is None.  The menu build then raised AttributeError from OUTSIDE the
    handler written for exactly this case."""
    import internal.ui.systray as systray_mod

    monkeypatch.setattr(systray_mod, "pystray", None)

    told = []
    tray = systray_mod.SystrayApp.__new__(systray_mod.SystrayApp)
    tray._on_tray_failed = lambda: told.append(True)

    tray.run()  # must not raise

    assert told == [True], "the user must be told the tray is not there"


def test_webview_falls_back_when_the_found_browser_cannot_be_launched(monkeypatch):
    """_find_browser() proves a path was DISCOVERED, not that it runs.  A
    moved or permission-denied binary made Popen raise straight out of
    start(), so no dashboard opened at all — even though the default-browser
    fallback right there would have worked."""
    from internal.ui.webview_window import WebViewWindow

    win = WebViewWindow("http://127.0.0.1:9580/index.html?token=t")
    monkeypatch.setattr(win, "is_running", lambda: False)
    monkeypatch.setattr(
        "internal.ui.webview_window._find_browser", lambda: ("chrome", ["/gone/chrome", "--app=%s"])
    )

    def _cannot_exec():
        raise PermissionError("nope")

    monkeypatch.setattr(win, "_launch_browser", _cannot_exec)

    opened = []
    monkeypatch.setattr(win, "_start_fallback", lambda: opened.append(True))

    win.start()  # must not raise

    assert opened == [True]
    assert win._process is None


def test_webview_fallback_survives_a_dead_default_browser(monkeypatch):
    """Last resort: no BROWSER, no xdg-open.  Raising here would take down the
    tray click for something the user can work around by typing the URL."""
    import webbrowser

    from internal.ui.webview_window import WebViewWindow

    def _boom(url):
        raise RuntimeError("no browser")

    monkeypatch.setattr(webbrowser, "open_new", _boom)
    win = WebViewWindow("http://127.0.0.1:9580/index.html")
    win._start_fallback()  # must not raise


def test_web_port_is_restored_before_the_failure_dialog():
    """After the +1..+5 scan fails, cfg.web_port is parked on the LAST port
    tried.  Left there it gets persisted by the very next save, so each failed
    launch drifts the port another +5 while settings and diagnostics report a
    port nothing ever listened on."""
    import inspect

    from src.main import Application

    src = inspect.getsource(Application._start_services)
    marker = "self.cfg.web_port = base_port"
    assert marker in src, "the base port must be restored after a failed scan"
    restore = src.index(marker)
    dialog = src.index('show_error(self.root, T("ui.web_companion")')
    assert restore < dialog, "the port must be restored before anything can persist or display it"
