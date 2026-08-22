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
