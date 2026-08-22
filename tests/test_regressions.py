"""Regression tests for fixes from the 1.0.13 audit.

Covers the highest-impact logic fixes that don't need a GUI or a live
network: history id handling, content-filter image-format preservation,
inline-script escaping, web JSON validation and log redaction, and the
first-run language flag round-trip.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
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
