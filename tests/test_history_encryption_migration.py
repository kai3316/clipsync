"""The encrypted migration writes one ciphertext layer, and stays readable.

An earlier `_migrate_from_json_file` decrypted the legacy JSON and then
re-encrypted it before handing the entries to `_entry_row`, which encrypts for
storage in turn.  Every migrated field therefore landed as
``encrypt(encrypt(plain))`` while every reader opens exactly one layer, so the
copied text came back as base64 — on disk, permanently, and only for users who
migrated to SQLite with encryption enabled.

These tests pin both halves of the repair: the migration must write one layer,
and a database written by the old code must come back readable after a restart.
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from internal.clipboard.history_db import ClipboardHistoryDB
from internal.security.encryption import EncryptionManager

TEXT = "敏感内容 secret ✓"
FINGERPRINT = "fingerprint-abcdefghijklmnop"


@pytest.fixture()
def enc():
    return EncryptionManager(FINGERPRINT, "hunter2")


def _legacy_entry(enc, entry_id: int = 1) -> dict:
    return {
        "entry_id": entry_id,
        "timestamp": 1_700_000_000.0 + entry_id,
        "content_type": "Text",
        "text_preview": enc.encrypt_storage(TEXT),
        "types": {"TEXT": enc.encrypt_storage(base64.b64encode(TEXT.encode()).decode())},
        "source_device": enc.encrypt_storage("peer-1"),
        "source_app": enc.encrypt_storage("Editor"),
        "source_title": enc.encrypt_storage("doc.txt"),
        "pinned": False,
        "paste_count": 0,
    }


def test_migrated_json_is_readable_after_a_restart(tmp_path, enc):
    (tmp_path / "clipboard_history.json").write_text(
        json.dumps([_legacy_entry(enc)]), encoding="utf-8"
    )
    db_path = tmp_path / "clipboard_history.db"

    db = ClipboardHistoryDB(storage_path=str(db_path), enc_mgr=enc)
    try:
        items = db.get_all()
        assert len(items) == 1
        assert items[0]["text_preview"] == TEXT
        assert items[0]["source_device"] == "peer-1"
        assert base64.b64decode(items[0]["types"]["TEXT"]).decode() == TEXT
    finally:
        db.close()

    reopened = ClipboardHistoryDB(storage_path=str(db_path), enc_mgr=enc)
    try:
        items = reopened.get_all()
        assert items[0]["text_preview"] == TEXT, "the row must survive the migration itself"
        assert items[0]["source_app"] == "Editor"
        assert base64.b64decode(items[0]["types"]["TEXT"]).decode() == TEXT
    finally:
        reopened.close()


def test_database_written_by_the_old_migration_is_repaired(tmp_path, enc):
    """A DB already holding two layers is repaired on the next load."""
    db_path = tmp_path / "clipboard_history.db"
    ClipboardHistoryDB(storage_path=str(db_path), enc_mgr=enc).close()

    double = lambda value: enc.encrypt_storage(enc.encrypt_storage(value))  # noqa: E731
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute(
            "INSERT INTO history (entry_id, timestamp, content_type, text_preview, types, "
            "source_device, source_app, source_title, pinned, paste_count, status, "
            "image_fmt, transport) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                7,
                1_700_000_001.0,
                "Text",
                double(TEXT),
                json.dumps({"TEXT": double(base64.b64encode(TEXT.encode()).decode())}),
                double("peer-2"),
                double("Editor"),
                double("doc.txt"),
                0,
                0,
                "",
                "",
                "",
            ),
        )
        raw.commit()
    finally:
        raw.close()

    db = ClipboardHistoryDB(storage_path=str(db_path), enc_mgr=enc)
    try:
        items = db.get_all()
        assert items[0]["text_preview"] == TEXT
        assert items[0]["source_device"] == "peer-2"
        assert base64.b64decode(items[0]["types"]["TEXT"]).decode() == TEXT
    finally:
        db.close()


def test_plaintext_history_is_left_alone(tmp_path, enc):
    """No password ever touched these rows; the repair must not re-wrap them."""
    db_path = tmp_path / "clipboard_history.db"
    plain = ClipboardHistoryDB(storage_path=str(db_path))
    plain._insert_row(
        {
            "entry_id": 3,
            "timestamp": 1_700_000_002.0,
            "content_type": "Text",
            "text_preview": "plain text",
            "types": {"TEXT": base64.b64encode(b"plain text").decode()},
            "source_device": "",
            "source_app": "",
            "source_title": "",
            "pinned": False,
            "paste_count": 0,
        }
    )
    plain.close()

    db = ClipboardHistoryDB(storage_path=str(db_path), enc_mgr=enc)
    try:
        assert db.get_all()[0]["text_preview"] == "plain text"
    finally:
        db.close()
