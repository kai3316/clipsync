import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from internal.application.errors import ApplicationError
from internal.application.use_cases.favorites import FavoritesUseCase
from internal.infrastructure.persistence.favorites import FavoritesRepository
from internal.web.api import favorites as web


@pytest.fixture
def repository(tmp_path):
    return FavoritesRepository(tmp_path / "favorites.db")


@pytest.fixture
def use_case(repository):
    return FavoritesUseCase(repository)


def test_crud_and_clipboard(repository):
    writes = []
    service = FavoritesUseCase(repository, lambda text: writes.append(text) is None)
    entry = service.add(" title ", "complete " * 100, " work ")["favorite"]
    assert entry["title"] == "title"
    assert entry["group"] == "work"
    assert entry["updated"] is None
    assert service.get(entry["id"]) == {"favorite": entry}
    assert service.copy(entry["id"]) == {"copied": True}
    assert writes == [entry["content"]]
    changed = service.update(entry["id"], title="new", position=5)["favorite"]
    assert changed["created"] == entry["created"]
    assert changed["content"] == entry["content"]
    assert changed["updated"] >= entry["created"]
    assert service.delete(entry["id"]) == {"deleted": True}
    with pytest.raises(ApplicationError, match="no longer exists"):
        service.get(entry["id"])


def test_list_filter_order_and_preview(use_case):
    first = use_case.add("Alpha", "x" * 300, "one")["favorite"]
    second = use_case.add("Beta", "needle", "two")["favorite"]
    result = use_case.list()
    assert result["groups"] == ["one", "two"]
    assert result["total"] == 2
    assert len(result["items"][0]["preview"]) == 256
    assert "content" not in result["items"][0]
    assert use_case.list(query="NEEDLE")["items"][0]["id"] == second["id"]
    assert use_case.list(group="one")["items"][0]["id"] == first["id"]
    assert use_case.list(offset=1, limit=1)["items"][0]["id"] == second["id"]
    assert use_case.list(offset=1000000)["items"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "q" * 513},
        {"group": "g" * 129},
        {"offset": -1},
        {"offset": 1000001},
        {"offset": True},
        {"limit": 0},
        {"limit": 101},
        {"limit": 1.5},
        {"query": None},
    ],
)
def test_list_limits(use_case, arguments):
    with pytest.raises(ApplicationError) as error:
        use_case.list(**arguments)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "t" * 257, "content": ""},
        {"title": "", "content": "c" * 65537},
        {"title": "ok", "content": "", "group": "g" * 129},
        {"title": " ", "content": ""},
        {"title": 4, "content": ""},
    ],
)
def test_add_limits(use_case, arguments):
    with pytest.raises(ApplicationError) as error:
        use_case.add(**arguments)
    assert error.value.code == "INVALID_ARGUMENT"
    assert use_case.list()["total"] == 0


def test_boundaries_and_update_rollback(use_case):
    entry = use_case.add("t" * 256, "c" * 65536, "g" * 128)["favorite"]
    changed = use_case.update(entry["id"], position=1000000)["favorite"]
    for position in (-1, 1000001, True, 0.5):
        with pytest.raises(ApplicationError):
            use_case.update(entry["id"], position=position)
    with pytest.raises(ApplicationError):
        use_case.update(entry["id"], title="", content="")
    assert use_case.get(entry["id"])["favorite"] == changed
    with pytest.raises(ApplicationError):
        use_case.add("position overflow", "")


@pytest.mark.parametrize("favorite_id", ["", "x" * 65, None, 4, " "])
def test_id_limits(use_case, favorite_id):
    for method in (use_case.get, use_case.delete, use_case.copy, use_case.update):
        with pytest.raises(ApplicationError) as error:
            method(favorite_id)
        assert error.value.code == "INVALID_ARGUMENT"


def test_legacy_json_preserved_and_paths_isolated(repository, tmp_path):
    legacy = [
        {
            "id": "original-id",
            "title": "title",
            "content": "full\ncontent",
            "group": "group",
            "position": 8,
            "created": 123.5,
            "updated": 456.5,
        }
    ]
    source = json.dumps(legacy)
    repository.json_path.write_text(source, encoding="utf-8")
    assert repository.get_all() == legacy
    assert repository.json_path.read_text(encoding="utf-8") == source
    assert FavoritesRepository(repository.db_path).get_all() == legacy
    assert FavoritesRepository(tmp_path / "other" / "favorites.db").get_all() == []


def test_old_schema_position_migration(repository):
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute(
            "CREATE TABLE favorites (id TEXT PRIMARY KEY, title TEXT, content TEXT, "
            '"group" TEXT, created REAL, updated REAL)'
        )
        conn.execute("INSERT INTO favorites VALUES ('a', 't', 'full', 'g', 1.5, 2.5)")
    assert repository.get("a") == dict(
        id="a", title="t", content="full", group="g", created=1.5, updated=2.5, position=0
    )


@pytest.mark.parametrize("source", ["{bad", "{}", '[{"id":"a"}, {"id":"a"}]'])
def test_bad_legacy_migration_fails_atomically(repository, source):
    repository.json_path.write_text(source, encoding="utf-8")
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository).list()
    assert error.value.code == "STORAGE_ERROR"
    assert repository.json_path.read_text(encoding="utf-8") == source
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0


def test_corrupt_database_not_reset(repository, monkeypatch, tmp_path):
    raw = b"not a sqlite database - private content"
    repository.db_path.write_bytes(raw)
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository).list()
    assert error.value.code == "STORAGE_ERROR"
    assert "private" not in str(error.value)
    assert repository.db_path.read_bytes() == raw
    monkeypatch.setattr(web, "_FAV_DB_PATH", str(repository.db_path))
    monkeypatch.setattr(web, "_config_dir", lambda: str(tmp_path))
    assert web.get_favorites()[1] == 500
    assert web.export_favorites(b"{}", dest_dir=tmp_path)[1] == 500
    assert repository.db_path.read_bytes() == raw


def test_oversized_legacy_never_saved_as_preview(repository):
    entry = repository.add("legacy", "x" * 65537, "")
    service = FavoritesUseCase(repository)
    assert len(service.list()["items"][0]["preview"]) == 256
    for action in (
        lambda: service.get(entry["id"]),
        lambda: service.copy(entry["id"]),
        lambda: service.update(entry["id"], content="x" * 256),
        lambda: service.update(entry["id"], title="changed"),
    ):
        with pytest.raises(ApplicationError) as error:
            action()
        assert error.value.code == "DATA_INVALID"
    assert repository.get(entry["id"]) == entry


@pytest.mark.parametrize("writer", [None, lambda _: False, lambda _: 1])
def test_copy_failure(repository, writer):
    entry = repository.add("title", "content", "")
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository, writer).copy(entry["id"])
    assert error.value.code in ("NOT_SUPPORTED", "CLIPBOARD_WRITE_FAILED")


def test_copy_exception_is_redacted(repository):
    def writer(_):
        raise RuntimeError("secret clipboard content")

    entry = repository.add("title", "content", "")
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository, writer).copy(entry["id"])
    assert error.value.code == "CLIPBOARD_WRITE_FAILED"
    assert "secret" not in str(error.value)


def test_invalid_metadata_is_explicit(repository):
    entry = repository.add("title", "content", "")
    with repository.transaction() as conn:
        conn.execute("UPDATE favorites SET created = ?", ("private",))
    service = FavoritesUseCase(repository)
    for action in (service.list, lambda: service.get(entry["id"])):
        with pytest.raises(ApplicationError) as error:
            action()
        assert error.value.code == "DATA_INVALID"


def test_custom_json_path_and_maximum_id(tmp_path):
    legacy = tmp_path / "legacy" / "clips.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps([{"id": "a" * 64, "content": "whole"}]), encoding="utf-8")
    repository = FavoritesRepository(tmp_path / "db" / "favorites.db", legacy)
    assert FavoritesUseCase(repository).get("a" * 64)["favorite"]["content"] == "whole"


def test_parallel_append_positions_and_safe_close(repository):
    repository.initialize()

    def add(index):
        return FavoritesRepository(repository.db_path).add(str(index), "text", "")["position"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(add, range(20))) == list(range(20))
    # Windows refuses this rename when a connection is leaked.
    moved = repository.db_path.with_name("moved.db")
    repository.db_path.rename(moved)
    moved.rename(repository.db_path)


def test_http_compatibility_and_shared_store(repository, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_FAV_DB_PATH", None)
    monkeypatch.setattr(web, "_config_dir", lambda: str(tmp_path))
    body, status = web.add_favorite(b'{"title":"old","content":"complete","group":"g"}')
    assert status == 200
    entry = body["favorite"]
    assert "updated" not in entry
    service = FavoritesUseCase(repository)
    assert service.get(entry["id"])["favorite"]["content"] == "complete"
    assert web.update_favorite(json.dumps({"id": entry["id"], "position": "4"}).encode())[1] == 200
    assert service.get(entry["id"])["favorite"]["position"] == 4
    exported, status = web.export_favorites(b'{"format":"markdown"}', dest_dir=tmp_path)
    assert status == 200
    assert exported["count"] == 1
    assert web.delete_favorite(json.dumps({"id": entry["id"]}).encode()) == ({"ok": True}, 200)
    assert service.list()["total"] == 0


def test_legacy_delete_all_reopen_does_not_resurrect(repository):
    source = json.dumps([{"id": "legacy", "title": "old", "content": "full"}])
    repository.json_path.write_text(source, encoding="utf-8")
    assert repository.maybe_migrate() == 1
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert repository.delete("legacy")
    reopened = FavoritesRepository(repository.db_path)
    assert reopened.maybe_migrate() == 0
    assert reopened.get_all() == []
    assert repository.json_path.read_text(encoding="utf-8") == source


def test_existing_database_marks_legacy_adoption_complete(repository):
    repository.ensure_db()
    with repository.transaction() as conn:
        conn.execute(
            'INSERT INTO favorites (id, title, content, "group", position, created, updated) '
            "VALUES ('existing', 'keep', 'complete', 'group', 7, 123, 456)"
        )
    # SQLite is authoritative, even if the legacy source is unreadable.
    repository.json_path.write_text("{invalid legacy", encoding="utf-8")
    assert repository.maybe_migrate() == 0
    assert repository.get("existing") == dict(
        id="existing",
        title="keep",
        content="complete",
        group="group",
        position=7,
        created=123,
        updated=456,
    )
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert repository.delete("existing")
    assert FavoritesRepository(repository.db_path).get_all() == []
    assert repository.json_path.read_text(encoding="utf-8") == "{invalid legacy"


@pytest.mark.parametrize("source", [None, "[]"])
def test_absent_or_empty_legacy_source_marks_completion(repository, source):
    if source is not None:
        repository.json_path.write_text(source, encoding="utf-8")
    assert repository.maybe_migrate() == 0
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    repository.json_path.write_text('[{"id":"late", "content":"late"}]', encoding="utf-8")
    assert FavoritesRepository(repository.db_path).get_all() == []


@pytest.mark.parametrize("source", ["{invalid", '[{"id":"duplicate"}, {"id":"duplicate"}]'])
def test_failed_migration_can_retry_without_completion_marker(repository, source):
    repository.json_path.write_text(source, encoding="utf-8")
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        repository.maybe_migrate()
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0
    assert repository.json_path.read_text(encoding="utf-8") == source
    repaired = '[{"id":"recovered", "content":"whole"}]'
    repository.json_path.write_text(repaired, encoding="utf-8")
    assert repository.maybe_migrate() == 1
    assert FavoritesRepository(repository.db_path).get("recovered")["content"] == "whole"
    assert repository.json_path.read_text(encoding="utf-8") == repaired
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_http_delete_final_legacy_favorite_does_not_resurrect(repository, monkeypatch, tmp_path):
    source = '[{"id":"legacy", "content":"original"}]'
    repository.json_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(web, "_FAV_DB_PATH", str(repository.db_path))
    monkeypatch.setattr(web, "_config_dir", lambda: str(tmp_path))
    assert web.get_favorites()[0]["favorites"][0]["id"] == "legacy"
    assert web.delete_favorite(b'{"id":"legacy"}') == ({"ok": True}, 200)
    assert web.get_favorites() == ({"favorites": []}, 200)
    assert web.export_favorites(b"{}", dest_dir=tmp_path)[0]["count"] == 0
    added, status = web.add_favorite(b'{"title":"new","content":"new"}')
    assert status == 200
    assert [entry["id"] for entry in web.get_favorites()[0]["favorites"]] == [
        added["favorite"]["id"]
    ]
    assert repository.json_path.read_text(encoding="utf-8") == source


@pytest.mark.parametrize("version", [2, 999, -1])
def test_unknown_database_version_rejected_without_changes(repository, version):
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute(f"PRAGMA user_version={version}")
    before = repository.db_path.read_bytes()
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository).list()
    assert error.value.code == "STORAGE_ERROR"
    assert repository.db_path.read_bytes() == before
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='favorites'").fetchone() is None
        )


def test_batch_add_keeps_full_text_and_selection_order(repository):
    full = "Full clip content <b>not markup</b>. " * 30
    entries = {
        "a": {"types": {"TEXT": base64.b64encode(full.encode()).decode()},
              "text_preview": "preview-a"},
        "b": {"types": {"TEXT": base64.b64encode(b"second").decode()},
              "text_preview": "preview-b"},
    }
    service = FavoritesUseCase(repository, history_lookup=entries.get)
    result = service.batch_add(["a", "b"], "Work")
    assert result["added"] == 2
    stored = repository.get_all()
    assert [entry["id"] for entry in stored] == result["ids"]
    assert [entry["content"] for entry in stored] == [full, "second"]
    assert stored[0]["title"] == full[:50]
    assert stored[0]["group"] == "Work"
    assert [entry["position"] for entry in stored] == [0, 1]


def test_batch_add_skips_unknown_and_undecodable_entries(repository):
    service = FavoritesUseCase(repository, history_lookup=lambda entry_id: {
        "gone": None,
        "empty": {"types": {}, "text_preview": ""},
        "bad": {"types": {"TEXT": "***"}, "text_preview": "fallback"},
    }.get(entry_id))
    result = service.batch_add(["gone", "empty", "bad"])
    assert result["added"] == 2
    stored = repository.get_all()
    assert [entry["content"] for entry in stored] == ["", "fallback"]
    assert stored[1]["title"] == "fallback"


@pytest.mark.parametrize("entry_ids", [[], ["a"] * 101, ["a", "a"], [""], ["x" * 65], [7]])
def test_batch_add_rejects_invalid_ids(repository, entry_ids):
    service = FavoritesUseCase(repository, history_lookup=lambda entry_id: None)
    with pytest.raises(ApplicationError) as error:
        service.batch_add(entry_ids)
    assert error.value.code == "INVALID_ARGUMENT"


def test_batch_add_without_history_lookup_is_not_supported(repository):
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository).batch_add(["a"])
    assert error.value.code == "NOT_SUPPORTED"


@pytest.mark.parametrize("fmt,suffix", [("markdown", ".md"), ("text", ".txt")])
def test_export_writes_every_favorite_and_reports_the_path(tmp_path, repository, fmt, suffix):
    service = FavoritesUseCase(repository, export_dir=tmp_path)
    service.add("Alpha", "body ` with backtick", "one")
    service.add("Beta", "second", "two")
    result = service.export(format=fmt, dest_dir=tmp_path)
    assert result["format"] == fmt
    assert result["count"] == 2
    assert result["filename"].startswith("clipsync-favorites-")
    assert result["filename"].endswith(suffix)
    written = (tmp_path / result["filename"]).read_text(encoding="utf-8")
    assert "Alpha" in written and "Beta" in written
    assert "body ` with backtick" in written


def test_export_rejects_unknown_format(repository):
    with pytest.raises(ApplicationError) as error:
        FavoritesUseCase(repository).export("pdf")
    assert error.value.code == "INVALID_ARGUMENT"


def test_initialized_repository_rejects_future_version(repository):
    entry = repository.add("title", "content", "")
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="Unsupported"):
        repository.delete(entry["id"])
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT content FROM favorites").fetchone()[0] == "content"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def _phone_store(tmp_path, monkeypatch):
    """Point the web favourites API at this test's own config dir."""
    monkeypatch.setattr(web, "_FAV_DB_PATH", None)
    monkeypatch.setattr(web, "_config_dir", lambda: str(tmp_path))


def test_the_phone_batch_body_applies_every_position_at_once(tmp_path, monkeypatch):
    """One request carries the phone's whole drag.

    The route took a single favourite, so a reorder was one PATCH per moved
    item — one round trip each on a phone, and one published snapshot each
    once a favourite write tells the host.  The batch body is additive: the
    single-favourite contract above is untouched.
    """
    _phone_store(tmp_path, monkeypatch)
    ids = [
        web.add_favorite(json.dumps({"title": t, "content": t}).encode())[0]["favorite"]["id"]
        for t in ("a", "b", "c")
    ]
    body, status = web.update_favorites(json.dumps({"updates": [
        {"id": ids[0], "position": 2},
        {"id": ids[1], "position": 0},
        {"id": ids[2], "position": 1},
    ]}).encode())
    assert (body, status) == ({"ok": True, "updated": 3}, 200)
    assert [f["title"] for f in web.get_favorites()[0]["favorites"]] == ["b", "c", "a"]


def test_the_batch_body_carries_a_group_change_too(tmp_path, monkeypatch):
    """A group rename is the same gesture as a drag, so it is the same body.

    The panel renamed a group by patching every member — the same burst the
    batch body exists to remove — which is why the body is a bag of fields
    rather than a list of positions.  One entry changes two fields at once.
    """
    _phone_store(tmp_path, monkeypatch)
    ids = [
        web.add_favorite(
            json.dumps({"title": t, "content": t, "group": group}).encode()
        )[0]["favorite"]["id"]
        for t, group in (("a", "work"), ("b", "work"), ("c", "home"))
    ]
    body, status = web.update_favorites(json.dumps({"updates": [
        {"id": ids[0], "group": "job", "position": 7},
        {"id": ids[1], "group": "job"},
    ]}).encode())
    assert (body, status) == ({"ok": True, "updated": 2}, 200)
    stored = {f["id"]: f for f in web.get_favorites()[0]["favorites"]}
    assert (stored[ids[0]]["group"], stored[ids[0]]["position"]) == ("job", 7)
    assert stored[ids[1]]["group"] == "job"
    assert stored[ids[2]]["group"] == "home"


def test_the_batch_body_can_empty_a_group(tmp_path, monkeypatch):
    """Deleting a group is the same body with an empty group.

    An empty string is a *change*, not a missing field: a body that dropped it
    would leave a deleted group's favourites still in it.
    """
    _phone_store(tmp_path, monkeypatch)
    favorite_id = web.add_favorite(
        b'{"title":"a","content":"a","group":"work"}'
    )[0]["favorite"]["id"]
    body, status = web.update_favorites(
        json.dumps({"updates": [{"id": favorite_id, "group": ""}]}).encode()
    )
    assert (body, status) == ({"ok": True, "updated": 1}, 200)
    assert web.get_favorites()[0]["favorites"][0]["group"] == ""


def test_the_single_favourite_body_still_goes_through(tmp_path, monkeypatch):
    _phone_store(tmp_path, monkeypatch)
    favorite_id = web.add_favorite(b'{"title":"one","content":"one"}')[0]["favorite"]["id"]
    assert web.update_favorites(
        json.dumps({"id": favorite_id, "position": 4}).encode()
    )[1] == 200
    assert web.get_favorites()[0]["favorites"][0]["position"] == 4


def test_a_batch_naming_a_missing_favourite_still_applies_the_rest(tmp_path, monkeypatch):
    """A favourite deleted mid-gesture is not there to change; the rest land."""
    _phone_store(tmp_path, monkeypatch)
    ids = [
        web.add_favorite(json.dumps({"title": t, "content": t}).encode())[0]["favorite"]["id"]
        for t in ("a", "b")
    ]
    body, status = web.update_favorites(json.dumps({"updates": [
        {"id": "gone", "position": 0},
        {"id": ids[1], "position": 5},
    ]}).encode())
    assert (body, status) == ({"ok": True, "updated": 1}, 200)
    stored = web.get_favorites()[0]["favorites"]
    assert [f["id"] for f in stored] == ids
    assert stored[1]["position"] == 5


@pytest.mark.parametrize(
    "updates",
    [
        [],
        "a, b",
        [{"position": 0}],
        [{"id": "", "position": 0}],
        [{"id": "x"}],
        [{"id": "x", "position": "0"}],
        [{"id": "x", "position": -1}],
        [{"id": "x", "position": True}],
        [{"id": "x", "position": 1.5}],
        [{"id": "x", "title": 3}],
        [{"id": "x", "position": 0}, {"id": "x", "position": 1}],
        [{"id": " x ", "position": 0}, {"id": "x", "position": 1}],
        [{"id": "x", "position": 0}, "y"],
    ],
)
def test_a_malformed_batch_is_refused(updates, tmp_path, monkeypatch):
    _phone_store(tmp_path, monkeypatch)
    web.add_favorite(b'{"title":"kept","content":"kept"}')
    body, status = web.update_favorites(json.dumps({"updates": updates}).encode())
    assert status == 400 and body["ok"] is False
    # Nothing was touched on the way to the refusal.
    assert web.get_favorites()[0]["favorites"][0]["position"] == 0


def test_a_batch_naming_nothing_stored_is_a_404(tmp_path, monkeypatch):
    _phone_store(tmp_path, monkeypatch)
    body, status = web.update_favorites(
        json.dumps({"updates": [{"id": "gone", "position": 0}]}).encode()
    )
    assert status == 404 and body["ok"] is False


def test_a_malformed_body_still_answers_invalid_json(tmp_path, monkeypatch):
    _phone_store(tmp_path, monkeypatch)
    assert web.update_favorites(b"{not json") == ({"ok": False, "error": "invalid json"}, 400)
