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


# ══════════════════════════════════════════════════
# merged from test_pairing_lifecycle.py
# ══════════════════════════════════════════════════

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.pairing import (
    PAIRING_STATUS_CANCELLED,
    PAIRING_STATUS_CONFIRMED_WAITING,
    PAIRING_STATUS_PAIRED,
    PAIRING_STATUS_PEER_CONFIRMED,
    PAIRING_STATUS_PENDING,
)


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
    assert mgr.is_peer_paired("device-b") is True
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
    assert mgr.is_peer_paired("device-b") is True

    mgr.mark_peer_unpaired("device-b")
    assert mgr.is_peer_paired("device-b") is False
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_CANCELLED
    assert mgr.get_pending_pairings() == []


def test_local_reject_cancels(mgr):
    mgr.generate_pairing_code("device-b")
    mgr.reject_pairing("device-b")
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_CANCELLED
    assert mgr.confirm_pairing("device-b", "00000000") is False


def test_pending_expiry_removes_entry(mgr):
    mgr.generate_pairing_code("device-b")
    # Force the entry's timestamp into the past so it exceeds PAIRING_TIMEOUT.
    old = time.time() - 10_000
    with mgr._lock:
        mgr._pending_pairings["device-b"] = (mgr._pending_pairings["device-b"][0], old)
    assert mgr.get_pending_pairings() == []
    assert mgr.get_pairing_status("device-b") == PAIRING_STATUS_CANCELLED


def test_get_pending_includes_status(mgr):
    mgr.generate_pairing_code("device-b")
    entries = mgr.get_pending_pairings()
    assert len(entries) == 1
    pid, code, name, status = entries[0]
    assert pid == "device-b"
    assert code
    assert name == "Test B"
    assert status == PAIRING_STATUS_PENDING


# ══════════════════════════════════════════════════
# merged from test_pairing_retrust.py
# ══════════════════════════════════════════════════

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



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


# ══════════════════════════════════════════════════
# split from test_round3_core.py — pairing confirm lifecycle
# ══════════════════════════════════════════════════

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

from internal.security.fingerprint import normalize_fingerprint, sas_code

# ══════════════════════════════════════════════════
# split from test_round11_webui.py — SAS + devices SAS
# ══════════════════════════════════════════════════

# ── 1. SAS derivation ──────────────────────────────────────────────────


def test_sas_code_is_deterministic():
    a = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
    b = "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00"
    assert sas_code(a, b) == sas_code(a, b)
    assert sas_code(a, b) == sas_code(a, b)  # stable across repeated calls


def test_sas_code_is_symmetric():
    # Both devices must derive the same string from local knowledge alone.
    assert sas_code("fp-alpha", "fp-beta") == sas_code("fp-beta", "fp-alpha")
    assert sas_code("", "xyz") == sas_code("xyz", "")


def test_sas_code_matches_reference_digest():
    # Pin the exact formula: sha256(sorted-normalized concat)[:8] as 4-4 hex.
    import hashlib
    a = normalize_fingerprint("ab:cd:ef")   # ABCDEF
    b = normalize_fingerprint("012345")     # 012345
    lo, hi = sorted([a, b])
    digest = hashlib.sha256((lo + hi).encode("ascii")).hexdigest()
    expected = digest[:8].upper()
    expected = f"{expected[:4]}-{expected[4:]}"
    assert sas_code("ab:cd:ef", "012345") == expected


def test_sas_code_format_and_normalization():
    code = sas_code("AAAA", "BBBB")
    assert len(code) == 9 and code[4] == "-"
    left, right = code.split("-")
    assert len(left) == 4 and len(right) == 4
    assert all(c in "0123456789ABCDEF" for c in left + right)
    # Colon-separated display form hashes identically to bare hex.
    assert sas_code("aa:bb", "cc:dd") == sas_code("AABB", "CCDD")
    assert sas_code("aabb", "ccdd") == sas_code("AA:BB", "ccdD")


def test_sas_code_differs_across_device_pairs():
    seen = {
        sas_code(f"device-{i}", f"peer-{j}")
        for i in range(8) for j in range(8)
    }
    # 64 distinct pairs must not collide on an 8-hex-char code.
    assert len(seen) == 64




# ── 3. Devices API surfaces the pairing SAS ────────────────────────────


def _minimal_cfg():
    class _Cfg:
        device_id = "self"
        device_name = "Self"
        peers = {}
    return _Cfg()


def test_devices_pending_pairings_tuple_with_sas():
    from internal.web.api.devices import get_devices
    pending = [("peer-abc", "12345678", "Phone", "pending", "3A2F-91C4")]
    res, status = get_devices(
        _minimal_cfg(), lambda: [], get_pending_pairings=lambda: pending)
    assert status == 200
    row = res["pending_pairings"][0]
    assert row["sas"] == "3A2F-91C4"
    assert row["code"] == "12345678"
    assert row["status"] == "pending"


def test_devices_pending_pairings_without_sas_defaults_empty():
    from internal.web.api.devices import get_devices
    pending = [("peer-abc", "12345678", "Phone", "pending")]
    res, _ = get_devices(
        _minimal_cfg(), lambda: [], get_pending_pairings=lambda: pending)
    assert res["pending_pairings"][0]["sas"] == ""
    # dict-shaped entries carry it under the same key
    res, _ = get_devices(
        _minimal_cfg(), lambda: [],
        get_pending_pairings=lambda: [{"peer_id": "p", "sas": "AAAA-BBBB"}])
    assert res["pending_pairings"][0]["sas"] == "AAAA-BBBB"

# ══════════════════════════════════════════════════
# split from test_round13_wrapup.py — Tk SAS display
# ══════════════════════════════════════════════════



def test_tk_sas_locale_keys_exist_in_desktop_i18n():
    import internal.i18n as i18n
    for key in ("devices.sas_label", "devices.sas_verify_hint"):
        assert key in i18n._EN, f"missing from _EN: {key}"
        assert key in i18n._ZH, f"missing from _ZH: {key}"
        assert i18n._EN[key].strip(), key
        assert i18n._ZH[key].strip(), key
    # Wording must match the web UI's SAS card so both surfaces agree.
    assert "Security code" in i18n._EN["devices.sas_label"]
    assert "安全代码" in i18n._ZH["devices.sas_label"]
    assert "other device" in i18n._EN["devices.sas_verify_hint"]
    assert "对方设备" in i18n._ZH["devices.sas_verify_hint"]


def test_tk_pending_row_wires_sas_display():
    import inspect

    from internal.ui import dashboard as dash

    # _create_pending_row accepts the SAS as an optional 5th arg…
    sig = inspect.signature(dash.DashboardWindow._create_pending_row)
    params = list(sig.parameters)
    assert "sas" in params
    assert sig.parameters["sas"].default == ""

    # …and renders it (plus a verify hint) when present.
    src = inspect.getsource(dash.DashboardWindow._create_pending_row)
    assert "devices.sas_label" in src
    assert "devices.sas_verify_hint" in src

    # The pending-list unpacking tolerates 4- and 5-element tuples: the 5th
    # element is the SAS appended by main.py._get_pending; expired rows carry
    # only 4.  Guard must not index past a 4-tuple.
    refresh_src = inspect.getsource(dash.DashboardWindow._refresh_devices)
    assert "len(item) > 4" in refresh_src
    assert "item[4] if len(item) > 4 else" in refresh_src


def test_sas_code_shape_via_main_same_source():
    """main.py._pairing_sas uses fingerprint.sas_code — the same derivation
    the web device API surfaces — so the Tk card shows the identical string."""
    import inspect

    import src.main as main_mod
    src = inspect.getsource(main_mod.Application._pairing_sas)
    assert "sas_code" in src
    assert "get_peer_fingerprint" in src
    assert "get_identity().fingerprint" in src
