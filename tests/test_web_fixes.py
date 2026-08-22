"""Regression tests for the converged-round web fixes.

Covers the Python-side logic of the 15 findings:
  #2  delete/pin/batch-delete/clear now broadcast over the WebSocket so every
      client's list stays in sync.
  #3  WS keepalive: server pings clients and drops stale ones; broadcast()
      reports actual delivery count.
  #9  DialogManager._broadcast is delivery-based, and a queued dialog's
      response window starts when it is actually shown (flushed), not when it
      was created.

Client-side (store.js / ws.js / api.js / context-menu.js) fixes are covered
by `node --check` + manual review; the WS event handlers' Python counterparts
are exercised here.
"""

import json
import os
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.web.dialog import DialogManager
from internal.web.routes import dispatch
from internal.web.ws import WebSocketClient, WebSocketManager


# ── Helpers ────────────────────────────────────────────────────────────

def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _make_db(tmp_path) -> ClipboardHistoryDB:
    db = ClipboardHistoryDB(
        storage_path=str(tmp_path / "history.db"), max_entries=50,
    )
    db.add(
        ClipboardContent(types={ContentType.TEXT: b"hello"}, timestamp=1000.0),
        source_app=None,
    )
    return db


def _dispatch_post(path, body_bytes, history, dialog_mgr):
    return dispatch(
        "POST", path, {}, body_bytes,
        cfg=object(),
        history=history,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        dialog_mgr=dialog_mgr,
    )


class FakeWSManager:
    """Records the broadcast calls routes.py makes through dialog_mgr."""

    def __init__(self):
        self.calls = []

    def broadcast_history(self):
        self.calls.append(("history_updated",))

    def broadcast_history_deleted(self, entry_ids, total=None):
        self.calls.append(("history_item_deleted", list(entry_ids)))

    def broadcast_history_clear(self):
        self.calls.append(("history_clear",))


class FakeDialogMgr:
    def __init__(self, ws_mgr):
        self.ws_manager = ws_mgr


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


# ── #2: history mutations broadcast ────────────────────────────────────

def test_dispatch_delete_broadcasts_deleted(tmp_path):
    db = _make_db(tmp_path)
    eid = db.get_all()[0]["entry_id"]
    ws = FakeWSManager()
    status, _ct, _body_b = _dispatch_post(
        "/api/delete", _body({"entry_id": eid}), db, FakeDialogMgr(ws),
    )
    assert status == 200
    assert ws.calls == [("history_item_deleted", [eid])]


def test_dispatch_delete_without_dialog_mgr_does_not_crash(tmp_path):
    db = _make_db(tmp_path)
    eid = db.get_all()[0]["entry_id"]
    status, _ct, _body_b = _dispatch_post("/api/delete", _body({"entry_id": eid}), db, None)
    assert status == 200
    assert json.loads(_body_b)["ok"] is True


def test_dispatch_pin_broadcasts_history_updated(tmp_path):
    db = _make_db(tmp_path)
    eid = db.get_all()[0]["entry_id"]
    ws = FakeWSManager()
    status, _ct, _body_b = _dispatch_post(
        "/api/pin", _body({"entry_id": eid}), db, FakeDialogMgr(ws),
    )
    assert status == 200
    assert ws.calls == [("history_updated",)]


def test_dispatch_batch_delete_broadcasts_deleted(tmp_path):
    db = _make_db(tmp_path)
    db.add(ClipboardContent(types={ContentType.TEXT: b"two"}, timestamp=2000.0),
           source_app=None)
    ids = [e["entry_id"] for e in db.get_all()]
    ws = FakeWSManager()
    status, _ct, _body_b = _dispatch_post(
        "/api/batch-delete", _body({"entry_ids": ids}), db, FakeDialogMgr(ws),
    )
    assert status == 200
    assert ws.calls == [("history_item_deleted", ids)]


def test_dispatch_clear_broadcasts_clear(tmp_path):
    db = _make_db(tmp_path)
    ws = FakeWSManager()
    status, _ct, _body_b = _dispatch_post(
        "/api/history/clear", b"", db, FakeDialogMgr(ws),
    )
    assert status == 200
    assert json.loads(_body_b)["ok"] is True
    assert ws.calls == [("history_clear",)]


# ── #3: WS keepalive + delivery counts ─────────────────────────────────

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
    mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None,
                           get_connected_ids=lambda: [])
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
    mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None,
                           get_connected_ids=lambda: [])
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
    mgr = WebSocketManager(cfg=None, history=None, sync_mgr=None,
                           get_connected_ids=lambda: [])
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
            try:
                s.close()
            except OSError:
                pass


# ── #9: dialog delivery-based broadcast + queued-window timing ─────────

def test_dialog_broadcast_uses_delivery_count():
    class ZeroMgr:
        def broadcast(self, msg_type, data):
            return 0

    class OneMgr:
        def broadcast(self, msg_type, data):
            return 1

    dm = DialogManager()
    dm.ws_manager = ZeroMgr()
    assert dm._broadcast("show_dialog", {"a": 1}) is False

    dm.ws_manager = OneMgr()
    assert dm._broadcast("show_dialog", {"a": 1}) is True


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


def test_dialog_flush_pending_marks_shown_and_strips_keys():
    class OneMgr:
        def __init__(self):
            self.calls = []

        def broadcast(self, msg_type, data):
            self.calls.append((msg_type, data))
            return 1

    dm = DialogManager()
    mgr = OneMgr()
    dm.ws_manager = mgr
    data = {"dialog_id": "abc", "dialog_type": "alert", "_queued_at": 123.0}
    with dm._lock:
        dm._queued_dialogs.append(data)
    shown = threading.Event()
    with dm._lock:
        dm._pending["abc"] = {
            "event": threading.Event(), "response": {}, "shown_event": shown,
        }

    dm.flush_pending()

    assert shown.is_set()
    assert mgr.calls == [("show_dialog", {"dialog_id": "abc", "dialog_type": "alert"})]
    with dm._lock:
        assert dm._queued_dialogs == []


def test_dialog_flush_pending_requeues_when_not_delivered():
    class ZeroMgr:
        def broadcast(self, msg_type, data):
            return 0

    dm = DialogManager()
    dm.ws_manager = ZeroMgr()
    data = {"dialog_id": "xyz", "dialog_type": "alert"}
    with dm._lock:
        dm._queued_dialogs.append(data)

    dm.flush_pending()

    with dm._lock:
        assert dm._queued_dialogs == [data]


def test_dialog_queued_flushed_response_window_starts_at_flush():
    """A queued dialog's response window starts when it is flushed (shown),
    not when it was created — a response arriving after the creation deadline
    but within the post-flush window must still be accepted."""
    state = {"deliver": False}

    class FlakyMgr:
        def broadcast(self, msg_type, data):
            return 1 if state["deliver"] else 0

    dm = DialogManager()
    dm.ws_manager = FlakyMgr()
    holder = {}

    def run():
        holder["result"] = dm.show("alert", title="t", message="m", timeout=0.4)

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

    time.sleep(0.15)  # let the dialog sit queued (client absent)
    state["deliver"] = True
    dm.flush_pending()  # delivered at t≈0.15
    time.sleep(0.3)     # t≈0.45 — past the 0.4s creation deadline

    ok = dm.handle_response(dialog_id, "ok")
    t.join(timeout=1.0)

    assert not t.is_alive(), "show() should have returned by now"
    assert ok is True
    assert holder["result"] == {"action": "ok"}


# ── #7: DELETE /api/files (mobile file delete) ───────────────────────

def _dispatch_delete(path, body_bytes, upload_dir):
    return dispatch(
        "DELETE", path, {}, body_bytes,
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
        "/api/files", _body({"name": "photo.jpg"}), str(tmp_path),
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
        "/api/files", _body({"name": "../evil.txt"}), str(uploads),
    )
    # basename strips the traversal, so the request can only ever target an
    # in-dir name (missing here -> 404) — never the outside file.  Either a
    # 400 rejection or a 404 not-found is safe; the outside file must survive.
    assert status in (400, 404)
    assert evil.exists(), "traversal must never delete outside the upload dir"


def test_dispatch_delete_file_rejects_absolute_path(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    keep = tmp_path / "keep.txt"
    keep.write_bytes(b"keep")
    # An absolute path is basename-stripped and confined to the upload dir, so
    # it can never target the outside file — the outside file must survive.
    status, _ct, _body_b = _dispatch_delete(
        "/api/files", _body({"name": str(keep)}), str(uploads),
    )
    assert status in (400, 404)
    assert keep.exists()


def test_dispatch_delete_file_not_found(tmp_path):
    status, _ct, body_b = _dispatch_delete(
        "/api/files", _body({"name": "missing.txt"}), str(tmp_path),
    )
    assert status == 404
    assert json.loads(body_b)["ok"] is False


def test_dispatch_delete_file_requires_name(tmp_path):
    status, _ct, body_b = _dispatch_delete("/api/files", _body({}), str(tmp_path))
    assert status == 400
    assert json.loads(body_b)["error"] == "filename required"


def test_dispatch_delete_file_rejects_directory(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    status, _ct, body_b = _dispatch_delete(
        "/api/files", _body({"name": "subdir"}), str(tmp_path),
    )
    assert status == 400
    assert d.is_dir(), "a directory must not be deleted"


# ── Frontend fix guards (#1 quickpaste close, #9 reconnect merge) ────

def _read_repo_file(rel: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def test_quickpaste_close_no_longer_gated_on_opener():
    """#1: the desktop popup's close paths are gated on touch, not
    window.opener — it is opened via webbrowser.open_new which has no opener.
    The inline onclick that referenced an IIFE-local closeWindow() is gone."""
    html = _read_repo_file("internal/web/static/quickpaste.html")
    assert 'onclick="closeWindow()"' not in html, (
        "inline onclick would ReferenceError against the IIFE-local function"
    )
    assert "closeBtn.addEventListener('click'" in html
    assert "if (IS_TOUCH) { return; }" in html
    # No close path may still be gated on window.opener (it only appears in
    # comments explaining the webbrowser.open_new no-opener behavior).
    assert "if (window.opener)" not in html


def test_dashboard_history_reconnect_merge_present():
    """#9: loadHistory must not clobber already-loaded pages on a WS
    reconnect — it merges/upserts when more than one page is loaded."""
    js = _read_repo_file("internal/web/static/js/app.js")
    assert "store.history.length > limit" in js
    assert "store.history.unshift(fresh" in js
    # The mounted() direct load was removed (WS 'connected' is the sole load
    # trigger) so startup no longer fetches everything twice.
    assert "this.loadData();" not in js
