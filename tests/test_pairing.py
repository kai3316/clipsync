"""PairingManager's trust rules.

Identity persistence, peer certificates and pinning, the pairing code with its
two-sided confirmation lifecycle, re-trust after a certificate change, and the
SAS both devices compare.  The pending-list readouts and the pure fingerprint
helpers are used by the surfaces that consume them rather than restated here.

Merged from test_pairing.py, _pairing_lifecycle.py, _pairing_retrust.py and the
confirm-lifecycle / SAS halves of test_round3_core.py, test_round11_webui.py and
test_round13_wrapup.py.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.fingerprint import normalize_fingerprint, sas_code  # noqa: E402
from internal.security.pairing import (  # noqa: E402
    MAX_PAIRING_ATTEMPTS,
    PAIRING_CODE_LENGTH,
    PAIRING_STATUS_CANCELLED,
    PAIRING_STATUS_CONFIRMED_WAITING,
    PAIRING_STATUS_PAIRED,
    PAIRING_STATUS_PEER_CONFIRMED,
    PAIRING_STATUS_PENDING,
    CertificateChangedError,
    PairingManager,
)


class TestDeviceIdentity:
    def test_load_existing_identity(self):
        mgr1 = PairingManager("device-b", "Test B")
        id1 = mgr1.load_or_create_identity("", "")

        mgr2 = PairingManager("device-b", "Test B")
        id2 = mgr2.load_or_create_identity(id1.private_key_pem, id1.certificate_pem)
        assert id2.private_key_pem == id1.private_key_pem
        assert id2.certificate_pem == id1.certificate_pem
        assert id2.fingerprint == id1.fingerprint


class TestPeerManagement:
    def test_add_peer(self):
        mgr = PairingManager("self", "self-name")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer-1", "Peer One", identity.certificate_pem, paired=True)
        assert mgr.is_peer_paired("peer-1")
        assert mgr.get_peer_certificate("peer-1") == identity.certificate_pem

    def test_add_peer_tolerates_empty_certificate(self):
        """A peer with no pinned certificate yet must survive add_peer.

        restore_peer leaves certificate_pem empty on purpose (it is re-pinned
        on the next handshake) and _save_cfg_and_peers persists that empty
        value, so the startup reload feeds "" back in.  Feeding "" to
        fingerprint_pem raised MalformedFraming, which made the startup loop
        log "Skipping peer <name>" and DROP the peer — a restored device
        silently half-vanished after a restart.
        """
        mgr = PairingManager("self", "self-name")
        mgr.load_or_create_identity("", "")

        mgr.add_peer("peer-3", "Restored Box", "", paired=False)

        ids = {p.device_id for p in mgr.get_known_peers()}
        assert "peer-3" in ids, "an unpinned peer must not be dropped"
        assert mgr.get_peer_certificate("peer-3") == ""

    def test_add_peer_empty_certificate_keeps_existing_pin(self):
        """An empty incoming PEM means "not pinned yet", never "forget the pin".

        Overwriting a real pin with "" would silently disable certificate
        pinning for an already-paired peer.
        """
        mgr = PairingManager("self", "self-name")
        identity = mgr.load_or_create_identity("", "")
        mgr.add_peer("peer-4", "Pinned Box", identity.certificate_pem, paired=True)

        mgr.add_peer("peer-4", "Pinned Box", "", paired=True)

        assert mgr.get_peer_certificate("peer-4") == identity.certificate_pem
        assert mgr.get_peer_fingerprint("peer-4")

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


class TestPairingCode:
    def test_generate_code_format(self):
        mgr = PairingManager("self", "self")
        code = mgr.generate_pairing_code("peer-x")
        assert len(code) == PAIRING_CODE_LENGTH
        assert code.isdigit()
        assert 0 <= int(code) <= 99999999

    def test_confirm_pairing_unknown_peer(self):
        mgr = PairingManager("self", "self")
        assert not mgr.confirm_pairing("ghost", "12345678")

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


@pytest.fixture
def mgr():
    m = PairingManager("device-a", "Test A")
    m.load_or_create_identity("", "")
    # A peer with its own identity so we have a real cert to register.
    peer = PairingManager("device-b", "Test B")
    peer_id = peer.load_or_create_identity("", "")
    m.add_peer("device-b", "Test B", peer_id.certificate_pem, paired=False)
    return m


def test_local_confirm_waits_for_peer(mgr):
    code = mgr.generate_pairing_code("device-b")
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_PENDING

    assert mgr.confirm_pairing("device-b", code) is True
    # Single-sided confirm awaits the peer — not paired yet.
    assert mgr.is_peer_paired("device-b") is False
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_CONFIRMED_WAITING
    # The pending entry stays so the UI can show "waiting for the other device".
    assert "device-b" in [p[0] for p in mgr.get_pending_pairings()]

    # Peer confirms → two-sided handshake completes, pending clears.
    assert mgr.mark_peer_confirmed("device-b") == PAIRING_STATUS_PAIRED
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_PAIRED
    assert mgr.get_pending_pairings() == []


def test_peer_confirms_first_then_local_confirm_completes(mgr):
    code = mgr.generate_pairing_code("device-b")

    # Peer confirmed first.
    assert mgr.mark_peer_confirmed("device-b") == PAIRING_STATUS_PEER_CONFIRMED
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_PEER_CONFIRMED

    # Local confirm now completes the pairing.
    assert mgr.confirm_pairing("device-b", code) is True
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_PAIRED
    assert mgr.get_pending_pairings() == []


def test_peer_reject_cancels(mgr):
    mgr.generate_pairing_code("device-b")
    mgr.mark_peer_rejected("device-b")
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_CANCELLED
    assert mgr.get_pending_pairings() == []
    # No confirmation can succeed afterwards.
    assert mgr.confirm_pairing("device-b", "12345678") is False


def test_peer_unpair_after_pairing(mgr):
    code = mgr.generate_pairing_code("device-b")
    mgr.confirm_pairing("device-b", code)
    assert mgr.mark_peer_confirmed("device-b") == PAIRING_STATUS_PAIRED
    assert mgr.is_peer_paired("device-b") is True

    mgr.mark_peer_unpaired("device-b")
    assert mgr.is_peer_paired("device-b") is False
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_CANCELLED
    assert mgr.get_pending_pairings() == []


def test_get_pending_includes_status(mgr):
    mgr.generate_pairing_code("device-b")
    entries = mgr.get_pending_pairings()
    assert len(entries) == 1
    pid, code, name, status = entries[0]
    assert pid == "device-b"
    assert code
    assert name == "Test B"
    assert status == PAIRING_STATUS_PENDING


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

    def test_update_on_unpaired_peer_marks_paired(self):
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-c", "Peer C", old_cert, paired=False)
        assert not host.is_peer_paired("peer-c")

        assert host.update_peer_certificate("peer-c", new_cert) is True
        assert host.is_peer_paired("peer-c")
        assert host.get_peer_certificate("peer-c") == new_cert


class TestPairingConfirmLifecycle:
    def _mgr(self) -> PairingManager:
        return PairingManager("device-a", "Device A")

    def test_duplicate_peer_confirm_keeps_paired(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-b")
        assert mgr.confirm_pairing("peer-b", code) is True
        # Peer confirms second -> handshake completes.
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_PAIRED
        # A duplicate / late pairing_confirm (reconnect storm re-delivery)
        # must NOT regress the completed handshake to peer_confirmed.
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_PAIRED
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_PAIRED

    def test_expired_confirmation_cancels_lifecycle_status(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-c")
        # Age the request past PAIRING_TIMEOUT (300 s).
        pending_code, _ts = mgr._pending_pairings["peer-c"]
        mgr._pending_pairings["peer-c"] = (pending_code, time.time() - 400)
        assert mgr.confirm_pairing("peer-c", code) is False
        assert mgr.get_pairing_status("peer-c") == PAIRING_STATUS_CANCELLED
        assert mgr.get_pending_pairings() == []

    def test_wrong_code_then_correct_within_window(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-d")
        assert mgr.confirm_pairing("peer-d", "00000000") is False
        assert mgr.confirm_pairing("peer-d", code) is True


def test_sas_code_is_symmetric():
    # Both devices must derive the same string from local knowledge alone.
    assert sas_code("fp-alpha", "fp-beta") == sas_code("fp-beta", "fp-alpha")
    assert sas_code("", "xyz") == sas_code("xyz", "")


def test_sas_code_matches_reference_digest():
    # Pin the exact formula: sha256(sorted-normalized concat)[:8] as 4-4 hex.
    import hashlib

    a = normalize_fingerprint("ab:cd:ef")  # ABCDEF
    b = normalize_fingerprint("012345")  # 012345
    lo, hi = sorted([a, b])
    digest = hashlib.sha256((lo + hi).encode("ascii")).hexdigest()
    expected = digest[:8].upper()
    expected = f"{expected[:4]}-{expected[4:]}"
    assert sas_code("ab:cd:ef", "012345") == expected


def _minimal_cfg():
    class _Cfg:
        device_id = "self"
        device_name = "Self"
        peers = {}

    return _Cfg()


def test_devices_pending_pairings_tuple_with_sas():
    from internal.web.api.devices import get_devices

    pending = [("peer-abc", "12345678", "Phone", "pending", "3A2F-91C4")]
    res, status = get_devices(_minimal_cfg(), lambda: [], get_pending_pairings=lambda: pending)
    assert status == 200
    row = res["pending_pairings"][0]
    assert row["sas"] == "3A2F-91C4"
    assert row["code"] == "12345678"
    assert row["status"] == "pending"
