"""A history write that the disk refused must not come back as success.

Every incremental write used to catch its ``sqlite3`` error, log it and return
``None``.  ``add()`` still handed back an entry id (the id a file offer names),
``delete()`` still answered True, and a pin still reported success — while the
database kept the opposite state, which the next start read back.  These tests
inject that failure and check three things at once: the call raises, memory is
unchanged, and a restart sees what memory said.
"""

from __future__ import annotations

import sqlite3

import pytest

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB, HistoryWriteError


class FailingConnection:
    """A connection whose every statement fails, like a disk that went away."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    def executemany(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def _content(text: str) -> ClipboardContent:
    return ClipboardContent(types={ContentType.TEXT: text.encode()})


@pytest.fixture()
def db(tmp_path):
    store = ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"))
    yield store
    store.close()


@pytest.fixture()
def broken(db):
    """Break the connection for the duration of a test."""
    real = db._conn
    db._conn = FailingConnection(real)
    yield db
    db._conn = real


def test_add_raises_and_leaves_no_row_behind(broken):
    with pytest.raises(HistoryWriteError):
        broken.add(_content("second"))
    assert broken.get_all() == []
    assert broken._next_id == 0, "a refused insert must not consume an id"


def test_delete_reports_failure(db):
    first = db.add(_content("first"))
    db.add(_content("second"))
    real = db._conn
    db._conn = FailingConnection(real)
    try:
        with pytest.raises(HistoryWriteError):
            db.delete_by_id(first)
    finally:
        db._conn = real
    assert len(db.get_all()) == 2, "the row the disk kept must stay in memory"


@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.pin(0),
        lambda db: db.unpin(0),
        lambda db: db.batch_set_pinned([db.get_all()[0]["entry_id"]], True),
        lambda db: db.batch_delete([db.get_all()[0]["entry_id"]]),
        lambda db: db.touch(db.get_all()[0]["entry_id"]),
        lambda db: db.increment_paste(db.get_all()[0]["entry_id"]),
        lambda db: db.clear(),
    ],
)
def test_every_mutation_reports_the_failure(db, call):
    db.add(_content("first"))
    db.add(_content("second"))
    real = db._conn
    db._conn = FailingConnection(real)
    try:
        with pytest.raises(HistoryWriteError):
            call(db)
    finally:
        db._conn = real
    assert len(db.get_all()) == 2
    assert db.get_all()[0]["pinned"] is False


def test_a_restart_agrees_with_memory(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path)
    db.add(_content("first"))
    real = db._conn
    db._conn = FailingConnection(real)
    with pytest.raises(HistoryWriteError):
        db.add(_content("second"))
    db._conn = real
    db.close()

    reopened = ClipboardHistoryDB(storage_path=path)
    try:
        assert [row["text_preview"] for row in reopened.get_all()] == ["first"]
    finally:
        reopened.close()
