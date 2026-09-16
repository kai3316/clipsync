"""Timed sync pause (classic tray-menu parity) and the state it reads.

POST /api/sync/pause flips sync off through the standard settings live-apply
path, persists a deadline in cfg.timed_pause_until and arms the auto-resume
timer; POST /api/sync/resume re-enables sync through the same callback and
clears the deadline.  Invalid input must not flip anything, and the deadline
is exposed through GET /api/settings read-only.  A restore that leaves the
config fresh re-surfaces the onboarding wizard, and a language choice made
through POST /api/settings marks the first run done.
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
        settings_api,
        "_persist_preserving_at_rest_private_key",
        lambda cfg: None,
    )
    yield
    sync_control._cancel_timer()


def _noop_persist(monkeypatch):
    monkeypatch.setattr(sync_control, "_persist_cfg", lambda c: True)


def _post_pause(cfg, minutes=15, on_settings_change=None):
    return dispatch(
        "POST",
        "/api/sync/pause",
        {},
        json.dumps({"minutes": minutes}).encode("utf-8"),
        cfg=cfg,
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        upload_dir=".",
        on_nav_url=None,
        on_forward_file=None,
        on_settings_change=on_settings_change,
    )


def _post_resume(cfg, on_settings_change=None):
    return dispatch(
        "POST",
        "/api/sync/resume",
        {},
        b"{}",
        cfg=cfg,
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        upload_dir=".",
        on_nav_url=None,
        on_forward_file=None,
        on_settings_change=on_settings_change,
    )


# ── 1a. Pause endpoint ────────────────────────────────────────────────


def test_pause_disables_sync_and_sets_deadline(monkeypatch):
    cfg = _Cfg()
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_pause(
        cfg,
        minutes=15,
        on_settings_change=lambda updated, special: applied.append(dict(updated)),
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


@pytest.mark.parametrize("minutes", [0, "abc"])
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


# ── 1b. Resume endpoint ───────────────────────────────────────────────


def test_resume_reenables_sync_via_live_apply(monkeypatch):
    cfg = _Cfg()
    cfg.sync_enabled = False
    cfg.timed_pause_until = time.time() + 900
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_resume(
        cfg,
        on_settings_change=lambda updated, special: applied.append(dict(updated)),
    )
    data = json.loads(body_b)

    assert status == 200
    assert data["ok"] is True and data["resumed"] is True
    assert applied == [{"sync_enabled": True}]
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

    # The panel's history routes reach the panels through these three, and the
    # broadcast helpers swallow a missing one by design — so a recorder without
    # them would silently record nothing at all.
    def broadcast_history(self):
        self.calls.append(("history_updated", None))

    def broadcast_history_deleted(self, entry_ids):
        self.calls.append(("history_item_deleted", list(entry_ids)))

    def broadcast_history_clear(self):
        self.calls.append(("history_clear", None))


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
        "POST",
        "/api/restore",
        {},
        json.dumps({"backup_path": "whatever.zip"}).encode("utf-8"),
        cfg=cfg,
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        upload_dir=".",
        on_nav_url=None,
        on_forward_file=None,
        dialog_mgr=dlg,
    )

    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert dlg.ws_manager.calls == [("onboarding_required", None)] or [
        c[0] for c in dlg.ws_manager.calls
    ] == ["onboarding_required"]


def test_web_language_choice_marks_language_chosen():
    """Choosing a language through POST /api/settings counts as a real
    first-run choice: without this, language_chosen stayed False and the
    desktop picker kept re-nagging while every later page load read as
    'fresh'."""
    cfg = _Cfg()
    cfg.language = "en"
    cfg.language_chosen = False

    data, status = settings_api.update_settings(
        json.dumps({"language": "zh-CN"}).encode(), cfg, None
    )

    assert status == 200
    assert data["updated"]["language"] == "zh-CN"
    assert cfg.language_chosen is True


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
        method,
        path,
        {},
        body_bytes,
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
        storage_path=str(tmp_path / "history.db"),
        max_entries=50,
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


def test_retry_route_requires_transfer_id():
    calls = []
    status, _ct, body_b = _dispatch(
        "POST",
        "/api/transfer/retry",
        _body({}),
        on_transfer_action=lambda action, tid: calls.append((action, tid)),
    )
    assert status == 400
    assert json.loads(body_b)["error"] == "transfer_id required"
    assert calls == []


def test_retry_route_invokes_host_retry_action():
    calls = []
    status, _ct, body_b = _dispatch(
        "POST",
        "/api/transfer/retry",
        _body({"transfer_id": "deadbeef" * 4}),
        on_transfer_action=lambda action, tid: calls.append((action, tid)) or True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert calls == [("retry", "deadbeef" * 4)]


# ── 1b. POST /api/transfer/history/delete ──────────────────────────────


def test_history_delete_route_without_handler_is_503():
    status, _ct, body_b = _dispatch(
        "POST",
        "/api/transfer/history/delete",
        _body({"transfer_id": "abc"}),
    )
    assert status == 503
    assert json.loads(body_b)["ok"] is False


def test_history_delete_route_invokes_history_delete_action():
    # Distinct action name from "retry"/"cancel": the host must not confuse a
    # bookkeeping history delete with cancelling a live transfer.
    calls = []
    status, _ct, body_b = _dispatch(
        "POST",
        "/api/transfer/history/delete",
        _body({"transfer_id": "cafe1234"}),
        on_transfer_action=lambda action, tid: calls.append((action, tid)) or True,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert calls == [("history_delete", "cafe1234")]


def test_history_delete_route_reports_a_missing_row_as_not_ok():
    # An id that is no longer in the history must answer ok:false rather than
    # pretending the delete happened, so the UI can surface the failure.
    status, _ct, body_b = _dispatch(
        "POST",
        "/api/transfer/history/delete",
        _body({"transfer_id": "gone"}),
        on_transfer_action=lambda action, tid: False,
    )
    assert status == 200
    assert json.loads(body_b)["ok"] is False


# ── 1c. POST /api/transfer/cancel-all (new feature) ────────────────────


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
        "POST",
        "/api/transfer/cancel-all",
        on_get_transfers=on_get_transfers,
        on_transfer_action=on_transfer_action,
    )
    assert status == 200
    data = json.loads(body_b)
    assert data["ok"] is True
    assert data["cancelled"] == 2
    assert calls == [("cancel", "aaa1"), ("cancel", "bbb2")]


def test_cancel_all_survives_a_failing_state_callback():
    def boom():
        raise RuntimeError("host gone")

    status, _ct, body_b = _dispatch(
        "POST",
        "/api/transfer/cancel-all",
        on_get_transfers=boom,
        on_transfer_action=lambda action, tid: True,
    )
    assert status == 200
    assert json.loads(body_b) == {"ok": True, "cancelled": 0}


# ── 2. transfers history carries peer_id ───────────────────────────────


def test_transfer_history_rows_map_peer_id():
    history = [
        {
            "transfer_id": "t-up",
            "file_name": "doc.pdf",
            "file_size": 10,
            "direction": "up",
            "success": False,
            "cancelled": False,
            "status": "peer_offline",
            "state": "awaiting_ack",
            "source_path": "C:/docs/doc.pdf",
            "peer_id": "0123456789abcdef",
            "timestamp": 100.0,
        },
        {
            "transfer_id": "t-old",
            "file_name": "old.bin",
            "file_size": 1,
            "direction": "down",
            "success": True,
            "cancelled": False,
            "status": "success",
            "saved_path": "C:/out/old.bin",
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
        cfg,
        lambda: [],
        None,
        None,
        None,
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
        cfg,
        lambda: [],
        None,
        None,
        None,
        get_reconnect_states=lambda: {hashed: {"attempts": 1, "max_attempts": 5}},
    )
    off = next(d for d in data["devices"] if d["device_id"] == "p2")
    assert off["reconnecting"] is True
    assert off["reconnect_attempt"] == 1
    assert off["reconnect_max"] == 5


def test_ws_devices_fingerprint_follows_the_pending_pairing(tmp_path):
    """The fingerprint is what the device-page push keys on (see the server's
    broadcast loop), so dropping a pending pairing — a chat invite does that —
    must change it."""
    cfg = _DevCfg(peers={"p1": _Peer("p1", "One", paired=False)})
    db = _make_db(tmp_path)
    pending = [("p1", "12345678", "One", "pending")]
    mgr = WebSocketManager(
        cfg=cfg,
        history=db,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        get_pending_pairings=lambda: list(pending),
    )
    try:
        with_pairing = mgr.devices_fingerprint()
        assert "12345678" in with_pairing
        pending.clear()
        without_pairing = mgr.devices_fingerprint()
        assert without_pairing != with_pairing
        assert "12345678" not in without_pairing
        # A steady page keeps the same fingerprint, so the loop stays quiet.
        assert mgr.devices_fingerprint() == without_pairing
    finally:
        db.close()


def test_ws_device_snapshot_carries_reconnect_fields(tmp_path):
    a, b = socket.socketpair()

    class SnapCfg:
        device_id = "self1"
        device_name = "Self"
        web_history_limit = 10
        peers = {"p1": _Peer("p1", "Offline One")}

    db = _make_db(tmp_path)
    mgr = WebSocketManager(
        cfg=SnapCfg(),
        history=db,
        sync_mgr=None,
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
        pathlib.Path,
        "home",
        classmethod(lambda cls: tmp_path),
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


def test_export_markdown_names_the_route_a_clip_came_in_on(isolated_downloads, tmp_path):
    """The route is the other half of the pair a reader of the file needs: the
    source name says which device, and only the route can say which way."""
    db = ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"), max_entries=50)
    for transport, text in (
        ("lan", "over the cable"),
        ("relay", "over the relay"),
        ("web", "from the panel"),
        # A clip captured here is built with no route at all — the caller that
        # records one is the clipboard monitor, which has nothing to pass.
        (None, "typed here"),
    ):
        content = ClipboardContent(types={ContentType.TEXT: text.encode()}, timestamp=1000.0)
        if transport is not None:
            content.transport = transport
        db.add(content, source_app=None)
    data, status = export_data(_body({"format": "markdown"}), object(), db)
    assert status == 200
    content = (isolated_downloads / data["filename"]).read_text(encoding="utf-8")
    lines = {
        text: next(line for line in content.splitlines() if text in line)
        for text in ("over the cable", "over the relay", "from the panel", "typed here")
    }
    assert "local link" in lines["over the cable"]
    assert "internet relay" in lines["over the relay"]
    assert "web push" in lines["from the panel"]
    # A row whose route was never recorded claims nothing rather than guessing.
    routes = ("local link", "internet relay", "web push")
    assert not any(word in lines["typed here"] for word in routes)


def test_export_markdown_unsupported_format_is_400(tmp_path):
    db = _make_db(tmp_path)
    for bad in ("yaml", 5, None, ["json"]):
        data, status = export_data(_body({"format": bad}), object(), db)
        assert status == 400, bad
        assert "unsupported format" in data["error"], bad


# ── 4b. settings-update stubs (used by the 4c/4d/4e groups) ────────────


class _SettingsCfg:
    def __init__(self):
        self.device_id = "dev1"
        self.history_max_age_days = 0
        self.private_key_pem = "KEY"
        self.relay_username = ""
        self.relay_password = ""
        self.relay_max_message_bytes = 256 * 1024


@pytest.fixture()
def sandboxed_persist(monkeypatch, tmp_path):
    """Redirect config persistence into the test sandbox."""
    from internal.web.api import settings as settings_api

    monkeypatch.setattr(
        settings_api,
        "_config_path",
        lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(
        settings_api,
        "save_config",
        lambda cfg, enc_mgr=None: None,
    )


# ── 4c. netpair_password (layered pairing passphrase) ──────────────────

_STRONG_PW = "Passw0rd!123"


@pytest.mark.usefixtures("sandboxed_persist")
def test_netpair_password_set_then_cleared():
    cfg = _SettingsCfg()
    data, status = update_settings(_body({"netpair_password": _STRONG_PW}), cfg)
    assert status == 200
    assert cfg.netpair_password == _STRONG_PW
    # an empty string clears the passphrase (back to the code-only key)
    data, status = update_settings(_body({"netpair_password": ""}), cfg)
    assert status == 200
    assert cfg.netpair_password == ""


@pytest.mark.usefixtures("sandboxed_persist")
@pytest.mark.parametrize(
    "bad",
    [
        "tooshort1",  # <12
        "Password1abc",  # missing special
    ],
)
def test_netpair_password_strength_rejected(bad):
    cfg = _SettingsCfg()
    data, status = update_settings(_body({"netpair_password": bad}), cfg)
    assert status == 400
    assert data["ok"] is False
    assert not getattr(cfg, "netpair_password", "")


# ── 4d. password special action (unified encryption password) ─────────
# The unified app password carries the SAME strength rules the netpair
# passphrase had (length ≥ 12 + upper/lower/digit/special), enforced
# server-side so the web UI's checklist can't be bypassed via the body.


@pytest.mark.usefixtures("sandboxed_persist")
@pytest.mark.parametrize(
    "bad,tag",
    [
        ("tooshort1", "length"),  # <12
        ("password1!ab", "upper"),  # missing uppercase
        ("PASSWORD1!AB", "lower"),  # missing lowercase
        ("Password!!ab", "digit"),  # missing digit
        ("Password1abc", "special"),  # missing special
    ],
)
def test_password_special_action_strength_rejected(bad, tag):
    cfg = _SettingsCfg()
    data, status = update_settings(_body({"password": bad}), cfg)
    assert status == 400
    assert data["ok"] is False
    assert data["error"] == "password_" + tag


@pytest.mark.usefixtures("sandboxed_persist")
def test_password_special_action_strong_accepted():
    cfg = _SettingsCfg()
    data, status = update_settings(_body({"password": _STRONG_PW}), cfg)
    assert status == 200
    assert data["ok"] is True


@pytest.mark.usefixtures("sandboxed_persist")
def test_password_special_action_non_string_rejected():
    cfg = _SettingsCfg()
    data, status = update_settings(_body({"password": 12345}), cfg)
    assert status == 400
    assert data["error"] == "password_type"


def test_get_settings_exposes_only_netpair_set_flag():
    cfg = _SettingsCfg()
    data, _ = settings_api.get_settings(cfg)
    assert data["settings"]["netpair_password_set"] is False
    assert "netpair_password" not in data["settings"]
    cfg.netpair_password = _STRONG_PW
    data, _ = settings_api.get_settings(cfg)
    assert data["settings"]["netpair_password_set"] is True
    assert "netpair_password" not in data["settings"]


# ── 4e. relay broker credentials (v1.0.86) ──────────────────────────────
# relay_username/relay_password are ordinary mutable string fields: no
# strength rules (a broker may accept any credential), and the password is
# never echoed back — only a set/not-set flag, mirroring translate_api_key.


@pytest.mark.usefixtures("sandboxed_persist")
def test_relay_credentials_saved_and_echoed_username_only():
    cfg = _SettingsCfg()
    data, status = update_settings(
        _body({"relay_username": "clipsync_mqtt", "relay_password": "s3cret!"}),
        cfg,
    )
    assert status == 200
    assert cfg.relay_username == "clipsync_mqtt"
    assert cfg.relay_password == "s3cret!"
    # The sender's own write is echoed back (same as translate_api_key) — the
    # secrecy boundary is GET: settings never expose the password to a reader.
    assert data["updated"]["relay_username"] == "clipsync_mqtt"
    assert data["updated"]["relay_password"] == "s3cret!"


@pytest.mark.usefixtures("sandboxed_persist")
def test_get_settings_exposes_relay_username_and_password_flag():
    cfg = _SettingsCfg()
    data, _ = settings_api.get_settings(cfg)
    assert data["settings"]["relay_username"] == ""
    assert data["settings"]["relay_password_set"] is False
    assert "relay_password" not in data["settings"]
    # The broker's per-message ceiling is a plain number and not a credential —
    # so it is a value, where the password beside it is a flag.  It is readable
    # and settable through this API even though the legacy panel draws no row
    # for it: the desktop settings page is where the control lives, and an API
    # that dropped the key would let a phone-side save reset it.
    assert data["settings"]["relay_max_message_bytes"] == 256 * 1024
    cfg.relay_username = "clipsync_mqtt"
    cfg.relay_password = "s3cret!"
    cfg.relay_max_message_bytes = 64 * 1024
    data, _ = settings_api.get_settings(cfg)
    assert data["settings"]["relay_username"] == "clipsync_mqtt"
    assert data["settings"]["relay_password_set"] is True
    assert data["settings"]["relay_max_message_bytes"] == 64 * 1024
    assert "relay_password" not in data["settings"]


# ── 4d. /api/device/test (test-connection probe route) ─────────────────


def test_device_test_route_probes_and_reports_per_channel():
    called = {}

    def on_device_test(peer_id):
        called["peer_id"] = peer_id
        return {
            "ok": True,
            "results": [
                {"channel": "lan", "ok": True, "latency_ms": 1.2, "error": None},
                {"channel": "relay", "ok": False, "latency_ms": None, "error": "timeout"},
            ],
        }

    status, _ct, body_b = _dispatch(
        "POST",
        "/api/device/test",
        _body({"peer_id": "peer-1"}),
        on_device_test=on_device_test,
    )
    data = json.loads(body_b)
    assert status == 200 and data["ok"] is True
    assert called["peer_id"] == "peer-1"
    assert data["results"][0]["channel"] == "lan"
    assert data["results"][1]["error"] == "timeout"


def test_device_test_route_invalid_json_is_400():
    status, _ct, body_b = _dispatch(
        "POST",
        "/api/device/test",
        b"not-json",
        on_device_test=lambda pid: {"ok": True},
    )
    assert status == 400
    assert json.loads(body_b)["error"] == "invalid json"


# ── 5. failure-history regressions in FileTransferManager ──────────────


def _outgoing_mgr(tmp_path, source_text=b"data"):
    src = tmp_path / "source.bin"
    src.write_bytes(source_text)
    mgr = FileTransferManager("dev1", output_dir=str(tmp_path / "out"))
    sent = []
    tid = mgr.send_file(
        str(src),
        lambda data, pid="0123456789abcdef": sent.append(data),
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


# ══════════════════════════════════════════════════
# merged from test_round10_mobile.py (stage 4 only)
# ══════════════════════════════════════════════════

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═════════════════════════════════════════════════════════════════════════
# 7. Stage 4: handlers must not lie, crash, or mutate shared state
# ═════════════════════════════════════════════════════════════════════════

# Captured at import time: the autouse ``_isolate`` fixture replaces this
# helper with a no-op for every test in the file, so the tests that exercise
# the helper itself need a handle to the real one.
_REAL_PERSIST = settings_api._persist_preserving_at_rest_private_key


class _FakeWriter:
    """Stand-in for the platform clipboard writer."""

    def __init__(self, result=True):
        self.result = result
        self.calls = 0

    def write(self, content):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeHistory:
    def __init__(self, fail=False):
        self.added = []
        self.fail = fail

    def add(self, content):
        if self.fail:
            raise RuntimeError("history is closed")
        self.added.append(content)


class _FakeSyncMgr:
    def __init__(self):
        self.sent = []
        self.suppressed = []
        self.notified = 0

        class _Monitor:
            def __init__(self, outer):
                self.outer = outer

            def suppress_for(self, seconds):
                self.outer.suppressed.append(seconds)

        self._monitor = _Monitor(self)
        self.on_send = self.sent.append

    def _notify_history_change(self):
        self.notified += 1


class TestPushTextTellsTheTruth:
    """``writer.write()`` returns False when the platform clipboard refused
    (Win32 OpenClipboard held by another app, no xclip on Linux).  Discarding
    that and answering {"ok": true} told the user their text was on the
    clipboard when pasting would produce whatever was there before."""

    def _cfg(self):
        class _Cfg:
            device_id = "me"

        return _Cfg()

    def test_failed_clipboard_write_reports_failure(self, monkeypatch):
        from internal.web.api import history as history_api

        writer = _FakeWriter(result=False)
        monkeypatch.setattr("internal.clipboard.platform.create_writer", lambda: writer)
        sync = _FakeSyncMgr()
        hist = _FakeHistory()
        data, status = history_api.push_text(_body({"text": "hello"}), self._cfg(), sync, hist)
        assert status == 500
        assert data["ok"] is False
        assert "clipboard" in data["error"]

    def test_successful_write_still_broadcasts(self, monkeypatch):
        from internal.web.api import history as history_api

        monkeypatch.setattr(
            "internal.clipboard.platform.create_writer", lambda: _FakeWriter(result=True)
        )
        sync = _FakeSyncMgr()
        hist = _FakeHistory()
        data, status = history_api.push_text(_body({"text": "  hello  "}), self._cfg(), sync, hist)
        assert status == 200
        assert data == {"ok": True, "len": 5}
        assert len(sync.sent) == 1
        assert len(hist.added) == 1
        assert sync.suppressed == [2.0]

    def test_the_row_a_push_records_carries_its_route(self, monkeypatch):
        """A push arrives over this machine's own web server, not over a peer
        link, and the row would otherwise carry a source and no route: "Web"
        says nothing about where the browser was."""
        from internal.web.api import history as history_api

        monkeypatch.setattr(
            "internal.clipboard.platform.create_writer", lambda: _FakeWriter(result=True)
        )
        sync = _FakeSyncMgr()
        hist = _FakeHistory()
        data, status = history_api.push_text(_body({"text": "hi"}), self._cfg(), sync, hist)
        assert status == 200
        assert data["ok"] is True
        assert [content.transport for content in hist.added] == ["web"]

    def test_sync_disabled_does_not_crash(self, monkeypatch):
        """``sync_mgr`` is None when sync is off — the suppress_for call was
        guarded but ``sync_mgr.on_send`` right below it was not, so a push with
        sync disabled raised AttributeError after the clipboard already had
        the text."""
        from internal.web.api import history as history_api

        monkeypatch.setattr(
            "internal.clipboard.platform.create_writer", lambda: _FakeWriter(result=True)
        )
        data, status = history_api.push_text(
            _body({"text": "hi"}), self._cfg(), None, _FakeHistory()
        )
        assert status == 200
        assert data["ok"] is True


class TestOneBadTransferRowDoesNotHideThePanel:
    """The mapping ran outside the try/except, so a single malformed row from
    the host's FileTransferManager answered 500 for the whole panel."""

    def test_unmappable_active_row_is_skipped(self):
        active = [
            {"transfer_id": "good", "file_name": "a.bin", "progress": 0.5},
            {"transfer_id": "bad", "progress": "not-a-number"},
        ]
        data, status = get_transfers(lambda: (active, []))
        assert status == 200
        assert [t["id"] for t in data["active"]] == ["good"]

class TestPersistDoesNotMutateSharedConfig:
    """The persist helper blanked ``cfg.private_key_pem`` in place while
    saving.  cfg is the one live object every thread reads, so a peer
    handshake signing during that window got "" instead of the key.

    The autouse ``_isolate`` fixture stubs this very helper out (so other
    tests never touch the real config.json), hence the module-level handle to
    the real implementation captured at import time.
    """

    def test_shared_cfg_is_never_written_to(self, tmp_path, monkeypatch):
        import json as _json

        from internal.web.api import settings as s_api

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(_json.dumps({"private_key_pem": "ENCRYPTED-BLOB"}), encoding="utf-8")
        monkeypatch.setattr(s_api, "_config_path", lambda: cfg_file)

        seen = []
        monkeypatch.setattr(s_api, "save_config", lambda c, enc_mgr=None: seen.append(c))

        class _Cfg:
            private_key_pem = "PLAINTEXT-KEY"
            device_name = "laptop"

        cfg = _Cfg()
        _REAL_PERSIST(cfg)

        # The live object is untouched, at every moment.
        assert cfg.private_key_pem == "PLAINTEXT-KEY"
        # What went to disk is the encrypted blob, from a copy.
        assert len(seen) == 1
        assert seen[0] is not cfg
        assert seen[0].private_key_pem == "ENCRYPTED-BLOB"
        assert seen[0].device_name == "laptop"

    def test_unreadable_config_still_does_not_blank_the_live_key(self, tmp_path, monkeypatch):
        from internal.web.api import settings as s_api

        monkeypatch.setattr(s_api, "_config_path", lambda: tmp_path / "missing.json")
        seen = []
        monkeypatch.setattr(s_api, "save_config", lambda c, enc_mgr=None: seen.append(c))

        class _Cfg:
            private_key_pem = "PLAINTEXT-KEY"

        cfg = _Cfg()
        _REAL_PERSIST(cfg)
        assert cfg.private_key_pem == "PLAINTEXT-KEY"
        # No at-rest blob to preserve (the file is missing), so the save copy
        # keeps the in-memory key rather than blanking identity: the safest
        # reading of a missing/corrupt store is "there is no key to downgrade",
        # not "erase the device's key".
        assert seen[0].private_key_pem == "PLAINTEXT-KEY"


# ── Panel-owned favourites writes tell the host ───────────────────────
#
# The phone's panel writes the shared favourites store through its own routes
# (internal/web/api/favorites.py), not through the native request adapter that
# publishes `favorites.changed`.  The desktop window invalidates its cached
# favourites list on that event, so without a publish from this path a
# favourite added on a phone stayed invisible in the window — the two surfaces
# legacy had as one.  These pin the host callback, and that it stays optional
# and harmless.


def _favorites_db(tmp_path, monkeypatch):
    """Point the favourites API at a database of this test's own."""
    from internal.web.api import favorites as favorites_api

    monkeypatch.setattr(favorites_api, "_FAV_DB_PATH", None)
    monkeypatch.setattr(favorites_api, "_config_dir", lambda: str(tmp_path))


def _favorites_dispatch(method, path, payload, tmp_path, monkeypatch, on_favorites_change):
    _favorites_db(tmp_path, monkeypatch)
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return dispatch(
        method,
        path,
        {},
        body,
        cfg=_Cfg(),
        history=None,
        sync_mgr=None,
        get_connected_ids=lambda: [],
        upload_dir=".",
        on_nav_url=None,
        on_forward_file=None,
        on_favorites_change=on_favorites_change,
    )


def _add_favorite(tmp_path, monkeypatch, title):
    """Store one favourite through the panel's own route and return its id."""
    status, _ct, body = _favorites_dispatch(
        "POST", "/api/favorites", {"title": title, "content": title},
        tmp_path, monkeypatch, None,
    )
    assert status == 200
    return json.loads(body)["favorite"]["id"]


def test_every_panel_favourite_write_tells_the_host(tmp_path, monkeypatch):
    seen = []
    notify = lambda: seen.append("changed")  # noqa: E731

    favorite_id = _add_favorite(tmp_path, monkeypatch, "From the phone")
    status, _ct, _body = _favorites_dispatch(
        "PATCH", "/api/favorites", {"id": favorite_id, "title": "Renamed"},
        tmp_path, monkeypatch, notify,
    )
    assert status == 200 and seen == ["changed"]

    status, _ct, _body = _favorites_dispatch(
        "DELETE", "/api/favorites", {"id": favorite_id}, tmp_path, monkeypatch, notify,
    )
    assert status == 200 and seen == ["changed", "changed"]


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"title": "", "content": ""}, 400),
        ({"id": "no-such-favourite", "title": "x"}, 404),
        ({"updates": [{"id": "x", "position": "0"}]}, 400),
    ],
)
def test_a_rejected_favourite_write_tells_the_host_nothing(
    payload, status, tmp_path, monkeypatch
):
    """A 400/404 changed nothing, so no surface should hear about it."""
    seen = []
    got, _ct, _body = _favorites_dispatch(
        "PATCH", "/api/favorites", payload, tmp_path, monkeypatch,
        lambda: seen.append("changed"),
    )
    assert got == status
    assert seen == []


# ── the panel's history routes and the host ────────────────────────────


class _ClearableHistory:
    """Just enough history for the clear route: it counts and empties.

    Rows carry ``entry_id`` because the store's own do -- the clear route only
    needs the length, but a fake that renamed the key would hide a route that
    read the wrong one.
    """

    def __init__(self, ids=()):
        self.ids = list(ids)

    def get_all(self):
        return [{"entry_id": entry_id} for entry_id in self.ids]

    def clear(self):
        count = len(self.ids)
        self.ids = []
        return count


def _history_dispatch(path, payload, monkeypatch, on_history_change=None, dialog_mgr=None,
                      ok=True, ids=("e1", "e2")):
    """POST one panel history route with the api layer stubbed to succeed.

    What is under test here is the routing: which route tells the host, with
    which payload, and what it does when no host is wired.  The api functions
    themselves are covered where they live.
    """
    from internal.web import routes as routes_module

    result = {"ok": ok, "count": len(ids)}
    for name in ("delete_item", "toggle_pin", "batch_pin", "batch_delete"):
        monkeypatch.setattr(routes_module, name, lambda body, history: (dict(result), 200))
    return dispatch(
        "POST",
        path,
        {},
        json.dumps(payload).encode("utf-8"),
        cfg=_Cfg(),
        history=_ClearableHistory(ids),
        sync_mgr=None,
        get_connected_ids=lambda: [],
        upload_dir=".",
        on_nav_url=None,
        on_forward_file=None,
        dialog_mgr=dialog_mgr,
        on_history_change=on_history_change,
    )


def test_every_panel_history_change_tells_the_host(monkeypatch):
    """A delete, a batch delete, a pin and a wipe each reach the journal.

    The panel's history routes broadcast straight to the panels (that predates
    the migration), so nothing published on the journal — which is where the
    native window hears about a change.  The payloads are the ones the
    runtime's own history writes publish, so the phone bridge answers the
    panels exactly as it already does for a change made in the window.
    """
    seen = []
    for path, payload, expected in [
        ("/api/delete", {"entry_id": "e1"}, {"id": "e1"}),
        ("/api/batch-delete", {"entry_ids": ["e1", "e2"]}, {"ids": ["e1", "e2"]}),
        ("/api/pin", {"entry_id": "e1"}, {}),
        ("/api/batch-pin", {"entry_ids": ["e1"]}, {}),
        ("/api/history/clear", {}, {"cleared": 2}),
    ]:
        seen.clear()
        status, _ct, _body = _history_dispatch(
            path, payload, monkeypatch, on_history_change=seen.append
        )
        assert status == 200, path
        assert seen == [expected], path


def test_a_refused_history_change_tells_the_host_nothing(monkeypatch):
    """A 400/404 changed nothing, so no surface should hear about it."""
    seen = []
    for path in ("/api/delete", "/api/pin", "/api/batch-delete", "/api/batch-pin"):
        seen.clear()
        status, _ct, _body = _history_dispatch(
            path, {"entry_id": "gone"}, monkeypatch, on_history_change=seen.append, ok=False
        )
        assert status == 200, path
        assert seen == [], path


def test_a_history_route_without_a_host_still_broadcasts(monkeypatch):
    """The legacy desktop runs this server with no companion, so with no
    callback wired the direct broadcast to the panels stays exactly as it was."""
    dlg = _FakeDialogMgr()
    for path, payload, expected in [
        ("/api/delete", {"entry_id": "e1"}, [("history_item_deleted", ["e1"])]),
        ("/api/delete", {"index": 3}, [("history_updated", None)]),
        ("/api/batch-delete", {"entry_ids": ["e1", "e2"]},
         [("history_item_deleted", ["e1", "e2"])]),
        ("/api/pin", {"entry_id": "e1"}, [("history_updated", None)]),
        ("/api/batch-pin", {"entry_ids": ["e1"]}, [("history_updated", None)]),
        ("/api/history/clear", {}, [("history_clear", None)]),
    ]:
        dlg.ws_manager.calls.clear()
        status, _ct, _body = _history_dispatch(path, payload, monkeypatch, dialog_mgr=dlg)
        assert status == 200, path
        assert dlg.ws_manager.calls == expected, path
