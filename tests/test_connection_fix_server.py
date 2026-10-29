"""Server-side fixes: exclusive bind on Windows, temporary holds, address cache.

* On Windows ``SO_REUSEADDR`` lets a second live listener take the same port, so
  the port-in-use detection was effectively dead there (two ClipSync instances
  both bound 9500 and both believed they were listening).  ``SO_EXCLUSIVEADDRUSE``
  is the Windows option with the POSIX meaning; POSIX keeps ``SO_REUSEADDR`` so a
  restart can rebind across TIME_WAIT.
* ``hold_peer`` is the temporary refusal an "unpair" needs: refusing inbound
  connections for a bounded time without the permanent blacklist that made the
  machine whose user unpaird first unable ever to re-pair.
* ``forget_saved_address`` drops the cached address a failed dial kept repeating
  after the peer moved networks.
"""

import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.transport import connection as C  # noqa: N812
from tests.test_connection import MockPairingManager

# --------------------------------------------------------------- test helpers


def _manager(port=0):
    return C.TransportManager("dev-a", "Device A", port, MockPairingManager())


def _scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        C.TransportManager, "_secure_scratch_dir", staticmethod(lambda: tmp_path)
    )


# ------------------------------------------------- medium-21: exclusive binding


def test_the_listening_socket_uses_an_exclusive_bind_on_windows(monkeypatch, tmp_path):
    """POSIX refuses a second live listener with SO_REUSEADDR; Windows does not.

    So Windows needs the option that has the POSIX meaning.  Asserted as the
    socket option actually in force, because the behaviour it prevents
    (a double bind) is what the next test measures.
    """
    _scratch(monkeypatch, tmp_path)
    manager = _manager()
    try:
        manager.start_server()
        sock = manager._server_sock
        assert sock is not None
        if os.name == "nt":
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
        else:
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    finally:
        manager.stop_server(timeout=5)


def test_a_second_manager_cannot_bind_the_same_port(monkeypatch, tmp_path):
    """The port-in-use check has to work for the most common conflict.

    Platform note: handled by the platform's own option, so this runs unchanged
    on Windows (exclusive bind) and POSIX (a listening socket refuses the bind
    even with reuse set).  No skip is needed — if a platform ever did allow it,
    this test is the one that should fail.
    """
    _scratch(monkeypatch, tmp_path)
    first = _manager()
    second = None
    try:
        first.start_server()
        port = first._port
        second = C.TransportManager("dev-b", "Device B", port, MockPairingManager())
        with pytest.raises(C.PortInUseError) as raised:
            second.start_server()
        assert raised.value.port == port
        assert second._running is False, "a manager that could not bind thinks it is running"
    finally:
        if second is not None:
            second.stop_server(timeout=5)
        first.stop_server(timeout=5)


# ------------------------------------------------- medium-22: temporary refusal


def test_hold_peer_refuses_inbound_for_a_while_and_then_expires():
    """Unpair is temporary; "remove device" is permanent.  Different calls."""
    manager = _manager()
    timer = threading.Timer(60, lambda: None)
    timer.daemon = True
    timer.start()
    try:
        with manager._lock:
            manager._peer_addresses["dev-b"] = ("Device B", "192.168.1.9", 7000)
        manager._reconnect_attempts["dev-b"] = 3
        manager._reconnect_timers["dev-b"] = timer

        manager.hold_peer("dev-b", 180)

        deadline = manager._connect_holds.get("dev-b")
        assert deadline is not None
        assert deadline > time.monotonic() + 170, "the hold is shorter than asked for"
        assert manager._on_hold("dev-b") is True, "inbound connections are not refused"
        assert "dev-b" not in manager._reconnect_timers, "a reconnect timer survived the hold"
        assert "dev-b" not in manager._reconnect_attempts
        # A hold is not a removal: the peer is not blacklisted, and the address
        # it will need for the next pairing is kept.
        assert "dev-b" not in manager._rejected_peer_ids
        assert "dev-b" in manager._peer_addresses

        # Expiry is what makes this different from forget_peer.
        with manager._lock:
            manager._connect_holds["dev-b"] = time.monotonic() - 0.01
        assert manager._on_hold("dev-b") is False, "the hold never expired"
    finally:
        timer.cancel()
        manager._running = False


def test_forget_peer_is_still_permanent():
    """The control: removing a device must not become temporary."""
    manager = _manager()
    with manager._lock:
        manager._peer_addresses["dev-b"] = ("Device B", "192.168.1.9", 7000)
    try:
        manager.forget_peer("dev-b")
        assert "dev-b" in manager._rejected_peer_ids
        assert "dev-b" not in manager._peer_addresses
    finally:
        manager._running = False


def test_hold_peer_lifts_a_stale_permanent_rejection():
    """An unpair after an old removal must not leave the peer refusable forever."""
    manager = _manager()
    manager._rejected_peer_ids.add("dev-b")
    try:
        manager.hold_peer("dev-b", 30)
        assert "dev-b" not in manager._rejected_peer_ids
        assert manager._on_hold("dev-b") is True
    finally:
        manager._running = False


# ------------------------------------------------- medium-19: cached addresses


def test_forget_saved_address_removes_both_address_tables():
    manager = _manager()
    with manager._lock:
        manager._peer_addresses["dev-b"] = ("Device B", "10.0.0.5", 7000)
        manager._callback_addresses["dev-b"] = ("Device B", "10.0.0.6", 7000)
    try:
        assert manager.get_saved_address("dev-b") is not None
        assert manager.forget_saved_address("dev-b") is True
        assert manager.get_saved_address("dev-b") is None
        assert "dev-b" not in manager._peer_addresses
        assert "dev-b" not in manager._callback_addresses
    finally:
        manager._running = False


def test_forget_saved_address_can_keep_the_callback_address():
    """A dial-only cache entry can be dropped while the peer's own call is kept."""
    manager = _manager()
    with manager._lock:
        manager._peer_addresses["dev-b"] = ("Device B", "10.0.0.5", 7000)
        manager._callback_addresses["dev-b"] = ("Device B", "10.0.0.6", 7000)
    try:
        assert manager.forget_saved_address("dev-b", include_callback=False) is True
        assert "dev-b" not in manager._peer_addresses
        assert manager._callback_addresses["dev-b"] == ("Device B", "10.0.0.6", 7000)
        assert manager.get_saved_address("dev-b") == ("Device B", "10.0.0.6", 7000)
    finally:
        manager._running = False


def test_forget_saved_address_says_when_there_was_nothing_to_forget():
    manager = _manager()
    try:
        assert manager.forget_saved_address("dev-unknown") is False
    finally:
        manager._running = False
