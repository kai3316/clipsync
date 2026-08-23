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
    """#1: the desktop popup's close paths are gated on the auto_close query
    flag, not on a touch heuristic or window.opener — it is opened via a
    Chromium --app window / webbrowser.open_new (no opener), and a touch-screen
    laptop reports maxTouchPoints>0 so the old touch gate would leave it open
    forever.  IS_TOUCH (coarse-pointer) now gates KEYBOARD NAVIGATION only,
    deliberately decoupled from AUTO_CLOSE."""
    html = _read_repo_file("internal/web/static/quickpaste.html")
    assert 'onclick="closeWindow()"' not in html, (
        "inline onclick would ReferenceError against the IIFE-local function"
    )
    assert "closeBtn.addEventListener('click'" in html
    assert "var AUTO_CLOSE = params.get('auto_close') === '1'" in html
    # Close behavior (X/Esc/auto-close) is gated on AUTO_CLOSE, not touch.
    assert "if (!AUTO_CLOSE) { return; }" in html
    # IS_TOUCH exists and gates keyboard navigation (listbox focus / 1-9 hint),
    # not the close paths — a manually-opened desktop tab (no auto_close) still
    # gets keyboard paste support.
    assert "var IS_TOUCH = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);" in html
    assert "if (!IS_APP_WINDOW) {" in html
    # No close path may still be gated on window.opener (it only appears in
    # comments explaining the webbrowser.open_new no-opener behavior).
    assert "if (window.opener)" not in html


def test_main_passes_auto_close_to_quickpaste():
    """#1: main.py opens the Quick Paste popup with auto_close=1 so the page
    enables the close affordances; a user-opened tab (no flag) keeps the X
    hidden because a plain tab cannot window.close() itself."""
    src = _read_repo_file("src/main.py")
    assert "&auto_close=1" in src
    assert "webbrowser.open_new(url)" in src


def test_history_merge_cursor_recomputed_from_length():
    """#2: after an upsert/prepend merge of a page-1 snapshot, the load-more
    cursor is recomputed as store.history.length instead of a "+fresh.length"
    delta — dedupe may have discarded incoming duplicates so the delta would
    overshoot and the next fetch would skip entries."""
    for rel in ("internal/web/static/js/app.js", "internal/web/static/js/ws.js"):
        js = _read_repo_file(rel)
        assert "store.historyOffset = store.history.length;" in js, rel
        assert "store.historyOffset += fresh.length;" not in js, rel


def test_mobile_merge_prunes_missing_entries():
    """#3: the mobile poll merge drops local entries the page-1 snapshot no
    longer reports (desktop deletions) so ghost rows don't linger forever,
    while the load-more (offset>0) path stays a pure append merge."""
    html = _read_repo_file("internal/web/static/mobile.html")
    assert "function mergeHistoryPage(items, pruneMissing)" in html
    assert "mergeHistoryPage(items, true)" in html
    assert "!snapshotIds[cur.entry_id]" in html
    # The append/load-more path still uses the seen-set pure merge.
    assert "var seen = {};" in html


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
            "/api/files", _body({"name": name}), str(uploads),
        )
        assert status == 200
        assert json.loads(body_b)["ok"] is True
        assert not f.exists(), f"{name!r} should be deleted by its exact name"


def test_chat_file_sender_reports_success_not_unconfirmed():
    """#4: the sender no longer fires a half-baked 'unconfirmed' terminal
    status (no UI consumes it) — the entry is 'done' and the done callback
    reports 'success' either way."""
    src = _read_repo_file("internal/sync/nearby_chat.py")
    assert 'self._fire("_on_file_done", sid, tid, True, "", "unconfirmed")' not in src
    assert 'self._fire("_on_file_done", sid, tid, True, "", "success")' in src


def test_chat_expire_stale_receives_defers_fire_outside_lock():
    """#8: _expire_stale_receives collects the done callbacks and only fires
    them when the caller requests inline firing — the heartbeat passes
    defer_fire=True so a slow WS/UI callback can't freeze the chat lock."""
    from internal.sync.nearby_chat import ChatManager
    mgr = ChatManager("x", "X")
    try:
        mgr.handle_message(
            "chat_invite",
            {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
            "peer-a", "A1", None,
        )
        sid = mgr.get_sessions()[0]["session_id"]
        mgr.accept_invitation(sid, None)
        done_events = []
        mgr.set_on_file_done(
            lambda s, tid, ok, path, st: done_events.append((tid, ok, st)),
        )
        mgr.handle_message(
            "chat_file_offer",
            {"session_id": sid, "transfer_id": "b" * 32,
             "file_name": "x.bin", "file_size": 100, "mime": ""},
            "peer-a", "A1", None,
        )
        mgr._receives["b" * 32]["entry"].ts -= (
            ChatManager.INVITE_ACCEPT_TIMEOUT + 10
        )
        with mgr._lock:
            done, changed = mgr._expire_stale_receives(defer_fire=True)
        assert done == [(sid, "b" * 32, "declined")]
        assert changed is True
        assert done_events == [], "deferred mode must not fire callbacks inline"
    finally:
        mgr.shutdown()


def test_dashboard_history_reconnect_merge_present():
    """#9/#10: loadHistory must not clobber already-loaded pages on a WS
    reconnect — it merges/upserts when more than one page is loaded, via the
    shared mergeHistoryFresh helper."""
    js = _read_repo_file("internal/web/static/js/app.js")
    assert "store.history.length > limit" in js
    assert "store.mergeHistoryFresh(items)" in js
    # The mounted() direct load was removed (WS 'connected' is the sole load
    # trigger) so startup no longer fetches everything twice.
    assert "this.loadData();" not in js


# ── v1.0.29-regression guards (#1 mobile pruning, #2 ghost cursor, #3 done) ──

def test_mobile_paged_poll_does_not_prune_loaded_pages():
    """#1: a page-1 poll while more pages are loaded must NOT prune them — the
    v1.0.29 paged merge (mergeHistoryPage(items, true)) collapsed pages 31..N
    every 5s.  The paged path is upsert/prepend-only; ghosts heal via the full
    calibration (fetch limit=total and replace) rather than a tail-trim, because
    history is ordered pinned-DESC/timestamp-DESC and a deleted row can sit at
    the top (pinned) or in the middle."""
    html = _read_repo_file("internal/web/static/mobile.html")
    # Paged path calls the merge WITHOUT pruning.
    assert "mergeHistoryPage(items, false);" in html
    # Ghosts (length > total) trigger a full calibration fetch + replace,
    # never a tail-trim splice.
    assert "getJson('/api/history?limit=' + total)" in html
    assert "historyItems.splice(total, historyItems.length - total);" not in html
    # The not-paged path still prunes (snapshot fully authoritative).
    assert "mergeHistoryPage(items, true);" in html
    # Load-more path stays a pure append merge.
    assert "var seen = {};" in html


def test_history_cursor_calibrated_from_total():
    """#2/#8: when the history API returns `total` and the loaded list has
    ghosts (length > total), a page-1 / WS merge does a FULL calibration — fetch
    the authoritative list at limit=total and replace wholesale — instead of a
    tail-trim splice.  A deleted row can be pinned (top) or mid-list, so a
    tail-trim would evict LIVE oldest entries and keep the ghost.  The shared
    logic now lives in store.calibrateHistory() (both app.js and ws.js delegate
    to it) so there is exactly one copy."""
    app = _read_repo_file("internal/web/static/js/app.js")
    assert "store.history.splice(res.total, store.history.length - res.total);" not in app
    assert "store.calibrateHistory(res.total)" in app
    ws = _read_repo_file("internal/web/static/js/ws.js")
    assert "store.history.splice(data.total, store.history.length - data.total);" not in ws
    assert "store.calibrateHistory(data.total)" in ws
    # The shared calibration itself still does the wholesale replace at
    # limit=total (guarded against the delete/clear race + throttled).
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "calibrateHistory: function (total) {" in store
    assert "ClipsyncAPI.getHistory({ limit: total, offset: 0 })" in store
    assert "self.history.splice(0, self.history.length);" in store
    assert "historyMutationTick !== startTick" in store


def test_history_api_returns_total():
    """#1/#2 backend guard: GET /api/history already returns `total` so the
    frontend can trim ghosts precisely.  (Backwards compatible — clients that
    ignore it keep working.)"""
    from internal.web.api.history import get_history
    src = _read_repo_file("internal/web/api/history.py")
    assert '"total": total' in src


def _dispatch_post_with_qp_done(body_bytes, on_quickpaste_done):
    return dispatch(
        "POST", "/api/quickpaste/done", {}, body_bytes,
        cfg=object(),
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        on_quickpaste_done=on_quickpaste_done,
    )


def test_routes_quickpaste_done_invokes_dispatch_handler():
    """#3/#1: POST /api/quickpaste/done invokes the dispatch-provided host
    callback (main.py's _close_quick_paste), passing the popup's instance id
    from the body so the host closes exactly that instance.  The page sends the
    id as a JSON number, but on the wire it arrives as an int-like STRING —
    routes must normalize it to int (the host keys _quickpaste_instances by
    int) or the done POST would never match and the popup would never close.
    Token-gating is handled by the server's /api/* POST auth gate."""
    calls = []
    status, _ct, body_b = _dispatch_post_with_qp_done(
        _body({"instance": "7"}), lambda instance_id: calls.append(instance_id),
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert calls == [7]


def test_routes_quickpaste_done_passes_missing_instance_as_none():
    """#3: a legacy done POST without an `instance` body value still reaches the
    host callback (with None) instead of erroring — the host falls back to the
    most-recent instance."""
    calls = []
    status, _ct, body_b = _dispatch_post_with_qp_done(
        _body({}), lambda instance_id: calls.append(instance_id),
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert calls == [None]


def test_routes_quickpaste_done_unavailable_without_handler():
    status, _ct, body_b = _dispatch_post_with_qp_done(_body({}), None)
    assert status == 503
    assert json.loads(body_b)["error"] == "not available"


def test_routes_quickpaste_done_rejects_invalid_instance():
    """#1: a done POST carrying a non-int / non-int-convertible instance (bool,
    float, garbage string, object) is malformed — the route returns 400 and
    never calls the host, so a bad id can't reach main.py as a confusing value.
    A missing instance is allowed through (the host no-ops on it)."""
    for bad in (
        {"instance": True},
        {"instance": 1.5},
        {"instance": "abc"},
        {"instance": {"x": 1}},
        {"instance": ["7"]},
    ):
        calls = []
        status, _ct, body_b = _dispatch_post_with_qp_done(
            _body(bad), lambda instance_id: calls.append(instance_id),
        )
        assert status == 400, bad
        assert json.loads(body_b)["error"] == "invalid instance", bad
        assert calls == [], bad


# ── v1.0.30 quick-paste refactor (#1 instance ids + user-data-dir + terminal) ──

def test_main_quickpaste_launch_forces_private_profile():
    """#1: the --app launch must pass a private --user-data-dir (per instance)
    so Chromium starts a brand-new instance instead of handing the URL off to an
    already-running browser — handoff makes the spawned Popen exit in ~1s so a
    later terminate() is a no-op, and without a browser running the spawned
    process IS the whole browser (a blind terminate() would kill it)."""
    src = _read_repo_file("src/main.py")
    assert "--user-data-dir=" in src
    assert "tempfile.mkdtemp" in src
    assert "prefix=f\"clipsync_qp_{instance_id}_\"" in src


def test_main_quickpaste_close_uses_taskkill_tree_on_windows():
    """#1: Windows teardown uses `taskkill /PID <pid> /T /F` (whole tree) so the
    popup's private browser instance is killed without touching the user's own
    browser; non-Windows SIGTERMs the process group.  Both live behind a guard
    so the kill is only attempted while the process is actually running."""
    src = _read_repo_file("src/main.py")
    assert '"taskkill", "/PID", str(proc.pid), "/T", "/F"' in src
    assert "os.killpg" in src
    assert "proc.poll() is None" in src


def test_close_quick_paste_noop_when_no_instances():
    """#1: _close_quick_paste is a safe no-op with an empty instance dict and
    for an unknown instance id (plain-tab fallback / legacy id)."""
    from src.main import Application
    app = Application.__new__(Application)
    app._quickpaste_instances = {}
    app._close_quick_paste(None)   # must not raise
    app._close_quick_paste(42)     # unknown id must not raise
    assert app._quickpaste_instances == {}


def test_close_quick_paste_cleans_exited_instance_and_profile(tmp_path):
    """#1: an exited instance (proc.poll() != None) is removed from the dict and
    its private --user-data-dir profile is cleaned up; no kill is attempted."""
    import os
    import shutil

    from src.main import Application
    profile_dir = str(tmp_path / "clipsync_qp_profile")
    os.makedirs(profile_dir, exist_ok=True)

    class FakeProc:
        pid = 999999
        def poll(self):
            return 0  # already exited

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        3: {"proc": FakeProc(), "profile_dir": profile_dir},
    }
    app._close_quick_paste(3)
    assert app._quickpaste_instances == {}
    assert not os.path.exists(profile_dir)


def test_close_quick_paste_none_is_noop_not_fallback(tmp_path):
    """#7: a done POST without an instance id is a strict no-op — it must NOT
    fall back to closing the most recently opened popup.  A blind guess could
    close a NEWER popup that issued its own valid done POST (the page always
    sends its id, so a missing id means an unknown/legacy caller)."""
    from src.main import Application

    class FakeProc:
        pid = 1
        def poll(self):
            return 0  # already exited → no kill attempted

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        5: {"proc": FakeProc(), "profile_dir": ""},
        9: {"proc": FakeProc(), "profile_dir": ""},
    }
    app._close_quick_paste(None)
    # Nothing was closed — both instances remain registered.
    assert 9 in app._quickpaste_instances
    assert 5 in app._quickpaste_instances


def test_close_quick_paste_normalizes_string_instance(tmp_path):
    """#1: the done POST carries the instance id as a JSON number on the wire,
    but a page may send an int-like STRING; _close_quick_paste normalizes it to
    int so dict lookup matches the int keys in _quickpaste_instances — the
    popup actually closes instead of leaking."""
    import os
    import shutil

    from src.main import Application
    profile_dir = str(tmp_path / "clipsync_qp_profile")
    os.makedirs(profile_dir, exist_ok=True)

    class FakeProc:
        pid = 999998
        def poll(self):
            return 0  # already exited → no kill attempted

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        3: {"proc": FakeProc(), "profile_dir": profile_dir},
    }
    app._close_quick_paste("3")
    assert app._quickpaste_instances == {}
    assert not os.path.exists(profile_dir)


def test_close_quick_paste_invalid_instance_is_noop(tmp_path):
    """#1/#7: a non-numeric instance id (garbage string, float, dict, None) is
    a no-op — never raises and never closes any popup."""
    from src.main import Application

    class FakeProc:
        pid = 1
        def poll(self):
            return 0  # already exited → no kill attempted

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        5: {"proc": FakeProc(), "profile_dir": ""},
    }
    # Must not raise and must not close instance 5.
    app._close_quick_paste("abc")
    app._close_quick_paste(1.5)
    app._close_quick_paste(None)
    app._close_quick_paste(True)   # bool is an int subclass — but not a valid id
    assert 5 in app._quickpaste_instances


def test_sweep_quick_paste_removes_dead_instances_and_profiles(tmp_path):
    """#6: _sweep_quick_paste_instances reclaims entries whose process already
    exited (crash / OS window close / Task Manager never POST done) and their
    leftover --user-data-dir profiles, while leaving live popups untouched."""
    import os

    from src.main import Application
    dead_profile = str(tmp_path / "dead_profile")
    live_profile = str(tmp_path / "live_profile")
    os.makedirs(dead_profile, exist_ok=True)
    os.makedirs(live_profile, exist_ok=True)

    class DeadProc:
        pid = 111
        def poll(self):
            return 1  # exited

    class LiveProc:
        pid = 222
        def poll(self):
            return None  # still running

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        1: {"proc": DeadProc(), "profile_dir": dead_profile},
        2: {"proc": LiveProc(), "profile_dir": live_profile},
    }
    app._sweep_quick_paste_instances()
    assert 1 not in app._quickpaste_instances
    assert 2 in app._quickpaste_instances
    assert not os.path.exists(dead_profile)
    assert os.path.exists(live_profile)


def test_cleanup_quick_paste_instances_kills_live_popups_in_parallel(monkeypatch, tmp_path):
    """#6/#8: shutdown cleanup signals every live popup and reclaims profiles
    and the dict — in PARALLEL (Windows fires one taskkill Popen per live popup
    without waiting, then waits a single shared round) instead of serializing N
    blocking 2–5s teardowns that would stretch exit to N× the budget."""
    import subprocess

    from src.main import Application
    profile_a = str(tmp_path / "qp_a")
    profile_b = str(tmp_path / "qp_b")
    os.makedirs(profile_a, exist_ok=True)
    os.makedirs(profile_b, exist_ok=True)

    class FakeProc:
        pid = 333
        def poll(self):
            return None  # live — must be signalled

    spawned = []

    class FakeKiller:
        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, *args, **kwargs):
        spawned.append(cmd)
        return FakeKiller()

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        4: {"proc": FakeProc(), "profile_dir": profile_a},
        8: {"proc": FakeProc(), "profile_dir": profile_b},
    }
    if sys.platform == "win32":
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        app._cleanup_quick_paste_instances()
        assert len(spawned) == 2, "both live popups must be signalled"
        for cmd in spawned:
            assert cmd[0] == "taskkill" and "333" in cmd
    else:
        monkeypatch.setattr("src.main.os.killpg", lambda *a, **k: None)
        monkeypatch.setattr("src.main.os.getpgid", lambda *a, **k: 999)
        app._cleanup_quick_paste_instances()
    # Dict cleared and every profile reclaimed after the kill round.
    assert app._quickpaste_instances == {}
    assert not os.path.exists(profile_a)
    assert not os.path.exists(profile_b)


def test_main_quickpaste_launch_uses_own_process_group_on_posix():
    """#2: non-Windows launches pass start_new_session=True so the popup owns
    its own process group — _close_quick_paste's os.killpg() then signals only
    the popup's tree, never ClipSync itself (a shared group would kill the
    whole app).  Windows keeps taskkill /T /F and does not pass the POSIX-only
    flag."""
    src = _read_repo_file("src/main.py")
    assert "start_new_session" in src
    assert 'sys.platform != "win32"' in src


def test_history_api_limit_total_returns_authoritative_list(tmp_path):
    """#2: the frontend full-calibration depends on `limit=total` returning
    every remaining history item — the authoritative list that replaces ghost
    rows when the client has loaded past the true count."""
    from internal.web.api.history import get_history
    db = _make_db(tmp_path)
    for i in range(5):
        db.add(
            ClipboardContent(types={ContentType.TEXT: ("t%d" % i).encode()},
                             timestamp=2000.0 + i),
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


def test_quickpaste_terminal_state_is_final():
    """#6: the plain-tab "✓ Pasted" fallback marks state.terminal so render()
    and the keydown paste paths short-circuit (no redraw, no further paste, no
    duplicate postDone), keeping the fallback a real terminal state."""
    html = _read_repo_file("internal/web/static/quickpaste.html")
    assert "terminal:     false," in html
    assert "state.terminal = true;" in html
    # render() short-circuits at the very top.
    assert "// Terminal (plain-tab \"✓ Pasted\" fallback): freeze the done state" in html
    # pasteItem() short-circuits before touching the list.
    assert "function pasteItem(index) {\n    if (state.terminal) { return; }" in html
    # keydown keeps Escape live but short-circuits the navigation/paste paths.
    assert "// Terminal (plain-tab \"✓ Pasted\" fallback): no more navigation or paste" in html
    assert html.count("if (state.terminal) { return; }") == 3


def test_main_chat_accept_file_annotated_bool_or_none():
    """#7: _chat_accept_file's annotation is `bool | None` so the None sentinel
    (offer expired) is a documented, first-class return — never collapsed into
    False, which means "offer exists but can't accept right now"."""
    src = _read_repo_file("src/main.py")
    assert (
        "def _chat_accept_file(self, session_id: str, transfer_id: str) -> bool | None:"
        in src
    )
    assert "return self.chat_mgr.accept_file(" in src


def test_dashboard_chat_do_accept_file_handles_none_expired():
    """#7: the desktop chat panel distinguishes the None sentinel (offer gone →
    show "request expired") from False (generic failure) when accepting a file."""
    src = _read_repo_file("internal/ui/dashboard.py")
    assert "result = self._chat_accept_file(session_id, transfer_id)" in src
    assert "if result is None:" in src
    assert 'self._chat_show_hint(T("pairing.state.expired"))' in src

def test_main_prefers_app_window_and_registers_done_handler():
    """#3: main.py opens Quick Paste as a Chromium --app subprocess (mode=app,
    so the page enables the done-close flow), pins a unique instance id into the
    URL, forces a private --user-data-dir so the app owns the whole process
    tree, and falls back to webbrowser for the plain-tab degradation.  The done
    callback is wired through the WebServer constructor (dispatch-param mode,
    like on_send_url) so a re-created server never holds a stale reference."""
    src = _read_repo_file("src/main.py")
    assert "_launch_quickpaste_app_window(" in src
    assert "url + \"&mode=app\", instance_id," in src
    assert '"&instance=' in src
    assert '"--user-data-dir=" + profile_dir' in src
    assert "taskkill" in src
    assert "os.killpg" in src
    assert "webbrowser.open_new(url)" in src
    assert "on_quickpaste_done=self._close_quick_paste" in src
    assert "set_quickpaste_done_handler" not in src
    assert "def _close_quick_paste(self" in src
    assert "self._quickpaste_instances" in src


def test_quickpaste_page_posts_done_and_has_safety_net():
    """#3/#5: the page POSTs /api/quickpaste/done after a paste (and as a 60s
    safety net), echoes its instance id in the body, uses the fetchWithTimeout
    helper (not a bare fetch), keeps window.close() as a harmless extra attempt,
    and shows a '✓ Pasted' confirmation for the plain-tab fallback.  #5: the
    done flag is set ONLY on server confirmation (not before the fetch), so a
    timeout/failure leaves it false and the 60s net retries up to 3 times, then
    shows a 'close this window' hint."""
    html = _read_repo_file("internal/web/static/quickpaste.html")
    assert "postDone()" in html
    assert "fetchWithTimeout(apiUrl('/api/quickpaste/done')," in html
    assert "fetch(apiUrl('/api/quickpaste/done')," not in html
    assert "body: JSON.stringify({ instance: INSTANCE_ID })" in html
    assert "var INSTANCE_ID = params.get('instance') || '';" in html
    # donePosted is set only inside the 2xx confirmation branch — a timeout /
    # network failure keeps it false so the safety net retries.
    assert "if (r.ok) {\n        donePosted = true;" in html
    # The 60s safety net retries up to DONE_NET_ATTEMPTS, then gives up with a
    # persistent "close this window" hint.
    assert "DONE_NET_ATTEMPTS = 3;" in html
    assert "scheduleDoneNet(0);" in html
    assert "Could not auto-close" in html
    assert "showPastedFallback()" in html
    assert "window.close()" in html


def test_chat_panel_toasts_expired_not_generic():
    """#4 frontend guard: the chat file-accept failure toast distinguishes the
    backend's {error:'expired'} (offer lapsed under the stale-receive reaper
    while its Accept button was still shown) from a generic send failure."""
    js = _read_repo_file("internal/web/static/components/chat-panel.js")
    assert "res.error === 'expired'" in js
    assert "self.t('pairing.state.expired')" in js


# ── v1.0.32 adversarial-self-review fixes (#1–#10) ─────────────────────────

def test_routes_quickpaste_done_empty_string_instance_is_missing():
    """#6: a legacy done POST carrying an EMPTY-STRING instance is treated as
    missing (the host no-ops on a missing id) instead of 400 — "" is a legacy
    client's way of omitting the id, and the route comment already promised
    missing ids pass through."""
    calls = []
    status, _ct, body_b = _dispatch_post_with_qp_done(
        _body({"instance": ""}), lambda instance_id: calls.append(instance_id),
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert calls == [None]


def test_calibrate_history_all_terminal_states_consume_budget_and_advance_gen():
    """Core calibration semantics (#2/#6): every terminal state of a
    calibration — success write-back, raced abandon, failure — consumes the
    throttle budget (_lastCalibration) and advances the generation
    (_calibrationGen).  A constantly-mutating history used to raced-abandon
    every calibration without consuming the budget, so every broadcast
    triggered a full limit=total download with no backoff; unified consumption
    caps that at one attempt per window and ghosts heal in the first 30s silent
    window.

    The TIMEOUT is the one exception to gen advancement: it unwedges the lock
    and consumes the budget so the next poll retries, but must NOT advance the
    gen — a slow-but-valid response that settles after the timeout still passes
    the gen guard and writes back (staleness vs. newer data is the mutation
    tick's job, not the timer's).  Advancing the gen on timeout made any fetch
    slower than the timeout permanently unable to heal ghosts."""
    store = _read_repo_file("internal/web/static/js/store.js")
    # Budget consumed in every terminal state (incl. timeout).
    assert store.count("self._lastCalibration = Date.now();") >= 4
    # Generation advances in success / raced-abandon / failure — but NOT the
    # timeout path.
    assert store.count("self._calibrationGen += 1;") >= 3
    # The timeout block clears the lock + stamps budget but leaves gen alone.
    timeout_block = store[store.index("var calibTimer = setTimeout"):
                         store.index("return window.ClipsyncAPI.getHistory")]
    assert "self._calibrationGen += 1;" not in timeout_block
    assert "self._historyCalibrating = false;" in timeout_block
    assert "self._lastCalibration = Date.now();" in timeout_block
    # The raced-abandon path consumes the budget AND advances the gen.
    race_abandon = store[store.index("if (self.historyMutationTick !== startTick)"):
                         store.index("var calItems")]
    assert "self._lastCalibration = Date.now();" in race_abandon
    assert "self._calibrationGen += 1;" in race_abandon


def test_calibrate_history_timeout_unwedges_lock():
    """#2/#9: the _historyCalibrating lock has a timeout fallback so a
    calibration fetch that never settles (old webview without AbortController)
    can't wedge the lock permanently — it clears after CALIBRATION_TIMEOUT_MS,
    generation-guarded so a superseded fetch can't clear a newer calibration's
    lock or write back.  The timeout is itself a terminal state: it advances
    the generation so the late response can no longer pass the gen guard and
    write back the old snapshot, clears the lock, and consumes the budget."""
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "var CALIBRATION_TIMEOUT_MS = 16000;" in store
    assert "clearTimeout(calibTimer);" in store
    assert "self._calibrationGen !== calibGen" in store
    assert "if (self._calibrationGen === calibGen && self._historyCalibrating)" in store
    assert "self._calibrationGen += 1;" in store
    assert "self._lastCalibration = Date.now();" in store


def test_local_delete_clear_bump_mutation_tick():
    """#2/#10: a local delete / batch-delete / clear that removes rows must
    bump historyMutationTick (so an in-flight calibration abandons its
    write-back instead of resurrecting them) — now via the shared helpers in
    store.js, which bump the tick when the list actually changes."""
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "this.historyMutationTick += 1;" in store
    item = _read_repo_file("internal/web/static/components/history-item.js")
    assert "store.removeHistoryItems([eid])" in item
    # The old hand-written splice + tick in the component are gone.
    assert "store.history.splice(idx, 1);" not in item
    panel = _read_repo_file("internal/web/static/components/history-panel.js")
    assert "store.removeHistoryItems(selectedIds)" in panel
    assert "self.store.clearHistory()" in panel
    assert "store.history = newHistory;" not in panel


def test_ws_history_updated_bumps_tick_on_new_data():
    """#1/#10: history_updated broadcasts that actually merge NEW data — a new
    entry_id OR a same-id content/pin/timestamp change in the wholesale path, or
    any fresh/upserted row in the paged merge — bump historyMutationTick so an
    in-flight calibration abandons its write-back instead of overwriting the
    concurrent new item.  A pure display refresh (same entries, unchanged) does
    not bump."""
    ws = _read_repo_file("internal/web/static/js/ws.js")
    assert "var histMutated = false;" in ws
    # Wholesale path indexes the current rows by id and detects both a
    # genuinely-new entry and a same-id row change (pin toggle / peer edit).
    assert "var oldById = {};" in ws
    assert "if (!oldRow) {" in ws
    assert "rowChanged = true;" in ws
    # The paged merge delegates to the shared helper (which bumps the tick).
    assert "store.mergeHistoryFresh(incoming)" in ws
    # Deletes / clears still bump (the delete handler keeps its unconditional
    # bump so an in-flight calibration can't resurrect rows).
    assert "store.historyMutationTick += 1;" in ws
    # The shared merge helper itself detects in-place changes.
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "incChanged = true;" in store


def test_mobile_calibration_throttled_and_failure_pins_min():
    """#4/#7/#9: mobile's full-history calibration is throttled to one per 30s
    (_lastCalibMobile) — inside the window it only pins the cursor — and its
    failure path pins Math.min(length, total) (aligned with the shared store
    version) instead of the ghost-inflated length.  Every terminal state stamps
    the budget (success AND failure), so a persistently-failing calibration
    backs off instead of re-entering a full download on every poll."""
    html = _read_repo_file("internal/web/static/mobile.html")
    assert "var _lastCalibMobile = null;" in html
    assert "(_lastCalibMobile && (calibNow - _lastCalibMobile) < 30000)" in html
    # Success AND failure paths both stamp the budget.
    assert html.count("_lastCalibMobile = Date.now();") >= 2
    assert html.count("historyOffset = Math.min(historyItems.length, total);") >= 2


def test_quickpaste_safety_net_does_not_clobber_pasted_state():
    """#5: the 60s safety net must not overwrite a successful "✓ Pasted" final
    state with the 'could not auto-close' banner — pastedOk guards it: when set
    (a paste succeeded), the exhaustion path only toasts a light hint; otherwise
    it shows the manual-close banner."""
    html = _read_repo_file("internal/web/static/quickpaste.html")
    assert "pastedOk:     false," in html
    assert "state.pastedOk = true;" in html
    assert "if (state.pastedOk) {" in html
    # The banner branch only runs when a paste did NOT succeed.
    assert html.index("if (state.pastedOk) {") < html.index("Could not auto-close")
    # Both paste success paths set pastedOk — the --app window success path AND
    # the plain-tab showPastedFallback — so a successful paste is never
    # clobbered by the banner even when the --app close POST goes unconfirmed.
    assert html.count("state.pastedOk = true;") >= 2


def test_sweep_quick_paste_keeps_entry_when_rmtree_incomplete(monkeypatch, tmp_path):
    """#10: a dead popup whose profile dir can't be fully removed (a leftover
    child still holds a lock) must KEEP its instance entry so the next sweep
    retries — popping it would leak the partial profile forever."""
    from src.main import Application

    profile_dir = str(tmp_path / "locked_profile")
    os.makedirs(profile_dir, exist_ok=True)

    class DeadProc:
        pid = 111
        def poll(self):
            return 1  # exited

    def stuck_rmtree(path, ignore_errors=False):
        raise OSError("file still in use (partial deletion)")

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        1: {"proc": DeadProc(), "profile_dir": profile_dir},
    }
    monkeypatch.setattr("src.main.shutil.rmtree", stuck_rmtree)
    app._sweep_quick_paste_instances()
    # rmtree failed both attempts → the entry is kept for a later retry.
    assert 1 in app._quickpaste_instances
    assert os.path.exists(profile_dir)


def test_sweep_quick_paste_retries_rmtree_once(tmp_path, monkeypatch):
    """#10: the sweep retries a failed profile removal once; a transient lock
    that clears on retry still reclaims the entry."""
    import shutil as _shutil

    from src.main import Application

    profile_dir = str(tmp_path / "flaky_profile")
    os.makedirs(profile_dir, exist_ok=True)

    class DeadProc:
        pid = 112
        def poll(self):
            return 1  # exited

    real_rmtree = _shutil.rmtree
    state = {"n": 0}

    def flaky_rmtree(path, ignore_errors=False):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("transient lock")
        real_rmtree(path, ignore_errors=ignore_errors)

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        1: {"proc": DeadProc(), "profile_dir": profile_dir},
    }
    monkeypatch.setattr("src.main.shutil.rmtree", flaky_rmtree)
    app._sweep_quick_paste_instances()
    assert state["n"] == 2, "rmtree is retried once after a failure"
    assert 1 not in app._quickpaste_instances
    assert not os.path.exists(profile_dir)


# ── v1.0.33 adversarial-self-review fixes (#4/#5/#7/#10) ─────────────────

def test_history_mutation_helpers_consolidate_sites():
    """#10: store.js exposes the shared history-mutation helpers and every
    hand-written splice/unshift/Object.assign history-change site in
    history-item.js, history-panel.js, ws.js and app.js routes through them —
    so every future history change is guaranteed to bump historyMutationTick
    (no new holes can be opened by a future hand-written mutation)."""
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "removeHistoryItems: function (ids)" in store
    assert "clearHistory: function ()" in store
    assert "mergeHistoryFresh: function (items)" in store

    ws = _read_repo_file("internal/web/static/js/ws.js")
    assert "store.removeHistoryItems(data.entry_ids)" in ws
    assert "store.clearHistory()" in ws
    assert "store.mergeHistoryFresh(incoming)" in ws

    app = _read_repo_file("internal/web/static/js/app.js")
    assert "store.mergeHistoryFresh(items)" in app
    # The hand-written unshift merge is gone — the helper owns it now.
    assert "store.history.unshift(fresh" not in app


def test_sweep_quick_paste_never_evicts_live_popup_with_missing_profile(tmp_path):
    """#4: a LIVE popup is never evicted, even when its profile_dir is missing
    or doesn't exist — evicting it would orphan the running process (its done
    POST would then find no entry to tear down).  Only DEAD processes are
    evaluated for profile cleanup."""
    from src.main import Application

    class LiveProc:
        pid = 444
        def poll(self):
            return None  # still running

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        1: {"proc": LiveProc(), "profile_dir": ""},
        2: {"proc": LiveProc(), "profile_dir": str(tmp_path / "nonexistent")},
    }
    app._sweep_quick_paste_instances()
    assert 1 in app._quickpaste_instances
    assert 2 in app._quickpaste_instances


def test_sweep_quick_paste_gives_up_after_retry_cap(tmp_path, monkeypatch):
    """#7: a dead entry whose profile can NEVER be removed (a permanently-
    locked dir) is dropped with a log after MAX_SWEEP_ATTEMPTS sweeps instead
    of being retried forever — the residue is left for system cleanup rather
    than repeated I/O on every sweep."""
    from src.main import Application

    profile_dir = str(tmp_path / "locked_forever")
    os.makedirs(profile_dir, exist_ok=True)

    class DeadProc:
        pid = 999
        def poll(self):
            return 1  # exited

    def stuck_rmtree(path, ignore_errors=False):
        raise OSError("file still in use (permanent lock)")

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        1: {"proc": DeadProc(), "profile_dir": profile_dir},
    }
    monkeypatch.setattr("src.main.shutil.rmtree", stuck_rmtree)
    for _ in range(3):
        app._sweep_quick_paste_instances()
    # After 3 failed sweeps the entry is dropped (bounded leak).
    assert 1 not in app._quickpaste_instances
    assert os.path.exists(profile_dir)


def test_close_quick_paste_keeps_entry_when_profile_removal_fails(monkeypatch, tmp_path):
    """#5: _close_quick_paste must NOT pop the entry before the profile is
    removed — a partial/failed rmtree keeps the entry registered so the sweep
    (with its retry cap) can reclaim the profile later, instead of leaking a
    partial tree forever."""
    import os

    from src.main import Application

    profile_dir = str(tmp_path / "locked_profile")
    os.makedirs(profile_dir, exist_ok=True)

    class DeadProc:
        pid = 998
        def poll(self):
            return 1  # exited → no kill attempted

    def stuck_rmtree(path, ignore_errors=False):
        raise OSError("file still in use")

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        3: {"proc": DeadProc(), "profile_dir": profile_dir},
    }
    monkeypatch.setattr("src.main.shutil.rmtree", stuck_rmtree)
    app._close_quick_paste(3)
    # Entry kept so the sweep can retry the removal.
    assert 3 in app._quickpaste_instances
    assert os.path.exists(profile_dir)


def test_close_quick_paste_pops_only_after_profile_removed(monkeypatch, tmp_path):
    """#5: after a successful profile removal _close_quick_paste pops the
    entry; a subsequent done POST for the same id is a clean no-op."""
    from src.main import Application

    profile_dir = str(tmp_path / "clean_profile")
    os.makedirs(profile_dir, exist_ok=True)

    class DeadProc:
        pid = 997
        def poll(self):
            return 1  # exited → no kill attempted

    app = Application.__new__(Application)
    app._quickpaste_instances = {
        4: {"proc": DeadProc(), "profile_dir": profile_dir},
    }
    app._close_quick_paste(4)
    assert 4 not in app._quickpaste_instances
    assert not os.path.exists(profile_dir)
    # Second done POST for the same id is a no-op (entry already gone).
    app._close_quick_paste(4)
    assert app._quickpaste_instances == {}
