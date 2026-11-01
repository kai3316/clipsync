"""Relay delivery regressions: a send nobody acked goes back on the queue.

The review's 中-2: the offline queue only covered "the local relay is down",
not "the peer is down".  A clip published while the peer was asleep, rebooting
or between brokers was accepted by the broker at QoS 0, so its ack window
elapsed and the row became a permanent ``failed`` — the peer coming back never
got it.  These tests pin the new shape: at the end of the window the row (with
its exact frame bytes) is requeued and survives a restart, the normal retry
triggers republish it, and only a spent retry budget makes it final.
"""

from __future__ import annotations

from types import SimpleNamespace

from internal.infrastructure.persistence.relay_delivery import RelayDeliveryQueue
from internal.infrastructure.runtime.relay_delivery import (
    ACK_WINDOW,
    MAX_RETRIES,
    RelayDelivery,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def delivery_rig(tmp_path, ok=True):
    state = SimpleNamespace(
        ok=ok,
        clock=Clock(),
        events=[],
        published=[],
    )

    def publish(frame, peer_id):
        state.published.append((peer_id, frame))
        return state.ok

    def notify(peer_id, msg_id, status, content_hash, kind, session_id):
        state.events.append((peer_id, msg_id, status))

    queue = RelayDeliveryQueue(tmp_path / "relay_pending.json")
    state.queue = queue
    state.path = queue.path
    state.unit = RelayDelivery(queue, publish, notify, clock=state.clock)
    return state


def statuses(unit, peer_id=""):
    return [row["status"] for row in unit.status(peer_id)["items"]]


def test_an_unacked_send_is_requeued_with_its_frame_instead_of_failing(tmp_path):
    rig = delivery_rig(tmp_path)
    rig.unit.note_sent("peer", "m1", "hash-1", "hello", frame=b"frame-bytes")
    assert statuses(rig.unit) == ["sent"]

    rig.clock.advance(ACK_WINDOW + 1)
    rig.unit.scan()

    assert statuses(rig.unit) == ["queued"]
    assert rig.unit.status()["pending"] == 1
    # The exact bytes, on disk: a retry cannot rebuild a frame it no longer has.
    assert b"frame-bytes".hex()[:8] in rig.path.read_text(encoding="utf-8") or (
        "ZnJhbWUtYnl0ZXM=" in rig.path.read_text(encoding="utf-8")
    )
    assert [event[2] for event in rig.events] == ["sent", "queued"]

    # The ordinary trigger: the peer is back / relay online -> republish.
    rig.unit.retry_all()
    assert rig.published == [("peer", b"frame-bytes")]
    assert statuses(rig.unit) == ["sent"]


def test_a_requeued_send_survives_a_restart(tmp_path):
    rig = delivery_rig(tmp_path)
    rig.unit.note_sent("peer", "m1", "hash-1", "hello", frame=b"frame-bytes")
    rig.clock.advance(ACK_WINDOW + 1)
    rig.unit.scan()

    reopened = RelayDelivery(
        RelayDeliveryQueue(rig.path), lambda frame, peer_id: True, lambda *args: None
    )

    assert statuses(reopened) == ["queued"]
    assert reopened.status()["items"][0]["content_hash"] == "hash-1"
    assert reopened.status()["items"][0]["preview"] == "hello"


def test_a_send_whose_frame_was_not_kept_still_fails_at_the_window(tmp_path):
    """The fallback for a caller that hands the ledger no bytes to retry with.

    The runtime path always supplies them; this pins that an older/other caller
    is reported as failed rather than left ``sent`` forever.
    """
    rig = delivery_rig(tmp_path)
    rig.unit.note_sent("peer", "m1", "hash-1", "hello")

    rig.clock.advance(ACK_WINDOW + 1)
    rig.unit.scan()

    assert statuses(rig.unit) == ["failed"]
    assert rig.unit.status()["pending"] == 0


def test_a_peer_that_never_acks_fails_after_the_retry_budget(tmp_path):
    rig = delivery_rig(tmp_path)  # publish succeeds, but no ack ever arrives
    rig.unit.note_sent("peer", "m1", "hash-1", "hello", frame=b"frame")

    for _ in range(MAX_RETRIES):
        rig.clock.advance(ACK_WINDOW + 1)
        rig.unit.scan()
        assert statuses(rig.unit) == ["queued"]
        rig.unit.retry_peer("peer")
        assert statuses(rig.unit) == ["sent"]

    rig.clock.advance(ACK_WINDOW + 1)
    rig.unit.scan()

    assert statuses(rig.unit) == ["failed"]
    assert rig.unit.status()["pending"] == 0
    assert len(rig.published) == MAX_RETRIES
    # The transition into "failed" is reported once, not on every sweep after.
    assert [event[2] for event in rig.events].count("failed") == 1


def test_an_acked_send_is_not_requeued(tmp_path):
    rig = delivery_rig(tmp_path)
    rig.unit.note_sent("peer", "m1", "hash-1", "hello", frame=b"frame")
    rig.unit.note_ack("peer", "m1")

    rig.clock.advance(ACK_WINDOW * 5)
    rig.unit.scan()

    assert statuses(rig.unit) == ["delivered"]
    assert rig.unit.status()["pending"] == 0
    assert rig.published == []


def test_a_redelivery_of_delivered_content_is_delivered_not_requeued(tmp_path):
    rig = delivery_rig(tmp_path)
    rig.unit.note_sent("peer", "m1", "same-hash", "hello", frame=b"frame")
    rig.unit.note_ack("peer", "m1")
    rig.unit.note_sent("peer", "m2", "same-hash", "hello", frame=b"frame")

    rig.clock.advance(ACK_WINDOW + 1)
    rig.unit.scan()

    assert statuses(rig.unit) == ["delivered", "delivered"]
    assert rig.unit.status()["pending"] == 0
