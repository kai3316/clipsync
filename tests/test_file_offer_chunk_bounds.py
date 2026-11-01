"""An offer cannot make this side allocate memory in proportion to its numbers.

``chunk_size`` and ``file_size`` both come from the sender, and the receiver
used to turn them straight into ``set(range(total_chunks))`` — before the
accept prompt, before any consent.  A 2 GiB offer cut at one byte asked for a
two-billion element set; smaller cuts asked for more.  The missing-chunk
bookkeeping is now a bitmap and the cut is refused when it is absurd, which is
the only safe answer: the receiver slices at ``index * chunk_size``, so it
cannot re-cut the file without corrupting it.
"""

from __future__ import annotations

import tracemalloc

import pytest

from internal.sync.file_transfer import (
    MAX_TOTAL_CHUNKS,
    FileTransferManager,
    _MissingChunks,
)


class Router:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def on_request(self, transfer_id, file_name, file_size, mime, send_fn, *, relay):
        self.requests.append({"transfer_id": transfer_id, "file_size": file_size})


class Rejects:
    """Collects the frames this side sends back (they are wire-framed, not JSON)."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def __call__(self, raw):
        self.frames.append(raw if isinstance(raw, bytes) else bytes(raw))


def _offer(transfer_id="t1", *, size=1000, chunk_size=None, kind="file") -> dict:
    payload = {
        "msg_type": "file_request",
        "transfer_id": transfer_id,
        "file_name": "a.bin",
        "file_size": size,
        "mime_type": "application/octet-stream",
        "kind": kind,
    }
    if chunk_size is not None:
        payload["chunk_size"] = chunk_size
    return payload


def _manager(tmp_path):
    manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
    router = Router()
    manager.set_on_transfer_request(router.on_request)
    return manager, router


@pytest.mark.parametrize(
    "payload",
    [
        _offer(size=2 * 1024**3, chunk_size=1),
        _offer(size=2 * 1024**3, chunk_size=1024),
        _offer(size=2 * 1024**3, chunk_size=4096),
        # One byte under the size that would just fit the cap.
        _offer(size=2 * 1024**3, chunk_size=2 * 1024**3 // MAX_TOTAL_CHUNKS - 1),
    ],
)
def test_an_oversized_cut_is_refused_before_anything_is_allocated(tmp_path, payload):
    manager, router = _manager(tmp_path)
    send = Rejects()
    tracemalloc.start()
    try:
        manager.handle_message("file_request", payload, send, "peer-b")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert router.requests == [], "no prompt for a cut this side will not accept"
    assert all(
        row.get("transfer_id") != payload["transfer_id"] for row in manager.get_transfers()
    ), "a refused offer must not appear in the transfers list"
    assert any(b"file_reject" in frame for frame in send.frames)
    # A set of two billion integers would be tens of gigabytes; the refusal
    # path must not go anywhere near that.
    assert peak < 8 * 1024 * 1024, f"refusal allocated {peak} bytes"


def test_a_normal_cut_still_reaches_the_prompt(tmp_path):
    manager, router = _manager(tmp_path)
    manager.handle_message("file_request", _offer(size=1000, chunk_size=250), lambda _d: None, "peer-b")
    assert len(router.requests) == 1


def test_the_missing_chunk_map_costs_a_bit_per_chunk():
    chunks = _MissingChunks(1_000_000)
    assert len(chunks) == 1_000_000
    assert 0 in chunks and 999_999 in chunks
    assert 1_000_000 not in chunks
    assert len(chunks._bits) == 125_000

    chunks.discard(0)
    assert 0 not in chunks
    assert len(chunks) == 999_999
    assert list(chunks)[0] == 1


def test_a_partial_last_byte_is_not_iterated_as_missing():
    chunks = _MissingChunks(10)  # 2 spare bits in the last byte
    assert list(chunks) == list(range(10))
    for index in range(10):
        chunks.discard(index)
    assert not chunks
    assert list(chunks) == []
