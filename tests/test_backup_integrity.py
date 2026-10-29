"""A backup is the last copy of a user's data, so "success" has to be earned.

Three failures lived here, all of them invisible at the moment they happened:

* a favorites database that could not be read was folded into "there are no
  favorites", so the archive shipped without ``favorites.json`` and the call
  returned a path as if everything was fine;
* two backups started in the same second picked one filename and ``os.replace``
  left the earlier one gone;
* the exported config omitted the security preferences, so restoring onto a new
  machine silently turned automatic file receiving and open chats back on.

``tests/test_backup_contents.py`` holds the field contract; this file holds the
behaviour.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config
from internal.data import backup as backup_mod
from internal.data.backup import BackupIncompleteError, create_backup, restore_backup


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    """Point the module's config-directory lookups at a scratch directory."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(backup_mod, "_get_favorites_db_path", lambda: cfg_dir / "favorites.db")
    monkeypatch.setattr(backup_mod, "_get_favorites_path", lambda: cfg_dir / "favorites.json")
    return cfg_dir


def _favorites_db(path: Path, rows: int = 1) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            'CREATE TABLE favorites (id TEXT PRIMARY KEY, title TEXT, content TEXT, '
            '"group" TEXT, position INTEGER, created REAL, updated REAL)'
        )
        for index in range(rows):
            conn.execute(
                'INSERT INTO favorites VALUES (?,?,?,?,?,?,?)',
                (f"id{index}", f"t{index}", f"body{index}", "", index, 1.0, None),
            )
        conn.commit()
    finally:
        conn.close()


def _history(tmp_path) -> ClipboardHistoryDB:
    return ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"))


def test_an_unreadable_favorites_database_aborts_the_backup(tmp_path, config_dir):
    db_path = config_dir / "favorites.db"
    # A file that is not a database at all: the shape of a truncated write or a
    # corrupt page, and the case that used to be read as "no favorites".
    db_path.write_bytes(b"not a database")
    history = _history(tmp_path)
    try:
        with pytest.raises(BackupIncompleteError):
            create_backup(Config(), history, backup_dir=str(tmp_path / "backups"))
    finally:
        history.close()
    assert list((tmp_path / "backups").iterdir()) == [], "no archive, and no placeholder"


def test_an_empty_favorites_database_still_writes_the_member(tmp_path, config_dir):
    _favorites_db(config_dir / "favorites.db", rows=0)
    history = _history(tmp_path)
    try:
        path = create_backup(Config(), history, backup_dir=str(tmp_path / "backups"))
    finally:
        history.close()

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "favorites.json" in names
        assert json.loads(zf.read("favorites.json")) == []
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["components"]["favorites"] == 0
    assert {"config.json", "history.json", "manifest.json"} <= names


def test_stale_legacy_favorites_are_not_used_when_the_database_exists(tmp_path, config_dir):
    """An empty DB plus an old JSON file must not resurrect the old library."""
    _favorites_db(config_dir / "favorites.db", rows=0)
    (config_dir / "favorites.json").write_text(
        json.dumps([{"id": "old", "title": "old", "content": "old"}]), encoding="utf-8"
    )
    history = _history(tmp_path)
    try:
        path = create_backup(Config(), history, backup_dir=str(tmp_path / "backups"))
    finally:
        history.close()

    with zipfile.ZipFile(path) as zf:
        assert json.loads(zf.read("favorites.json")) == []


def test_two_backups_in_the_same_second_are_two_archives(tmp_path, config_dir):
    _favorites_db(config_dir / "favorites.db", rows=1)
    history = _history(tmp_path)
    try:
        first = create_backup(Config(), history, backup_dir=str(tmp_path / "backups"))
        second = create_backup(Config(), history, backup_dir=str(tmp_path / "backups"))
    finally:
        history.close()

    assert first != second
    for path in (first, second):
        with zipfile.ZipFile(path) as zf:
            assert "config.json" in zf.namelist(), f"{path} is not restorable"


def test_the_restore_reports_a_truncated_favorites_member(tmp_path, config_dir):
    _favorites_db(config_dir / "favorites.db", rows=2)
    history = _history(tmp_path)
    try:
        path = create_backup(Config(), history, backup_dir=str(tmp_path / "backups"))
    finally:
        history.close()

    # Rewrite the archive as a truncated copy: manifest still says 2.
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "favorites.json":
                payload = json.loads(data)
                data = json.dumps(payload[:1]).encode()
            dst.writestr(item.filename, data)

    cfg = Config()
    history = _history(tmp_path)
    try:
        summary = restore_backup(str(tampered), cfg, history)
    finally:
        history.close()
    assert "favorites" in summary.get("errors", [])


def test_the_security_posture_survives_a_round_trip(tmp_path, config_dir):
    """A restore must not quietly re-open what the source machine had closed."""
    _favorites_db(config_dir / "favorites.db", rows=1)
    source = Config()
    source.file_open_to_all = False
    source.chat_open_to_all = False
    source.log_sharing = False

    history = _history(tmp_path)
    try:
        path = create_backup(source, history, backup_dir=str(tmp_path / "backups"))
    finally:
        history.close()

    restored = Config()  # a fresh machine: permissive defaults
    assert restored.file_open_to_all is True
    history = _history(tmp_path)
    try:
        summary = restore_backup(path, restored, history)
    finally:
        history.close()

    assert summary["config"] is True
    assert restored.file_open_to_all is False
    assert restored.chat_open_to_all is False
    assert restored.log_sharing is False
