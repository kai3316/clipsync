"""Every failed answer carries a stable code, and the phone speaks it.

The sidecar's error sentences were English only, so a Chinese phone page showed
`not available` at the exact moment the reader needed to know what to do about
it.  U22's plan was the small one: add a `code` to the frequent failures and keep
`error` untouched as the fallback — no contract break, nothing renamed.

Two tests below hold the two halves of that: the server adds the code at the one
place every JSON answer passes through, and the phone translates the codes it
knows.  The last test keeps the two tables from drifting, which is the failure
mode a mapping like this has: a code the server emits that the page has never
heard of reads as English, and nobody notices until a reader does.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from internal.web.routes import ERROR_CODES, _json_response, dispatch

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "internal" / "web" / "static" / "mobile.html"


def body(data, status=200):
    status_out, content_type, raw = _json_response(data, status)
    assert status_out == status
    assert content_type.startswith("application/json")
    return json.loads(raw.decode("utf-8"))


def test_a_failure_keeps_its_sentence_and_gains_a_code():
    payload = {"ok": False, "error": "not available"}
    decoded = body(payload, 503)
    assert decoded["error"] == "not available", "the sentence is the fallback and must not move"
    assert decoded["code"] == "unavailable"


def test_the_answer_is_a_copy_not_the_callers_dict():
    # Handlers keep what they return; a code written into it here would leak
    # into the next place that reads the same object.
    payload = {"ok": False, "error": "not found"}
    body(payload, 404)
    assert "code" not in payload


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"ok": True, "error": ""},
        {"ok": True, "items": []},
        # No `ok` at all, and no error either: the overview's own counters.
        {"connected_count": 2},
    ],
)
def test_a_success_payload_gains_nothing(payload):
    # `ok: true` with a code in it would read as a warning to anything that
    # checks for one.
    assert body(payload) == payload


def test_a_message_with_no_entry_keeps_its_own_words():
    # Inventing a code would make the phone say something the server never
    # meant; an unknown sentence is better left in the server's words.
    decoded = body({"ok": False, "error": "something nobody mapped"}, 400)
    assert decoded["error"] == "something nobody mapped"
    assert "code" not in decoded


def test_the_code_set_is_small_and_stable():
    # The page carries one sentence per code, so a new value here is a
    # translation nobody wrote.  Growing this set is a deliberate act.
    assert set(ERROR_CODES.values()) == {
        "unavailable",
        "not_found",
        "bad_request",
        "bad_url",
        "bad_path",
        "failed",
        "server_error",
    }


def test_a_real_request_carries_the_code_all_the_way_out():
    """The path, not just the function: a code added in `_json_response` reaches
    a client only if every handler really answers through it.

    `GET /api/history/item` with no id is answered by `internal/web/api/` — a
    module that returns a `(dict, status)` pair rather than calling the helper
    itself — so this is the code path that would have been missed by a per-site
    patch.
    """
    status, _ctype, raw = dispatch(
        "GET",
        "/api/history/item",
        {},
        b"",
        cfg=object(),
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
    )
    assert status == 400
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded["error"] == "entry_id required"
    assert decoded["code"] == "bad_request"


def test_the_phone_has_a_sentence_for_every_code_the_server_sends():
    text = PAGE.read_text(encoding="utf-8")
    block = re.search(r"var ERR_CODE_TEXT = \{(.*?)\n  \};", text, re.S)
    assert block, "the phone's code table is gone"
    keys = set(re.findall(r"^\s{4}([a-z_]+):", block.group(1), re.M))
    missing = set(ERROR_CODES.values()) - keys
    assert not missing, "the phone cannot say: " + ", ".join(sorted(missing))
    # And nothing extra: a sentence for a code the server never sends is dead
    # weight that reads as coverage.
    assert keys == set(ERROR_CODES.values())


def test_the_phone_never_shows_the_servers_english_directly():
    """Every failure toast goes through `apiError`, which is where the code is
    turned into words."""
    text = PAGE.read_text(encoding="utf-8")
    assert re.search(r"function apiError\(data, fallback\)", text)
    # No call site keeps its own `error ||` fallback; they all hand the body to
    # the one function that knows the codes.
    leftovers = re.findall(r"res\.data\.error\s*\|\||\(res\.data && res\.data\.error\)\s*\|\|", text)
    assert not leftovers, leftovers
    calls = len(re.findall(r"apiError\(", text)) - 1  # minus the definition
    assert calls >= 20, calls
