"""Tests for the two-sided pairing lifecycle state machine.

Confirmation is a two-sided commitment: a local confirm puts the peer into
``confirmed_waiting`` until the peer's ``pairing_confirm`` arrives (or it
rejects / un-pairs).  These tests cover every transition and the expiry path.
"""

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
    PairingManager,
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
