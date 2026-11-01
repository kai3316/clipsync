"""M1: the open-to-all default can no longer exhaust the disk or the slots.

Two gaps the review named: under ``file_open_to_all`` every reachable peer's
legal-size file was accepted with no disk check and no concurrency bound, so a
flood filled the volume (and then starved the history store, H1).  Both are now
refused up front -- a file the volume cannot hold, and a peer already at its
inbound cap.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

from internal.sync.file_transfer import (
    DISK_HEADROOM_RATIO,
    MAX_INCOMING_PER_PEER,
    FileTransferManager,
)


class Frames:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def __call__(self, raw):
        self.frames.append(raw if isinstance(raw, bytes) else bytes(raw))


def _offer(transfer_id, *, size=1000, kind="file") -> dict:
    return {
        "msg_type": "file_request",
        "transfer_id": transfer_id,
        "file_name": "a.bin",
        "file_size": size,
        "mime_type": "application/octet-stream",
        "kind": kind,
    }


def _manager(tmp_path, *, open_to_all=True):
    manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
    manager._file_open_to_all = open_to_all
    return manager


def test_a_file_the_volume_cannot_hold_is_refused(tmp_path, monkeypatch):
    """The disk guard lives in accept_transfer, so it covers every inbound kind."""
    manager = _manager(tmp_path)
    # Report a volume with almost no free space.
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(total=1, used=1, free=10)
    )
    send = Frames()
    manager.handle_message("file_request", _offer("t1", size=1000), send, "peer-b")

    assert any(b"file_reject" in frame for frame in send.frames), "no free space -> file_reject"
    assert all(
        row.get("transfer_id") != "t1" for row in manager.get_transfers()
    ), "a refused transfer must not sit in the list"


def test_a_file_that_fits_is_still_accepted(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10**12, used=0, free=10**12),
    )
    send = Frames()
    manager.handle_message("file_request", _offer("t1", size=1000), send, "peer-b")

    assert any(b"file_ack" in frame for frame in send.frames), "room to spare -> file_ack"
    # 1000 bytes plus 10% headroom fits in a terabyte.
    assert free_headroom_check(1000, 10**12)


def free_headroom_check(size, free):
    return free >= size + int(size * DISK_HEADROOM_RATIO)


def test_a_peer_past_its_cap_is_refused(tmp_path):
    """One peer flooding the open default cannot occupy every inbound slot."""
    manager = _manager(tmp_path)
    send = Frames()
    accepted = 0
    for index in range(1, MAX_INCOMING_PER_PEER + 2):
        send.frames.clear()
        manager.handle_message(
            "file_request", _offer(f"t{index}", size=1000), send, "flooder"
        )
        if any(b"file_ack" in frame for frame in send.frames):
            accepted += 1
        else:
            # The first rejection should be exactly the offer that exceeds the cap.
            assert index == MAX_INCOMING_PER_PEER + 1
            assert any(b"file_reject" in frame for frame in send.frames)

    assert accepted == MAX_INCOMING_PER_PEER, (
        f"expected {MAX_INCOMING_PER_PEER} accepted, got {accepted}"
    )


def test_the_cap_is_per_peer_not_global(tmp_path):
    """A second peer gets a fresh cap; the limit is not shared across peers."""
    manager = _manager(tmp_path)
    send = Frames()
    # Fill peer A to the cap.
    for index in range(1, MAX_INCOMING_PER_PEER + 1):
        manager.handle_message(
            "file_request", _offer(f"a{index}", size=1000), Frames(), "peer-a"
        )
    # Peer B's first offer must still be accepted.
    manager.handle_message("file_request", _offer("b1", size=1000), send, "peer-b")
    assert any(b"file_ack" in frame for frame in send.frames)
