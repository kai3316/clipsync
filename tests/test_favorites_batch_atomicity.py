"""A multi-row favourites action lands completely, or not at all.

The failure this holds shut: ``reorder``, ``rename_group``, ``delete_group``
and the web panel's batch edit each committed row by row.  A refusal or a
storage error on the third of ten rows left the library partially sorted, half
renamed or partly deleted, while the caller was told the action failed — and a
retry could not tell the applied half from the rest.  The group registry was a
separate commit on top of that, so a rename could move every row and leave the
group name behind.
"""

from __future__ import annotations

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


def _library(use_case, count=4, group="work"):
    return [
        use_case.add(f"title {index}", f"body {index}", group)["favorite"]
        for index in range(count)
    ]


def _positions(use_case):
    return {item["id"]: item["position"] for item in use_case.list(limit=100)["items"]}


def test_a_reorder_that_fails_on_the_second_row_changes_nothing(use_case, monkeypatch):
    entries = _library(use_case)
    before = _positions(use_case)
    original = use_case._validate
    refused = {"id": entries[2]["id"]}

    def refuse(entry):
        if entry["id"] == refused["id"]:
            raise ApplicationError("INVALID_ARGUMENT", "Invalid position")
        return original(entry)

    monkeypatch.setattr(use_case, "_validate", refuse)
    order = [entry["id"] for entry in reversed(entries)]
    with pytest.raises(ApplicationError):
        use_case.reorder(order)

    # The first row's new position was applied before the refusal under the old
    # code; now the transaction rolls it back with the rest.
    assert _positions(use_case) == before


def test_a_reorder_commits_every_row_when_nothing_fails(use_case):
    entries = _library(use_case)
    original = _positions(use_case)
    order = [entry["id"] for entry in reversed(entries)]

    result = use_case.reorder(order)

    after = _positions(use_case)
    assert result["moved"] == len(entries)
    # Same slots, new occupants: the set of positions is unchanged.
    assert sorted(after.values()) == sorted(original.values())
    assert after[order[0]] == min(original.values())


def test_a_purge_that_fails_halfway_keeps_the_rows_and_the_group(use_case, repository, monkeypatch):
    entries = _library(use_case)
    calls = {"count": 0}
    real_delete = repository._delete_row

    def refuse_second(conn, favorite_id):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("database is locked")
        return real_delete(conn, favorite_id)

    monkeypatch.setattr(repository, "_delete_row", refuse_second)
    with pytest.raises(ApplicationError):
        use_case.delete_group("work", purge=True)

    assert len(use_case.list(limit=100)["items"]) == len(entries)
    assert "work" in use_case.list(limit=1)["groups"]


def test_a_rename_that_fails_on_a_row_leaves_the_registry_alone(use_case, repository, monkeypatch):
    _library(use_case, 3, "work")
    real_update = repository._update_row
    calls = {"count": 0}

    def refuse_second(conn, favorite_id, changes, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("database is locked")
        return real_update(conn, favorite_id, changes, *args, **kwargs)

    monkeypatch.setattr(repository, "_update_row", refuse_second)
    with pytest.raises(ApplicationError):
        use_case.rename_group("work", "archive")

    names = use_case.list(limit=1)["groups"]
    # Neither the group name nor the rows moved: the rename is one action.
    assert names == ["work"]
    assert {item["group"] for item in use_case.list(limit=100)["items"]} == {"work"}


def test_a_rename_moves_rows_and_registry_together(use_case):
    _library(use_case, 3, "work")

    result = use_case.rename_group("work", "archive")

    assert result["renamed"] == 3
    assert {item["group"] for item in use_case.list(limit=100)["items"]} == {"archive"}
    assert "archive" in use_case.list(limit=1)["groups"]
    assert "work" not in use_case.list(limit=1)["groups"]


def test_the_web_batch_edit_is_one_transaction(repository, monkeypatch):
    service = FavoritesUseCase(repository)
    entries = _library(service, 3, "work")
    monkeypatch.setattr(web, "_repository", lambda: repository)
    assert repository is not None
    real_update = repository._update_row
    calls = {"count": 0}

    def refuse_second(conn, favorite_id, changes, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("database is locked")
        return real_update(conn, favorite_id, changes, *args, **kwargs)

    monkeypatch.setattr(repository, "_update_row", refuse_second)
    payload = [{"id": entry["id"], "title": "renamed"} for entry in entries]
    data, status = web._apply_updates(payload)

    assert status == 500 and data["ok"] is False
    titles = {item["title"] for item in service.list(limit=100)["items"]}
    assert titles == {"title 0", "title 1", "title 2"}
