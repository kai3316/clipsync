"""Internet-paired download: B presses 下载, A serves the file over the relay.

Two real ``LanRuntime`` instances are wired to each other through a fake relay
that delivers a published frame to the other runtime exactly as
``RelayTransport`` would.  What this covers end to end is the pull path no other
test drives across both sides:

* B's ``clip_file_request`` reaches A;
* A's answer (``file_request``) reaches B and is recognised as the download B
  asked for (no prompt, even with ``file_open_to_all`` off);
* the accept, chunks, chunk acks and completion all travel;
* the file lands on B, and **A draws no transfer row and no completion notice**:
  the transfer belongs to the side that pressed the button.
"""

import sys
import time

import pytest

sys.path.insert(0, r"D:\copyboard")

from internal.application.events import EventJournal
from internal.config.config import Config
from internal.infrastructure.runtime.lan import LanRuntime
from tests.sidecar.test_lan_runtime import (
    Clipboard,
    Discovery,
    History,
    Monitor,
    Transport,
    identity,
)

A_ID = "a1a2a3a4a5a6"
B_ID = "b1b2b3b4b5b6"
SECRET = "netpair-secret"


class _Link:
    """One runtime's end of the fake broker; publishes to the other runtime."""

    state = "online"

    def __init__(self):
        self.runtime = None
        self.other = None
        self.published = []

    def publish(self, frame, topic, key, qos=0):
        self.published.append((frame, topic, key, qos))
        if self.other is not None:
            self.other.runtime._receive_relay(frame, topic, 0)
        return True

    def refresh_channels(self):
        return None


class _Peer:
    def __init__(self, device_id, name, peer_id, receive_dir, file_open_to_all=True):
        self.config = Config(
            device_id=device_id,
            device_name=name,
            encryption_enabled=False,
            retry_capture_enabled=False,
            sync_debounce=0.01,
        )
        self.config.internet_sync_enabled = True
        self.config.netpair_secrets = {peer_id: SECRET}
        self.config.file_open_to_all = file_open_to_all
        self.config.file_receive_dir = str(receive_dir)
        self.pairing = identity(device_id)
        self.transport, self.discovery = Transport(), Discovery()
        self.clipboard, self.history, self.events = Clipboard(), History(), EventJournal()
        self.runtime = LanRuntime(
            self.config,
            self.pairing,
            None,
            self.history,
            self.events,
            lambda: None,
            monitor=Monitor(),
            reader=self.clipboard,
            writer=self.clipboard,
            transport=self.transport,
            discovery=self.discovery,
            open_url=lambda _url: None,
        )
        self.runtime.REFRESH_INTERVAL = 60
        self.link = _Link()
        self.link.runtime = self.runtime

    def start(self):
        self.runtime.start()
        self.config.internet_sync_enabled = True
        self.runtime.relay = self.link
        self.runtime.internet_pairing.attach_relay(self.link)
        return self

    def stop(self):
        self.transport.stop_result = True
        self.runtime.relay = None
        self.runtime.stop()


def _pair(tmp_path, file_open_to_all=True):
    a = _Peer(A_ID, "A", B_ID, tmp_path / "a-received")
    b = _Peer(B_ID, "B", A_ID, tmp_path / "b-received", file_open_to_all)
    a.link.other, b.link.other = b.link, a.link
    return a, b


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pair(tmp_path):
    a, b = _pair(tmp_path)
    a.start()
    b.start()
    try:
        yield a, b, tmp_path
    finally:
        a.stop()
        b.stop()


def _source(tmp_path, payload):
    path = tmp_path / "report.txt"
    path.write_bytes(payload)
    return path


def test_b_presses_download_and_a_serves_the_file_over_the_relay(pair):
    a, b, tmp_path = pair
    payload = b"hello from the internet-paired machine"
    source = _source(tmp_path, payload)
    a.runtime.set_clip_file_source(lambda _entry: ([str(source)], "ok"))

    assert b.runtime.request_entry_files(A_ID, "entry-1") == {"requested": True}

    assert _wait_until(
        lambda: any(row.get("status") == "completed" for row in b.runtime.transfers()["history"])
    ), f"download never completed: {b.runtime.transfers()}"
    landed = tmp_path / "b-received" / "report.txt"
    assert landed.exists() and landed.read_bytes() == payload

    # The serving side: tracked work, but not the user's transfer.  Neither an
    # active row, nor a history row, nor a completion event.
    assert a.runtime.transfers()["active"] == []
    assert a.runtime.transfers()["history"] == []
    names = [event["name"] for event in a.events.since(0)[0]]
    assert "transfer.complete" not in names
    assert "transfer.progress" not in names
    # ...while the asking side does get one completed download row.
    rows = b.runtime.transfers()["history"]
    assert len(rows) == 1
    assert rows[0]["direction"] == "down"
    assert rows[0]["success"] is True


def test_a_download_is_taken_without_a_prompt_even_when_file_open_to_all_is_off(tmp_path):
    """The ledger, not the setting, is what admits the file this side asked for."""
    a, b = _pair(tmp_path, file_open_to_all=False)
    a.start()
    b.start()
    try:
        payload = b"requested file, no prompt"
        source = _source(tmp_path, payload)
        a.runtime.set_clip_file_source(lambda _entry: ([str(source)], "ok"))

        assert b.runtime.request_entry_files(A_ID, "entry-1") == {"requested": True}

        assert _wait_until(
            lambda: any(
                row.get("status") == "completed" for row in b.runtime.transfers()["history"]
            )
        ), f"download never completed: {b.runtime.transfers()}"
        landed = tmp_path / "b-received" / "report.txt"
        assert landed.exists() and landed.read_bytes() == payload
        assert a.runtime.transfers()["active"] == []
    finally:
        a.stop()
        b.stop()


def test_a_served_download_does_not_leave_the_archive_behind(tmp_path):
    """A folder pulled by the peer is unlinked on the serving side, silently."""
    a, b = _pair(tmp_path)
    a.start()
    b.start()
    try:
        folder = tmp_path / "album"
        folder.mkdir()
        (folder / "one.txt").write_text("one", encoding="utf-8")
        (folder / "two.txt").write_text("two", encoding="utf-8")
        a.runtime.set_clip_file_source(lambda _entry: ([str(folder)], "ok"))

        assert b.runtime.request_entry_files(A_ID, "entry-1") == {"requested": True}
        assert _wait_until(
            lambda: bool(list((tmp_path / "b-received").glob("album*")))
        ), f"folder download never landed: {list((tmp_path / 'b-received').glob('*'))}"
        assert _wait_until(
            lambda: not a.runtime.transfers()["active"]
        ), "the served folder transfer stayed registered"
        # The archive this side built is reclaimed by the completion callback
        # even though no event is published for it.
        assert not list(tmp_path.glob("*.zip"))
    finally:
        a.stop()
        b.stop()
