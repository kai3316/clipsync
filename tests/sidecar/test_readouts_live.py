"""The readouts the new pages promise, driven out of a real link.

Four readouts were added to the Tauri front after the parity audit — the
reconnect counter on a device row, the sidebar's unread chat total, the speed
test's grade, and the transfer history's summary line.  Each was closed against
fixtures: the Vue suite hands the component a payload it wrote itself, and the
Playwright suite hands the page a fixture host.  That proves the sentence is
built right; it does not prove the payload the *sidecar* sends carries the
fields the sentence is built from.

These two tests close that seam.  They start two real sidecar processes, pair
them over real TLS on loopback, and drive the same commands the desktop host
drives — then assert the readouts' inputs on the wire: that a peer which falls
off the link grows ``reconnect_attempt`` up to ``reconnect_max`` and loses it
again when it comes back; that a message the far side has not opened counts 1
in ``chat.sessions`` (the sidebar badge is the sum of exactly that field,
``App.vue:2315-2324``); that a real transfer's history row carries the status
and size the summary line adds up; and that a speed test over a connected link
finishes with a measurement rather than the zero that means it never echoed.

What these still do not establish: nothing here has seen a window.  The pixels
remain the Playwright suite's job, and neither suite has touched a second
machine, a phone, or an installed build — those stay acceptance gates, listed
in ``notes/tauri/feature-parity.md``.
"""

import time
from contextlib import ExitStack

from internal.sync.file_transfer import SPEED_TEST_CHUNKS

# The transport's own ceiling, read from the module rather than restated, so a
# change there fails this test instead of quietly making it agree with itself.
from internal.transport.connection import MAX_RECONNECT_ATTEMPTS
from tests.sidecar.test_runtime_integration import PeerProcess, configure_pair


def wait_for(peer, method, predicate, *, deadline=45.0, interval=0.05, **params):
    """Poll *method* until *predicate* holds, or fail saying so.

    ``PeerProcess.wait`` caps at ten seconds, which is right for state that
    settles immediately and too tight for a counter that waits out a reconnect
    backoff — the first retry is deliberately three seconds after the peer is
    noticed gone, and noticing is not instant either.
    """
    end = time.monotonic() + deadline
    while True:
        result = peer.call(method, **params)
        if predicate(result):
            return result
        assert time.monotonic() < end, f"Timed out after {deadline}s waiting on {method}"


def pair(left, right):
    """Walk two processes through the pairing handshake and leave them paired.

    Both sides confirm, because that is what the prompt asks of two people: the
    first confirmation is answered ``confirmed_waiting`` and pairs nothing, and
    only the second turns the pair on.  Asserting that shape here keeps the
    two-sidedness of the handshake from being mistaken for an accident.
    """
    assert left.call("pairing.start", device_id="right") == {"accepted": True}
    left_rows = wait_for(left, "devices.list", lambda d: bool(d["items"][0]["pairing_code"]))
    right_rows = wait_for(right, "devices.list", lambda d: bool(d["items"][0]["pairing_code"]))
    code = left_rows["items"][0]["pairing_code"]
    # The short authentication string is what a person compares when the code is
    # read aloud, so it has to be the same string on both screens.
    assert code == right_rows["items"][0]["pairing_code"]
    assert left_rows["items"][0]["sas"] == right_rows["items"][0]["sas"]
    first = left.call("pairing.confirm", device_id="right", code=code)
    assert first == {"paired": False, "status": "confirmed_waiting"}
    second = right.call("pairing.confirm", device_id="left", code=code)
    assert second == {"paired": True, "status": "paired"}
    wait_for(left, "devices.list", lambda d: d["items"][0]["paired"])
    wait_for(right, "devices.list", lambda d: d["items"][0]["paired"])


def open_session(left, right):
    """Open a chat session from *left* to *right* and return its id.

    Direct send: the invitation is the whole handshake.  The opener's session is
    live the moment it is minted and the peer's the moment the frame lands, so
    there is nothing between the call and an active conversation to wait on.
    """
    opened = left.call("chat.invite", peer_id="right", peer_name="right")
    session_id = opened["chat_session_id"]
    wait_for(right, "chat.sessions", lambda r: any(
        row["session_id"] == session_id and row["status"] == "active"
        for row in r["sessions"]))
    return session_id


def unread_total(peer):
    """The sidebar badge's number, computed the way `App.vue` computes it."""
    return sum(
        int(row.get("unread", 0) or 0)
        for row in peer.call("chat.sessions")["sessions"]
    )


def session_of(sessions_payload, session_id):
    """One conversation's unread count, or None when it is not listed yet."""
    for row in sessions_payload["sessions"]:
        if row["session_id"] == session_id:
            return int(row.get("unread", 0) or 0)
    return None


def test_unread_badge_and_the_reconnect_counter_over_a_real_link(tmp_path, monkeypatch):
    configure_pair(tmp_path, monkeypatch, False, "")
    left = PeerProcess(tmp_path / "left")
    right = None
    try:
        right = PeerProcess(tmp_path / "right")
        pair(left, right)
        session_id = open_session(left, right)

        # Accepting the invite is engaging with it, so the far side starts at
        # zero -- the count below is then the message's doing and nothing else.
        assert unread_total(right) == 0

        assert left.call(
            "chat.action", action="send", session_id=session_id,
            text="waiting for the far side to open this",
        )["ok"]
        wait_for(right, "chat.sessions", lambda r: any(
            row["session_id"] == session_id and row["unread"] == 1
            for row in r["sessions"]))
        assert unread_total(right) == 1
        # The sender's own copy is not waiting for anyone, so the badge on the
        # machine that typed it stays empty.
        assert unread_total(left) == 0

        assert right.call("chat.action", action="read", session_id=session_id)["ok"]
        wait_for(right, "chat.sessions", lambda r: session_of(r, session_id) == 0)
        assert unread_total(right) == 0

        # A peer that is answering carries no counter at all: the row is not
        # "retrying 0 times", it has nothing to retry.
        assert not left.call("devices.list")["items"][0].get("reconnecting")
        assert "reconnect_attempt" not in left.call("devices.list")["items"][0]

        # The peer leaves the way a machine does -- no shutdown call at all --
        # and the only thing that can tell the user a retry is in flight is the
        # counter this readout draws.
        right.terminate()
        right = None
        rows = wait_for(
            left, "devices.list",
            lambda d: bool(d["items"][0].get("reconnecting")),
            # The first retry is scheduled three seconds out, so this is a
            # generous ceiling rather than the expected wait.  Kept well under
            # the suite's own 60s per-test timeout so a stuck transport fails
            # with the line that says what it was waiting for.
            deadline=25.0,
        )
        row = rows["items"][0]
        assert row["connection_state"] != "online"
        assert row["reconnecting"] is True
        assert row["reconnect_attempt"] >= 1
        assert row["reconnect_max"] == MAX_RECONNECT_ATTEMPTS
        # Capped, not merely bounded by courtesy: the transport reports the
        # budget it will stop fast-retrying at, and never a count past it.
        assert row["reconnect_attempt"] <= row["reconnect_max"]

        # It counts rather than flags.  The transport backs off 3s between the
        # first two attempts, so a second number arriving is the retries
        # actually happening -- which is what the row's "2/10" claims to a
        # reader who is deciding whether to walk over and check the machine.
        grown = wait_for(
            left, "devices.list",
            lambda d: (d["items"][0].get("reconnect_attempt") or 0) >= 2,
            deadline=15.0,
        )
        assert grown["items"][0]["reconnect_attempt"] <= grown["items"][0]["reconnect_max"]

        right = PeerProcess(tmp_path / "right")
        back = wait_for(
            left, "devices.list",
            lambda d: d["items"][0]["paired"] and d["items"][0]["connection_state"] == "online",
            deadline=30.0,
        )
        # The counter is a state, not a log: once the peer answers, the row
        # carries no trace of the retries that got it there.
        assert "reconnecting" not in back["items"][0]
        assert not back["items"][0].get("reconnect_attempt")
        # The trust does survive the restart -- `paired` above is the row on the
        # machine that never went away -- but the *conversation* does not: chat
        # sessions live in memory (`nearby_chat.py`), so the peer that came back
        # has none.  That is shared behaviour rather than a migration gap: both
        # legacy fronts build their session list from this same store, so a
        # legacy restart forgot the same conversations.
        assert right.call("chat.sessions")["sessions"] == []
    finally:
        for peer in (right, left):
            if peer is not None:
                peer.close()


def test_speed_test_and_the_history_summary_over_a_real_link(tmp_path, monkeypatch):
    configure_pair(tmp_path, monkeypatch, False, "")
    with ExitStack() as cleanup:
        left = PeerProcess(tmp_path / "left")
        cleanup.callback(left.close)
        right = PeerProcess(tmp_path / "right")
        cleanup.callback(right.close)
        pair(left, right)

        payload = bytes(range(256)) * 4096  # 1 MiB, small enough to be quick
        source = tmp_path / "summary.bin"
        source.write_bytes(payload)
        transfer_id = left.call("transfers.send", paths=[str(source)])["transfer_id"]
        wait_for(right, "transfers.list", lambda r: any(
            row["id"] == transfer_id and row["status"] == "pending" for row in r["active"]))
        assert right.call(
            "transfers.action", action="accept", transfer_id=transfer_id)["ok"]
        finished = wait_for(right, "transfers.list", lambda r: any(
            row["id"] == transfer_id and row["status"] == "completed"
            for row in r["history"]))
        record = next(row for row in finished["history"] if row["id"] == transfer_id)

        # The three things the summary line adds up -- how many records, how
        # they ended, how much moved -- out of a row a real transfer produced.
        # `size` is the byte total it sums, so it has to be the file's own.
        assert record["status"] == "completed"
        assert record["size"] == len(payload)
        assert record["filename"] == "summary.bin"

        # A speed test needs a connected peer and refuses without one, so the
        # link this test just used is the precondition for the reading.
        assert left.call("transfers.speed_test")["test_id"]
        completed = wait_for(
            left, "transfers.list",
            lambda r: r["speed_test"]["state"] == "done",
            deadline=60.0, interval=0.2,
        )
        speed = completed["speed_test"]
        assert speed["chunks_sent"] == speed["total_chunks"] == SPEED_TEST_CHUNKS
        assert speed["done"] is True
        # A zero here is not a slow link: it is the run that never heard its own
        # echo back, which the readout renders as a failure rather than as
        # "0 MB/s".  Over loopback with a paired peer the echo arrives, so this
        # asserts the branch that shows a number.
        assert speed["result_mbps"] > 0
        assert speed["mbps"] == speed["result_mbps"]
