"""The transport's wire constants, its send contract, and scratch hygiene.

``MockPairingManager`` also lives here: the TCP/TLS lifecycle suite
(tests/sidecar/test_transport_lifecycle.py) imports it to stand a real
``TransportManager`` up without a real pairing store.
"""

import logging
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.transport.connection import (
    FRAME_HEADER_SIZE,
    MAX_FRAME_SIZE,
    TransportManager,
)


class MockPairingManager:
    """Minimal mock of PairingManager with the interface TransportManager needs.

    Returns a real Ed25519 DeviceIdentity so that any code that reads PEM
    fields or computes fingerprints works without patching cryptography.
    """

    def __init__(self):
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.x509.oid import NameOID

        private_key = ed25519.Ed25519PrivateKey.generate()
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "test-device"),
            ]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(12345)
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365))
            .sign(private_key, None)
        )
        cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        from internal.security.pairing import DeviceIdentity, fingerprint_short

        self._identity = DeviceIdentity(
            device_id="test-device",
            device_name="Test Device",
            private_key=private_key,
            certificate=certificate,
            certificate_pem=cert_pem,
            private_key_pem=key_pem,
            fingerprint="AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99",
            fingerprint_short=fingerprint_short(cert_pem),
        )

    # --- PairingManager interface ------------------------------------------

    def get_identity(self):
        return self._identity

    def get_peer_certificate(self, peer_id):
        return self._identity.certificate_pem

    def is_peer_paired(self, peer_id):
        return False

    def add_peer(self, *args, **kwargs):
        pass

    def generate_shared_pairing_code(self, peer_id):
        return "00000000"


def test_send_to_peer_unknown_peer_returns_false():
    # Regression: nearby chat's _send_frame treats a non-True result as
    # "nothing delivered", so the transport MUST return a real bool — a
    # bare `return` (None) made every chat send look like a failure in
    # the real app while the unit tests' bool-returning stubs hid it.
    tm = TransportManager("dev-1", "Device 1", 9999, MockPairingManager())
    assert tm.send_to_peer("nobody", b"data") is False


class TestConstants:
    """Protocol constants are what the rest of the stack expects."""

    def test_max_frame_size_is_10_mb(self):
        assert MAX_FRAME_SIZE == 10 * 1024 * 1024

    def test_frame_header_size_is_4(self):
        assert FRAME_HEADER_SIZE == 4


def test_stale_scratch_pem_files_are_removed(monkeypatch, tmp_path):
    """Leftover key/cert files in the TLS scratch directory do not survive."""
    monkeypatch.setattr(
        TransportManager,
        "_secure_scratch_dir",
        staticmethod(lambda: tmp_path),
    )

    pem1 = tmp_path / "old_key.pem"
    pem2 = tmp_path / "old_cert.pem"
    keep = tmp_path / "config.txt"

    pem1.write_text("dummy key data")
    pem2.write_text("dummy cert data")
    keep.write_text("should remain")

    assert pem1.exists()
    assert pem2.exists()

    TransportManager._cleanup_stale_scratch()

    assert not pem1.exists(), "Stale .pem file should be removed"
    assert not pem2.exists(), "Stale .pem file should be removed"
    assert keep.exists(), "Non-.pem files must be left untouched"


class _TimeoutThatDiesAfterClose:
    """A socket whose timeout cannot be restored once it has been closed.

    This is what an SSLSocket does: ``sslsocket.settimeout()`` raises
    ``RuntimeError("handshake not done yet")`` when the wrapper's underlying
    ``_sslobj`` is gone, which is the state a socket is left in after the peer
    closes the connection.
    """

    def __init__(self, recv_error: BaseException):
        self._recv_error = recv_error
        self._timeout: float | None = 10.0
        self.closed = False

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        if self.closed:
            raise RuntimeError("handshake not done yet")
        self._timeout = value

    def recv(self, _size):
        # The peer went away between the handshake and the identity frame.
        self.closed = True
        raise self._recv_error


def test_a_peer_that_drops_the_link_is_named_by_its_own_error(caplog):
    """The identity read reports why it failed, not what the cleanup failed at.

    Restoring the socket's timeout runs from ``finally``, so an exception there
    replaces the one that got us into the block.  When the peer closes the
    connection mid-handshake, ``settimeout`` is exactly what raises — so the
    warning in the log used to read "connect failed: handshake not done yet" for
    every such dial, naming neither the peer nor the reason.  The dial's own
    error has to survive.
    """
    peer_gone = ConnectionResetError("connection reset by peer")
    sock = _TimeoutThatDiesAfterClose(peer_gone)

    with caplog.at_level(logging.WARNING, logger="internal.transport.connection"):
        assert TransportManager._recv_identity(sock) is None

    assert "connection reset by peer" in caplog.text
    assert "handshake not done yet" not in caplog.text


def test_a_handshake_that_fails_keeps_its_own_reason():
    """``_wrap_socket`` must not let the timeout restore replace the failure.

    A handshake that times out leaves the wrapper unusable, so the restore in
    the failure path is the other place the real reason could be swapped for
    the cleanup's error.
    """

    class TimingOutWrapper(_TimeoutThatDiesAfterClose):
        def do_handshake(self):
            self.closed = True
            raise TimeoutError("timed out during handshake")

    class Context:
        def wrap_socket(self, sock, **kwargs):
            return TimingOutWrapper(TimeoutError("unused"))

    class Manager(TransportManager):
        def __init__(self):
            self._lock = __import__("threading").RLock()
            self._running = True
            self._pending_sockets = {}
            self._closed = []

        def _close_socket(self, sock):
            self._closed.append(sock)

    manager = Manager()
    with pytest.raises(TimeoutError, match="timed out during handshake"):
        manager._wrap_socket(Context(), object(), server_hostname="peer")
    assert manager._closed, "the failed wrapper must be closed"


class TestStoppingADialInFlight:
    """A dial must not outlive the stop that closed the transport.

    This is the shutdown defect, written down.  `LanRuntime.stop()` gives the
    whole teardown one five-second budget, and the drain inside
    `TransportManager.stop_server` reported a `clipsync-connect` worker still
    sitting in `socket.create_connection` when that budget ran out -- a dial that
    `create_connection` had not yet returned a socket for, so the stop had
    nothing to close.  The runtime then failed to release ownership, the process
    did not exit cleanly, and the log said only "did not release ownership
    within its budget".
    """

    @staticmethod
    def _manager():
        tm = TransportManager("dev-1", "Device 1", 9999, MockPairingManager())
        tm._running = True
        return tm

    @staticmethod
    def _blackhole():
        """A listener that accepts and then says nothing, so a dial hangs.

        Not an unroutable address: whether that hangs or is refused at once
        depends on the network, and this test has to fail for one reason only.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        return listener, listener.getsockname()[1]

    def test_a_dial_is_registered_before_it_connects(self):
        tm = self._manager()
        listener, port = self._blackhole()
        dial = None
        try:
            seen = []
            original = tm._track_socket

            def watch(sock):
                seen.append(sock)
                return original(sock)

            tm._track_socket = watch
            dial = threading.Thread(
                target=self._swallow, args=(tm, "127.0.0.1", port, 10), daemon=True
            )
            dial.start()
            # The socket exists and is known to the manager while the dial is
            # still in progress -- that is the whole fix, and the wait below
            # gives the thread time to get there.
            deadline = time.monotonic() + 5
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)
            assert seen, "the dial did not register its socket"
            assert seen[0].fileno() != -1, "the socket should still be open"
        finally:
            tm._running = False
            tm.stop_server(timeout=1)
            if dial is not None:
                dial.join(5)
            listener.close()

    def test_stopping_closes_a_dial_in_progress_and_returns_inside_its_budget(self):
        tm = self._manager()
        listener, port = self._blackhole()
        result = {}
        dial = None
        try:
            dial = threading.Thread(
                target=self._swallow,
                args=(tm, "127.0.0.1", port, 10),
                kwargs={"into": result},
                daemon=True,
            )
            dial.start()
            deadline = time.monotonic() + 5
            while not tm._pending_sockets and time.monotonic() < deadline:
                time.sleep(0.01)
            assert tm._pending_sockets, "the dial should be registered before it connects"

            started = time.monotonic()
            # A fraction of what the dial would have waited on its own, so a
            # regression shows up as a slow test rather than a flaky one.
            stopped = tm.stop_server(timeout=2)
            elapsed = time.monotonic() - started
            assert stopped is True
            assert elapsed < 2, f"the drain took {elapsed:.1f}s of its 2s budget"
            dial.join(5)
            assert not dial.is_alive(), "the dial outlived the stop"
            # An aborted dial is a stop, not a connection: it must not report a
            # socket the caller could go on to use.  Either it raised, or the
            # connect completed in the window before the close landed -- in which
            # case what it returned is a socket that is already closed.  Both are
            # "not usable", which is the property; asserting only `is None` made
            # this test racy, because which one happens depends on whether the
            # kernel finished the connect before `stop_server` closed the socket.
            outcome = result["outcome"]
            assert outcome is None or outcome.fileno() == -1, (
                f"the dial handed back a usable socket: {outcome!r}"
            )
        finally:
            tm._running = False
            tm.stop_server(timeout=1)
            if dial is not None:
                dial.join(5)
            listener.close()

    @staticmethod
    def _swallow(manager, address, port, timeout, into=None):
        """Dial, reporting None where a stop cut it short rather than an error."""
        try:
            outcome = manager._dial(address, port, timeout)
        except OSError:
            outcome = None
        if into is not None:
            into["outcome"] = outcome
        return outcome


class TestPinningMeansThePin:
    """A pinned dial must trust that certificate and nothing else.

    `create_default_context(Purpose.SERVER_AUTH)` loads the platform trust store,
    and `load_verify_locations` only *adds* to it.  So the old shape meant
    `CERT_REQUIRED` accepted a chain to the pinned certificate **or to any of the
    ~250 public roots on the machine**, with `check_hostname` off, so no name had
    to match either: a pin that let a publicly-signed impostor complete the
    handshake.  Measured on this machine, the store still held 251 CA roots after
    the pin was loaded.
    """

    def test_a_pinned_dial_carries_no_system_trust_anchors(self, tmp_path):
        import ssl

        manager = TransportManager("dev-1", "Device 1", 9999, MockPairingManager())
        peer = MockPairingManager()
        peer_cert = peer.get_identity().certificate_pem

        class Pairing(MockPairingManager):
            def get_peer_certificate(self, peer_id):
                return peer_cert

        manager._pairing_mgr = Pairing()
        # The scratch dir is where the context writes its temporary PEMs.
        scratch = tmp_path / ".scratch"
        scratch.mkdir()
        manager._secure_scratch_dir = staticmethod(lambda: scratch)

        context = manager._build_ssl_context(server_side=False, verify_peer_id="peer-1")

        assert context.verify_mode == ssl.CERT_REQUIRED
        # The load-bearing assertion: only the pinned certificate is an anchor.
        stats = context.cert_store_stats()
        assert stats["x509_ca"] == 0, (
            f"the pinned context still trusts {stats['x509_ca']} CA roots; "
            f"pinning must not be additive"
        )

    def test_an_unpinned_dial_is_unchanged(self, tmp_path):
        """First contact has nothing to pin yet, so it keeps the old shape."""
        import ssl

        manager = TransportManager("dev-1", "Device 1", 9999, MockPairingManager())
        scratch = tmp_path / ".scratch"
        scratch.mkdir()
        manager._secure_scratch_dir = staticmethod(lambda: scratch)

        context = manager._build_ssl_context(server_side=False, verify_peer_id=None)

        # Whatever the platform store holds, this path is not the pinned one:
        # the app-layer identity frame is what decides trust on first contact.
        assert context.verify_mode in (ssl.CERT_NONE, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
