"""How a typed search is read, in one place.

Every list in this build has a search box — the window's history, the phone
panel's history, the favourites list on both — and each of them used to answer
the same question its own way: a single lowercased substring against one
string.  That rule quietly promised less than the box looks like it promises.
"chrome 周报" (the application a clip was copied in, plus a word from it) found
nothing, because no row contains that exact run of characters; and the user had
no way to tell "there is no such clip" from "this box cannot do that", which is
the worse of the two failures.

So the rule lives here and every box asks this module:

* whitespace separates terms, and **every** term must be found — the words a
  user remembers about a clip are rarely adjacent, and requiring only one of
  them turns a search into a filter that returns almost everything;
* a pair of quotes groups its contents into one term, so a user who *does*
  remember a phrase can ask for it exactly (``"annual report"``);
* a term may match any of the fields a row shows, not just its visible line —
  searching the application name is how you find the clip you cannot quote.

Full-width space and the CJK quotation marks are read the same way as their
ASCII counterparts: this is a Chinese-first build, both arrive from an IME
without the user choosing them, and a search box that treats “周报” as three
characters including two quote marks is a search box that finds nothing.
"""

from __future__ import annotations

from collections.abc import Callable

#: Opening mark → the mark that closes it.  Only the kinds that arrive from an
#: IME or a keyboard are here; a phrase opened with one and closed with the
#: other (""…"") is not repaired, because guessing which half the user meant
#: would be guessing about the text itself.
_OPENERS = {'"': '"', "\u201c": "\u201d"}

#: Marks that end a phrase, as a set, so a stray one outside a phrase can be
#: swallowed instead of searched for.  A user who types a closing quote first
#: meant to group something, not to find a quote character.
_CLOSERS = frozenset(_OPENERS.values())


def terms(query: str) -> tuple[str, ...]:
    """*query* as the terms that must all be found, case-folded.

    Returned in the order they were typed, empty ones dropped.  An unterminated
    phrase runs to the end of the query and is kept as one term: the user is
    still typing, and the phrase they are about to close is the one they can
    see on screen — dropping the quote and splitting on the spaces would make
    the results jump twice for one keystroke.
    """
    found: list[str] = []
    buffer: list[str] = []
    closer = ""

    def flush() -> None:
        if buffer:
            found.append("".join(buffer))
            buffer.clear()

    for char in str(query or ""):
        if closer:
            if char == closer:
                flush()
                closer = ""
            else:
                buffer.append(char)
            continue
        if char in _OPENERS:
            # Whatever was being read before the quote is a term of its own:
            # `foo"bar baz"` is two terms, not one word glued to a phrase.
            flush()
            closer = _OPENERS[char]
            continue
        if char in _CLOSERS or char.isspace():
            flush()
            continue
        buffer.append(char)
    flush()
    return tuple(term for term in (part.casefold() for part in found) if term)


def matches(query: str, *fields: str) -> bool:
    """Whether every term of *query* appears in one of *fields*.

    Fields are searched together but each term stands alone: a term may land in
    the title while another lands in the body, which is what lets "figma 配色"
    find the favourite whose title says where it came from.  An empty query
    matches everything — a filter with nothing in it is not a filter.

    For one row.  A list wants ``matcher`` instead: this reads the query afresh
    on every call, and a list asks the same question once per row.
    """
    return matcher(query)(*fields)


def matcher(query: str) -> Callable[..., bool]:
    """*query* read once, as a predicate over a row's fields.

    The list side of ``matches``: the query is parsed here, not per row, so a
    history of thousands costs one parse rather than thousands — and a filter is
    run on every keystroke of a search box.
    """
    wanted = terms(query)
    if not wanted:
        return lambda *fields: True

    def looking_for(*fields: str) -> bool:
        haystack = "\n".join(str(field or "") for field in fields).casefold()
        return all(term in haystack for term in wanted)

    return looking_for
