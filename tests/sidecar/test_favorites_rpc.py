from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from internal.adapters.sidecar.rpc import Dispatcher
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.clipboard.format import ClipboardContent, ContentType
from internal.config.config import Config, save
from internal.infrastructure.security.device_identity import prepare_identity


@pytest.fixture
def favorite_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False))
    writer = SimpleNamespace(write=Mock(return_value=True))
    app = SidecarApplication(clipboard_writer_factory=lambda: writer)
    app.lifecycle.start()
    yield app, writer
    assert app.lifecycle.stop()


def test_favorite_commands_preserve_full_content_and_copy(favorite_app):
    app, writer = favorite_app
    dispatcher = Dispatcher(app)
    full = "Favorite text with rich-looking <b>markup</b>. " * 50
    entry = dispatcher.call("favorites.add", {
        "title": "Title", "content": full, "group": "Work",
    })["favorite"]
    listing = dispatcher.call("favorites.list", {})
    assert listing["session_id"] == app.events.session_id
    assert listing["total"] == 1
    assert listing["items"][0]["preview"] == full[:256]
    assert "content" not in listing["items"][0]
    assert dispatcher.call("favorites.get", {"favorite_id": entry["id"]}) == {"favorite": entry}
    assert dispatcher.call("favorites.copy", {"favorite_id": entry["id"]}) == {"copied": True}
    assert writer.write.call_args.args[0].types == {ContentType.TEXT: full.encode()}
    changed = dispatcher.call("favorites.update", {
        "favorite_id": entry["id"], "title": "Edited", "content": full,
        "group": "Home", "position": 2,
    })["favorite"]
    assert changed["content"] == full
    assert dispatcher.call("favorites.list", {"group": "Home"})["total"] == 1
    assert dispatcher.call("favorites.delete", {"favorite_id": entry["id"]}) == {"deleted": True}
    assert dispatcher.call("favorites.list", {})["total"] == 0
    events, _ = app.events.since(0)
    assert [event["name"] for event in events] == ["favorites.changed"] * 3


def test_backup_restores_independent_favorites_into_live_repository(favorite_app):
    app, writer = favorite_app
    rpc = Dispatcher(app)
    content = "Full favorite content <b>as text</b>. " * 40
    entry = rpc.call("favorites.add", {
        "title": "Backup favorite", "content": content, "group": "Work",
    })["favorite"]
    path = app.create_backup()["backup_path"]
    rpc.call("favorites.delete", {"favorite_id": entry["id"]})
    assert rpc.call("favorites.list", {})["total"] == 0
    restored = app.restore_backup(path)
    assert restored["favorites"] == 1
    favorite = rpc.call("favorites.get", {"favorite_id": entry["id"]})["favorite"]
    assert favorite["content"] == content
    assert favorite["title"] == entry["title"]
    assert favorite["group"] == "Work"
    assert rpc.call("favorites.list", {"group": "Work"})["total"] == 1
    rpc.call("favorites.copy", {"favorite_id": entry["id"]})
    assert writer.write.call_args.args[0].types == {ContentType.TEXT: content.encode()}


@pytest.mark.parametrize("payload", ["{broken", "{}", "[null]"])
def test_invalid_favorites_backup_does_not_replace_legacy_file(favorite_app, tmp_path, payload):
    import zipfile

    app, _ = favorite_app
    legacy = tmp_path / "favorites.json"
    legacy.write_text("[]", encoding="utf-8")
    archive_path = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("favorites.json", payload)
    with pytest.raises(ApplicationError) as error:
        app.restore_backup(str(archive_path))
    assert error.value.code == "RESTORE_PARTIAL_FAILED"
    assert legacy.read_text(encoding="utf-8") == "[]"


@pytest.mark.parametrize("entries", [
    [],
    [{"title": "First", "content": "one"}, {"title": "Second", "content": "two"}],
])
def test_legacy_favorites_backup_preserves_distinct_idless_entries(favorite_app, tmp_path, entries):
    import json
    import zipfile

    app, _ = favorite_app
    archive_path = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("favorites.json", json.dumps(entries))
    result = app.restore_backup(str(archive_path))
    assert result["favorites"] == len(entries)
    listing = Dispatcher(app).call("favorites.list", {})
    assert listing["total"] == len(entries)
    ids = [item["id"] for item in listing["items"]]
    assert len(set(ids)) == len(entries)
    assert all(ids)
    contents = [
        Dispatcher(app).call("favorites.get", {"favorite_id": entry_id})["favorite"]["content"]
        for entry_id in ids
    ]
    assert sorted(contents) == sorted(entry["content"] for entry in entries)


@pytest.mark.parametrize(
    "method,params",
    [
        ("favorites.add", {"title": "x", "content": "x", "group": "", "path": "x"}),
        ("favorites.add", {"title": "x", "content": "x"}),
        ("favorites.add", {"title": 1, "content": "x", "group": ""}),
        ("favorites.list", {"limit": True}),
        ("favorites.list", {"offset": -1}),
        ("favorites.list", {"query": "x" * 513}),
        ("favorites.get", {"favorite_id": ""}),
        ("favorites.update", {"favorite_id": "x", "content": "preview"}),
        ("favorites.delete", {"favorite_id": ["x"]}),
        ("favorites.copy", {"favorite_id": "x", "command": "shell"}),
    ],
)
def test_favorites_rpc_rejects_invalid_dtos_before_storage(favorite_app, method, params):
    app, _ = favorite_app
    with pytest.raises(ApplicationError) as error:
        Dispatcher(app).call(method, params)
    assert error.value.code == "VALIDATION_ERROR"
    assert app.favorites.list()["total"] == 0


def test_batch_favorite_keeps_full_history_text_and_publishes_one_change(favorite_app):
    app, _ = favorite_app
    full = "Full history content <b>as text</b>. " * 40
    app._repository.add(ClipboardContent(types={ContentType.TEXT: full.encode()}))
    entry_id = str(app._repository.get_all()[0]["entry_id"])
    dispatcher = Dispatcher(app)
    result = dispatcher.call("favorites.batch_add", {"entry_ids": [entry_id], "group": "Work"})
    assert result["added"] == 1
    stored = dispatcher.call("favorites.get", {"favorite_id": result["ids"][0]})["favorite"]
    assert stored["content"] == full
    assert stored["group"] == "Work"
    events, _ = app.events.since(0)
    assert [event["name"] for event in events] == ["favorites.changed"]


def test_export_favorites_writes_a_file_without_publishing_a_change(
    favorite_app, monkeypatch, tmp_path
):
    app, _ = favorite_app
    # Keep the export inside the isolated data directory instead of the real
    # user's Downloads folder.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    Dispatcher(app).call("favorites.add", {"title": "T", "content": "C", "group": "G"})
    result = Dispatcher(app).call("favorites.export", {"format": "markdown"})
    assert result["count"] == 1
    assert result["filename"].endswith(".md")
    written = Path(result["filepath"]).read_text(encoding="utf-8")
    assert written.startswith("# ClipSync Favorites")
    assert "C" in written
    events, _ = app.events.since(0)
    assert [event["name"] for event in events] == ["favorites.changed"]


def test_favorite_storage_survives_restart(favorite_app):
    app, _ = favorite_app
    created = app.favorites.add("Persist", "full content", "Saved")["favorite"]
    app.lifecycle.stop()
    restarted = SidecarApplication()
    restarted.lifecycle.start()
    try:
        assert restarted.require_favorites().get(created["id"]) == {"favorite": created}
    finally:
        assert restarted.lifecycle.stop()


def test_favorite_access_requires_unlock(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    identity = prepare_identity(Config(), password="favorite-password")
    save(identity.config, identity.encryption)
    app = SidecarApplication()
    app.lifecycle.start()
    try:
        with pytest.raises(ApplicationError) as error:
            Dispatcher(app).call("favorites.list", {})
        assert error.value.code == "APP_LOCKED"
        assert not (tmp_path / "favorites.db").exists()
        app.unlock("favorite-password")
        assert Dispatcher(app).call("favorites.list", {})["total"] == 0
    finally:
        assert app.lifecycle.stop()


def test_group_registry_keeps_an_empty_group_and_reports_counts(favorite_app):
    """A group is a name the user made, not a side effect of a favourite.

    The legacy panel kept this list in the browser, which is how it could
    offer groups the data folder no longer had.  Here it lives with the
    favourites, so what the sidebar lists and what exists cannot drift.
    """
    app, _ = favorite_app
    dispatcher = Dispatcher(app)
    assert dispatcher.call("favorites.group_create", {"name": "Travel"}) == {
        "groups": ["Travel"],
    }
    listing = dispatcher.call("favorites.list", {})
    assert listing["groups"] == ["Travel"]
    assert listing["group_counts"] == {"Travel": 0}
    assert listing["library_total"] == 0

    entry = dispatcher.call("favorites.add", {
        "title": "Ticket", "content": "seat 14C", "group": "Travel",
    })["favorite"]
    listing = dispatcher.call("favorites.list", {})
    assert listing["group_counts"] == {"Travel": 1}
    assert listing["library_total"] == 1
    # A search does not renumber the sidebar.
    assert dispatcher.call("favorites.list", {"query": "nothing"})["group_counts"] == {
        "Travel": 1,
    }

    # Deleting the group keeps the favourite and unfiles it.
    assert dispatcher.call("favorites.group_delete", {"name": "Travel"}) == {
        "moved": 1, "groups": [],
    }
    assert dispatcher.call("favorites.get", {"favorite_id": entry["id"]})["favorite"]["group"] == ""
    assert dispatcher.call("favorites.list", {})["library_total"] == 1


def test_renaming_a_group_moves_its_favourites_and_the_registry(favorite_app):
    app, _ = favorite_app
    dispatcher = Dispatcher(app)
    first = dispatcher.call("favorites.add", {
        "title": "A", "content": "1", "group": "Old",
    })["favorite"]
    second = dispatcher.call("favorites.add", {
        "title": "B", "content": "2", "group": "Old",
    })["favorite"]
    dispatcher.call("favorites.group_create", {"name": "Empty"})
    assert dispatcher.call("favorites.group_rename", {"name": "Old", "rename_to": "New"}) == {
        "renamed": 2, "groups": ["Empty", "New"],
    }
    listing = dispatcher.call("favorites.list", {"group": "New"})
    assert [item["id"] for item in listing["items"]] == [first["id"], second["id"]]
    assert dispatcher.call("favorites.list", {})["groups"] == ["Empty", "New"]
    # Renaming the group that only the registry knows about still works, which
    # is the whole reason the registry exists.
    assert dispatcher.call("favorites.group_rename", {"name": "Empty", "rename_to": "Later"}) == {
        "renamed": 0, "groups": ["Later", "New"],
    }
