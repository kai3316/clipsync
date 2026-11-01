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
    PAIRING_STATUS_EXPIRED,
    PAIRING_STATUS_NONE,
    PAIRING_STATUS_PAIRED,
    PAIRING_STATUS_PEER_CONFIRMED,
    PAIRING_STATUS_PENDING,
    CertificateChangedError,
    PairingManager,
    fingerprint_pem,
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

    def test_update_on_unpaired_peer_does_not_restore_trust(self):
        """中-24 / 严重-8: re-trusting must not resurrect an ended pairing.

        The old version set ``paired = True`` and re-pinned whatever certificate
        it was handed, so answering a certificate prompt after the peer had
        declared an unpair put this side back into a pairing the peer had
        already ended — the peer then re-offered a code and this side swallowed
        its confirmation (``_adopt_peer_pairing`` returns early for a paired
        device), leaving the two ends stuck for the request's whole lifetime.
        """
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-c", "Peer C", old_cert, paired=False)

        assert host.update_peer_certificate("peer-c", new_cert) is False
        assert not host.is_peer_paired("peer-c")
        # Nothing was written: the old pinned value is untouched.
        assert host.get_peer_certificate("peer-c") == old_cert
        assert host.get_peer_fingerprint("peer-c") == fingerprint_pem(old_cert)

    def test_update_after_the_peer_declared_an_unpair_is_a_no_op(self):
        """The concrete 中-24 sequence: paired -> peer unpairs -> re-trust."""
        old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", old_cert, paired=True)
        host.mark_peer_unpaired("peer-a")
        assert not host.is_peer_paired("peer-a")

        assert host.update_peer_certificate("peer-a", new_cert) is False
        assert not host.is_peer_paired("peer-a")
        assert host.get_peer_certificate("peer-a") == old_cert

    def test_update_with_the_same_fingerprint_is_a_no_op(self):
        """严重-8: "trust again" on an unchanged certificate must do nothing.

        A copied PEM is the same certificate the machine already pins, so the
        alert it raises (see the accept path) cannot be answered by re-pinning:
        the value would be identical.  Returning False is what lets the owner
        answer "the certificate did not change" instead of silently pretending
        the trust decision worked.
        """
        old_cert, _new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", old_cert, paired=True)
        before = host.get_peer_fingerprint("peer-a")

        assert host.update_peer_certificate("peer-a", old_cert) is False
        assert host.is_peer_paired("peer-a")
        assert host.get_peer_fingerprint("peer-a") == before

    def test_update_with_an_empty_certificate_is_refused(self):
        old_cert, _new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", old_cert, paired=True)
        assert host.update_peer_certificate("peer-a", "") is False
        assert host.get_peer_certificate("peer-a") == old_cert

    def test_update_on_a_paired_peer_that_lost_its_pin_still_pins(self):
        """The historical "paired but no certificate" state is repairable.

        ``restore_peer``/an old config can leave a paired peer with an empty
        pin; a genuine re-trust is exactly how that state acquires one.  It is
        the only case where ``update_peer_certificate`` may write a pin without
        a fingerprint to compare against.
        """
        _old_cert, new_cert = _two_certs()

        host = PairingManager("host", "Host")
        host.add_peer("peer-a", "Peer A", "", paired=True)
        assert host.update_peer_certificate("peer-a", new_cert) is True
        assert host.is_peer_paired("peer-a")
        assert host.get_peer_fingerprint("peer-a") == fingerprint_pem(new_cert)


class TestTerminalPairingStatuses:
    """The end of a request is told once, then cleared.

    The manager owns no event bus, so a terminal status (``cancelled`` /
    ``expired``) is queued for its owner; the owner drains it, publishes
    ``pairing.resolved`` and refreshes.  Before this queue the status stayed on
    the peer's row forever (the device list keeps any row whose status is
    non-empty), so a rejected/expired device that had left the network was
    still listed until a restart, and nothing ever said the request had ended.
    """

    def _mgr(self) -> PairingManager:
        return PairingManager("device-a", "Device A")

    def test_a_rejection_is_reported_once_then_cleared(self):
        mgr = self._mgr()
        mgr.reject_pairing("peer-b")
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_CANCELLED
        assert mgr.drain_terminal_statuses() == [("peer-b", PAIRING_STATUS_CANCELLED)]
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_NONE
        assert mgr.drain_terminal_statuses() == []

    def test_an_expiry_is_reported_once_then_cleared(self):
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-b")
        # Age the request past PAIRING_TIMEOUT; the expiry runs on the next read.
        with mgr._lock:
            mgr._pending_pairings["peer-b"] = (code, time.time() - 400)
        assert mgr.get_pending_pairings() == []
        assert mgr.drain_terminal_statuses() == [("peer-b", PAIRING_STATUS_EXPIRED)]
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_NONE

    def test_a_peer_declared_unpair_is_reported(self):
        mgr = self._mgr()
        mgr.mark_peer_unpaired("peer-b")
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_CANCELLED
        assert mgr.drain_terminal_statuses() == [("peer-b", PAIRING_STATUS_CANCELLED)]

    def test_a_new_request_cancels_the_queued_terminal(self):
        """A later request is a new lifecycle, not the old one's ending."""
        mgr = self._mgr()
        mgr.reject_pairing("peer-b")
        mgr.generate_pairing_code("peer-b")
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_PENDING
        assert mgr.drain_terminal_statuses() == []

    def test_a_chat_drop_is_not_an_answer(self):
        """``discard_pending_pairing`` is "this was not a pairing", not a no.

        A chat invite that auto-generated a code must not surface as a prompt,
        but it also must not tell the user the pairing was cancelled — nothing
        was asked and nothing was answered.
        """
        mgr = self._mgr()
        mgr.generate_pairing_code("peer-b")
        mgr.discard_pending_pairing("peer-b")
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_NONE
        assert mgr.drain_terminal_statuses() == []

    def test_removing_the_peer_forgets_the_queued_terminal(self):
        """A removed device has no row left to resolve — no event for it."""
        mgr = self._mgr()
        mgr.reject_pairing("peer-b")
        mgr.remove_peer("peer-b")
        assert mgr.drain_terminal_statuses() == []


class TestAPeerCertificateChangeDuringPairing:
    """中-25: the pending code is derived from the certificates, so it follows them.

    The derivation is deterministic *for a pair of certificates*.  A peer that
    reissues its identity (reset, reinstall, lost key) while a request is open
    derives a different code, and this side used to keep displaying the cached
    old one: two different 8-digit codes on two screens, a confirmation that
    could only mismatch, and five rate-limited attempts spent proving it.
    """

    def _host_with_peer(self, certificate_pem):
        host = PairingManager("host", "Host")
        host.load_or_create_identity("", "")
        host.add_peer("peer", "Peer", certificate_pem, paired=False)
        return host

    @staticmethod
    def _fresh_certificate():
        peer = PairingManager("peer", "Peer")
        return peer.load_or_create_identity("", "").certificate_pem

    @staticmethod
    def _derived(host, peer_certificate):
        import hashlib

        fps = sorted([host.get_identity().fingerprint, fingerprint_pem(peer_certificate)])
        digest = hashlib.sha256((fps[0] + fps[1]).encode()).hexdigest()
        return str(int(digest[:10], 16) % 10**PAIRING_CODE_LENGTH).zfill(PAIRING_CODE_LENGTH)

    def test_a_certificate_change_updates_the_code_on_the_row(self):
        host = self._host_with_peer(self._fresh_certificate())
        first = host.generate_shared_pairing_code("peer")
        assert host.get_pending_pairings()[0][1] == first

        new_certificate = self._fresh_certificate()
        assert host.update_peer_certificate("peer", new_certificate) is False
        # The pin of an unpaired peer is replaced by the handshake itself, not
        # by update_peer_certificate:
        host.add_peer("peer", "Peer", new_certificate, paired=False)
        second = host.generate_shared_pairing_code("peer")

        assert second != first
        assert second == self._derived(host, new_certificate)
        # What the user reads off the row is the new code, not the cached one.
        assert host.get_pending_pairings()[0][1] == second

    def test_a_certificate_change_restarts_the_timeout(self):
        host = self._host_with_peer(self._fresh_certificate())
        host.generate_shared_pairing_code("peer")
        with host._lock:
            old_code, _old_ts = host._pending_pairings["peer"]
            host._pending_pairings["peer"] = (old_code, time.time() - 400)

        new_certificate = self._fresh_certificate()
        host.add_peer("peer", "Peer", new_certificate, paired=False)
        fresh_code = host.generate_shared_pairing_code("peer")

        # A request that had already run out of time came back to life with the
        # new certificate rather than staying expired.
        assert host.get_pending_pairings()[0][1] == fresh_code
        with host._lock:
            assert time.time() - host._pending_pairings["peer"][1] < 60

    def test_a_certificate_change_clears_the_rate_limit(self):
        host = self._host_with_peer(self._fresh_certificate())
        first = host.generate_shared_pairing_code("peer")
        for _ in range(MAX_PAIRING_ATTEMPTS):
            assert host.confirm_pairing("peer", "99999999") is False
        # Rate-limited even for the right code now.
        assert host.confirm_pairing("peer", first) is False

        new_certificate = self._fresh_certificate()
        host.add_peer("peer", "Peer", new_certificate, paired=False)
        second = host.generate_shared_pairing_code("peer")
        # A different certificate is a different guess surface: the user can
        # confirm the new code without waiting out a window opened against the
        # old one.
        assert host.confirm_pairing("peer", second) is True

    def test_a_certificate_change_returns_a_half_confirmed_request_to_pending(self):
        host = self._host_with_peer(self._fresh_certificate())
        first = host.generate_shared_pairing_code("peer")
        assert host.confirm_pairing("peer", first) is True
        assert host.get_pairing_status("peer") == PAIRING_STATUS_CONFIRMED_WAITING

        host.add_peer("peer", "Peer", self._fresh_certificate(), paired=False)
        host.generate_shared_pairing_code("peer")
        # The local confirmation was for a code derived from a certificate that
        # is no longer the peer's: the request is pending again.
        assert host.get_pairing_status("peer") == PAIRING_STATUS_PENDING


class TestAConfirmationWithNoRequest:
    """轻-9: a confirm frame answers a request, and only while there is one.

    Defaulting the missing status to ``pending`` and writing ``peer_confirmed``
    put a "the other device confirmed — now confirm the code" row on screen for
    a peer with no code to confirm (its confirm button is only usable when a
    pending entry carries a code), and only a reject cleared it.
    """

    def _mgr(self) -> PairingManager:
        mgr = PairingManager("device-a", "Device A")
        mgr.load_or_create_identity("", "")
        peer = PairingManager("peer-b", "Peer B")
        certificate = peer.load_or_create_identity("", "").certificate_pem
        mgr.add_peer("peer-b", "Peer B", certificate, paired=False)
        return mgr

    def test_a_known_peer_without_a_request_gets_no_status(self):
        mgr = self._mgr()
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_NONE
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_NONE
        assert mgr.get_pending_pairings() == []
        assert not mgr.is_peer_paired("peer-b")

    def test_an_unknown_peer_gets_no_status(self):
        mgr = self._mgr()
        assert mgr.mark_peer_confirmed("ghost") == PAIRING_STATUS_NONE
        assert mgr.get_pairing_status("ghost") == PAIRING_STATUS_NONE

    def test_a_late_confirm_after_rejection_does_not_resurrect_the_prompt(self):
        mgr = self._mgr()
        mgr.reject_pairing("peer-b")
        assert mgr.mark_peer_confirmed("peer-b") == PAIRING_STATUS_CANCELLED
        assert mgr.get_pairing_status("peer-b") == PAIRING_STATUS_CANCELLED


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

    def test_expired_confirmation_expires_the_lifecycle_status(self):
        """A timeout is its own answer, and not the peer's.

        Reading back ``cancelled`` here made a request nobody answered — the
        prompt left on screen, the clock run out — indistinguishable from one
        the other side refused, which is the one case the user has to act on
        differently (retry deliberately rather than look for a fault there).
        """
        mgr = self._mgr()
        code = mgr.generate_pairing_code("peer-c")
        # Age the request past PAIRING_TIMEOUT (300 s).
        pending_code, _ts = mgr._pending_pairings["peer-c"]
        mgr._pending_pairings["peer-c"] = (pending_code, time.time() - 400)
        assert mgr.confirm_pairing("peer-c", code) is False
        assert mgr.get_pairing_status("peer-c") == PAIRING_STATUS_EXPIRED
        assert mgr.get_pending_pairings() == []
        # A late confirm from the peer cannot revive it either: the request is
        # gone, and the row is not waiting on anybody any more.
        assert mgr.mark_peer_confirmed("peer-c") == PAIRING_STATUS_EXPIRED

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
