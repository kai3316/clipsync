"""Final optimization-round tests: cross-feature interaction sweep + closing checks.

Groups covered:
  1. plain_text_only strip function unit tests (HTML -> plain text, RTF
     drop, image/file retention, degenerate inputs).
  2. strip x sensitive-filter interaction: the strip-first order in the
     sender path makes HTML-only clips detectable; RTF payloads are now
     filterable too (a sensitive RTF used to ride to peers unredacted
     next to a [FILTERED] TEXT payload).
  3. strip x dedup: stripping keeps the TEXT-body dedup key stable so the
     same content cannot land as two history entries across format sets.
  4. history_max_age_days cleanup x pinned/favorites: age pruning never
     touches pinned rows; favorites snapshot content, so pruning history
     leaves no ghost references behind.
  5. i18n consistency: desktop _EN/_ZH key parity, web locale en/zh-CN
     parity, and every statically referenced t('...') key resolves in
     both web locales.
  6. Theme JSON: every CTk widget class the code instantiates has a theme
     entry (customtkinter raises KeyError on missing widget keys), color
     pairs are well-formed, and unused stock-widget keys stay defined.
  7. webview_window: Firefox templates carry no dead -width/-height flags
     (removed from Firefox; --window-size is screenshot-only there).
"""

import glob
import json
import os
import re
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from internal.clipboard.clipboard import _html_to_plain_text, strip_rich_formats
from internal.clipboard.filter import ContentFilter, _rtf_to_text
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB, _make_dedup_key, set_max_age_days
from internal.web.api import favorites as favorites_api
from internal.web.api.history import batch_favorite

# ── Helpers ────────────────────────────────────────────────────────────

ONE_LINE_HTML = (
    b"<html><head><style>p{color:red}</style></head><body>"
    b"<b>Hello</b> World &amp; more<script>alert(1)</script></body></html>"
)

# Classic Luhn-valid test card.
CARD_TEXT = "Pay 4111 1111 1111 1111 today"
CARD_TEXT_BYTES = CARD_TEXT.encode("utf-8")

RTF_CARD = (
    b"{\\rtf1\\ansi\\deff0 {\\*\\generator ClipSync}"
    b"\\par Pay 4111 1111 1111 1111 today}"
)


def _content(types_map) -> ClipboardContent:
    return ClipboardContent(types=types_map)


# ── 1. strip_rich_formats unit tests ──────────────────────────────────

def test_strip_drops_html_and_rtf_keeps_text():
    c = _content({
        ContentType.TEXT: b"Hello World & more",
        ContentType.HTML: ONE_LINE_HTML,
        ContentType.RTF: b"{\\rtf1\\ansi Hello}",
    })
    s = strip_rich_formats(c)
    assert set(s.types) == {ContentType.TEXT}
    # The existing TEXT payload is kept verbatim, not re-derived from HTML.
    assert s.types[ContentType.TEXT] == b"Hello World & more"


def test_strip_converts_html_only_clip_to_plain_text():
    c = _content({ContentType.HTML: ONE_LINE_HTML})
    s = strip_rich_formats(c)
    assert set(s.types) == {ContentType.TEXT}
    assert s.types[ContentType.TEXT] == b"Hello World & more"


def test_strip_keeps_image_file_and_url():
    png, file_list, url = b"\x89PNG fake", b"C:\\a.txt\nC:\\b.txt", b"https://x.y"
    c = _content({
        ContentType.IMAGE_PNG: png,
        ContentType.FILE: file_list,
        ContentType.URL: url,
        ContentType.HTML: ONE_LINE_HTML,
    })
    s = strip_rich_formats(c)
    assert s.types[ContentType.IMAGE_PNG] == png
    assert s.types[ContentType.FILE] == file_list
    assert s.types[ContentType.URL] == url
    assert ContentType.HTML not in s.types


def test_strip_keeps_rtf_only_clip_unchanged():
    rtf = b"{\\rtf1\\ansi\\deff0 Just rich text}"
    c = _content({ContentType.RTF: rtf})
    s = strip_rich_formats(c)
    # Naively dropping RTF would turn an RTF-only clip into brace garbage
    # or nothing; it passes through instead.
    assert s.types == {ContentType.RTF: rtf}


def test_strip_degenerate_html_returns_original():
    html = b"<div></div>"  # no visible text to extract
    c = _content({ContentType.HTML: html})
    s = strip_rich_formats(c)
    assert s.types == {ContentType.HTML: html}


def test_strip_empty_content_passthrough():
    c = _content({})
    s = strip_rich_formats(c)
    assert s.types == {}


def test_html_to_plain_text_drops_script_style_and_unescapes():
    out = _html_to_plain_text(ONE_LINE_HTML)
    assert out == b"Hello World & more"
    assert b"alert" not in out and b"color:red" not in out


def test_rtf_to_text_extraction_for_detection():
    text = _rtf_to_text(RTF_CARD)
    assert "4111 1111 1111 1111" in text
    assert "generator" not in text  # {\*\...} destination group dropped
    assert "\\" not in text


# ── 2. strip x sensitive-filter interaction ───────────────────────────

def test_filter_detects_sensitive_rtf():
    f = ContentFilter()
    c = _content({ContentType.RTF: RTF_CARD})
    assert f.is_sensitive(c) is True
    assert f.describe_sensitivity(c) == ["credit_card"]


def test_filter_drops_sensitive_rtf_keeps_redacted_text():
    f = ContentFilter()
    c = _content({
        ContentType.TEXT: CARD_TEXT_BYTES,
        ContentType.RTF: RTF_CARD,
    })
    out = f.filter_content(c)
    # The unredactable RTF payload must not ride along next to [FILTERED].
    assert ContentType.RTF not in out.types
    redacted = out.types[ContentType.TEXT]
    assert b"[FILTERED]" in redacted
    assert b"4111" not in redacted


def test_filter_keeps_clean_rtf():
    f = ContentFilter()
    rtf = b"{\\rtf1\\ansi Nothing sensitive here}"
    c = _content({ContentType.TEXT: b"nothing sensitive", ContentType.RTF: rtf})
    out = f.filter_content(c)
    assert out.types.get(ContentType.RTF) == rtf


def test_filter_rtf_only_sensitive_clip_becomes_plain_text():
    f = ContentFilter()
    c = _content({ContentType.RTF: RTF_CARD})
    out = f.filter_content(c)
    assert ContentType.RTF not in out.types
    # The clip must still sync — its redacted visible text becomes TEXT.
    assert ContentType.TEXT in out.types
    assert b"[FILTERED]" in out.types[ContentType.TEXT]


def test_strip_first_order_makes_tagged_card_detectable():
    # Digits split across inline tags are invisible to the filter on raw
    # markup but detectable after the plain-text strip — this locks in the
    # strip-then-filter order of the outgoing sync path.
    html = (b"<span>4111</span><span>1111</span>"
            b"<span>1111</span><span>1111</span>")
    f = ContentFilter()
    raw = _content({ContentType.HTML: html})
    assert f.is_sensitive(raw) is False
    stripped = strip_rich_formats(raw)
    assert stripped.types[ContentType.TEXT] == b"4111 1111 1111 1111"
    assert f.is_sensitive(stripped) is True


# ── 3. strip x dedup interaction ──────────────────────────────────────

def test_stripped_clip_shares_dedup_key_with_plain_text():
    rich = _content({
        ContentType.TEXT: b"same body",
        ContentType.HTML: b"<b>same body</b>",
        ContentType.RTF: b"{\\rtf1 same body}",
    })
    stripped = strip_rich_formats(rich)
    plain = _content({ContentType.TEXT: b"same body"})
    # The dedup key hashes the TEXT body first, so a stripped message and a
    # plain re-copy coalesce instead of landing as two entries.
    assert _make_dedup_key(stripped) == _make_dedup_key(plain)
    assert _make_dedup_key(stripped).startswith("text:")


# ── 4. history_max_age_days cleanup x pinned / favorites ──────────────

def test_age_prune_spares_pinned_entries(tmp_path):
    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"), max_entries=50)
    old_ts = time.time() - 5 * 86400
    db.add(ClipboardContent(types={ContentType.TEXT: b"pinned note"},
                            timestamp=old_ts))
    db.add(ClipboardContent(types={ContentType.TEXT: b"stale note"},
                            timestamp=old_ts - 10))
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


# ── 5. i18n consistency ───────────────────────────────────────────────

def test_desktop_i18n_en_zh_key_parity():
    from internal import i18n
    en_keys, zh_keys = set(i18n._EN), set(i18n._ZH)
    assert en_keys == zh_keys, (
        f"only in EN: {sorted(en_keys - zh_keys)}; "
        f"only in ZH: {sorted(zh_keys - en_keys)}"
    )


def _load_web_locales():
    en = json.load(open(os.path.join(ROOT, "internal/web/static/locales/en.json"),
                        encoding="utf-8"))
    zh = json.load(open(os.path.join(ROOT, "internal/web/static/locales/zh-CN.json"),
                        encoding="utf-8"))
    return en, zh


def test_web_locale_en_zh_key_parity():
    en, zh = _load_web_locales()
    en_keys, zh_keys = set(en), set(zh)
    assert en_keys == zh_keys, (
        f"only in en.json: {sorted(en_keys - zh_keys)}; "
        f"only in zh-CN.json: {sorted(zh_keys - en_keys)}"
    )


def test_web_t_literals_resolve_in_both_locales():
    """Every statically referenced t('...') key exists in en.json AND zh-CN.json.

    Dynamic prefixes like t('hotkeys.' + key) end with a dot and are
    skipped; js/i18n.js documents its own fallback with a demo literal and
    is skipped wholesale.
    """
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
        src = open(fp, encoding="utf-8").read()
        for m in pattern.finditer(src):
            key = m.group(1)
            if key.endswith("."):  # dynamic prefix, resolved at runtime
                continue
            checked += 1
            if key not in en or key not in zh:
                missing.setdefault(key, []).append(rel)
    assert checked > 100  # sanity: the scan actually saw the UI strings
    assert not missing, f"unresolved t() keys: {missing}"


def test_hotkeys_dynamic_prefix_keys_exist():
    """The dynamic t('hotkeys.' + key) family resolves in both locales."""
    en, zh = _load_web_locales()
    hotkey_keys = {k for k in en if k.startswith("hotkeys.")}
    assert hotkey_keys, "no hotkeys.* keys found"
    for k in hotkey_keys:
        assert k in zh


# ── 6. theme JSON consistency ─────────────────────────────────────────

_THEME_PATH = os.path.join(ROOT, "assets", "themes", "clipsync.json")
# Widget classes that take no theme entry (not themeable via the JSON).
_CTK_NON_THEMED = {"CTkImage"}
# Structural sections: never instantiated via ``ctk.X(...)`` but consumed by
# customtkinter itself (root-window background, dropdown menu popups).
_CTK_STRUCTURAL = {"CTk", "DropdownMenu"}


def _ctk_classes_used_in_code():
    names = set()
    for dir_name in ("internal/ui", "src"):
        for fp in glob.glob(os.path.join(ROOT, dir_name, "**", "*.py"), recursive=True):
            src = open(fp, encoding="utf-8").read()
            names.update(re.findall(r"\bctk\.(CTk[A-Za-z]+)\b", src))
    return names


def test_theme_covers_every_ctk_widget_class_used():
    """A missing widget key crashes construction: customtkinter reads
    ThemeManager.theme[class][prop] directly with no default fallback."""
    theme = json.load(open(_THEME_PATH, encoding="utf-8"))
    used = _ctk_classes_used_in_code()
    assert used, "code scan found no CTk classes"
    absent = sorted(n for n in used if n not in theme and n not in _CTK_NON_THEMED)
    assert not absent, f"theme lacks entries for widgets the code uses: {absent}"
    # The stock widgets customtkinter itself can instantiate must stay
    # defined even when today's code does not touch them (same crash class).
    for required in ("CTkOptionMenu", "CTkComboBox", "CTkScrollbar"):
        assert required in theme


def test_theme_has_no_unknown_or_malformed_entries():
    """Every theme section must be a real customtkinter theme key.

    Foreign sections (the removed CTkTabview was one) are dead weight and
    hide typos; stock-widget sections stay defined even where today's code
    does not instantiate the widget yet.
    """
    theme = json.load(open(_THEME_PATH, encoding="utf-8"))
    expected = {
        # Structural pseudo-classes read by customtkinter itself.
        "CTk", "CTkToplevel", "DropdownMenu",
        # Widget classes (themed surface of the whole stock toolkit).
        "CTkFrame", "CTkButton", "CTkLabel", "CTkEntry",
        "CTkComboBox", "CTkOptionMenu", "CTkCheckBox", "CTkSwitch",
        "CTkRadioButton", "CTkProgressBar", "CTkSlider",
        "CTkSegmentedButton", "CTkTextbox", "CTkScrollableFrame",
        "CTkScrollbar", "CTkFont",
    }
    assert set(theme) == expected, (
        f"unknown: {sorted(set(theme) - expected)}; "
        f"missing: {sorted(expected - set(theme))}"
    )
    # Color values: either a string ("transparent"/named color) or a
    # [light, dark] pair of exactly two entries.  Geometry props carry
    # plain ints; CTkFont (size/slant) is skipped entirely.
    for widget, props in theme.items():
        if widget == "CTkFont":
            continue
        for prop, value in props.items():
            ok = (
                isinstance(value, str)
                or (isinstance(value, int) and not isinstance(value, bool))
                or (isinstance(value, list) and len(value) == 2
                    and all(isinstance(v, str) for v in value))
            )
            assert ok, f"{widget}.{prop} is not a valid light/dark color pair: {value!r}"


# ── 7. webview_window Firefox flags ───────────────────────────────────

def test_firefox_templates_carry_no_dead_size_flags():
    from internal.ui.webview_window import _BROWSERS
    firefox = [tmpl for name, tmpl, plats in _BROWSERS if name == "firefox"]
    assert firefox, "firefox entries missing from browser table"
    for tmpl in firefox:
        assert "--new-window" in tmpl
        assert any("{url}" in a for a in tmpl)
        for arg in tmpl:
            assert "--width" not in arg and "--height" not in arg
            assert "{width}" not in arg and "{height}" not in arg


def test_chromium_templates_keep_app_mode_and_size():
    """Every desktop non-Firefox entry is Chromium-family and must keep its
    --app window plus --window-size sizing."""
    from internal.ui.webview_window import _BROWSERS
    desktop = [(name, tmpl) for name, tmpl, plats in _BROWSERS
               if "Darwin" not in plats and name != "firefox"]
    assert desktop
    for name, tmpl in desktop:
        assert any(a.startswith("--app={url}") for a in tmpl), name
        assert "--window-size={width},{height}" in tmpl, name
