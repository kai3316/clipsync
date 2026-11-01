"""The phone's history page searches and filters; the window does the same
work over IPC.  Both go through ``GET /api/history``, so the filtering lives
in the API and the phone only sends the parameters.

These cover the shared contract: what a query matches, what a chip means,
which order a sort yields, and — the part that is easy to get wrong — that
``total`` counts the *matching* rows, because the phone paginates against it.
"""

import pytest

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.web.api.history import get_history


class _Cfg:
    device_id = "dev-local"
    device_name = "Local"
    peers = {}
    web_history_limit = 20


@pytest.fixture
def db(tmp_path):
    return ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"), max_entries=50)


def _text(body: str, ts: float) -> ClipboardContent:
    return ClipboardContent(types={ContentType.TEXT: body.encode("utf-8")}, timestamp=ts)


def _url(body: str, ts: float) -> ClipboardContent:
    return ClipboardContent(types={ContentType.URL: body.encode("utf-8")}, timestamp=ts)


def _image(ts: float) -> ClipboardContent:
    return ClipboardContent(types={ContentType.IMAGE_PNG: b"\x89PNG"}, timestamp=ts)


def _file(ts: float) -> ClipboardContent:
    return ClipboardContent(types={ContentType.FILE: b"a.txt\x00"}, timestamp=ts)


def _seed(db):
    """One row per kind plus a plain-text link, newest last."""
    db.add(_file(1000.0))
    db.add(_image(1001.0))
    db.add(_url("https://example.com/a", 1002.0))
    db.add(_text("https://example.com/b", 1003.0))  # a link that arrived as text
    db.add(_text("meeting notes", 1004.0))
    return db


def _previews(payload):
    return [item["text_preview"] for item in payload["items"]]


# ── q ──────────────────────────────────────────────────────────────────


def test_query_matches_and_total_counts_only_the_matches(db):
    """``total`` is what the phone paginates against, so it has to describe
    the filtered list — not every row the machine ever kept."""
    _seed(db)
    payload, status = get_history(db, _Cfg(), query_str="example.com")
    assert status == 200
    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    assert payload["has_history"] is True


def test_query_is_case_insensitive_and_searches_the_source_badges(db):
    """A row shows the app it was copied in; searching what you can read has
    to find it (``searchable`` covers source_app / source_title)."""
    db.add(_text("standup", 1005.0), source_app={"name": "Chrome", "title": "Meet"})
    db.add(_text("unrelated", 1006.0))

    payload, _ = get_history(db, _Cfg(), query_str="chrome")
    assert [_["text_preview"] for _ in payload["items"]] == ["standup"]

    payload, _ = get_history(db, _Cfg(), query_str="CHROME")
    assert payload["total"] == 1


def test_empty_and_whitespace_query_is_no_filter(db):
    _seed(db)
    for query in (None, "", "   "):
        payload, _ = get_history(db, _Cfg(), query_str=query)
        assert payload["total"] == 5


# ── kind ───────────────────────────────────────────────────────────────


def test_link_chip_covers_both_a_url_row_and_a_text_that_reads_as_a_url(db):
    """The chip is what the user is looking for, not the stored type: a link
    copied out of a browser usually arrives as plain text."""
    _seed(db)
    payload, status = get_history(db, _Cfg(), kind_str="link")
    assert status == 200
    assert payload["total"] == 2
    assert payload["kind"] == "link"
    assert set(_previews(payload)) == {"https://example.com/a", "https://example.com/b"}


def test_kind_chip_narrows_each_kind(db):
    _seed(db)
    assert get_history(db, _Cfg(), kind_str="file")[0]["total"] == 1
    assert get_history(db, _Cfg(), kind_str="image")[0]["total"] == 1
    assert get_history(db, _Cfg(), kind_str="text")[0]["total"] == 2  # the text rows
    assert get_history(db, _Cfg(), kind_str="all")[0]["total"] == 5


def test_unknown_kind_falls_back_to_all_rather_than_matching_everything(db):
    """An invented chip used to fall through ``matches_kind`` as "matches
    everything", which looked like the filter worked.  The server answers with
    the filter it actually applied so the client's chip cannot lie."""
    _seed(db)
    payload, _ = get_history(db, _Cfg(), kind_str="videos")
    assert payload["kind"] == "all"
    assert payload["total"] == 5


def test_counts_describe_the_search_not_the_whole_history(db):
    """A chip's badge is the number clicking it yields, so it has to be
    recomputed under the active query."""
    _seed(db)
    payload, _ = get_history(db, _Cfg(), query_str="example.com")
    assert payload["counts"]["link"] == 2
    assert payload["counts"]["text"] == 1  # only the text-typed link
    assert payload["counts"]["image"] == 0
    assert payload["counts"]["all"] == 2


def test_has_history_survives_a_search_that_matches_nothing(db, tmp_path):
    """``has_history`` is "is there anything to filter".  A chip bar that
    vanishes the moment a typo matches nothing takes the way back out."""
    _seed(db)
    payload, _ = get_history(db, _Cfg(), query_str="zzzz-nothing")
    assert payload["total"] == 0
    assert payload["items"] == []
    assert payload["has_history"] is True

    fresh = ClipboardHistoryDB(storage_path=str(tmp_path / "empty.db"), max_entries=50)
    payload, _ = get_history(fresh, _Cfg(), query_str="zzzz")
    assert payload["has_history"] is False
    assert payload["counts"]["all"] == 0


# ── sort ───────────────────────────────────────────────────────────────


def test_oldest_flips_the_order(db):
    _seed(db)
    newest, _ = get_history(db, _Cfg(), sort_str="newest")
    oldest, _ = get_history(db, _Cfg(), sort_str="oldest")
    assert _previews(newest)[0] == "meeting notes"
    assert _previews(oldest)[0] != "meeting notes"
    assert _previews(oldest) == list(reversed(_previews(newest)))


def test_pinned_rows_float_to_the_top_in_both_orders(db):
    """Two stable passes, not one compound key: reversing a compound key would
    sink pinned rows to the bottom in one of the two orders."""
    _seed(db)
    rows = db.get_all()
    oldest = min(rows, key=lambda r: r["timestamp"])
    db.batch_set_pinned([oldest["entry_id"]], True)

    for sort in ("newest", "oldest"):
        payload, _ = get_history(db, _Cfg(), sort_str=sort)
        assert payload["items"][0]["pinned"] is True
        assert payload["sort"] == sort


def test_unknown_sort_falls_back_to_newest(db):
    _seed(db)
    payload, _ = get_history(db, _Cfg(), sort_str="shuffled")
    assert payload["sort"] == "newest"
    assert _previews(payload)[0] == "meeting notes"


# ── pagination inside the filtered set ──────────────────────────────────


def test_offset_steps_through_the_filtered_rows_not_the_whole_table(db):
    _seed(db)
    first, _ = get_history(db, _Cfg(), kind_str="text", limit_str="1", offset_str="0")
    second, _ = get_history(db, _Cfg(), kind_str="text", limit_str="1", offset_str="1")
    assert first["total"] == second["total"] == 2
    assert _previews(first) + _previews(second) == ["meeting notes", "https://example.com/b"]

    # Past the end of the filtered set — not a window onto the other kinds.
    beyond, _ = get_history(db, _Cfg(), kind_str="text", offset_str="2")
    assert beyond["items"] == []


def test_limit_stays_capped_when_a_query_is_present(db):
    """The ceiling exists because the response is one JSON document built in
    memory; a search must not become a way around it."""
    _seed(db)
    payload, _ = get_history(db, _Cfg(), limit_str="100000000")
    assert payload["limit"] <= 20 * 1000  # WEB_HISTORY_LIMIT_MAX, not the ask
    assert len(payload["items"]) == 5


# ── source ─────────────────────────────────────────────────────────────


def _two_machines(db):
    """Two rows captured here, one from each of two peers."""
    db.add(_text("local one", 2000.0))
    db.add(_text("local two", 2001.0))
    remote = _text("from the laptop", 2002.0)
    remote.source_device = "dev-laptop"
    db.add(remote)
    picture = _image(2003.0)
    picture.source_device = "dev-desk"
    db.add(picture)
    return db


def test_source_keeps_only_the_rows_from_one_machine(db):
    """The row already names its source, so tapping the name has to be able to
    mean "only that one" — the same question a search answers badly, because a
    device name matched as a word can also turn up in the clip itself."""
    _two_machines(db)
    payload, status = get_history(db, _Cfg(), source_str="dev-laptop")
    assert status == 200
    assert payload["total"] == 1
    assert _previews(payload) == ["from the laptop"]
    assert payload["source"] == "dev-laptop"


def test_local_source_is_the_rows_this_machine_captured(db):
    """Locally captured rows carry an empty ``source_device``, which an empty
    query parameter cannot tell apart from "no filter" — hence the token."""
    _two_machines(db)
    payload, _ = get_history(db, _Cfg(), source_str="local")
    assert payload["total"] == 2
    assert {item["source_device"] for item in payload["items"]} == {""}
    assert payload["source"] == "local"


def test_source_narrows_the_chip_counts_it_is_added_to(db):
    """A chip's badge is the number clicking it yields, under every filter that
    is on — a source filter included."""
    _two_machines(db)
    payload, _ = get_history(db, _Cfg(), source_str="dev-desk")
    assert payload["counts"]["all"] == 1
    assert payload["counts"]["image"] == 1
    assert payload["counts"]["text"] == 0


def test_a_source_that_matches_nothing_is_not_an_empty_history(db):
    """``has_history`` stays true, so the chip that got the reader here is
    still on screen to get them back."""
    _two_machines(db)
    payload, _ = get_history(db, _Cfg(), source_str="gone-away")
    assert payload["total"] == 0
    assert payload["items"] == []
    assert payload["has_history"] is True
    assert payload["source"] == "gone-away"


def test_no_source_parameter_keeps_every_machine(db):
    _two_machines(db)
    for source in (None, "", "   "):
        payload, _ = get_history(db, _Cfg(), source_str=source)
        assert payload["total"] == 4
        assert payload["source"] == ""


def test_source_combines_with_the_query_and_the_chip(db):
    _two_machines(db)
    payload, _ = get_history(db, _Cfg(), query_str="laptop", kind_str="text", source_str="dev-laptop")
    assert payload["total"] == 1
    payload, _ = get_history(db, _Cfg(), query_str="laptop", kind_str="image", source_str="dev-laptop")
    assert payload["total"] == 0


# ── the route wires the parameters through ──────────────────────────────


def test_route_passes_source_through(db):
    from internal.web.routes import dispatch

    _two_machines(db)
    status, _headers, body = dispatch(
        "GET",
        "/api/history",
        {"source": ["dev-laptop"]},
        b"",
        cfg=_Cfg(),
        history=db,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
    )
    assert status == 200
    import json

    payload = json.loads(body)
    assert payload["total"] == 1
    assert payload["source"] == "dev-laptop"
    assert payload["items"][0]["text_preview"] == "from the laptop"


def test_route_passes_q_kind_and_sort(db, tmp_path):
    from internal.web.routes import dispatch

    _seed(db)
    status, _headers, body = dispatch(
        "GET",
        "/api/history",
        {"q": ["meeting"], "kind": ["text"], "sort": ["oldest"]},
        b"",
        cfg=_Cfg(),
        history=db,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
    )
    assert status == 200
    import json

    payload = json.loads(body)
    assert payload["total"] == 1
    assert payload["kind"] == "text"
    assert payload["sort"] == "oldest"
    assert payload["items"][0]["text_preview"] == "meeting notes"
