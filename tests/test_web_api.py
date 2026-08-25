"""Round 10 classic→web parity tests.

Covers the two features ported from the classic desktop UI to the web UI:

  1. Timed sync pause (tray menu parity) — POST /api/sync/pause and
     POST /api/sync/resume:
       - pausing flips sync off through the standard settings live-apply
         path, persists a deadline in cfg.timed_pause_until, and arms the
         auto-resume timer;
       - pausing over an already-manual pause attaches the timer without a
         redundant flip (mirrors the tray menu);
       - resume re-enables sync via the same live-apply callback and clears
         the persisted deadline;
       - a stale auto-resume timer whose deadline was superseded (an explicit
         toggle anywhere zeroes timed_pause_until) is a no-op;
       - input validation (minutes bounds, non-integer, invalid JSON);
       - GET /api/settings exposes timed_pause_until read-only.
  2. Settings search box (classic round-8 parity, frontend-only) plus the
     new i18n keys — wiring guards over routes/api.js/components/locales.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.web.api import settings as settings_api
from internal.web.api import sync_control
from internal.web.routes import dispatch

# ── Helpers ────────────────────────────────────────────────────────────


class _Cfg:
    """Minimal config stand-in for the settings/sync-control handlers."""

    def __init__(self):
        self.device_id = "dev1"
        self.device_name = "Dev"
        self.web_token = ""
        self.sync_enabled = True
        self.timed_pause_until = 0.0
        self.private_key_pem = ""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Keep every test off the real config.json and leak no timers."""
    # update_settings persists through this helper (enc_mgr is None on this
    # path); a no-op keeps the stub _Cfg from overwriting real config data.
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key",
        lambda cfg: None,
    )
    yield
    sync_control._cancel_timer()


def _noop_persist(monkeypatch):
    monkeypatch.setattr(sync_control, "_persist_cfg", lambda c: True)


def _post_pause(cfg, minutes=15, on_settings_change=None):
    return dispatch(
        "POST", "/api/sync/pause",
        {}, json.dumps({"minutes": minutes}).encode("utf-8"),
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None,
        on_settings_change=on_settings_change,
    )


def _post_resume(cfg, on_settings_change=None):
    return dispatch(
        "POST", "/api/sync/resume", {}, b"{}",
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None,
        on_settings_change=on_settings_change,
    )


# ── 1a. Pause endpoint ────────────────────────────────────────────────

def test_pause_disables_sync_and_sets_deadline(monkeypatch):
    cfg = _Cfg()
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_pause(
        cfg, minutes=15,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )
    data = json.loads(body_b)

    assert status == 200
    assert data["ok"] is True and data["minutes"] == 15
    # Sync went off through the standard settings path (live-apply callback).
    assert applied == [{"sync_enabled": False}]
    assert cfg.sync_enabled is False
    # Deadline persisted ≈ now + 15 min.
    assert cfg.timed_pause_until == pytest.approx(time.time() + 15 * 60, abs=5)
    assert data["until"] == pytest.approx(cfg.timed_pause_until, abs=1e-6)


def test_pause_when_already_paused_attaches_timer_without_flip(monkeypatch):
    """A manual pause with no deadline: pause attaches the timed resume but
    must NOT call sync_enabled=False again (mirrors the tray menu)."""
    cfg = _Cfg()
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_pause(
        cfg, minutes=30,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )

    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert applied == []
    assert cfg.timed_pause_until == pytest.approx(time.time() + 30 * 60, abs=5)


def test_pause_replaces_previous_deadline_and_timer(monkeypatch):
    cfg = _Cfg()
    _noop_persist(monkeypatch)

    _post_pause(cfg, minutes=60)
    first = cfg.timed_pause_until

    _post_pause(cfg, minutes=15)
    second = cfg.timed_pause_until

    assert first != second
    assert second == pytest.approx(time.time() + 15 * 60, abs=5)


@pytest.mark.parametrize("minutes", [0, -5, 1441, "abc", None])
def test_pause_rejects_out_of_range_minutes(monkeypatch, minutes):
    cfg = _Cfg()
    _noop_persist(monkeypatch)
    before = cfg.timed_pause_until

    status, _ct, body_b = _post_pause(cfg, minutes=minutes)
    data = json.loads(body_b)

    assert status == 400
    assert data["ok"] is False
    assert cfg.sync_enabled is True, "no flip may happen on invalid input"
    assert cfg.timed_pause_until == before


def test_pause_invalid_json_400():
    cfg = _Cfg()
    status, _ct, body_b = dispatch(
        "POST", "/api/sync/pause", {}, b"{not-json",
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None,
    )
    assert status == 400
    assert json.loads(body_b)["ok"] is False


# ── 1b. Resume endpoint ───────────────────────────────────────────────

def test_resume_reenables_sync_via_live_apply(monkeypatch):
    cfg = _Cfg()
    cfg.sync_enabled = False
    cfg.timed_pause_until = time.time() + 900
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_resume(
        cfg,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )
    data = json.loads(body_b)

    assert status == 200
    assert data["ok"] is True and data["resumed"] is True
    assert applied == [{"sync_enabled": True}]
    assert cfg.timed_pause_until == 0.0


def test_resume_with_sync_already_on_only_clears_deadline(monkeypatch):
    cfg = _Cfg()
    cfg.sync_enabled = True
    cfg.timed_pause_until = time.time() + 900  # leftover field, e.g. restored
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_resume(
        cfg,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )

    assert status == 200
    assert json.loads(body_b)["resumed"] is True
    assert applied == [], "must not force another enable when already on"
    assert cfg.timed_pause_until == 0.0


# ── 1c. Stale-timer guard ─────────────────────────────────────────────

def test_stale_timer_is_noop_after_explicit_toggle(monkeypatch):
    """An explicit toggle anywhere zeroes timed_pause_until; a stale armed
    timer firing later must not resurrect/kill that state."""
    cfg = _Cfg()
    cfg.timed_pause_until = 12345.0   # some other (newer/cleared) state
    cfg.sync_enabled = True           # user re-enabled manually meanwhile
    _noop_persist(monkeypatch)
    calls = []

    sync_control._fire_resume(
        cfg, lambda u, s: calls.append(dict(u)), deadline=999.0)

    assert calls == []


def test_timer_fires_resume_only_for_matching_deadline(monkeypatch):
    cfg = _Cfg()
    cfg.timed_pause_until = time.time() - 1  # expired while we watch
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    calls = []

    sync_control._fire_resume(
        cfg, lambda u, s: calls.append(dict(u)),
        deadline=cfg.timed_pause_until,
    )

    assert calls == [{"sync_enabled": True}]
    assert cfg.timed_pause_until == 0.0


def test_armed_timer_resumes_after_expiry(monkeypatch):
    """End-to-end: arm the daemon timer for a fraction of a second, wait,
    and it resumes sync through the live-apply callback exactly once."""
    cfg = _Cfg()
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    seen = []

    def _fake_resume(body, c, cb=None):
        seen.append(cb)
        if cb is not None:
            cb({"sync_enabled": True}, {})
        return {"ok": True}, 200

    monkeypatch.setattr(sync_control, "resume_sync", _fake_resume)

    deadline = time.time() + 0.05
    cfg.timed_pause_until = deadline
    sync_control._arm_timer(cfg, lambda u, s: None, deadline, 0.05)

    give_up = time.time() + 3
    while not seen and time.time() < give_up:
        time.sleep(0.02)

    assert len(seen) == 1 and seen[0] is not None


def test_superseded_timer_does_not_fire(monkeypatch):
    """A newer pause replaced the armed timer: the OLD firing must be inert
    even if it already left the gate."""
    cfg = _Cfg()
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    seen = []
    monkeypatch.setattr(
        sync_control, "resume_sync",
        lambda body, c, cb=None: seen.append(True) or ({"ok": True}, 200),
    )

    old_deadline = time.time() - 0.01
    sync_control._fire_resume(cfg, lambda u, s: None, deadline=old_deadline)
    # cfg.deadline is 0 (no pause) ≠ old_deadline → nothing fired.
    assert seen == []


# ── 1d. Settings exposure ─────────────────────────────────────────────

def test_get_settings_exposes_timed_pause_until_readonly():
    cfg = _Cfg()
    cfg.timed_pause_until = 1234.5

    data, status = settings_api.get_settings(cfg)
    assert status == 200
    assert data["settings"]["timed_pause_until"] == 1234.5

    # Not mutable through POST /api/settings: only /api/sync/pause|resume may
    # move it (they own the live-apply + timer bookkeeping).
    payload = json.dumps({"timed_pause_until": 999.0}).encode()
    _data, status2 = settings_api.update_settings(payload, cfg, None)
    assert cfg.timed_pause_until == 1234.5
    # The request carried no valid fields → rejected outright.
    assert status2 == 400


# ── 1e. Onboarding wizard freshness bug (user-reported) ──────────────

class _RecordingWsManager:
    def __init__(self):
        self.calls = []

    def broadcast(self, message_type, data=None):
        self.calls.append((message_type, data))


class _FakeDialogMgr:
    def __init__(self):
        self.ws_manager = _RecordingWsManager()


def test_restore_broadcasts_onboarding_when_config_goes_fresh(monkeypatch):
    """Restoring a backup whose config has language_chosen=False flips the
    app back to fresh-install state.  __CLIPSYNC_FRESH__ is baked in at page
    serve time, so a live dashboard would never re-surface the wizard without
    this WS nudge (the reported 'wizard only appears after refresh' bug)."""
    from internal.web import routes as routes_module

    cfg = _Cfg()
    cfg.language_chosen = True

    def _fake_restore(body, c, history):
        # The restored config is fresh: no language chosen yet.
        c.language_chosen = False
        return {"ok": True, "summary": {}}, 200

    monkeypatch.setattr(routes_module, "restore_backup_api", _fake_restore)
    dlg = _FakeDialogMgr()

    status, _ct, body_b = dispatch(
        "POST", "/api/restore", {},
        json.dumps({"backup_path": "whatever.zip"}).encode("utf-8"),
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None, dialog_mgr=dlg,
    )

    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert dlg.ws_manager.calls == [("onboarding_required", None)] or [
        c[0] for c in dlg.ws_manager.calls] == ["onboarding_required"]


def test_restore_no_broadcast_when_config_stays_chosen(monkeypatch):
    """An ordinary restore that keeps language_chosen=True must NOT pop the
    wizard on every connected client."""
    from internal.web import routes as routes_module

    cfg = _Cfg()
    cfg.language_chosen = True

    def _fake_restore(body, c, history):
        return {"ok": True, "summary": {}}, 200

    monkeypatch.setattr(routes_module, "restore_backup_api", _fake_restore)
    dlg = _FakeDialogMgr()

    dispatch(
        "POST", "/api/restore", {},
        json.dumps({"backup_path": "whatever.zip"}).encode("utf-8"),
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None, dialog_mgr=dlg,
    )

    assert dlg.ws_manager.calls == []


def test_web_language_choice_marks_language_chosen():
    """Choosing a language through POST /api/settings counts as a real
    first-run choice: without this, language_chosen stayed False and the
    desktop picker kept re-nagging while every later page load read as
    'fresh'."""
    cfg = _Cfg()
    cfg.language = "en"
    cfg.language_chosen = False

    data, status = settings_api.update_settings(
        json.dumps({"language": "zh-CN"}).encode(), cfg, None)

    assert status == 200
    assert data["updated"]["language"] == "zh-CN"
    assert cfg.language_chosen is True


def test_web_language_choice_idempotent_when_already_chosen():
    cfg = _Cfg()
    cfg.language = "en"
    cfg.language_chosen = True

    settings_api.update_settings(
        json.dumps({"language": "en"}).encode(), cfg, None)

    assert cfg.language_chosen is True


def test_onboarding_wiring_end_to_end():
    routes = _read_repo_file("internal/web/routes.py")
    assert '"onboarding_required"' in routes
    ws_js = _read_repo_file("internal/web/static/js/ws.js")
    assert "case 'onboarding_required':" in ws_js
    assert "store.showOnboarding = true;" in ws_js
    assert "clipsync_onboarded" in ws_js


# ── 2. Frontend wiring guards ─────────────────────────────────────────


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_repo_file(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_routes_registered_and_wired():
    routes = _read_repo_file("internal/web/routes.py")
    assert 'path == "/api/sync/pause"' in routes
    assert 'path == "/api/sync/resume"' in routes
    assert "pause_sync, resume_sync" in routes


def test_api_js_wrappers_exist():
    api_js = _read_repo_file("internal/web/static/js/api.js")
    assert "pauseSync: function (minutes)" in api_js
    assert "'/api/sync/pause'" in api_js
    assert "resumeSync: function ()" in api_js
    assert "'/api/sync/resume'" in api_js


def test_overview_panel_pause_ui_wired():
    panel = _read_repo_file(
        "internal/web/static/components/overview-panel.js")
    # Countdown computed from the exposed setting + local tick.
    assert "timed_pause_until" in panel
    assert "pauseLeftMin" in panel
    assert "pauseSync(15)" in panel
    assert "ClipsyncAPI.resumeSync()" in panel
    # Expired countdown resyncs server truth once.
    assert "getSettings" in panel


def test_settings_search_box_wired():
    panel = _read_repo_file(
        "internal/web/static/components/settings-panel.js")
    assert "searchQuery" in panel
    assert "SETTINGS_SEARCH_KEYS" in panel
    assert "onSearchEnter" in panel
    assert "settings-dialog__search" in panel
    html = _read_repo_file("internal/web/static/index.html")
    assert ".settings-dialog__search" in html
    assert ".settings-dialog__tab--dim" in html
    assert ".overview-pause-btn" in html


def test_new_i18n_keys_present_in_both_locales():
    keys = [
        "overview.pause_for",
        "overview.paused_left",
        "overview.resume_now",
        "overview.pause_15m",
        "overview.pause_30m",
        "overview.pause_1h",
        "overview.paused_toast",
        "overview.resumed",
        "overview.pause_failed",
        "settings.search_placeholder",
        "settings.search_no_matches",
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
# merged from test_sync_web_round.py
# ══════════════════════════════════════════════════

import os
import socket
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.sync.file_transfer import FileTransferManager
from internal.transport.discovery import Discovery
from internal.web.api.devices import get_devices
from internal.web.api.settings import export_data, update_settings
from internal.web.api.transfer import get_transfers
from internal.web.ws import WebSocketClient, WebSocketManager

# ── Helpers ────────────────────────────────────────────────────────────

def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _dispatch(method, path, body_bytes=b"", history=None, **callbacks):
    """POST/GET a JSON route straight through the dispatcher."""
    return dispatch(
        method, path, {}, body_bytes,
        cfg=object(),
        history=history,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        on_nav_url=None,
        on_forward_file=None,
        upload_dir=".",
        **callbacks,
    )


def _make_db(tmp_path, text="hello") -> ClipboardHistoryDB:
    db = ClipboardHistoryDB(
        storage_path=str(tmp_path / "history.db"), max_entries=50,
    )
    db.add(
        ClipboardContent(types={ContentType.TEXT: text.encode()}, timestamp=1000.0),
        source_app=None,
    )
    return db


class _Peer:
    def __init__(self, device_id, name, paired=True):
        self.device_id = device_id
        self.device_name = name
        self.paired = paired
        self.os = ""
        self.notes = ""


class _DevCfg:
    device_id = "self1"
    device_name = "Self"

    def __init__(self, peers=None):
        self.peers = peers or {}


def _read_frame(sock):
    """Read one server->client WS frame: returns (opcode, payload_bytes)."""
    header = sock.recv(2)
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return opcode, payload


# ── 1a. POST /api/transfer/retry ───────────────────────────────────────

def test_retry_route_without_handler_is_503():
    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/retry", _body({"transfer_id": "abc"}),
    )
    assert status == 503
    assert json.loads(body_b)["ok"] is False


def test_retry_route_rejects_invalid_json():
    calls = []
    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/retry", b"{not json",
        on_transfer_action=lambda action, tid: calls.append((action, tid)),
    )
    assert status == 400
    assert calls == []


def test_retry_route_requires_transfer_id():
    calls = []
    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/retry", _body({}),
        on_transfer_action=lambda action, tid: calls.append((action, tid)),
    )
    assert status == 400
    assert json.loads(body_b)["error"] == "transfer_id required"
    assert calls == []


def test_retry_route_invokes_host_retry_action():
    calls = []
    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/retry", _body({"transfer_id": "deadbeef" * 4}),
        on_transfer_action=lambda action, tid: calls.append((action, tid)) or True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert calls == [("retry", "deadbeef" * 4)]


def test_retry_route_only_exists_for_post():
    status, _ct, _body_b = _dispatch(
        "GET", "/api/transfer/retry",
        on_transfer_action=lambda action, tid: True,
    )
    assert status == 404


# ── 1b. POST /api/transfer/cancel-all (new feature) ────────────────────

def test_cancel_all_cancels_every_active_transfer():
    calls = []

    def on_get_transfers():
        active = [
            {"transfer_id": "aaa1", "file_name": "a.bin"},
            {"transfer_id": "bbb2", "file_name": "b.bin"},
            "not-a-dict",  # malformed row must be skipped, not crash
        ]
        return active, []

    def on_transfer_action(action, tid):
        calls.append((action, tid))
        return action == "cancel"

    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/cancel-all",
        on_get_transfers=on_get_transfers,
        on_transfer_action=on_transfer_action,
    )
    assert status == 200
    data = json.loads(body_b)
    assert data["ok"] is True
    assert data["cancelled"] == 2
    assert calls == [("cancel", "aaa1"), ("cancel", "bbb2")]


def test_cancel_all_without_handler_is_503():
    status, _ct, body_b = _dispatch("POST", "/api/transfer/cancel-all", b"")
    assert status == 503
    assert json.loads(body_b)["ok"] is False


def test_cancel_all_survives_a_failing_state_callback():
    def boom():
        raise RuntimeError("host gone")

    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/cancel-all",
        on_get_transfers=boom,
        on_transfer_action=lambda action, tid: True,
    )
    assert status == 200
    assert json.loads(body_b) == {"ok": True, "cancelled": 0}


def test_cancel_all_with_no_active_transfers():
    status, _ct, body_b = _dispatch(
        "POST", "/api/transfer/cancel-all",
        on_get_transfers=lambda: ([], []),
        on_transfer_action=lambda action, tid: True,
    )
    assert status == 200
    assert json.loads(body_b) == {"ok": True, "cancelled": 0}


# ── 2. transfers history carries peer_id ───────────────────────────────

def test_transfer_history_rows_map_peer_id():
    history = [
        {
            "transfer_id": "t-up", "file_name": "doc.pdf", "file_size": 10,
            "direction": "up", "success": False, "cancelled": False,
            "status": "peer_offline", "state": "awaiting_ack",
            "source_path": "C:/docs/doc.pdf", "peer_id": "0123456789abcdef",
            "timestamp": 100.0,
        },
        {
            "transfer_id": "t-old", "file_name": "old.bin", "file_size": 1,
            "direction": "down", "success": True, "cancelled": False,
            "status": "success", "saved_path": "C:/out/old.bin",
            # Legacy row written before peer_id existed -> empty string.
            "timestamp": 90.0,
        },
    ]
    data, status = get_transfers(lambda: ([], history))
    assert status == 200
    up = next(t for t in data["history"] if t["id"] == "t-up")
    assert up["peer_id"] == "0123456789abcdef"
    assert up["status"] == "failed"
    assert up["reason"] == "peer_offline"
    legacy = next(t for t in data["history"] if t["id"] == "t-old")
    assert legacy["peer_id"] == ""


# ── 3. devices reconnecting fields ─────────────────────────────────────

def test_devices_attach_reconnect_state_by_real_id():
    cfg = _DevCfg({"p1": _Peer("p1", "Offline One")})

    data, status = get_devices(
        cfg, lambda: [], None, None, None,
        get_reconnect_states=lambda: {"p1": {"attempts": 2, "max_attempts": 10}},
    )
    assert status == 200
    by_id = {d["device_id"]: d for d in data["devices"]}
    off = by_id["p1"]
    assert off["connected"] is False
    assert off["reconnecting"] is True
    assert off["reconnect_attempt"] == 2
    assert off["reconnect_max"] == 10
    # The local device never carries reconnect bookkeeping.
    assert "reconnecting" not in by_id[cfg.device_id]


def test_devices_attach_reconnect_state_by_hashed_mdns_id():
    hashed = Discovery._hash_device_id("p2")
    cfg = _DevCfg({"p2": _Peer("p2", "Offline Two")})

    data, _status = get_devices(
        cfg, lambda: [], None, None, None,
        get_reconnect_states=lambda: {hashed: {"attempts": 1, "max_attempts": 5}},
    )
    off = next(d for d in data["devices"] if d["device_id"] == "p2")
    assert off["reconnecting"] is True
    assert off["reconnect_attempt"] == 1
    assert off["reconnect_max"] == 5


def test_devices_ignore_garbage_reconnect_states():
    cfg = _DevCfg({
        "p3": _Peer("p3", "Junk State"),
        "p4": _Peer("p4", "Bad Attempts"),
    })

    data, _status = get_devices(
        cfg, lambda: [], None, None, None,
        get_reconnect_states=lambda: {
            "p3": "garbage",                       # not a dict
            "p4": {"attempts": "many", "max": 3},  # non-int attempts
        },
    )
    by_id = {d["device_id"]: d for d in data["devices"]}
    assert "reconnecting" not in by_id["p3"]
    bad = by_id["p4"]
    assert bad["reconnecting"] is True
    assert bad["reconnect_attempt"] == 0
    assert bad["reconnect_max"] == 0


def test_ws_device_snapshot_carries_reconnect_fields(tmp_path):
    a, b = socket.socketpair()

    class SnapCfg:
        device_id = "self1"
        device_name = "Self"
        web_history_limit = 10
        peers = {"p1": _Peer("p1", "Offline One")}

    db = _make_db(tmp_path)
    mgr = WebSocketManager(
        cfg=SnapCfg(), history=db, sync_mgr=None,
        get_connected_ids=lambda: [],
        get_reconnect_states=lambda: {"p1": {"attempts": 3, "max_attempts": 9}},
    )
    try:
        client = WebSocketClient(a, ("127.0.0.1", 0))
        mgr._send_snapshot(client)
        opcode, payload = _read_frame(b)
        assert opcode == 0x1
        msg = json.loads(payload)
        assert msg["type"] == "devices_updated"
        off = next(d for d in msg["data"]["devices"] if d["device_id"] == "p1")
        assert off["reconnecting"] is True
        assert off["reconnect_attempt"] == 3
        assert off["reconnect_max"] == 9
    finally:
        mgr.shutdown()
        a.close()
        b.close()


# ── 4a. markdown export ────────────────────────────────────────────────

@pytest.fixture()
def isolated_downloads(monkeypatch, tmp_path):
    """Point Path.home() at tmp with an existing Downloads dir so exports
    land inside the test sandbox instead of the user's real Downloads."""
    import pathlib
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(
        pathlib.Path, "home", classmethod(lambda cls: tmp_path),
    )
    return downloads


def test_export_markdown_writes_md_file(isolated_downloads, tmp_path):
    db = _make_db(tmp_path, text="hello export")
    data, status = export_data(_body({"format": "markdown"}), object(), db)
    assert status == 200
    assert data["ok"] is True
    assert data["format"] == "markdown"
    assert data["count"] == 1
    assert data["filename"].endswith(".md")
    out_file = isolated_downloads / data["filename"]
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# ClipSync History Export" in content
    assert "hello export" in content


def test_export_markdown_unsupported_format_is_400(tmp_path):
    db = _make_db(tmp_path)
    for bad in ("yaml", 5, None, ["json"]):
        data, status = export_data(_body({"format": bad}), object(), db)
        assert status == 400, bad
        assert "unsupported format" in data["error"], bad


# ── 4b. settings whitelist bounds for history_max_age_days ─────────────

class _SettingsCfg:
    def __init__(self):
        self.device_id = "dev1"
        self.history_max_age_days = 0
        self.private_key_pem = "KEY"


@pytest.fixture()
def sandboxed_persist(monkeypatch, tmp_path):
    """Redirect config persistence into the test sandbox."""
    from internal.web.api import settings as settings_api
    monkeypatch.setattr(
        settings_api, "_config_path", lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(
        settings_api, "save_config", lambda cfg, enc_mgr=None: None,
    )


@pytest.mark.usefixtures("sandboxed_persist")
def test_history_max_age_days_out_of_range_rejected():
    cfg = _SettingsCfg()
    for bad in (-1, 36501, 999999):
        data, status = update_settings(
            _body({"history_max_age_days": bad}), cfg,
        )
        # Rejected -> nothing valid remains -> 400, value untouched.
        assert status == 400, bad
        assert data["ok"] is False
        assert cfg.history_max_age_days == 0


@pytest.mark.usefixtures("sandboxed_persist")
def test_history_max_age_days_boundaries_accepted():
    cfg = _SettingsCfg()
    for good in (0, 1, 36500):
        data, status = update_settings(
            _body({"history_max_age_days": good}), cfg,
        )
        assert status == 200, good
        assert data["updated"]["history_max_age_days"] == good
        assert cfg.history_max_age_days == good


@pytest.mark.usefixtures("sandboxed_persist")
def test_history_max_age_days_string_type_mismatch_rejected():
    cfg = _SettingsCfg()
    data, status = update_settings(
        _body({"history_max_age_days": "30"}), cfg,
    )
    assert status == 400
    assert cfg.history_max_age_days == 0


@pytest.mark.usefixtures("sandboxed_persist")
def test_plain_text_only_bool_roundtrip_and_type_guard():
    cfg = _SettingsCfg()
    # Bool values are accepted and persisted on the cfg object.
    for good in (True, False):
        data, status = update_settings(
            _body({"plain_text_only": good}), cfg,
        )
        assert status == 200, good
        assert data["updated"]["plain_text_only"] is good
        assert cfg.plain_text_only is good
    # A non-bool value against a bool field is a type mismatch -> rejected.
    cfg.plain_text_only = False
    data, status = update_settings(
        _body({"plain_text_only": "yes"}), cfg,
    )
    assert status == 400
    assert cfg.plain_text_only is False


# ── 5. failure-history regressions in FileTransferManager ──────────────

def _outgoing_mgr(tmp_path, source_text=b"data"):
    src = tmp_path / "source.bin"
    src.write_bytes(source_text)
    mgr = FileTransferManager("dev1", output_dir=str(tmp_path / "out"))
    sent = []
    tid = mgr.send_file(
        str(src), lambda data, pid="0123456789abcdef": sent.append(data),
    )
    return mgr, tid


def test_stale_sweeper_records_timeout_in_history(tmp_path):
    mgr, tid = _outgoing_mgr(tmp_path)
    with mgr._lock:
        mgr._transfers[tid]["_last_activity"] = time.time() - 99999
    mgr.cleanup_stale_transfers()
    assert tid not in mgr._transfers
    hist = mgr.get_history()
    assert len(hist) == 1
    entry = hist[0]
    assert entry["transfer_id"] == tid
    assert entry["status"] == "error_timeout"
    assert entry["success"] is False
    assert entry["direction"] == "up"
    assert entry["peer_id"] == "0123456789abcdef"


def test_remote_reject_records_failed_outgoing_row_in_history(tmp_path):
    mgr, tid = _outgoing_mgr(tmp_path)
    fired = []
    mgr.set_on_transfer_complete(
        lambda t, ok, cancelled, status: fired.append(status),
    )
    mgr.handle_message("file_reject", {"transfer_id": tid}, None)
    assert fired == ["rejected"]
    assert tid not in mgr._transfers
    hist = mgr.get_history()
    assert len(hist) == 1
    entry = hist[0]
    assert entry["transfer_id"] == tid
    assert entry["status"] == "rejected"
    assert entry["success"] is False
    assert entry["cancelled"] is False
    assert entry["direction"] == "up"
    assert entry["peer_id"] == "0123456789abcdef"


def test_accept_temp_open_failure_records_error_disk(tmp_path):
    blocked_dir = tmp_path / "blocked-out"
    blocker = FileTransferManager("dev1", output_dir=str(blocked_dir))
    # Replace the (real) output dir with a regular FILE so the incoming
    # transfer's temp-file open fails with an OSError mid-accept.
    blocked_dir.rmdir()
    blocked_dir.write_bytes(b"not a dir")

    with blocker._lock:
        blocker._transfers["incoming1"] = {
            "transfer_id": "incoming1",
            "type": "incoming",
            "kind": "file",
            "peer_id": "fedcba9876543210",
            "file_name": "x.bin",
            "file_size": 4,
            "mime_type": "application/octet-stream",
            "total_chunks": 1,
            "received_chunks": 0,
            "received_bytes": 0,
            "temp_fh": None,
            "state": "pending",
            "start_time": time.time(),
            "_last_activity": time.time(),
            "chunks": {0},
        }
    blocker.accept_transfer("incoming1", None)
    assert "incoming1" not in blocker._transfers
    hist = blocker.get_history()
    assert len(hist) == 1
    entry = hist[0]
    assert entry["status"] == "error_disk"
    assert entry["direction"] == "down"
    assert entry["peer_id"] == "fedcba9876543210"


# ── 6. locale parity guard for the new keys ────────────────────────────

def test_new_locale_keys_present_in_both_languages():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    locales = {}
    for name in ("en.json", "zh-CN.json"):
        with open(os.path.join(root, "internal/web/static/locales", name),
                  encoding="utf-8") as f:
            locales[name] = json.load(f)
    en, zh = locales["en.json"], locales["zh-CN.json"]
    for key in ("transfer.cancel_all", "settings.export_markdown",
                "device.reconnecting", "common.retry"):
        assert key in en and key in zh, key


def test_swjs_and_index_still_carry_v177_fixes():
    """v1.0.77: the service worker pins the app-shell cache version and
    index.html hides pre-Vue markup.  A regression here serves stale cached
    pages forever after an update (SW cache-first), or flashes the raw
    onboarding markup on refresh."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "internal/web/static/sw.js"),
              encoding="utf-8") as f:
        sw = f.read()
    assert "clipsync-shell-v2" in sw
    with open(os.path.join(root, "internal/web/static/index.html"),
              encoding="utf-8") as f:
        index = f.read()
    assert "v-cloak" in index


# ══════════════════════════════════════════════════
# merged from test_round10_mobile.py
# ══════════════════════════════════════════════════

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "internal", "web", "static",
)


def _mobile_path():
    return os.path.join(_STATIC, "mobile.html")


@pytest.fixture(scope="module")
def html():
    with open(_mobile_path(), encoding="utf-8") as f:
        return f.read()


def _scripts(html_text):
    return re.findall(r"<script>(.*?)</script>", html_text, re.S)


# ═════════════════════════════════════════════════════════════════════════
# 1. Transfers — cancel all + retry failed outgoing
# ═════════════════════════════════════════════════════════════════════════


class TestTransfersParity:
    def test_cancel_all_button_wired_to_endpoint(self, html):
        # Button anchor rendered into the Active section header...
        assert 'data-act="cancelall"' in html
        assert "tr-cancelall" in html
        # ...handled separately from the single-transfer actions (it takes no
        # transfer_id and hits its own endpoint).
        assert "apiFetch('/api/transfer/cancel-all'" in html
        assert "act === 'cancelall'" in html
        # Double-tap guard while the request is in flight.
        assert "transfersCancellingAll" in html

    def test_cancel_all_bilingual_strings(self, html):
        assert "trCancelAll: ZH ? '取消全部' : 'Cancel all'," in html
        assert "trCancelAllDone: ZH ? '✓ 已取消 {n} 个传输' : '✓ Cancelled {n} transfers'," in html
        # The count placeholder is actually substituted.
        assert "trCancelAllDone.replace('{n}'" in html

    def test_retry_only_on_failed_outgoing_history_rows(self, html):
        assert "function canRetryTransfer(h)" in html
        # Same eligibility rule as the desktop panel's canRetry: outgoing,
        # not completed, not cancelled — otherwise the host cannot re-send.
        assert "h.direction === 'up'" in html
        assert "h.status !== 'completed'" in html
        assert "h.status === 'cancelled' || h.cancelled" in html
        assert "apiFetch('/api/transfer/retry'" in html
        assert "data-retry-id=" in html

    def test_retry_double_tap_guard_is_per_row(self, html):
        assert "retryBusyId" in html
        assert "retryBusyId === h.id ? ' disabled'" in html

    def test_transfer_actions_surface_stale_token(self, html):
        # New control paths must keep the shared 403 -> re-scan-QR behavior.
        assert "notifyTokenExpired(); renderTransfers(); return;" in html


# ═════════════════════════════════════════════════════════════════════════
# 2. Chat — mute bell / close session / file attachment
# ═════════════════════════════════════════════════════════════════════════


class TestChatParity:
    def test_sessions_response_mute_set_captured(self, html):
        # GET /api/chat/sessions carries {sessions, muted}; the page must
        # read both (it used to drop `muted`).
        assert "(out[1] && out[1].sessions) || []" in html
        assert "(out[1] && out[1].muted) || []" in html
        assert "chatMutedSet" in html

    def test_mute_bell_on_session_rows(self, html):
        assert "data-mute-peer=" in html
        assert "apiFetch('/api/chat/mute'" in html
        # Payload shape matches the backend contract {peer_id, muted}.
        assert "{ peer_id: peerId, muted: newMuted }" in html
        # Bell tap must never open the conversation: the mute branch is
        # checked BEFORE the session-row branch.
        assert html.index("data-mute-peer") < html.index("data-session-id")
        # Bilingual tooltips + both bell states.
        assert "chatMute: ZH ? '静音' : 'Mute'," in html
        assert "chatUnmute: ZH ? '取消静音' : 'Unmute'," in html
        assert "'🔕' : '🔔'" in html

    def test_close_session_with_confirm_and_back(self, html):
        assert "id=\"chatCloseBtn\"" in html
        assert "apiFetch('/api/chat/close'" in html
        # Destructive-ish action gated behind a confirm, and success returns
        # to the session list instead of leaving a dead conversation open.
        assert "window.confirm(T.chatCloseConfirm)" in html
        assert "chatCloseConfirm: ZH ? '关闭并移除这个会话？' : 'Close and remove this conversation?'," in html
        assert "chatBack();" in html

    def test_attachment_uses_chat_purpose_upload_then_send(self, html):
        # Hidden picker + composer button.
        assert 'id="chatFileInput"' in html
        assert "id=\"chatAttachBtn\"" in html
        # Step 1: purpose=chat upload (lands in the server temp dir, skips
        # receive notification/Files record).
        assert "form.append('purpose', 'chat');" in html
        # Step 2: hand the returned absolute temp path to /api/chat/file.
        assert "apiFetch('/api/chat/file'" in html
        assert "file_path: data.path" in html
        # The bare-name legacy flow resolves against the received-files dir,
        # which is WRONG for purpose=chat uploads — require the path field.
        assert "data.ok && data.path" in html

    def test_attachment_size_preflight_and_xhr(self, html):
        # Same 128 MB cap (minus multipart headroom) as the Files tab.
        assert "MAX_UPLOAD_BYTES - 64 * 1024" in html
        # Large uploads must not go through the 8s fetch timeout — plain XHR.
        assert "new XMLHttpRequest();" in html

    def test_attachment_busy_state_survives_poll_rerenders(self, html):
        # The composer template renders the busy state so a poll landing
        # mid-upload doesn't resurrect an enabled 📎 button.
        assert "(chatFileBusy ? ' disabled' : '')" in html
        assert "(chatFileBusy ? '…' : '📎')" in html


# ═════════════════════════════════════════════════════════════════════════
# 3. History — pin / push / favorite / delete / clear all
# ═════════════════════════════════════════════════════════════════════════


class TestHistoryParity:
    def test_pin_toggle_updates_local_order(self, html):
        assert "apiFetch('/api/pin'" in html
        assert "{ entry_id: modalEntryId }" in html
        # Pinned rows carry a visible marker in the list...
        assert "item.pinned ? '📌 '" in html
        # ...and the local list is re-partitioned pinned-first like the
        # server returns it (stable partition, not sort).
        assert "function reorderHistoryPinnedFirst()" in html
        assert "histPinBtn.textContent = item.pinned ? T.histUnpin : T.histPin;" in html

    def test_push_to_computer_clipboard(self, html):
        # paste-rich writes ALL stored formats (incl. image bytes) to the PC
        # clipboard — the phone-side copy button only writes the phone's.
        assert "apiFetch('/api/paste-rich'" in html
        assert "histPushBtn" in html
        assert "histPushOk: ZH ? '✓ 已写入电脑剪贴板' : '✓ Written to the computer clipboard'," in html

    def test_favorite_fetches_full_text_first(self, html):
        # List previews are truncated — favorites must come from the detail
        # payload, same as the desktop context menu.
        assert "getJson('/api/history/item?entry_id='" in html
        assert "apiFetch('/api/favorites'" in html
        assert "{ title: titleLine, content: content, group: '' }" in html

    def test_delete_shrinks_pagination_cursor(self, html):
        assert "apiFetch('/api/delete'" in html
        assert "window.confirm(T.histDeleteConfirm)" in html
        # Cursor must shrink with the array or "load more" skips an entry
        # (same contract as store.historyOffset on the desktop).
        assert "historyOffset = Math.max(0, historyOffset - 1);" in html

    def test_modal_actions_resolve_by_entry_id_not_index(self, html):
        # List indices shift between polls; the modal tracks entry_id.
        assert "modalEntryId = (item.entry_id !== undefined && item.entry_id !== null) ? item.entry_id : null;" in html
        assert "function findHistById(eid)" in html

    def test_favorite_disabled_for_image_only_clips(self, html):
        # Image-only entries have no TEXT payload to favorite.
        assert "isImageOnly" in html
        assert "types.IMAGE || types.IMAGE_EMF) && !types.TEXT" in html

    def test_clear_all_gated_and_resets_cursor(self, html):
        assert "apiFetch('/api/history/clear'" in html
        assert "window.confirm(T.histClearConfirm)" in html
        assert "historyItems.splice(0, historyItems.length);" in html
        assert "historyHasMore = false;" in html
        # The affordance is only visible when there is something to clear.
        assert "histBar.style.display = historyItems.length > 0 ? 'flex' : 'none';" in html

    def test_one_inflight_modal_action_guard(self, html):
        # A double-tap on any modal action must not fire two POSTs.
        assert "function modalAction(fn)" in html
        assert "modalActionBusy" in html


# ═════════════════════════════════════════════════════════════════════════
# 4. Settings — diagnostics card
# ═════════════════════════════════════════════════════════════════════════


class TestDiagnosticsCard:
    def test_scan_and_fix_endpoints_wired(self, html):
        assert "getJson('/api/diagnostics')" in html
        assert "apiFetch('/api/diagnostics/request'" in html
        # Same action mapping as diagnostics-panel.js.
        assert "'local_network'" in html
        assert "'firewall'" in html

    def test_check_labels_cover_server_ids(self, html):
        for check_id in ("server_port", "discovery", "advertising",
                         "web_companion", "network", "firewall",
                         "permissions", "mdns", "clipboard_tool"):
            assert f"{check_id}: T.diag" in html

    def test_summary_states_bilingual(self, html):
        assert "diagAllOk: ZH ? '✓ 全部正常' : '✓ All good'," in html
        assert "diagWarn: ZH ? '⚠ 存在警告' : '⚠ Warnings found'," in html
        assert "diagFail: ZH ? '✕ 发现问题' : '✕ Problems found'," in html
        # Summary class carries the server verdict (ok / warn / fail).
        assert "diag-summary--' + esc(diagState.summary)" in html

    def test_results_survive_poll_rerenders(self, html):
        # Scan state lives outside the HTML and is re-applied after each
        # rebuild (loadSettings renders every poll tick).
        assert "var diagState =" in html
        assert "renderDiag();" in html
        assert "function renderSettingsView()" in html

    def test_firewall_fix_triggers_rescan(self, html):
        assert "setTimeout(runDiagnostics, 2500);" in html


# ═════════════════════════════════════════════════════════════════════════
# 5. i18n completeness + structural regressions
# ═════════════════════════════════════════════════════════════════════════

_NEW_T_KEYS = [
    "histClear", "histClearConfirm", "histCleared", "histClearFail",
    "histPin", "histUnpin", "histPinnedToast", "histUnpinnedToast",
    "histPinFail", "histPush", "histPushOk", "histPushFail",
    "histFav", "histFavOk", "histFavFail",
    "histDel", "histDeleteConfirm", "histDeleted", "histDelFail",
    "trCancelAll", "trCancelAllDone", "trRetry", "trRetryFail",
    "chatMute", "chatUnmute", "chatMuteFail",
    "chatCloseSession", "chatCloseConfirm",
    "chatAttach", "chatFileSentOk", "chatFileSendFail",
    "diagTitle", "diagHint", "diagRun", "diagScanning", "diagScanFail",
    "diagAllOk", "diagWarn", "diagFail",
    "diagFix", "diagFixDone", "diagFixFail",
    "diagServerPort", "diagDiscovery", "diagAdvertising", "diagWeb",
    "diagNetwork", "diagFirewall", "diagPermissions", "diagMdns",
    "diagClipboardTool",
]


class TestInlineI18n:
    @pytest.mark.parametrize("key", _NEW_T_KEYS)
    def test_every_new_string_exists_inline(self, html, key):
        # The mobile page owns its translations — each key must appear in
        # the T dictionary (both languages live in the single entry).
        pattern = rf"^\s*{key}: ZH \? '.+' : '.+',\s*$"
        assert re.search(pattern, html, re.M), f"missing inline T entry: {key}"

    def test_existing_round7_export_parity_untouched(self, html):
        assert 'id="favExportBtn"' in html
        assert "apiFetch('/api/favorites/export'" in html
        assert "{ format: 'markdown' }" in html

    def test_typing_indicator_still_present(self, html):
        assert "chatTypingRow" in html
        assert "chatPeerTypingUntil" in html


# ═════════════════════════════════════════════════════════════════════════
# 6. Inline script syntax (node --check over each <script> block)
# ═════════════════════════════════════════════════════════════════════════


class TestScriptSyntax:
    def test_all_inline_scripts_parse(self, html, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        blocks = _scripts(html)
        assert len(blocks) >= 3, "expected the head helpers + main app script"
        for i, block in enumerate(blocks):
            path = os.path.join(str(tmp_path), f"block_{i}.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(block)
            proc = subprocess.run(
                [node, "--check", path],
                capture_output=True, text=True, timeout=30,
            )
            assert proc.returncode == 0, (
                f"inline <script> block {i} fails node --check:\n{proc.stderr}"
            )
