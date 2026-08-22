"""Tests for TransportManager and PeerConnection — init, attributes, and
operations that do not require a live network or TLS handshake."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.transport.connection import (
    DATA_TIMEOUT,
    FRAME_HEADER_SIZE,
    MAX_FRAME_SIZE,
    PeerConnection,
    TransportManager,
)

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

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
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "test-device"),
        ])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(12345)
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(
                datetime.datetime.now(datetime.UTC)
                + datetime.timedelta(days=365)
            )
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


class MockSocket:
    """Minimal socket stub — has the methods PeerConnection calls."""

    def __init__(self):
        self._timeout = None

    def settimeout(self, timeout):
        self._timeout = timeout

    def shutdown(self, how):
        pass

    def close(self):
        pass

    def sendall(self, data):
        pass

    def recv(self, bufsize):
        return b""


# ---------------------------------------------------------------------------
# TransportManager
# ---------------------------------------------------------------------------

class TestTransportManagerInit:
    """TransportManager.__init__ stores constructor arguments."""

    def setup_method(self):
        self.pairing_mgr = MockPairingManager()

    def test_stores_device_id(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._device_id == "dev-1"

    def test_stores_device_name(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._device_name == "Device 1"

    def test_stores_port(self):
        tm = TransportManager("dev-1", "Device 1", 5555, self.pairing_mgr)
        assert tm._port == 5555

    def test_stores_pairing_mgr(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._pairing_mgr is self.pairing_mgr

    def test_starts_with_empty_peers_dict(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._peers == {}

    def test_starts_not_running(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._running is False

    def test_on_peer_message_is_none_initially(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._on_peer_message is None

    def test_server_sock_is_none_initially(self):
        tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)
        assert tm._server_sock is None


class TestTransportManagerOperations:
    """Operations that do not need a running server or network."""

    def setup_method(self):
        self.pairing_mgr = MockPairingManager()
        self.tm = TransportManager("dev-1", "Device 1", 9999, self.pairing_mgr)

    def test_set_on_peer_message_sets_callback(self):
        def cb(msg):
            pass
        self.tm.set_on_peer_message(cb)
        assert self.tm._on_peer_message is cb

    def test_set_on_peer_message_overwrites_previous(self):
        def cb1(msg):
            pass
        def cb2(msg):
            pass
        self.tm.set_on_peer_message(cb1)
        self.tm.set_on_peer_message(cb2)
        assert self.tm._on_peer_message is cb2

    def test_get_connected_peers_returns_empty_list_initially(self):
        assert self.tm.get_connected_peers() == []
        assert isinstance(self.tm.get_connected_peers(), list)

    def test_broadcast_with_no_peers_does_not_raise(self):
        self.tm.broadcast(b"test-data")

    def test_broadcast_empty_data_does_not_raise(self):
        self.tm.broadcast(b"")

    def test_disconnect_unknown_peer_does_not_raise(self):
        self.tm.disconnect_peer("nonexistent-peer-id")

    def test_disconnect_unknown_peer_does_not_affect_peers(self):
        self.tm.disconnect_peer("nonexistent-peer-id")
        assert self.tm._peers == {}


# ---------------------------------------------------------------------------
# PeerConnection
# ---------------------------------------------------------------------------

class TestPeerConnectionInit:
    """PeerConnection.__init__ stores constructor arguments."""

    def setup_method(self):
        self.sock = MockSocket()
        self.conn = PeerConnection("peer-1", "Peer One", self.sock)

    def test_stores_device_id(self):
        assert self.conn.device_id == "peer-1"

    def test_stores_device_name(self):
        assert self.conn.device_name == "Peer One"

    def test_stores_socket(self):
        assert self.conn._sock is self.sock

    def test_sets_socket_data_timeout(self):
        assert self.sock._timeout == DATA_TIMEOUT

    def test_starts_not_running(self):
        assert self.conn._running is False

    def test_recv_thread_is_none_initially(self):
        assert self.conn._recv_thread is None

    def test_on_message_is_none_initially(self):
        assert self.conn._on_message is None

    def test_on_disconnect_is_none_initially(self):
        assert self.conn._on_disconnect is None

    def test_send_lock_is_initialized(self):
        import threading
        assert isinstance(self.conn._send_lock, type(threading.Lock()))


class TestPeerConnectionCallbacks:
    """set_on_message / set_on_disconnect store user-provided callbacks."""

    def setup_method(self):
        self.sock = MockSocket()
        self.conn = PeerConnection("peer-1", "Peer One", self.sock)

    def test_set_on_message_stores_callback(self):
        def cb(msg):
            pass
        self.conn.set_on_message(cb)
        assert self.conn._on_message is cb

    def test_set_on_disconnect_stores_callback(self):
        def cb(peer_id):
            pass
        self.conn.set_on_disconnect(cb)
        assert self.conn._on_disconnect is cb

    def test_set_on_message_replaces_previous(self):
        def cb1(msg):
            pass
        def cb2(msg):
            pass
        self.conn.set_on_message(cb1)
        self.conn.set_on_message(cb2)
        assert self.conn._on_message is cb2

    def test_set_on_disconnect_replaces_previous(self):
        def cb1(peer_id):
            pass
        def cb2(peer_id):
            pass
        self.conn.set_on_disconnect(cb1)
        self.conn.set_on_disconnect(cb2)
        assert self.conn._on_disconnect is cb2


class TestPeerConnectionSend:
    """send() serialises a length-prefixed frame and writes to the socket."""

    def setup_method(self):
        self.sock = MockSocket()
        self.conn = PeerConnection("peer-1", "Peer One", self.sock)

    def test_send_returns_true_for_mock_socket(self):
        """With a cooperative socket, send() returns True."""
        assert self.conn.send(b"test payload") is True


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    """Protocol constants are what the rest of the stack expects."""

    def test_max_frame_size_is_10_mb(self):
        assert MAX_FRAME_SIZE == 10 * 1024 * 1024

    def test_frame_header_size_is_4(self):
        assert FRAME_HEADER_SIZE == 4

    def test_data_timeout_is_positive(self):
        assert DATA_TIMEOUT > 0


# ---------------------------------------------------------------------------
# _secure_scratch_dir / _cleanup_stale_scratch
# ---------------------------------------------------------------------------

class TestSecureScratchDir:
    """_secure_scratch_dir() returns a per-user, secure scratch directory."""

    def test_returns_path_instance(self):
        path = TransportManager._secure_scratch_dir()
        assert isinstance(path, Path)

    def test_returns_existing_directory(self):
        path = TransportManager._secure_scratch_dir()
        assert path.exists()
        assert path.is_dir()

    def test_idempotent(self):
        """Calling it twice returns the same path and does not raise."""
        path1 = TransportManager._secure_scratch_dir()
        path2 = TransportManager._secure_scratch_dir()
        assert path1 == path2


class TestCleanupStaleScratch:
    """_cleanup_stale_scratch() removes leftover *.pem files."""

    def test_removes_pem_files(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            TransportManager, "_secure_scratch_dir",
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

    def test_handles_empty_scratch_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            TransportManager, "_secure_scratch_dir",
            staticmethod(lambda: tmp_path),
        )
        # tmp_path is empty — should not raise
        TransportManager._cleanup_stale_scratch()

    def test_handles_missing_scratch_dir(self, monkeypatch, tmp_path):
        missing = tmp_path / "does_not_exist"
        monkeypatch.setattr(
            TransportManager, "_secure_scratch_dir",
            staticmethod(lambda: missing),
        )
        # The method catches all exceptions internally — should not propagate
        TransportManager._cleanup_stale_scratch()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
