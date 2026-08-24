"""Round 7 web-face regression tests.

Covers:
  1. multipart boundary hardening in internal/web/server.py — truncated
     bodies / missing closing delimiter now fail with an explicit error
     instead of writing a partial file; file content is preserved
     byte-for-byte (trailing CRLF no longer stripped); quote-aware
     Content-Disposition parsing keeps ';'/'=' inside filenames.
  2. GET /api/logs tail semantics — reads only the log tail, honours
     ?lines= and the new ?tail= alias, clamps, and redacts the web token.
  3. POST /api/favorites/export — one-click Markdown/text export of all
     favorites (new feature), plus the frontend wiring guards for the two
     component bugs fixed this round (chat bubble :class expression,
     favorites-panel lifecycle placement) and i18n key parity.
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.web.server import (
    _MultipartError,
    _check_declared_length,
    _parse_content_disposition,
    _parse_multipart,
)
from internal.web.routes import dispatch


# ── Helpers ────────────────────────────────────────────────────────────


def _mp_body(fields, boundary="testboundary123"):
    """Build a browser-style multipart/form-data body.

    fields: list of (name, filename_or_None, bytes).
    """
    out = bytearray()
    for name, filename, data in fields:
        out += b"--" + boundary.encode() + b"\r\n"
        if filename is not None:
            disp = f'form-data; name="{name}"; filename="{filename}"'
        else:
            disp = f'form-data; name="{name}"'
        out += b"Content-Disposition: " + disp.encode() + b"\r\n"
        out += b"Content-Type: application/octet-stream\r\n"
        out += b"\r\n"
        out += data
        out += b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return bytes(out)


def _ct(boundary="testboundary123"):
    return f"multipart/form-data; boundary={boundary}"


# ── 1. Multipart parsing ───────────────────────────────────────────────

def test_parse_multipart_roundtrip_preserves_trailing_crlf():
    """A file whose content ends with newlines must arrive byte-for-byte:
    the old parser rstripped the part after cutting at the closing boundary
    and silently corrupted every text file ending in \\r\\n."""
    content = b"line1\r\nline2\r\n\r\n"
    body = _mp_body([("file", "notes.txt", content)])
    fields = _parse_multipart(body, _ct())
    assert fields["file"][0] == "notes.txt"
    assert fields["file"][1] == content, "file bytes must be preserved exactly"


def test_parse_multipart_rejects_truncated_body():
    """A body cut off mid-upload has no closing --boundary-- — it must raise
    (the endpoint answers 400) instead of silently accepting garbage."""
    full = _mp_body([("file", "big.bin", b"A" * 500)])
    truncated = full[: len(full) // 2]  # cut before the closing delimiter
    with pytest.raises(_MultipartError):
        _parse_multipart(truncated, _ct())


def test_parse_multipart_rejects_wrong_preamble():
    junk = b"this is not multipart at all" * 10
    with pytest.raises(_MultipartError):
        _parse_multipart(junk, _ct())


def test_parse_multipart_rejects_missing_boundary_header():
    body = _mp_body([("file", "x.bin", b"data")])
    with pytest.raises(_MultipartError):
        _parse_multipart(body, "multipart/form-data")


def test_parse_multipart_empty_form_returns_no_fields():
    assert _parse_multipart(
        b"--testboundary123--\r\n", _ct()) == {}


def test_parse_multipart_filename_with_semicolon():
    """Quote-aware Content-Disposition parsing: a filename containing ';'
    (legal on every OS) survives intact."""
    line = 'form-data; name="file"; filename="report;final=v2.pdf"'
    name, filename = _parse_content_disposition(line)
    assert name == "file"
    assert filename == "report;final=v2.pdf"


def test_parse_multipart_boundary_like_bytes_inside_file_survive():
    """File content that happens to contain a near-boundary sequence must
    not be cut — only the exact standalone delimiter line splits parts."""
    boundary = "abc123"
    content = b"before\r\n--" + boundary.encode() + b"XYZ-after\r\ntail"
    body = _mp_body([("file", "f.bin", content)], boundary=boundary)
    fields = _parse_multipart(body, _ct(boundary))
    assert fields["file"][1] == content


def test_parse_multipart_extra_text_fields_decoded():
    body = _mp_body([
        ("file", "photo.jpg", b"JPGDATA"),
        ("device_id", None, b"peer-42"),
        ("purpose", None, b"chat"),
    ])
    fields = _parse_multipart(body, _ct())
    assert fields["device_id"] == ("", b"peer-42")
    assert fields["purpose"] == ("", b"chat")
    assert fields["file"] == ("photo.jpg", b"JPGDATA")


def test_check_declared_length_flags_short_read():
    err = _check_declared_length(b"partial", 100)
    assert err is not None
    assert "incomplete upload" in err
    # Exact match and no-declared-length cases pass.
    assert _check_declared_length(b"x" * 100, 100) is None
    assert _check_declared_length(b"", 0) is None


# ── 2. GET /api/logs tail semantics ───────────────────────────────────

class _Cfg:
    device_id = "dev1"
    device_name = "Dev"
    web_token = "sekret-token"


def _write_log(tmp_path, n_lines):
    lines = [f"2026-08-25 10:00:{i:02d} INFO log line {i}" for i in range(n_lines)]
    (tmp_path / "clipsync.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def _get_logs(query_params):
    return dispatch(
        "GET", "/api/logs", query_params, b"",
        cfg=_Cfg(),
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
    )


def test_api_logs_returns_last_n_lines(monkeypatch, tmp_path):
    from internal.config import config as config_module
    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)
    lines = _write_log(tmp_path, 300)

    status, _ct_, body_b = _get_logs({"lines": ["5"]})
    assert status == 200
    logs = json.loads(body_b)["logs"]
    assert len(logs) == 5
    assert logs[-1].endswith("log line 299"), "must be the LAST lines of the file"
    assert logs[0].endswith("log line 295")


def test_api_logs_tail_alias_param(monkeypatch, tmp_path):
    from internal.config import config as config_module
    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)
    _write_log(tmp_path, 50)

    status, _ct_, body_b = _get_logs({"tail": ["3"]})
    assert status == 200
    assert len(json.loads(body_b)["logs"]) == 3


def test_api_logs_invalid_value_falls_back_to_200(monkeypatch, tmp_path):
    from internal.config import config as config_module
    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)
    _write_log(tmp_path, 250)

    _status, _ct_, body_b = _get_logs({"lines": ["not-a-number"]})
    assert len(json.loads(body_b)["logs"]) == 200


def test_api_logs_clamped_to_1000(monkeypatch, tmp_path):
    from internal.config import config as config_module
    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)
    _write_log(tmp_path, 1200)

    _status, _ct_, body_b = _get_logs({"lines": ["99999"]})
    assert len(json.loads(body_b)["logs"]) == 1000


def test_api_logs_redacts_web_token(monkeypatch, tmp_path):
    from internal.config import config as config_module
    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)
    (tmp_path / "clipsync.log").write_text(
        "INFO request used token sekret-token ok\n", encoding="utf-8")

    _status, _ct_, body_b = _get_logs({"lines": ["10"]})
    logs = json.loads(body_b)["logs"]
    assert len(logs) == 1
    assert "sekret-token" not in logs[0]
    assert "[redacted]" in logs[0]


def test_api_logs_missing_file_returns_empty(monkeypatch, tmp_path):
    from internal.config import config as config_module
    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)

    status, _ct_, body_b = _get_logs({})
    assert status == 200
    assert json.loads(body_b)["logs"] == []


# ── 3. Favorites export (new feature) ────────────────────────────────

@pytest.fixture()
def fav_db(tmp_path, monkeypatch):
    from internal.web.api import favorites as favorites_api
    monkeypatch.setattr(favorites_api, "_FAV_DB_PATH",
                        str(tmp_path / "favorites.db"))
    # Keep a legacy JSON on this machine from migrating into the test DB.
    monkeypatch.setattr(favorites_api, "_get_json_path",
                        lambda: str(tmp_path / "no_legacy.json"))
    return favorites_api


def _seed_two_favorites(favorites_api):
    favorites_api.add_favorite(json.dumps(
        {"title": "Alpha note", "content": "alpha-content", "group": "Work"}).encode())
    favorites_api.add_favorite(json.dumps(
        {"title": "", "content": "ungrouped-content", "group": ""}).encode())


def test_export_favorites_markdown_groups_and_content(fav_db, tmp_path):
    api = fav_db
    _seed_two_favorites(api)
    body = json.dumps({"format": "markdown"}).encode("utf-8")
    data, status = api.export_favorites(body, dest_dir=str(tmp_path))
    assert status == 200 and data["ok"] is True
    assert data["count"] == 2
    assert data["filename"].endswith(".md")
    text = open(data["filepath"], encoding="utf-8").read()
    assert "# ClipSync Favorites" in text
    assert "## Work" in text
    assert "**Alpha note**" in text
    assert "alpha-content" in text
    assert "## Ungrouped" in text
    assert "ungrouped-content" in text
    # Stored order preserved (position ASC): Alpha first.
    assert text.index("**Alpha note**") < text.index("ungrouped-content")


def test_export_favorites_text_format(fav_db, tmp_path):
    api = fav_db
    _seed_two_favorites(api)
    body = json.dumps({"format": "text"}).encode("utf-8")
    data, status = api.export_favorites(body, dest_dir=str(tmp_path))
    assert status == 200 and data["ok"] is True
    assert data["filename"].endswith(".txt")
    text = open(data["filepath"], encoding="utf-8").read()
    assert "[Work] Alpha note" in text
    assert "ungrouped-content" in text


def test_export_favorites_fence_grows_past_backticks(fav_db, tmp_path):
    api = fav_db
    api.add_favorite(json.dumps(
        {"title": "code", "content": "```python\nprint(1)\n```",
         "group": ""}).encode())
    body = json.dumps({"format": "markdown"}).encode("utf-8")
    data, _status = api.export_favorites(body, dest_dir=str(tmp_path))
    text = open(data["filepath"], encoding="utf-8").read()
    # The fence around the content must be LONGER than any backtick run in
    # the content itself so the block cannot be broken open.
    assert "\n````\n```python\nprint(1)\n```\n````\n" in text


def test_export_favorites_invalid_format_400(fav_db, tmp_path):
    data, status = fav_db.export_favorites(
        json.dumps({"format": "pdf"}).encode("utf-8"), dest_dir=str(tmp_path))
    assert status == 400
    assert data["ok"] is False


def test_export_favorites_empty_list_ok(fav_db, tmp_path):
    data, status = fav_db.export_favorites(
        json.dumps({}).encode("utf-8"), dest_dir=str(tmp_path))
    assert status == 200
    assert data["ok"] is True and data["count"] == 0


def test_export_favorites_invalid_json_400(fav_db):
    data, status = fav_db.export_favorites(b"{not-json")
    assert status == 400
    assert data["ok"] is False


# ── Frontend wiring guards ───────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_repo_file(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_chat_panel_failed_bubble_class_expression_valid():
    """The failed-bubble :class ternary was shipped with an empty else-arm
    (`... : ]`) — a JS syntax error that broke Vue's runtime template
    compilation, taking down the whole chat panel since v1.0.54."""
    src = _read_repo_file("internal/web/static/components/chat-panel.js")
    normalized = src.replace("\\'", "'")
    assert "'chat-bubble--failed' : ''" in normalized
    assert "' : ]" not in normalized


def test_favorites_panel_lifecycle_hooks_at_component_top_level():
    """mounted/beforeUnmount were nested INSIDE methods, where Vue never
    calls them — the group context menu could never be dismissed by an
    outside click or Escape. They must be top-level component options."""
    src = _read_repo_file("internal/web/static/components/favorites-panel.js")
    # Top-level hooks sit at 4-space indent directly under the component
    # object, right after the methods block closes (comment lines allowed
    # in between).
    assert re.search(
        r"\n    \},\n\n(?:    //[^\n]*\n)*    mounted: function \(\) \{", src,
    ), "mounted must be a top-level component option (4-space indent)"
    assert re.search(r"\n    beforeUnmount: function \(\) \{", src)
    # No lifecycle hook left nested inside methods (6-space indent).
    assert "\n      mounted: function" not in src
    assert "\n      beforeUnmount: function" not in src


def test_export_endpoint_wiring_end_to_end():
    """routes.py exposes POST /api/favorites/export, api.js wraps it, and the
    favorites panel calls it behind the Export button."""
    routes = _read_repo_file("internal/web/routes.py")
    assert 'path == "/api/favorites/export"' in routes
    assert "export_favorites" in routes
    api_js = _read_repo_file("internal/web/static/js/api.js")
    assert "exportFavorites: function (format)" in api_js
    assert "'/api/favorites/export'" in api_js
    panel = _read_repo_file(
        "internal/web/static/components/favorites-panel.js")
    assert "ClipsyncAPI.exportFavorites('markdown')" in panel
    assert "@click=\"exportFavorites\"" in panel


def test_mobile_page_export_parity():
    """mobile.html gets the same one-click favorites export: a button wired
    to POST /api/favorites/export with bilingual inline strings, shown only
    when there are favorites."""
    html = _read_repo_file("internal/web/static/mobile.html")
    assert 'id="favExportBtn"' in html
    assert "apiFetch('/api/favorites/export'" in html
    assert "{ format: 'markdown' }" in html
    # Bilingual inline strings (the mobile page's own i18n style).
    assert "favExport:" in html and "favExportDone:" in html
    assert "'⬇️ 导出全部为 Markdown'" in html
    # Hidden when there is nothing to export.
    assert "favoritesItems.length === 0 ? 'none' : 'flex'" in html


def test_new_i18n_keys_present_in_both_locales():
    keys = [
        "favorites.export",
        "favorites.export_tooltip",
        "favorites.exported",
        "favorites.export_failed",
    ]
    base = os.path.join(_ROOT, "internal", "web", "static", "locales")
    with open(os.path.join(base, "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(base, "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    for k in keys:
        assert k in en, f"missing {k} in en.json"
        assert k in zh, f"missing {k} in zh-CN.json"
        assert isinstance(en[k], str) and isinstance(zh[k], str)
