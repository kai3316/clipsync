"""Importing a favourites file: the round trip, and the four ways it refuses.

The guarantee this module is about is narrow and worth pinning down: what the
JSON export writes, the importer reads back, and a file that is not one of ours
changes nothing at all.
"""

import json

import pytest

from internal.application.errors import ApplicationError
from internal.application.use_cases.favorites import IMPORT_MAX_ITEMS, FavoritesUseCase


def _favourite(entry_id, title="t", content="c", group="", position=0):
    return {
        "id": entry_id,
        "title": title,
        "content": content,
        "group": group,
        "position": position,
        "created": 1000.0,
        "updated": 1000.0,
    }


class _Repo:
    """A favourites table with just enough behaviour to export and import."""

    def __init__(self, entries=()):
        self.entries = [dict(entry) for entry in entries]
        self.calls = []

    def get_all(self):
        return [dict(entry) for entry in self.entries]

    def add(self, title, content, group, validate=None):
        self.calls.append(("add", title))
        entry = _favourite(
            f"new-{len(self.entries)}", title, content, group, position=len(self.entries)
        )
        self.entries.append(entry)
        return dict(entry)

    def register_group(self, name):
        return True

    def group_names(self):
        return sorted({entry["group"] for entry in self.entries if entry["group"]})


def _export(tmp_path, entries):
    """Write an export of *entries* and answer the path it landed on."""
    payload = FavoritesUseCase(repository=_Repo(entries)).export("json", dest_dir=tmp_path)
    assert payload["format"] == "json"
    assert payload["filepath"].endswith(".json")
    return payload["filepath"]


def _write(tmp_path, document, name="favorites.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_what_the_export_writes_the_import_reads_back(tmp_path):
    source = [
        _favourite("a", "Alpha", "first body", "Work", 0),
        _favourite("b", "Beta", "second body", "Work", 1),
        _favourite("c", "Gamma", "third body", "", 2),
    ]
    path = _export(tmp_path, source)

    target = _Repo()
    payload = FavoritesUseCase(repository=target).import_file(path)

    assert payload == {"added": 3, "skipped": 0, "total": 3}
    # The order is the file's, and the bodies are whole: a migration that
    # reorders a library or cuts a clip short has not migrated it.
    assert [(entry["title"], entry["content"], entry["group"]) for entry in target.entries] == [
        ("Alpha", "first body", "Work"),
        ("Beta", "second body", "Work"),
        ("Gamma", "third body", ""),
    ]


def test_importing_the_same_file_twice_adds_nothing_the_second_time(tmp_path):
    path = _export(tmp_path, [_favourite("a", "Alpha", "body", "Work")])
    repo = _Repo()
    use_case = FavoritesUseCase(repository=repo)

    assert use_case.import_file(path)["added"] == 1
    assert use_case.import_file(path) == {"added": 0, "skipped": 1, "total": 1}
    assert len(repo.entries) == 1


def test_a_body_already_in_the_library_is_skipped_whatever_its_title(tmp_path):
    """The check is on the body, not on the name it was filed under: renaming a
    favourite and importing the old file must not make a second copy."""
    path = _export(tmp_path, [_favourite("a", "Alpha", "shared body", "Work")])
    repo = _Repo([_favourite("x", "Renamed", "shared body", "Home")])

    payload = FavoritesUseCase(repository=repo).import_file(path)

    assert payload == {"added": 0, "skipped": 1, "total": 1}
    assert [entry["title"] for entry in repo.entries] == ["Renamed"]


def test_a_favourite_with_no_body_is_matched_by_its_title(tmp_path):
    path = _export(tmp_path, [
        _favourite("a", "Todo", "", ""),
        _favourite("b", "Elsewhere", "", ""),
    ])
    repo = _Repo([_favourite("x", "Todo", "", "")])

    payload = FavoritesUseCase(repository=repo).import_file(path)

    assert payload == {"added": 1, "skipped": 1, "total": 2}
    assert [entry["title"] for entry in repo.entries] == ["Todo", "Elsewhere"]


def test_a_file_that_repeats_a_body_imports_it_once(tmp_path):
    path = _write(tmp_path, {
        "kind": "clipsync-favorites", "version": 1,
        "favorites": [{"title": "One", "content": "same"}, {"title": "Two", "content": "same"}],
    })
    repo = _Repo()

    assert FavoritesUseCase(repository=repo).import_file(path)["added"] == 1
    assert [entry["title"] for entry in repo.entries] == ["One"]


@pytest.mark.parametrize("document", [
    {"kind": "something-else", "version": 1, "favorites": []},
    {"kind": "clipsync-favorites", "favorites": []},
    {"kind": "clipsync-favorites", "version": 2, "favorites": []},
    {"kind": "clipsync-favorites", "version": 1},
    {"kind": "clipsync-favorites", "version": 1, "favorites": [{"title": "", "content": ""}]},
    {"kind": "clipsync-favorites", "version": 1, "favorites": [{"title": "t", "content": "x" * 65537}]},
    {"kind": "clipsync-favorites", "version": 1, "favorites": ["not a row"]},
    [1, 2, 3],
])
def test_a_file_that_is_not_one_of_ours_is_refused_and_changes_nothing(tmp_path, document):
    path = _write(tmp_path, document)
    repo = _Repo([_favourite("x", "Kept", "kept", "Work")])

    with pytest.raises(ApplicationError) as caught:
        FavoritesUseCase(repository=repo).import_file(path)

    assert caught.value.code == "INVALID_ARGUMENT"
    assert repo.entries == [_favourite("x", "Kept", "kept", "Work")]
    assert repo.calls == []


def test_a_damaged_file_is_refused_before_the_first_row_is_written(tmp_path):
    """The whole file is checked first: four hundred good rows followed by one
    broken one must not leave four hundred rows in a library the reader
    believed was untouched."""
    path = _write(tmp_path, {
        "kind": "clipsync-favorites", "version": 1,
        "favorites": [
            {"title": "Good", "content": "one"},
            {"title": "Good", "content": "two"},
            {"title": "", "content": ""},
        ],
    })
    repo = _Repo()

    with pytest.raises(ApplicationError) as caught:
        FavoritesUseCase(repository=repo).import_file(path)

    assert caught.value.message == "Favorites file has an entry that cannot be imported"
    assert repo.entries == []
    assert repo.calls == []


def test_broken_json_and_a_missing_file_are_different_failures(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    repo = _Repo()
    use_case = FavoritesUseCase(repository=repo)

    with pytest.raises(ApplicationError) as caught:
        use_case.import_file(str(broken))
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.message == "Not a ClipSync favorites file"

    with pytest.raises(ApplicationError) as caught:
        use_case.import_file(str(tmp_path / "gone.json"))
    # A file that is not there is worth retrying once the reader has found it;
    # a file full of nonsense is not.
    assert caught.value.code == "IMPORT_FAILED"
    assert caught.value.retryable is True


def test_a_file_with_too_many_rows_is_refused(tmp_path):
    path = _write(tmp_path, {
        "kind": "clipsync-favorites", "version": 1,
        "favorites": [{"title": f"t{index}", "content": f"c{index}"}
                      for index in range(IMPORT_MAX_ITEMS + 1)],
    })
    repo = _Repo()

    with pytest.raises(ApplicationError) as caught:
        FavoritesUseCase(repository=repo).import_file(path)

    assert caught.value.message == "Favorites file has too many entries"
    assert repo.entries == []


def test_an_empty_export_is_a_file_the_import_accepts(tmp_path):
    """Exporting an empty library and importing it back is "nothing to do", not
    an error: the reader may well have exported before adding anything."""
    path = _export(tmp_path, [])

    assert FavoritesUseCase(repository=_Repo()).import_file(path) == {
        "added": 0, "skipped": 0, "total": 0,
    }
