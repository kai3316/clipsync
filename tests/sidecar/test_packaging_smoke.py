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
    lines = [line for line in output.splitlines() if line.strip()]
    # Every line this smoke prints is a check, and a check that ran says PASS —
    # so the assertion is that there are no silent or failing lines, plus one
    # named expectation per check.  It used to be a bare total (`count("PASS")
    # == 2`), which drifts the moment a check is added and does so *silently*:
    # the share check landed in `verify_share` and took the total to 4, and the
    # count was not updated, so the suite went red while the smoke itself still
    # printed nothing but PASS.  Naming the checks makes the next addition fail
    # here loudly instead.
    assert lines and all(line.endswith(": PASS") for line in lines), output
    assert output.count("Companion authenticated HTTP/stop/exit/restart") == 2
    assert output.count("Shared file reaches the phone's list and download") == 2
    # Once, not twice: the executable's advertised capabilities cannot change
    # between the two processes, so the second iteration would only re-read it.
    assert output.count("Packaged capabilities match this tree") == 1
    # Once, and on the second iteration only: it clears the token, so it runs
    # after everything that needs one.
    assert output.count("Cleared access token serves without one") == 1
    assert "token=" not in output


def test_capabilities_are_read_from_the_tree_both_ways(monkeypatch, tmp_path):
    """`verify_capabilities` compares the executable with `bootstrap.py`.

    A stale packaged sidecar is what this catches, and it is not hypothetical —
    the record credits a build ten Python modules behind, which every other
    check in the smoke would have passed.
    """
    from scripts import smoke_sidecar

    expected = smoke_sidecar.fold_capabilities(smoke_sidecar.capability_expression(), False)
    # The engine-down half, which is what the smoke compares when the sidecar
    # reports the engine as down.
    assert len(expected) > 40, "bootstrap.py's capability list did not parse"

    def status(**overrides):
        return {"sync_state": "not_started", "capabilities": list(expected), **overrides}

    smoke_sidecar.verify_capabilities(status())
    # An executable built before a method was added does not know it.
    with pytest.raises(SmokeError) as error:
        smoke_sidecar.verify_capabilities(status(capabilities=expected[:-1]))
    assert "older than this tree" in str(error.value)
    # And one advertising what the tree has dropped is caught in the other
    # direction, so a removed method cannot linger in a released build.
    with pytest.raises(SmokeError) as error:
        smoke_sidecar.verify_capabilities(status(capabilities=expected + ["gone.method"]))
    assert "does not grant" in str(error.value)
    # The half it should carry follows the state the executable reports: the
    # engine's methods are advertised only while the engine is up.
    up = smoke_sidecar.fold_capabilities(smoke_sidecar.capability_expression(), True)
    assert len(up) > len(expected)
    smoke_sidecar.verify_capabilities({"sync_state": "running", "capabilities": list(up)})
    with pytest.raises(SmokeError):
        smoke_sidecar.verify_capabilities({"sync_state": "running", "capabilities": list(expected)})
