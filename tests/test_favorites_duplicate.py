"""Saving a body that is already in the library.

The decision is "ask", not "merge" and not "refuse": the same snippet under two
titles is something readers do on purpose, so the first save writes nothing and
answers with the row it matched, and ``force`` — the reader saying yes — writes
it.  A favourite that is only a title has no body to compare, so it is never a
duplicate.
"""

from internal.application.use_cases.favorites import FavoritesUseCase


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
    """A favourites table whose only rule is "one row per id".

    ``calls`` records what the use case asked for, because "nothing was
    written" is the assertion a duplicate check is worth having.
    """

    def __init__(self, entries):
        self.entries = [dict(entry) for entry in entries]
        self.calls = []

    def find_by_content(self, content):
        self.calls.append(("find_by_content", content))
        for entry in self.entries:
            if entry["content"] == content:
                return dict(entry)
        return None

    def add(self, title, content, group, validate=None):
        self.calls.append(("add", title))
        entry = _favourite(
            f"new-{len(self.entries)}", title, content, group, position=len(self.entries)
        )
        self.entries.append(entry)
        return dict(entry)

    def register_group(self, name):
        self.calls.append(("register_group", name))
        return True

    def group_names(self):
        return sorted({entry["group"] for entry in self.entries if entry["group"]})

    def get_all(self):
        return [dict(entry) for entry in self.entries]


def test_an_identical_body_answers_with_the_row_it_matched():
    repo = _Repo([_favourite("a", title="Alpha", content="same", group="Work")])
    payload = FavoritesUseCase(repository=repo).add("Alpha again", "same")

    assert payload["favorite"] is None
    assert payload["duplicate"] == {"id": "a", "title": "Alpha", "group": "Work"}
    # Nothing was written, and the answer says which row to look at rather than
    # only that one exists.
    assert [entry["id"] for entry in repo.entries] == ["a"]
    assert ("add", "Alpha again") not in repo.calls


def test_force_writes_the_second_copy_the_reader_asked_for():
    repo = _Repo([_favourite("a", title="Alpha", content="same")])
    payload = FavoritesUseCase(repository=repo).add("Beta", "same", force=True)

    assert payload["duplicate"] is None
    assert payload["favorite"]["title"] == "Beta"
    assert [entry["title"] for entry in repo.entries] == ["Alpha", "Beta"]
    # A forced save does not even look: asking and then ignoring the answer
    # would be two reads of the table for one write.
    assert not [call for call in repo.calls if call[0] == "find_by_content"]


def test_a_body_that_is_not_there_is_stored_on_the_first_try():
    repo = _Repo([_favourite("a", content="one")])
    payload = FavoritesUseCase(repository=repo).add("Two", "two")

    assert payload["duplicate"] is None
    assert payload["favorite"]["content"] == "two"
    assert [entry["content"] for entry in repo.entries] == ["one", "two"]


def test_a_favourite_that_is_only_a_title_is_never_a_duplicate():
    """Two untitled-body rows are two notes, not a repetition: there is nothing
    to compare, and refusing the second would make 只写标题 impossible."""
    repo = _Repo([_favourite("a", title="Todo", content="")])
    payload = FavoritesUseCase(repository=repo).add("Todo", "")

    assert payload["duplicate"] is None
    assert payload["favorite"]["title"] == "Todo"
    assert len(repo.entries) == 2
    assert not [call for call in repo.calls if call[0] == "find_by_content"]


def test_a_whitespace_body_is_not_compared_either():
    repo = _Repo([_favourite("a", title="Blank", content="   ")])
    payload = FavoritesUseCase(repository=repo).add("Blank again", " \n ")

    assert payload["duplicate"] is None
    assert len(repo.entries) == 2
