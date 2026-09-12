import sys
from unittest.mock import Mock

import pytest

from scripts.smoke_sidecar import (
    SidecarProcess,
    SmokeError,
    verify_companion_runtime,
    verify_http,
)


def test_smoke_skips_interleaved_events_and_matches_response():
    import queue

    child = SidecarProcess.__new__(SidecarProcess)
    child.process = Mock()
    child.counter = 0
    child.frames = queue.Queue()
    child.frames.put({"type": "event", "data": {"secret": "do-not-print"}})
    child.frames.put({"type": "resync"})
    child.frames.put({"type": "response", "id": "1", "ok": True, "result": {"running": True}})
    assert child.call("companion.status") == {"running": True}


def test_smoke_rpc_errors_do_not_expose_credentials():
    import queue

    child = SidecarProcess.__new__(SidecarProcess)
    child.process = Mock()
    child.counter = 0
    child.frames = queue.Queue()
    child.frames.put({"type": "response", "id": "1", "ok": False,
                      "error": {"message": "credential-do-not-print"}})
    with pytest.raises(SmokeError) as error:
        child.call("companion.status")
    assert "credential" not in str(error.value)


def test_smoke_http_errors_do_not_expose_urls(monkeypatch):
    opener = Mock()
    opener.open.side_effect = RuntimeError("http://host?token=credential-do-not-print")
    monkeypatch.setattr("scripts.smoke_sidecar.build_opener", lambda *args: opener)
    with pytest.raises(SmokeError) as error:
        verify_http({"running": True, "actual_port": 12345, "token": "secret"})
    assert str(error.value) == "Companion HTTP verification failed"


def test_companion_smoke_against_real_source_process(tmp_path, capsys):
    verify_companion_runtime([sys.executable, "-u", "-m", "src.sidecar_main"], tmp_path)
    output = capsys.readouterr().out
    assert output.count("PASS") == 2
    assert "token=" not in output
