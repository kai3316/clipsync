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


# ── Frontend fix guards (reconnect merge) ──────────────────────────

def _read_repo_file(rel: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def test_history_merge_cursor_recomputed_from_length():
    """#2: after an upsert/prepend merge of a page-1 snapshot, the load-more
    cursor is aligned via the shared store.setHistoryCursor (visible list
    length, not a "+fresh.length" delta) — dedupe may have discarded incoming
    duplicates so a raw delta would overshoot and the next fetch would skip
    entries.  The convention lives in ONE place so it can't drift again."""
    for rel in ("internal/web/static/js/app.js", "internal/web/static/js/ws.js"):
        js = _read_repo_file(rel)
        assert "store.setHistoryCursor(" in js, rel
        assert "store.historyOffset += fresh.length;" not in js, rel
        assert "historyOffset = offset + items.length" not in js, rel
    # The dashboard's Load More handler — the original raw-delta bug site —
    # must also route through the shared helper.
    panel = _read_repo_file("internal/web/static/components/history-panel.js")
    assert "self.store.setHistoryCursor(" in panel
    assert "offset + items.length" not in panel
    # The calibration branches all use the shared helper, and mobile has its
    # own local copy of the same convention.
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "setHistoryCursor: function (total)" in store
    assert "this.historyOffset = Math.min(this.history.length, total);" not in store
    mobile = _read_repo_file("internal/web/static/mobile.html")
    assert "function _setHistCursor(total)" in mobile


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
        # A working channel: since the honest-accept fix, activation only
        # commits when the chat_accept frame actually goes out.
        mgr.accept_invitation(sid, lambda data: True)
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
    # limit=total (guarded against the delete/clear race + throttled), routed
    # through the shared replace helper so null rows are never stored.
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "calibrateHistory: function (total) {" in store
    assert "ClipsyncAPI.getHistory({ limit: total, offset: 0 })" in store
    assert "self.replaceHistory(calItems)" in store
    assert "historyMutationTick !== startTick" in store


def test_history_api_returns_total():
    """#1/#2 backend guard: GET /api/history already returns `total` so the
    frontend can trim ghosts precisely.  (Backwards compatible — clients that
    ignore it keep working.)"""
    src = _read_repo_file("internal/web/api/history.py")
    assert '"total": total' in src


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

def test_chat_panel_toasts_expired_not_generic():
    """#4 frontend guard: the chat file-accept failure toast distinguishes the
    backend's {error:'expired'} (offer lapsed under the stale-receive reaper
    while its Accept button was still shown) from a generic send failure."""
    js = _read_repo_file("internal/web/static/components/chat-panel.js")
    assert "res.error === 'expired'" in js
    assert "self.t('pairing.state.expired')" in js


# ── v1.0.32 adversarial-self-review fixes (#1–#10) ─────────────────────────

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
    lock or write back.  The timeout is the one terminal state that does NOT
    advance the generation: it unwedges the lock and consumes the budget so
    the next poll can retry, while a slow-but-valid response that settles later
    still passes the gen guard and writes back (staleness vs. newer data is the
    mutation tick's job, not the timer's)."""
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
    # Wholesale path delegates to the shared replace helper (which detects a
    # genuinely-new entry OR a same-id row change on every user-visible field
    # and bumps the tick only for real changes).
    assert "store.replaceHistory(incoming)" in ws
    # The paged merge delegates to the shared merge helper.
    assert "store.mergeHistoryFresh(incoming)" in ws
    # Deletes / clears still bump (the delete handler keeps its unconditional
    # bump so an in-flight calibration can't resurrect rows).
    assert "store.historyMutationTick += 1;" in ws
    # The shared replace helper owns change detection (field-differ), filters
    # malformed null rows, and bumps only on real changes: an unchanged snapshot
    # early-returns WITHOUT rebuilding or bumping, a changed one rebuilds and
    # bumps.
    store = _read_repo_file("internal/web/static/js/store.js")
    assert "replaceHistory: function (items)" in store
    assert "_rowDiffer: function (a, b)" in store
    rh_block = store[store.index("replaceHistory: function (items)"):
                     store.index("removeHistoryItems: function (ids)")]
    assert "if (!changed) {\n        return false;\n      }" in rh_block
    assert "this.historyMutationTick += 1;" in rh_block
    assert "if (items[ri] == null) continue;" in rh_block
    # The merge helper's change detection is also in place.
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
    # The cursor is aligned via the local shared-convention helper (offset =
    # visible length pinned to total), not hand-written per site.
    assert "function _setHistCursor(total)" in html
    assert "historyOffset = Math.min(historyItems.length, total);" not in html


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


# ══════════════════════════════════════════════════
# merged from test_round7_web.py
# ══════════════════════════════════════════════════

import re

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


# ══════════════════════════════════════════════════
# merged from test_static_pages.py
# ══════════════════════════════════════════════════

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "internal", "web", "static",
)

_PAGES = ("mobile.html",)


def _interpolate(html: str, locale: str = "zh-CN", token: str = "tok") -> str:
    """Replicate the server's _interpolate_html replacements for the pages
    under test (locale + token are the ones these pages carry)."""
    html = html.replace("__TOKEN__", token)
    html = html.replace("__CLIPSYNC_I18N_LOCALE__", json.dumps(locale))
    return html


def test_static_pages_interpolation_does_not_break_inline_js():
    for page in _PAGES:
        with open(os.path.join(_STATIC, page), encoding="utf-8") as f:
            served = _interpolate(f.read())
        # The value placeholder must not corrupt a property access into
        # `window."zh-CN"` (the "only a title" bug).
        assert 'window."' not in served, f"{page}: locale value leaked into a JS property name"
        # The LHS must survive interpolation so the locale actually lands.
        assert "window.__I18N_LOCALE__ = " in served, (
            f"{page}: locale property assignment is missing after interpolation"
        )


def test_static_pages_never_use_interpolated_placeholder_as_property():
    for page in _PAGES:
        with open(os.path.join(_STATIC, page), encoding="utf-8") as f:
            raw = f.read()
        # The only allowed use of __CLIPSYNC_I18N_LOCALE__ is as a bare VALUE
        # (RHS of an assignment / argument).  Property access is forbidden.
        assert "window.__CLIPSYNC_I18N_LOCALE__" not in raw, (
            f"{page}: interpolated placeholder used as a JS property name"
        )
        # The value placeholder must still appear (so the server injects it).
        assert "__CLIPSYNC_I18N_LOCALE__" in raw


def test_index_page_uses_correct_pattern_too():
    with open(os.path.join(_STATIC, "index.html"), encoding="utf-8") as f:
        raw = f.read()
    assert "window.__I18N_LOCALE__ = __CLIPSYNC_I18N_LOCALE__;" in raw
    assert 'window."' not in _interpolate(raw)
