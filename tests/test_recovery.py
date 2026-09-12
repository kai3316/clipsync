"""Repairing a data directory the sidecar refuses to start on.

`internal/data/recovery.py` moves aside exactly what
`internal/application/bootstrap.py` refuses, so the two must be checked against
the same cases: every finding here is a directory the app would not open.
"""

import json
import sqlite3

import pytest

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config, save
from internal.data import recovery
from internal.infrastructure.security.device_identity import prepare_identity
from internal.security.encryption import EncryptionManager


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """The directory the sidecar reads, selected the way the host selects it."""
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    return tmp_path


def write_config(directory, config):
    (directory / recovery.CONFIG_NAME).write_text(
        config if isinstance(config, str) else json.dumps(config), encoding="utf-8"
    )


def write_history(directory, rows=0, *, keyed=True):
    """Rows through the real writer, so "keyed" means what the app means by it.

    Keyed rows are encrypted with a fingerprint that no identity in these
    directories matches, which is history nobody can read.  Plain rows are the
    user's own text, which needs no key at all.
    """
    encryption = EncryptionManager("legacy-fingerprint") if keyed else None
    repository = ClipboardHistoryDB(
        storage_path=str(directory / recovery.HISTORY_NAME), max_entries=50, enc_mgr=encryption
    )
    for index in range(rows):
        repository.add(ClipboardContent(types={ContentType.TEXT: f"clip {index}".encode()}))
    repository.close()


def healthy_config(directory):
    """A complete identity, written the way the application writes one."""
    session = prepare_identity(Config(encryption_enabled=False))
    save(session.config, session.encryption)
    return directory / recovery.CONFIG_NAME


def reasons(findings):
    return [(finding.artifact, finding.reason) for finding in findings]


def test_a_clean_directory_has_nothing_to_repair(data_dir):
    write_config(data_dir, {"config_version": 2, "device_id": "a"})
    assert recovery.quarantine() == []


@pytest.mark.parametrize(
    "config",
    [
        "{not json",
        "[]",
        {"config_version": 3},
        {"config_version": "2"},
        {"config_version": 2, "private_key_pem": 42},
        {"config_version": 2, "encryption_enabled": "yes"},
    ],
    ids=["not-json", "not-an-object", "unknown-version", "version-type",
         "identity-field-type", "encryption-type"],
)
def test_an_unusable_configuration_is_moved_aside(data_dir, config):
    write_config(data_dir, config)
    assert reasons(recovery.inspect()) == [("config", "unreadable")]
    report = recovery.quarantine()
    assert [entry["artifact"] for entry in report] == ["config"]
    # Renamed, never deleted: the user can put the original back.
    assert not (data_dir / recovery.CONFIG_NAME).exists()
    archived = data_dir / report[0]["files"][0]
    assert archived.name.startswith("config.json.corrupt-")
    assert archived.exists()
    assert recovery.quarantine() == []


def test_encrypted_history_leaves_with_a_configuration_that_has_to_go(data_dir):
    # The at-rest key is derived from the fingerprint of the identity that the
    # broken config carries, so the history cannot be read once it is gone.
    write_config(data_dir, {"config_version": 3, "encryption_enabled": True})
    write_history(data_dir, rows=2)
    assert reasons(recovery.inspect()) == [
        ("config", "unreadable"),
        ("history", "history_identity_lost"),
    ]
    recovery.quarantine()
    assert not (data_dir / recovery.HISTORY_NAME).exists()


def test_a_broken_configuration_that_cannot_be_read_keeps_the_history(data_dir):
    # Unreadable as JSON says nothing about encryption, so the history stays put
    # for a manual rescue rather than being moved on a guess.
    write_config(data_dir, "{not json")
    write_history(data_dir, rows=2)
    assert reasons(recovery.inspect()) == [("config", "unreadable")]
    recovery.quarantine()
    assert (data_dir / recovery.HISTORY_NAME).exists()


def test_a_locked_install_is_not_damage(data_dir):
    # Encryption on with a verification hash means the key is waiting for a
    # password, not that anything is broken.
    write_config(data_dir, {
        "config_version": 2, "device_id": "a", "certificate_pem": "cert",
        "private_key_pem": "key", "encryption_enabled": True,
        "encryption_password_hash": "hash",
    })
    assert recovery.quarantine() == []


def test_a_corrupt_history_database_is_moved_with_its_companions(data_dir):
    healthy_config(data_dir)
    (data_dir / recovery.HISTORY_NAME).write_bytes(b"not a database")
    (data_dir / "clipboard_history.db-wal").write_bytes(b"wal")
    assert reasons(recovery.inspect()) == [("history", "history_corrupt")]
    report = recovery.quarantine()
    moved = report[0]["files"]
    assert moved[0].startswith("clipboard_history.db.corrupt-")
    assert moved[1].startswith("clipboard_history.db-wal.corrupt-")
    # A stale write-ahead log would otherwise replay into the fresh database.
    assert not (data_dir / recovery.HISTORY_NAME).exists()
    assert not (data_dir / "clipboard_history.db-wal").exists()


def test_records_no_identity_can_unlock_are_moved(data_dir):
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    write_history(data_dir, rows=3)
    assert reasons(recovery.inspect()) == [("history", "history_identityless")]
    recovery.quarantine()
    assert not (data_dir / recovery.HISTORY_NAME).exists()


def test_plaintext_records_need_no_identity_at_all(data_dir):
    # A 1.x install that had encryption turned off leaves rows the user can read.
    # The replacement config that follows a repair says encryption is on, so the
    # question has to be put to the rows: moving these aside would discard clips
    # that no key was ever needed for.
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    write_history(data_dir, rows=3, keyed=False)
    assert recovery.inspect() == []
    assert recovery.quarantine() == []
    assert (data_dir / recovery.HISTORY_NAME).exists()


def test_a_history_the_app_cannot_read_counts_as_records(data_dir):
    # A schema this build does not know cannot be shown to be readable, and
    # guessing wrong hands a fresh identity the job of reading it.
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    connection = sqlite3.connect(data_dir / recovery.HISTORY_NAME)
    connection.execute("CREATE TABLE history (id TEXT PRIMARY KEY, content BLOB)")
    connection.execute("INSERT INTO history VALUES ('1', x'78')")
    connection.commit()
    connection.close()
    assert reasons(recovery.inspect()) == [("history", "history_identityless")]


def test_a_mixed_history_waits_for_the_key_that_wrote_part_of_it(data_dir):
    # Encryption turned on mid-life leaves readable rows beside keyed ones. The
    # keyed rows are the reason the whole file waits: a fresh identity opening
    # the database would read base64 where the user expects their clips.
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    write_history(data_dir, rows=1, keyed=False)
    write_history(data_dir, rows=1)
    assert reasons(recovery.inspect()) == [("history", "history_identityless")]


def test_a_legacy_json_history_keyed_by_a_lost_identity_is_records(data_dir):
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    stored = EncryptionManager("legacy-fingerprint").encrypt_storage("prototype history")
    (data_dir / recovery.LEGACY_HISTORY_NAME).write_text(
        json.dumps([{"entry_id": 1, "text_preview": stored, "types": {"text": stored}}]),
        encoding="utf-8",
    )
    assert reasons(recovery.inspect()) == [("history", "history_identityless")]


def test_a_plaintext_legacy_json_history_is_left_to_the_app(data_dir):
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    (data_dir / recovery.LEGACY_HISTORY_NAME).write_text(
        json.dumps([{"entry_id": 1, "text_preview": "prototype history",
                     "types": {"text": "cHJvdG90eXBl"}}]),
        encoding="utf-8",
    )
    assert recovery.inspect() == []
    assert (data_dir / recovery.LEGACY_HISTORY_NAME).exists()


def test_a_plaintext_legacy_json_does_not_hide_a_keyed_database(data_dir):
    # Both artifacts are asked. A readable file beside a keyed one is still
    # history nobody can open, and adopting it would decrypt nothing.
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    (data_dir / recovery.LEGACY_HISTORY_NAME).write_text(
        json.dumps([{"entry_id": 1, "text_preview": "prototype history"}]), encoding="utf-8"
    )
    write_history(data_dir, rows=2)
    assert reasons(recovery.inspect()) == [("history", "history_identityless")]


def test_an_empty_legacy_json_history_is_not_records(data_dir):
    # The file existing is not the same as it holding something to rescue.
    write_config(data_dir, {"config_version": 2, "encryption_enabled": True, "device_id": "a"})
    (data_dir / recovery.LEGACY_HISTORY_NAME).write_text("[]", encoding="utf-8")
    assert recovery.inspect() == []


def test_records_behind_a_working_identity_are_left_alone(data_dir):
    healthy_config(data_dir)
    write_history(data_dir, rows=3)
    assert recovery.quarantine() == []


def test_a_half_identity_has_to_go(data_dir):
    # A certificate without its key cannot sign, and the app refuses to
    # regenerate one over it — the peers that pinned it would all have to re-pair.
    # Encryption is on, so the history is keyed by the identity being set aside
    # and has to leave with it.
    write_config(data_dir, {
        "config_version": 2, "device_id": "a", "certificate_pem": "cert",
        "encryption_enabled": True,
    })
    write_history(data_dir, rows=2)
    assert reasons(recovery.inspect()) == [
        ("config", "identity"),
        ("history", "history_identity_lost"),
    ]


def test_a_password_without_its_salt_is_left_alone(data_dir):
    # The hash is salted with the certificate fingerprint and records nothing
    # about which one, so "salted with the identity that is gone" — a lock no
    # password opens — is on disk identical to "salted with an empty
    # fingerprint", which the right password opens. A repair that guessed would
    # throw away a working password, and moving the history would discard records
    # the next unlock proves were fine. The app starts locked either way.
    write_config(data_dir, {
        "config_version": 2, "device_id": "a", "encryption_enabled": True,
        "encryption_password_hash": "hash",
    })
    write_history(data_dir, rows=2)
    assert recovery.inspect() == []
    assert recovery.quarantine() == []
    assert (data_dir / recovery.HISTORY_NAME).exists()


def test_an_inert_password_is_not_damage(data_dir):
    # Encryption off means the stale hash cannot lock anything.
    write_config(data_dir, {
        "config_version": 2, "device_id": "a", "encryption_enabled": False,
        "encryption_password_hash": "hash",
    })
    assert recovery.quarantine() == []


def test_a_complete_identity_is_not_damage(data_dir):
    healthy_config(data_dir)
    assert recovery.quarantine() == []
