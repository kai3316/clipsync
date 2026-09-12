"""Relay send ledger: ACK window, retry budget, triggers and queue caps.

The legacy host tracked every relayed clipboard frame in an in-memory ledger
(sent → delivered / failed, with the offline queue mirrored in as queued) and
retried the persisted queue on relay-online, on any frame from that peer and
on a 60s timer.  These tests pin that behavior on the sidecar runtime: what
the UI reports, when a frame is retried, and that a tracked send is never
dropped silently.
"""

import time
from types import SimpleNamespace

import pytest

from internal.application.events import EventJournal
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.config.config import Config, PeerInfo
from internal.infrastructure.persistence.relay_delivery import RelayDeliveryQueue
from internal.infrastructure.runtime.lan import LanRuntime
from internal.infrastructure.runtime.relay_delivery import (
    ACK_WINDOW,
    MAX_RETRIES,
    RETRY_INTERVAL,
    SCAN_INTERVAL,
    RelayDelivery,
)
from internal.protocol.codec import decode_message, encode_frame, encode_message
from internal.sync.nearby_chat import ChatManager
from internal.transport.relay import (
    derive_key,
    derive_topic,
    generate_netpair_code,
    generate_netpair_secret,
    netpair_device_tag,
    netpair_key,
    netpair_topic,
)
from tests.sidecar.test_lan_runtime import (
    Clipboard,
    Discovery,
    History,
    Monitor,
    Transport,
    events_named,
    identity,
)


class Clock:
    """Injectable wall clock so the ACK window and cadences are deterministic."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def delivery_rig(tmp_path):
    """A ledger over a real (isolated) queue file with a scripted broker."""
    published: dict[str, list] = {}
    events: list[tuple] = []
    state = SimpleNamespace(ok=True, clock=Clock(), events=events, published=published)

    def publish(frame, peer_id):
        published.setdefault(peer_id, []).append(frame)
        return state.ok

    def notify(peer_id, msg_id, status, content_hash, kind, session_id):
        events.append((peer_id, msg_id, status, kind, session_id))

    queue = RelayDeliveryQueue(tmp_path / "relay_pending.json")
    state.queue = queue
    state.path = queue.path
    state.unit = RelayDelivery(queue, publish, notify, clock=state.clock)
    return state


def statuses(unit, peer_id=""):
    return [row["status"] for row in unit.status(peer_id)["items"]]


# ------------------------------------------------------------------ the ledger


def test_a_published_send_waits_for_its_ack_then_is_delivered(tmp_path):
    d = delivery_rig(tmp_path)
    d.unit.note_sent("peer", "m1", "hash-1", "hello")

    assert statuses(d.unit) == ["sent"]
    assert d.events == [("peer", "m1", "sent", "clipboard", "")]

    d.unit.note_ack("peer", "m1")
    assert statuses(d.unit) == ["delivered"]
    assert d.events[-1] == ("peer", "m1", "delivered", "clipboard", "")


def test_an_ack_for_an_unknown_send_is_ignored(tmp_path):
    d = delivery_rig(tmp_path)
    d.unit.note_ack("peer", "never-sent")

    assert d.unit.status() == {"pending": 0, "items": []}
    assert d.events == []


def test_a_send_inside_its_window_is_not_failed(tmp_path):
    d = delivery_rig(tmp_path)
    d.unit.note_sent("peer", "m1", "hash-1")

    d.clock.advance(ACK_WINDOW - 1)
    d.unit.scan()
    assert statuses(d.unit) == ["sent"]

    d.clock.advance(2)
    d.unit.scan()
    assert statuses(d.unit) == ["failed"]
    assert [event[2] for event in d.events] == ["sent", "failed"]


def test_content_already_delivered_is_not_reported_failed(tmp_path):
    """A redelivery of confirmed content is delivered, not failed."""
    d = delivery_rig(tmp_path)
    d.unit.note_sent("peer", "m1", "same-hash")
    d.unit.note_ack("peer", "m1")

    # Same content again: it is delivered on arrival, without waiting.
    d.unit.note_sent("peer", "m2", "same-hash")
    assert d.events[-1] == ("peer", "m2", "delivered", "clipboard", "")

    # A second copy that was still in flight when the first one was acked
    # passes its window as delivered too, instead of being reported failed.
    d.unit.note_sent("peer", "m3", "other-hash")
    d.unit.note_sent("peer", "m4", "other-hash")
    d.unit.note_ack("peer", "m3")
    d.clock.advance(ACK_WINDOW + 1)
    d.unit.scan()
    assert set(statuses(d.unit)) == {"delivered"}


def test_status_keeps_the_newest_rows_first(tmp_path):
    d = delivery_rig(tmp_path)
    for index in range(3):
        d.unit.note_sent("peer", f"m{index}", f"hash-{index}")
        d.clock.advance(1)

    rows = d.unit.status("peer")["items"]
    assert [row["msg_id"] for row in rows] == ["m2", "m1", "m0"]
    assert rows[0]["content_hash"] == "hash-2"


# --------------------------------------------------------------- offline queue


def test_a_failed_publish_is_queued_on_disk(tmp_path):
    d = delivery_rig(tmp_path)
    d.ok = False
    d.unit.enqueue("peer", "m1", "hash-1", "hello", b"frame-bytes")

    assert statuses(d.unit) == ["queued"]
    assert d.unit.status()["pending"] == 1
    assert "m1" in d.path.read_text(encoding="utf-8")
    assert d.events == [("peer", "m1", "queued", "clipboard", "")]


def test_a_delivered_send_never_writes_the_queue_file(tmp_path):
    d = delivery_rig(tmp_path)
    d.unit.note_sent("peer", "m1", "hash-1")
    d.unit.note_ack("peer", "m1")

    assert not d.path.exists()

    # …and a first-time install stays file-free after a queue round trip.
    d.unit.enqueue("peer", "m2", "hash-2", "", b"frame")
    assert d.path.exists()
    d.queue.remove("peer", "m2")
    assert "m2" not in d.path.read_text(encoding="utf-8")


def test_queued_rows_come_back_as_queued_after_a_restart(tmp_path):
    d = delivery_rig(tmp_path)
    d.unit.enqueue("peer", "m1", "hash-1", "hello", b"frame")

    reopened = RelayDelivery(
        RelayDeliveryQueue(d.path), lambda frame, peer_id: True, lambda *args: None
    )
    assert statuses(reopened) == ["queued"]
    assert reopened.status()["items"][0]["content_hash"] == "hash-1"


def test_a_successful_retry_moves_the_row_back_to_sent(tmp_path):
    d = delivery_rig(tmp_path)
    d.ok = False
    d.unit.enqueue("peer", "m1", "hash-1", "", b"frame")
    d.ok = True

    d.unit.retry_peer("peer")
    assert d.published["peer"] == [b"frame"]
    assert statuses(d.unit) == ["sent"]
    assert d.unit.status()["pending"] == 0
    assert [event[2] for event in d.events] == ["queued", "sent"]


def test_the_retry_budget_fails_the_row_once_and_drops_it(tmp_path):
    d = delivery_rig(tmp_path)
    d.ok = False
    d.unit.enqueue("peer", "m1", "hash-1", "", b"frame")

    for _ in range(MAX_RETRIES + 2):
        d.unit.retry_peer("peer")

    assert len(d.published["peer"]) == MAX_RETRIES
    assert statuses(d.unit) == ["failed"]
    assert d.unit.status()["pending"] == 0
    # A send at its budget is failed once, not re-reported on every sweep.
    assert [event[2] for event in d.events] == ["queued", "failed"]


def test_an_unreadable_payload_is_failed_rather_than_retried_forever(tmp_path):
    d = delivery_rig(tmp_path)
    d.ok = False
    d.unit.enqueue("peer", "m1", "hash-1", "", b"frame")
    d.queue._peers["peer"]["m1"]["frame_b64"] = "A"  # invalid base64 length

    d.unit.retry_peer("peer")
    assert statuses(d.unit) == ["failed"]
    assert d.unit.status()["pending"] == 0


def test_the_queue_cap_reports_the_evicted_send_as_failed(tmp_path):
    d = delivery_rig(tmp_path)
    d.queue.max_per_peer = 2
    d.ok = False
    for index in range(3):
        d.unit.enqueue("peer", f"m{index}", f"hash-{index}", "", b"frame")

    assert d.unit.status()["pending"] == 2
    assert ("peer", "m0", "failed", "clipboard", "") in d.events


def test_clearing_a_peer_drops_its_sends_and_its_queue(tmp_path):
    d = delivery_rig(tmp_path)
    d.ok = False
    d.unit.enqueue("peer", "m1", "hash-1", "", b"frame")
    d.ok = True
    d.unit.note_sent("peer", "m2", "hash-2")

    d.unit.clear_peer("peer")
    assert d.unit.status() == {"pending": 0, "items": []}
    assert d.unit.counts() == {"peers": {}}
    assert "frame" not in d.path.read_text(encoding="utf-8")


def test_the_tick_scans_on_its_own_cadence_and_retries_on_another(tmp_path):
    d = delivery_rig(tmp_path)
    d.ok = False
    d.unit.enqueue("peer", "m1", "hash-1", "", b"frame")
    d.unit.note_sent("peer", "m2", "hash-2")
    d.ok = True

    d.clock.advance(ACK_WINDOW + SCAN_INTERVAL)
    d.unit.tick()
    # The expired send is swept; the queue retry is not due yet.
    assert sorted(statuses(d.unit)) == ["failed", "queued"]
    assert d.unit.status()["pending"] == 1

    d.clock.advance(RETRY_INTERVAL)
    d.unit.tick()
    assert d.unit.status()["pending"] == 0
    assert sorted(statuses(d.unit)) == ["failed", "sent"]


# ------------------------------------------------- the runtime's delivery path


def clipboard_message(msg_id):
    """A locally captured clipboard message, as the monitor emits it."""
    return SimpleNamespace(
        content=ClipboardContent({ContentType.TEXT: b"relayed hello"}), msg_id=msg_id
    )


class FakeRelay:
    """Stand-in for RelayTransport: records what the runtime handed the broker."""

    def __init__(self):
        self.published = []
        self.state = "online"
        self.ok = True

    def publish(self, frame, topic, key, qos=0):
        self.published.append((frame, topic, key, qos))
        return self.ok

    def start(self):
        pass

    def stop(self):
        pass

    def refresh_channels(self):
        pass


@pytest.fixture
def relay_rig(tmp_path, monkeypatch):
    """A started LAN runtime with a fake relay channel to a netpair peer."""
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    config = Config(
        device_id="local",
        device_name="Local",
        encryption_enabled=False,
        retry_capture_enabled=False,
        sync_debounce=0.01,
    )
    pairing = identity("local")
    pairing.add_peer("remote", "Remote", identity("remote").get_identity().certificate_pem,
                     paired=True)
    config.peers["remote"] = PeerInfo(device_id="remote", device_name="Remote", paired=True)
    config.netpair_secrets = {"remote": "netpair-secret"}
    transport, discovery = Transport(), Discovery()
    clipboard, history, events, saves = Clipboard(), History(), EventJournal(), []
    runtime = LanRuntime(
        config,
        pairing,
        None,
        history,
        events,
        lambda: saves.append(None),
        monitor=Monitor(),
        reader=clipboard,
        writer=clipboard,
        transport=transport,
        discovery=discovery,
        open_url=lambda _url: None,
    )
    runtime.REFRESH_INTERVAL = 60
    runtime.start()
    try:
        # Attached after start so no real MQTT client is built; the delivery
        # path only needs publish()/state.
        config.internet_sync_enabled = True
        runtime.relay = relay = FakeRelay()
        yield SimpleNamespace(runtime=runtime, config=config, relay=relay, events=events,
                              queue=runtime.delivery_queue, transport=transport,
                              history=history)
    finally:
        transport.stop_result = True
        assert runtime.stop()


def test_a_relayed_clipboard_send_is_ledgered_and_acked(relay_rig):
    runtime, relay = relay_rig.runtime, relay_rig.relay
    assert runtime._on_local_sync(clipboard_message("m1"))
    assert len(relay.published) == 1
    row = runtime.relay_delivery_status()["items"][0]
    assert (row["msg_id"], row["status"], row["kind"]) == ("m1", "sent", "clipboard")

    # The peer's receipt settles the row and unblocks the device badge.
    runtime._receive_relay(
        encode_frame({"msg_type": "relay_ack", "msg_id": "m1"}, source_device="remote")
    )
    assert runtime.relay_delivery_status()["items"][0]["status"] == "delivered"
    assert runtime.delivery_counts() == {"peers": {}}


def test_a_failed_relay_publish_is_queued_and_flushed_by_a_peer_frame(relay_rig):
    runtime, relay, queue = relay_rig.runtime, relay_rig.relay, relay_rig.queue
    relay.ok = False

    assert runtime._on_local_sync(
        clipboard_message("m2")
    )
    row = runtime.relay_delivery_status()["items"][0]
    assert row["status"] == "queued"
    assert runtime.delivery_counts() == {"peers": {"remote": 1}}
    assert queue.path.exists()

    # Any frame from that peer proves it reachable — the queue flushes.
    relay.ok = True
    runtime._receive_relay(
        encode_frame({"msg_type": "nav_url", "url": "https://example.com"},
                     source_device="remote")
    )
    assert runtime.relay_delivery_status()["items"][0]["status"] == "sent"
    assert runtime.delivery_counts() == {"peers": {}}
    assert len(relay.published) == 2


def test_going_online_flushes_every_queued_send(relay_rig):
    runtime, relay = relay_rig.runtime, relay_rig.relay
    relay.ok = False
    assert runtime._on_local_sync(clipboard_message("m3"))

    relay.ok = True
    runtime._relay_state_changed("online")
    assert runtime.delivery_counts() == {"peers": {}}
    assert events_named(relay_rig.events, "relay.state.changed") == [{"state": "online"}]
    assert events_named(relay_rig.events, "relay.delivery.changed")[-1]["status"] == "sent"


def test_a_peer_reachable_over_both_channels_is_published_to_once(relay_rig):
    """A netpair peer that is also relay-enrolled gets one frame, not two."""
    relay_rig.config.peer_relay_secrets = {"remote": "enrolled-secret"}
    runtime, relay = relay_rig.runtime, relay_rig.relay

    assert runtime._on_local_sync(clipboard_message("m4"))
    assert len(relay.published) == 1
    assert len(runtime.relay_delivery_status()["items"]) == 1


def test_unpairing_a_peer_drops_its_pending_sends(relay_rig):
    runtime, relay, queue = relay_rig.runtime, relay_rig.relay, relay_rig.queue
    relay.ok = False
    assert runtime._on_local_sync(clipboard_message("m5"))
    assert runtime.delivery_counts() == {"peers": {"remote": 1}}

    runtime.unpair_device("remote")
    assert runtime.relay_delivery_status() == {"pending": 0, "items": []}
    assert runtime.delivery_counts() == {"peers": {}}
    assert "m5" not in queue.path.read_text(encoding="utf-8")


def test_a_confirmed_hello_reports_the_peer_paired_and_online(relay_rig):
    """The hello is the first proof the peer is up, so the panel hears it now."""
    runtime, events = relay_rig.runtime, relay_rig.events
    runtime._receive_relay(
        encode_frame(
            {
                "msg_type": "netpair_hello",
                "peer_id": netpair_device_tag(runtime.config.device_id),
                "device_name": "Remote",
                "ts": time.time(),
            },
            source_device="remote",
        ),
        netpair_topic("netpair-secret"),
    )

    rows = events_named(events, "netpair.peer.changed")
    assert len(rows) == 1
    assert rows[0]["peer_id"] == "remote"
    assert rows[0]["name"] == "Remote"
    assert rows[0]["status"] == "paired"
    assert rows[0]["online"] is True
    assert abs(rows[0]["last_seen"] - time.time()) < 30


def test_unpairing_an_internet_peer_drops_its_pending_sends(relay_rig):
    runtime, relay = relay_rig.runtime, relay_rig.relay
    relay.ok = False
    assert runtime._on_local_sync(clipboard_message("m6"))
    assert runtime.delivery_counts() == {"peers": {"remote": 1}}

    assert runtime.internet_pairing.unpair("remote") == {"ok": True}
    assert runtime.relay_delivery_status() == {"pending": 0, "items": []}
    assert runtime.delivery_counts() == {"peers": {}}
    # The phone's other tabs only learn about the removal from this push, and
    # it comes from the runtime so a desktop-side unpair reaches them too.
    assert events_named(relay_rig.events, "netpair.peer.changed") == [
        {"peer_id": "remote", "status": "unpaired"}
    ]


# ------------------------------------------------- chat and files over the relay
#
# Legacy mirrored chat frames to the public relay, but only when the LAN send
# had already failed — chat has no content dedup, so an unconditional mirror
# would append every message twice to a dual-connected peer.  The receiving end
# feeds relay frames through the same router LAN frames use, which is what makes
# a conversation with an internet-only paired device work at all.


def chat_frame(kind, msg_id, **payload):
    return encode_frame({"msg_type": kind, **payload}, msg_id=msg_id, source_device="local")


def test_a_chat_frame_takes_the_relay_only_when_the_lan_send_fails(relay_rig):
    runtime, relay, transport = relay_rig.runtime, relay_rig.relay, relay_rig.transport
    send = runtime._chat_send_fn("remote")
    frame_bytes = chat_frame("chat_text", "c1", session_id="s1", text="hi")

    transport.connected.add("remote")
    assert send(frame_bytes) is True
    assert relay.published == []  # LAN delivered it — no duplicate mirror

    transport.connected.discard("remote")
    assert send(frame_bytes) is True  # the relay carried it instead
    assert [route[3] for route in relay.published] == [0]


def test_a_lan_delivered_chat_frame_waits_for_its_relay_ack(relay_rig):
    """The sender's send list has to show the receipt the peer sends back."""
    runtime, transport = relay_rig.runtime, relay_rig.transport
    transport.connected.add("remote")
    assert runtime._chat_send_fn("remote")(
        chat_frame("chat_text", "c2", session_id="s1", text="hello there")
    )

    row = runtime.relay_delivery_status()["items"][0]
    assert (row["msg_id"], row["kind"], row["session_id"], row["preview"]) == (
        "c2", "chat_text", "s1", "hello there"
    )
    assert row["status"] == "sent"

    runtime._receive_relay(
        encode_frame({"msg_type": "relay_ack", "msg_id": "c2"}, source_device="remote")
    )
    assert runtime.relay_delivery_status()["items"][0]["status"] == "delivered"


def test_an_internet_only_peer_gets_relay_safe_file_chunks(relay_rig):
    runtime, transport = relay_rig.runtime, relay_rig.transport
    # Not connected, but paired over the relay: every byte has to fit an
    # envelope, so chat must chunk relay-safe and refuse anything past the cap.
    send = runtime._chat_send_fn("remote")
    assert send.chunk_size == ChatManager.RELAY_CHUNK_SIZE
    assert send.internet_cap == ChatManager.RELAY_FILE_CAP

    # A LAN-connected peer keeps the 256 KiB wire format peers already speak.
    transport.connected.add("remote")
    assert not hasattr(runtime._chat_send_fn("remote"), "chunk_size")


def test_a_relayed_file_chunk_rides_at_qos_one(relay_rig):
    """QoS 1 buys redelivery of a dropped chunk; the receiver dedups by index."""
    runtime, relay = relay_rig.runtime, relay_rig.relay
    send = runtime._chat_send_fn("remote")

    assert send(chat_frame("file_chunk", "f1", session_id="s1", index=0, data=""))
    assert relay.published[-1][3] == 1
    assert send(chat_frame("chat_text", "c3", session_id="s1", text="after"))
    assert relay.published[-1][3] == 0


def test_a_relayed_chat_invite_reaches_the_chat_layer_and_is_acked(relay_rig):
    runtime, relay = relay_rig.runtime, relay_rig.relay
    runtime._receive_relay(
        encode_frame(
            {
                "msg_type": "chat_invite",
                "session_id": "abcdef0123456789",
                "from_name": "Remote",
                "fingerprint_short": "ABCD-EFGH",
            },
            msg_id="i1",
            source_device="remote",
        )
    )

    sessions = runtime.chat_sessions()["sessions"]
    assert [(session["status"], session["peer_id"]) for session in sessions] == [
        ("invited", "remote")
    ]
    acked = [decode_message(route[0]) for route in relay.published]
    assert [(message.msg_type, message._raw_payload["msg_id"]) for message in acked] == [
        ("relay_ack", "i1")
    ]


def test_a_pairing_frame_over_the_relay_is_refused(relay_rig):
    """Trust is earned on the pinned LAN handshake, never on a shared secret."""
    runtime, relay = relay_rig.runtime, relay_rig.relay
    runtime._receive_relay(
        encode_frame({"msg_type": "pairing_unpair"}, msg_id="p1", source_device="remote")
    )

    assert runtime.config.peers["remote"].paired is True
    assert events_named(relay_rig.events, "pairing.resolved") == []
    assert relay.published == []


# ------------------------------------ the channels, and who may speak on them
#
# A relay topic is derived from a secret both ends already hold, so the channel
# — never a frame's self-declared sender — is what names the peer that may
# speak on it.  Legacy bound the frame to its channel in the same three cases;
# the sidecar routed on the claimed source alone, which let one paired peer have
# everything it sent filed against a second device that holds a pairing it never
# used.


def relay_clip(text, source_device, msg_id="relay-1"):
    """A clipboard frame as it arrives off the relay: content, and a claimed sender."""
    return encode_message(
        SyncMessage(
            ClipboardContent({ContentType.TEXT: text.encode()}), msg_id, source_device
        )
    )


def test_a_frame_is_attributed_to_the_owner_of_the_channel_it_arrived_on(relay_rig):
    runtime, config, history = relay_rig.runtime, relay_rig.config, relay_rig.history
    # A second device, paired in its own right, so the claim below is one that
    # would otherwise be believed and reachable both.
    config.netpair_secrets["d1d2d3d4d5d6"] = "second-secret"
    config.peers["d1d2d3d4d5d6"] = PeerInfo(
        device_id="d1d2d3d4d5d6", device_name="Second", paired=True
    )

    runtime._receive_relay(
        relay_clip("filed against the wrong device", "d1d2d3d4d5d6"),
        netpair_topic("netpair-secret"),
    )

    assert [item.source_device for item in history.items] == ["remote"]
    assert history.items[-1].transport == "relay"


def test_any_frame_from_a_paired_peer_is_what_keeps_it_online(relay_rig):
    """A hello is not the only frame a peer sends, and not its last.

    Only the handshake touched the last-seen stamp, so a peer that synced all
    afternoon still read 离线 ninety seconds after the hello it happened to have
    sent — the badge contradicted the traffic arriving under it.
    """
    runtime, service = relay_rig.runtime, relay_rig.runtime.internet_pairing
    assert service.status()["peers"][0]["online"] is False

    runtime._receive_relay(relay_clip("still here", "remote"), netpair_topic("netpair-secret"))

    peer = service.status()["peers"][0]
    assert (peer["peer_id"], peer["online"]) == ("remote", True)


def test_a_frame_on_a_waiting_code_is_only_taken_from_the_device_that_owns_it(relay_rig):
    """A provisional channel vouches for a 4-char tag, and for nothing else.

    The same frame *proves* the pairing when its source does hash to that tag.
    The confirmation hello is a single best-effort publish, so a lost one used
    to leave the tag-keyed entry permanent: a phantom "paired" device with no
    name and no real id, which could be sent to and never answered.
    """
    runtime, config, history = relay_rig.runtime, relay_rig.config, relay_rig.history
    relay = relay_rig.relay
    service = runtime.internet_pairing
    # The fixture hands the runtime a relay after start, so the service needs it
    # too: ``enter`` publishes its hello through its own reference, and refuses
    # to write a pairing whose hello cannot go out.
    service.attach_relay(relay)
    entering = "b1b2b3b4b5b6"
    secret = generate_netpair_secret()
    tag = netpair_device_tag(entering)
    assert service.enter(generate_netpair_code(entering, secret)) == {
        "peer_id": tag,
        "waiting": True,
    }
    assert [row["peer_id"] for row in service.status()["waiting"]] == [tag]
    published = len(relay.published)

    # Some other device on that channel.  The code carried the tag and nothing
    # else, so a source that does not hash to it is not who the code was for.
    impostor = "c1c2c3c4c5c6"
    assert netpair_device_tag(impostor) != tag
    runtime._receive_relay(
        relay_clip("not from the peer", impostor, msg_id="impostor"),
        netpair_topic(secret),
    )
    assert history.items == []
    assert config.netpair_secrets[tag] == secret
    # Nothing answered it either: it is dropped before anything routes, so
    # no receipt goes back to a device that never held the channel.
    assert len(relay.published) == published

    # The peer itself, and the wait turns into the device it really is.
    runtime._receive_relay(
        relay_clip("from the peer", entering, msg_id="from-peer"),
        netpair_topic(secret),
    )
    assert set(config.netpair_secrets) == {"remote", entering}
    assert config.netpair_secrets[entering] == secret
    assert service.status()["waiting"] == []
    assert [item.source_device for item in history.items] == [entering]
    # ...and this one earns the receipt, addressed to the frame it came from.
    receipts = [decode_message(item[0]) for item in relay.published[published:]]
    assert [frame.msg_type for frame in receipts] == ["relay_ack"]
    assert receipts[0]._raw_payload["msg_id"] == "from-peer"


def test_a_wait_that_outlived_a_restart_is_still_shown_and_still_removable(relay_rig):
    """The one entry legacy deleted behind the user's back, and this stack does not.

    A code entered here whose partner never answered is persisted under the
    provisional 4-char tag it was addressed to, and legacy swept every such
    entry when it loaded the config -- ``_netpair_drop_provisional``, on the
    grounds that the entry is "a few-seconds-long placeholder" the UI "cannot
    show or remove".  Neither half holds now: the row this stack shows *is* the
    wait, and Cancel is how it ends.  So a wait that outlives the process is
    still a wait -- listed, still listening on the channel the code derives,
    and still removable -- and that is a deliberate divergence, pinned here.
    """
    config, service = relay_rig.config, relay_rig.runtime.internet_pairing
    entering = "b1b2b3b4b5b6"
    secret = generate_netpair_secret()
    tag = netpair_device_tag(entering)
    # What the run before this one left on disk.  The clock it was entered at
    # and the name it was waiting under went with that process, so the row can
    # say who it waits for and not how long -- which is why the age is optional.
    config.netpair_secrets[tag] = secret

    assert [
        (row["peer_id"], row["name"], row["since"]) for row in service.status()["waiting"]
    ] == [(tag, "", None)]
    # ...and it is still not a device: the tag cannot be sent to, and listing
    # it among the peers showed a phantom that could not be removed either.
    assert [peer["peer_id"] for peer in service.status()["peers"]] == ["remote"]
    assert netpair_topic(secret) in service.channels()

    assert service.unpair(tag) == {"ok": True}
    assert config.netpair_secrets == {"remote": "netpair-secret"}
    assert service.status()["waiting"] == []
    assert netpair_topic(secret) not in service.channels()


def test_a_hello_still_completes_a_wait_that_outlived_a_restart(relay_rig):
    """What keeping the entry buys: the answer can still arrive.

    Legacy's sweep made this impossible -- a pairing a code began could not be
    completed after a restart, and the code had to be typed again on both
    machines.  Here the entry is still on the channel it derives, so the late
    hello is accepted, the tag is re-keyed to the device that really answered,
    and the wait becomes the device it was always going to be.
    """
    config, runtime = relay_rig.config, relay_rig.runtime
    service = runtime.internet_pairing
    entering = "b1b2b3b4b5b6"
    secret = generate_netpair_secret()
    tag = netpair_device_tag(entering)
    config.netpair_secrets[tag] = secret

    runtime._receive_relay(
        encode_frame(
            {
                "msg_type": "netpair_hello",
                "peer_id": config.device_id,
                "device_name": "Answered",
            },
            source_device=entering,
        ),
        netpair_topic(secret),
    )

    assert set(config.netpair_secrets) == {"remote", entering}
    assert config.netpair_secrets[entering] == secret
    assert service.status()["waiting"] == []
    assert entering in [peer["peer_id"] for peer in service.status()["peers"]]


def test_the_relay_channels_cover_both_ways_a_peer_can_be_paired(relay_rig):
    """Subscribing and publishing are the same list, and it was half of one.

    The netpair family was there and the LAN-enrolled family was not, so a
    device paired over the local network and currently away from it was
    published *to* and never heard *from*: its frames arrived on a topic this
    machine had not subscribed to, which reads exactly like a peer that is
    simply not there.
    """
    config, service = relay_rig.config, relay_rig.runtime.internet_pairing
    config.peer_relay_secrets = {"enrolled": "enrolled-secret"}
    config.peers["enrolled"] = PeerInfo(
        device_id="enrolled", device_name="Enrolled", paired=True
    )
    config.relay_secret = "our-relay-secret"

    channels = service.channels()

    assert channels[netpair_topic("netpair-secret")] == netpair_key("netpair-secret", "")
    assert channels[derive_topic("our-relay-secret", "enrolled-secret")] == derive_key(
        "our-relay-secret", "enrolled-secret"
    )
    # This machine's own secret is what both ends of the derivation need, so it
    # is one per machine and never regenerated under a live channel.
    assert service.ensure_relay_secret() == "our-relay-secret"


def test_the_relay_secret_is_generated_once_and_persisted(relay_rig):
    config, service = relay_rig.config, relay_rig.runtime.internet_pairing
    config.peer_relay_secrets = {"enrolled": "enrolled-secret"}
    config.peers["enrolled"] = PeerInfo(
        device_id="enrolled", device_name="Enrolled", paired=True
    )
    assert config.relay_secret == ""

    secret = service.ensure_relay_secret()

    assert secret and config.relay_secret == secret
    assert service.ensure_relay_secret() == secret
    assert derive_topic(secret, "enrolled-secret") in service.channels()


def test_a_peer_reachable_both_ways_is_listened_for_on_one_channel(relay_rig):
    """The same peer on the same machine is one channel, not two.

    A netpair entry wins over the enrolled one, as it does on the publish side:
    a machine answering on one topic twice would have to dedup its own frames.
    """
    config, service = relay_rig.config, relay_rig.runtime.internet_pairing
    config.peer_relay_secrets = {"remote": "enrolled-secret"}
    config.relay_secret = "our-relay-secret"

    channels = service.channels()

    assert netpair_topic("netpair-secret") in channels
    assert derive_topic("our-relay-secret", "enrolled-secret") not in channels


def test_the_netpair_key_follows_the_one_password_the_user_sets(relay_rig):
    """``netpair_password`` is a fallback, never the source of truth.

    A config loaded from 1.x carries its passphrase in ``encryption_password``
    and nothing in ``netpair_password``, so a key derived from the latter alone
    is the same key only while something has mirrored it.  The two ends of a
    pairing then derive different keys from the same code and every frame on
    that channel fails to decrypt: the pairing reports success and nothing ever
    syncs, with no error anywhere.
    """
    config, service = relay_rig.config, relay_rig.runtime.internet_pairing

    config.encryption_password = "the-users-password"
    config.netpair_password = ""
    assert service.netpair_password() == "the-users-password"
    assert service.channels()[netpair_topic("netpair-secret")] == netpair_key(
        "netpair-secret", "the-users-password"
    )

    # The older field is still honoured where there is no encryption password to
    # prefer, so a passphrase saved before the two were merged keeps working.
    config.encryption_password = ""
    config.netpair_password = "legacy-only"
    assert service.netpair_password() == "legacy-only"
    assert service.channels()[netpair_topic("netpair-secret")] == netpair_key(
        "netpair-secret", "legacy-only"
    )
# ------------------------------------------- the secret that opens the channel
#
# The enrolled channel is derived from two secrets, one per machine, so neither
# end can derive it alone: ``relay_enroll`` is the exchange that hands them over.
# It travels on the LAN link only — the frame decides which public topic this
# machine listens on, and the LAN link is the one transport where the peer at
# the far end is pinned by a certificate rather than by a key both ends already
# share.


def enroll_payload(secret):
    """The body of a ``relay_enroll``: the secret the other end listens on."""
    return {"msg_type": "relay_enroll", "relay_secret": secret}


def enroll_frame(secret, source_device=""):
    """A ``relay_enroll`` as it arrives off the LAN: decoded, with a sender."""
    return decode_message(encode_frame(enroll_payload(secret), source_device=source_device))


def test_a_relay_secret_is_offered_over_the_lan_and_answered_once(relay_rig):
    """Both halves of the exchange, and the guard that has to end it.

    Legacy answered every enroll unconditionally, so two machines running it
    answer each other forever: each answer is a frame that provokes another,
    over a link that is already up, with nothing to make it stop.  Ours answers
    when the peer's secret is new, or when we have not yet told them ours.
    """
    runtime, config, transport = relay_rig.runtime, relay_rig.config, relay_rig.transport
    # The fixture's peer is also a netpair peer, whose own channel wins over the
    # enrolled one (the one-channel test below pins that); the enrolled family is
    # what carries a peer that has no netpair secret at all.
    config.netpair_secrets.pop("remote")
    transport.connected.add("remote")

    runtime.internet_pairing.enroll_peers()

    assert [(pid, msg.msg_type) for pid, msg in transport.sent] == [
        ("remote", "relay_enroll")
    ]
    assert transport.sent[0][1]._raw_payload["relay_secret"] == config.relay_secret

    # The peer's own secret arrives, is stored, and is what the channel this
    # machine listens for it on is derived from.
    theirs = "a" * 64
    runtime._receive(enroll_frame(theirs, source_device="remote"), "remote")
    assert config.peer_relay_secrets == {"remote": theirs}
    assert derive_topic(config.relay_secret, theirs) in runtime.internet_pairing.channels()
    # ...and it is answered in turn, because until it is the peer cannot derive
    # that channel either.
    assert [msg.msg_type for _, msg in transport.sent] == ["relay_enroll"] * 2
    assert transport.sent[1][1]._raw_payload["relay_secret"] == config.relay_secret

    # A peer that repeats itself is not answered again: we have already offered
    # ours and its secret has not changed.  This is the frame that would
    # otherwise ping-pong.
    runtime._receive(enroll_frame(theirs, source_device="remote"), "remote")
    assert len(transport.sent) == 2

    # A rotated secret is answered, because the channel it replaces is dead.
    rotated = "b" * 64
    runtime._receive(enroll_frame(rotated, source_device="remote"), "remote")
    assert config.peer_relay_secrets == {"remote": rotated}
    assert derive_topic(config.relay_secret, rotated) in runtime.internet_pairing.channels()
    assert len(transport.sent) == 3
    # None of this was clipboard traffic, on either side of the exchange.
    assert relay_rig.history.items == []


def test_an_enroll_with_an_unusable_secret_changes_nothing(relay_rig):
    """A malformed secret is dropped where it arrives.

    It comes off a LAN link from a peer, and it becomes the topic this machine
    subscribes to: a short or non-hex value would be a channel nobody can
    derive, so the pairing would read enrolled and sync nothing, with no error
    raised on either side.
    """
    runtime, config, transport = relay_rig.runtime, relay_rig.config, relay_rig.transport
    transport.connected.add("remote")

    for bad in ("", "a" * 63, "a" * 65, "Z" * 64, None, 12345):
        runtime._receive(enroll_frame(bad, source_device="remote"), "remote")

    assert config.peer_relay_secrets == {}
    # Not even this machine's own secret was minted for a peer's bad frame.
    assert config.relay_secret == ""
    assert transport.sent == []
    assert runtime.internet_pairing.channels() == {
        netpair_topic("netpair-secret"): netpair_key("netpair-secret", "")
    }


def test_a_relay_secret_offered_over_the_relay_is_refused(relay_rig):
    """The broker does not get to choose which topic this machine listens on.

    Off the relay a frame has only a claimed sender to vouch for it, and this
    frame is the one that decides the channel.  A legacy host never publishes
    one there either — its enroll send has no relay fallback — so refusing costs
    nothing a real peer does.
    """
    runtime, config, transport = relay_rig.runtime, relay_rig.config, relay_rig.transport
    transport.connected.add("remote")

    runtime._receive_relay(
        encode_frame(enroll_payload("c" * 64), source_device="remote"),
        netpair_topic("netpair-secret"),
    )

    assert config.peer_relay_secrets == {}
    assert transport.sent == []
    # ...and it did not fall through to the clipboard path on its way out.
    assert relay_rig.history.items == []


def test_a_peer_that_arrives_on_the_lan_is_offered_enrollment_then(relay_rig):
    """A peer that starts after this machine's relay did is not left out.

    Legacy offered once, from the relay-start path, and never retried: a peer
    that was down at that instant was never enrolled and nothing noticed.  The
    link coming up is the moment the runtime can see that the peer it was
    waiting for is reachable.
    """
    runtime, transport = relay_rig.runtime, relay_rig.transport
    assert transport.sent == []

    transport.connected.add("remote")
    runtime._refresh()

    assert [(pid, msg.msg_type) for pid, msg in transport.sent] == [
        ("remote", "relay_enroll")
    ]


def test_nothing_is_offered_while_there_is_no_relay_to_answer_on(relay_rig):
    """The offer is one half of an exchange, and the other half needs a relay.

    With internet sync off this machine subscribes to no relay topic at all, so
    a secret handed to a peer would open a channel with nobody listening on it
    — and legacy's offer only ever ran from the relay-start path, which is
    reached only when internet sync is on.
    """
    runtime, config, transport = relay_rig.runtime, relay_rig.config, relay_rig.transport
    transport.connected.add("remote")
    config.internet_sync_enabled = False

    runtime.internet_pairing.enroll_peers()

    assert transport.sent == []
    assert config.relay_secret == ""

    # A device this machine holds no pairing with is not taught it either: the
    # LAN link to it is not one a certificate pins to a known peer.  It is
    # connected here, so the missing pairing is the only thing that can refuse.
    config.internet_sync_enabled = True
    config.peers["stranger"] = PeerInfo(device_id="stranger", device_name="Stranger")
    transport.connected.add("stranger")
    assert runtime.internet_pairing.offer_enroll("stranger") is False
    assert transport.sent == []

