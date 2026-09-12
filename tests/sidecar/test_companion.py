import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from internal.adapters.sidecar.rpc import Dispatcher
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.config.config import Config, load, save
from internal.infrastructure.runtime.companion import MobileCompanion
from tests.sidecar.test_application_runtime import Runtime


@pytest.mark.parametrize("enabled", [True, False])
def test_companion_owned_after_runtime(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False, web_enabled=enabled))
    server = Mock()
    server.stop.return_value = True
    factory = Mock(return_value=server)
    app = SidecarApplication(runtime_factory=Runtime, companion_factory=factory)
    app.lifecycle.start()
    try:
        assert factory.call_count == int(enabled)
        if enabled:
            assert len(load().web_token) >= 32
            assert factory.call_args.args[1] is app._repository
            assert factory.call_args.args[2] is app.runtime
            server.stop.return_value = False
            assert app.lifecycle.stop() is False
            assert app.runtime is not None
            assert app._repository is not None
    finally:
        server.stop.return_value = True
        assert app.lifecycle.stop()


def test_failed_bind_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False, web_enabled=True))
    server = Mock()
    server.start.side_effect = ApplicationError("COMPANION_START_FAILED", "bind")
    app = SidecarApplication(
        runtime_factory=Runtime, companion_factory=lambda *args, **kwargs: server
    )
    with pytest.raises(ApplicationError):
        app.lifecycle.start()
    server.stop.assert_called_once()
    assert app.runtime is None
    assert app.lifecycle.state == "stopped"


def test_adapter_preserves_chat_contract_and_retries_stop():
    runtime = Mock()
    runtime.chat_devices.return_value = {"devices": [{"id": "peer"}]}
    runtime.chat_sessions.return_value = {"muted": ["peer"]}
    runtime.set_chat_muted.return_value = {"muted": []}
    server = Mock(_thread=None)
    factory = Mock(return_value=server)
    adapter = MobileCompanion(SimpleNamespace(), object(), runtime, object(), factory)
    kwargs = factory.call_args.kwargs
    assert kwargs["get_chat_devices"]() == [{"id": "peer"}]
    assert kwargs["get_chat_muted"]() == ["peer"]
    assert kwargs["set_chat_muted"]("peer", False) == []
    server.start.return_value = False
    with pytest.raises(ApplicationError):
        adapter.start()
    server.stop.side_effect = [RuntimeError("busy"), None]
    assert adapter.stop() is False
    assert adapter.stop() is True


def test_real_mobile_page_requires_token_and_releases_socket(tmp_path, monkeypatch):
    import json
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from internal.clipboard.format import ClipboardContent, ContentType
    from internal.clipboard.history_db import ClipboardHistoryDB
    from internal.web.server import WebServer

    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(WebServer, "_open_firewall", lambda *args: False)
    cfg = Config(encryption_enabled=False, web_enabled=True, web_port=0,
                 web_token="test-companion-secret")
    runtime = Mock()
    runtime.transport.get_connected_peers.return_value = []
    history = ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"))
    history.add(ClipboardContent(types={ContentType.TEXT: b"shared desktop history"}))
    adapter = MobileCompanion(cfg, history, runtime, None)
    try:
        adapter.start()
        address = adapter.server._httpd.server_address
        base = f"http://127.0.0.1:{address[1]}"
        devices = adapter.server._device_thread
        assert devices is not None and devices.is_alive()
        with pytest.raises(HTTPError) as denied:
            urlopen(base + "/mobile.html", timeout=3)
        assert denied.value.code in (401, 403)
        with urlopen(base + "/mobile.html?token=test-companion-secret", timeout=3) as response:
            assert response.status == 200
            assert b"<html" in response.read().lower()
        with pytest.raises(HTTPError) as denied_history:
            urlopen(base + "/api/history", timeout=3)
        assert denied_history.value.code in (401, 403)
        with urlopen(base + "/api/history?token=test-companion-secret", timeout=3) as response:
            payload = json.load(response)
        assert payload["total"] == 1
        assert "shared desktop history" in json.dumps(payload)
        assert adapter.stop()
        assert adapter.server._httpd is None
        # The device-page poll dies with the listener instead of outliving it.
        assert not devices.is_alive()
        adapter.start()
        assert adapter.server._device_thread is not devices
        assert adapter.server._device_thread.is_alive()
        restarted_port = adapter.server._httpd.server_address[1]
        with urlopen(
            f"http://127.0.0.1:{restarted_port}/api/history?token=test-companion-secret",
            timeout=3,
        ) as response:
            assert json.load(response)["total"] == 1
    finally:
        assert adapter.stop()
        assert adapter.server._httpd is None
        history.close()


@pytest.fixture
def controlled_companion(tmp_path, monkeypatch):
    from internal.web.server import WebServer

    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(WebServer, "_open_firewall", lambda *args: False)
    monkeypatch.setattr(WebServer, "_get_lan_ip", staticmethod(lambda: "127.0.0.1"))
    save(Config(encryption_enabled=False, web_enabled=False))
    app = SidecarApplication(runtime_factory=Runtime)
    app.lifecycle.start()
    app.runtime.transport = Mock()
    app.runtime.chat = Mock()
    app.runtime._chat_send_fn = Mock()
    app.runtime.chat_invite = Mock()
    app.runtime.transport.get_connected_peers.return_value = []
    yield app, Dispatcher(app)
    assert app.lifecycle.stop()


def test_the_device_page_is_pushed_only_when_its_snapshot_changes():
    """Legacy's fingerprint poll: a steady page stays quiet."""
    import threading

    from internal.web.server import WebServer

    server = WebServer.__new__(WebServer)
    server.DEVICE_BROADCAST_INTERVAL = 0.01
    server._httpd = object()
    server._device_stop = threading.Event()

    class Ws:
        def __init__(self):
            self.polls = 0
            self.broadcasts = []

        def devices_fingerprint(self):
            self.polls += 1
            if self.polls >= 4:
                # The script is consumed — let the loop finish.
                server._device_stop.set()
            return ["", "a", "a", "b"][min(self.polls, 4) - 1]

        def broadcast_devices(self):
            self.broadcasts.append(self.polls)

    server._ws_manager = ws = Ws()
    server._device_broadcast_loop()
    # The first real snapshot and the change to "b" are pushed; the unchanged
    # poll between them (and the empty one before the page had anything) is not.
    assert ws.broadcasts == [2, 4]


def test_the_device_page_loop_ends_when_the_listener_is_gone():
    import threading

    from internal.web.server import WebServer

    server = WebServer.__new__(WebServer)
    server.DEVICE_BROADCAST_INTERVAL = 0.01
    server._httpd = None
    server._device_stop = threading.Event()
    server._ws_manager = Mock()
    server._device_broadcast_loop()
    server._ws_manager.devices_fingerprint.assert_not_called()


def free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def post_settings(base, token, payload):
    """POST /api/settings the way the phone's web panel does."""
    import json
    from urllib.request import Request, urlopen

    request = Request(
        f"{base}/api/settings?token={token}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def started_companion(rpc):
    """Start the real listener and return (settings base URL, token)."""
    status = rpc.call("companion.configure", {"enabled": True, "port": free_port()})
    return f"http://127.0.0.1:{status['actual_port']}", status["token"]


def test_adapter_forwards_the_web_host_callbacks():
    runtime = Mock()
    runtime.chat_devices.return_value = {"devices": []}
    runtime.chat_sessions.return_value = {"muted": []}
    runtime.set_chat_muted.return_value = {"muted": []}
    server = Mock(_thread=None)
    factory = Mock(return_value=server)
    changes, restarts = [], []
    MobileCompanion(
        SimpleNamespace(), object(), runtime, object(), factory,
        on_settings_change=lambda updated, special: changes.append((updated, special)),
        on_restart=lambda: restarts.append(True),
    )
    kwargs = factory.call_args.kwargs
    kwargs["on_settings_change"]({"device_name": "Phone"}, {"clear_password": True})
    kwargs["on_restart"]()
    assert changes == [({"device_name": "Phone"}, {"clear_password": True})]
    assert restarts == [True]
    # The panel's own favourite routes write the store directly, so the host
    # has to say so on the journal the desktop window listens to.
    kwargs["on_favorites_change"]()
    runtime.events.publish.assert_called_once_with("favorites.changed", {})
    # Same seam, other event: the panel's history routes broadcast to the
    # panels directly, so a delete made on a phone reached no native window.
    runtime.events.publish.reset_mock()
    kwargs["on_history_change"]({"id": "e1"})
    runtime.events.publish.assert_called_once_with("history.changed", {"id": "e1"})
    # A route that could not name what it changed still says something.
    runtime.events.publish.reset_mock()
    kwargs["on_history_change"]({})
    runtime.events.publish.assert_called_once_with("history.changed", {})
    # The seam is a plain callable, so nothing stops a host wiring something
    # that hands it a payload which is not a mapping at all.  It is normalised
    # to that same "something changed" shape rather than passed through:
    # `PhonePush` reads these payloads by key, and an event whose payload is a
    # bare string would be the one history change it could not answer.
    runtime.events.publish.reset_mock()
    kwargs["on_history_change"](None)
    runtime.events.publish.assert_called_once_with("history.changed", {})


def test_web_panel_settings_apply_live(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)

    # Plain fields reach the live engines, not just config.json.
    result = post_settings(base, token, {"sync_enabled": False, "device_name": "From Phone"})
    assert result["ok"] is True
    assert app.config.sync_enabled is False
    assert app.config.device_name == "From Phone"
    assert app.runtime.sync.set_enabled.call_args_list[-1].args == (False,)
    assert app.runtime.applied_settings[-1] == {
        "sync_enabled": False, "device_name": "From Phone",
    }

    # The security section re-wires live encryption, exactly like the desktop
    # path: the panel used to persist the toggle and change nothing.
    result = post_settings(
        base, token, {"encryption_enabled": True, "password": "Str0ng-Passw0rd!"}
    )
    assert result["password_set"] is True
    assert app.config.encryption_password == "Str0ng-Passw0rd!"
    assert app.config.netpair_password == "Str0ng-Passw0rd!"
    assert app.config.encryption_password_hash
    assert app.identity.encryption is not None
    assert app.runtime.applied_encryption is app.identity.encryption

    result = post_settings(base, token, {"password": "", "clear_password": True})
    assert result["password_set"] is False
    assert app.config.encryption_password == ""
    assert app.config.encryption_password_hash == ""
    assert app.config.netpair_password == ""
    # The clear action does not flip the toggle (the panel sends only
    # password/clear_password), so the live manager falls back to the
    # fingerprint-derived at-rest key exactly as the legacy handler does.
    assert app.identity.encryption is not None
    assert app.identity.encryption._password == ""
    assert app.runtime.applied_encryption is app.identity.encryption

    # Turning the toggle off over the web drops live encryption entirely.
    post_settings(base, token, {"encryption_enabled": False})
    assert app.identity.encryption is None
    assert app.runtime.applied_encryption is None

    # Translation key set/clear are action keys the settings API cannot apply.
    assert post_settings(base, token, {"set_translate_key": " panel-key "}) == {
        "ok": True, "updated": {}, "translate_key_set": True,
    }
    assert app.config.translate_api_key == "panel-key"
    assert load().translate_api_key == "panel-key"
    assert post_settings(base, token, {"clear_translate_key": True}) == {
        "ok": True, "updated": {}, "translate_key_set": False,
    }
    assert app.config.translate_api_key == ""

    # Token rotation answers with the new token so the phone can keep working.
    rotated = post_settings(base, token, {"regenerate_web_token": True})
    assert rotated["token_updated"] is True
    assert rotated["web_token"] and rotated["web_token"] != token
    assert load().web_token == rotated["web_token"]
    assert post_settings(base, rotated["web_token"], {"device_name": "Phone 2"})["ok"]
    cleared = post_settings(base, rotated["web_token"], {"clear_web_token": True})
    assert cleared["web_token"] == ""
    assert load().web_token == ""
    assert post_settings(base, "", {"device_name": "Phone 3"})["ok"]


def test_web_panel_factory_reset_and_restart_reach_the_host(
    controlled_companion, monkeypatch
):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)
    # The real reset stops the services answering this request and the host
    # relaunches the process; both are asserted directly below, so the timer is
    # run inline and the sidecar's own reset is replaced.
    monkeypatch.setattr(
        SidecarApplication, "_defer", staticmethod(lambda callback, delay=0.4: callback())
    )
    resets = []
    monkeypatch.setattr(app, "factory_reset", lambda: resets.append(True) or {"ok": True})

    assert post_settings(base, token, {"factory_reset": True})["ok"] is True
    assert resets == [True]
    events, _ = app.events.since(0)
    assert [event["name"] for event in events].count("app.restart_requested") == 1
    assert events[-1]["data"] == {"reason": "factory_reset"}

    # The panel's Restart App action uses the same host hand-off.
    from urllib.request import Request, urlopen

    request = Request(f"{base}/api/restart?token={token}", data=b"{}", method="POST")
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
    events, _ = app.events.since(0)
    assert events[-1]["name"] == "app.restart_requested"
    assert events[-1]["data"] == {"reason": "restart"}


def test_web_panel_remote_access_toggle_moves_the_listener(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)
    assert app.companion_status()["running"] is True

    post_settings(base, token, {"web_enabled": False, "web_port": app.config.web_port})
    deadline = time.time() + 5
    while time.time() < deadline and app.companion_status()["running"]:
        time.sleep(0.05)
    assert app.companion_status()["running"] is False
    assert load().web_enabled is False


def test_rpc_controls_real_listener_and_rotates_credentials(controlled_companion):
    from urllib.error import HTTPError
    from urllib.request import urlopen

    app, rpc = controlled_companion
    assert rpc.call("companion.status", {})["token"] is None
    first = rpc.call("companion.configure", {"enabled": True, "port": free_port()})
    assert first["running"] and first["actual_port"] == first["port"]
    with urlopen(first["access_url"], timeout=3) as response:
        assert response.status == 200
    owner = app.companion
    assert rpc.call("companion.configure", {"enabled": True}) == first
    assert app.companion is owner
    rotated = rpc.call("companion.configure", {"enabled": True, "rotate_token": True})
    assert rotated["token"] != first["token"]
    with pytest.raises(HTTPError) as denied:
        urlopen(first["access_url"], timeout=3)
    assert denied.value.code in (401, 403)
    moved = rpc.call("companion.configure", {"enabled": True, "port": free_port()})
    assert moved["actual_port"] != first["actual_port"]
    with urlopen(moved["access_url"], timeout=3) as response:
        assert response.status == 200
    stopped = rpc.call("companion.configure", {"enabled": False})
    assert not stopped["running"] and stopped["access_url"] is None
    assert stopped["actual_port"] is None and stopped["token"] is None
    assert load().web_enabled is False
    assert app.runtime is not None


@pytest.mark.parametrize("params", [
    {}, {"enabled": 1}, {"enabled": True, "port": True},
    {"enabled": True, "port": 0}, {"enabled": True, "port": 65536},
    {"enabled": True, "token": "injected"}, {"enabled": True, "rotate_token": 1},
])
def test_rpc_rejects_invalid_companion_configuration(controlled_companion, params):
    _, rpc = controlled_companion
    with pytest.raises(ApplicationError) as error:
        rpc.call("companion.configure", params)
    assert error.value.code == "VALIDATION_ERROR"


def test_rpc_bind_failure_reports_stopped_and_allows_retry(controlled_companion):
    import socket

    app, rpc = controlled_companion
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        with pytest.raises(ApplicationError) as error:
            rpc.call("companion.configure", {
                "enabled": True, "port": occupied.getsockname()[1],
            })
        assert error.value.code == "COMPANION_START_FAILED"
    status = rpc.call("companion.status", {})
    assert status["enabled"] and not status["running"]
    assert status["access_url"] is None
    assert app.runtime is not None
    assert rpc.call("companion.configure", {"enabled": True})["running"]


def test_rpc_save_failure_and_stop_failure(controlled_companion, monkeypatch):
    app, rpc = controlled_companion
    first = rpc.call("companion.configure", {"enabled": True, "port": free_port()})
    with monkeypatch.context() as patch:
        patch.setattr(app.companion, "stop", lambda: False)
        with pytest.raises(ApplicationError) as error:
            rpc.call("companion.configure", {"enabled": False})
        assert error.value.code == "COMPANION_STOP_FAILED"
        assert app.config.web_enabled is True
    with monkeypatch.context() as patch:
        patch.setattr("internal.application.bootstrap.save", Mock(side_effect=OSError))
        with pytest.raises(ApplicationError) as error:
            rpc.call("companion.configure", {"enabled": False})
        assert error.value.code == "SAVE_FAILED"
    assert app.config.web_enabled is True
    assert not rpc.call("companion.status", {})["running"]
    assert load().web_token == first["token"]


# ── the phone panel's host callbacks (every route must answer, never 503) ──


def get_json(base, token, path):
    import json
    from urllib.request import urlopen

    sep = "&" if "?" in path else "?"
    with urlopen(f"{base}{path}{sep}token={token}", timeout=5) as response:
        return json.load(response)


def post_json(base, token, path, payload=None):
    import json
    from urllib.request import Request, urlopen

    request = Request(
        f"{base}{path}?token={token}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def multipart(parts):
    """Build a multipart/form-data body from ``[(field, filename|None, bytes)]``."""
    boundary = "----clipsync-test-boundary"
    chunks = []
    for field, filename, data in parts:
        disposition = f'form-data; name="{field}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post_raw(base, token, path, body, content_type):
    import json
    from urllib.request import Request, urlopen

    request = Request(
        f"{base}{path}?token={token}",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def test_web_panel_devices_and_certs_read_live_host_state(controlled_companion):
    app, rpc = controlled_companion
    # The adapter binds the runtime callbacks when the companion is built, so
    # a replacement has to be in place before the listener starts.
    app.runtime.discovered = {
        "hash1234": {"name": "Phone", "address": "10.0.0.7", "port": 8765}
    }
    app.runtime.pending_pairings = lambda: [
        ("peer", "123456", "Peer", "pending", "AB12-CD34")
    ]
    base, token = started_companion(rpc)

    payload = get_json(base, token, "/api/devices")
    rows = {row["device_id"]: row for row in payload["devices"]}
    assert rows["hash1234"]["device_name"] == "Phone"
    assert rows["hash1234"]["known"] is False
    # The SAS the runtime appends is what the phone's confirmation card shows.
    assert payload["pending_pairings"] == [
        {
            "peer_id": "peer",
            "code": "123456",
            "peer_name": "Peer",
            "status": "pending",
            "sas": "AB12-CD34",
        }
    ]
    assert get_json(base, token, "/api/devices/certs") == {"devices": []}


def test_web_panel_overview_uses_the_host_aggregate(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)

    overview = get_json(base, token, "/api/overview")["overview"]
    assert overview["port"] == app.config.web_port
    assert overview["local_ip"] == "127.0.0.1"
    assert overview["discovering"] is True and overview["visible"] is True
    assert overview["web_enabled"] is True
    assert overview["paired_count"] == 0
    assert overview["history_count"] == 0
    assert overview["version"] and overview["platform"]
    assert overview["uptime_seconds"] >= 0


def test_web_panel_diagnostics_and_update_status_come_from_the_host(
    controlled_companion,
):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)

    report = get_json(base, token, "/api/diagnostics")
    assert report["checks"], "the host report replaces the unavailable fallback"
    assert not any(c.get("detail") == "diagnostics unavailable" for c in report["checks"])

    status = get_json(base, token, "/api/update/status")
    assert "state" in status
    assert status["state"]["phase"] in ("idle", "downloading", "ready", "failed")


def test_web_panel_diagnostics_request_reaches_the_host_action(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)

    # An unknown action exercises the host callback without touching the OS.
    assert post_json(base, token, "/api/diagnostics/request", {
        "action": "no-such-action",
    }) == {"ok": False, "error": "Unknown action: no-such-action"}


def test_web_panel_transfer_routes_reach_the_runtime(controlled_companion):
    app, rpc = controlled_companion
    # Bound when the companion is built — patch before the listener starts.
    app.runtime.transfer_lists = lambda: (
        [
            {
                "transfer_id": "t1",
                "file_name": "big.zip",
                "direction": "down",
                "state": "receiving",
                "progress": 0.25,
            }
        ],
        [],
    )
    base, token = started_companion(rpc)

    payload = get_json(base, token, "/api/transfer")
    assert payload["active"][0]["id"] == "t1"
    assert payload["active"][0]["filename"] == "big.zip"
    assert payload["active"][0]["progress"] == 25.0

    assert post_json(base, token, "/api/transfer/cancel", {"transfer_id": "t1"}) == {
        "ok": True
    }
    assert post_json(
        base, token, "/api/transfer/history/delete", {"transfer_id": "t2"}
    ) == {"ok": True}
    # The panel speaks the legacy name; LanRuntime maps history_delete→delete.
    assert app.runtime.transfer_actions == [("cancel", "t1"), ("history_delete", "t2")]

    # Cancel-all reads the live list through on_get_transfers and cancels each row.
    assert post_json(base, token, "/api/transfer/cancel-all") == {
        "ok": True,
        "cancelled": 1,
    }
    assert app.runtime.transfer_actions[-1] == ("cancel", "t1")

    assert post_json(base, token, "/api/speed-test") == {"ok": True}
    assert get_json(base, token, "/api/speed-test") == {
        "done": False,
        "mbps": None,
        "progress": 0.0,
        "status": "",
        "quality": "",
    }


def test_web_panel_device_actions_reach_the_runtime(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)

    assert post_json(
        base, token, "/api/device/note", {"peer_id": "peer", "note": "desk"}
    ) == {"ok": True}
    assert post_json(
        base, token, "/api/device/pair", {"peer_id": "peer", "code": "123456"}
    ) == {"ok": True}
    assert post_json(base, token, "/api/device/unpair", {"peer_id": "peer"}) == {
        "ok": True
    }
    assert post_json(base, token, "/api/device/test", {"peer_id": "peer"}) == {
        "ok": True,
        "device_id": "peer",
    }
    assert app.runtime.device_actions == [
        ("edit_note", "peer", ("desk",)),
        ("pair", "peer", ("123456",)),
        ("unpair", "peer", ()),
    ]


def test_web_panel_toggles_and_nav_reach_the_runtime(controlled_companion):
    app, rpc = controlled_companion
    calls = []
    # Bound when the companion is built — patch before the listener starts.
    app.runtime.set_discovery_enabled = lambda enabled: calls.append(("discovery", enabled))
    app.runtime.set_discovery_visible = lambda enabled: calls.append(("visible", enabled))
    base, token = started_companion(rpc)

    assert post_json(base, token, "/api/discovery/toggle", {"enabled": True}) == {
        "ok": True,
        "enabled": True,
    }
    assert post_json(base, token, "/api/visibility/toggle", {"enabled": False}) == {
        "ok": True,
        "enabled": False,
    }
    assert calls == [("discovery", True), ("visible", False)]

    assert post_json(
        base,
        token,
        "/api/nav",
        {"url": "https://example.com/x", "device_id": "peer"},
    ) == {"ok": True}
    assert app.runtime.sent_url == ("peer", "https://example.com/x")


def test_web_panel_ui_requests_become_host_events(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)

    assert post_json(base, token, "/api/show_qr") == {"ok": True}
    assert post_json(base, token, "/api/send_url") == {"ok": True}
    assert post_json(base, token, "/api/window", {"action": "close"}) == {"ok": True}
    names = [event["name"] for event in app.events.since(0)[0]]
    assert names[-3:] == [
        "app.qr_requested",
        "app.send_url_requested",
        "app.window_close_requested",
    ]


def test_web_panel_upload_records_a_received_file(controlled_companion, tmp_path):
    from pathlib import Path

    app, rpc = controlled_companion
    app.config.file_receive_dir = str(tmp_path)
    base, token = started_companion(rpc)
    recorded = []
    app.runtime.record_web_upload = (
        lambda name, size, path: recorded.append((name, size, path)) or "web-tid"
    )

    body, content_type = multipart([("file", "notes.txt", b"hello phone")])
    payload = post_raw(base, token, "/api/upload", body, content_type)
    assert payload == {"ok": True, "name": "notes.txt", "size": 11}
    name, size, saved_path = recorded[0]
    assert (name, size) == ("notes.txt", 11)
    assert Path(saved_path).read_bytes() == b"hello phone"
    # The transfers panel is told to refresh with the new row, and the host is
    # told a file arrived so it can raise the legacy "file received" notification.
    events = app.events.since(0)[0]
    assert [e["name"] for e in events][-2:] == ["transfers.changed", "transfer.web_upload"]
    assert events[-1]["data"] == {
        "transfer_id": "web-tid", "name": "notes.txt", "size": 11,
    }


def test_web_panel_upload_forwards_to_a_peer_instead_of_recording(
    controlled_companion, tmp_path
):
    app, rpc = controlled_companion
    app.config.file_receive_dir = str(tmp_path)
    forwarded, recorded = [], []
    # forward_file is bound when the companion is built — patch before start.
    app.runtime.forward_file = (
        lambda path, device_id: forwarded.append((path, device_id)) or True
    )
    app.runtime.record_web_upload = lambda *args: recorded.append(args)
    base, token = started_companion(rpc)

    body, content_type = multipart(
        [("file", "report.pdf", b"%PDF-1.4"), ("device_id", None, b"peer")]
    )
    payload = post_raw(base, token, "/api/upload", body, content_type)
    assert payload == {"ok": True, "name": "report.pdf", "size": 8}
    assert forwarded[0][1] == "peer"
    assert recorded == []


def test_web_panel_upload_reports_an_offline_forward_target(
    controlled_companion, tmp_path
):
    from pathlib import Path

    from internal.web.server import _get_upload_dir

    app, rpc = controlled_companion
    app.config.file_receive_dir = str(tmp_path)
    # forward_file is bound when the companion is built — patch before start.
    app.runtime.forward_file = lambda path, device_id: False
    base, token = started_companion(rpc)
    upload_dir = Path(_get_upload_dir(app.config))

    body, content_type = multipart(
        [("file", "offline.bin", b"data"), ("device_id", None, b"peer")]
    )
    payload = post_raw(base, token, "/api/upload", body, content_type)
    assert payload["ok"] is False and payload["error"]
    # The file the target never received is not left behind on disk.
    assert not (upload_dir / "offline.bin").exists()


def test_web_panel_file_open_and_reveal_use_the_host_handlers(
    controlled_companion, monkeypatch, tmp_path
):
    from pathlib import Path

    from internal.system import file_manager
    from internal.web.server import _get_upload_dir

    app, rpc = controlled_companion
    app.config.file_receive_dir = str(tmp_path)
    base, token = started_companion(rpc)
    received = Path(_get_upload_dir(app.config)) / "received.txt"
    received.write_bytes(b"payload")
    opened, revealed = [], []
    with monkeypatch.context() as patch:
        patch.setattr(
            file_manager, "open_file", lambda p: (opened.append(p), (True, p))[1]
        )
        patch.setattr(
            file_manager, "reveal_folder", lambda p: (revealed.append(p), (True, p))[1]
        )
        assert post_json(base, token, "/api/file/open", {"path": "received.txt"}) == {
            "ok": True
        }
        assert post_json(base, token, "/api/file/reveal", {"path": "received.txt"}) == {
            "ok": True
        }
    assert opened == [str(received)]
    assert revealed == [str(received)]

    # Confinement: a path outside the received-files directory is refused.
    from urllib.error import HTTPError

    with pytest.raises(HTTPError) as refused:
        post_json(base, token, "/api/file/open", {"path": "../outside.txt"})
    assert refused.value.code == 400


@pytest.mark.parametrize(
    "method, path, payload",
    [
        ("GET", "/api/devices", None),
        ("GET", "/api/overview", None),
        ("GET", "/api/devices/certs", None),
        ("GET", "/api/transfer", None),
        ("GET", "/api/speed-test", None),
        ("GET", "/api/diagnostics", None),
        ("GET", "/api/update/status", None),
        ("POST", "/api/device/note", {"peer_id": "peer", "note": "x"}),
        ("POST", "/api/device/pair", {"peer_id": "peer", "code": "1"}),
        ("POST", "/api/device/reject", {"peer_id": "peer"}),
        ("POST", "/api/device/unpair", {"peer_id": "peer"}),
        ("POST", "/api/device/connect", {"peer_id": "peer"}),
        ("POST", "/api/device/disconnect", {"peer_id": "peer"}),
        ("POST", "/api/device/forget", {"peer_id": "peer"}),
        ("POST", "/api/device/restore", {"peer_id": "peer"}),
        ("POST", "/api/device/purge", {"peer_id": "peer"}),
        ("POST", "/api/device/test", {"peer_id": "peer"}),
        ("POST", "/api/transfer/cancel", {"transfer_id": "t"}),
        ("POST", "/api/transfer/pause", {"transfer_id": "t"}),
        ("POST", "/api/transfer/resume", {"transfer_id": "t"}),
        ("POST", "/api/transfer/retry", {"transfer_id": "t"}),
        ("POST", "/api/transfer/history/delete", {"transfer_id": "t"}),
        ("POST", "/api/transfer/cancel-all", {}),
        ("POST", "/api/speed-test", {}),
        ("POST", "/api/diagnostics/request", {"action": "no-such-action"}),
        ("POST", "/api/update/open-folder", {}),
        ("POST", "/api/discovery/toggle", {"enabled": True}),
        ("POST", "/api/visibility/toggle", {"enabled": True}),
        ("POST", "/api/show_qr", {}),
        ("POST", "/api/send_url", {}),
        ("POST", "/api/window", {"action": "close"}),
        ("POST", "/api/nav", {"url": "https://example.com", "device_id": "peer"}),
    ],
)
def test_every_phone_panel_route_is_wired(controlled_companion, method, path, payload):
    """No route the panel calls may answer 503 "not available".

    A missing host callback does not fail loudly — the phone shows a button
    that silently does nothing — so this pins every route to a live handler.
    """
    _, rpc = controlled_companion
    base, token = started_companion(rpc)
    result = (
        get_json(base, token, path)
        if method == "GET"
        else post_json(base, token, path, payload)
    )
    assert result.get("error") != "not available"


def request_json(base, token, method, path, payload=None):
    """``get_json``/``post_json`` that return the error status instead of raising.

    The internet routes answer 400/503 for the states the panel is expected to
    render (a bad code, an offline relay), so their tests need the status code
    and the body together.
    """
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    sep = "&" if "?" in path else "?"
    request = Request(
        f"{base}{path}{sep}token={token}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.fixture
def internet_panel(controlled_companion):
    """A started companion whose runtime serves the internet panel routes.

    The shared Runtime double has no internet subsystem, so the real pairing
    service and delivery readouts are attached here.  The routes are bound to
    the runtime rather than to a snapshot of it, so what they answer is
    whatever the runtime exposes at request time.
    """
    from internal.infrastructure.runtime.internet_pairing import InternetPairingService

    app, rpc = controlled_companion
    app.config.internet_sync_enabled = True
    sends = [
        {
            "msg_id": "m1", "ts": 1234.5, "status": "queued", "preview": "hello",
            "content_hash": "hash-1", "kind": "clipboard", "session_id": "",
        }
    ]
    app.runtime.internet_pairing = InternetPairingService(app.config, app.runtime.save_config)
    app.runtime.relay_delivery_status = lambda peer_id="": {"pending": 1, "items": sends}
    app.runtime.delivery_counts = lambda: {"peers": {"peer": 1}}
    base, token = started_companion(rpc)
    return SimpleNamespace(
        app=app, rpc=rpc, base=base, token=token,
        service=app.runtime.internet_pairing, sends=sends,
    )


def test_the_phone_internet_panel_lists_confirmed_peers_only(internet_panel):
    """A provisional tag from an unconfirmed `enter` is not a paired device.

    It is not nothing, either: the tag is a code this machine is waiting on, and
    the status reports it under ``waiting`` — apart from ``peers``, because a
    4-char tag cannot be reached, sent to or renamed, and listing it as a device
    showed a phantom with no way to remove it.  Dropping it entirely was the
    other half of the same mistake: between submitting a code and the partner's
    reply, the panel had no answer to "did it connect?" at all.
    """
    from internal.config.config import PeerInfo

    cfg, service = internet_panel.app.config, internet_panel.service
    cfg.netpair_secrets = {"remote": "secret", "ABCD": "provisional"}
    cfg.peers["remote"] = PeerInfo(device_id="remote", device_name="Remote", paired=True)
    # A hello was heard, but it carried no device name — the paired device's
    # own name is the fallback the legacy status used.
    service.note_hello("remote", "")

    status, body = request_json(
        internet_panel.base, internet_panel.token, "GET", "/api/internetpair/status"
    )
    assert status == 200
    assert body["generated_code"] is None
    assert [peer["peer_id"] for peer in body["peers"]] == ["remote"]
    peer = body["peers"][0]
    assert peer["name"] == "Remote"
    assert peer["online"] is True
    assert peer["last_seen"] is not None
    assert peer["alias"] == "" and peer["paired"] is True
    # The unconfirmed code is reported as the wait it is, and only there.
    assert [row["peer_id"] for row in body["waiting"]] == ["ABCD"]
    assert body["waiting"][0]["name"] == ""


def test_generating_a_code_from_the_phone_supersedes_the_last_one(internet_panel):
    base, token = internet_panel.base, internet_panel.token
    status, body = request_json(base, token, "POST", "/api/internetpair/generate")
    assert status == 200 and body["ok"] is True and body["code"]

    _, after_first = request_json(base, token, "GET", "/api/internetpair/status")
    assert after_first["generated_code"] == body["code"]

    _, second = request_json(base, token, "POST", "/api/internetpair/generate")
    _, after_second = request_json(base, token, "GET", "/api/internetpair/status")
    assert second["code"] != body["code"]
    assert after_second["generated_code"] == second["code"]


def test_the_phone_delivery_routes_report_the_runtime_ledger(internet_panel):
    base, token = internet_panel.base, internet_panel.token
    status, body = request_json(base, token, "GET", "/api/internetdelivery?peer_id=peer")
    assert status == 200
    # The panel reads `sends`; the runtime calls the same rows `items`.
    assert body == {"pending": 1, "sends": internet_panel.sends}

    assert request_json(base, token, "GET", "/api/internetdelivery/counts") == (
        200, {"peers": {"peer": 1}},
    )


def test_renaming_from_the_phone_validates_the_alias(internet_panel):
    cfg = internet_panel.app.config
    cfg.netpair_secrets = {"remote": "secret"}
    base, token = internet_panel.base, internet_panel.token

    def rename(payload):
        return request_json(base, token, "POST", "/api/internetpair/rename", payload)

    assert rename({"peer_id": "ghost", "name": "x"}) == (
        400, {"ok": False, "error": "unknown peer"},
    )
    assert rename({"peer_id": "remote", "name": 7}) == (
        400, {"ok": False, "error": "invalid name"},
    )
    assert rename({"peer_id": "remote", "name": "x" * 121})[0] == 400
    assert cfg.netpair_aliases == {}

    assert rename({"peer_id": "remote", "name": "  Desk  "}) == (200, {"ok": True})
    assert cfg.netpair_aliases == {"remote": "Desk"}
    # Whitespace clears the alias rather than storing a blank one.
    assert rename({"peer_id": "remote", "name": "   "}) == (200, {"ok": True})
    assert cfg.netpair_aliases == {}


def test_entering_a_pairing_code_from_the_phone_needs_the_relay_online(internet_panel):
    from internal.transport.relay import generate_netpair_code, generate_netpair_secret

    cfg = internet_panel.app.config
    base, token = internet_panel.base, internet_panel.token
    code = generate_netpair_code("some-other-device", generate_netpair_secret())

    # The confirmation hello rides the relay; pairing without it would leave a
    # device listed as paired that never syncs.
    status, body = request_json(base, token, "POST", "/api/internetpair/enter", {"code": code})
    assert (status, body) == (503, {"ok": False, "error": "relay not connected"})
    assert cfg.netpair_secrets == {}

    assert request_json(
        base, token, "POST", "/api/internetpair/enter", {"code": "nope"}
    ) == (400, {"ok": False, "error": "invalid pairing code"})


def test_unpairing_from_the_phone_severs_the_internet_pair(internet_panel):
    """The route drops the secret + alias; the sibling-tab push comes from the
    runtime (see test_relay_delivery), so every caller of the unpair reaches it."""
    cfg = internet_panel.app.config
    cfg.netpair_secrets = {"remote": "secret"}
    cfg.netpair_aliases["remote"] = "Desk"
    base, token = internet_panel.base, internet_panel.token

    assert request_json(
        base, token, "POST", "/api/internetpair/unpair", {"peer_id": "ghost"}
    ) == (400, {"ok": False, "error": "unknown peer"})

    assert request_json(
        base, token, "POST", "/api/internetpair/unpair", {"peer_id": "remote"}
    ) == (200, {"ok": True})
    assert cfg.netpair_secrets == {} and cfg.netpair_aliases == {}


def test_the_phone_relay_test_probes_the_brokers_it_was_given(internet_panel):
    base, token = internet_panel.base, internet_panel.token
    app = internet_panel.app
    probed = []

    def probe(brokers):
        probed.append(list(brokers))
        return [
            {"endpoint": "mqtt://b:1", "ok": True, "latency_ms": 5.0, "detail": "reachable"},
            {"endpoint": "mqtt://a:1", "ok": False, "latency_ms": None, "detail": "timeout"},
        ]

    app.companion.internet_panel._probe = probe
    status, body = request_json(
        base, token, "POST", "/api/internetpair/test",
        {"brokers": ["mqtt://a:1", "mqtt://b:1"]},
    )
    assert status == 200
    assert probed == [["mqtt://a:1", "mqtt://b:1"]]
    assert body["summary"] == "1/2 reachable"
    assert [row["endpoint"] for row in body["results"]] == ["mqtt://b:1", "mqtt://a:1"]

    # Nothing staged and nothing saved is a 400 the panel renders as its
    # result summary — and no probe is attempted.
    app.config.relay_brokers = []
    assert request_json(base, token, "POST", "/api/internetpair/test", {}) == (
        400, {"ok": False, "error": "no relay brokers configured"},
    )
    assert probed == [["mqtt://a:1", "mqtt://b:1"]]

    # An empty staged list falls back to the persisted brokers.
    app.config.relay_brokers = ["mqtt://saved:1883"]
    assert request_json(base, token, "POST", "/api/internetpair/test", {"brokers": []})[0] == 200
    assert probed[-1] == ["mqtt://saved:1883"]


@pytest.mark.parametrize(
    "method, path, payload",
    [
        ("POST", "/api/internetpair/generate", {}),
        ("POST", "/api/internetpair/enter", {"code": "NOPE-NOPE-NOPE"}),
        ("GET", "/api/internetpair/status", None),
        ("POST", "/api/internetpair/rename", {"peer_id": "remote", "name": "Desk"}),
        ("POST", "/api/internetpair/unpair", {"peer_id": "remote"}),
        ("POST", "/api/internetpair/test", {"brokers": ["mqtt://a:1"]}),
        ("GET", "/api/internetdelivery", None),
        ("GET", "/api/internetdelivery/counts", None),
    ],
)
def test_every_phone_internet_route_is_wired(internet_panel, method, path, payload):
    """No internet route may answer "unavailable" again.

    These are the two bind-style branches legacy bound to the Application.
    Nothing bound them under the sidecar, so every one of them answered 503
    and the panel's buttons did nothing; this pins each to a live host.
    """
    app = internet_panel.app
    app.config.netpair_secrets = {"remote": "secret"}
    app.companion.internet_panel._probe = lambda brokers: []
    status, body = request_json(
        internet_panel.base, internet_panel.token, method, path, payload
    )

    assert status != 404
    assert body.get("error") not in (
        "not available", "internet pairing unavailable", "internet delivery unavailable",
    )


def test_web_panel_update_download_is_wired(controlled_companion):
    app, rpc = controlled_companion
    base, token = started_companion(rpc)
    started = []
    app.updates.start_download = lambda: (
        started.append(True) or {"ok": True, "started": True, "error": None}
    )
    assert post_json(base, token, "/api/update/download") == {
        "ok": True,
        "started": True,
        "error": None,
    }
    assert started == [True]


def test_the_phone_writes_reach_the_journal_the_window_listens_to(
    controlled_companion, tmp_path, monkeypatch
):
    """A favourite changed on the phone is published, not just stored.

    The panel writes the shared favourites store through its own routes, so
    nothing published `favorites.changed` — the event the desktop window
    invalidates its cached favourites list on.  A favourite added on a phone
    stayed invisible in the window, which legacy never had to solve: its
    desktop *was* this page, so one surface held one list.

    Driven over real HTTP through the real server: the route, the host
    callback, the runtime's journal.  The batch is the second half — a drag
    reordering two favourites and a group rename touching one are each one
    request, so each publishes once rather than once per item.
    """
    from internal.web.api import favorites as favorites_api

    app, rpc = controlled_companion
    # The favourites store is a config-dir file; keep it in the test's own.
    monkeypatch.setattr(favorites_api, "_FAV_DB_PATH", str(tmp_path / "favorites.db"))
    base, token = started_companion(rpc)

    def put(method, path, payload):
        import json as _j
        from urllib.request import Request, urlopen

        request = Request(
            f"{base}{path}?token={token}",
            data=_j.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=5) as response:
            return _j.load(response)

    published = []
    unsubscribe = app.runtime.events.subscribe(
        lambda name, data: published.append(name)
    )
    try:
        first = post_json(base, token, "/api/favorites", {"title": "a", "content": "a"})
        second = post_json(base, token, "/api/favorites", {"title": "b", "content": "b"})
        assert first["ok"] and second["ok"]
        assert published == ["favorites.changed", "favorites.changed"]

        ids = [first["favorite"]["id"], second["favorite"]["id"]]
        batch = put("PATCH", "/api/favorites", {"updates": [
            {"id": ids[0], "position": 1, "group": "work"},
            {"id": ids[1], "position": 0},
        ]})
        assert batch == {"ok": True, "updated": 2}
        assert published == ["favorites.changed"] * 3

        listed = favorites_api.get_favorites()[0]["favorites"]
        assert [f["title"] for f in listed] == ["b", "a"]
        assert [f["group"] for f in listed] == ["", "work"]
    finally:
        unsubscribe()


def test_the_phones_history_routes_reach_the_journal_the_window_listens_to(
    controlled_companion,
):
    """A clip deleted or pinned on the phone is published, not just stored.

    The panel's history routes have broadcast straight to the panels since
    before the migration, so a delete made on a phone reached no native window
    at all: the window refreshes on the event journal, and nothing put a
    ``history.changed`` there.  Legacy never had to solve it — its desktop *was*
    this page.

    Driven over real HTTP through the real server, and the payloads are
    asserted because they are meant to be indistinguishable from the ones the
    runtime's own history writes publish: ``{"id": ...}`` for one row,
    ``{"ids": [...]}`` for a batch, ``{"cleared": n}`` for a wipe, and ``{}``
    when the route could only say "something changed".
    """
    from internal.clipboard.format import ClipboardContent, ContentType

    app, rpc = controlled_companion
    for text in (b"clip one", b"clip two"):
        app._repository.add(ClipboardContent(types={ContentType.TEXT: text}))
    ids = [entry["entry_id"] for entry in app._repository.get_all()]
    assert len(ids) == 2

    base, token = started_companion(rpc)
    published = []
    unsubscribe = app.runtime.events.subscribe(
        lambda name, data: published.append((name, data)) if name == "history.changed" else None
    )
    try:
        assert post_json(base, token, "/api/delete", {"entry_id": ids[0]})["ok"] is True
        assert published == [("history.changed", {"id": ids[0]})]

        assert post_json(base, token, "/api/pin", {"entry_id": ids[1]})["ok"] is True
        assert published[-1] == ("history.changed", {})

        assert post_json(base, token, "/api/batch-pin", {"entry_ids": [ids[1]]})["ok"] is True
        assert published[-1] == ("history.changed", {})

        assert post_json(base, token, "/api/batch-delete", {"entry_ids": [ids[1]]})["ok"] is True
        assert published[-1] == ("history.changed", {"ids": [ids[1]]})

        # A wipe carries the count it removed, the same number the response
        # reports -- the phone bridge turns that shape into a panel wipe rather
        # than a snapshot, so a wiped list stops offering "Load more".
        app._repository.add(ClipboardContent(types={ContentType.TEXT: b"clip three"}))
        assert post_json(base, token, "/api/history/clear") == {"ok": True, "count": 1}
        assert published[-1] == ("history.changed", {"cleared": 1})
    finally:
        unsubscribe()
