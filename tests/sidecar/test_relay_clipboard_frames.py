"""A clipboard frame bigger than one relay message still reaches the peer.

From the Mac log this comes from: a 1.8 MB image is copied, the relay refuses
the 1893212-byte frame against its 48968-byte limit, and the sending side goes
on reporting the capture as synced while the Windows machine's clipboard never
changes.  Two things were wrong, and both are pinned here:

* the frame never crossed at all, and it must -- cut into chunks that fit one
  broker message, rebuilt on the far side and applied as the single clipboard
  message it was;
* a frame that still could not be carried was reported as sent, and it must not
  -- the sending side has to say which limit stopped it and which peer is
  missing the clip.

The third property is the one that is easy to lose while fixing the first: a
frame that fits one message must keep going out as one frame, because that is
every clip on a healthy LAN.

The rig is two runtimes on one enrolled-relay channel (the LAN-relay route
``_publish_relay``'s first loop uses), which is the shape a real pair has when
neither device is on the other's network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from internal.application.events import EventJournal
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.config.config import Config, PeerInfo
from internal.infrastructure.runtime.lan import LanRuntime
from internal.protocol.codec import decode_message, encode_message
from internal.sync.nearby_chat import ChatManager
from internal.transport.relay import derive_topic, frame_limit_for
from internal.transport.relay_chunks import (
    ClipChunkAssembler,
    is_clip_transfer_id,
    mint_clip_transfer_id,
    split_frame,
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

SENDER_ID = "aaaaaaaaaaaa"
PEER_ID = "bbbbbbbbbbbb"
SENDER_SECRET = "sender-relay-secret"
PEER_SECRET = "peer-relay-secret"


class FakeRelay:
    """Stand-in for RelayTransport: records what the runtime handed the broker."""

    def __init__(self):
        self.published = []
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


def build_runtime(device_id, peer_id, own_secret, peer_secret):
    """One side of an enrolled-relay pair, started, with a fake broker."""
    config = Config(
        device_id=device_id,
        device_name=device_id.upper(),
        encryption_enabled=False,
        retry_capture_enabled=False,
        sync_debounce=0.01,
    )
    # A LAN-relay channel: each side holds its own secret and the peer's, and
    # both derive the same topic from the pair.
    config.relay_secret = own_secret
    config.peer_relay_secrets = {peer_id: peer_secret}
    config.peers[peer_id] = PeerInfo(device_id=peer_id, device_name=peer_id.upper(), paired=True)
    pairing = identity(device_id)
    pairing.add_peer(
        peer_id,
        peer_id.upper(),
        identity(peer_id).get_identity().certificate_pem,
        paired=True,
    )
    transport, discovery = Transport(), Discovery()
    clipboard, history, events = Clipboard(), History(), EventJournal()
    runtime = LanRuntime(
        config,
        pairing,
        None,
        history,
        events,
        lambda: None,
        monitor=Monitor(),
        reader=clipboard,
        writer=clipboard,
        transport=transport,
        discovery=discovery,
        open_url=lambda _url: None,
    )
    runtime.REFRESH_INTERVAL = 60
    # The gap before this side asks for missing chunks.  A test cannot wait the
    # real five seconds, and what is under test is the wiring, not the number.
    runtime.CLIP_CHUNK_STALL_GRACE = 0.0
    runtime.start()
    config.internet_sync_enabled = True
    runtime.relay = FakeRelay()
    return SimpleNamespace(
        runtime=runtime,
        config=config,
        pairing=pairing,
        relay=runtime.relay,
        transport=transport,
        clipboard=clipboard,
        history=history,
        events=events,
    )


@pytest.fixture
def relay_pair(tmp_path, monkeypatch):
    """Two enrolled-relay runtimes: the sender, and the peer it speaks to."""
    # Each runtime reads its own identity and pending queue out of this
    # directory at construction, so the two get one each rather than sharing a
    # queue file behind the tests' back.
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path / "sender"))
    sender = build_runtime(SENDER_ID, PEER_ID, SENDER_SECRET, PEER_SECRET)
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path / "peer"))
    receiver = build_runtime(PEER_ID, SENDER_ID, PEER_SECRET, SENDER_SECRET)
    pair = SimpleNamespace(
        sender=sender,
        receiver=receiver,
        topic=derive_topic(SENDER_SECRET, PEER_SECRET),
    )
    try:
        yield pair
    finally:
        for side in (sender, receiver):
            side.transport.stop_result = True
            assert side.runtime.stop()


def text_message(text: bytes, msg_id: str) -> SyncMessage:
    return SyncMessage(ClipboardContent({ContentType.TEXT: text}), msg_id, SENDER_ID)


def oversized_message(msg_id: str, frame_limit: int) -> SyncMessage:
    """A message whose encoded frame is well over one relay message.

    Ordinary, non-repeating content, so a piece that landed at the wrong offset
    would be visible rather than invisible.
    """
    seed = b"".join(b"clipsync-%06d " % index for index in range(2000))
    text = (seed * ((3 * frame_limit) // len(seed) + 1))[: 3 * frame_limit]
    return text_message(text, msg_id)


def encoded_frame(message: SyncMessage) -> bytes:
    """The frame ``_on_local_sync`` will put on the wire for *message*."""
    return encode_message(SyncMessage(message.content, message.msg_id, SENDER_ID))


def published_frames(side) -> list[bytes]:
    return [route[0] for route in side.relay.published]


def deliver(pair, frames) -> None:
    """Hand frames to the peer the way the broker would -- on the channel."""
    for frame in frames:
        pair.receiver.runtime._receive_relay(frame, pair.topic)


def test_an_oversized_frame_crosses_the_relay_and_lands_as_one_clipboard_message(relay_pair):
    """The whole bug: it arrives, and what arrives is the frame that was sent."""
    sender, receiver = relay_pair.sender, relay_pair.receiver
    limit = frame_limit_for(sender.config.relay_max_message_bytes)
    message = oversized_message("big-1", limit)
    frame = encoded_frame(message)
    assert len(frame) > limit, "the test frame has to be one the relay refuses"

    assert sender.runtime._on_local_sync(message) is True, "an oversized clip must still send"

    frames = published_frames(sender)
    assert len(frames) > 1, "it cannot have crossed as one message the relay refuses"
    payloads = [decode_message(frame_bytes)._raw_payload for frame_bytes in frames]
    assert all(is_clip_transfer_id(payload["transfer_id"]) for payload in payloads)
    assert all(payload["total_chunks"] == len(frames) for payload in payloads)
    # Every chunk is a message the broker took at QoS 1, which is what buys
    # redelivery of one it drops in flight.
    assert {route[3] for route in sender.relay.published} == {1}

    deliver(relay_pair, frames)
    assert receiver.runtime._clip_chunks.pending_count() == 0, "the peer finished assembling it"

    text = message.content.types[ContentType.TEXT]
    assert receiver.clipboard.writes[-1].types[ContentType.TEXT] == text
    assert [row.types[ContentType.TEXT] for row in receiver.history.items] == [text]

    # ...and byte for byte, which is the promise the chunks make: rebuilding the
    # published frames with the same assembler the peer used gives back the frame
    # the sender encoded.
    rebuild = ClipChunkAssembler()
    whole = None
    for frame_bytes in frames:
        payload = decode_message(frame_bytes)._raw_payload
        whole = rebuild.add(
            payload["transfer_id"],
            payload["chunk_index"],
            payload["total_chunks"],
            payload["_raw_data"],
            PEER_ID,
        )
    assert whole == frame

    # The clip landed as itself, so the peer's receipt resolves the sender's
    # send-list row -- the half that says "已送达" only for a clip that arrived.
    ack = published_frames(receiver)[-1]
    assert decode_message(ack).msg_type == "relay_ack"
    sender.runtime._receive_relay(ack, relay_pair.topic)
    rows = sender.runtime.relay_delivery_status()["items"]
    assert [(row["msg_id"], row["status"]) for row in rows] == [("big-1", "delivered")]


def test_a_frame_that_fits_still_goes_out_as_exactly_one_frame(relay_pair):
    """The common path: no chunking, no extra frame, byte-identical content.

    Every clip on a healthy LAN takes this path, and a fix that cut everything
    would be the regression this pins.
    """
    sender = relay_pair.sender
    limit = frame_limit_for(sender.config.relay_max_message_bytes)
    message = text_message(b"a small clip on a healthy lan", "small-1")
    expected = encoded_frame(message)
    assert len(expected) <= limit

    assert sender.runtime._on_local_sync(message) is True

    frames = published_frames(sender)
    assert len(frames) == 1, "a clip that fits must not be cut into several frames"
    assert frames[0] == expected, "the frame on the wire is the frame that was encoded"
    assert sender.relay.published[0][3] == 0, "an ordinary clipboard frame stays at QoS 0"
    decoded = decode_message(frames[0])
    assert decoded.msg_type == "clipboard"
    assert decoded.msg_id == "small-1"


def test_a_frame_that_fits_one_message_but_not_one_chunk_still_goes_as_one_frame(relay_pair):
    """The edge the cut must not swallow: fitting beats chunking.

    A frame can fit one relay message and still be larger than the chunk size
    that message's limit derives.  Cutting "anything above a chunk" would take
    this frame apart with no reason to, so the frame on the wire is asserted to
    be the frame that was encoded -- one message, not two pieces of one.
    """
    sender = relay_pair.sender
    limit = frame_limit_for(sender.config.relay_max_message_bytes)
    chunk_size = ChatManager.relay_chunk_for(sender.config.relay_max_message_bytes)
    target = (chunk_size + limit) // 2

    def frame_len(payload: int) -> int:
        return len(encoded_frame(text_message(b"x" * payload, "m-1")))

    low, high = 1, limit
    while low < high:
        middle = (low + high + 1) // 2
        if frame_len(middle) <= target:
            low = middle
        else:
            high = middle - 1
    expected = encoded_frame(text_message(b"x" * low, "m-1"))
    assert chunk_size < len(expected) <= limit, "the test frame has to sit between the two numbers"

    message = text_message(b"x" * low, "m-1")
    assert sender.runtime._on_local_sync(message) is True

    frames = published_frames(sender)
    assert len(frames) == 1, "a frame that fits one message is one message"
    assert frames[0] == expected


def test_an_undeliverable_frame_is_reported_and_not_recorded_as_sent(relay_pair):
    """The silent success: the clip never went out, so the send must not say it did."""
    sender = relay_pair.sender
    limit = frame_limit_for(sender.config.relay_max_message_bytes)
    sender.relay.ok = False  # the broker refuses every message

    assert sender.runtime._on_local_sync(oversized_message("big-2", limit)) is False

    errors = events_named(sender.events, "runtime.error")
    assert errors, "a clipboard frame that did not go out has to be reported"
    assert errors[-1]["code"] == "CLIPBOARD_TOO_LARGE"
    # Which limit was hit, and which peer did not get it.
    assert str(limit) in errors[-1]["detail"]
    assert PEER_ID[:12] in errors[-1]["detail"]

    # It was attempted, every piece of it, and the send list does not call it sent.
    assert sender.relay.published, "the cut pieces were offered to the broker"
    assert {route[3] for route in sender.relay.published} == {1}
    rows = sender.runtime.relay_delivery_status()["items"]
    assert [(row["msg_id"], row["status"]) for row in rows] == [("big-2", "queued")]


def test_a_lost_chunk_is_asked_for_again_and_the_frame_still_lands(relay_pair):
    """A broker drops what it has acknowledged, so the chunks repair themselves.

    The receiving half of the file transfer's retransmit: the gap is noticed,
    the sender is asked for exactly those chunks, and the frame completes.  The
    request is only ever sent by ``_sweep_clip_chunks``, which is what the
    maintenance tick runs.
    """
    sender, receiver = relay_pair.sender, relay_pair.receiver
    limit = frame_limit_for(sender.config.relay_max_message_bytes)
    message = oversized_message("big-3", limit)
    assert sender.runtime._on_local_sync(message) is True

    frames = published_frames(sender)
    assert len(frames) > 1
    # The first chunk is lost in flight; the rest arrive.
    deliver(relay_pair, frames[1:])
    assert receiver.runtime._clip_chunks.pending_count() == 1

    receiver.runtime._sweep_clip_chunks()
    request_frame = published_frames(receiver)[-1]
    request = decode_message(request_frame)
    assert request.msg_type == "file_chunk_ack"
    assert request._raw_payload["missing_chunks"] == [0]
    assert is_clip_transfer_id(request._raw_payload["transfer_id"])
    assert request.source_device == PEER_ID, (
        "a source-less relay control frame is dropped at the far end, so the "
        "request would be sent and never heard"
    )

    # The sender answers the request, and the frame completes with the resent piece.
    before = len(sender.relay.published)
    sender.runtime._receive_relay(request_frame, relay_pair.topic)
    resent = published_frames(sender)[before:]
    assert len(resent) == 1, "one missing chunk is one frame back"
    assert decode_message(resent[0])._raw_payload["chunk_index"] == 0

    deliver(relay_pair, resent)
    assert receiver.runtime._clip_chunks.pending_count() == 0
    assert (
        receiver.clipboard.writes[-1].types[ContentType.TEXT]
        == message.content.types[ContentType.TEXT]
    )


def test_a_clipboard_chunk_from_a_known_but_unpaired_peer_is_not_applied(relay_pair):
    """Clipboard content never reaches a peer this machine has not paired with.

    A discovered device is in the repository before it is trusted, so the chunk
    gate cannot be the repository: the pieces of a clipboard frame are held to
    the trust check the frame itself is held to.
    """
    receiver = relay_pair.receiver
    stranger = "cccccccccccc"
    receiver.pairing.add_peer(
        stranger,
        "Stranger",
        identity(stranger).get_identity().certificate_pem,
        paired=False,
    )
    limit = frame_limit_for(receiver.config.relay_max_message_bytes)
    message = oversized_message("big-4", limit)
    frame = encoded_frame(message)

    for chunk in split_frame(frame, 1024, mint_clip_transfer_id()):
        receiver.runtime._on_peer_message(decode_message(chunk), stranger, via_relay=False)

    assert receiver.runtime._clip_chunks.pending_count() == 0, (
        "an unpaired peer's pieces are dropped"
    )
    assert receiver.clipboard.writes == []
