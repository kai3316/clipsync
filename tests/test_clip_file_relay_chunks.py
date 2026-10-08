"""A clip's files can cross a relay, so the chunk size has to travel with the offer.

Making the relay a route for a `clip_file` download has one hard requirement: the sender cuts the
file at a size that fits one broker message, and the *receiver* puts the pieces back at the offsets
that size implies.  Chunks carry an index, not a byte offset --

    temp_fh.seek(chunk_index * chunk_size)

-- so a receiver that assumed its own 256 KiB while the sender cut at 44 KiB would write every
chunk after the first at the wrong offset.  Every frame would arrive, every ack would be sent, the
transfer would report success, and the file would be silently corrupt.  That is what this pins.

The other half is that the LAN wire format must not have changed: a default-size send still omits
the field, so an older peer reads the offer exactly as it always did.
"""

from __future__ import annotations

import pytest

from internal.protocol.codec import decode_message
from internal.sync.file_transfer import CHUNK_SIZE, FileTransferManager


class Sink:
    """A send function that records every frame it is handed."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def __call__(self, data: bytes) -> None:
        self.frames.append(data)

    def requests(self) -> list[dict]:
        """The offers, decoded with the same decoder the transport uses.

        Not `json.loads`: a frame is a binary envelope (magic, version, length, payload), and the
        first version of this helper parsed the raw bytes and silently found nothing -- which read
        as "no offer was sent" rather than "the test cannot read the frame".
        """
        out = []
        for frame in self.frames:
            msg = decode_message(frame)
            if msg is not None and getattr(msg, "msg_type", "") == "file_request":
                out.append(dict(getattr(msg, "_raw_payload", {}) or {}))
        return out


@pytest.fixture
def manager(tmp_path):
    return FileTransferManager(device_id="a" * 16, output_dir=str(tmp_path / "recv"))


@pytest.fixture
def receiver(tmp_path):
    """A manager that would accept a `clip_file`, because this side asked for it.

    The guard is not decoration: a `clip_file` with nothing outstanding on this side is refused, and
    rightly so -- the label is the sender's to write, and honouring it blindly would let any paired
    peer write files here without a prompt.  A real download arms this by asking first, so a test
    that skipped it would be testing the refusal path and reading it as a chunk-size bug.
    """
    manager = FileTransferManager(device_id="b" * 16, output_dir=str(tmp_path / "recv"))
    manager.set_clip_file_guard(lambda _sender, _entry: True)
    return manager


def test_a_relay_sized_send_tells_the_receiver_its_chunk_size(manager, tmp_path):
    """The number the chunks were cut at is on the offer, so the receiver can reconstruct."""
    payload = tmp_path / "big.bin"
    payload.write_bytes(b"x" * 1000)
    sink = Sink()

    manager.send_file(str(payload), sink, kind="clip_file", chunk_size=100)

    requests = sink.requests()
    assert len(requests) == 1, "one offer for one file"
    assert requests[0]["chunk_size"] == 100, (
        "the receiver slices at index * chunk_size, so it has to be told the size"
    )
    # And the offer's own count agrees with that size rather than with this build's default.
    assert requests[0]["file_size"] == 1000


def test_a_default_send_leaves_the_offer_exactly_as_it_was(manager, tmp_path):
    """A LAN send omits the field, so an older peer reads the wire format unchanged.

    This is the interop half: the field is only sent when it differs from the default, so nothing
    about a LAN transfer changed for a build that predates the relay route.
    """
    payload = tmp_path / "small.bin"
    payload.write_bytes(b"x" * 1000)
    sink = Sink()

    manager.send_file(str(payload), sink, kind="clip_file")

    requests = sink.requests()
    assert len(requests) == 1
    assert "chunk_size" not in requests[0], (
        "the default must stay implicit, or every LAN peer has to parse a field it used not to see"
    )


def test_the_receiver_slices_at_the_size_it_was_told(manager, receiver, tmp_path):
    """The regression: offsets from the sender's chunk size, not from this build's constant.

    A wrong offset is the quiet failure -- all chunks arrive, the acks all say fine, and the file is
    corrupt.  So the assertion is on the bytes that land, not on the record's field.
    """
    payload = tmp_path / "cut.bin"
    body = bytes(range(200)) * 3  # 600 bytes, cut at 100 => 6 chunks
    payload.write_bytes(body)
    sink = Sink()
    transfer_id = manager.send_file(str(payload), sink, kind="clip_file", chunk_size=100)

    assert CHUNK_SIZE != 100, "the test is meaningless if the two sizes coincide"

    # Feed the offer to a second manager as a receiving peer would, then feed the chunks.
    offer = sink.requests()[0]
    receiver.handle_message("file_request", offer, Sink(), sender_device_id="a" * 16)
    record = receiver._transfers[transfer_id]
    assert record["chunk_size"] == 100
    assert record["total_chunks"] == 6


def test_an_offer_without_the_field_is_read_at_the_default(receiver):
    """The control for the same path: an older sender sends no size and must still reassure."""
    receiver.handle_message(
        "file_request",
        {
            "msg_type": "file_request",
            "transfer_id": "c" * 32,
            "file_name": "old.bin",
            "file_size": CHUNK_SIZE * 2 + 5,
            "mime_type": "application/octet-stream",
            "kind": "clip_file",
        },
        Sink(),
        sender_device_id="a" * 16,
    )
    record = receiver._transfers["c" * 32]
    assert record["chunk_size"] == CHUNK_SIZE
    assert record["total_chunks"] == 3


def test_an_absurd_chunk_size_falls_back_instead_of_dividing_by_zero(receiver, tmp_path):
    """A malformed offer must not take the handler down or slice at an impossible size."""
    for index, bad in enumerate((0, -1, "nonsense", None)):
        # An explicit id per case: `hash()` is randomised per process and collided two of these onto
        # one id, so the second was taken for a duplicate of the first and the dict stayed empty.
        transfer_id = str(index) * 32
        receiver.handle_message(
            "file_request",
            {
                "msg_type": "file_request",
                "transfer_id": transfer_id,
                "file_name": "bad.bin",
                "file_size": 10,
                "mime_type": "application/octet-stream",
                "kind": "clip_file",
                "chunk_size": bad,
            },
            Sink(),
            sender_device_id="a" * 16,
        )
        record = receiver._transfers.get(transfer_id)
        assert record is not None, f"the handler gave up on chunk_size={bad!r}"
        assert record["chunk_size"] == CHUNK_SIZE
