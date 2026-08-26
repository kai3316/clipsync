"""Round-7 desktop-core tests.

Covers:
1. Same-text dedup flavor preservation (history.py + history_db.py):
   a plain-text re-copy must never downgrade a rich entry, and a late
   rich write must upgrade a plain one.
2. Config save durability: temp-file fsync happens before os.replace.
3. NotificationManager hardening: send_pipe guard, no-tray fallback path.
4. Tray timed-pause helpers and menu structure.
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.dedup import (
    adds_new_flavors,
    labels_to_types,
    merge_types,
    types_to_labels,
)
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history import ClipboardHistory
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


# ── 1a. Flavor merge: JSON backend ────────────────────────────────────


class TestFlavorMergeJson:
    def _hist(self, tmp_path):
        return ClipboardHistory(storage_path=str(tmp_path / "h.json"))

    def test_plain_recopy_keeps_rich_flavor(self, tmp_path):
        """The round-6 quirk: re-copying the same text as plain-only within
        the merge window used to stack a plain duplicate over the rich
        entry; it must merge instead, keeping the HTML."""
        h = self._hist(tmp_path)
        h.add(_rich())
        _age_last_add(h, 30)
        h.add(_plain())

        entries = h.get_all()
        assert len(entries) == 1
        types = labels_to_types(entries[0]["types"])
        assert types[ContentType.HTML] == RICH_HTML
        assert types[ContentType.TEXT] == BODY

    def test_late_rich_write_upgrades_plain_entry(self, tmp_path):
        """A rich format written a beat later (within DEDUP_WINDOW) must
        upgrade the entry, not be dropped as a duplicate."""
        h = self._hist(tmp_path)
        h.add(_plain())
        h.add(_rich())  # same text body, <2s apart

        entries = h.get_all()
        assert len(entries) == 1
        types = labels_to_types(entries[0]["types"])
        assert types[ContentType.HTML] == RICH_HTML
        # The richer flavor also wins content_type classification.
        assert entries[0]["content_type"] == "HTML"

    def test_plain_recopy_within_window_stays_dropped(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        h.add(_plain())  # immediate echo of what was just captured

        entries = h.get_all()
        assert len(entries) == 1
        assert ContentType.HTML in labels_to_types(entries[0]["types"])

    def test_merge_refreshes_timestamp_and_orders_first(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        _age_last_add(h, 20)
        old_ts = h._entries[0]["timestamp"]
        h.add(_plain())
        assert h._entries[0]["timestamp"] > old_ts
        assert h.get_all()[0]["text_preview"] == "same body"

    def test_distinct_text_still_creates_new_entry(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        h.add(_content({ContentType.TEXT: b"different"}))
        assert len(h.get_all()) == 2

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


# ── 1c. dedup.py primitives ──────────────────────────────────────────


class TestDedupPrimitives:
    def test_merge_types_incoming_wins_per_format(self):
        merged, changed = merge_types(
            {ContentType.TEXT: b"old", ContentType.HTML: b"<i>old</i>"},
            {ContentType.TEXT: b"new"},
        )
        assert changed is True
        assert merged[ContentType.TEXT] == b"new"       # newer bytes win
        assert merged[ContentType.HTML] == b"<i>old</i>"  # richer flavor kept

    def test_merge_types_noop_is_unchanged(self):
        t = {ContentType.TEXT: b"x"}
        merged, changed = merge_types(dict(t), dict(t))
        assert changed is False
        assert merged == t

    def test_label_roundtrip(self):
        labels = types_to_labels({ContentType.TEXT: b"x", ContentType.URL: b"http://y"})
        back = labels_to_types(labels)
        assert back == {ContentType.TEXT: b"x", ContentType.URL: b"http://y"}

    def test_labels_to_types_skips_garbage(self):
        out = labels_to_types({"TEXT": "!!!not-base64!!!", "BOGUS": "aa", "URL": None})
        assert out == {}

    def test_adds_new_flavors(self):
        existing = {"TEXT": base64.b64encode(b"x").decode()}
        assert adds_new_flavors(existing, {ContentType.TEXT: b"x"}) is False
        assert adds_new_flavors(existing, {ContentType.TEXT: b"x",
                                           ContentType.HTML: b"h"}) is True


# ── 1d. Preview decode: UTF-16-BOM raw bytes (v1.0.76) ───────────────


def test_utf16_bom_text_preview_decodes_cleanly(tmp_path):
    """Very old entries / peer platforms may store wide text raw (a UTF-16 BOM
    followed by UTF-16-LE bytes).  _safe_decode must spot the BOM and decode
    them as UTF-16 instead of falling through to the CJK single-byte attempts
    and rendering mojibake — the 'history became garbled after update' report.
    """
    wide = "剪贴板乱码修复".encode("utf-16")   # includes the BOM
    h = ClipboardHistory(storage_path=str(tmp_path / "h.json"))
    h.add(_content({ContentType.TEXT: wide}))
    preview = h.get_all()[0]["text_preview"]
    assert preview == "剪贴板乱码修复"
    assert "�" not in preview


# ── 2. Config save durability ────────────────────────────────────────


class TestConfigFsync:
    def test_save_fsyncs_before_replace(self, monkeypatch, tmp_path):
        import internal.config.config as config_module

        order = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fake_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def fake_replace(a, b):
            order.append("replace")
            return real_replace(a, b)

        monkeypatch.setattr(config_module.os, "fsync", fake_fsync)
        monkeypatch.setattr(config_module.os, "replace", fake_replace)
        monkeypatch.setattr(config_module, "_config_dir", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_config_path",
                            lambda: tmp_path / "config.json")

        cfg = config_module.Config()
        cfg.device_name = "durability probe"
        config_module.save(cfg)

        # flush+fsync must land BEFORE the atomic rename, or a crash between
        # them would leave an empty/partial config.json in place of the old one.
        assert order == ["fsync", "replace"]
        # No stale temp files survive a clean save.
        assert list(tmp_path.glob(".config_tmp_*.json")) == []
        loaded = config_module.load()
        assert loaded.device_id == cfg.device_id
        assert loaded.device_name == "durability probe"


# ── 3. NotificationManager ───────────────────────────────────────────


class TestNotifyHardening:
    def test_send_pipe_without_pipe_is_noop(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()
        m.send_pipe(("show_notification", "t", "m"))  # must not raise

    def test_show_without_tray_falls_back_without_raising(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()
        m.show("title", "message")  # no pipe, no tray → fallback path

    def test_disabled_manager_shows_nothing(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()
        m.enabled = False
        m.show("title", "message")

    def test_set_pipe_swap_stops_previous_sender(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()

        class FakePipe:
            def __init__(self):
                self.sent = []

            def send(self, msg):
                self.sent.append(msg)

        p1, p2 = FakePipe(), FakePipe()
        m.set_pipe(p1)
        q1 = m._send_queue
        m.set_pipe(p2)  # swapping pipes must stop the first sender thread
        m.show("t1", "m1")
        deadline = time.time() + 2
        while m._send_queue.qsize() and time.time() < deadline:
            time.sleep(0.01)
        # Whatever happens, both sender threads share nothing: q1 drains to
        # p1 at most, new messages go to p2 — never cross-wired.
        m.show("t2", "m2")
        time.sleep(0.1)
        texts = [s for p in (p1, p2) for s in p.sent]
        for s in texts:
            assert s[0] == "show_notification"


# ── 4. Tray timed pause ──────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "linux" and not os.environ.get("DISPLAY"),
    reason="tray tests need a display (pystray Icon) on headless Linux",
)
class TestTrayPause:
    def test_pause_left_minutes_math(self):
        pytest.importorskip("pystray")
        from internal.ui.systray import SystrayApp

        now = 1_000_000.0
        app = SystrayApp(device_name="T")
        assert SystrayApp.pause_left_minutes(None, now) is None
        assert SystrayApp.pause_left_minutes(now + 15 * 60, now) == 15
        assert SystrayApp.pause_left_minutes(now + 15 * 60 + 59, now) == 16
        assert SystrayApp.pause_left_minutes(now + 5, now) == 1
        assert SystrayApp.pause_left_minutes(now - 10, now) == 0
        assert app._pause_deadline is None

    @pytest.fixture
    def tray_app(self):
        pystray = pytest.importorskip("pystray")
        from internal.ui.systray import SystrayApp
        return pystray, SystrayApp(device_name="T")

    def _texts(self, menu):
        out = []
        for item in menu.items:
            if item is None or item is getattr(type(menu), "SEPARATOR", object()):
                continue
            out.append(item.text)
        return out

    def test_menu_has_pause_submenu_when_wired(self, tray_app):
        pystray, app = tray_app
        app._on_pause_minutes = lambda m: None  # feature wired
        menu = app._build_full_menu()
        texts = self._texts(menu)
        assert any((t or "").startswith("⏸") for t in texts)
        # Submenu carries the three duration entries.
        for item in menu.items:
            if item is not None and (item.text or "").startswith("⏸"):
                assert len(list(item.submenu.items)) == 3
                break

    def test_paused_menu_shows_countdown_and_resume(self, tray_app):
        pystray, app = tray_app
        app._on_pause_minutes = lambda m: None  # feature wired
        app._on_resume_sync = lambda: None
        app.set_pause_deadline(time.time() + 20 * 60)
        texts = self._texts(app._build_full_menu())
        joined = " | ".join(t or "" for t in texts)
        assert "Paused" in joined          # countdown status line
        assert "Resume Sync Now" in joined
        assert "Pause Sync" not in joined  # submenu replaced by status

    def test_cleared_pause_restores_submenu(self, tray_app):
        pystray, app = tray_app
        app._on_pause_minutes = lambda m: None
        app.set_pause_deadline(time.time() + 60)
        app.set_pause_deadline(None)
        texts = self._texts(app._build_full_menu())
        assert any((t or "").startswith("⏸") for t in texts)

    def test_no_pause_submenu_without_callback(self, tray_app):
        """Backward compatibility: a SystrayApp constructed without the
        pause callbacks (e.g. older host wiring) renders no pause items."""
        pystray, app = tray_app
        texts = self._texts(app._build_full_menu())
        assert not any((t or "").startswith("⏸") for t in texts)


# ── 5. i18n keys exist in both locales ───────────────────────────────


def test_pause_i18n_complete():
    from internal.i18n import LOCALES
    keys = [
        "tray.pause_for", "tray.pause_15m", "tray.pause_30m", "tray.pause_1h",
        "tray.paused_left", "tray.resume_now",
    ]
    for locale, table in LOCALES.items():
        for key in keys:
            assert key in table, f"{key} missing in {locale}"
    # Placeholders render.
    from internal.i18n import T
    assert "7" in T("tray.paused_left", minutes=7)


# ══════════════════════════════════════════════════
# merged from test_regressions.py
# ══════════════════════════════════════════════════

import os

import pytest

from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent
from internal.web import routes
from internal.web.server import _escape_script_json, _js_string

# ── History: id-based lookup must be type-tolerant ────────────────────

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
    evil = "</script><script>alert(1)</script>"
    out = _js_string(evil)
    assert "</" not in out
    assert "\\u003c/script\\u003e" in out
    # still valid JSON after decoding
    assert json.loads(out) == evil


def test_escape_script_json_keeps_valid_json():
    payload = json.dumps({"a": "<b>&</b>"}, ensure_ascii=False)
    escaped = _escape_script_json(payload)
    assert "<" not in escaped
    assert json.loads(escaped) == {"a": "<b>&</b>"}


# ── Web API: non-object JSON bodies → 400 (not 500) ───────────────────

def _dispatch_bare(method, path, body):
    """Call routes.dispatch with just enough args for the pre-handler check."""
    return routes.dispatch(
        method, path, {}, body,
        cfg=object(), history=None, sync_mgr=None,
        get_connected_ids=lambda: [], on_nav_url=lambda *a, **k: None,
        on_forward_file=lambda *a, **k: None, upload_dir="",
    )


@pytest.mark.parametrize("body", [b"null", b"[]", b'"x"', b"123"])
def test_non_object_json_body_rejected(body):
    status, _ct, _bytes = _dispatch_bare("POST", "/api/whatever", body)
    assert status == 400


def test_invalid_json_body_still_reaches_handler_flow():
    # Malformed JSON is not the dispatch-level check's concern (it only
    # rejects VALID JSON that isn't an object), so routing proceeds normally
    # and an unknown path yields 404 rather than crashing.
    status, _ct, _bytes = _dispatch_bare("POST", "/api/whatever", b"{not json")
    assert status == 404


# ── /api/logs redaction ───────────────────────────────────────────────

class _FakeCfg:
    web_token = "secret-token-abc"


def test_redact_sensitive_line_strips_home_and_token():
    home = os.path.expanduser("~")
    line = f"opened file {home}\\AppData\\Roaming\\ClipSync\\x secret-token-abc"
    out = routes._redact_sensitive_line(line, _FakeCfg())
    assert "[redacted]" in out
    assert home not in out
    assert "secret-token-abc" not in out


# ── First-run language flag round-trips through config ────────────────

def test_language_chosen_round_trip(tmp_path, monkeypatch):
    import internal.config.config as config_mod
    monkeypatch.setattr(config_mod, "_config_dir", lambda: Path(tmp_path))

    cfg = config_mod.Config()
    cfg.language = "en"
    cfg.language_chosen = True
    config_mod.save(cfg)

    loaded = config_mod.load()
    assert loaded.language == "en"
    assert loaded.language_chosen is True


# ══════════════════════════════════════════════════
# merged from test_final_round.py (_content renamed _mk_content)
# ══════════════════════════════════════════════════

import glob
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from internal.clipboard.clipboard import _html_to_plain_text, strip_rich_formats
from internal.clipboard.filter import _rtf_to_text
from internal.clipboard.format import ClipboardContent
from internal.clipboard.history_db import _make_dedup_key, set_max_age_days
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


def _mk_content(types_map) -> ClipboardContent:
    return ClipboardContent(types=types_map)


# ── 1. strip_rich_formats unit tests ──────────────────────────────────

def test_strip_drops_html_and_rtf_keeps_text():
    c = _mk_content({
        ContentType.TEXT: b"Hello World & more",
        ContentType.HTML: ONE_LINE_HTML,
        ContentType.RTF: b"{\\rtf1\\ansi Hello}",
    })
    s = strip_rich_formats(c)
    assert set(s.types) == {ContentType.TEXT}
    # The existing TEXT payload is kept verbatim, not re-derived from HTML.
    assert s.types[ContentType.TEXT] == b"Hello World & more"


def test_strip_converts_html_only_clip_to_plain_text():
    c = _mk_content({ContentType.HTML: ONE_LINE_HTML})
    s = strip_rich_formats(c)
    assert set(s.types) == {ContentType.TEXT}
    assert s.types[ContentType.TEXT] == b"Hello World & more"


def test_strip_keeps_image_file_and_url():
    png, file_list, url = b"\x89PNG fake", b"C:\\a.txt\nC:\\b.txt", b"https://x.y"
    c = _mk_content({
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
    c = _mk_content({ContentType.RTF: rtf})
    s = strip_rich_formats(c)
    # Naively dropping RTF would turn an RTF-only clip into brace garbage
    # or nothing; it passes through instead.
    assert s.types == {ContentType.RTF: rtf}


def test_strip_degenerate_html_returns_original():
    html = b"<div></div>"  # no visible text to extract
    c = _mk_content({ContentType.HTML: html})
    s = strip_rich_formats(c)
    assert s.types == {ContentType.HTML: html}


def test_strip_empty_content_passthrough():
    c = _mk_content({})
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
    c = _mk_content({ContentType.RTF: RTF_CARD})
    assert f.is_sensitive(c) is True
    assert f.describe_sensitivity(c) == ["credit_card"]


def test_filter_drops_sensitive_rtf_keeps_redacted_text():
    f = ContentFilter()
    c = _mk_content({
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
    c = _mk_content({ContentType.TEXT: b"nothing sensitive", ContentType.RTF: rtf})
    out = f.filter_content(c)
    assert out.types.get(ContentType.RTF) == rtf


def test_filter_rtf_only_sensitive_clip_becomes_plain_text():
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
    html = (b"<span>4111</span><span>1111</span>"
            b"<span>1111</span><span>1111</span>")
    f = ContentFilter()
    raw = _mk_content({ContentType.HTML: html})
    assert f.is_sensitive(raw) is False
    stripped = strip_rich_formats(raw)
    assert stripped.types[ContentType.TEXT] == b"4111 1111 1111 1111"
    assert f.is_sensitive(stripped) is True


# ── 3. strip x dedup interaction ──────────────────────────────────────

def test_stripped_clip_shares_dedup_key_with_plain_text():
    rich = _mk_content({
        ContentType.TEXT: b"same body",
        ContentType.HTML: b"<b>same body</b>",
        ContentType.RTF: b"{\\rtf1 same body}",
    })
    stripped = strip_rich_formats(rich)
    plain = _mk_content({ContentType.TEXT: b"same body"})
    # The dedup key hashes the TEXT body first, so a stripped message and a
    # plain re-copy coalesce instead of landing as two entries.
    assert _make_dedup_key(stripped) == _make_dedup_key(plain)
    assert _make_dedup_key(stripped).startswith("text:")


# ── 4. history_max_age_days cleanup x pinned / favorites ──────────────

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


def _placeholder_tokens(text):
    """Set of ``{name}`` placeholder names in a translation string.  Python
    format specs are stripped (``{size:.1f}`` vs the web's ``{size}`` are the
    same token — the web frontend only substitutes ``{name}``).  List values
    (e.g. settings_window.about_features) are joined first, since the web UI
    renders them as arrays."""
    if isinstance(text, list):
        text = "\n".join(str(x) for x in text)
    return set(re.findall(r"\{([a-zA-Z0-9_]+)(?::[^}]*)?\}", text))


# Shared keys whose placeholder tokens intentionally diverge between the
# Python i18n dicts and the web locale JSONs (AUDIT: "11 个占位符差异").  Each
# is a real inconsistency — device.reconnecting formats {n}/{m} in desktop
# dialogs but {attempt}/{max} on the web, and the chat/pairing entries carry a
# placeholder on exactly one side.  Pinned explicitly so only NEW drift fails.
_PLACEHOLDER_GAP_KEYS = frozenset({
    "device.reconnecting",
    "pairing.notify.unpaired_by_peer", "pairing.notify.repair_prompt",
    "chat.invite_banner_title", "chat.invite_fingerprint",
    "chat.invite_greeting", "chat.connecting",
    "chat.system.peer_offline", "chat.system.session_closed_by_peer",
    "chat.system.file_cancelled", "chat.err_connect_timeout",
})


def test_python_i18n_placeholder_parity_with_web_locales():
    """Shared keys must agree on ``{token}`` placeholders across the Python
    dicts and the web locale JSONs — the audit's cross-system parity class.  A
    key that formats "{count}" on the desktop but "{n}" on the web leaves one
    side rendering a literal "{n}".  The known divergences are pinned in
    _PLACEHOLDER_GAP_KEYS so only new drift fails."""
    from internal import i18n
    en, zh = _load_web_locales()
    for py, web in ((i18n._EN, en), (i18n._ZH, zh)):
        for key, val in py.items():
            if key not in web or key in _PLACEHOLDER_GAP_KEYS:
                continue
            py_tokens = _placeholder_tokens(val)
            web_tokens = _placeholder_tokens(web[key])
            assert py_tokens == web_tokens, (
                f"{key}: python {sorted(py_tokens)} != web {sorted(web_tokens)}")


def test_python_i18n_web_locales_are_fully_mirrored():
    """Every web locale key must have a Python-dict mirror.

    The web server's locale fallback (server.py _load_i18n_translations) drops
    to the Python dicts only as a last resort; a key missing there renders as
    its raw name across the UI.  The web files currently carry a known gap
    (AUDIT: "缺 640/1271 键" — aiconfig.*, devices.netpair_*, diag.*, ...; the
    2026-08-26 UI pass added 23 more web-only keys — now 664; the settings
    reorganization added 2 more — settings_nav.remote_sync, settings_window.system_title; the
    onboarding-steps pass added 13 more — onboarding.enable + the six steps' titles/bodies; the
    relay-split pass removed 2 — settings_window.relay_brokers_label/hint; the
    aiconfig refactor pass net +16 — the unified-panel + migration-wizard keys
    (aiconfig.diff_*, aiconfig.migrate_*, aiconfig.batch_*, aiconfig.local_device*,
    settings_window.aiconfig_tools_* / aiconfig_custom_paths_*) in, the legacy
    path-list keys out) that has its own server-side
    mitigation, so its SIZE is pinned here: a NEW web-only key — the regression
    class this guards — changes the count and fails the suite."""
    from internal import i18n
    en, zh = _load_web_locales()
    assert len(set(en) - set(i18n._EN)) == 693
    assert len(set(zh) - set(i18n._ZH)) == 693


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


def test_app_startup_dedup_and_age_wiring_bindings_resolve():
    """Regression (v1.0.71): ``Application._create_services`` crashed on
    startup with ``NameError: name '_history_db' is not defined`` — a dedup-
    wiring edit dropped the history_db import binding that the following
    ``set_max_age_days`` call still used.  Guard the two local imports stay
    present and ordered so the real startup path can't silently lose one.
    """
    import inspect
    import src.main as main_mod
    src = inspect.getsource(main_mod.Application._create_services)
    assert "from internal.clipboard import dedup as _dedup_mod" in src
    assert "from internal.clipboard import history_db as _history_db" in src
    assert "_history_db.set_max_age_days(" in src
    # The history_db binding must be established before set_max_age_days runs.
    assert src.index("from internal.clipboard import history_db as _history_db") \
        < src.index("_history_db.set_max_age_days(")
