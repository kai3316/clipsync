import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import internal.adapters.sidecar.favorites as favorites
import internal.adapters.sidecar.rpc as rpc
import internal.application.bootstrap as bootstrap
from internal.adapters.sidecar.rpc import (
    MAX_FRAME_BYTES,
    Dispatcher,
    RpcServer,
    decode_frame,
    encode_frame,
)

HOST_SOURCE = Path(__file__).resolve().parents[2] / "desktop" / "src-tauri" / "src" / "main.rs"
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.application.events import EventJournal
from internal.clipboard.format import ClipboardContent, ContentType
from internal.config.config import Config, config_dir, save
from internal.infrastructure.persistence.instance_lock import DataInUseError, InstanceLock
from internal.security.encryption import make_password_hash


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False))
    application = SidecarApplication()
    application.lifecycle.start()
    yield application
    application.lifecycle.stop()


def request(method, params=None, request_id="req-1"):
    return encode_frame(
        {
            "type": "request",
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
        }
    )


def run_rpc(app, raw):
    output = io.BytesIO()
    status = RpcServer(app, io.BytesIO(raw), output).serve()
    return status, [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type":"request","type":"request"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"nested":{"x":-1e999}}',
        b"[]",
        b"\xff",
        pytest.param(b" " * (MAX_FRAME_BYTES + 1), id="oversized"),
    ],
)
def test_decoder_rejects_invalid_or_unbounded_frames(raw):
    with pytest.raises((ValueError, UnicodeError)):
        decode_frame(raw)


def test_a_result_may_not_claim_the_transports_session():
    """The frame envelope owns two names, and a result is read against them.

    A response's top-level `session_id` is *this* sidecar session: the host
    compares it with the one it started, so a reply from an earlier process
    cannot be mistaken for one from this one, and refuses the frame when it does
    not match.  A top-level `seq` is the host's event cursor.  A command that
    has a session of its own -- a chat conversation, say -- has to name it
    something else, because the one place a claim about the transport belongs is
    the envelope this check protects.
    """
    session = "e1f13c26-1caf-4b03-876c-17d921bd7d49"
    rpc.validate_result({}, session)
    rpc.validate_result({"chat_session_id": "abcdef0123456789"}, session)
    rpc.validate_result({"session_id": session}, session)
    rpc.validate_result({"seq": 0}, session)
    for refused in ({"session_id": "abcdef0123456789"}, {"seq": "later"},
                    {"seq": -1}, {"seq": True}, {"seq": 2 ** 64}):
        with pytest.raises(ApplicationError) as failure:
            rpc.validate_result(refused, session)
        assert failure.value.code == "INVALID_RESULT", refused


def test_a_result_that_is_not_an_object_is_refused():
    """Every command answers with an object, because the host insists on one.

    A list or a scalar is refused there as an invalid frame, which is fatal, so
    a command whose natural answer is a list wraps it: the profile is what lets
    a field be added to a result without every reader learning a new shape.
    `choose_file` and its neighbours look like counter-examples and are not --
    the host owns those dialogs, so they never cross this channel.
    """
    session = "e1f13c26-1caf-4b03-876c-17d921bd7d49"
    for refused in ([], ["a"], "abcdef", 3, True, None):
        with pytest.raises(ApplicationError) as failure:
            rpc.validate_result(refused, session)
        assert failure.value.code == "INVALID_RESULT", refused


def test_a_chat_invite_names_its_conversation_something_else(app, monkeypatch):
    """The conversation id, under a key that is not the transport's.

    `chat.invite` answers with the conversation it opened and used to put it
    under `session_id`, which the host reads as a claim about *this* process's
    session.  A sixteen-character chat id is not a UUID, so the frame was
    refused -- and a refused frame is fatal: the host killed the sidecar and
    restarted it.  That is the whole of what the window showed ("invalid sidecar
    frame", a restart, no messages), and clicking a nearby device was all it
    took.  Neither existing test of this result could catch it: the web panel's
    and the LAN runtime's both call the runtime directly, and the mistake only
    exists at the frame boundary.
    """
    invited = []

    class Runtime:
        session = "abcdef0123456789"

        def chat_invite(self, peer_id, peer_name):
            invited.append((peer_id, peer_name))
            return Runtime.session

    monkeypatch.setattr(app, "runtime", Runtime())
    result = Dispatcher(app).call(
        "chat.invite", {"peer_id": "remote", "peer_name": "Remote"}
    )
    assert invited == [("remote", "Remote")]
    assert result == {"chat_session_id": "abcdef0123456789", "connecting": False}
    rpc.validate_result(result, app.events.session_id)

    # No session yet is a dial still going rather than a refused invite: the
    # runtime publishes `chat.connect_timeout` when the peer never answers, and
    # the page's own poll picks the conversation up once the link is up.  A
    # null under the reserved key would have been refused for the reason another
    # id was -- `{"session_id": null}` is not a session either.
    Runtime.session = None
    waiting = Dispatcher(app).call(
        "chat.invite", {"peer_id": "remote", "peer_name": "Remote"}
    )
    assert waiting == {"chat_session_id": None, "connecting": True}
    rpc.validate_result(waiting, app.events.session_id)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"query": ["bad"]},
        # A chip that names no filter must be refused rather than treated as
        # "all": the window would otherwise show a filter that is not on.
        {"kind": "images"},
        {"sort": "recent"},
        {"kind": None},
        {"unexpected": "x"},
    ],
)
def test_history_rejects_invalid_parameters(app, params):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("history.list", params)


def test_handshake_status_and_shutdown(app):
    status, frames = run_rpc(app, request("app.status") + request("app.shutdown", request_id="q"))
    assert status == 0
    assert frames[0]["type"] == "ready"
    assert frames[0]["protocol"] == 1
    assert frames[1]["result"]["health"] == "ready"
    assert frames[1]["result"]["sync_state"] == "not_started"
    assert frames[2]["result"] == {"accepted": True}


def test_settings_round_trip_and_unknown_field_rejection(app):
    settings = Dispatcher(app).call("settings.get", {})
    assert isinstance(settings["settings"]["device_name"], str)
    result = Dispatcher(app).call(
        "settings.update",
        {
            "device_name": "Desk",
            "appearance_mode": "dark",
            "plain_text_only": True,
            "notifications_enabled": False,
            "notify_transfer": False,
            "history_max_entries": 125,
            "history_max_age_days": 7,
        },
    )
    assert result["ok"] is True
    assert result["updated"]["device_name"] == "Desk"
    assert Dispatcher(app).call("settings.get", {})["settings"]["device_name"] == "Desk"
    assert Dispatcher(app).call("settings.get", {})["settings"]["notify_transfer"] is False
    assert app._repository.MAX_ENTRIES == 125
    assert Dispatcher(app).call("settings.get", {})["settings"]["history_max_age_days"] == 7
    with pytest.raises(ApplicationError):
        Dispatcher(app).call("settings.update", {"history_max_entries": 0})
    with pytest.raises(ApplicationError):
        Dispatcher(app).call("settings.update", {"notify_transfer": "false"})
    with pytest.raises(ApplicationError, match="Unexpected"):
        Dispatcher(app).call("settings.update", {"private_key_pem": "secret"})


def test_notification_switches_round_trip_but_the_unread_one_is_refused(app):
    """The per-type switches the native host reads are settable; the fourth is not.

    ``notify_pairing`` and ``notify_device_connect`` gate the OS notifications the
    Rust host builds itself, so a window that could read them but not set them
    would offer a switch the user cannot move.  ``notify_sync`` stays refused on
    purpose: nothing native reads it, and a control that changes nothing is worse
    than no control.  The web panel keeps its own control through the legacy
    route, which is why the key exists in the config at all.
    """
    rpc = Dispatcher(app)
    assert rpc.call("settings.get", {})["settings"]["notify_pairing"] is True
    result = rpc.call(
        "settings.update",
        {"notify_pairing": False, "notify_device_connect": False},
    )
    assert result["ok"] is True
    settings = rpc.call("settings.get", {})["settings"]
    assert settings["notify_pairing"] is False
    assert settings["notify_device_connect"] is False
    # A later update that does not mention them must not quietly re-default them:
    # the host reads these on every notification, so a reset here is a reset the
    # user never asked for and would not see.
    assert rpc.call("settings.update", {"device_name": "Desk"})["ok"] is True
    settings = rpc.call("settings.get", {})["settings"]
    assert settings["device_name"] == "Desk"
    assert settings["notify_pairing"] is False
    assert settings["notify_device_connect"] is False
    with pytest.raises(ApplicationError, match="Unexpected"):
        rpc.call("settings.update", {"notify_sync": False})


def test_advanced_settings_round_trip(app):
    """Every advanced/network field the legacy web panel exposes is reachable."""
    rpc = Dispatcher(app)
    values = {
        "port": 53318,
        "service_type": "_clipsync._tcp.local.",
        "web_history_limit": 50,
        "sync_debounce": 0.5,
        "clipboard_poll_interval": 2,
        "file_receive_dir": "",
        "transfer_timeout": 300,
        "max_reconnect_attempts": 5,
        "log_level": "DEBUG",
        "low_memory_mode": True,
        "retry_capture_enabled": False,
        "dedup_method": "simple",
        "data_dir": "",
    }
    assert rpc.call("settings.update", values)["ok"] is True
    saved = rpc.call("settings.get", {})["settings"]
    assert all(saved[key] == value for key, value in values.items())
    assert app.config.sync_debounce == 0.5
    assert app.config.dedup_method == "simple"


@pytest.mark.parametrize("values", [
    {"port": 80}, {"port": 70000}, {"port": "53317"},
    {"service_type": ""}, {"service_type": " x "}, {"service_type": "x" * 129},
    {"service_type": "bad\nname"},
    {"web_history_limit": 0}, {"web_history_limit": 501},
    {"sync_debounce": 0}, {"sync_debounce": 11},
    {"clipboard_poll_interval": 0.05}, {"clipboard_poll_interval": 61},
    {"transfer_timeout": 4}, {"transfer_timeout": 3601},
    {"max_reconnect_attempts": -1}, {"max_reconnect_attempts": 101},
    {"log_level": "TRACE"}, {"log_level": "info"},
    {"low_memory_mode": 1}, {"retry_capture_enabled": "true"},
    {"dedup_method": "md5"}, {"data_dir": "x" * 4097},
])
def test_advanced_settings_reject_invalid_values(app, values):
    with pytest.raises(ApplicationError):
        Dispatcher(app).call("settings.update", values)


def test_security_password_round_trip_rewires_live_encryption(app, tmp_path):
    """Set/clear/toggle must rebuild the live manager, not just the config."""
    rpc = Dispatcher(app)
    assert rpc.call("settings.get", {})["settings"]["password_set"] is False
    assert app.identity.encryption is None

    result = rpc.call("settings.update", {"password": "Str0ng-Passw0rd!"})
    assert result["ok"] is True and result["password_set"] is True
    assert app.config.encryption_enabled is True
    assert app.config.encryption_password == "Str0ng-Passw0rd!"
    # Unification: the single password also drives the netpair channel keys.
    assert app.config.netpair_password == "Str0ng-Passw0rd!"
    assert app.identity.encryption is not None
    assert app.identity.encryption._password == "Str0ng-Passw0rd!"
    assert app.config.encryption_password_hash == make_password_hash(
        "Str0ng-Passw0rd!", app.identity.pairing.get_identity().fingerprint
    )
    # The at-rest private key follows the new manager: no plaintext PEM on disk.
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "BEGIN PRIVATE KEY" not in persisted["private_key_pem"]

    # The toggle alone re-wires the runtime without touching the stored hash.
    rpc.call("settings.update", {"encryption_enabled": False})
    assert app.identity.encryption is None
    assert app.config.encryption_password_hash != ""

    cleared = rpc.call("settings.update", {"password": "", "clear_password": True})
    assert cleared["ok"] is True and cleared["password_set"] is False
    assert app.config.encryption_password == ""
    assert app.config.encryption_password_hash == ""
    assert app.config.netpair_password == ""
    assert rpc.call("settings.get", {})["settings"]["password_set"] is False


@pytest.mark.parametrize("values", [
    {"password": "short"},
    {"password": "alllowercase123!"},
    {"password": "ALLUPPERCASE123!"},
    {"password": "NoDigitsHere!!"},
    {"password": "NoSpecials12345"},
    {"password": 123},
    {"clear_password": False},
    {"encryption_enabled": "true"},
])
def test_security_settings_reject_invalid_values(app, values):
    with pytest.raises(ApplicationError):
        Dispatcher(app).call("settings.update", values)
    # A rejected password must not leave a half-applied encryption state.
    assert app.config.encryption_enabled is False
    assert app.config.encryption_password == ""


def test_factory_reset_deletes_data_and_arms_the_web_markers(app, tmp_path):
    """The reset clears every user-data file, stops the services, and reports."""
    app._repository.add(ClipboardContent(types={ContentType.TEXT: b"secret"}))
    (tmp_path / "favorites.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.json.corrupt-1700000000").write_text("{}", encoding="utf-8")
    (tmp_path / "clipsync.log").write_text("log", encoding="utf-8")

    result = Dispatcher(app).call("app.factory_reset", {})
    assert result["ok"] is True and result["factory_reset"] is True and result["deleted"] >= 1
    for name in (
        "config.json", "clipboard_history.db", "clipboard_history.db-wal",
        "clipboard_history.db-shm", "favorites.json", "clipsync.log",
    ):
        assert not (tmp_path / name).exists(), f"{name} survived the factory reset"
    assert not (tmp_path / "config.json.corrupt-1700000000").exists()
    # Browser state lives outside the data dir: the markers make the web page
    # clear its own localStorage on the next load.
    assert (tmp_path / "factory_reset_pending").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "web_fresh_pending").read_text(encoding="utf-8") == "1"
    # Services are down; the host is expected to relaunch the app.
    assert app.lifecycle.state == "stopped"


def test_history_limit_update_prunes_on_capture_and_preserves_pinned(app):
    history = app._repository
    for index in range(15):
        history.add(ClipboardContent(types={ContentType.TEXT: f"entry-{index}".encode()}))
    oldest_id = history.get_all()[-1]["entry_id"]
    assert history.batch_set_pinned([oldest_id], True) == 1
    Dispatcher(app).call("settings.update", {"history_max_entries": 10})
    history.add(ClipboardContent(types={ContentType.TEXT: b"after limit update"}))
    rows = history.get_all()
    assert len(rows) == 10
    assert any(row["entry_id"] == oldest_id and row["pinned"] for row in rows)
    assert history._get_conn().execute("SELECT COUNT(*) FROM history").fetchone()[0] == 10


def test_translation_service_address_round_trip(app):
    dispatcher = Dispatcher(app)
    dispatcher.call("settings.update", {"translate_url": "http://127.0.0.1:5000/translate"})
    assert dispatcher.call("settings.get", {})["settings"]["translate_url"] == "http://127.0.0.1:5000/translate"
    dispatcher.call("settings.update", {"translate_url": ""})
    assert dispatcher.call("settings.get", {})["settings"]["translate_url"] == ""
    for value in ["file:///private", "javascript:alert(1)", "x" * 2049]:
        with pytest.raises(ApplicationError):
            dispatcher.call("settings.update", {"translate_url": value})


def test_filter_categories_preserve_explicit_empty_selection(app):
    rpc = Dispatcher(app)
    rpc.call("settings.update", {"filter_enabled_categories": ["email", "api_key"]})
    assert rpc.call("settings.get", {})["settings"]["filter_enabled_categories"] == [
        "email", "api_key",
    ]
    rpc.call("settings.update", {"filter_enabled_categories": []})
    assert app.config.filter_enabled_categories == []
    for value in [["unknown"], [True], "email"]:
        with pytest.raises(ApplicationError):
            rpc.call("settings.update", {"filter_enabled_categories": value})


def test_app_filter_settings_round_trip_and_matching(app):
    from internal.clipboard.source_tracker import is_app_allowed

    rpc = Dispatcher(app)
    values = {
        "app_filter_enabled": True,
        "app_filter_mode": "blacklist",
        "app_filter_list": ["secret*", "CHROME.EXE"],
    }
    rpc.call("settings.update", values)
    saved = rpc.call("settings.get", {})["settings"]
    assert all(saved[key] == value for key, value in values.items())
    assert not is_app_allowed({"process": "Secret.exe"}, app.config)
    assert is_app_allowed({"process": "editor.exe"}, app.config)
    rpc.call("settings.update", {"app_filter_mode": "whitelist"})
    assert is_app_allowed({"process": "chrome.exe"}, app.config)
    assert not is_app_allowed({"process": "editor.exe"}, app.config)
    rpc.call("settings.update", {"app_filter_enabled": False})
    assert is_app_allowed({"process": "editor.exe"}, app.config)


@pytest.mark.parametrize("values", [
    {"app_filter_enabled": 1}, {"app_filter_mode": "other"},
    {"app_filter_list": "chrome.exe"}, {"app_filter_list": [True]},
    {"app_filter_list": [""]}, {"app_filter_list": [" chrome.exe"]},
    {"app_filter_list": ["bad\nname"]}, {"app_filter_list": ["bad\0name"]},
    {"app_filter_list": ["x" * 261]}, {"app_filter_list": ["x"] * 257},
])
def test_app_filter_rejects_invalid_settings(app, values):
    with pytest.raises(ApplicationError):
        Dispatcher(app).call("settings.update", values)


def test_backup_restore_reports_malformed_history_instead_of_success(app, tmp_path):
    import zipfile

    backup = tmp_path / "invalid-history.zip"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("history.json", "{invalid-json")
    before = app._repository.get_all()
    with pytest.raises(ApplicationError) as error:
        app.restore_backup(str(backup))
    assert error.value.code == "RESTORE_PARTIAL_FAILED"
    assert "history" in str(error.value)
    assert app._repository.get_all() == before


def test_real_backup_round_trip_restores_history_content_and_pin(app):
    history = app._repository
    history.add(ClipboardContent(types={ContentType.TEXT: b"backup round trip"}))
    entry_id = history.get_all()[0]["entry_id"]
    assert history.batch_set_pinned([entry_id], True) == 1
    path = app.create_backup()["backup_path"]
    assert Path(path).is_file()
    assert history.batch_delete([entry_id]) == 1
    assert history.get_all() == []
    result = app.restore_backup(path)
    assert result["history"] == 1
    assert not result.get("errors")
    rows = history.get_all()
    assert len(rows) == 1
    assert rows[0]["pinned"] is True
    assert rows[0]["text_preview"] == "backup round trip"


def test_restored_configuration_is_persisted(app, tmp_path):
    import zipfile

    from internal.config.config import load

    archive_path = tmp_path / "config-restore.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("config.json", json.dumps({
            "device_name": "Restored name", "history_max_entries": 125,
        }))
    assert app.restore_backup(str(archive_path))["config"] is True
    assert app.config.device_name == "Restored name"
    assert load().device_name == "Restored name"
    assert app._repository.MAX_ENTRIES == 125


def test_restore_configuration_save_failure_is_not_reported_as_success(app, tmp_path, monkeypatch):
    import zipfile

    archive_path = tmp_path / "config-save-failure.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("config.json", json.dumps({"device_name": "Restored name"}))
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr("internal.application.bootstrap.save", fail)
    with pytest.raises(ApplicationError) as error:
        app.restore_backup(str(archive_path))
    assert error.value.code == "RESTORE_PARTIAL_FAILED"
    assert "config persistence" in str(error.value)


def test_translation_key_update_clear_and_failed_save(app, monkeypatch):
    dispatcher = Dispatcher(app)
    result = dispatcher.call("settings.update", {"set_translate_key": " secret-test-key "})
    assert result == {"ok": True, "translate_key_set": True}
    assert app.config.translate_api_key == "secret-test-key"
    assert "secret-test-key" not in json.dumps(dispatcher.call("settings.get", {}))
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("disk failed")
        patch.setattr("internal.application.bootstrap.save", fail)
        with pytest.raises(ApplicationError, match="Could not save"):
            dispatcher.call("settings.update", {"clear_translate_key": True})
        assert app.config.translate_api_key == "secret-test-key"
    with pytest.raises(ApplicationError):
        dispatcher.call("settings.update", {"set_translate_key": "new", "device_name": "Other"})
    assert dispatcher.call("settings.update", {"clear_translate_key": True}) == {
        "ok": True, "translate_key_set": False,
    }
    assert app.config.translate_api_key == ""


@pytest.mark.parametrize("service_reply", [
    {"translatedText": "translated fixture"}, [], {}, {"translatedText": 42},
])
def test_translation_rpc_calls_configured_http_service(app, service_reply):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            body = json.dumps(service_reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        dispatcher = Dispatcher(app)
        dispatcher.call("settings.update", {
            "translate_url": f"http://127.0.0.1:{server.server_port}/translate",
        })
        dispatcher.call("settings.update", {"set_translate_key": "fixture-key"})
        params = {"text": "fixture text", "source_lang": "en", "target_lang": "zh"}
        if isinstance(service_reply, dict) and isinstance(service_reply.get("translatedText"), str):
            assert dispatcher.call("translate.text", params)["translated"] == "translated fixture"
        else:
            with pytest.raises(ApplicationError, match="Invalid response"):
                dispatcher.call("translate.text", params)
        assert received == [{
            "path": "/translate",
            "authorization": "Bearer fixture-key",
            "body": {"q": "fixture text", "source": "en", "target": "zh", "format": "text"},
        }]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.mark.parametrize("value", [True, False, -1, 36501, float("nan"), float("inf"), "7"])
def test_history_age_rejects_invalid_values(app, value):
    with pytest.raises(ApplicationError):
        Dispatcher(app).call("settings.update", {"history_max_age_days": value})


@pytest.mark.parametrize("restart", [False, True])
def test_history_age_prunes_expired_rows_but_preserves_pins(app, restart):
    import time

    history = app._repository
    history.add(ClipboardContent(types={ContentType.TEXT: b"expired ordinary"}))
    history.add(ClipboardContent(types={ContentType.TEXT: b"expired pinned"}))
    pinned_id = history.get_all()[0]["entry_id"]
    history.batch_set_pinned([pinned_id], True)
    conn = history._get_conn()
    with conn:
        conn.execute("UPDATE history SET timestamp = ?", (time.time() - 3 * 86400,))
    Dispatcher(app).call("settings.update", {"history_max_age_days": 1})
    reopened = None
    if restart:
        assert app.lifecycle.stop()
        reopened = SidecarApplication()
        reopened.lifecycle.start()
        history = reopened._repository
    else:
        history.add(ClipboardContent(types={ContentType.TEXT: b"fresh capture"}))
    try:
        rows = history.get_all()
        assert len(rows) == (1 if restart else 2)
        assert any(row["entry_id"] == pinned_id and row["pinned"] for row in rows)
        counted = history._get_conn().execute("SELECT COUNT(*) FROM history").fetchone()[0]
        assert counted == len(rows)
    finally:
        if reopened:
            reopened.lifecycle.stop()


def test_transfer_commands_require_runtime_and_validate_arguments(app):
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("transfers.list", {})


def test_cancel_all_takes_no_parameters_and_requires_runtime(app):
    # Cancels every row at once, so there is no id to pass and none to validate.
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("transfers.cancel_all", {"transfer_id": "t1"})
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("transfers.cancel_all", {})


def test_cancel_all_publishes_one_event_for_the_whole_sweep(app, monkeypatch):
    from types import SimpleNamespace

    published = []

    class Events:
        def publish(self, name, payload):
            published.append((name, payload))

    monkeypatch.setattr(
        app, "runtime", SimpleNamespace(cancel_all_transfers=lambda: 3, stop=lambda: True)
    )
    monkeypatch.setattr(app, "events", Events())
    assert Dispatcher(app).call("transfers.cancel_all", {}) == {"cancelled": 3}
    # One event, not one per row: the window re-reads the list, so a row it never
    # saw is covered by the same publish.
    assert published == [("transfers.changed", {"cancelled": 3})]


def test_clear_transfer_history_takes_no_parameters_and_requires_runtime(app):
    # The records are the sidecar's own list, so there is no id to pass and none
    # to validate — the same shape as clearing the whole clipboard history.
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("transfers.clear_history", {"transfer_id": "t1"})
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("transfers.clear_history", {})


def test_clear_transfer_history_reports_the_count_it_deleted(app, monkeypatch):
    from types import SimpleNamespace

    published = []

    class Events:
        def publish(self, name, payload):
            published.append((name, payload))

    monkeypatch.setattr(
        app, "runtime", SimpleNamespace(clear_transfer_history=lambda: 4, stop=lambda: True)
    )
    monkeypatch.setattr(app, "events", Events())
    assert Dispatcher(app).call("transfers.clear_history", {}) == {"cleared": 4}
    assert published == [("transfers.changed", {"cleared": 4})]


@pytest.mark.parametrize(
    "method",
    [
        "devices.connect", "devices.disconnect", "devices.forget",
        "devices.restore", "devices.purge", "devices.test", "devices.retrust",
    ],
)
def test_device_commands_validate_arguments_and_require_runtime(app, method):
    for params in ({}, {"device_id": ""}, {"device_id": "x" * 129}, {"device_id": 7}):
        with pytest.raises(ApplicationError, match="parameter"):
            Dispatcher(app).call(method, params)
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call(method, {"device_id": "peer"})


def test_device_certs_takes_no_parameters_and_requires_runtime(app):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("devices.certs", {"device_id": "peer"})
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("devices.certs", {})


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"device_id": "peer"},
        {"url": "https://example.com"},
        {"device_id": "", "url": "https://example.com"},
        {"device_id": "x" * 129, "url": "https://example.com"},
        {"device_id": 7, "url": "https://example.com"},
        {"device_id": "peer", "url": ""},
        {"device_id": "peer", "url": "https://example.com/" + "x" * 2048},
        {"device_id": "peer", "url": 7},
        {"device_id": "peer", "url": "https://example.com", "extra": 1},
    ],
)
def test_send_url_validates_arguments_and_requires_runtime(app, params):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("url.send", params)
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("url.send", {"device_id": "peer", "url": "https://example.com"})


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"text": ""},
        {"text": 7},
        {"text": None},
        pytest.param({"text": "x" * 100001}, id="too-long"),
        {"text": "hello", "extra": 1},
    ],
)
def test_clipboard_push_validates_arguments_and_requires_runtime(app, params):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("clipboard.push", params)
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("clipboard.push", {"text": "hello"})


def test_discovery_commands_take_exact_parameters_and_require_runtime(app):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("discovery.status", {"enabled": True})
    with pytest.raises(ApplicationError, match="LAN runtime"):
        Dispatcher(app).call("discovery.status", {})
    for method in ("discovery.set_enabled", "discovery.set_visible"):
        for params in ({}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": True, "extra": 1}):
            with pytest.raises(ApplicationError, match="parameter"):
                Dispatcher(app).call(method, params)
        with pytest.raises(ApplicationError, match="LAN runtime"):
            Dispatcher(app).call(method, {"enabled": True})


@pytest.mark.parametrize(
    "params",
    [
        {"lines": 0},
        {"lines": 1001},
        {"lines": "5"},
        {"lines": True},
        {"lines": 200, "extra": 1},
    ],
)
def test_logs_tail_validates_its_line_count(app, params):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("logs.tail", params)


def test_diagnostics_report_takes_no_parameters(app):
    for params in ({"action": "firewall"}, {"lines": 1}):
        with pytest.raises(ApplicationError, match="parameter"):
            Dispatcher(app).call("diagnostics.report", params)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"action": ""},
        {"action": "Firewall"},
        {"action": "permissions"},
        {"action": 1},
        {"action": True},
        {"action": "firewall", "extra": 1},
    ],
)
def test_diagnostics_request_validates_its_action(app, params):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("diagnostics.request", params)


@pytest.mark.parametrize("method", [
    "update.check", "update.status", "update.download", "update.open_folder",
])
def test_update_commands_take_no_parameters(app, method):
    for params in ({"action": "download"}, {"force": True}):
        with pytest.raises(ApplicationError, match="parameter"):
            Dispatcher(app).call(method, params)


def test_update_status_starts_idle(app):
    assert Dispatcher(app).call("update.status", {}) == {
        "state": {
            "phase": "idle", "fraction": 0, "downloaded": 0, "total": 0,
            "error": "", "version": "", "path": "",
        }
    }


def test_auto_update_check_round_trips_through_settings(app):
    dispatcher = Dispatcher(app)
    assert dispatcher.call("settings.get", {})["settings"]["auto_update_check"] is True
    dispatcher.call("settings.update", {"auto_update_check": False})
    assert app.config.auto_update_check is False
    assert dispatcher.call("settings.get", {})["settings"]["auto_update_check"] is False
    with pytest.raises(ApplicationError, match="parameter"):
        dispatcher.call("settings.update", {"auto_update_check": "false"})


def test_ai_local_commands_reject_unknown_actions_and_incomplete_saves(app):
    with pytest.raises(ApplicationError, match="Unknown AI config action"):
        Dispatcher(app).call("ai.local.delete", {})
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call(
            "ai.local.save",
            {"tool": "codex", "rel_path": "config.toml"},
        )


@pytest.mark.parametrize(
    "method,params",
    [
        ("ai.preview", {"peer_id": "", "tool": "codex", "rel_path": "config.toml"}),
        ("ai.preview", {"peer_id": "peer", "tool": "", "rel_path": "config.toml"}),
        ("ai.pull", {"peer_id": "peer", "items": [], "mode": "copy"}),
        ("ai.pull", {"peer_id": "peer", "items": [{}], "mode": "replace"}),
        ("ai.inventory", {"refresh": "yes", "peer_id": ""}),
    ],
)
def test_ai_commands_reject_invalid_parameters(app, method, params):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call(method, params)
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("transfers.send", {"paths": []})
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("transfers.send", {"paths": ["/tmp/a.bin"], "device_id": 7})
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("transfers.send", {"paths": [""]})
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("transfers.action", {"action": "explode", "transfer_id": "x"})


def test_history_dtos_do_not_expose_raw_payloads_or_paths(app):
    app._repository.add(ClipboardContent(types={ContentType.TEXT: b"sample"}))
    dispatcher = Dispatcher(app)
    page = dispatcher.call("history.list", {})
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert set(item) == {
        "id", "preview", "timestamp", "content_type", "pinned",
        # Provenance the legacy row showed: which device it synced from, the
        # application it was copied in, that window's title, and the count. None
        # of it is a stored payload or a filesystem path — the title is what the
        # legacy panel and the phone were already given.
        "source_name", "source_app", "source_title", "paste_count",
        # ...and which link carried it (or, for a push from the panel, that it
        # came in over this machine's own web server), which is a word this
        # build records rather than anything the entry stores.
        "transport",
    }
    assert item["preview"] == "sample"
    assert page["session_id"] == app.events.session_id
    assert dispatcher.call("history.set_pinned", {"entry_id": item["id"], "pinned": True})
    assert dispatcher.call("history.list", {})["items"][0]["pinned"] is True
    assert dispatcher.call("history.delete", {"entry_id": item["id"]})
    assert dispatcher.call("history.list", {})["total"] == 0
    with pytest.raises(ApplicationError) as exc:
        dispatcher.call("history.delete", {"entry_id": item["id"]})
    assert exc.value.code == "NOT_FOUND"


def test_reading_a_clip_returns_its_text_and_not_its_stored_payloads(app):
    # The read exists so a window can show or translate a whole clip without
    # copying it, so the text comes back as words rather than as the stored
    # base64 bag — and nothing else about the row crosses with it.
    app._repository.add(
        ClipboardContent(types={ContentType.TEXT: b"sample", ContentType.HTML: b"<b>sample</b>"})
    )
    dispatcher = Dispatcher(app)
    item = dispatcher.call("history.list", {})["items"][0]
    assert dispatcher.call("history.text", {"entry_id": item["id"]}) == {
        "id": item["id"], "text": "sample", "truncated": False,
    }
    with pytest.raises(ApplicationError) as exc:
        dispatcher.call("history.text", {"entry_id": "gone"})
    assert exc.value.code == "NOT_FOUND"
    with pytest.raises(ApplicationError, match="parameter"):
        dispatcher.call("history.text", {})


def test_opening_a_clip_opens_the_row_and_takes_no_url_from_the_caller(app):
    # The route is the whole security posture of "open this link": the window
    # names a row, and only the row's own text can reach the browser.
    app._repository.add(ClipboardContent(types={ContentType.TEXT: b"https://example.com/page"}))
    dispatcher = Dispatcher(app)
    item = dispatcher.call("history.list", {})["items"][0]
    opened = []

    def record(url):
        opened.append(url)
        return (True, url)

    app.history._open_url = record
    assert dispatcher.call("history.open_link", {"entry_id": item["id"]}) == {
        "opened": True, "url": "https://example.com/page",
    }
    assert opened == ["https://example.com/page"]
    with pytest.raises(ApplicationError) as exc:
        dispatcher.call("history.open_link", {"entry_id": "gone"})
    assert exc.value.code == "NOT_FOUND"
    with pytest.raises(ApplicationError, match="parameter"):
        dispatcher.call("history.open_link", {})
    with pytest.raises(ApplicationError, match="parameter"):
        # A URL parameter is not part of this route, so a caller cannot smuggle
        # one in beside the id.
        dispatcher.call("history.open_link", {"entry_id": item["id"], "url": "https://evil.example"})


def test_clear_history_removes_every_entry_and_publishes_the_change(app):
    for text in (b"one", b"two", b"three"):
        app._repository.add(ClipboardContent(types={ContentType.TEXT: text}))
    dispatcher = Dispatcher(app)
    assert dispatcher.call("history.clear", {}) == {"cleared": 3}
    assert dispatcher.call("history.list", {})["total"] == 0
    assert dispatcher.call("history.clear", {}) == {"cleared": 0}
    events, _ = app.events.since(0)
    assert [event["name"] for event in events].count("history.changed") == 2


def test_clear_history_rejects_parameters(app):
    with pytest.raises(ApplicationError, match="parameter"):
        Dispatcher(app).call("history.clear", {"entry_id": "x"})


@pytest.mark.parametrize("ids", [[], ["a", "a"], [True], [{}], [""], ["a"] * 101])
def test_batch_history_rejects_invalid_ids(app, ids):
    with pytest.raises(ApplicationError) as error:
        Dispatcher(app).call("history.batch_delete", {"entry_ids": ids})
    assert error.value.code == "VALIDATION_ERROR"


def test_batch_history_operates_by_stable_id_and_reports_missing_as_unmatched(app):
    for text in (b"one", b"two"):
        app._repository.add(ClipboardContent(types={ContentType.TEXT: text}))
    dispatcher = Dispatcher(app)
    ids = [item["id"] for item in dispatcher.call("history.list", {})["items"]]
    assert len(ids) == 2
    assert dispatcher.call(
        "history.batch_set_pinned", {"entry_ids": ids, "pinned": True}
    ) == {"updated": 2}
    assert all(item["pinned"] for item in dispatcher.call("history.list", {})["items"])
    assert dispatcher.call(
        "history.batch_delete", {"entry_ids": [ids[0], "missing"]}
    ) == {"deleted": 1}
    remaining = dispatcher.call("history.list", {})["items"]
    assert [item["id"] for item in remaining] == [ids[1]]


def test_unknown_method_and_duplicate_ids_do_not_execute(app):
    _, frames = run_rpc(app, request("arbitrary.method") + request("app.shutdown"))
    assert frames[1]["error"]["code"] == "METHOD_NOT_FOUND"
    assert frames[2]["error"]["code"] == "DUPLICATE_ID"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type":"request"}\n',
        b"{}\n",
        b'{"unterminated"',
        pytest.param(b"x" * (MAX_FRAME_BYTES + 2), id="oversized"),
    ],
)
def test_protocol_errors_end_the_session(app, raw):
    status, frames = run_rpc(app, raw)
    assert status == 2
    assert frames[-1]["type"] == "fatal"
    assert frames[-1]["error"]["code"] == "PROTOCOL_ERROR"


def test_internal_errors_do_not_disclose_sensitive_exception(app, monkeypatch, caplog):
    def fail():
        raise RuntimeError("secret-password")

    monkeypatch.setattr(app, "devices", fail)
    _, frames = run_rpc(app, request("devices.list"))
    assert frames[1]["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-password" not in json.dumps(frames)
    assert "secret-password" not in caplog.text


def test_replay_gap_is_detected():
    events = EventJournal(capacity=2)
    for index in range(3):
        events.publish("history.changed", {"id": str(index)})
    replay, gap = events.since(0)
    assert gap is True
    assert [event["seq"] for event in replay] == [2, 3]
    assert events.snapshot(lambda: {"total": 3})["seq"] == 3


def test_process_exits_on_eof_and_releases_ownership(tmp_path):
    environment = {**os.environ, "CLIPSYNC_CONFIG_DIR": str(tmp_path)}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-u", "-m", "src.sidecar_main", "--history-only"],
            input=request("app.status"),
            capture_output=True,
            timeout=15,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        assert [frame["type"] for frame in frames] == ["ready", "response"]


def test_no_tk_import_in_sidecar_process(tmp_path):
    code = (
        "import sys; from internal.application.bootstrap import SidecarApplication; "
        "a=SidecarApplication(); a.lifecycle.start(); "
        "assert 'tkinter' not in sys.modules; assert 'src.main' not in sys.modules; "
        "a.lifecycle.stop()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=15,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "CLIPSYNC_CONFIG_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_two_instances_cannot_open_same_data(app):
    other = SidecarApplication()
    with pytest.raises(ApplicationError) as exc:
        other.lifecycle.start()
    assert exc.value.code == "DATA_IN_USE"
    other.lifecycle.stop()
    assert app.lifecycle.state == "running"


def test_legacy_lock_is_not_removed(tmp_path):
    lock = tmp_path / ".lock"
    lock.write_text('{"pid":123}', encoding="utf-8")
    instance = InstanceLock(tmp_path)
    with pytest.raises(DataInUseError):
        instance.start()
    instance.stop()
    assert lock.read_text(encoding="utf-8") == '{"pid":123}'


def test_sidecar_marker_is_visible_to_legacy_and_removed_on_stop(tmp_path):
    instance = InstanceLock(tmp_path)
    instance.start()
    try:
        marker = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
        assert marker["pid"] == os.getpid()
        assert marker["owner"] == "tauri-sidecar-v1"
    finally:
        instance.stop()
    assert not (tmp_path / ".lock").exists()


def test_crashed_sidecar_marker_is_recovered_only_after_os_lock(tmp_path):
    (tmp_path / ".lock").write_text(
        json.dumps({"pid": 0, "owner": "tauri-sidecar-v1", "token": "stale"}),
        encoding="utf-8",
    )
    instance = InstanceLock(tmp_path)
    instance.start()
    instance.stop()
    assert not (tmp_path / ".lock").exists()


def test_stop_does_not_remove_another_owners_marker(tmp_path):
    instance = InstanceLock(tmp_path)
    instance.start()
    (tmp_path / ".lock").write_text('{"pid":123}', encoding="utf-8")
    instance.stop()
    assert (tmp_path / ".lock").read_text(encoding="utf-8") == '{"pid":123}'


def test_corrupt_database_is_not_quarantined_or_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "clipboard_history.db"
    path.write_bytes(b"not a database")
    application = SidecarApplication()
    with pytest.raises(ApplicationError) as exc:
        application.lifecycle.start()
    assert exc.value.code == "DATA_INVALID"
    assert path.read_bytes() == b"not a database"
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_unlock_does_not_open_history_with_wrong_password(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=True, encryption_password_hash=make_password_hash("test", "")))
    application = SidecarApplication()
    application.lifecycle.start()
    try:
        assert application.status()["health"] == "locked"
        assert not (tmp_path / "clipboard_history.db").exists()
        with pytest.raises(ApplicationError) as exc:
            application.unlock("wrong")
        assert exc.value.code == "INVALID_PASSWORD"
        assert application.history is None
        assert application.unlock("test") == {"unlocked": True}
    finally:
        application.lifecycle.stop()


def test_config_override_must_be_absolute(monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", "relative")
    with pytest.raises(ValueError):
        config_dir()


@contextmanager
def live_rpc(app):
    read_fd, write_fd = os.pipe()
    frames = queue.Queue()
    outcome = queue.Queue()

    class Output:
        def write(self, raw):
            frames.put(decode_frame(raw.rstrip(b"\n")))
            return len(raw)

        def flush(self):
            pass

    reader = os.fdopen(read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    server = RpcServer(app, reader, Output())

    def serve():
        try:
            outcome.put(server.serve())
        except Exception as exc:
            outcome.put(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert frames.get(timeout=3)["type"] == "ready"
        yield writer, frames, outcome
    finally:
        writer.close()
        thread.join(timeout=5)
        reader.close()
        assert not thread.is_alive()


def test_autonomous_event_arrives_while_stdin_is_idle(app):
    with live_rpc(app) as (writer, frames, outcome):
        app.events.publish("device.changed", {"id": "peer"})
        event = frames.get(timeout=3)
        assert event["type"] == "event"
        assert event["data"] == {"id": "peer"}
        assert event["seq"] == 1
        writer.write(request("app.shutdown"))
        assert frames.get(timeout=3)["result"] == {"accepted": True}
        # Shutdown must finish even though the parent keeps stdin open.
        assert outcome.get(timeout=3) == 0


def test_mutation_response_precedes_its_event(app):
    app._repository.add(ClipboardContent(types={ContentType.TEXT: b"sample"}))
    entry_id = Dispatcher(app).call("history.list", {})["items"][0]["id"]
    with live_rpc(app) as (writer, frames, _):
        writer.write(request("history.set_pinned", {"entry_id": entry_id, "pinned": True}))
        assert frames.get(timeout=3)["type"] == "response"
        event = frames.get(timeout=3)
        assert event["name"] == "history.changed"
        assert event["data"]["id"] == entry_id


def test_idle_replay_overflow_requests_resync_then_preserves_event_order(app):
    app.events = EventJournal(capacity=2)
    for index in range(5):
        app.events.publish("device.changed", {"id": str(index)})
    with live_rpc(app) as (_, frames, _):
        assert frames.get(timeout=3)["type"] == "resync"
        assert [frames.get(timeout=3)["seq"] for _ in range(2)] == [4, 5]


def test_oversized_notification_does_not_kill_rpc(app):
    app.events.publish("device.changed", {"value": "x" * MAX_FRAME_BYTES})
    app.events.publish("device.changed", {"id": "valid"})
    with live_rpc(app) as (writer, frames, _):
        assert frames.get(timeout=3)["type"] == "resync"
        assert frames.get(timeout=3)["data"] == {"id": "valid"}
        writer.write(request("app.status"))
        assert frames.get(timeout=3)["result"]["health"] == "ready"


def test_output_disconnect_is_not_misreported_as_command_failure(app):
    class BrokenOutput:
        calls = 0

        def write(self, raw):
            self.calls += 1
            if self.calls > 1:
                raise BrokenPipeError

        def flush(self):
            pass

    output = BrokenOutput()
    with pytest.raises(BrokenPipeError):
        RpcServer(app, io.BytesIO(request("app.status")), output).serve()
    assert output.calls == 2


def dispatched_methods() -> tuple[set[str], tuple[str, ...]]:
    """The method names the dispatcher can route, read out of its own source.

    Read rather than kept beside it, for the reason the desktop's Rust test
    reads ``main.rs`` the same way: a hand-kept list has to be updated with the
    dispatcher, and that is exactly the copy that drifts.  The shapes it knows
    are the three the dispatcher uses -- ``method == "x"``, ``method in (...)``,
    a dispatch table's keys -- plus the families it routes by prefix, which
    count by their prefix, and the favorites table, which lives in its own
    module.
    """
    source = Path(rpc.__file__).read_text(encoding="utf-8")
    found = set(re.findall(r'method == "([a-z][a-z0-9_.]*)"', source))
    for group in re.findall(r"method in \(([^)]*)\)", source):
        found |= set(re.findall(r'"([a-z][a-z0-9_.]*)"', group))
    found |= set(re.findall(r'"([a-z][a-z0-9_]*\.[a-z0-9_]+)"\s*:', source))
    prefixes = tuple(re.findall(r'method\.startswith\("([a-z][a-z0-9_.]*)"', source))
    favorites_source = Path(favorites.__file__).read_text(encoding="utf-8")
    found |= set(re.findall(r'"([a-z][a-z0-9_]*\.[a-z0-9_]+)"', favorites_source))
    return found, prefixes


def test_every_advertised_capability_names_a_method_the_dispatcher_routes(app):
    """``status``'s capability list is a contract with the window.

    The shell turns some of these strings into controls it enables and disables,
    so a name with no method behind it is a control that can never come alive --
    the same defect as a hidden control, one layer down.  The list and the
    dispatcher are two copies of one fact, and this reads both instead of
    trusting either: `chat.resend` was advertised for a while while resending
    was a verb of ``chat.action``, and nothing could answer the name.
    """
    capabilities = app.status()["capabilities"]
    # Guard the reading below: an extractor that matched nothing would make the
    # comparison vacuous.  This app has no runtime up, so the list is the half
    # that answers without the engine; the other half arrives with the engine
    # and is covered by `test_application_runtime.py` and by the split test
    # below, which reads both halves out of the source.
    assert len(capabilities) > 30, f"only {len(capabilities)} capabilities"
    dispatched, prefixes = dispatched_methods()
    assert len(dispatched) > 80, f"only {len(dispatched)} dispatched methods read"
    unrouted = [
        name
        for name in capabilities
        if name not in dispatched and not name.startswith(prefixes)
    ]
    assert not unrouted, f"advertised but not routed by the dispatcher: {unrouted}"

def capability_blocks() -> tuple[set[str], set[str]]:
    """The two halves of the advertised capability list, read from its source.

    ``status`` answers with the first block while the sync engine is down and
    with both while it is up, so the second block is the set of abilities the
    sidecar itself says cannot answer without the engine.  Reading the source
    keeps this beside the naming test above, which is about the same list; the
    live list is compared against the first block below, so a parse that had
    drifted from the code could not pass quietly.
    """
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")
    block = source[source.index('"capabilities": ['):source.index("def read_logs")]
    always, _, while_running = block.partition("] + ([")
    pattern = r'"([a-z][a-z0-9_]*\.[a-z0-9_.]+)"'
    return (set(re.findall(pattern, always)), set(re.findall(pattern, while_running)))


def dispatch_blocks(source: str) -> list[tuple[list[str], list[str], list[str], str]]:
    """Every ``if method ...:`` block in a dispatcher module.

    Each entry is the method names the block's own test names, the prefix
    families it matches, the tables it dispatches by name, and its body.  Three
    shapes are read: the names a comparison lists (``==``, ``!=``,
    ``in (...)``), the families matched by ``startswith``, and a branch that
    dispatches a table, whose keys :func:`dispatch_table` resolves separately.
    """
    lines = source.split("\n")
    blocks = []
    for index, line in enumerate(lines):
        comparison = re.match(r"\s*(?:if|elif) method (?:==|!=|in) (.+?):", line)
        family = re.match(
            r'\s*(?:if|elif) method\.startswith\("([a-z][a-z0-9_.]*)"\)', line
        )
        if not comparison and not family:
            continue
        indent = len(line) - len(line.lstrip())
        body = []
        for later in lines[index + 1:]:
            if later.strip() and (len(later) - len(later.lstrip())) <= indent:
                break
            body.append(later)
        test = comparison.group(1) if comparison else ""
        blocks.append((
            re.findall(r'"([a-z][a-z0-9_.]*)"', test),
            [family.group(1)] if family else [],
            re.findall(r"\b([a-z_]+_commands)\b", test),
            "\n".join(body),
        ))
    return blocks


def dispatch_table(source: str, name: str) -> str:
    """The variable of the dispatch table that carries this method name."""
    for start in re.finditer(r"\n(\s*)([a-z_]+) = \{\n", source):
        tail = source[start.end():]
        end = re.search(r"\n\s*\}", tail)
        body = tail[:end.start()] if end else tail
        if re.search('"' + re.escape(name) + r'"\s*:', body):
            return start.group(2)
    return ""


def routing_bodies(name: str) -> list[str]:
    """Every dispatch body that can answer this method, from both modules."""
    rpc_source = Path(rpc.__file__).read_text(encoding="utf-8")
    favorites_source = Path(favorites.__file__).read_text(encoding="utf-8")
    blocks = dispatch_blocks(rpc_source) + dispatch_blocks(favorites_source)
    bodies = []
    for names, families, _, body in blocks:
        if name in names or any(name.startswith(family.rstrip(".") + ".")
                                for family in families):
            bodies.append(body)
    table = dispatch_table(rpc_source, name)
    if table:
        for _, _, tables, body in blocks:
            if table in tables:
                bodies.append(body)
    return bodies


def application_methods() -> dict[str, str]:
    """Every application method's body, by name, across the application layer."""
    bodies = {}
    for path in sorted((Path(bootstrap.__file__).parent).rglob("*.py")):
        lines = path.read_text(encoding="utf-8").split("\n")
        for index, line in enumerate(lines):
            definition = re.match(r"    def ([a-z_][a-z0-9_]*)\(", line)
            if not definition or definition.group(1) in bodies:
                continue
            body = []
            for later in lines[index + 1:]:
                if re.match(r"    def ", later):
                    break
                body.append(later)
            bodies[definition.group(1)] = "\n".join(body)
    return bodies


APPLICATION_METHODS = application_methods()
RUNTIME_REQUIREMENTS = ("require_runtime()", "require_internet_pairing()")


def needs_the_runtime(text: str, depth: int = 1) -> bool:
    """Whether a dispatch body, one level into the application layer, asks for the engine."""
    if any(word in text for word in RUNTIME_REQUIREMENTS):
        return True
    if depth <= 0:
        return False
    for called in set(re.findall(r"self\.app\.([a-z_][a-z0-9_]*)\(", text)):
        body = APPLICATION_METHODS.get(called)
        if body and needs_the_runtime(body, depth - 1):
            return True
    return False


def split_by_runtime(names: set[str]) -> tuple[set[str], list[str]]:
    """Which of these methods need the engine, and which no route was read for."""
    runtime_bound, unread = set(), []
    for name in sorted(names):
        bodies = routing_bodies(name)
        if not bodies:
            unread.append(name)
        elif any(needs_the_runtime(body) for body in bodies):
            runtime_bound.add(name)
    return runtime_bound, unread


def host_calls() -> set[str]:
    """The methods the desktop host sends, read from its own source."""
    source = HOST_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r'"([a-z][a-z0-9_]*\.[a-z0-9_]+)"',
                          source[:source.index("#[cfg(test)]")]))


def test_the_capability_split_follows_the_dispatchers_runtime_requirement(app):
    """What waits for the engine is decided by the handler, in both directions.

    The list was split by what the window happened to gate rather than by what
    the dispatcher can answer, so a family that calls ``require_runtime()`` --
    chat, transfers, the sync pause verbs, the AI config editor, a device's note
    and the companion's own configuration -- was advertised before the engine
    starts, where a control gated on it is live and the call comes back
    ``LAN_NOT_READY``.  Nothing but this list said otherwise: the phone panel
    reads none of it.  The other direction had drifted too -- the internet
    pairing family, the delivery ledger, the transfer history's clear and the
    answer to a certificate prompt are routed, called by the host, and were
    named nowhere.
    """
    always, while_running = capability_blocks()
    # The parse above is only as good as its agreement with the running app:
    # this fixture has no engine up, so it advertises exactly the first block.
    assert set(app.status()["capabilities"]) == always, "the parsed first block is not the live one"
    assert len(always) > 30 and len(while_running) > 30, (
        f"only {len(always)} always and {len(while_running)} while running")

    runtime_bound, unread = split_by_runtime(always | while_running)
    # A route this reader does not know would be skipped in silence, which is
    # the one failure mode a guard must not have.
    assert not unread, f"no dispatch route was read for: {unread}"
    promised = sorted(always & runtime_bound)
    assert not promised, f"advertised before the engine but needs it: {promised}"
    misfiled = sorted(while_running ^ runtime_bound)
    assert not misfiled, f"the engine block and the handlers disagree about: {misfiled}"

    # The block is only half the fact.  These are the methods a client can
    # reach, and one of them needing the engine while the list stays silent is
    # how the transfer history's clear and the internet pairing family went
    # unnamed while the host called them.
    unadvertised = sorted(split_by_runtime(host_calls())[0] - always - while_running)
    assert not unadvertised, f"the host calls these, and needs the engine for them: {unadvertised}"


def result_keys(body: str) -> set[str]:
    """The keys a dispatch body's own ``return {...}`` puts at its top level.

    Scanned rather than matched, because the answer is about depth: a
    `session_id` inside a list inside the result is the command's own business,
    and only a top-level one is a claim about the frame.  Strings and comments
    are stepped over so a key of the same name inside one is not read as one.
    """
    keys: set[str] = set()
    for match in re.finditer(r"\breturn \{", body):
        depth = 0
        index = match.end() - 1
        while index < len(body):
            character = body[index]
            if character == "#":
                newline = body.find("\n", index)
                index = len(body) if newline < 0 else newline
                continue
            if character in "\"'":
                end = index + 1
                while end < len(body):
                    if body[end] == "\\":
                        end += 2
                        continue
                    if body[end] == character:
                        break
                    end += 1
                if depth == 1 and body[end + 1:].lstrip().startswith(":"):
                    keys.add(body[index + 1:end])
                index = end + 1
                continue
            if character in "{[(":
                depth += 1
            elif character in "}])":
                depth -= 1
                if depth == 0:
                    break
            index += 1
    return keys


def test_the_result_key_reader_sees_only_the_top_level():
    """Its own guard: a scanner that read nothing would pass the sweep below."""
    assert result_keys('return {"copied": True}') == {"copied"}
    assert result_keys('return {}') == set()
    assert result_keys('return {"items": [{"session_id": one}]}') == {"items"}
    assert result_keys('return {"session_id": one}') == {"session_id"}
    # A key of the right name inside a string or a comment is not one, and a
    # body that never returns a literal yields nothing.
    assert result_keys('# a note about "session_id"\nreturn {"ok": True}') == {"ok"}
    assert result_keys('return {"note": "session_id"}') == {"note"}
    assert result_keys('result = {"session_id": one}\nreturn result') == set()


def test_no_dispatch_result_claims_a_name_the_envelope_reserves():
    """The same profile, read out of both dispatchers before a window meets one.

    `validate_result` refuses these at runtime, and refusing them there is what
    a restart loop is made of when it is missed: the host kills the sidecar
    rather than failing the command, so the mistake reports itself as a process
    that will not stay up and names no method.  Reading the dispatchers says so
    at the line instead.
    """
    scanned, offenders = 0, []
    for module in (rpc, favorites):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for names, families, _, body in dispatch_blocks(source):
            scanned += 1
            claimed = sorted(result_keys(body) & {"session_id", "seq"})
            if claimed:
                offenders.append(f"{names or families}: {claimed}")
    # Guard the reading: a scanner that found no blocks at all would pass on
    # anything, which is the one failure mode a guard must not have.
    assert scanned > 50, f"only {scanned} dispatch blocks read"
    assert not offenders, (
        f"a result may not carry these at its top level: {offenders}"
    )
