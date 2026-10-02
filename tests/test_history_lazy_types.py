"""The history's ``types`` column is read on demand, and must stay correct.

Why it is on demand: on this machine's own history the column is 169 MB of a
170 MB file (one image row is 19 MB), so reading it is I/O rather than row work
-- it was 940 ms of a 1080 ms startup, into memory nothing had asked for yet.
Each loaded row now carries a mapping that reads its own column the first time
anything wants a format.

The tests are grouped by the two ways this can go wrong.  The first group is
"does reading still work", which any mistake shows up in immediately.  The
second is the silent half: ``json``, ``copy``, and the union operator read a
dict through its C storage and never call an override, so an entry that reached
a serializer unread would write ``{}`` -- only for the formats, so the row would
still look right.
"""

import base64
import copy
import json
import pickle
import sqlite3
import time

import pytest

from internal.clipboard.dedup import ContentType
from internal.clipboard.history_db import ClipboardHistoryDB

TEXT = base64.b64encode(b"hello").decode("ascii")
IMAGE = base64.b64encode(b"\x89PNG" + b"x" * 64).decode("ascii")


def seed(path, rows):
    """A database with *rows*, written the way the real writer writes them."""
    conn = sqlite3.connect(str(path))
    conn.executescript(ClipboardHistoryDB._SCHEMA)
    for index, types in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO history (entry_id, timestamp, content_type, text_preview, "
            "types, pinned) VALUES (?, ?, 'TEXT', ?, ?, 0)",
            (index, time.time() + index, f"row {index}", json.dumps(types)),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}, {"TEXT": TEXT, "IMAGE": IMAGE}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    yield handle
    handle.close()


def row_with(db, preview):
    return next(entry for entry in db.get_all() if entry["text_preview"] == preview)


# ── reading still works ────────────────────────────────────────────────


def test_a_loaded_row_has_not_read_its_payloads_yet(db):
    # The whole point: the column is not read, for any row.
    assert all(entry["types"].hydrated is False for entry in db.get_all())


def test_asking_for_a_format_reads_that_row_only(db):
    first = row_with(db, "row 1")
    second = row_with(db, "row 2")
    assert first["types"]["TEXT"] == TEXT
    assert first["types"].hydrated is True
    # The row nobody asked about is still unread.
    assert second["types"].hydrated is False


def test_the_mapping_reads_like_a_dict(db):
    types = row_with(db, "row 2")["types"]
    assert types.get("TEXT") == TEXT
    assert types.get("MISSING") is None
    assert types.get("MISSING", "fallback") == "fallback"
    assert "IMAGE" in types
    assert "NOPE" not in types
    assert len(types) == 2
    assert bool(types) is True
    assert sorted(types.keys()) == ["IMAGE", "TEXT"]
    assert sorted(types.values()) == sorted([TEXT, IMAGE])
    assert sorted(types.items()) == sorted([("TEXT", TEXT), ("IMAGE", IMAGE)])
    assert sorted(iter(types)) == ["IMAGE", "TEXT"]
    # And it still is one: callers do check.
    assert isinstance(types, dict)


def test_an_empty_or_corrupt_row_reads_as_no_formats(tmp_path):
    path = tmp_path / "h.db"
    seed(path, [{}])
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE history SET types = '{corrupt' WHERE entry_id = 1")
    conn.commit()
    conn.close()
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        entry = handle.get_all()[0]
        assert entry["types"] == {}
        assert entry["types"].get("TEXT") is None
        assert len(entry["types"]) == 0
        # Reading an empty mapping does not leave it needing a second look.
        assert entry["types"].hydrated is True
    finally:
        handle.close()


def test_a_row_deleted_before_it_is_read_answers_with_nothing(tmp_path):
    # The window between the load and the read is real: another thread can
    # delete the row.  An empty mapping is what every caller already handles.
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        entry = handle.get_all()[0]
        handle.delete_by_id(entry["entry_id"])
        assert dict(entry["types"]) == {}
    finally:
        handle.close()


def test_the_entry_still_reads_as_a_dict_for_the_deduplicator(db):
    # `_stored_text_key` rebuilds a dedup key out of an entry's stored formats,
    # and the capture path calls it on the top row while holding the lock.
    entry = row_with(db, "row 1")
    assert entry["types"]["TEXT"] == TEXT
    assert db._stored_text_key(entry).startswith("text:")


def test_a_second_read_does_not_go_back_to_the_database(db, monkeypatch):
    types = row_with(db, "row 1")["types"]
    dict(types)
    calls = []
    monkeypatch.setattr(db, "_read_types", lambda entry_id: calls.append(entry_id) or {})
    assert types["TEXT"] == TEXT
    assert types.items() is not None
    assert calls == []


# ── the silent half: paths that skip overrides ─────────────────────────


def test_the_mapping_compares_and_copies_as_the_mapping_it_holds(db):
    types = row_with(db, "row 2")["types"]
    # `==` is wrapped, because `dict` compares through C storage otherwise and
    # an unread mapping would equal nothing at all -- not even `{}`.
    assert types == {"TEXT": TEXT, "IMAGE": IMAGE}
    # `!=` is a separate method here (it does not fall back to `not __eq__` for a
    # subclass), so both directions are exercised -- on a row that is still
    # unread, because that is where the C-level comparison went wrong.
    other = row_with(db, "row 1")["types"]
    assert other != {"TEXT": TEXT, "IMAGE": IMAGE}
    assert other == {"TEXT": TEXT}
    assert copy.copy(types) == {"TEXT": TEXT, "IMAGE": IMAGE}
    assert copy.deepcopy(types) == {"TEXT": TEXT, "IMAGE": IMAGE}
    assert types.copy() == {"TEXT": TEXT, "IMAGE": IMAGE}
    assert types | {"X": "y"} == {"TEXT": TEXT, "IMAGE": IMAGE, "X": "y"}
    assert {"X": "y"} | types == {"TEXT": TEXT, "IMAGE": IMAGE, "X": "y"}
    assert {**types} == {"TEXT": TEXT, "IMAGE": IMAGE}
    # And each of those returned a plain dict, not another lazy mapping.
    assert type(copy.deepcopy(types)) is dict


def test_pickling_a_loaded_row_keeps_its_formats(db):
    entry = row_with(db, "row 1")
    revived = pickle.loads(pickle.dumps(entry))
    assert revived["types"] == {"TEXT": TEXT}


def test_json_sees_an_unread_mapping_as_empty_which_is_why_nothing_serialises_it(db):
    """The one thing this class cannot do, pinned as a fact.

    `json.dumps` serialises a dict through its C storage, so it never calls an
    override and an unread mapping writes `{}`.  That is silent and it is only
    the formats, so a row would still look right.  It is why no path that
    serialises an entry may be handed a raw loaded row, and this test is here so
    the day that changes, it fails loudly instead of writing empty payloads into
    somebody's backup.

    Both halves: the hazard is real, and reading first is the fix.
    """
    entry = row_with(db, "row 1")
    assert json.dumps(entry["types"]) == "{}"
    assert entry["types"]["TEXT"] == TEXT  # what every caller does first
    assert json.loads(json.dumps(entry))["types"] == {"TEXT": TEXT}
    # `dict(...)` is the explicit form of the same thing.
    fresh = row_with(db, "row 2")
    assert json.dumps(dict(fresh["types"])) != "{}"


def test_every_export_and_backup_path_reads_the_formats_before_serialising(db):
    """The paths that do serialise an entry, exercised on a loaded one.

    Not a mock of them: they are the two functions that turn a stored row into a
    file, and both go through ``_decode_types`` / ``entry.get("types")``, which
    is what hydrates.  If either were rewritten to reach past the mapping, the
    export would lose every payload and only this test would notice.
    """
    from internal.data.export import _decode_types

    entry = row_with(db, "row 2")
    decoded = _decode_types(entry)
    assert decoded["TEXT"] == "hello"
    # A binary payload is kept as a marked wrapper rather than corrupted.
    assert decoded["IMAGE"] == {"_b64": IMAGE}


# ── writing ────────────────────────────────────────────────────────────


def test_writing_through_the_mapping_reads_it_first(db):
    # A merge in the capture path mutates the stored formats in place; the row
    # has to be there before the mutation or the write would replace it with the
    # one key being set.
    entry = row_with(db, "row 2")
    entry["types"]["EXTRA"] = "value"
    assert entry["types"] == {"TEXT": TEXT, "IMAGE": IMAGE, "EXTRA": "value"}
    assert entry["types"]["TEXT"] == TEXT


def test_a_merged_row_survives_a_restart(tmp_path):
    """The capture path's merge, end to end, with the payloads never pre-read."""
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        entry = handle.get_all()[0]
        assert entry["types"].hydrated is False
        # What `_merge_into_top` does: read the union, write it back.
        merged = dict(entry["types"])
        merged["IMAGE"] = IMAGE
        entry["types"] = merged
        handle._persist_entry_update(entry)
    finally:
        handle.close()

    reopened = ClipboardHistoryDB(storage_path=str(path))
    try:
        stored = reopened.get_all()[0]
        assert stored["types"]["TEXT"] == TEXT
        assert stored["types"]["IMAGE"] == IMAGE
    finally:
        reopened.close()


def test_clearing_and_trimming_do_not_leave_a_readable_row_behind(tmp_path):
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}, {"TEXT": TEXT, "IMAGE": IMAGE}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        assert len(handle.get_all()) == 2
        handle.clear()
        assert handle.get_all() == []
        # A fresh row afterwards is readable, so the table really was reset.
        from internal.clipboard.clipboard import ClipboardContent

        handle.add(ClipboardContent(types={ContentType.TEXT: b"after clear"}))
        entry = handle.get_all()[0]
        assert base64.b64decode(entry["types"]["TEXT"]) == b"after clear"
    finally:
        handle.close()


def test_a_full_save_writes_every_row_it_was_given(tmp_path):
    # `_save` is the bulk path (import/restore).  It walks every entry, so it
    # reads every payload -- correct, and the one place where that is the right
    # answer.  It is also the path that caught this change: the row builder used
    # to hand the lazy mapping straight to `json.dumps`, which serialises a dict
    # through its C storage, so a full save wrote `{}` into every row's formats.
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}, {"TEXT": TEXT, "IMAGE": IMAGE}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        handle._save()
    finally:
        handle.close()
    reopened = ClipboardHistoryDB(storage_path=str(path))
    try:
        assert sorted(len(entry["types"]) for entry in reopened.get_all()) == [1, 2]
    finally:
        reopened.close()


def test_a_full_save_reads_its_rows_before_it_clears_the_table(tmp_path):
    """The ordering inside `_save`, which is load-bearing rather than tidy.

    A row is built by reading the entry's payloads, and a loaded entry reads its
    payloads *from this table* -- so clearing the table first meant the read
    found nothing and every row was written with `{}` for its formats.  That
    shape only worked while the payloads happened to be in memory already, which
    is exactly what reading them on demand stopped being true.

    Asserted on the file rather than on a mock, because the failure is silent:
    the save reports success, the row count is unchanged, and only the payloads
    are gone.  `import_history` and `restore` both come through here.
    """
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}, {"TEXT": TEXT, "IMAGE": IMAGE}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        assert all(entry["types"].hydrated is False for entry in handle.get_all())
        handle._save()
        # Nothing was pre-read by the test, so every payload had to come back
        # out of the table after the generator ran.
        assert all(entry["types"].hydrated is True for entry in handle.get_all())
    finally:
        handle.close()

    conn = sqlite3.connect(str(path))
    stored = dict(conn.execute("SELECT entry_id, types FROM history"))
    conn.close()
    assert json.loads(stored[1]) == {"TEXT": TEXT}
    assert json.loads(stored[2]) == {"TEXT": TEXT, "IMAGE": IMAGE}


def test_an_import_round_trip_keeps_the_payloads(tmp_path):
    """The path a user actually takes: export a history, import it into another.

    Importing back into the history it came from is a no-op by design -- the
    duplicate check matches preview-and-timestamp -- so the target here is a
    fresh database, which is the restore case the path exists for.
    """
    from internal.data.export import export_history_json, import_history_json

    source_path = tmp_path / "source.db"
    seed(source_path, [{"TEXT": TEXT}, {"TEXT": TEXT, "IMAGE": IMAGE}])
    source = ClipboardHistoryDB(storage_path=str(source_path))
    target = ClipboardHistoryDB(storage_path=str(tmp_path / "target.db"))
    try:
        exported = tmp_path / "out.json"
        assert export_history_json(source, str(exported)) == 2
        assert import_history_json(str(exported), target) == 2
        by_preview = {entry["text_preview"]: entry for entry in target.get_all()}
        # A text payload survives as text; a binary one comes back byte-exact
        # through the `{"_b64": ...}` wrapper the export uses for it.
        assert by_preview["row 1"]["types"]["TEXT"] == TEXT
        assert by_preview["row 2"]["types"]["IMAGE"] == IMAGE
    finally:
        source.close()
        target.close()

    # And it is on disk in the target, not only in that process's memory.
    reopened = ClipboardHistoryDB(storage_path=str(tmp_path / "target.db"))
    try:
        by_preview = {entry["text_preview"]: entry for entry in reopened.get_all()}
        assert by_preview["row 2"]["types"]["IMAGE"] == IMAGE
    finally:
        reopened.close()


def test_the_lazy_mapping_is_unhashable_like_the_dict_it_replaces(db):
    # `dict` sets `__hash__` to None, and a subclass that overrides `__eq__`
    # without saying so inherits `object.__hash__` instead -- which would make a
    # loaded row's formats hashable while a plain dict's are not.
    types = row_with(db, "row 1")["types"]
    with pytest.raises(TypeError):
        hash(types)


def test_repr_does_not_read_the_row(db):
    # Printing an entry must not pull 19 MB into memory, which is also why the
    # repr names the state instead of the contents.
    entry = row_with(db, "row 2")
    assert "not read yet" in repr(entry["types"])
    assert entry["types"].hydrated is False
    dict(entry["types"])
    assert repr(entry["types"]) == repr({"TEXT": TEXT, "IMAGE": IMAGE})


def test_the_encryption_path_opens_metadata_without_the_payloads(tmp_path):
    """A loaded row's text columns are decrypted; its payloads are not read."""
    path = tmp_path / "h.db"
    seed(path, [{"TEXT": TEXT}])
    handle = ClipboardHistoryDB(storage_path=str(path))
    try:
        entry = handle.get_all()[0]
        # Without an encryption manager this is a no-op that must still leave the
        # payload unread -- it is called on every load.
        handle._decrypt_metadata([entry])
        assert entry["types"].hydrated is False
        assert entry["text_preview"] == "row 1"
    finally:
        handle.close()
