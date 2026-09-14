"""Backup, restore, export and the lock — the paths where a mistake loses data.

The static wiring assertions and the private-method pokes this file used to
carry are gone.  What is left is everything that reads or writes something a
user owns:

1. Backup/restore carries the global-hotkey bindings, drops junk pairs on the
   way out and on the way back in, and re-applies peers WITHOUT the pinned
   public keys (a stale pin is a certificate lockout).
2. Config load degrades per field, and archives an unparseable config before
   falling back to defaults, so an identity is never lost outright.
3. Exports and backups are atomic (no ``.part`` or partial archive left), and
   a backup whose history cannot be read is refused rather than shipped.
4. The ``%TEMP%`` sweep removes only our own abandoned scratch, and the
   single-instance lock recognises a live instance whatever it was launched
   from — and always releases the lock on the way out.
"""

import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config
from internal.data import backup as backup_mod

# ── 1. Backup carries hotkey bindings ─────────────────────────────────


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


# ── 2. Config parse failures degrade per-field, not whole-identity ────


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


# ── 3. Backup restore applies peers (and drops pinned public keys) ────


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


# ── 4. Export / backup writes are atomic ──────────────────────────────


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


# ── 5. The %TEMP% sweep deletes only our own abandoned scratch ────────


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


# ── 6. The single-instance lock ───────────────────────────────────────


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


def test_pid_alive_rejects_an_unrelated_process_reusing_the_pid(monkeypatch):
    """PID reuse is the whole reason this reads cmdline at all."""
    import pathlib

    import src.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "linux")
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: b"/usr/bin/vim\x00notes.txt\x00")
    assert main_mod._pid_alive(4242) is False


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
