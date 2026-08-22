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
    """#9: loadHistory must not clobber already-loaded pages on a WS
    reconnect — it merges/upserts when more than one page is loaded."""
    js = _read_repo_file("internal/web/static/js/app.js")
    assert "store.history.length > limit" in js
    assert "store.history.unshift(fresh" in js
    # The mounted() direct load was removed (WS 'connected' is the sole load
    # trigger) so startup no longer fetches everything twice.
    assert "this.loadData();" not in js


# ── v1.0.29-regression guards (#1 mobile pruning, #2 ghost cursor, #3 done) ──

def test_mobile_paged_poll_does_not_prune_loaded_pages():
    """#1: a page-1 poll while more pages are loaded must NOT prune them — the
    v1.0.29 paged merge (mergeHistoryPage(items, true)) collapsed pages 31..N
    every 5s.  The paged path is upsert/prepend-only; ghosts self-heal via the
    WS / next load-more / full refresh, and a snapshot `total` trims only the
    exact tail beyond it."""
    html = _read_repo_file("internal/web/static/mobile.html")
    # Paged path calls the merge WITHOUT pruning.
    assert "mergeHistoryPage(items, false);" in html
    # Tail-trim to the authoritative total (deleted ghosts).
    assert "historyItems.splice(total, historyItems.length - total);" in html
    # The not-paged path still prunes (snapshot fully authoritative).
    assert "mergeHistoryPage(items, true);" in html
    # Load-more path stays a pure append merge.
    assert "var seen = {};" in html


def test_history_cursor_calibrated_from_total():
    """#2: when the history API returns `total`, a page-1 merge trims ghost
    rows beyond it and pins the load-more cursor to total — a missed
    history_item_deleted broadcast would otherwise inflate the cursor and make
    Load More skip live entries."""
    app = _read_repo_file("internal/web/static/js/app.js")
    assert "store.history.splice(res.total, store.history.length - res.total);" in app
    assert "store.historyOffset = Math.min(store.history.length, res.total);" in app
    ws = _read_repo_file("internal/web/static/js/ws.js")
    assert "store.history.splice(data.total, store.history.length - data.total);" in ws
    assert "store.historyOffset = Math.min(store.history.length, data.total);" in ws


def test_history_api_returns_total():
    """#1/#2 backend guard: GET /api/history already returns `total` so the
    frontend can trim ghosts precisely.  (Backwards compatible — clients that
    ignore it keep working.)"""
    from internal.web.api.history import get_history
    src = _read_repo_file("internal/web/api/history.py")
    assert '"total": total' in src


def test_routes_quickpaste_done_invokes_registered_handler():
    """#3: POST /api/quickpaste/done invokes the registered host callback
    (main.py's _close_quick_paste) and returns ok.  Token-gating is handled by
    the server's /api/* POST auth gate."""
    from internal.web.routes import set_quickpaste_done_handler
    calls = []
    set_quickpaste_done_handler(lambda: calls.append(1))
    try:
        status, _ct, body_b = _dispatch_post("/api/quickpaste/done", _body({}), None, None)
        assert status == 200
        assert json.loads(body_b)["ok"] is True
        assert calls == [1]
    finally:
        set_quickpaste_done_handler(None)


def test_routes_quickpaste_done_unavailable_without_handler():
    from internal.web.routes import set_quickpaste_done_handler
    set_quickpaste_done_handler(None)
    status, _ct, body_b = _dispatch_post("/api/quickpaste/done", _body({}), None, None)
    assert status == 503
    assert json.loads(body_b)["error"] == "not available"


def test_main_prefers_app_window_and_registers_done_handler():
    """#3: main.py opens Quick Paste as a Chromium --app subprocess (mode=app,
    so the page enables the done-close flow) and falls back to webbrowser for
    the plain-tab degradation.  The done handler is registered so the endpoint
    can kill the process."""
    src = _read_repo_file("src/main.py")
    assert "_launch_quickpaste_app_window(url + \"&mode=app\")" in src
    assert '["--app=" + url' in src or '"--app=" + url' in src
    assert "webbrowser.open_new(url)" in src
    assert "set_quickpaste_done_handler(self._close_quick_paste)" in src
    assert "def _close_quick_paste(self)" in src


def test_quickpaste_page_posts_done_and_has_safety_net():
    """#3: the page POSTs /api/quickpaste/done after a paste (and as a 60s
    safety net), keeps window.close() as a harmless extra attempt, and shows a
    '✓ Pasted' confirmation for the plain-tab fallback."""
    html = _read_repo_file("internal/web/static/quickpaste.html")
    assert "postDone()" in html
    assert "fetch(apiUrl('/api/quickpaste/done')," in html
    assert "setTimeout(postDone, 60000);" in html
    assert "showPastedFallback()" in html
    assert "window.close()" in html


def test_chat_panel_toasts_expired_not_generic():
    """#4 frontend guard: the chat file-accept failure toast distinguishes the
    backend's {error:'expired'} (offer lapsed under the stale-receive reaper
    while its Accept button was still shown) from a generic send failure."""
    js = _read_repo_file("internal/web/static/components/chat-panel.js")
    assert "res.error === 'expired'" in js
    assert "self.t('pairing.state.expired')" in js
