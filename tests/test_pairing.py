"""Tests for PairingManager — identity, peer management, pairing codes."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.pairing import (
    MAX_PAIRING_ATTEMPTS,
    PAIRING_CODE_LENGTH,
    CertificateChangedError,
    PairingManager,
    fingerprint_pem,
    fingerprint_short,
)


class TestDeviceIdentity:
    def test_create_new_identity(self):
        mgr = PairingManager("device-a", "Test A")
        identity = mgr.load_or_create_identity("", "")
        assert identity.device_id == "device-a"
        assert identity.device_name == "Test A"
        assert identity.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert identity.certificate_pem.startswith("-----BEGIN CERTIFICATE-----")
        assert len(identity.fingerprint) > 0
        assert "..." in identity.fingerprint_short

    def test_load_existing_identity(self):
        mgr1 = PairingManager("device-b", "Test B")
        id1 = mgr1.load_or_create_identity("", "")

        mgr2 = PairingManager("device-b", "Test B")
        id2 = mgr2.load_or_create_identity(id1.private_key_pem, id1.certificate_pem)
        assert id2.private_key_pem == id1.private_key_pem
        assert id2.certificate_pem == id1.certificate_pem
        assert id2.fingerprint == id1.fingerprint

    def test_get_identity_before_load(self):
        mgr = PairingManager("dev", "name")
        with pytest.raises(RuntimeError):
            mgr.get_identity()

    def test_fingerprint_format(self):
        mgr = PairingManager("dev", "name")
        identity = mgr.load_or_create_identity("", "")
        # Full fingerprint: colon-separated hex, 64 chars for SHA-256
        parts = identity.fingerprint.split(":")
        assert len(parts) == 32  # SHA-256 = 32 bytes = 64 hex chars
        assert all(len(p) == 2 for p in parts)

        # Short fingerprint
        short = identity.fingerprint_short
        assert "..." in short
        assert len(short) == 19  # 8 + 3 + 8


class TestPeerManagement:
    def test_add_peer(self):
        mgr = PairingManager("self", "self-name")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer-1", "Peer One", identity.certificate_pem, paired=True)
        assert mgr.is_peer_paired("peer-1")
        assert mgr.get_peer_certificate("peer-1") == identity.certificate_pem

    def test_add_peer_unpaired(self):
        mgr = PairingManager("self", "self-name")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer-2", "Peer Two", identity.certificate_pem, paired=False)
        assert not mgr.is_peer_paired("peer-2")

    def test_certificate_change_detection(self):
        """Certificate change for a paired peer should raise CertificateChangedError."""
        mgr1 = PairingManager("peer-a", "Peer A")
        id1 = mgr1.load_or_create_identity("", "")

        mgr2 = PairingManager("peer-a", "Peer A")
        id2 = mgr2.load_or_create_identity("", "")  # different key

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", id1.certificate_pem, paired=True)

        with pytest.raises(CertificateChangedError) as exc_info:
            host.add_peer("peer-a", "Peer A", id2.certificate_pem, paired=True)
        assert "Certificate" in str(exc_info.value)

    def test_verify_fingerprint(self):
        mgr = PairingManager("self", "self")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer", "Peer", identity.certificate_pem, paired=True)

        # Correct fingerprint
        assert mgr.verify_peer_fingerprint("peer", identity.fingerprint)
        # With removed colons
        assert mgr.verify_peer_fingerprint("peer", identity.fingerprint.replace(":", ""))
        # Wrong fingerprint
        assert not mgr.verify_peer_fingerprint("peer", "a" * 64)
        # Unknown peer
        assert not mgr.verify_peer_fingerprint("unknown", identity.fingerprint)

    def test_remove_peer(self):
        mgr = PairingManager("self", "self")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer", "Peer", identity.certificate_pem)
        assert "peer" in [p.device_id for p in mgr.get_known_peers()]

        mgr.remove_peer("peer")
        assert "peer" not in [p.device_id for p in mgr.get_known_peers()]

    def test_get_paired_peers(self):
        mgr = PairingManager("self", "self")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("p1", "P1", identity.certificate_pem, paired=True)
        mgr.add_peer("p2", "P2", identity.certificate_pem, paired=False)

        paired = mgr.get_paired_peers()
        assert len(paired) == 1
        assert paired[0].device_id == "p1"

        known = mgr.get_known_peers()
        assert len(known) == 2


class TestPairingCode:
    def test_generate_code_format(self):
        mgr = PairingManager("self", "self")
        code = mgr.generate_pairing_code("peer-x")
        assert len(code) == PAIRING_CODE_LENGTH
        assert code.isdigit()
        assert 0 <= int(code) <= 99999999

    def test_confirm_pairing_success(self):
        mgr = PairingManager("self", "self")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer", "Peer", identity.certificate_pem, paired=False)

        code = mgr.generate_pairing_code("peer")
        assert mgr.confirm_pairing("peer", code)
        assert mgr.is_peer_paired("peer")

    def test_confirm_pairing_wrong_code(self):
        mgr = PairingManager("self", "self")
        mgr.generate_pairing_code("peer")
        assert not mgr.confirm_pairing("peer", "00000000")

    def test_confirm_pairing_unknown_peer(self):
        mgr = PairingManager("self", "self")
        assert not mgr.confirm_pairing("ghost", "12345678")

    def test_reject_pairing(self):
        mgr = PairingManager("self", "self")
        mgr.generate_pairing_code("peer")
        mgr.reject_pairing("peer")
        # After rejection, can't confirm
        assert not mgr.confirm_pairing("peer", "anycode")

    def test_get_pending_pairings(self):
        mgr = PairingManager("self", "self")
        mgr.generate_pairing_code("peer-1")
        mgr.generate_pairing_code("peer-2")

        pending = mgr.get_pending_pairings()
        assert len(pending) == 2
        peer_ids = [p[0] for p in pending]
        assert "peer-1" in peer_ids
        assert "peer-2" in peer_ids
        # Codes should be 8-digit strings
        for pid, code, _name, _status in pending:
            assert len(code) == 8
            assert code.isdigit()

    def test_rate_limiting(self):
        """After MAX_PAIRING_ATTEMPTS wrong guesses, pairing should be blocked."""
        mgr = PairingManager("self", "self")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer", "Peer", identity.certificate_pem, paired=False)
        mgr.generate_pairing_code("peer")

        # Exhaust all attempts with wrong codes
        for i in range(MAX_PAIRING_ATTEMPTS):
            assert not mgr.confirm_pairing("peer", f"9999999{i}")

        # Now even the correct code should fail
        pending = mgr.get_pending_pairings()
        if pending:
            correct_code = pending[0][1]
            assert not mgr.confirm_pairing("peer", correct_code)


class TestFingerprintHelpers:
    def test_fingerprint_pem(self):
        mgr = PairingManager("dev", "name")
        identity = mgr.load_or_create_identity("", "")
        fp = fingerprint_pem(identity.certificate_pem)
        # Should match the identity's own fingerprint
        assert fp == identity.fingerprint

    def test_fingerprint_short(self):
        mgr = PairingManager("dev", "name")
        identity = mgr.load_or_create_identity("", "")
        short = fingerprint_short(identity.certificate_pem)
        assert "..." in short
        assert short == identity.fingerprint_short


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
