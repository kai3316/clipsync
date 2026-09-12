import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

from internal.application.errors import ApplicationError
from internal.data import recovery
from src import sidecar_main


def test_shutdown_retries_incomplete_cleanup_without_pretending_success():
    app = SimpleNamespace(lifecycle=SimpleNamespace(stop=Mock(side_effect=[False, True])))
    assert sidecar_main._shutdown(app)
    assert app.lifecycle.stop.call_count == 2


def test_shutdown_is_bounded_and_sanitizes_errors(caplog):
    stop = Mock(side_effect=OSError("secret path"))
    assert not sidecar_main._shutdown(SimpleNamespace(lifecycle=SimpleNamespace(stop=stop)))
    assert stop.call_count == 2
    assert "secret path" not in caplog.text


def test_constructor_failure_still_returns_protocol_error(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(sidecar_main.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(
        sidecar_main, "SidecarApplication", Mock(side_effect=ValueError("private path"))
    )
    assert sidecar_main.main(["--history-only"]) == 2
    frame = json.loads(output.getvalue())
    assert frame["type"] == "fatal"
    assert frame["error"]["code"] == "STARTUP_FAILED"
    assert b"private path" not in output.getvalue()


def test_failed_start_and_closed_parent_pipe_do_not_raise_again(monkeypatch):
    output = Mock()
    output.write.side_effect = BrokenPipeError
    app = SimpleNamespace(lifecycle=SimpleNamespace(
        start=Mock(side_effect=ApplicationError("DATA_INVALID", "Recover data")),
        stop=Mock(return_value=True),
    ))
    monkeypatch.setattr(sidecar_main.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(sidecar_main, "SidecarApplication", Mock(return_value=app))
    assert sidecar_main.main(["--history-only"]) == 2
    app.lifecycle.stop.assert_called_once()


def test_recover_mode_reports_and_never_starts_the_application(monkeypatch, tmp_path):
    # The pass exists because the application refuses to start, so it must not
    # construct one — and it must name the files it moved, since they are
    # renamed rather than deleted and the user may want them back.
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    output = io.BytesIO()
    monkeypatch.setattr(sidecar_main.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(sidecar_main, "SidecarApplication", Mock(
        side_effect=AssertionError("recovery must not start the application")
    ))
    assert sidecar_main.main(["--recover"]) == 0
    frame = json.loads(output.getvalue())
    assert frame["type"] == "recovered"
    assert frame["items"][0]["artifact"] == "config"
    assert frame["items"][0]["files"][0].startswith("config.json.corrupt-")


def test_recover_mode_reports_a_failed_pass_as_a_fatal_frame(monkeypatch, tmp_path):
    output = io.BytesIO()
    # Point the pass at a scratch directory even though it is patched out, so a
    # patch that stopped applying could never move the developer's own data.
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(sidecar_main.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(recovery, "quarantine", Mock(side_effect=OSError("private path")))
    assert sidecar_main.main(["--recover"]) == 2
    frame = json.loads(output.getvalue())
    assert frame["type"] == "fatal"
    assert frame["error"]["code"] == "RECOVERY_FAILED"
    assert b"private path" not in output.getvalue()


def test_incomplete_cleanup_sets_nonzero_exit_code(monkeypatch):
    app = SimpleNamespace(lifecycle=SimpleNamespace(
        start=Mock(), stop=Mock(return_value=False),
    ))
    monkeypatch.setattr(sidecar_main, "SidecarApplication", Mock(return_value=app))
    monkeypatch.setattr(sidecar_main.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(sidecar_main.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(sidecar_main, "RpcServer", Mock(
        return_value=SimpleNamespace(serve=Mock(return_value=0))
    ))
    assert sidecar_main.main(["--history-only"]) == 3
    assert app.lifecycle.stop.call_count == 2
