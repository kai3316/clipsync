"""Recovery paths in the transport layer.

Every case here is a "the app never comes back on its own" bug: a retry loop
that never escalates, an mDNS registration that can never be retried, a
sleep/wake detector fooled by the clock, and a reconnect suppression with no
way out.  They all look fine in a happy-path test and only bite after a real
network event, so they get their own file.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.transport import discovery as D  # noqa: N812
from internal.transport import relay as R  # noqa: N812

# --------------------------------------------------------------- relay backoff


def _relay(monkeypatch):
    t = R.RelayTransport(
        ["wss://broker.example:8884/mqtt"],
        get_channels=lambda: {},
        on_frame=lambda frame, _topic=None: None,
        on_state=lambda s: None,
        client_factory=lambda: None,
        sleeper=lambda s: None,
    )
    return t


def _drive_run(t, sessions: int, session_len: float = 0.0):
    """Run the worker loop for `sessions` connect/drop cycles, capturing sleeps."""
    delays: list[float] = []
    t._sleeper = delays.append
    stop = threading.Event()
    seen = {"n": 0}

    t._connect_one = lambda index, stop_ev=None: True

    def fake_serve(index, stop_ev=None):
        seen["n"] += 1
        if session_len:
            time.sleep(session_len)
        if seen["n"] >= sessions:
            stop.set()

    t._serve_until_lost = fake_serve
    t._run(stop)
    return delays


def test_backoff_escalates_when_the_broker_drops_us_immediately(monkeypatch):
    """CONNACK is not success.

    A broker that accepts the connection and drops the socket a second later
    used to reset `attempt` on every cycle, so the delay stayed pinned at
    BACKOFF_SEQUENCE[0] == 1s -- an endless once-a-second reconnect storm that
    never escalated and never gave the next broker a fair chance.
    """
    t = _relay(monkeypatch)
    delays = _drive_run(t, sessions=4)
    # 1st drop -> attempt 1 -> 2s, then 4s, then 8s.
    assert delays[:3] == [2, 4, 8]


# ------------------------------------------------------------ mDNS advertising


class _FakeZeroconf:
    def __init__(self, fail_times=0):
        self.registered = []
        self.unregistered = []
        self._fail_times = fail_times

    def register_service(self, info):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise OSError("no route to host")
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)


@pytest.fixture
def disco(monkeypatch):
    monkeypatch.setattr(D, "get_all_local_addresses", lambda: ["192.168.1.5"])
    monkeypatch.setattr(D, "_get_local_address", lambda: "192.168.1.5")
    d = D.Discovery("dev-1", "Laptop", 45678, "_clipsync._tcp.local.")
    return d


def test_failed_registration_can_be_retried(disco):
    """The killer bug: a failed register left _service_info set.

    _service_info was assigned before register_service(), and the failure path
    only logged -- so the `if self._service_info is not None: return` guard at
    the top rejected every later attempt.  The device stayed undiscoverable for
    the rest of the process while is_advertising() reported True.
    """
    disco._zc = _FakeZeroconf(fail_times=1)
    disco.start_advertising()
    assert disco._service_info is None
    assert disco.is_advertising is False

    disco.start_advertising()  # must actually try again
    assert disco._zc.registered, "start_advertising never retried"
    assert disco.is_advertising is True


# ------------------------------------------------------- sleep/wake detection


class _StubPairing:
    def __init__(self, paired=True, fp="AA:BB"):
        self._paired = paired
        self._fp = fp

    def is_peer_paired(self, pid):
        return self._paired

    def get_peer_fingerprint(self, pid):
        return self._fp


def _mgr(monkeypatch):
    from internal.transport import connection as C  # noqa: N812

    m = C.TransportManager.__new__(C.TransportManager)
    m._lock = threading.RLock()
    m._peers = {}
    m._peer_addresses = {}
    m._reconnect_timers = {}
    m._reconnect_attempts = {}
    m._cert_pin_blocked = {}
    m._hash_to_real_id = {}
    m._running = True
    m._max_reconnect_attempts = 5
    m._pairing_mgr = _StubPairing()
    return m


def test_cert_pin_block_is_lifted_once_the_pin_changes(monkeypatch):
    """A pin mismatch stops reconnects -- re-pairing has to start them again.

    Nothing used to lift the suppression, so after the user accepted the new
    certificate the device stayed "paired but unreachable" until an app
    restart.
    """
    m = _mgr(monkeypatch)
    m._peer_addresses["hash-1"] = ("Laptop", "192.168.1.9", 45678)
    m._cert_pin_blocked["hash-1"] = ("real-1", "OLD:FP")
    scheduled: list[str] = []
    m._schedule_reconnect = scheduled.append

    m._pairing_mgr = _StubPairing(fp="OLD:FP")
    m._recheck_cert_pin_blocks()
    assert scheduled == [], "reconnected while the pin was still the refused one"
    assert "hash-1" in m._cert_pin_blocked

    m._pairing_mgr = _StubPairing(fp="NEW:FP")  # user re-paired
    m._recheck_cert_pin_blocks()
    assert scheduled == ["hash-1"]
    assert "hash-1" not in m._cert_pin_blocked


def test_cert_pin_block_is_dropped_without_reconnecting_when_unpaired(monkeypatch):
    m = _mgr(monkeypatch)
    m._cert_pin_blocked["hash-1"] = ("real-1", "OLD:FP")
    scheduled: list[str] = []
    m._schedule_reconnect = scheduled.append
    m._pairing_mgr = _StubPairing(paired=False, fp="NEW:FP")
    m._recheck_cert_pin_blocks()
    assert scheduled == []
    assert m._cert_pin_blocked == {}


@pytest.mark.parametrize(
    "wall_gap,mono_gap,expected,why",
    [
        (15.0, 15.0, False, "a normal tick"),
        (900.0, 900.0, True, "suspend on Windows/macOS (monotonic includes it)"),
        (900.0, 15.0, True, "suspend on Linux (monotonic paused, wall ran on)"),
        (15.0, 900.0, True, "the loop itself was starved for 15 minutes"),
        (-3600.0, 15.0, False, "clock stepped an hour backwards -- not a sleep"),
        (16.0, 15.0, False, "a second of scheduling jitter"),
    ],
)
def test_wake_detection_uses_both_clocks(wall_gap, mono_gap, expected, why):
    from internal.transport.connection import _looks_like_wake

    assert _looks_like_wake(wall_gap, mono_gap) is expected, why


