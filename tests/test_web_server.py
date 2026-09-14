"""The Python side of the web panel's wire and its file/multipart handling.

  * the frames the server writes to a browser: the PING a keepalive sends, the
    delivered count broadcast() reports, the history_item_deleted payload, and
    which clients the heartbeat collects;
  * a queued dialog's response window starting at the flush that showed it;
  * DELETE /api/files: name confinement (traversal, directories, absolute
    paths) and exact-name deletion;
  * the multipart parser and the declared-length check behind uploads;
  * GET /api/logs (tail semantics and token redaction) and the favourites
    export endpoint's file output;
  * two request-path/field-type hardening cases that decide the status a
    client sees;
  * that starting the companion resolves no names -- binding its socket must
    not make the OS look this machine up.
"""

import contextlib
import json
import os
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.web import server as web_server
from internal.web.dialog import DialogManager
from internal.web.routes import dispatch
from internal.web.ws import WebSocketClient, WebSocketManager

# ── Helpers ────────────────────────────────────────────────────────────


def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _make_db(tmp_path) -> ClipboardHistoryDB:
    db = ClipboardHistoryDB(
        storage_path=str(tmp_path / "history.db"),
        max_entries=50,
    )
    db.add(
        ClipboardContent(types={ContentType.TEXT: b"hello"}, timestamp=1000.0),
        source_app=None,
    )
    return db


def _read_frame(sock):
    """Read one server→client WS frame: returns (opcode, payload_bytes)."""
    header = sock.recv(2)
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        ext = sock.recv(2)
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = sock.recv(8)
        length = struct.unpack("!Q", ext)[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return opcode, payload


# ── WS keepalive + delivery counts ─────────────────────────────────────


def test_ws_send_ping_writes_ping_frame():
    a, b = socket.socketpair()
    try:
        client = WebSocketClient(a, ("127.0.0.1", 0))
        assert client.send_ping() is True
        opcode, payload = _read_frame(b)
        assert opcode == 0x9  # PING
        assert payload == b""
    finally:
        a.close()
        b.close()


def test_ws_broadcast_returns_delivered_count():
    a, b = socket.socketpair()
    mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None, get_connected_ids=lambda: [])
    try:
        client = WebSocketClient(a, ("127.0.0.1", 0))
        with mgr._lock:
            mgr._clients.append(client)
        assert mgr.broadcast("toast", {"message": "hi"}) == 1
        opcode, payload = _read_frame(b)
        assert opcode == 0x1
        assert json.loads(payload)["type"] == "toast"
        # A closed client does not count as delivered.
        client._closed = True
        assert mgr.broadcast("toast", {"message": "hi"}) == 0
    finally:
        mgr.shutdown()
        a.close()
        b.close()


def test_ws_broadcast_history_deleted_payload():
    a, b = socket.socketpair()
    mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None, get_connected_ids=lambda: [])
    try:
        client = WebSocketClient(a, ("127.0.0.1", 0))
        with mgr._lock:
            mgr._clients.append(client)
        mgr.broadcast_history_deleted([101, 202])
        opcode, payload = _read_frame(b)
        assert opcode == 0x1
        msg = json.loads(payload)
        assert msg["type"] == "history_item_deleted"
        assert msg["data"]["entry_ids"] == [101, 202]
    finally:
        mgr.shutdown()
        a.close()
        b.close()


def test_ws_heartbeat_drops_dead_and_silent_clients():
    a1, b1 = socket.socketpair()
    a2, b2 = socket.socketpair()
    a3, b3 = socket.socketpair()
    mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None, get_connected_ids=lambda: [])
    try:
        dead = WebSocketClient(a1, ("127.0.0.1", 0))
        dead._closed = True  # connection already gone
        silent = WebSocketClient(a2, ("127.0.0.1", 0))
        silent.last_recv = time.monotonic() - 9999  # way past 3×30s
        fresh = WebSocketClient(a3, ("127.0.0.1", 0))
        with mgr._lock:
            mgr._clients.extend([dead, silent, fresh])

        stale = mgr._ping_and_collect_stale()
        assert dead in stale
        assert silent in stale
        assert fresh not in stale

        mgr._drop_clients(stale)
        with mgr._lock:
            assert dead not in mgr._clients
            assert silent not in mgr._clients
            assert fresh in mgr._clients
    finally:
        mgr.shutdown()
        for s in (a1, a2, a3, b1, b2, b3):
            with contextlib.suppress(OSError):
                s.close()


# ── dialog delivery-based broadcast + queued-window timing ─────────────


def test_dialog_show_times_out_when_never_shown():
    class ZeroMgr:
        def broadcast(self, msg_type, data):
            return 0

    dm = DialogManager()
    dm.ws_manager = ZeroMgr()
    result = dm.show("alert", title="t", message="m", timeout=0.15)
    assert result is None
    with dm._lock:
        assert dm._pending == {}
        assert dm._queued_dialogs == []


def test_dialog_queued_flushed_response_window_starts_at_flush():
    """A queued dialog's response window starts when it is flushed (shown),
    not when it was created — a response arriving after the creation deadline
    but within the post-flush window must still be accepted.

    Timing notes: the sleeps are sized so every critical margin is ~1s, so a
    loaded CI runner stretching ``time.sleep`` a few tens of percent cannot
    flake it.  With timeout=3s, queue≈2s (flush) and post-flush≈2s: the flush
    must land before the 3s creation budget (2s slack) and the response must
    land past that budget yet before the flush+3s window end (1s slack each).
    """
    state = {"deliver": False}

    class FlakyMgr:
        def broadcast(self, msg_type, data):
            return 1 if state["deliver"] else 0

    dm = DialogManager()
    dm.ws_manager = FlakyMgr()
    holder = {}

    def run():
        holder["result"] = dm.show("alert", title="t", message="m", timeout=3.0)

    t = threading.Thread(target=run)
    t.start()

    # Wait until show() has queued the dialog (pending registered).
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with dm._lock:
            if dm._pending:
                dialog_id = next(iter(dm._pending))
                break
        time.sleep(0.005)
    else:
        pytest.fail("show() never registered a pending dialog")

    time.sleep(2.0)  # let the dialog sit queued (client absent) — flush at ~2s
    state["deliver"] = True
    dm.flush_pending()  # shown at ~2s; response window = [2s, 5s]
    time.sleep(2.0)  # handle at ~4s — past the 3s creation deadline, within
    # the post-flush window (proves flush-based timing).

    ok = dm.handle_response(dialog_id, "ok")
    t.join(timeout=6.0)

    assert not t.is_alive(), "show() should have returned by now"
    assert ok is True
    assert holder["result"] == {"action": "ok"}


# ── DELETE /api/files (mobile file delete) ─────────────────────────────


def _dispatch_delete(path, body_bytes, upload_dir):
    return dispatch(
        "DELETE",
        path,
        {},
        body_bytes,
        cfg=object(),
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=upload_dir,
    )


def test_dispatch_delete_file_removes_uploaded_file(tmp_path):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"jpg-bytes")
    status, _ct, body_b = _dispatch_delete(
        "/api/files",
        _body({"name": "photo.jpg"}),
        str(tmp_path),
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert not f.exists(), "the uploaded file should be deleted"


def test_dispatch_delete_file_rejects_traversal(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    evil = tmp_path / "evil.txt"  # sibling of uploads — outside the upload dir
    evil.write_bytes(b"evil")
    status, _ct, body_b = _dispatch_delete(
        "/api/files",
        _body({"name": "../evil.txt"}),
        str(uploads),
    )
    # basename strips the traversal, so the request can only ever target an
    # in-dir name (missing here -> 404) — never the outside file.  Either a
    # 400 rejection or a 404 not-found is safe; the outside file must survive.
    assert status in (400, 404)
    assert evil.exists(), "traversal must never delete outside the upload dir"


def test_dispatch_delete_file_rejects_directory(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    status, _ct, body_b = _dispatch_delete(
        "/api/files",
        _body({"name": "subdir"}),
        str(tmp_path),
    )
    assert status == 400
    assert d.is_dir(), "a directory must not be deleted"


def test_dispatch_delete_file_preserves_leading_trailing_spaces(tmp_path):
    """#5: DELETE /api/files must match GET's raw filename semantics.  A file
    whose name has leading/trailing spaces (legal on macOS/Linux) is deleted
    by its exact name — stripping the name would fail to match or hit a
    different file."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    for name in (" lead.jpg", "trail .jpg"):
        f = uploads / name
        f.write_bytes(b"jpg-bytes")
        status, _ct, body_b = _dispatch_delete(
            "/api/files",
            _body({"name": name}),
            str(uploads),
        )
        assert status == 200
        assert json.loads(body_b)["ok"] is True
        assert not f.exists(), f"{name!r} should be deleted by its exact name"


def test_chat_stale_transfer_sweep_defers_fire_outside_lock():
    """#8: the stall sweeper collects the done callbacks and fires none of them
    itself.  The heartbeat calls it while holding the chat lock, so a slow
    WS/UI callback running inline here would freeze every other chat thread."""
    import tempfile
    from pathlib import Path

    from internal.sync.nearby_chat import ChatManager

    mgr = ChatManager("x", "X")
    tmp = tempfile.TemporaryDirectory()
    try:
        src = Path(tmp.name) / "x.bin"
        src.write_bytes(b"payload")
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a",
            "A1",
            lambda data: True,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        done_events = []
        mgr.set_on_file_done(
            lambda s, tid, ok, path, st: done_events.append((tid, ok, st)),
        )
        # A send the peer never answers, aged past the stall window.
        tid = mgr.send_file(sid, str(src), lambda data: True)
        assert tid
        mgr._sends[tid]["last_progress_mono"] = time.monotonic() - (
            ChatManager.TRANSFER_STALL_TIMEOUT + 10
        )
        fired: list = []
        with mgr._lock:
            done = mgr._expire_stale_transfers(fired)
        assert any(d[1] == tid for d in done), "the stalled send was not collected"
        assert fired, "the UI notice was not queued"
        assert done_events == [], "the sweep must not fire callbacks inline"
    finally:
        mgr.shutdown()
        tmp.cleanup()


def test_history_api_limit_total_returns_authoritative_list(tmp_path):
    """#2: the frontend full-calibration depends on `limit=total` returning
    every remaining history item — the authoritative list that replaces ghost
    rows when the client has loaded past the true count."""
    from internal.web.api.history import get_history

    db = _make_db(tmp_path)
    for i in range(5):
        db.add(
            ClipboardContent(types={ContentType.TEXT: ("t%d" % i).encode()}, timestamp=2000.0 + i),  # noqa: UP031
            source_app=None,
        )

    class Cfg:
        device_id = "dev1"
        device_name = "Dev"
        web_history_limit = 30
        peers = {}

    items = db.get_all()
    total = len(items)
    data, status = get_history(db, Cfg(), str(total), None)
    assert status == 200
    assert data["total"] == total
    assert len(data["items"]) == total
# merged from test_round7_web.py


from internal.web.server import (
    _check_declared_length,
    _MultipartError,
    _parse_content_disposition,
    _parse_multipart,
)

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
    body = _mp_body(
        [
            ("file", "photo.jpg", b"JPGDATA"),
            ("device_id", None, b"peer-42"),
            ("purpose", None, b"chat"),
        ]
    )
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
        "GET",
        "/api/logs",
        query_params,
        b"",
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
    _write_log(tmp_path, 300)

    status, _ct_, body_b = _get_logs({"lines": ["5"]})
    assert status == 200
    logs = json.loads(body_b)["logs"]
    assert len(logs) == 5
    assert logs[-1].endswith("log line 299"), "must be the LAST lines of the file"
    assert logs[0].endswith("log line 295")


def test_api_logs_redacts_web_token(monkeypatch, tmp_path):
    from internal.config import config as config_module

    monkeypatch.setattr(config_module, "_log_dir", lambda: tmp_path)
    (tmp_path / "clipsync.log").write_text(
        "INFO request used token sekret-token ok\n", encoding="utf-8"
    )

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

    monkeypatch.setattr(favorites_api, "_FAV_DB_PATH", str(tmp_path / "favorites.db"))
    # Keep a legacy JSON on this machine from migrating into the test DB.
    monkeypatch.setattr(favorites_api, "_get_json_path", lambda: str(tmp_path / "no_legacy.json"))
    return favorites_api


def _seed_two_favorites(favorites_api):
    favorites_api.add_favorite(
        json.dumps({"title": "Alpha note", "content": "alpha-content", "group": "Work"}).encode()
    )
    favorites_api.add_favorite(
        json.dumps({"title": "", "content": "ungrouped-content", "group": ""}).encode()
    )


def test_export_favorites_markdown_groups_and_content(fav_db, tmp_path):
    api = fav_db
    _seed_two_favorites(api)
    body = json.dumps({"format": "markdown"}).encode("utf-8")
    data, status = api.export_favorites(body, dest_dir=str(tmp_path))
    assert status == 200 and data["ok"] is True
    assert data["count"] == 2
    assert data["filename"].endswith(".md")
    text = open(data["filepath"], encoding="utf-8").read()  # noqa: SIM115
    assert "# ClipSync Favorites" in text
    assert "## Work" in text
    assert "**Alpha note**" in text
    assert "alpha-content" in text
    assert "## Ungrouped" in text
    assert "ungrouped-content" in text
    # Stored order preserved (position ASC): Alpha first.
    assert text.index("**Alpha note**") < text.index("ungrouped-content")


def test_export_favorites_fence_grows_past_backticks(fav_db, tmp_path):
    api = fav_db
    api.add_favorite(
        json.dumps({"title": "code", "content": "```python\nprint(1)\n```", "group": ""}).encode()
    )
    body = json.dumps({"format": "markdown"}).encode("utf-8")
    data, _status = api.export_favorites(body, dest_dir=str(tmp_path))
    text = open(data["filepath"], encoding="utf-8").read()  # noqa: SIM115
    # The fence around the content must be LONGER than any backtick run in
    # the content itself so the block cannot be broken open.
    assert "\n````\n```python\nprint(1)\n```\n````\n" in text


def test_export_favorites_invalid_format_400(fav_db, tmp_path):
    data, status = fav_db.export_favorites(
        json.dumps({"format": "pdf"}).encode("utf-8"), dest_dir=str(tmp_path)
    )
    assert status == 400
    assert data["ok"] is False


# ── Localized subprocess output must not crash the reader thread (GBK) ──


def test_decode_console_output_handles_both_codepages():
    """The same message must come out right whether netsh spoke UTF-8 or GBK."""
    from internal.platform import decode_console_output

    msg = "请求的操作需要提升(作为管理员运行)。"
    # The bug that started this: netsh spoke UTF-8, we read GBK.
    assert decode_console_output(msg.encode("utf-8")) == msg
    # The reverse case only has a right answer where the platform owns a
    # codepage that can represent these bytes (Chinese Windows).  Anywhere
    # else no decoder could recover them, so all we require is no raise —
    # this helper runs on a failure path and must never add a second failure.
    gbk = msg.encode("gbk")
    try:
        recoverable = gbk.decode("oem") == msg or gbk.decode("mbcs") == msg
    except (UnicodeDecodeError, LookupError):
        recoverable = False
    if recoverable:
        assert decode_console_output(gbk) == msg
    else:
        assert decode_console_output(gbk)
    # Degenerate inputs must never raise — this runs on a failure path.
    assert decode_console_output(b"") == ""
    assert decode_console_output(None) == ""
    assert decode_console_output("already text") == "already text"
    assert decode_console_output(b"\xff\xfe\x00garbage")


# ── Stage 4: web backend hardening ─────────────────────────────────────


class TestRequestPathAliasesRoute:
    """``//api/push`` must reach the same handler as ``/api/push``.

    ``posixpath.normpath`` preserves exactly two leading slashes by design
    (POSIX reserves ``//foo``), so the old canonicalisation left the alias
    untouched and it fell through to a 404 — while every browser, proxy and
    naive ``base + "/" + path`` join produces exactly that form.
    """

    def test_double_leading_slash_collapses(self):
        from internal.web.server import _canonical_request_path as canon

        assert canon("//api/push") == "/api/push"
        assert canon("///api/push") == "/api/push"
        assert canon("//") == "/"


class TestBadFieldTypesAre400NotCrash:
    """``req.get("peer_id", "").strip()`` raised AttributeError on a non-string.

    The outer safety net turned that into a 500 + traceback, which reads like
    a server fault when it is really bad client input.
    """
    def test_numeric_peer_id_answers_400(self):
        from internal.web.routes import dispatch

        status, _ctype, raw = dispatch(
            "POST",
            "/api/device/forget",
            {},
            _body({"peer_id": 123}),
            cfg=None,
            history=None,
            sync_mgr=None,
            get_connected_ids=lambda: [],
            on_nav_url=None,
            on_forward_file=None,
            upload_dir="",
            on_device_action=lambda *a, **k: True,
        )
        assert status == 400
        assert json.loads(raw)["error"] == "peer_id required"


class TestWebSocketCloseActuallyCloses:
    """``close()`` returned early whenever ``_closed`` was already True — and
    the normal disconnect path sets it before close() is ever called, so the
    socket was never released."""

    def test_close_releases_socket_even_when_already_marked_closed(self):
        a, b = socket.socketpair()
        try:
            client = WebSocketClient(a, ("127.0.0.1", 0))
            client._closed = True  # what recv_frame does on EOF
            client.close()
            assert client._sock_closed is True
            with pytest.raises(OSError):
                a.send(b"x")
        finally:
            for s in (a, b):
                with contextlib.suppress(OSError):
                    s.close()

    def test_close_sends_a_close_frame_while_peer_is_live(self):
        a, b = socket.socketpair()
        try:
            client = WebSocketClient(a, ("127.0.0.1", 0))
            client.close()
            opcode, _payload = _read_frame(b)
            assert opcode == 0x8  # CLOSE
            assert client.closed is True
        finally:
            for s in (a, b):
                with contextlib.suppress(OSError):
                    s.close()


class TestKeepaliveDropsOnFailedPing:
    """``send_ping()`` swallows OSError and returns False rather than raising,
    so the old bare ``try/except`` around it never fired."""

    def test_failed_ping_is_collected_immediately(self):
        a, b = socket.socketpair()
        mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None, get_connected_ids=lambda: [])
        try:
            client = WebSocketClient(a, ("127.0.0.1", 0))
            # Fresh client: it would survive the 90s silence window.
            client.last_recv = time.monotonic()
            a.close()  # sending now fails -> send_ping() returns False
            with mgr._lock:
                mgr._clients.append(client)
            stale = mgr._ping_and_collect_stale()
            assert client in stale
        finally:
            mgr.shutdown()
            for s in (a, b):
                with contextlib.suppress(OSError):
                    s.close()


# ── Starting the companion ─────────────────────────────────────────────


def test_starting_the_companion_resolves_no_names(monkeypatch):
    """Binding the socket must not make the OS look this machine up.

    ``ThreadingHTTPServer`` inherits ``HTTPServer.server_bind()``, which fills
    ``server_name`` with ``socket.getfqdn(bound_address)`` -- and ``getfqdn``
    turns the wildcard into ``gethostbyaddr(gethostname())``, a reverse lookup
    of this machine's own name.  That call is on the thread the user waits on
    while the companion starts, and where the name is in no zone (a macOS CI
    runner, a laptop that has just joined a guest network, anything behind a VPN
    that took the name server away) it does not fail fast: the resolver's own
    timeout is tens of seconds, and the app sits there for all of it.  macOS is
    where it is reliably fatal -- Linux answers its own name from /etc/hosts and
    Windows from the local DNS client, while macOS delegates to mDNSResponder,
    which waits out its full timeout.

    The fake resolver raises instead of sleeping, so this measures the call and
    not the clock: a bind that does the lookup fails here at once, and one that
    does not cannot be slow.  ``_get_lan_ip`` is stubbed for the same reason --
    it reaches the resolver through ``discovery.get_all_local_addresses``, which
    has its own bound and its own case in test_transport_recovery.py.
    """

    def _no_reverse_lookup(*args, **kwargs):
        raise AssertionError("the companion resolved a name while binding its socket")

    monkeypatch.setattr(socket, "getfqdn", _no_reverse_lookup)
    monkeypatch.setattr(web_server.WebServer, "_open_firewall", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(web_server.WebServer, "_get_lan_ip", staticmethod(lambda: "127.0.0.1"))

    # Port 0 asks the OS for a free port, so this can never collide with a real
    # companion on the developer's machine or with another test.
    cfg = SimpleNamespace(port=0, web_port=0, file_receive_dir=".")
    server = web_server.WebServer(cfg, None, None)
    try:
        assert server.start() is True
    finally:
        server.stop()
