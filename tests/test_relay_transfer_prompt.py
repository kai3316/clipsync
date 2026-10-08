"""An incoming offer says which route it was cut for, before the reader accepts.

A file that arrives over the internet relay is slower than the same file moving
between two devices on one network, and the reader is asked to accept it -- so
the prompt has to know which route it is asking about.  Nothing new crosses the
wire for that: `send_file` sizes a send for the route it may cross, cutting a
relay-bound offer at one broker message and leaving a LAN offer at this build's
`CHUNK_SIZE` (see `lan.py::_file_route_for`).  The receiver reads the sender's
number off the offer and hands the resulting fact to the prompt explicitly,
rather than every caller re-deriving it from the chunk size.

The control is the other half: a default-size offer reports not-relay, so a LAN
arrival asks the reader nothing new.
"""

from __future__ import annotations

from internal.sync.file_transfer import CHUNK_SIZE, FileTransferManager


class Router:
    """Stands in for the runtime's registrar and records what the prompt was told."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def on_request(self, transfer_id, file_name, file_size, mime, send_fn, *, relay):
        self.requests.append(
            {"transfer_id": transfer_id, "file_name": file_name, "relay": relay}
        )


def _offer(transfer_id: str, *, chunk_size: int | None = None, size: int = 1000) -> dict:
    """A `file_request` as a peer sends one, with the chunk size left out for LAN."""
    payload = {
        "msg_type": "file_request",
        "transfer_id": transfer_id,
        "file_name": "a.bin",
        "file_size": size,
        "mime_type": "application/octet-stream",
        "kind": "file",
    }
    if chunk_size is not None:
        payload["chunk_size"] = chunk_size
    return payload


def _receiver(tmp_path):
    manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
    router = Router()
    manager.set_on_transfer_request(router.on_request)
    return manager, router


def _offer_to(manager, payload):
    manager.handle_message("file_request", payload, lambda data: None, "peer-b")


def test_a_relay_sized_offer_reaches_the_prompt_as_relay(tmp_path):
    """The offer's own size is the route, and the prompt is told it explicitly."""
    manager, router = _receiver(tmp_path)
    # What a relay-bound send is actually cut at: one broker message, smaller
    # than this build's default.
    relay_size = CHUNK_SIZE // 6
    assert relay_size != CHUNK_SIZE, "the two routes have to differ for this to mean anything"

    _offer_to(manager, _offer("a" * 32, chunk_size=relay_size))

    assert len(router.requests) == 1
    assert router.requests[0]["relay"] is True
    # And the same fact is on the row the transfers page draws, which is where
    # the reader is actually asked: a pending row with no warning beside its
    # accept button would be a prompt that does not mention the route.
    row = manager.get_transfers()[0]
    assert row["transfer_id"] == "a" * 32
    assert row["relay"] is True


def test_a_default_sized_offer_reports_not_relay(tmp_path):
    """The control: the LAN case is unchanged, whether the field is absent or
    explicitly this build's default."""
    for index, chunk_size in enumerate((None, CHUNK_SIZE)):
        manager, router = _receiver(tmp_path / str(index))
        transfer_id = str(index) * 32

        _offer_to(manager, _offer(transfer_id, chunk_size=chunk_size))

        assert len(router.requests) == 1
        assert router.requests[0]["relay"] is False, (
            f"chunk_size={chunk_size!r} is the LAN route and must not warn anyone"
        )
        row = manager.get_transfers()[0]
        assert row["transfer_id"] == transfer_id
        assert row["relay"] is False
