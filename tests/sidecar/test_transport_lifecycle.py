"""Shutdown ownership tests for the existing TCP/TLS transport."""

import contextlib
import socket
import ssl
import struct
import threading
import time

import pytest

from internal.transport import connection as transport
from tests.test_connection import MockPairingManager


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(
        transport.TransportManager, "_secure_scratch_dir", staticmethod(lambda: tmp_path)
    )
    tm = transport.TransportManager("local", "Local", 0, MockPairingManager())
    yield tm
    assert tm.stop_server(timeout=2), "test left transport ownership outstanding"


def test_repeated_stop_and_restart_join_server_and_health(manager):
    assert manager.stop_server()
    manager.start_server()
    first = (manager._server_thread, manager._health_thread)
    listener = manager._server_sock
    manager.start_server()
    assert manager._server_sock is listener
    assert manager.stop_server(timeout=2)
    assert all(not thread.is_alive() for thread in first)
    assert listener.fileno() == -1
    assert manager._server_sock is None
    assert manager.stop_server(timeout=0)
    manager.start_server()
    assert manager._server_thread is not first[0]
    assert manager.stop_server(timeout=2)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_partial_thread_start_failure_closes_listener_and_joins(manager, monkeypatch, fail_at):
    original = threading.Thread.start
    calls = []

    def start(thread):
        calls.append(thread)
        if len(calls) == fail_at:
            raise RuntimeError("injected thread start failure")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="injected"):
        manager.start_server()
    assert not manager._running
    assert manager._server_sock is None
    assert all(not thread.is_alive() for thread in calls)
    assert manager.stop_server(timeout=0)


def test_partial_listen_failure_closes_socket(manager, monkeypatch):
    class Listener:
        closed = False

        def setsockopt(self, *args):
            pass

        def bind(self, *args):
            pass

        def listen(self, *args):
            raise OSError("injected listen failure")

        def close(self):
            self.closed = True

    listener = Listener()
    monkeypatch.setattr(manager, "_build_ssl_context", lambda **kwargs: object())
    monkeypatch.setattr(transport.socket, "socket", lambda *args: listener)
    with pytest.raises(OSError, match="injected"):
        manager.start_server()
    assert listener.closed
    assert manager._server_sock is None
    assert manager.stop_server(timeout=0)


def test_stop_interrupts_real_incoming_tls_handshake(manager, monkeypatch):
    entered = threading.Event()
    original = ssl.SSLSocket.do_handshake

    def handshake(sock, *args, **kwargs):
        if sock.server_side:
            entered.set()
        return original(sock, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLSocket, "do_handshake", handshake)
    manager.start_server()
    address = ("127.0.0.1", manager._server_sock.getsockname()[1])
    with socket.create_connection(address) as client:
        assert entered.wait(2)
        workers = set(manager._workers)
        assert manager.stop_server(timeout=2)
        assert all(not thread.is_alive() for thread in workers)
        client.settimeout(1)
        with contextlib.suppress(ConnectionResetError):
            assert client.recv(1) == b""
    assert not manager._peers
    assert not manager._pending_sockets


def test_stop_joins_receive_thread_and_suppresses_late_callback(manager):
    manager._running = True
    left, right = socket.socketpair()
    calls = []
    manager.set_on_peer_message(lambda *args: calls.append(args))
    conn = transport.PeerConnection("peer", "Peer", left)
    try:
        assert manager._start_peer(conn)
        callback = conn._on_message
        manager._peers["peer"] = conn
        assert manager.stop_server(timeout=2)
        assert not conn._recv_thread.is_alive()
        callback("late", object())
        assert calls == []
        assert manager.get_connected_peers() == []
    finally:
        right.close()


def test_receive_callback_can_stop_without_self_join(manager, monkeypatch):
    manager._running = True
    left, right = socket.socketpair()
    stopped = threading.Event()
    results = []

    def callback(*args):
        results.append(manager.stop_server(timeout=2))
        stopped.set()

    monkeypatch.setattr(transport, "decode_message", lambda payload: object())
    manager.set_on_peer_message(callback)
    conn = transport.PeerConnection("peer", "Peer", left)
    try:
        assert manager._start_peer(conn)
        right.sendall(struct.pack(">I", 1) + b"x")
        assert stopped.wait(2), "callback tried to join its own receive thread"
        assert results == [False]
        assert manager.stop_server(timeout=2)
        assert not conn._recv_thread.is_alive()
    finally:
        right.close()


def test_concurrent_callback_stops_do_not_join_each_other(manager):
    manager._running = True
    barrier = threading.Barrier(2)
    results = []

    def callback():
        barrier.wait(timeout=2)
        results.append(manager.stop_server(timeout=2))

    workers = [manager._start_worker(lambda: manager._invoke_callback(callback)) for _ in range(2)]
    for worker in workers:
        worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert results == [False, False]
    assert manager.stop_server(timeout=0)


def test_blocked_callback_retains_ownership_and_blocks_restart(manager):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def callback(*args):
        calls.append(args)
        entered.set()
        assert release.wait(3)

    manager.set_on_connect_rejected(callback)
    # Also account for callbacks invoked from threads not created by the manager.
    caller = threading.Thread(target=manager._notify_connect_rejected, args=("Peer", "peer"))
    caller.start()
    try:
        assert entered.wait(2)
        before = time.monotonic()
        assert manager.stop_server(timeout=0.05) is False
        assert time.monotonic() - before < 0.5
        with pytest.raises(RuntimeError, match="shutdown"):
            manager.start_server()
        manager._notify_connect_rejected("late", "late")
        assert calls == [("Peer", "peer")]
    finally:
        release.set()
        caller.join(2)
    assert manager.stop_server(timeout=2)


def test_connect_in_progress_cannot_resume_after_stop(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    left, right = socket.socketpair()
    manager._running = True
    dials = []

    def dial(*args, **kwargs):
        dials.append(args)
        entered.set()
        assert release.wait(3)
        return left

    monkeypatch.setattr(transport.socket, "create_connection", dial)
    manager.connect_to_peer("peer", "Peer", "127.0.0.1", 1234)
    try:
        assert entered.wait(2)
        assert manager.stop_server(timeout=0.05) is False
        with pytest.raises(RuntimeError, match="shutdown"):
            manager.start_server()
        manager.connect_to_peer("other", "Other", "127.0.0.1", 1234)
        assert len(dials) == 1
    finally:
        release.set()
        assert manager.stop_server(timeout=2)
        right.close()
    assert left.fileno() == -1
    assert not manager._peers
    assert not manager._reconnect_timers


def test_timer_already_firing_is_joined_and_cannot_reconnect(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    manager._running = True
    manager._peer_addresses["peer"] = ("Peer", "127.0.0.1", 1234)
    original = manager._try_reconnect
    dials = []

    def reconnect(peer):
        entered.set()
        assert release.wait(3)
        original(peer)

    monkeypatch.setattr(manager, "_try_reconnect", reconnect)
    monkeypatch.setattr(manager, "connect_to_peer", lambda *args: dials.append(args))
    monkeypatch.setattr(transport, "MIN_RECONNECT_DELAY", 0)
    monkeypatch.setattr(transport, "MAX_RECONNECT_BACKOFF", 0)
    manager._schedule_reconnect("peer")
    timer = manager._reconnect_timers["peer"]
    try:
        assert entered.wait(2)
        assert manager.stop_server(timeout=0.05) is False
    finally:
        release.set()
        assert manager.stop_server(timeout=2)
    assert not timer.is_alive()
    assert not dials


def test_accept_returning_after_stop_never_starts_handshake(manager, monkeypatch):
    entered = threading.Event()
    left, right = socket.socketpair()
    calls = []

    class Listener:
        def accept(self):
            entered.set()
            assert manager._stopped.wait(2)
            return left, ("127.0.0.1", 1234)

        def shutdown(self, *args):
            pass

        def close(self):
            pass

    manager._running = True
    manager._server_sock = Listener()
    monkeypatch.setattr(manager, "_handle_accepted", lambda *args: calls.append(args))
    worker = manager._start_worker(manager._accept_loop, (object(),))
    try:
        assert entered.wait(2)
        assert manager.stop_server(timeout=2)
        assert not worker.is_alive()
        assert left.fileno() == -1
        assert calls == []
    finally:
        right.close()


def test_concurrent_stop_does_not_release_ownership_during_socket_cleanup(manager):
    entered, release = threading.Event(), threading.Event()
    results = []

    class Listener:
        def shutdown(self, *args):
            entered.set()
            assert release.wait(3)

        def close(self):
            pass

    manager._server_sock = Listener()
    stopper = threading.Thread(target=lambda: results.append(manager.stop_server(timeout=2)))
    stopper.start()
    try:
        assert entered.wait(2)
        assert manager.stop_server(timeout=0) is False
        with pytest.raises(RuntimeError, match="shutdown"):
            manager.start_server()
    finally:
        release.set()
        stopper.join(2)
    assert results == [True]
    assert manager.stop_server(timeout=0)
