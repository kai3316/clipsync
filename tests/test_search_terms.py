"""How a search box is read, and that both lists read it the same way.

The rule itself is a handful of lines, but it is the line between a search box
and a filter: one lowercased substring over the whole string made "chrome 周报"
find nothing, and both the window and the phone showed that as "no such clip".
These cover the reading (terms, quotes, separators) and the two lists that
consume it — history through the window's use case, favourites through theirs.
"""

import pytest

from internal.application.use_cases import search as search_module
from internal.application.use_cases.favorites import FavoritesUseCase
from internal.application.use_cases.history import HistoryUseCase
from internal.application.use_cases.search import matches, terms
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB

# ── Reading a query ──────────────────────────────────────────────────


class TestTerms:
    def test_whitespace_separates_terms(self):
        assert terms("chrome 周报") == ("chrome", "周报")

    def test_full_width_space_is_a_separator(self):
        """An IME inserts U+3000 and the user never sees the difference."""
        assert terms("chrome\u3000周报") == ("chrome", "周报")

    def test_runs_of_separators_produce_no_empty_terms(self):
        assert terms("  chrome \t\n 周报  ") == ("chrome", "周报")
        assert "" not in terms("a  b")

    def test_terms_are_case_folded(self):
        assert terms("Chrome") == ("chrome",)
        # casefold, not lower: the two differ on the letters that stand for two
        assert terms("Stra\u00dfe") == ("strasse",)

    def test_quoted_run_is_one_term(self):
        assert terms('annual "report q3"') == ("annual", "report q3")

    def test_cjk_quotes_group_a_phrase_too(self):
        assert terms("\u201c周报 q3\u201d") == ("周报 q3",)

    def test_word_glued_to_an_opening_quote_stays_its_own_term(self):
        assert terms('foo"bar baz"') == ("foo", "bar baz")

    def test_unterminated_quote_runs_to_the_end(self):
        """The user is mid-word, not making a syntax error: keep what they see."""
        assert terms('a "b c') == ("a", "b c")

    def test_stray_closing_quote_is_swallowed(self):
        assert terms("a\u201d b") == ("a", "b")

    def test_empty_phrase_contributes_nothing(self):
        assert terms('""') == ()
        assert terms('a "" b') == ("a", "b")

    @pytest.mark.parametrize("query", ["", "   ", None])
    def test_nothing_typed_is_no_terms(self, query):
        assert terms(query) == ()


class TestMatches:
    def test_every_term_must_be_found(self):
        assert matches("chrome 周报", "chrome - 周报草稿")
        assert not matches("chrome 周报", "chrome - 月报")
    def test_order_does_not_matter(self):
        assert matches("周报 chrome", "chrome 周报草稿")

    def test_term_may_land_in_either_field(self):
        assert matches("figma 配色", "Figma 文件", "按钮配色方案")

    def test_terms_are_substrings_not_words(self):
        """A clipboard search is a find-in-page, not a tokenizer: "rep" is how
        the user starts typing "report"."""
        assert matches("rep", "quarterly report")

    def test_empty_query_matches_everything(self):
        assert matches("", "anything at all")
        assert matches(None, "")

    def test_missing_field_is_not_a_crash(self):
        assert not matches("x", None, "")


class TestMatcher:
    """The list side: the query is read once, then asked of every row."""

    def test_the_query_is_parsed_once(self, monkeypatch):
        reads = []
        real = search_module.terms

        def counting(query):
            reads.append(query)
            return real(query)

        monkeypatch.setattr(search_module, "terms", counting)
        hit = search_module.matcher("chrome 周报")
        assert reads == ["chrome 周报"]
        for _ in range(5):
            hit("chrome - 周报草稿")
        assert reads == ["chrome 周报"]

    def test_an_empty_query_is_a_predicate_that_says_yes(self):
        hit = search_module.matcher("   ")
        assert hit("anything") is True
        assert hit(None, "") is True


# ── The window's history ─────────────────────────────────────────────


@pytest.fixture
def history_db(tmp_path):
    return ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"), max_entries=50)


def _row(db, body, ts, app=None, title=None):
    content = ClipboardContent(
        types={ContentType.TEXT: body.encode("utf-8")}, timestamp=ts
    )
    if app is None:
        db.add(content)
    else:
        db.add(content, source_app={"name": app, "title": title or ""})


class TestHistorySearch:
    def test_two_words_from_different_columns(self, history_db):
        """The whole point: the app a clip came from and a word in it are two
        different places, and the user remembers both."""
        _row(history_db, "季度周报草稿", 1000.0, app="Chrome")
        _row(history_db, "季度月报草稿", 1001.0, app="Chrome")
        _row(history_db, "周报备份", 1002.0, app="Slack")
        payload = HistoryUseCase(repository=history_db).list(query="chrome 周报")
        assert [item["preview"] for item in payload["items"]] == ["季度周报草稿"]
        assert payload["total"] == 1

    def test_phrase_stays_whole(self, history_db):
        _row(history_db, "meeting notes for q3", 1000.0)
        _row(history_db, "notes from the meeting", 1001.0)
        payload = HistoryUseCase(repository=history_db).list(query='"meeting notes"')
        assert [item["preview"] for item in payload["items"]] == [
            "meeting notes for q3"
        ]

    def test_chip_counts_follow_the_terms(self, history_db):
        """A chip's badge is the number of rows clicking it yields, so it has
        to be counted over the searched set, not over everything."""
        _row(history_db, "周报 a", 1000.0, app="Chrome")
        _row(history_db, "周报 b", 1001.0, app="Firefox")
        payload = HistoryUseCase(repository=history_db).list(query="chrome")
        assert payload["counts"]["text"] == 1

    def test_total_and_page_agree(self, history_db):
        for index in range(4):
            _row(history_db, "周报 " + str(index), 1000.0 + index, app="Chrome")
        use_case = HistoryUseCase(repository=history_db)
        page = use_case.list(query="chrome 周报", offset=1, limit=2)
        assert page["total"] == 4
        assert len(page["items"]) == 2


# ── Favourites ───────────────────────────────────────────────────────


class _Favourites:
    """The two reads ``FavoritesUseCase.list`` makes, and nothing else."""

    def __init__(self, entries):
        self._entries = entries

    def get_all(self):
        return [dict(entry) for entry in self._entries]

    def group_names(self):
        return []


def _favourite(entry_id, title, content, group=""):
    return {
        "id": entry_id,
        "title": title,
        "content": content,
        "group": group,
        "position": 0,
        # The use case validates what a row says it stored, and a favourite's
        # timestamps are epoch numbers here as they are in the database.
        "created": 1000.0,
        "updated": 1000.0,
    }


class TestFavouritesSearch:
    def test_terms_may_split_between_title_and_body(self):
        repo = _Favourites([_favourite("a", "Figma 设计稿", "主色板与按钮配色")])
        payload = FavoritesUseCase(repository=repo).list(query="figma 配色")
        assert [item["id"] for item in payload["items"]] == ["a"]
        assert payload["total"] == 1

    def test_a_term_that_is_nowhere_drops_the_row(self):
        repo = _Favourites([_favourite("a", "Figma 设计稿", "主色板")])
        assert FavoritesUseCase(repository=repo).list(query="figma 字体")["total"] == 0

    def test_group_filter_still_applies_alongside_the_terms(self):
        repo = _Favourites(
            [
                _favourite("a", "会议纪要", "周报", group="工作"),
                _favourite("b", "会议纪要", "周报", group="私人"),
            ]
        )
        payload = FavoritesUseCase(repository=repo).list(query="周报", group="工作")
        assert [item["id"] for item in payload["items"]] == ["a"]
