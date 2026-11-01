"""``internet_pairing.test``: the relay probe, as a command.

The old web panel could test its relay brokers from the settings window
(`settings-panel.js` 测试 → `POST /api/internetpair/test`); no RPC command
answered for it, so the native window could not.  This is that command's own
suite: what it probes, what it refuses, and what it answers with.

The probe itself is replaced rather than driven.  A test that opened real
sockets would be a test of the network, and `tests/sidecar/test_internet_panel.py`
already drives `probe_relay_endpoints` against a stub broker; what is under test
here is the command — which brokers reach it, and what it does with the answer.
"""

from __future__ import annotations

import pytest

from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.config.config import Config, save


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False))
    application = SidecarApplication()
    application.lifecycle.start()
    yield application
    application.lifecycle.stop()


@pytest.fixture
def probed(monkeypatch):
    """Records the brokers the probe was asked about, and answers fixed rows."""
    calls: list[list[str]] = []

    def fake(brokers, timeout=4.0, join_timeout=6.0):
        calls.append(list(brokers))
        return [
            {"endpoint": endpoint, "ok": "good" in endpoint, "latency_ms": 12,
             "detail": ""}
            for endpoint in brokers
        ]

    monkeypatch.setattr("internal.application.bootstrap.probe_relay_endpoints", fake)
    return calls


def test_probes_the_staged_list_as_given(app, probed):
    """The list handed in is what is tested — that is the point of the verb."""
    result = app.internet_pairing_test(["wss://good:8884/mqtt", "wss://bad:8884/mqtt"])
    assert probed == [["wss://good:8884/mqtt", "wss://bad:8884/mqtt"]]
    assert result["total"] == 2
    assert result["reachable"] == 1


def test_the_same_broker_staged_twice_is_probed_once(app, probed):
    """The panel deduplicated both of its lists into one before probing, and a
    list that repeats an endpoint would otherwise open two sockets to it and
    report it twice."""
    result = app.internet_pairing_test(["wss://good:8884/mqtt", " wss://good:8884/mqtt "])
    assert probed == [["wss://good:8884/mqtt"]]
    assert result["total"] == 1


def test_an_empty_list_tests_what_is_saved(app, probed):
    """The panel's own reading of an empty body, and the reason this is not an
    error: the button works without staging anything first."""
    saved = app.config.relay_brokers
    saved[:] = ["wss://saved-good:8884/mqtt"]
    result = app.internet_pairing_test([])
    assert probed == [["wss://saved-good:8884/mqtt"]]
    assert result["total"] == 1


def test_nothing_to_test_is_an_error_and_not_an_empty_success(app, probed):
    """A success with no brokers would let a reader save a relay believing it had
    been checked — the one answer worse than a refusal."""
    app.config.relay_brokers[:] = []
    app.config.relay_private_brokers[:] = []
    with pytest.raises(ApplicationError) as refusal:
        app.internet_pairing_test([])
    assert refusal.value.code == "INVALID_ARGUMENT"
    assert probed == [], "a refused test must not open a socket"


def test_the_answer_carries_the_rows_and_both_counts(app, probed):
    """The rows are the answer — which endpoint, how fast, and why not — and the
    counts are what a front words into a sentence.  The sentence itself is
    deliberately absent: the old panel printed an English one, which is not a
    sentence this window can show in Chinese."""
    result = app.internet_pairing_test(["wss://good:8884/mqtt", "wss://bad:8884/mqtt"])
    assert [row["endpoint"] for row in result["results"]] == [
        "wss://good:8884/mqtt", "wss://bad:8884/mqtt",
    ]
    assert set(result) == {"results", "reachable", "total"}


def test_the_command_is_dispatched_and_its_parameters_are_checked(app, probed):
    """Through the dispatcher, because a method that answers here but is not
    routed is a control that can never come alive.  The bounds are the ones the
    settings write enforces: a frame cannot be tested that could not be saved."""
    from internal.adapters.sidecar.rpc import Dispatcher

    call = Dispatcher(app).call
    assert call("internet_pairing.test", {"brokers": ["wss://good:8884/mqtt"]})["total"] == 1
    for params in (
        {"brokers": "wss://good:8884/mqtt"},          # a string is not a list
        {"brokers": [1, 2]},                          # nor a list of numbers
        {"brokers": ["x" * 2049]},                    # longer than a broker may be
        {"brokers": [f"wss://b{i}" for i in range(17)]},  # more than the limit
        {"brokers": [], "unknown": True},             # a key the method has not got
    ):
        with pytest.raises(ApplicationError) as refusal:
            call("internet_pairing.test", params)
        assert refusal.value.code == "VALIDATION_ERROR", params
