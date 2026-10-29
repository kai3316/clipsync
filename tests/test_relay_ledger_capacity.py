"""A send waiting for its ack is never deleted to make room for a newer one.

The failure this holds shut: ``note_sent`` calls ``_bounded``, and when the
ledger was at ``LEDGER_MAX`` (50) it dropped the oldest row with
``popitem(last=False)`` — frame and all.  A row dropped while still ``sent``
had not been delivered, was not in the offline queue, and would never be found
by ``_mark_timeout``: it left the timeout sweep, the retry path and the UI
together, so the send became invisible rather than failed.  A burst of 51
un-acked sends reproduced it; no crash required.
"""

from __future__ import annotations

from types import SimpleNamespace

from internal.infrastructure.persistence.relay_delivery import RelayDeliveryQueue
from internal.infrastructure.runtime.relay_delivery import (
    ACK_WINDOW,
    LEDGER_MAX,
    RelayDelivery,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def rig(tmp_path, ok=True):
    state = SimpleNamespace(ok=ok, clock=Clock(), events=[], published=[])
    state.queue = RelayDeliveryQueue(tmp_path / "relay_pending.json")
    state.unit = RelayDelivery(
        state.queue,
        lambda frame, peer_id: state.published.append((peer_id, frame)) or state.ok,
        lambda peer_id, msg_id, status, *rest: state.events.append((msg_id, status)),
        clock=state.clock,
    )
    return state


def send_burst(unit, count, peer="peer-1"):
    for index in range(count):
        unit.note_sent(
            peer, f"m{index}", content_hash=f"h{index}", preview=f"clip {index}",
            frame=f"frame-{index}".encode(),
        )


def test_a_burst_past_the_cap_keeps_every_unsettled_send(tmp_path):
    state = rig(tmp_path)
    send_burst(state.unit, LEDGER_MAX + 1)

    # The very first send is the one the cap used to delete.  It is still a
    # tracked send: it just has not been acked yet.
    assert not state.queue.has("peer-1", "m0")  # not queued yet — still in flight
    assert all(
        status != "failed" for _msg_id, status in state.events
    ), "no send may be reported failed just because the ledger is full"


def test_the_burst_settles_into_the_queue_not_into_oblivion(tmp_path):
    state = rig(tmp_path, ok=True)
    send_burst(state.unit, LEDGER_MAX + 1)

    # The ack window elapses for all of them at once.
    state.clock.advance(ACK_WINDOW + 1)
    state.unit.scan()

    queued = state.queue.counts().get("peer-1", 0)
    assert queued == LEDGER_MAX + 1, (
        "every un-acked send must be on the retry queue; "
        f"{LEDGER_MAX + 1 - queued} of them are unaccounted for"
    )
    for index in range(LEDGER_MAX + 1):
        assert state.queue.has("peer-1", f"m{index}"), f"m{index} was dropped"

    # And each of them was reported as queued — the UI can show them.
    queued_events = [msg_id for msg_id, status in state.events if status == "queued"]
    assert set(queued_events) == {f"m{index}" for index in range(LEDGER_MAX + 1)}


def test_the_oldest_send_survives_a_settled_neighbour(tmp_path):
    """Eviction still happens — but only for rows that are finished."""
    state = rig(tmp_path)
    # One settled row first, then enough in-flight sends to force eviction.
    state.unit.note_sent("peer-1", "done", content_hash="h-done", frame=b"done")
    state.unit.note_ack("peer-1", "done")
    send_burst(state.unit, LEDGER_MAX)

    state.clock.advance(ACK_WINDOW + 1)
    state.unit.scan()

    rows = {row["msg_id"]: row["status"] for row in state.unit.status("peer-1")["items"]}
    # The settled row is history and may be evicted; nothing in flight may be.
    assert rows.get("done") != "sent"
    assert len(rows) <= LEDGER_MAX
    assert state.queue.counts().get("peer-1", 0) == LEDGER_MAX


def test_a_long_run_of_settled_sends_keeps_the_ledger_bounded(tmp_path):
    state = rig(tmp_path)
    for index in range(LEDGER_MAX * 3):
        state.unit.note_sent("peer-1", f"m{index}", content_hash=f"h{index}", frame=b"x")
        state.unit.note_ack("peer-1", f"m{index}")

    assert not state.queue.counts(), "an acked send must leave the queue alone"
    assert len(state.unit.status("peer-1")["items"]) <= LEDGER_MAX
