"""Desktop-core history, dedup and locale tests.

Covers:
1. Same-text dedup flavor preservation (history.py + history_db.py): a plain
   re-copy must never downgrade a rich entry, a late rich write must upgrade a
   plain one, and the merge window itself is a boundary.
2. Preview decode of wide text (a UTF-16 BOM written raw by an older build).
3. History id lookup and delete, inline-script escaping, log redaction.
4. The outgoing strip of rich formats, against the sensitive filter and the
   dedup key.
5. Age prune versus pinned entries, and the favourites snapshot.
6. Web locale parity, and every statically referenced t() key resolving.
"""

import json
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.dedup import labels_to_types
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB

RICH_HTML = b"<html><body><b>same body</b></body></html>"
BODY = b"same body"


def _content(types: dict) -> ClipboardContent:
    return ClipboardContent(types=types)


def _rich() -> ClipboardContent:
    return _content({ContentType.TEXT: BODY, ContentType.HTML: RICH_HTML})


def _plain() -> ClipboardContent:
    return _content({ContentType.TEXT: BODY})


def _age_last_add(h, seconds: float) -> None:
    """Back-date the coalesce bookkeeping so the next same-key add falls
    outside DEDUP_WINDOW but stays inside FLAVOR_MERGE_WINDOW."""
    h._last_dedup_time = time.time() - seconds
    h._entries[0]["timestamp"] = time.time() - seconds


# ── 1a. Flavor merge: window and key edges ───────────────────────────


class TestFlavorMergeEdges:
    def _hist(self, tmp_path):
        return ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))

    def test_plain_recopy_within_window_stays_dropped(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        h.add(_plain())  # immediate echo of what was just captured

        entries = h.get_all()
        assert len(entries) == 1
        assert ContentType.HTML in labels_to_types(entries[0]["types"])

    def test_merge_window_expiry_starts_fresh_entry(self, tmp_path):
        """Past FLAVOR_MERGE_WINDOW a re-copy is deliberate again and gets
        its own entry (existing dedup semantics unchanged)."""
        h = self._hist(tmp_path)
        h.add(_rich())
        h._last_dedup_time = time.time() - 200
        h._entries[0]["timestamp"] = time.time() - 200
        h.add(_plain())

        entries = h.get_all()
        assert len(entries) == 2
        # Newest entry is the plain re-copy; the original rich one survives.
        assert ContentType.HTML not in labels_to_types(entries[0]["types"])
        assert ContentType.HTML in labels_to_types(entries[-1]["types"])

    def test_image_keys_never_merge_across_entries(self, tmp_path):
        """Only text-body keys merge; different images stay distinct."""
        h = self._hist(tmp_path)
        h.add(_content({ContentType.IMAGE_PNG: b"\x89PNG-one"}))
        h._last_dedup_time = time.time() - 10
        h._entries[0]["timestamp"] = time.time() - 10
        h.add(_content({ContentType.IMAGE_PNG: b"\x89PNG-two"}))
        assert len(h.get_all()) == 2


# ── 1b. Flavor merge: SQLite backend ──────────────────────────────────


class TestFlavorMergeDb:
    def _hist(self, tmp_path):
        return ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))

    def test_plain_recopy_keeps_rich_flavor(self, tmp_path):
        """The round-6 quirk: re-copying the same text as plain-only within
        the merge window used to stack a plain duplicate over the rich
        entry; it must merge instead, keeping the HTML."""
        db = self._hist(tmp_path)
        db.add(_rich())
        _age_last_add(db, 30)
        db.add(_plain())

        entries = db.get_all()
        assert len(entries) == 1
        types = labels_to_types(entries[0]["types"])
        assert types[ContentType.HTML] == RICH_HTML
        assert types[ContentType.TEXT] == BODY

    def test_merge_survives_restart(self, tmp_path):
        """The merged row (not just the in-memory list) keeps the flavor."""
        db = self._hist(tmp_path)
        db.add(_rich())
        _age_last_add(db, 30)
        db.add(_plain())

        reopened = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        entries = reopened.get_all()
        assert len(entries) == 1
        assert ContentType.HTML in labels_to_types(entries[0]["types"])
        assert entries[0]["timestamp"] >= time.time() - 5

    def test_late_rich_write_upgrades_plain_entry(self, tmp_path):
        db = self._hist(tmp_path)
        db.add(_plain())
        db.add(_rich())
        entries = db.get_all()
        assert len(entries) == 1
        assert entries[0]["content_type"] == "HTML"
        assert ContentType.HTML in labels_to_types(entries[0]["types"])

    def test_merge_keeps_pinned_and_paste_count(self, tmp_path):
        db = self._hist(tmp_path)
        db.add(_rich())
        db.batch_set_pinned([db._entries[0]["entry_id"]], True)
        db.increment_paste(str(db._entries[0]["entry_id"]))
        _age_last_add(db, 30)
        db.add(_plain())

        entries = db.get_all()
        assert len(entries) == 1
        assert entries[0]["pinned"] is True
        assert entries[0]["paste_count"] == 1


# ── 1c. Preview decode: UTF-16-BOM raw bytes (v1.0.76) ───────────────


def test_utf16_bom_text_preview_decodes_cleanly(tmp_path):
    """Very old entries / peer platforms may store wide text raw (a UTF-16 BOM
    followed by UTF-16-LE bytes).  _safe_decode must spot the BOM and decode
    them as UTF-16 instead of falling through to the CJK single-byte attempts
    and rendering mojibake — the 'history became garbled after update' report.
    """
    wide = "剪贴板乱码修复".encode("utf-16")  # includes the BOM
    h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
    h.add(_content({ContentType.TEXT: wide}))
    preview = h.get_all()[0]["text_preview"]
    assert preview == "剪贴板乱码修复"
    assert "�" not in preview


# ── 2. History: id-based lookup and delete ────────────────────────────


@pytest.fixture
def history_db(tmp_path):
    db = ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"), max_entries=50)
    yield db


def _entry(content: str, timestamp: float):
    return ClipboardContent(
        types={ContentType.TEXT: content.encode("utf-8")},
        timestamp=timestamp,
    )


def test_find_by_id_accepts_str_and_int(history_db):
    history_db.add(_entry("hello", 1000.0), source_app=None)
    entry_id = history_db.get_all()[0]["entry_id"]
    assert isinstance(entry_id, int)

    # int (JSON number) and str (URL query param) both resolve
    _idx, entry = history_db.find_by_id(entry_id)
    assert entry is not None and entry.get("text_preview") == "hello"
    _idx, entry = history_db.find_by_id(str(entry_id))
    assert entry is not None and entry.get("text_preview") == "hello"


def test_delete_by_id_removes_only_target(history_db):
    history_db.add(_entry("alpha", 1000.0), source_app=None)
    history_db.add(_entry("beta", 2000.0), source_app=None)
    ids = [e["entry_id"] for e in history_db.get_all()]  # newest first: [beta, alpha]
    assert history_db.delete_by_id(str(ids[1])) is True  # delete alpha
    remaining = [e["text_preview"] for e in history_db.get_all()]
    assert remaining == ["beta"]


# ── Content filter: image format hint must survive filtering ──────────


def test_filter_content_preserves_image_fmt():
    from internal.clipboard.filter import ContentFilter

    content = ClipboardContent(
        types={
            ContentType.IMAGE_PNG: b"\x89PNG-fake-bytes",
            ContentType.TEXT: b"user@example.com",
        },
        image_fmt="bmp",
    )
    filtered = ContentFilter().filter_content(content)
    assert filtered.image_fmt == "bmp", "filtered image must keep its format hint"
    assert filtered.types[ContentType.IMAGE_PNG] == content.types[ContentType.IMAGE_PNG]


# ── Inline-script escaping (stored XSS via device name) ───────────────


def test_js_string_cannot_break_out_of_script():
    from internal.web.server import _js_string

    evil = "</script><script>alert(1)</script>"
    out = _js_string(evil)
    assert "</" not in out
    assert "\\u003c/script\\u003e" in out
    # still valid JSON after decoding
    assert json.loads(out) == evil


# ── /api/logs redaction ───────────────────────────────────────────────


class _FakeCfg:
    web_token = "secret-token-abc"


def test_redact_sensitive_line_strips_home_and_token():
    from internal.web import routes

    home = os.path.expanduser("~")
    line = f"opened file {home}\\AppData\\Roaming\\ClipSync\\x secret-token-abc"
    out = routes._redact_sensitive_line(line, _FakeCfg())
    assert "[redacted]" in out
    assert home not in out
    assert "secret-token-abc" not in out


# ── 3. strip_rich_formats and the sensitive filter ────────────────────

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.history_db import _make_dedup_key, set_max_age_days
from internal.web.api import favorites as favorites_api
from internal.web.api.history import batch_favorite

ONE_LINE_HTML = (
    b"<html><head><style>p{color:red}</style></head><body>"
    b"<b>Hello</b> World &amp; more<script>alert(1)</script></body></html>"
)

# Classic Luhn-valid test card.
CARD_TEXT = "Pay 4111 1111 1111 1111 today"
CARD_TEXT_BYTES = CARD_TEXT.encode("utf-8")

RTF_CARD = b"{\\rtf1\\ansi\\deff0 {\\*\\generator ClipSync}\\par Pay 4111 1111 1111 1111 today}"


def _mk_content(types_map) -> ClipboardContent:
    return ClipboardContent(types=types_map)


def test_strip_drops_html_and_rtf_keeps_text():
    c = _mk_content(
        {
            ContentType.TEXT: b"Hello World & more",
            ContentType.HTML: ONE_LINE_HTML,
            ContentType.RTF: b"{\\rtf1\\ansi Hello}",
        }
    )
    s = strip_rich_formats(c)
    assert set(s.types) == {ContentType.TEXT}
    # The existing TEXT payload is kept verbatim, not re-derived from HTML.
    assert s.types[ContentType.TEXT] == b"Hello World & more"


def test_strip_keeps_rtf_only_clip_unchanged():
    rtf = b"{\\rtf1\\ansi\\deff0 Just rich text}"
    c = _mk_content({ContentType.RTF: rtf})
    s = strip_rich_formats(c)
    # Naively dropping RTF would turn an RTF-only clip into brace garbage
    # or nothing; it passes through instead.
    assert s.types == {ContentType.RTF: rtf}


def test_filter_drops_sensitive_rtf_keeps_redacted_text():
    from internal.clipboard.filter import ContentFilter

    f = ContentFilter()
    c = _mk_content(
        {
            ContentType.TEXT: CARD_TEXT_BYTES,
            ContentType.RTF: RTF_CARD,
        }
    )
    out = f.filter_content(c)
    # The unredactable RTF payload must not ride along next to [FILTERED].
    assert ContentType.RTF not in out.types
    redacted = out.types[ContentType.TEXT]
    assert b"[FILTERED]" in redacted
    assert b"4111" not in redacted


def test_filter_rtf_only_sensitive_clip_becomes_plain_text():
    from internal.clipboard.filter import ContentFilter

    f = ContentFilter()
    c = _mk_content({ContentType.RTF: RTF_CARD})
    out = f.filter_content(c)
    assert ContentType.RTF not in out.types
    # The clip must still sync — its redacted visible text becomes TEXT.
    assert ContentType.TEXT in out.types
    assert b"[FILTERED]" in out.types[ContentType.TEXT]


def test_strip_first_order_makes_tagged_card_detectable():
    # Digits split across inline tags are invisible to the filter on raw
    # markup but detectable after the plain-text strip — this locks in the
    # strip-then-filter order of the outgoing sync path.
    from internal.clipboard.filter import ContentFilter

    html = b"<span>4111</span><span>1111</span><span>1111</span><span>1111</span>"
    f = ContentFilter()
    raw = _mk_content({ContentType.HTML: html})
    assert f.is_sensitive(raw) is False
    stripped = strip_rich_formats(raw)
    assert stripped.types[ContentType.TEXT] == b"4111 1111 1111 1111"
    assert f.is_sensitive(stripped) is True


# ── 4. strip x dedup interaction ──────────────────────────────────────


def test_stripped_clip_shares_dedup_key_with_plain_text():
    rich = _mk_content(
        {
            ContentType.TEXT: b"same body",
            ContentType.HTML: b"<b>same body</b>",
            ContentType.RTF: b"{\\rtf1 same body}",
        }
    )
    stripped = strip_rich_formats(rich)
    plain = _mk_content({ContentType.TEXT: b"same body"})
    # The dedup key hashes the TEXT body first, so a stripped message and a
    # plain re-copy coalesce instead of landing as two entries.
    assert _make_dedup_key(stripped) == _make_dedup_key(plain)
    assert _make_dedup_key(stripped).startswith("text:")


# ── 5. history_max_age_days cleanup x pinned / favorites ──────────────


def test_age_prune_spares_pinned_entries(tmp_path):
    from unittest.mock import patch

    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"), max_entries=50)
    # The DB stamps local receipt time (a sender's clock must not reorder
    # history), so inject genuine age by freezing the clock back 5 days
    # while the entries are added.
    old_ts = time.time() - 5 * 86400
    counter = {"n": 0}

    def fake_time():
        counter["n"] += 1
        return old_ts + counter["n"] * 100.0  # > FLAVOR_MERGE_WINDOW apart

    with patch("internal.clipboard.history_db.time.time", side_effect=fake_time):
        db.add(ClipboardContent(types={ContentType.TEXT: b"pinned note"}))
        db.add(ClipboardContent(types={ContentType.TEXT: b"stale note"}))
    entries = db.get_all()
    pinned_id = next(e["entry_id"] for e in entries if e["text_preview"] == "pinned note")
    stale_id = next(e["entry_id"] for e in entries if e["text_preview"] == "stale note")
    assert db.batch_set_pinned([pinned_id], True) == 1

    set_max_age_days(1)
    try:
        db._prune_by_age()
    finally:
        set_max_age_days(0)

    remaining = {e["entry_id"] for e in db.get_all()}
    assert remaining == {pinned_id}
    # The DB rows were deleted too — a fresh instance sees the same state.
    fresh = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"), max_entries=50)
    assert {e["entry_id"] for e in fresh.get_all()} == {pinned_id}
    assert stale_id not in remaining


def test_batch_favorite_snapshot_survives_history_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(favorites_api, "_FAV_DB_PATH", str(tmp_path / "favorites.db"))
    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"), max_entries=50)
    long_text = "keep me in favorites " * 30  # far beyond the 200-char preview cut
    db.add(ClipboardContent(types={ContentType.TEXT: long_text.encode("utf-8")}))
    eid = db.get_all()[0]["entry_id"]

    body = json.dumps({"entry_ids": [eid], "group": ""}).encode("utf-8")
    data, status = batch_favorite(body, db)
    assert status == 200 and data["ok"] is True and data["count"] == 1

    # Any later history removal (age prune, clear, delete) must not ghost
    # the favorite: favorites store their own content snapshot.
    assert db.delete_by_id(eid) is True

    favs = favorites_api.get_favorites()[0]["favorites"]
    assert len(favs) == 1
    assert favs[0]["content"] == long_text  # full text, not the truncated preview

    # Cleanup for process-global state hygiene.
    favorites_api.delete_favorite(json.dumps({"id": favs[0]["id"]}).encode("utf-8"))


# ── 6. web locale parity ──────────────────────────────────────────────


def _load_web_locales():
    en = json.load(
        open(os.path.join(ROOT, "internal/web/static/locales/en.json"), encoding="utf-8")  # noqa: SIM115
    )
    zh = json.load(
        open(os.path.join(ROOT, "internal/web/static/locales/zh-CN.json"), encoding="utf-8")  # noqa: SIM115
    )
    return en, zh


def test_web_locale_en_zh_key_parity():
    en, zh = _load_web_locales()
    en_keys, zh_keys = set(en), set(zh)
    assert en_keys == zh_keys, (
        f"only in en.json: {sorted(en_keys - zh_keys)}; "
        f"only in zh-CN.json: {sorted(zh_keys - en_keys)}"
    )


def _read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


# The one placeholder that reads the same in both languages, so a front end is
# right to leave it out -- and wrong to leave out any other.
_LANGUAGE_NEUTRAL_LABEL = "[HTML]"


def _placeholder_previews_written():
    """The bracketed previews ``history_db`` can store, read off its own source.

    From the function that builds previews rather than off a ``return`` shape:
    one of the four comes back from a conditional expression, and a scan that
    saw only the plain returns would agree with a map that had quietly dropped
    it.
    """
    body = _read("internal/clipboard/history_db.py")
    body = body[body.index("def _build_preview("):]
    body = body[: body.index("\ndef ", 1)]
    return set(re.findall(r'"(\[[A-Za-z ]+\])"', body))


def test_the_panel_translates_every_placeholder_preview():
    """A clip with no text of its own is previewed by a bracketed label.

    ``history_db`` decides what those labels are -- an image, a vector image
    and rich text have nothing to cut a preview out of -- and every front end
    used to print them verbatim, so a Chinese window read "[Image]" on the line
    above the row's own 图片 chip.  ``ClipsyncAPI.previewText`` translates them,
    which makes its map a contract with the database: a label added there and
    not here reverts to English with nothing to catch it.
    """
    written = _placeholder_previews_written()
    assert len(written) == 4, f"unexpected placeholder previews: {sorted(written)}"

    block = _read("internal/web/static/js/api.js")
    block = block[block.index("var PREVIEW_LABELS = {"):]
    block = block[: block.index("};")]
    mapped = dict(re.findall(r"'(\[[^']+\])':\s*'([^']+)'", block))

    assert written - set(mapped) == {_LANGUAGE_NEUTRAL_LABEL}, (
        f"untranslated placeholder previews: {sorted(written - set(mapped))}"
    )
    assert set(mapped) <= written, (
        f"the map names labels history_db never writes: {sorted(set(mapped) - written)}"
    )

    en, zh = _load_web_locales()
    for label, key in sorted(mapped.items()):
        assert key in en, f"{label} has no {key} in en.json"
        assert key in zh, f"{label} has no {key} in zh-CN.json"


def test_the_shell_translates_the_same_placeholder_previews():
    """The window's map, held to the database the same way the panel's is.

    ``desktop/src/i18n/format.ts`` keeps its own copy because the shell shares
    no code with the panel; two tables drifting apart is exactly the bug this
    pair of tests exists to make impossible.
    """
    written = _placeholder_previews_written()

    block = _read("desktop/src/i18n/format.ts")
    block = block[block.index("const PLACEHOLDER_PREVIEWS"):]
    block = block[: block.index("]);")]
    mapped = set(re.findall(r'\["(\[[^"]+\])",', block))

    assert written - mapped == {_LANGUAGE_NEUTRAL_LABEL}, (
        f"untranslated placeholder previews: {sorted(written - mapped)}"
    )
    assert mapped <= written, (
        f"the map names labels history_db never writes: {sorted(mapped - written)}"
    )


def test_the_placeholder_labels_are_looked_up_from_stored_content():
    """Both maps are indexed by a string that came out of the database.

    A bare ``map[key]`` would answer ``map['constructor']`` with a function and
    put it on screen, so both fronts have to ask for the key rather than index
    with it -- the one hazard the language makes easy to miss.
    """
    api = _read("internal/web/static/js/api.js")
    panel = api[api.index("previewText: function"):]
    panel = panel[: panel.index("\n    },")]
    assert "hasOwnProperty.call(PREVIEW_LABELS" in panel, (
        "the panel's previewText indexes its map with stored content"
    )

    mobile = _read("internal/web/static/mobile.html")
    phone = mobile[mobile.index("function previewText("):]
    phone = phone[: phone.index("\n  }")]
    assert "hasOwnProperty.call(PREVIEW_LABELS" in phone, (
        "the phone's previewText indexes its map with stored content"
    )

    # The shell's is a Map, which has no prototype keys to collide with.
    shell = _read("desktop/src/i18n/format.ts")
    assert "new Map<string, () => string>" in shell, (
        "the shell's placeholder map is not a Map"
    )


def test_web_t_literals_resolve_in_both_locales():
    """Every statically referenced t('...') key exists in en.json AND zh-CN.json.

    Dynamic prefixes like t('hotkeys.' + key) end with a dot and are
    skipped; js/i18n.js documents its own fallback with a demo literal and
    is skipped wholesale.
    """
    import glob
    import re

    en, zh = _load_web_locales()
    pattern = re.compile(r"""(?:^|[^a-zA-Z0-9_])t\(\s*['"]([a-zA-Z0-9_.]+)['"]""")
    files = sorted(
        set(glob.glob(os.path.join(ROOT, "internal/web/static/**/*.js"), recursive=True))
        | set(glob.glob(os.path.join(ROOT, "internal/web/static/*.html")))
    )
    missing = {}
    checked = 0
    for fp in files:
        rel = os.path.relpath(fp, ROOT).replace("\\", "/")
        if rel.endswith("js/i18n.js"):
            continue
        src = open(fp, encoding="utf-8").read()  # noqa: SIM115
        for m in pattern.finditer(src):
            key = m.group(1)
            if key.endswith("."):  # dynamic prefix, resolved at runtime
                continue
            checked += 1
            if key not in en or key not in zh:
                missing.setdefault(key, []).append(rel)
    assert checked > 100  # sanity: the scan actually saw the UI strings
    assert not missing, f"unresolved t() keys: {missing}"
