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


def test_unknown_favorite_command_never_mutates(favorite_app):
    app, _ = favorite_app
    dispatcher = Dispatcher(app)
    entry = dispatcher.call(
        "favorites.add", {"title": "Keep", "content": "text", "group": ""}
    )["favorite"]
    with pytest.raises(ApplicationError) as error:
        dispatcher.call("favorites.erase_anything", {"favorite_id": entry["id"]})
    assert error.value.code == "METHOD_NOT_FOUND"
    assert dispatcher.call("favorites.list", {})["total"] == 1


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


@pytest.mark.parametrize("params", [
    {}, {"entry_ids": []}, {"entry_ids": ["a"], "group": "g" * 129},
    {"entry_ids": ["a", "a"], "group": ""}, {"entry_ids": [7], "group": ""},
])
def test_batch_favorite_validates_arguments(favorite_app, params):
    app, _ = favorite_app
    with pytest.raises(ApplicationError) as error:
        Dispatcher(app).call("favorites.batch_add", params)
    assert error.value.code == "VALIDATION_ERROR"


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


@pytest.mark.parametrize("params", [{}, {"format": "pdf"}, {"format": 7}])
def test_export_favorites_validates_the_format(favorite_app, params):
    app, _ = favorite_app
    with pytest.raises(ApplicationError) as error:
        Dispatcher(app).call("favorites.export", params)
    assert error.value.code == "VALIDATION_ERROR"


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
