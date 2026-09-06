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


def test_backoff_resets_after_a_session_that_was_actually_usable(monkeypatch):
    monkeypatch.setattr(R, "MIN_USABLE_SESSION", 0.0)
    t = _relay(monkeypatch)
    delays = _drive_run(t, sessions=4)
    assert delays[:3] == [1, 1, 1]


def test_a_retired_worker_keeps_seeing_its_own_stop_event(monkeypatch):
    """start() must not un-stop a worker that outlived stop()'s bounded join."""
    t = _relay(monkeypatch)
    handed: list = []
    t._run = lambda stop=None: handed.append(stop)
    first = t._stop
    t._stop.set()  # as stop() does
    t._thread = None  # as stop() does
    t.start()  # spawns a worker with a fresh event
    try:
        assert t._stop is not first, "new worker inherited the old event"
        assert first.is_set(), "the retired worker was un-stopped by start()"
        assert not t._stop.is_set()
        deadline = time.time() + 2.0
        while time.time() < deadline and not handed:
            time.sleep(0.01)
        assert handed and handed[0] is t._stop, "_run was not given its own event"
    finally:
        t.stop()


def test_mirror_teardown_does_not_block_the_callback_thread(monkeypatch):
    """_sync_mirrors runs on paho's network thread; joining there stalls keepalive."""
    t = _relay(monkeypatch)
    released = threading.Event()

    class SlowMirror:
        def __init__(self):
            self.stopped = False

        def stop(self):
            released.wait(2.0)
            self.stopped = True

    mirror = SlowMirror()
    t._mirror_clients = {0: mirror}
    t._brokers = []  # every existing mirror is now stale
    began = time.monotonic()
    t._sync_mirrors()
    assert time.monotonic() - began < 0.5, "_sync_mirrors joined the mirror inline"
    released.set()
    deadline = time.time() + 2.0
    while time.time() < deadline and not mirror.stopped:
        time.sleep(0.01)
    assert mirror.stopped, "the retired mirror was never stopped"


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


def test_successful_registration_records_the_advertised_ips(disco):
    disco._zc = _FakeZeroconf()
    disco.start_advertising()
    assert disco._advertised_ips == frozenset({"192.168.1.5"})


def test_failed_registration_leaves_the_old_ip_set_for_a_retry(disco):
    disco._zc = _FakeZeroconf()
    disco.start_advertising()
    old = disco._advertised_ips

    # Interfaces changed, but the re-register fails.
    disco._zc = _FakeZeroconf(fail_times=99)
    disco.stop_advertising()
    disco.start_advertising()
    assert disco._advertised_ips == old, (
        "recording the new IP set on a failed register makes the failure "
        "permanent -- the watcher then sees no change and never retries"
    )


def test_stop_advertising_survives_a_raising_unregister(disco):
    class Boom(_FakeZeroconf):
        def unregister_service(self, info):
            raise OSError("interface gone")

    disco._zc = Boom()
    disco.start_advertising()
    disco.stop_advertising()  # must not propagate
    assert disco._service_info is None
    disco._zc = _FakeZeroconf()
    disco.start_advertising()
    assert disco.is_advertising is True


def test_advertising_is_idempotent(disco):
    disco._zc = _FakeZeroconf()
    disco.start_advertising()
    disco.start_advertising()
    assert len(disco._zc.registered) == 1


def test_is_advertising_reads_service_info_under_the_lock(disco):
    """The visibility flag must not observe a half-written register.

    Every other `_service_info` access is inside `self._lock`; this property
    is what the dashboard's "visible on the LAN" indicator reads, so it has
    to honour the same invariant.  Hold the lock from another thread and the
    read must block rather than sail past.
    """
    disco._zc = _FakeZeroconf()
    disco.start_advertising()

    entered = threading.Event()
    done = threading.Event()
    result = []

    with disco._lock:

        def reader():
            entered.set()
            result.append(disco.is_advertising)
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        assert entered.wait(2.0)
        # We still hold the lock, so the property must not have completed.
        assert not done.wait(0.2), "is_advertising read _service_info unlocked"

    assert done.wait(2.0)
    t.join(2.0)
    assert result == [True]


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


def test_max_idle_is_checked_where_the_waiting_happens(monkeypatch):
    """_recv_exact parks on TimeoutError, so the check must live there too.

    The half-open check at the top of _recv_loop only runs between frames; a
    quiet connection never leaves _recv_exact, which made MAX_IDLE_SECONDS dead
    code in exactly the case it exists for.
    """
    from internal.transport import connection as C  # noqa: N812

    class DeadSock:
        def recv(self, n):
            raise TimeoutError()

    conn = C.PeerConnection.__new__(C.PeerConnection)
    conn._sock = DeadSock()
    conn._running = True
    conn._pending_recv = b""
    conn._remote_closed = False
    conn.device_name = "ghost"
    conn._last_recv_time = time.monotonic() - (C.MAX_IDLE_SECONDS + 5)

    conn._max_idle_expired = lambda: True
    assert conn._recv_exact(4) is None, "recv_exact spun forever on a dead socket"

    # A healthy idle socket must NOT be reaped -- prove the loop keeps waiting.
    conn._max_idle_expired = lambda: False
    done = threading.Event()

    def _run():
        conn._recv_exact(4)
        done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert not done.wait(0.3), "a healthy idle connection was torn down"
    conn._running = False
    done.wait(1.0)


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


def test_backward_clock_step_does_not_hide_a_real_sleep():
    """The old wall-clock-only check went negative and missed the wake entirely."""
    from internal.transport.connection import _looks_like_wake

    # Suspended for 20 minutes AND the clock was corrected 5 minutes back.
    assert _looks_like_wake(1200.0 - 300.0, 1200.0) is True
