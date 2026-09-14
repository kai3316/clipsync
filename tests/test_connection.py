"""The transport's wire constants, its send contract, and scratch hygiene.

``MockPairingManager`` also lives here: the TCP/TLS lifecycle suite
(tests/sidecar/test_transport_lifecycle.py) imports it to stand a real
``TransportManager`` up without a real pairing store.
"""

import os
import sys

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
