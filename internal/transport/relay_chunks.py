"""Clipboard frames too large for one relay message, cut to fit and put back.

The relay refuses a frame whose envelope exceeds the broker's own message cap
(``relay.frame_limit_for``), and the clipboard path had no answer for that:
``RelayTransport.publish`` logged "relay publish skipped", returned False, the
frame was queued, retried to the retry budget and dropped -- while the capture
that produced it was recorded as synced, because ``LanRuntime._on_local_sync``
returned True whatever the broker did.  That is the reported "copied it on the
Mac and the Windows machine never saw it".

This module is the carriage for that frame, and it adds no wire *format*: the
pieces are the binary chunk frames file transfers already speak
(``codec.encode_binary_chunk``), cut with the same size arithmetic chat derives
for an internet peer (``ChatManager.relay_chunk_for``), so there is one chunk
layout and one size derivation for both.  The retransmit request is the file
transfer's ``file_chunk_ack`` for the same reason.

The one thing that is new on the wire is the chunk *id*.  Every other binary
chunk id in this app is ``uuid.uuid4().hex`` -- 32 lowercase hex characters --
so a 32-character id beginning ``cl`` cannot be one: ``l`` is not a hex digit.
A peer that predates this build drops a chunk for an id it never opened, exactly
as it already drops a chunk for an unknown transfer, so the wire stays
backward-safe in both directions.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

from internal.protocol.codec import MAX_FRAME_SIZE, encode_binary_chunk

logger = logging.getLogger(__name__)

# The namespace mark on a fragmented clipboard frame's chunk id.  Two characters
# rather than one so the id reads as deliberate; see the module docstring for
# why it cannot collide with a uuid4 hex id.
CLIP_CHUNK_ID_PREFIX = "cl"
# ``encode_binary_chunk`` requires exactly this many ascii characters.
CLIP_CHUNK_ID_LEN = 32
# A frame is capped at MAX_FRAME_SIZE (10 MiB) by the capture path, so this is a
# bound on nonsense rather than on any frame this app sends: the smallest chunk
# ``relay_chunk_for`` can return is 1 byte, which would be 10 million chunks.
MAX_CHUNKS = 4096
# How long a partial assembly is kept before it is given up on.  Generous: the
# sender answers a resend request on its own thread, and the receiver's own
# request cadence is slower than this.
ASSEMBLY_TIMEOUT = 120.0
# The most a reassembly may grow to.  The frame being rebuilt is a clipboard
# message the capture path already refuses past this bound
# (``LanRuntime._on_local_sync``), so the same number is the one a peer must not
# be able to exceed here either.
ASSEMBLY_MAX_BYTES = MAX_FRAME_SIZE
# Silence with gaps still missing: long enough not to fire mid-pass on a slow
# link, short enough that a dropped burst is repaired while the user is still
# looking at the other machine's screen.
STALL_GRACE = 5.0
# Resend requests per assembly.  Each round re-sends the whole missing set, so a
# link that is losing most of a burst converges quickly or not at all -- and not
# at all has to end somewhere rather than pinning memory for the process's life.
MAX_REQUEST_ROUNDS = 8
# Partial assemblies kept at once.  A user copies one thing at a time; a peer
# that opens more than this is not doing that.
MAX_ASSEMBLIES = 4
# Sent chunk sets kept so a resend request has something to answer with, and how
# long they are kept.  The hold covers the receiver's whole request window.
MAX_TRACKED_SENDS = 4
SEND_HOLD = 300.0
# Resend rounds for one sent frame; past this the receiver is not keeping up and
# answering again only feeds the loss.
MAX_RESEND_ROUNDS = MAX_REQUEST_ROUNDS


def mint_clip_transfer_id() -> str:
    """A fresh chunk id in the fragmented-clipboard namespace."""
    return CLIP_CHUNK_ID_PREFIX + uuid.uuid4().hex[: CLIP_CHUNK_ID_LEN - len(CLIP_CHUNK_ID_PREFIX)]


def is_clip_transfer_id(transfer_id: object) -> bool:
    """Whether *transfer_id* names a fragmented clipboard frame."""
    return (
        isinstance(transfer_id, str)
        and len(transfer_id) == CLIP_CHUNK_ID_LEN
        and transfer_id.startswith(CLIP_CHUNK_ID_PREFIX)
    )


def split_frame(frame: bytes, chunk_size: int, transfer_id: str) -> list[bytes]:
    """Cut *frame* into binary chunk frames of at most *chunk_size* bytes.

    *chunk_size* comes from ``ChatManager.relay_chunk_for``: the largest raw
    chunk whose binary frame still packs an envelope under the broker's message
    limit, which is the same number an internet file transfer is cut at.
    """
    size = int(chunk_size)
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if not is_clip_transfer_id(transfer_id):
        raise ValueError("transfer_id is not in the clipboard chunk namespace")
    total = max(1, (len(frame) + size - 1) // size)
    return [
        encode_binary_chunk(transfer_id, index, total, frame[index * size : (index + 1) * size])
        for index in range(total)
    ]


class ClipChunkAssembler:
    """Put one fragmented clipboard frame back together.

    Chunks are held in memory rather than streamed to a file: the frame they
    rebuild is a clipboard message the capture path already bounds at
    ``MAX_FRAME_SIZE``, and ``add`` enforces that bound as the pieces arrive so
    a peer cannot make this side hold an unbounded reassembly.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = ASSEMBLY_TIMEOUT,
        max_bytes: int = ASSEMBLY_MAX_BYTES,
    ):
        self._clock = clock
        self._timeout = float(timeout)
        self._max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, dict] = OrderedDict()

    def add(
        self,
        transfer_id: object,
        chunk_index: object,
        total_chunks: object,
        data: object,
        peer_id: str = "",
    ) -> bytes | None:
        """Take one chunk; return the whole frame once the last one has landed.

        ``None`` means "not complete" -- either a piece was taken, or the chunk
        was refused as malformed, of the wrong shape for an assembly already in
        progress, or past this side's own frame cap.
        """
        if not is_clip_transfer_id(transfer_id):
            return None
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            return None
        if isinstance(total_chunks, bool) or not isinstance(total_chunks, int):
            return None
        if not isinstance(data, bytes) or not data:
            return None
        if chunk_index < 0 or not 0 < total_chunks <= MAX_CHUNKS or chunk_index >= total_chunks:
            return None
        now = self._clock()
        with self._lock:
            state = self._pending.get(transfer_id)
            if state is None:
                self._evict_oldest_locked()
                state = {
                    "peer": peer_id,
                    "total": total_chunks,
                    "chunks": {},
                    "bytes": 0,
                    "started": now,
                    "last": now,
                    "rounds": 0,
                }
                self._pending[transfer_id] = state
            elif state["total"] != total_chunks:
                logger.warning(
                    "Clipboard frame %s: chunk declares %d total, assembly has %d -- dropped",
                    transfer_id[:8],
                    total_chunks,
                    state["total"],
                )
                return None
            elif state["peer"] and peer_id and state["peer"] != peer_id:
                logger.warning(
                    "Clipboard frame %s: chunk from %s, assembly belongs to %s -- dropped",
                    transfer_id[:8],
                    str(peer_id)[:12],
                    str(state["peer"])[:12],
                )
                return None
            elif chunk_index in state["chunks"]:
                # A resend of a chunk already held: not progress, not an error.
                state["last"] = now
                return None
            elif state["bytes"] + len(data) > self._max_bytes:
                logger.warning(
                    "Clipboard frame %s exceeds the %d-byte frame cap -- assembly dropped",
                    transfer_id[:8],
                    self._max_bytes,
                )
                self._pending.pop(transfer_id, None)
                return None
            state["chunks"][chunk_index] = data
            state["bytes"] += len(data)
            state["last"] = now
            if len(state["chunks"]) < state["total"]:
                return None
            frame = b"".join(state["chunks"][index] for index in range(state["total"]))
            self._pending.pop(transfer_id, None)
        return frame

    def missing(self, transfer_id: str) -> list[int] | None:
        """Sorted chunk indices still missing, or None for an unknown frame."""
        with self._lock:
            state = self._pending.get(transfer_id)
            if state is None:
                return None
            return sorted(set(range(state["total"])) - set(state["chunks"]))

    def claim_retransmit(
        self,
        grace: float = STALL_GRACE,
        max_rounds: int = MAX_REQUEST_ROUNDS,
    ) -> list[tuple[str, str, list[int]]]:
        """Gaps that have gone quiet long enough to ask the sender about again.

        Returns ``[(transfer_id, peer_id, missing_indices), ...]``.  Each claim
        counts against that assembly's round budget and restarts its grace, so a
        peer that never answers is asked a bounded number of times.
        """
        now = self._clock()
        claimed: list[tuple[str, str, list[int]]] = []
        with self._lock:
            for transfer_id, state in self._pending.items():
                missing = sorted(set(range(state["total"])) - set(state["chunks"]))
                if not missing or not state["peer"]:
                    continue
                if state["rounds"] >= max_rounds:
                    continue
                if now - state["last"] < grace:
                    continue
                state["rounds"] += 1
                state["last"] = now
                claimed.append((transfer_id, str(state["peer"]), missing))
        return claimed

    def sweep(self) -> list[str]:
        """Drop assemblies past their timeout; return the ids dropped."""
        now = self._clock()
        with self._lock:
            expired = [
                transfer_id
                for transfer_id, state in self._pending.items()
                if now - state["started"] > self._timeout
            ]
            for transfer_id in expired:
                self._pending.pop(transfer_id, None)
        if expired:
            logger.info("Gave up on %d unassembled clipboard frame(s)", len(expired))
        return expired

    def drop(self, transfer_id: str) -> None:
        with self._lock:
            self._pending.pop(transfer_id, None)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _evict_oldest_locked(self) -> None:
        """Keep the newest MAX_ASSEMBLIES; caller holds the lock."""
        while len(self._pending) >= MAX_ASSEMBLIES:
            dropped, _state = self._pending.popitem(last=False)
            logger.info("Dropped oldest unassembled clipboard frame %s for room", dropped[:8])


class ClipChunkSends:
    """The chunk sets this side just sent, so a resend request can be answered.

    Bounded and time-limited: the sender's half of the retransmit must not
    become a place a peer can park memory by asking forever.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        hold: float = SEND_HOLD,
        max_tracked: int = MAX_TRACKED_SENDS,
    ):
        self._clock = clock
        self._hold = float(hold)
        self._max_tracked = int(max_tracked)
        self._lock = threading.Lock()
        self._sends: OrderedDict[str, dict] = OrderedDict()

    def remember(self, transfer_id: str, peer_id: str, frames: list[bytes]) -> None:
        if not is_clip_transfer_id(transfer_id) or not frames:
            return
        with self._lock:
            self._sends.pop(transfer_id, None)
            self._sends[transfer_id] = {
                "peer": peer_id,
                "frames": list(frames),
                "expires": self._clock() + self._hold,
                "rounds": 0,
            }
            while len(self._sends) > self._max_tracked:
                self._sends.popitem(last=False)

    def resend(self, transfer_id: str, peer_id: str, missing: object) -> list[bytes]:
        """The frames to send again for *missing*, or an empty list.

        Empty when the frame was never sent from here, was sent to another peer,
        has expired, or has already been resent to its round budget.
        """
        if not is_clip_transfer_id(transfer_id):
            return []
        if isinstance(missing, bool) or not isinstance(missing, (list, tuple, set)):
            return []
        now = self._clock()
        with self._lock:
            state = self._sends.get(transfer_id)
            if state is None or (peer_id and state["peer"] and state["peer"] != peer_id):
                return []
            if now > state["expires"]:
                self._sends.pop(transfer_id, None)
                return []
            if state["rounds"] >= MAX_RESEND_ROUNDS:
                self._sends.pop(transfer_id, None)
                return []
            frames = state["frames"]
            wanted = sorted(
                {
                    int(index)
                    for index in missing
                    if isinstance(index, int)
                    and not isinstance(index, bool)
                    and 0 <= index < len(frames)
                }
            )
            if not wanted:
                return []
            state["rounds"] += 1
        return [frames[index] for index in wanted]

    def forget(self, transfer_id: str) -> None:
        with self._lock:
            self._sends.pop(transfer_id, None)

    def sweep(self) -> list[str]:
        now = self._clock()
        with self._lock:
            expired = [
                transfer_id
                for transfer_id, state in self._sends.items()
                if now > state["expires"]
            ]
            for transfer_id in expired:
                self._sends.pop(transfer_id, None)
        return expired

    def tracked_count(self) -> int:
        with self._lock:
            return len(self._sends)
