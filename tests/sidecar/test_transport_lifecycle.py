"""Stopping and restarting the TCP/TLS transport.

The two cases left are the ones a user can see: sync can be switched off and on
again without leaking the listener, and a stop closes a client that is sitting
in a TLS handshake instead of leaving the thread behind.
"""

import contextlib
import socket
import ssl
import threading

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


