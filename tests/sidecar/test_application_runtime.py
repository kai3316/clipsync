import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from internal.adapters.sidecar.rpc import Dispatcher
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.clipboard.format import ClipboardContent, ContentType
from internal.config.config import Config, save
from internal.infrastructure.security.device_identity import prepare_identity


class Runtime:
    def __init__(self, config, pairing, encryption, history, events, save_config):
        self.config = config
        self.pairing = pairing
        self.encryption = encryption
        self.history = history
        self.events = events
        self.save_config = save_config
        self.sync = SimpleNamespace(reset_dedup_for_restore=Mock(), set_enabled=Mock())
        self.sync_state = "not_started"
        self.stop_result = True
        self.applied_settings = []
        self.applied_encryption = None
        self.discovered = {}
        self.device_actions = []
        self.transfer_actions = []
        self.transport = Mock()
        self.transport.get_connected_peers.return_value = []
        self.transport.get_resolved_hashes.return_value = {}
        self.transport.get_reconnect_states.return_value = {}
        self.chat = Mock()
        self._chat_send_fn = Mock()

    def start(self):
        self.sync_state = "running" if self.config.sync_enabled else "paused"

    def stop(self):
        if self.stop_result:
            self.sync_state = "stopped"
        return self.stop_result

    def devices(self):
        return {"items": [{"id": "peer", "name": "Peer", "paired": False}]}

    def apply_settings(self, updated, special=None):
        """Mirror LanRuntime: the live engines read the already-mutated config."""
        self.applied_settings.append(dict(updated))
        if "sync_enabled" in updated:
            self.sync.set_enabled(bool(self.config.sync_enabled))
        if "device_name" in updated:
            self.sync.device_name = self.config.device_name

    def apply_encryption(self, encryption):
        self.applied_encryption = encryption

    def set_sync_enabled(self, enabled):
        self.config.sync_enabled = enabled
        self.sync_state = "running" if enabled else "paused"
        self.save_config()
        return {"enabled": enabled}

    def send_url(self, device_id, url):
        self.sent_url = (device_id, url)
        return {"sent": True, "device_id": device_id}

    def push_text(self, text):
        self.pushed_text = text
        return {"ok": True, "len": len(text), "sent": True}

    def discovery_state(self):
        return {"enabled": True, "visible": True}

    def set_discovery_enabled(self, enabled):
        return {"enabled": enabled, "visible": True}

    def set_discovery_visible(self, enabled):
        return {"enabled": True, "visible": enabled}

    def set_update_sink(self, sink):
        self.update_sink = sink

    # ── web companion accessors (see MobileCompanion) ────────────────────
    def discovered_peers(self):
        return dict(self.discovered)

    def resolved_hashes(self):
        return {}

    def reconnect_states(self):
        return {}

    def pending_pairings(self):
        return []

    def current_relay_broker(self):
        return ""

    def relay_state(self):
        return "off"

    def certs(self):
        return {"devices": []}

    def device_action(self, action, device_id, *args):
        self.device_actions.append((action, device_id, args))
        return True

    def test_device(self, device_id):
        return {"ok": True, "device_id": device_id}

    def web_transfer_action(self, action, transfer_id):
        self.transfer_actions.append((action, transfer_id))
        return True

    def transfer_lists(self):
        return [], []

    def speed_test_state(self):
        return {}

    def start_speed_test(self):
        return True

    def forward_file(self, path, device_id):
        return True

    def record_web_upload(self, file_name, file_size, saved_path):
        return "web-upload"

    def request_update_from_peers(self):
        self.update_requests = getattr(self, "update_requests", 0) + 1


@pytest.fixture
def runtime_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False))
    writer = SimpleNamespace(write=Mock(return_value=True))
    app = SidecarApplication(clipboard_writer_factory=lambda: writer, runtime_factory=Runtime)
    app.lifecycle.start()
    yield app
    if app.runtime is not None:
        app.runtime.stop_result = True
    assert app.lifecycle.stop()


def test_runtime_exposes_live_status_and_receives_shared_services(runtime_app):
    app = runtime_app
    assert app.status()["sync_state"] == "running"
    assert "sync.set_enabled" in app.status()["capabilities"]
    assert "devices.forget" in app.status()["capabilities"]
    assert "history.clear" in app.status()["capabilities"]
    assert "history.text" in app.status()["capabilities"]
    assert "history.open_link" in app.status()["capabilities"]
    assert "favorites.batch_add" in app.status()["capabilities"]
    assert "favorites.export" in app.status()["capabilities"]
    assert "url.send" in app.status()["capabilities"]
    assert "transfers.cancel_all" in app.status()["capabilities"]
    assert "clipboard.push" in app.status()["capabilities"]
    assert "discovery.status" in app.status()["capabilities"]
    assert app.runtime.history is app._repository
    assert app.runtime.pairing is app.identity.pairing
    assert app.devices()["items"][0]["id"] == "peer"
    assert Dispatcher(app).call("sync.set_enabled", {"enabled": False}) == {"enabled": False}
    assert app.status()["sync_state"] == "paused"
    assert Dispatcher(app).call(
        "url.send", {"device_id": "peer", "url": "https://example.com"}
    ) == {"sent": True, "device_id": "peer"}
    assert app.runtime.sent_url == ("peer", "https://example.com")
    assert Dispatcher(app).call("clipboard.push", {"text": "hello"}) == {
        "ok": True,
        "len": 5,
        "sent": True,
    }
    assert app.runtime.pushed_text == "hello"
    assert Dispatcher(app).call("discovery.status", {}) == {"enabled": True, "visible": True}
    assert Dispatcher(app).call("discovery.set_enabled", {"enabled": False}) == {
        "enabled": False,
        "visible": True,
    }
    assert Dispatcher(app).call("discovery.set_visible", {"enabled": False}) == {
        "enabled": True,
        "visible": False,
    }


def test_logs_tail_reads_the_shared_log_and_redacts_local_secrets(
    runtime_app, monkeypatch, tmp_path
):
    from internal.config import config as config_module

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(config_module, "_log_dir", lambda: log_dir)
    home = os.path.expanduser("~")
    runtime_app.config.web_token = "tok-secret"
    (log_dir / "clipsync.log").write_text(
        "\n".join(
            [f"line {i}" for i in range(300)]
            + [f"opened {home}/secret tok-secret"]
        )
        + "\n",
        encoding="utf-8",
    )
    assert "logs.tail" in runtime_app.status()["capabilities"]
    result = Dispatcher(runtime_app).call("logs.tail", {"lines": 2})
    assert result["logs"] == ["line 299", "opened [redacted]/secret [redacted]"]
    assert len(Dispatcher(runtime_app).call("logs.tail", {})["logs"]) == 200


def test_logs_export_copies_the_raw_log_to_the_host_chosen_path(
    runtime_app, monkeypatch, tmp_path
):
    from internal.config import config as config_module

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(config_module, "_log_dir", lambda: log_dir)
    (log_dir / "clipsync.log").write_text("raw token line\n", encoding="utf-8")
    dest = tmp_path / "picked by the save dialog.log"
    assert "logs.export" in runtime_app.status()["capabilities"]
    assert Dispatcher(runtime_app).call("logs.export", {"dest": str(dest)}) == {
        "ok": True,
        "path": str(dest),
        "bytes": (log_dir / "clipsync.log").stat().st_size,
    }
    # The export is the raw file: redaction is only for the viewer.
    assert dest.read_text(encoding="utf-8") == "raw token line\n"
    for params in ({}, {"dest": ""}, {"dest": str(dest), "extra": 1}, {"dest": 5}):
        with pytest.raises(ApplicationError) as error:
            Dispatcher(runtime_app).call("logs.export", params)
        assert error.value.code == "VALIDATION_ERROR"
    (log_dir / "clipsync.log").unlink()
    with pytest.raises(ApplicationError) as error:
        Dispatcher(runtime_app).call("logs.export", {"dest": str(dest)})
    assert error.value.code == "LOG_NOT_FOUND"


def test_companion_qr_encodes_the_token_url_and_reports_missing_states(
    runtime_app, monkeypatch
):
    assert "companion.qr" in runtime_app.status()["capabilities"]
    monkeypatch.setattr(
        runtime_app, "companion_status",
        lambda: {
            "running": True,
            "url": "http://10.0.0.2:8080/mobile.html",
            "access_url": "http://10.0.0.2:8080/mobile.html?token=t%2B1",
        },
    )
    result = Dispatcher(runtime_app).call("companion.qr", {})
    assert result["url"] == "http://10.0.0.2:8080/mobile.html?token=t%2B1"
    assert result["qr"].startswith("data:image/png;base64,")
    monkeypatch.setattr(
        runtime_app, "companion_status",
        lambda: {"running": False, "url": None, "access_url": None},
    )
    assert Dispatcher(runtime_app).call("companion.qr", {}) == {
        "ok": False, "error": "COMPANION_NOT_RUNNING", "url": None, "qr": None,
    }
    monkeypatch.setattr(
        runtime_app, "companion_status",
        lambda: {"running": True, "url": "http://10.0.0.2:8080/mobile.html",
                 "access_url": "http://10.0.0.2:8080/mobile.html"},
    )
    monkeypatch.setattr(
        "internal.application.bootstrap.png_data_url",
        lambda _url: (_ for _ in ()).throw(RuntimeError("no qrcode")),
    )
    assert Dispatcher(runtime_app).call("companion.qr", {}) == {
        "ok": False, "error": "QR_UNAVAILABLE",
        "url": "http://10.0.0.2:8080/mobile.html", "qr": None,
    }


def test_about_links_open_from_a_closed_target_table(runtime_app, monkeypatch):
    from internal.system import about

    opened = []
    monkeypatch.setattr(about.webbrowser, "open", opened.append)
    assert "app.open_link" in runtime_app.status()["capabilities"]
    assert Dispatcher(runtime_app).call("app.open_link", {"target": "homepage"}) == {
        "ok": True,
        "url": about.HOMEPAGE_URL,
    }
    assert opened == [about.HOMEPAGE_URL]
    for params in ({}, {"target": ""}, {"target": "https://evil.example.com"}, {"target": 1}):
        with pytest.raises(ApplicationError) as error:
            Dispatcher(runtime_app).call("app.open_link", params)
        assert error.value.code == "VALIDATION_ERROR"


def test_diagnostics_report_comes_from_the_shared_builder(runtime_app):
    app = runtime_app
    assert "diagnostics.report" in app.status()["capabilities"]
    assert "diagnostics.request" in app.status()["capabilities"]
    report = Dispatcher(app).call("diagnostics.report", {})
    assert report["v2"] is True
    assert report["summary"] in ("ok", "warn", "fail")
    assert [check["id"] for check in report["checks"]][:2] == ["server_port", "discovery"]
    assert sorted(report["groups"]) == [
        "ai_config", "chat", "filesystem", "internet", "network", "system", "transfer",
    ]
    assert report["version"]
    assert report["web_port"] == app.config.web_port
    # The report's keys live in the web panel's catalog, so the sidecar resolves
    # them for this client.
    system = report["groups"]["system"]
    assert system["label_text"] and system["items"][0]["label_text"]
    assert system["items"][0]["detail_text"]


def test_diagnostics_request_delegates_to_the_shared_repair(runtime_app, monkeypatch):
    from internal.diagnostics import actions

    monkeypatch.setattr(actions, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(actions, "linux_firewall_allow_script", lambda port, web: (None, None))
    assert Dispatcher(runtime_app).call("diagnostics.request", {"action": "firewall"}) == {
        "ok": True
    }
    assert Dispatcher(runtime_app).call("diagnostics.request", {"action": "local_network"}) == {
        "ok": True
    }


@pytest.mark.parametrize(
    "params",
    [{}, {"action": ""}, {"action": "unknown"}, {"action": 1}, {"action": "firewall", "x": 1}],
)
def test_diagnostics_request_rejects_invalid_actions(runtime_app, params):
    with pytest.raises(ApplicationError) as error:
        Dispatcher(runtime_app).call("diagnostics.request", params)
    assert error.value.code == "VALIDATION_ERROR"


def test_update_commands_delegate_to_the_shared_service(runtime_app, monkeypatch):
    app = runtime_app
    for capability in ("update.check", "update.status", "update.download",
                       "update.open_folder"):
        assert capability in app.status()["capabilities"]
    # The service reads the live configuration for the silent periodic check.
    assert app.updates._config() is app.config
    monkeypatch.setattr(
        app.updates, "check",
        lambda: {"available": True, "latest": "v2.0.0", "current": "1.0.0", "url": "u"},
    )
    # A peer-sent blob is handed to the shared service, and asking peers runs
    # before the release-server download starts.
    assert app.runtime.update_sink == app.updates.finish_from_peer
    order = []
    monkeypatch.setattr(
        app.runtime, "request_update_from_peers", lambda: order.append("peers")
    )
    monkeypatch.setattr(
        app.updates, "start_download",
        lambda: (order.append("download"), {"ok": True, "started": True, "error": None})[1],
    )
    monkeypatch.setattr(app.updates, "open_folder", lambda: {"ok": True})
    dispatcher = Dispatcher(app)
    assert dispatcher.call("update.check", {}) == {
        "available": True, "latest": "v2.0.0", "current": "1.0.0", "url": "u",
    }
    assert dispatcher.call("update.status", {})["state"]["phase"] == "idle"
    assert dispatcher.call("update.download", {}) == {
        "ok": True, "started": True, "error": None,
    }
    assert order == ["peers", "download"]
    assert dispatcher.call("update.open_folder", {}) == {"ok": True}


def test_update_download_still_starts_when_the_peer_request_fails(runtime_app, monkeypatch):
    app = runtime_app
    monkeypatch.setattr(
        app.updates, "start_download",
        lambda: {"ok": True, "started": True, "error": None},
    )

    def boom():
        raise RuntimeError("transport is down")

    monkeypatch.setattr(app.runtime, "request_update_from_peers", boom)
    assert Dispatcher(app).call("update.download", {}) == {
        "ok": True, "started": True, "error": None,
    }


def test_open_data_folder_reveals_the_app_folder(runtime_app, monkeypatch, tmp_path):
    from internal.config import config as config_module
    from internal.system import file_manager

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_module, "_config_dir", lambda: data_dir)
    calls = []
    monkeypatch.setattr(
        file_manager, "reveal_folder", lambda path: (calls.append(path), (True, path))[1]
    )
    assert "data.open_folder" in runtime_app.status()["capabilities"]
    assert Dispatcher(runtime_app).call("data.open_folder", {"which": "backups"}) == {
        "ok": True,
        "folder": str(data_dir / "backups"),
    }
    assert calls == [str(data_dir / "backups")]
    assert (data_dir / "backups").is_dir()
    with pytest.raises(ApplicationError) as error:
        Dispatcher(runtime_app).call("data.open_folder", {"which": "elsewhere"})
    assert error.value.code == "VALIDATION_ERROR"


def test_sidecar_never_registers_python_as_desktop_autostart(runtime_app, monkeypatch):
    from internal.platform import autostart

    enable = Mock()
    disable = Mock()
    monkeypatch.setattr(autostart, "enable_autostart", enable)
    monkeypatch.setattr(autostart, "disable_autostart", disable)
    runtime_app.runtime.apply_settings = Mock()
    for enabled in (True, False):
        runtime_app._on_settings_change({"auto_start": enabled}, {})
    enable.assert_not_called()
    disable.assert_not_called()


def test_failed_runtime_stop_keeps_history_identity_and_data_lock(runtime_app):
    app = runtime_app
    app.runtime.stop_result = False
    assert not app.lifecycle.stop()
    assert app.history is not None
    assert app.identity is not None
    assert app.history.list()["total"] == 0
    other = SidecarApplication()
    with pytest.raises(ApplicationError) as error:
        other.lifecycle.start()
    assert error.value.code == "DATA_IN_USE"
    app.runtime.stop_result = True
    assert app.lifecycle.stop()
    reopened = SidecarApplication()
    reopened.lifecycle.start()
    assert reopened.lifecycle.stop()


def test_copy_uses_runtime_restore_hook_before_os_write(runtime_app):
    app = runtime_app
    app._repository.add(ClipboardContent(types={ContentType.TEXT: b"restore"}))
    item = app.history.list()["items"][0]
    assert Dispatcher(app).call("history.copy", {"entry_id": item["id"]}) == {"copied": True}
    app.runtime.sync.reset_dedup_for_restore.assert_called_once_with()


def test_opening_a_link_goes_through_the_apps_own_opener(runtime_app, monkeypatch):
    # The use case is handed an opener rather than importing one, so this is the
    # wiring that decides whether "open in browser" works in a real process —
    # and the app's opener is the one with the scheme rules in it.
    app = runtime_app
    app._repository.add(ClipboardContent(types={ContentType.TEXT: b"https://example.com"}))
    opened = []
    monkeypatch.setattr("internal.system.about.webbrowser.open", opened.append)
    item = app.history.list()["items"][0]
    assert Dispatcher(app).call("history.open_link", {"entry_id": item["id"]}) == {
        "opened": True,
        "url": "https://example.com",
    }
    assert opened == ["https://example.com"]


@pytest.mark.parametrize(
    "method,params,member",
    [
        ("pairing.start", {"device_id": "peer"}, "start_pairing"),
        ("pairing.confirm", {"device_id": "peer", "code": "12345678"}, "confirm_pairing"),
        ("pairing.reject", {"device_id": "peer"}, "reject_pairing"),
        ("pairing.unpair", {"device_id": "peer"}, "unpair_device"),
    ],
)
def test_pairing_commands_call_only_the_closed_runtime_surface(runtime_app, method, params, member):
    method_mock = Mock(return_value={"accepted": True})
    setattr(runtime_app.runtime, member, method_mock)
    assert Dispatcher(runtime_app).call(method, params) == {"accepted": True}
    method_mock.assert_called_once_with(**params)


@pytest.mark.parametrize(
    "method,member",
    [
        ("devices.connect", "connect_device"),
        ("devices.disconnect", "disconnect_device"),
        ("devices.forget", "forget_device"),
        ("devices.restore", "restore_device"),
        ("devices.purge", "purge_device"),
        ("devices.test", "test_device"),
        ("devices.retrust", "retrust_device"),
    ],
)
def test_device_commands_call_only_the_closed_runtime_surface(runtime_app, method, member):
    method_mock = Mock(return_value={"ok": True})
    setattr(runtime_app.runtime, member, method_mock)
    assert Dispatcher(runtime_app).call(method, {"device_id": "peer"}) == {"ok": True}
    method_mock.assert_called_once_with("peer")


def test_device_certs_calls_only_the_closed_runtime_surface(runtime_app):
    method_mock = Mock(return_value={"devices": []})
    runtime_app.runtime.certs = method_mock
    assert Dispatcher(runtime_app).call("devices.certs", {}) == {"devices": []}
    method_mock.assert_called_once_with()


@pytest.mark.parametrize("code", ["1234567", "123456789", "abcdefgh", "\u0661" * 8, 12345678])
def test_pairing_rejects_non_ascii_or_invalid_codes(runtime_app, code):
    with pytest.raises(ApplicationError) as error:
        Dispatcher(runtime_app).call("pairing.confirm", {"device_id": "peer", "code": code})
    assert error.value.code == "VALIDATION_ERROR"


def test_password_verification_precedes_runtime_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    identity = prepare_identity(Config(), password="unlock-test")
    save(identity.config, identity.encryption)
    factory = Mock(side_effect=Runtime)
    app = SidecarApplication(runtime_factory=factory)
    app.lifecycle.start()
    try:
        factory.assert_not_called()
        with pytest.raises(ApplicationError):
            app.unlock("wrong-password")
        factory.assert_not_called()
        assert app.unlock("unlock-test") == {"unlocked": True}
        factory.assert_called_once()
        assert app.runtime.encryption is app.identity.encryption
        assert app.status()["sync_state"] == "running"
    finally:
        assert app.lifecycle.stop()


def test_partial_runtime_start_is_cleaned_before_identity_is_released(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    created = []

    class FailingRuntime(Runtime):
        def start(self):
            created.append(self)
            raise ApplicationError("LAN_START_FAILED", "Could not start LAN services")

    app = SidecarApplication(runtime_factory=FailingRuntime)
    with pytest.raises(ApplicationError):
        app.lifecycle.start()
    assert created[0].sync_state == "stopped"
    assert app.runtime is None
    assert app.identity is None
    assert app.history is None
    assert not (tmp_path / ".lock").exists()


def test_ai_profile_save_failure_restores_values_without_broadcast(runtime_app, monkeypatch):
    manager = Mock()
    runtime_app.runtime.ai_config = manager
    previous = (list(runtime_app.config.ai_config_tools),
                list(runtime_app.config.ai_config_custom_paths))
    monkeypatch.setattr("internal.application.bootstrap.save",
                        Mock(side_effect=OSError("disk full")))
    with pytest.raises(ApplicationError) as caught:
        runtime_app.update_ai_profiles(["codex"], ["~/fixture"])
    assert caught.value.code == "SAVE_FAILED"
    assert (runtime_app.config.ai_config_tools,
            runtime_app.config.ai_config_custom_paths) == previous
    manager.on_watch_list_changed.assert_not_called()


def test_ai_profile_broadcast_happens_after_successful_save(runtime_app, monkeypatch):
    events = []
    runtime_app.runtime.ai_config = SimpleNamespace(
        on_watch_list_changed=lambda: events.append("broadcast"))
    monkeypatch.setattr("internal.application.bootstrap.save",
                        lambda *_: events.append("saved"))
    result = runtime_app.update_ai_profiles(["codex", "codex"], [])
    assert result["enabled"] == ["codex"]
    assert events == ["saved", "broadcast"]
