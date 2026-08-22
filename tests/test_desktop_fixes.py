"""Regression tests for the desktop UI / platform fix round.

Covers:
  #1 (P0) webview-mode dialogs must never block the Tk main thread.
  #2 (P1) config parse failures degrade per-field instead of resetting identity.
  #4 (P4) backup restore applies the ``peers`` payload (and drops public keys).
  #5 (P5) export / backup writes are atomic (``<name>.part`` + ``os.replace``).

All tests are runnable on Windows; platform-specific assertions branch on
``sys.platform`` rather than skipping so the suite still validates the common
logic on every OS.
"""

import json
import os
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

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
        assert "self._web_dialog_async(" in src, (
            f"{method.__name__} must use the async web dialog"
        )
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

    _point_config_at(tmp_path, monkeypatch, {
        "device_name": "Valid Host",
        "port": "abc",              # wrong type -> skipped, default kept
        "sync_enabled": "yes",      # wrong type -> skipped, default kept
        "history_max_entries": 42,  # valid -> applied
    })
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
    assert vk == ord("1")


# ═════════════════════════════════════════════════════════════════════════
# #4 (P4) — backup restore applies peers (and drops pinned public keys)
# ═════════════════════════════════════════════════════════════════════════


def test_backup_restore_applies_peers():
    from internal.config.config import Config
    from internal.data.backup import _apply_config

    cfg = Config()
    _apply_config({"peers": [
        {"device_id": "peer1", "device_name": "One", "paired": True,
         "public_key_pem": "SENSITIVE", "notes": "lab"},
        {"device_id": "peer2", "device_name": "Two", "paired": False},
    ]}, cfg)

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
    _apply_config({"peers": [
        "not-a-dict",
        {"device_id": "", "device_name": "Empty"},          # empty id -> skip
        {"device_id": "ok-id", "device_name": 123},         # bad name -> skip
        {"device_id": "good", "device_name": "Good", "paired": True},
    ]}, cfg)

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

    hist = _StubHistory([{
        "timestamp": 1.0, "content_type": "TEXT", "text_preview": "hi",
        "types": {"TEXT": "aGk="}, "source_device": "",
        "pinned": False, "paste_count": 0,
    }])
    out = tmp_path / "history.json"
    n = export_history_json(hist, str(out))
    assert n == 1
    assert out.exists()
    assert not (tmp_path / "history.json.part").exists()
    assert json.loads(out.read_text(encoding="utf-8"))[0]["text_preview"] == "hi"


def test_export_history_csv_is_atomic(tmp_path):
    from internal.data.export import export_history_csv

    hist = _StubHistory([{
        "timestamp": 1.0, "content_type": "TEXT", "text_preview": "hello",
        "source_device": "", "pinned": False, "paste_count": 0,
    }])
    out = tmp_path / "history.csv"
    n = export_history_csv(hist, str(out))
    assert n == 1
    assert out.exists()
    assert not (tmp_path / "history.csv.part").exists()
    assert "hello" in out.read_text(encoding="utf-8")


def test_create_backup_is_atomic_and_valid(tmp_path, monkeypatch):
    import internal.data.backup as backup
    from internal.config.config import Config, PeerInfo

    monkeypatch.setattr(backup, "_get_favorites_path",
                        lambda: tmp_path / "cfg" / "favorites.json")
    monkeypatch.setattr(backup, "_get_favorites_db_path",
                        lambda: tmp_path / "cfg" / "favorites.db")

    cfg = Config()
    cfg.peers["p1"] = PeerInfo(device_id="p1", device_name="One", paired=True,
                               public_key_pem="SECRET")

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

    monkeypatch.setattr(backup, "_get_favorites_path",
                        lambda: tmp_path / "cfg" / "favorites.json")
    monkeypatch.setattr(backup, "_get_favorites_db_path",
                        lambda: tmp_path / "cfg" / "favorites.db")

    from internal.config.config import Config
    backups_dir = tmp_path / "backups"
    with pytest.raises(RuntimeError):
        backup.create_backup(Config(), _BrokenHistory(),
                             backup_dir=str(backups_dir))
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
