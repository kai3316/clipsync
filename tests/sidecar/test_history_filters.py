"""The history panel's kind chips, sort toggle and search, answered natively.

The window's toolbar had a search box and nothing else, so the only way to find
an image among a thousand text clips was to scroll.  The panel offered five
chips with counts, a newest/oldest toggle, and a search that ran over everything
the row displayed; this is the same toolbar, answered by the sidecar instead of
by a client-side pass over a fully-loaded list.
"""

from unittest.mock import Mock

import pytest

from internal.application.use_cases.history import KINDS, SORTS, HistoryUseCase, matches_kind


def use_case(entries):
    repository = Mock()
    repository.get_all.return_value = entries
    return HistoryUseCase(repository)


def row(**overrides):
    entry = {
        "entry_id": 0, "timestamp": 1, "text_preview": "clip", "content_type": "TEXT",
        "pinned": False, "types": {}, "source_device": "", "source_app": "",
        "source_title": "", "paste_count": 0,
    }
    entry.update(overrides)
    return entry


def ids(page):
    return [item["id"] for item in page["items"]]


def test_no_filter_ships_every_row_and_says_which_filter_was_used():
    page = use_case([row(entry_id="a")]).list()
    assert ids(page) == ["a"]
    assert page["kind"] == "all"
    assert page["sort"] == "newest"
    assert page["total"] == 1


@pytest.mark.parametrize(
    "content_type,preview,kind,expected",
    [
        # 文本 is every kind that carries words, not the TEXT type alone.
        ("TEXT", "hello", "text", True),
        ("HTML", "hello", "text", True),
        ("RTF", "hello", "text", True),
        ("IMAGE_PNG", "[Image]", "text", False),
        # This build stores a bitmap as IMAGE_PNG; older rows still say IMAGE.
        ("IMAGE_PNG", "[Image]", "image", True),
        ("IMAGE", "[Image]", "image", True),
        ("IMAGE_EMF", "[Image]", "image", True),
        ("FILE", "[File] report.pdf", "file", True),
        ("TEXT", "plain words", "file", False),
        # A link is a clip whose text reads as one, whatever type it carries —
        # which is how a link copied out of a browser arrives: plain text.
        ("TEXT", "https://example.com/a", "link", True),
        ("TEXT", "See https://example.com", "link", False),
        ("URL", "", "link", True),
        ("TEXT", "not a link", "link", False),
        # Every kind belongs to 全部.
        ("IMAGE_PNG", "[Image]", "all", True),
    ],
)
def test_the_chip_membership_rules(content_type, preview, kind, expected):
    assert matches_kind(row(content_type=content_type, text_preview=preview), kind) is expected


def test_a_chip_returns_only_its_kind():
    page = use_case([
        row(entry_id="t", content_type="TEXT", text_preview="words"),
        row(entry_id="i", content_type="IMAGE_PNG", text_preview="[Image]"),
    ]).list(kind="image")
    assert ids(page) == ["i"]
    # The total is the filtered total: paging must not promise rows the filter
    # has already excluded.
    assert page["total"] == 1


def test_the_counts_are_what_each_chip_would_show():
    page = use_case([
        row(entry_id="t", content_type="TEXT", text_preview="words"),
        row(entry_id="i", content_type="IMAGE_PNG", text_preview="[Image]"),
        row(entry_id="f", content_type="FILE", text_preview="[File] a.pdf"),
        row(entry_id="l", content_type="TEXT", text_preview="https://example.com"),
    ]).list()
    # The link is also text, so it is counted in both: a chip's badge is the
    # number of rows clicking it yields, not a partition.
    assert page["counts"] == {"all": 4, "text": 2, "image": 1, "file": 1, "link": 1}


def test_the_counts_follow_the_search_and_not_the_chip():
    rows = [
        row(entry_id="a", content_type="TEXT", text_preview="alpha"),
        row(entry_id="b", content_type="IMAGE_PNG", text_preview="[Image] alpha"),
        row(entry_id="c", content_type="IMAGE_PNG", text_preview="[Image] beta"),
    ]
    # Counted under the search and *not* under the active chip, so the badge on
    # 全部 keeps promising the rows the chip is currently hiding.
    page = use_case(rows).list(query="alpha", kind="image")
    assert page["counts"] == {"all": 2, "text": 1, "image": 1, "file": 0, "link": 0}
    assert ids(page) == ["b"]


def test_a_search_reaches_everything_the_row_shows():
    rows = [
        row(entry_id="app", source_app="chrome"),
        row(entry_id="title", source_title="Inbox — mail"),
        row(entry_id="device", source_device="peer-1"),
        row(entry_id="preview", text_preview="the needle"),
        row(entry_id="type", content_type="IMAGE_PNG"),
        row(entry_id="none", text_preview="nothing here"),
    ]
    service = use_case(rows)
    # The panel matched the preview, the source device and the type; the window
    # also shows an application and a window title, so those are searched too —
    # a badge the user can read and not search for is worse than no badge.
    assert ids(service.list(query="chrome")) == ["app"]
    assert ids(service.list(query="INBOX")) == ["title"]
    assert ids(service.list(query="peer-1")) == ["device"]
    assert ids(service.list(query="needle")) == ["preview"]
    assert ids(service.list(query="image_png")) == ["type"]
    assert ids(service.list(query="  ")) == [r["entry_id"] for r in rows]


def test_the_sort_toggle_reverses_the_timestamps():
    rows = [row(entry_id="old", timestamp=10), row(entry_id="new", timestamp=20)]
    assert ids(use_case(rows).list()) == ["new", "old"]
    assert ids(use_case(rows).list(sort="oldest")) == ["old", "new"]


def test_a_pinned_entry_comes_first_in_both_orders():
    rows = [
        row(entry_id="pinned", timestamp=1, pinned=True),
        row(entry_id="old", timestamp=10),
        row(entry_id="new", timestamp=20),
    ]
    # Both directions: a pinned row the user has to scroll to is not pinned, and
    # reversing the pinned half along with the timestamps is exactly how that
    # happens.
    assert ids(use_case(rows).list()) == ["pinned", "new", "old"]
    assert ids(use_case(rows).list(sort="oldest")) == ["pinned", "old", "new"]


def test_an_empty_history_still_answers_an_empty_page():
    page = use_case([]).list()
    assert page["items"] == []
    assert page["total"] == 0
    assert page["has_history"] is False
    assert page["counts"] == {"all": 0, "text": 0, "image": 0, "file": 0, "link": 0}


def test_history_exists_even_when_the_search_matches_nothing():
    # The chip row stays on screen while a search matches nothing — a bar that
    # vanishes on a typo takes the way back out with it.
    page = use_case([row(entry_id="a")]).list(query="no such thing")
    assert page["items"] == []
    assert page["has_history"] is True


def test_the_kinds_and_sorts_are_the_closed_sets_the_protocol_validates():
    assert KINDS == ("all", "text", "image", "file", "link")
    assert SORTS == ("newest", "oldest")
