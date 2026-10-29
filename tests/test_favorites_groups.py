"""Deleting a favourite group, with and without the things inside it.

The two answers are different promises: by default the favourites survive and
become unfiled, and with ``purge`` they go with the group.  What is worth
pinning down is the second one's *order* — entries first, then the group — and
that it touches nothing outside the group.
"""

from internal.application.use_cases.favorites import FavoritesUseCase


def _favourite(entry_id, title="t", content="c", group="", position=0):
    return {
        "id": entry_id,
        "title": title,
        "content": content,
        "group": group,
        "position": position,
        # A favourite's timestamps are epoch numbers here as they are in the
        # database: the use case validates what a row says it stored.
        "created": 1000.0,
        "updated": 1000.0,
    }


class _Repo:
    """Everything ``delete_group`` can reach, and a record of the order.

    ``calls`` is one shared list across every method so a test can ask whether
    the rows were deleted before the group was forgotten — which is the whole
    contract, not an implementation detail.
    """

    def __init__(self, entries):
        self.entries = [dict(entry) for entry in entries]
        self.calls = []

    def get_all(self):
        return [dict(entry) for entry in self.entries]

    def group_names(self):
        return sorted({entry["group"] for entry in self.entries if entry["group"]})

    def update(self, favorite_id, changes, validate=None, validate_existing=None):
        self.calls.append(("update", favorite_id))
        for entry in self.entries:
            if entry["id"] == favorite_id:
                entry.update(changes)
                return dict(entry)
        return None

    def rename_group(self, name, new_name):
        self.calls.append(("rename_group", name))
        for entry in self.entries:
            if entry["group"] == name:
                entry["group"] = new_name
        return True

    def delete(self, favorite_id):
        self.calls.append(("delete", favorite_id))
        for index, entry in enumerate(self.entries):
            if entry["id"] == favorite_id:
                del self.entries[index]
                return True
        return False

    def forget_group(self, name):
        self.calls.append(("forget_group", name))
        return True

    def batch(self, updates=(), deletes=(), *, register=(), forget=(), rename=None,
              validate=None, validate_existing=None):
        """The real repository runs this as one transaction; the double records
        each step in order so a caller can still assert 'rows before group'."""
        updated = []
        deleted = []
        for favorite_id, changes in updates:
            self.calls.append(("update", favorite_id))
            for entry in self.entries:
                if entry["id"] == favorite_id:
                    entry.update(changes)
                    updated.append(dict(entry))
                    break
            else:
                updated.append(None)
        for favorite_id in deletes:
            self.calls.append(("delete", favorite_id))
            for index, entry in enumerate(self.entries):
                if entry["id"] == favorite_id:
                    del self.entries[index]
                    deleted.append(favorite_id)
                    break
        for name in register:
            self.calls.append(("register_group", name))
        for name in forget:
            self.calls.append(("forget_group", name))
        if rename is not None:
            self.calls.append(("rename_group", rename[0]))
        return {"updated": updated, "deleted": deleted}


def _groups(payload):
    return payload["groups"]


def test_deleting_a_group_keeps_its_favourites_and_unfiles_them():
    repo = _Repo([_favourite("a", group="Work"), _favourite("b", group="Work"), _favourite("c")])
    payload = FavoritesUseCase(repository=repo).delete_group("Work")

    assert payload["moved"] == 2
    assert payload["removed"] == 0
    assert payload["removed_ids"] == []
    assert {entry["id"]: entry["group"] for entry in repo.entries} == {"a": "", "b": "", "c": ""}
    assert ("delete", "a") not in repo.calls
    assert _groups(payload) == []


def test_purging_a_group_takes_its_favourites_with_it():
    repo = _Repo([_favourite("a", group="Work"), _favourite("b", group="Work"), _favourite("c")])
    payload = FavoritesUseCase(repository=repo).delete_group("Work", purge=True)

    assert payload["moved"] == 0
    assert payload["removed"] == 2
    assert sorted(payload["removed_ids"]) == ["a", "b"]
    # The row outside the group is untouched, and nothing had to be moved to a
    # group that no longer exists on the way there.
    assert [entry["id"] for entry in repo.entries] == ["c"]
    assert ("update", "a") not in repo.calls
    assert _groups(payload) == []


def test_the_rows_go_before_the_group_does():
    repo = _Repo([_favourite("a", group="Work")])
    FavoritesUseCase(repository=repo).delete_group("Work", purge=True)

    assert repo.calls == [("delete", "a"), ("forget_group", "Work")]


def test_purge_leaves_an_empty_group_a_name_to_forget():
    """A group with no rows is exactly what the registry exists for: it still
    has to be forgotten, and no row has to be deleted."""
    repo = _Repo([_favourite("a")])
    payload = FavoritesUseCase(repository=repo).delete_group("Empty", purge=True)

    assert payload["removed"] == 0
    assert payload["removed_ids"] == []
    assert repo.calls == [("forget_group", "Empty")]


def test_a_blank_group_name_is_refused_either_way():
    repo = _Repo([])
    use_case = FavoritesUseCase(repository=repo)
    for purge in (False, True):
        try:
            use_case.delete_group("   ", purge=purge)
        except Exception as exc:  # noqa: BLE001 - the type is the assertion
            assert exc.code == "INVALID_ARGUMENT"
        else:  # pragma: no cover - a refusal that did not refuse
            raise AssertionError("a blank group name was accepted")
    assert repo.calls == []
