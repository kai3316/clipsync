"""Regression + feature tests for the sync/web round after commit 155d6a9.

Covers the API layer of the four new capability groups:
  1. POST /api/transfer/retry (params, missing handler, happy path) and the
     new POST /api/transfer/cancel-all bulk action.
  2. Transfers history rows carrying ``peer_id`` (the Retry affordance).
  3. GET devices / WS snapshot attaching reconnecting/reconnect_attempt/
     reconnect_max to offline peers mid-auto-reconnect.
  4. Markdown export + the server-side history_max_age_days whitelist bounds.

Plus regression guards for the failure-history fixes in FileTransferManager
(stale-sweeper timeouts and remote-rejected sends now land in history).
"""

import json
import os
import socket
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.sync.file_transfer import FileTransferManager
from internal.transport.discovery import Discovery
from internal.web.api.devices import get_devices
from internal.web.api.settings import export_data, update_settings
from internal.web.api.transfer import get_transfers
from internal.web.routes import dispatch
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
        assert getattr(cfg, "history_max_age_days") == 0


@pytest.mark.usefixtures("sandboxed_persist")
def test_history_max_age_days_boundaries_accepted():
    cfg = _SettingsCfg()
    for good in (0, 1, 36500):
        data, status = update_settings(
            _body({"history_max_age_days": good}), cfg,
        )
        assert status == 200, good
        assert data["updated"]["history_max_age_days"] == good
        assert getattr(cfg, "history_max_age_days") == good


@pytest.mark.usefixtures("sandboxed_persist")
def test_history_max_age_days_string_type_mismatch_rejected():
    cfg = _SettingsCfg()
    data, status = update_settings(
        _body({"history_max_age_days": "30"}), cfg,
    )
    assert status == 400
    assert getattr(cfg, "history_max_age_days") == 0


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
        assert getattr(cfg, "plain_text_only") is good
    # A non-bool value against a bool field is a type mismatch -> rejected.
    cfg.plain_text_only = False
    data, status = update_settings(
        _body({"plain_text_only": "yes"}), cfg,
    )
    assert status == 400
    assert getattr(cfg, "plain_text_only") is False


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
