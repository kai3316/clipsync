"""A clipboard frame too big for one relay message, cut to fit and put back.

The relay refuses a frame whose envelope exceeds the broker's message cap, and
that refusal is what made a copied image never arrive on the other machine.  The
carriage for it reuses the binary chunk frame file transfers already speak and
the size arithmetic chat already derives for an internet peer, so what these
tests pin is the two things a second protocol would have got wrong: that every
piece really fits one broker message (proved through the packer the publish path
runs, not by comparing to a constant), and that the pieces put the frame back
byte for byte.

They also pin the bounds: an oversized reassembly, an absurd chunk count, a
chunk from the wrong peer, and a resend request a peer could otherwise make
forever are all refused rather than allowed to park memory here.
"""

from __future__ import annotations

import time
import uuid

from internal.protocol.codec import MAX_FRAME_SIZE, decode_message
from internal.sync.nearby_chat import ChatManager
from internal.transport.relay import derive_key, frame_limit_for, pack_envelope
from internal.transport.relay_chunks import (
    ASSEMBLY_MAX_BYTES,
    ASSEMBLY_TIMEOUT,
    MAX_ASSEMBLIES,
    MAX_CHUNKS,
    MAX_REQUEST_ROUNDS,
    MAX_RESEND_ROUNDS,
    ClipChunkAssembler,
    ClipChunkSends,
    is_clip_transfer_id,
    mint_clip_transfer_id,
    split_frame,
)

# The shipped relay setting: a 64 KiB broker message, so a 48968-byte frame
# limit.  Named rather than read from Config so the test states the relay it is
# talking about.
SHIPPED_RELAY_BYTES = 64 * 1024
SHIPPED_FRAME_LIMIT = frame_limit_for(SHIPPED_RELAY_BYTES)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def oversized_frame(limit: int = SHIPPED_FRAME_LIMIT) -> bytes:
    """A frame a few times past one relay message, with no long repeats."""
    body = bytes(range(256)) * ((3 * limit) // 256 + 1)
    return body[: 3 * limit + 7]


def chunk_payloads(frame: bytes, chunk_size: int) -> list[dict]:
    """Split *frame* and decode each piece the way the receiver's router does."""
    pieces = split_frame(frame, chunk_size, mint_clip_transfer_id())
    out = []
    for piece in pieces:
        msg = decode_message(piece)
        assert msg is not None, "a chunk the transport could not decode is not a chunk"
        assert msg.msg_type == "file_chunk"
        out.append(msg._raw_payload)
    return out


def assemble(payloads, assembler: ClipChunkAssembler, peer_id: str = "peer"):
    """Feed decoded chunks in until one call returns the whole frame."""
    for payload in payloads:
        frame = assembler.add(
            payload["transfer_id"],
            payload["chunk_index"],
            payload["total_chunks"],
            payload["_raw_data"],
            peer_id,
        )
        if frame is not None:
            return frame
    return None


def shipped_chunks(frame: bytes | None = None) -> list[dict]:
    """*frame* cut at the size the shipped relay setting derives."""
    return chunk_payloads(
        oversized_frame() if frame is None else frame,
        ChatManager.relay_chunk_for(SHIPPED_RELAY_BYTES),
    )


class TestTheCutFitsTheRelay:
    def test_every_chunk_of_an_oversized_frame_packs_under_the_limit(self):
        """The proof is the packer, not the constant it was derived from.

        ``relay_chunk_for`` is the arithmetic an internet file transfer is cut
        with; a chunk size that only *equals* a number would be a size nobody
        had shown the broker would take.
        """
        frame = oversized_frame()
        assert len(frame) > SHIPPED_FRAME_LIMIT, "the test frame has to be one the relay refuses"

        chunk_size = ChatManager.relay_chunk_for(SHIPPED_RELAY_BYTES)
        payloads = chunk_payloads(frame, chunk_size)
        assert len(payloads) > 1, "a frame over the limit must cross as more than one piece"

        key = derive_key("a-secret", "another-secret")
        for piece in split_frame(frame, chunk_size, mint_clip_transfer_id()):
            # Raises ValueError for a frame whose envelope would not fit.
            pack_envelope(piece, key, time.time(), max_frame=SHIPPED_FRAME_LIMIT)

    def test_the_split_is_lossless_through_the_standard_decoder(self):
        frame = oversized_frame()
        chunk_size = ChatManager.relay_chunk_for(SHIPPED_RELAY_BYTES)
        payloads = chunk_payloads(frame, chunk_size)

        rebuilt = b"".join(
            payloads[index]["_raw_data"] for index in range(len(payloads))
        )
        assert rebuilt == frame
        assert [payload["chunk_index"] for payload in payloads] == list(range(len(payloads)))
        assert all(payload["total_chunks"] == len(payloads) for payload in payloads)


class TestTheChunkIdCannotCollide:
    def test_uuid4_chunk_ids_are_never_in_the_clipboard_namespace(self):
        assert not is_clip_transfer_id(uuid.uuid4().hex)
        assert not is_clip_transfer_id("0" * 32)
        assert not is_clip_transfer_id(None)
        assert not is_clip_transfer_id("cl")  # short: encode_binary_chunk refuses it

    def test_a_minted_id_is_a_chunk_id_the_codec_will_carry(self):
        transfer_id = mint_clip_transfer_id()
        assert is_clip_transfer_id(transfer_id)
        assert len(transfer_id) == 32
        assert transfer_id.isascii()
        # `encode_binary_chunk` is what mints frames from it; a 33rd character or
        # a space would raise here rather than later, in flight.
        split_frame(b"payload", 4, transfer_id)


class TestReassembly:
    def test_out_of_order_chunks_with_a_duplicate_rebuild_the_frame(self):
        frame = oversized_frame()
        chunk_size = ChatManager.relay_chunk_for(SHIPPED_RELAY_BYTES)
        payloads = chunk_payloads(frame, chunk_size)
        assembler = ClipChunkAssembler()

        # The last chunk first, a duplicate of the middle one, then the rest.
        order = [len(payloads) - 1, len(payloads) // 2, len(payloads) // 2]
        order += [index for index in range(len(payloads) - 1) if index != len(payloads) // 2]
        rebuilt = assemble([payloads[index] for index in order], assembler)

        assert rebuilt == frame
        assert assembler.pending_count() == 0, "a complete frame is not kept"

    def test_a_partial_frame_is_not_returned_and_reports_its_gaps(self):
        payloads = shipped_chunks()
        assembler = ClipChunkAssembler()
        for payload in payloads[:-1]:
            assert assemble([payload], assembler) is None

        transfer_id = payloads[0]["transfer_id"]
        assert assembler.missing(transfer_id) == [len(payloads) - 1]
        # ...and the last one closes it.
        last = payloads[-1]
        assert assemble([last], assembler) is not None
        assert assembler.missing(transfer_id) is None


class TestWhatIsRefused:
    def test_a_chunk_from_another_peer_cannot_join_an_assembly(self):
        payloads = shipped_chunks()
        assembler = ClipChunkAssembler()
        assert assemble([payloads[0]], assembler, peer_id="peer-a") is None

        second = payloads[1]
        assert (
            assembler.add(
                second["transfer_id"],
                second["chunk_index"],
                second["total_chunks"],
                second["_raw_data"],
                "peer-b",
            )
            is None
        )
        # ...and the piece it carried does not fill the gap: the second peer's
        # bytes never join the first peer's frame.
        assert second["chunk_index"] in assembler.missing(second["transfer_id"]), (
            "another peer's chunk filled a gap in this frame"
        )

    def test_an_impossible_chunk_count_or_index_is_refused(self):
        transfer_id = mint_clip_transfer_id()
        assembler = ClipChunkAssembler()
        for index, total in ((0, 0), (0, -1), (5, 3), (0, MAX_CHUNKS + 1), (-1, 3)):
            assert assembler.add(transfer_id, index, total, b"x", "peer") is None
        assert assembler.pending_count() == 0

    def test_a_malformed_chunk_is_refused(self):
        transfer_id = mint_clip_transfer_id()
        assembler = ClipChunkAssembler()
        for index, total, data in (
            ("0", 3, b"x"),
            (True, 3, b"x"),
            (0, "3", b"x"),
            (0, 3, "not bytes"),
            (0, 3, b""),
        ):
            assert assembler.add(transfer_id, index, total, data, "peer") is None
        assert assembler.pending_count() == 0

    def test_an_assembly_past_this_side_s_frame_cap_is_dropped(self):
        """A peer must not be able to make this side hold more than one frame."""
        transfer_id = mint_clip_transfer_id()
        assembler = ClipChunkAssembler(max_bytes=10)
        assert assembler.add(transfer_id, 0, 3, b"x" * 8, "peer") is None
        assert assembler.pending_count() == 1

        assert assembler.add(transfer_id, 1, 3, b"y" * 8, "peer") is None
        assert assembler.pending_count() == 0, "the over-cap assembly is dropped, not grown"

    def test_only_the_newest_assemblies_are_kept(self):
        assembler = ClipChunkAssembler()
        ids = [mint_clip_transfer_id() for _ in range(MAX_ASSEMBLIES + 1)]
        for transfer_id in ids:
            assembler.add(transfer_id, 0, 2, b"x", "peer")
        assert assembler.pending_count() == MAX_ASSEMBLIES
        assert assembler.missing(ids[0]) is None, "the oldest gave way"
        assert assembler.missing(ids[-1]) is not None


class TestRetransmit:
    def test_gaps_are_asked_for_only_after_the_grace(self):
        payloads = shipped_chunks()
        clock = FakeClock()
        assembler = ClipChunkAssembler(clock=clock)
        assert assemble([payloads[0]], assembler, "peer-a") is None

        assert assembler.claim_retransmit(grace=5.0) == [], "not yet silent"
        clock.advance(5.0)
        claimed = assembler.claim_retransmit(grace=5.0)
        assert len(claimed) == 1
        transfer_id, peer_id, missing = claimed[0]
        assert (transfer_id, peer_id) == (payloads[0]["transfer_id"], "peer-a")
        assert missing == list(range(1, len(payloads)))

    def test_a_peer_that_never_answers_is_asked_a_bounded_number_of_times(self):
        payloads = shipped_chunks()
        clock = FakeClock()
        assembler = ClipChunkAssembler(clock=clock)
        assert assemble([payloads[0]], assembler, "peer-a") is None

        rounds = 0
        for _ in range(MAX_REQUEST_ROUNDS + 3):
            clock.advance(5.0)
            rounds += len(assembler.claim_retransmit(grace=5.0))
        assert rounds == MAX_REQUEST_ROUNDS

    def test_a_stale_assembly_is_swept_away(self):
        clock = FakeClock()
        assembler = ClipChunkAssembler(clock=clock)
        transfer_id = mint_clip_transfer_id()
        assembler.add(transfer_id, 0, 2, b"x", "peer")

        assert assembler.sweep() == []
        clock.advance(ASSEMBLY_TIMEOUT + 1)
        assert assembler.sweep() == [transfer_id]
        assert assembler.pending_count() == 0


class TestResendRequests:
    def test_the_requested_chunks_are_the_ones_returned(self):
        frames = [b"chunk-0", b"chunk-1", b"chunk-2"]
        transfer_id = mint_clip_transfer_id()
        sends = ClipChunkSends()
        sends.remember(transfer_id, "peer", frames)

        assert sends.resend(transfer_id, "peer", [2, 0]) == [b"chunk-0", b"chunk-2"]
        assert sends.resend(transfer_id, "peer", []) == []
        assert sends.resend(transfer_id, "peer", [99, "nonsense"]) == []

    def test_a_frame_never_sent_or_sent_elsewhere_is_not_answered(self):
        sends = ClipChunkSends()
        transfer_id = mint_clip_transfer_id()
        sends.remember(transfer_id, "peer-a", [b"only"])

        assert sends.resend(mint_clip_transfer_id(), "peer-a", [0]) == []
        assert sends.resend(transfer_id, "peer-b", [0]) == []
        assert sends.resend("0" * 32, "peer-a", [0]) == []

    def test_resends_stop_at_the_round_budget_and_expire(self):
        clock = FakeClock()
        sends = ClipChunkSends(clock=clock, hold=60.0)
        transfer_id = mint_clip_transfer_id()
        sends.remember(transfer_id, "peer", [b"only"])

        for _ in range(MAX_RESEND_ROUNDS):
            assert sends.resend(transfer_id, "peer", [0]) == [b"only"]
        assert sends.resend(transfer_id, "peer", [0]) == [], "the round budget ends it"
        assert sends.tracked_count() == 0

        sends.remember(transfer_id, "peer", [b"only"])
        clock.advance(61.0)
        assert sends.resend(transfer_id, "peer", [0]) == []
        assert sends.tracked_count() == 0

    def test_only_a_bounded_number_of_sent_frames_is_kept(self):
        sends = ClipChunkSends(max_tracked=2)
        ids = [mint_clip_transfer_id() for _ in range(3)]
        for transfer_id in ids:
            sends.remember(transfer_id, "peer", [b"only"])
        assert sends.tracked_count() == 2
        assert sends.resend(ids[0], "peer", [0]) == []


def test_the_reassembly_cap_is_the_encoder_s_own_frame_cap():
    """One cap, not two: the rebuilt frame is one this app is willing to encode."""
    assert ASSEMBLY_MAX_BYTES == MAX_FRAME_SIZE
    assert ClipChunkAssembler()._max_bytes == MAX_FRAME_SIZE
