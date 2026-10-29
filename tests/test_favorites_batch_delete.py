"""Deleting a selection of favourites in one call.

The point of the call is the cost it removes — one round trip and one published
change instead of N of each — so what is worth pinning down is that it acts on
exactly the ids it was given, and that a row which is already gone is not an
error on the way to a library where it is gone.
"""

import pytest

from internal.application.errors import ApplicationError
from internal.application.use_cases.favorites import FavoritesUseCase


class _Repo:
    def __init__(self, ids):
        self.rows = list(ids)
        self.calls = []

    def delete(self, favorite_id):
        self.calls.append(("delete", favorite_id))
        if favorite_id in self.rows:
            self.rows.remove(favorite_id)
            return True
        return False

    def get_all(self):
        return []

    def group_names(self):
        return []

    def batch(self, updates=(), deletes=(), *, register=(), forget=(), rename=None,
              validate=None, validate_existing=None):
        deleted = []
        for favorite_id in deletes:
            self.calls.append(("delete", favorite_id))
            if favorite_id in self.rows:
                self.rows.remove(favorite_id)
                deleted.append(favorite_id)
        for name in forget:
            self.calls.append(("forget_group", name))
        return {"updated": [], "deleted": deleted}


def test_it_deletes_every_id_it_was_given():
    repo = _Repo(["a", "b", "c"])
    payload = FavoritesUseCase(repository=repo).batch_delete(["a", "c"])

    assert payload == {"deleted": 2, "ids": ["a", "c"]}
    assert repo.rows == ["b"]
    assert repo.calls == [("delete", "a"), ("delete", "c")]


def test_a_row_that_is_already_gone_is_not_a_failure():
    """The reader's goal is "these are not in the library".  One of them being
    gone already is that goal, half met — refusing the whole selection over it
    would leave the other rows behind for no reason."""
    repo = _Repo(["b"])
    payload = FavoritesUseCase(repository=repo).batch_delete(["a", "b"])

    assert payload == {"deleted": 1, "ids": ["b"]}
    assert repo.rows == []


@pytest.mark.parametrize("selection", [
    [],
    ["a", "a"],
    ["a"] * 101,
    [""],
    ["a" * 65],
    [1],
    "a",
    None,
])
def test_a_selection_that_is_not_a_list_of_ids_is_refused(selection):
    repo = _Repo(["a"])
    with pytest.raises(ApplicationError) as caught:
        FavoritesUseCase(repository=repo).batch_delete(selection)

    assert caught.value.code == "INVALID_ARGUMENT"
    assert repo.rows == ["a"]
    assert repo.calls == []


def test_a_hundred_ids_is_the_upper_bound_that_is_allowed():
    ids = [f"id{index}" for index in range(100)]
    repo = _Repo(ids)
    assert FavoritesUseCase(repository=repo).batch_delete(ids)["deleted"] == 100
