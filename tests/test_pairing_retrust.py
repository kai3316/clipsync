"""Tests for PairingManager.update_peer_certificate — re-trust after a device
reinstall/reset presents a new certificate."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.pairing import (
    CertificateChangedError,
    PairingManager,
)


def _two_certs():
    """Return (old_cert, new_cert) for the same device with different keys."""
    mgr1 = PairingManager("peer-a", "Peer A")
    old = mgr1.load_or_create_identity("", "")
    mgr2 = PairingManager("peer-a", "Peer A")
    new = mgr2.load_or_create_identity("", "")  # freshly generated key/cert
    return old.certificate_pem, new.certificate_pem


class TestUpdatePeerCertificate:
    def test_update_replaces_cert_and_keeps_paired(self):
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", old_cert, paired=True)
        assert host.get_peer_certificate("peer-a") == old_cert

        # Re-trust: pin the new certificate, keep the peer paired.
        assert host.update_peer_certificate("peer-a", new_cert) is True
        assert host.is_peer_paired("peer-a")
        assert host.get_peer_certificate("peer-a") == new_cert

        # A follow-up add_peer with the NEW cert must NOT raise.
        host.add_peer("peer-a", "Peer A", new_cert, paired=True)

        # A follow-up add_peer with the OLD cert must now be treated as a
        # change and rejected.
        with pytest.raises(CertificateChangedError):
            host.add_peer("peer-a", "Peer A", old_cert, paired=True)

    def test_update_unknown_peer_returns_false(self):
        host = PairingManager("host", "Host")
        other = PairingManager("x", "X")
        ident = other.load_or_create_identity("", "")
        assert host.update_peer_certificate("nope", ident.certificate_pem) is False

    def test_update_keeps_name_and_updates_fingerprint(self):
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", old_cert, paired=True)
        host.update_peer_certificate("peer-a", new_cert)

        known = host.get_known_peers()
        peer = next(p for p in known if p.device_id == "peer-a")
        assert peer.device_name == "Peer A"
        assert peer.paired is True
        assert peer.fingerprint == host.get_peer_fingerprint("peer-a")
        # New certificate verifies; the old one no longer does.
        assert host.verify_peer_fingerprint("peer-a", peer.fingerprint)
        assert not host.verify_peer_fingerprint("peer-a", old_cert)

    def test_update_makes_old_cert_a_change(self):
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-b", "Peer B", old_cert, paired=True)
        host.update_peer_certificate("peer-b", new_cert)

        with pytest.raises(CertificateChangedError):
            host.add_peer("peer-b", "Peer B", old_cert, paired=True)

    def test_update_on_unpaired_peer_marks_paired(self):
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-c", "Peer C", old_cert, paired=False)
        assert not host.is_peer_paired("peer-c")

        assert host.update_peer_certificate("peer-c", new_cert) is True
        assert host.is_peer_paired("peer-c")
        assert host.get_peer_certificate("peer-c") == new_cert

    def test_update_is_lock_safe(self):
        """update_peer_certificate can be called from multiple threads without
        corrupting the peer entry (basic concurrency smoke test)."""
        import threading

        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", old_cert, paired=True)

        errors = []

        def _update():
            try:
                host.update_peer_certificate("peer-a", new_cert)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=_update) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert host.is_peer_paired("peer-a")
        assert host.get_peer_certificate("peer-a") == new_cert


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
