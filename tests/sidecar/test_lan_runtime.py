"""Headless LAN integration with real pairing/codec/sync and fake clipboard I/O."""

import base64
import tempfile
import threading
import time
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from internal.application.errors import ApplicationError
from internal.application.events import EventJournal
from internal.clipboard.clipboard import ClipboardMonitor
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.config.config import Config, PeerInfo
from internal.infrastructure.runtime.lan import LanRuntime
from internal.protocol.codec import decode_message, encode_frame
from internal.security.encryption import EncryptionManager
from internal.security.pairing import (
    PAIRING_STATUS_NONE,
    PAIRING_TIMEOUT,
    PairingManager,
)
from internal.system import updater
from internal.transport.connection import TransportManager
from internal.transport.ids import peer_id_hash


class Monitor(ClipboardMonitor):
    def start(self, callback):
        self.callback = callback
        self.running = True

    def stop(self):
        self.running = False


class Clipboard:
    def __init__(self):
        self.content = ClipboardContent()
        self.writes = []
        self.success = True

    def read(self):
        return deepcopy(self.content)

    def write(self, content):
        if self.success:
            self.writes.append(deepcopy(content))
        return self.success


class History:
    def __init__(self):
        self.items = []

    def add(self, content, **kwargs):
        self.items.append(deepcopy(content))


class Discovery:
    def __init__(self):
        self.browsing = False
        self.advertising = False

    def set_callbacks(self, found, lost):
        self.found, self.lost = found, lost

    def start(self):
        self.running = True
        self.browsing = self.advertising = True

    def stop(self):
        self.running = False
        self.browsing = self.advertising = False

    @property
    def is_browsing(self):
        return self.browsing

    @property
    def is_advertising(self):
        return self.advertising

    def start_browsing(self):
        self.browsing = True

    def stop_browsing(self):
        self.browsing = False

    def start_advertising(self):
        self.advertising = True

    def stop_advertising(self):
        self.advertising = False

    def _wake_recovery(self):
        pass


class Transport:
    def __init__(self):
        self.connected = set()
        self.sent = []
        self.broadcasts = []
        self.dials = []
        self.dialed_without_pairing = []
        self.addresses = {}
        self.resolved = {}
        self.stop_result = True
        self.forgotten = []
        self.disconnected = []
        self.allowed = []
        self.reconnects = {}

    def set_on_peer_message(self, callback):
        self.message = callback

    def set_on_security_alert(self, callback):
        self.security_alert = callback

    def set_on_connect_rejected(self, callback):
        self.rejected = callback

    def set_on_wake(self, callback):
        self.wake = callback

    def set_encryption_manager(self, encryption):
        self.encryption = encryption

    def start_server(self):
        self.running = True

    def stop_server(self):
        self.running = False
        return self.stop_result

    def get_connected_peers(self):
        return list(self.connected)

    def get_resolved_hashes(self):
        return dict(self.resolved)

    def get_reconnect_states(self):
        return dict(self.reconnects)

    def get_saved_address(self, pid):
        return self.addresses.get(pid)

    def connect_to_peer(self, pid, name, address, port, no_auto_pairing=False):
        self.dials.append((pid, name, address, port))
        # Recorded, not just accepted: the chat dial's own case turns on it, and
        # a stub that swallowed it would let the pairing offer come back
        # unnoticed.
        self.dialed_without_pairing.append(no_auto_pairing)
        self.addresses[pid] = (name, address, port)

    def disconnect_peer(self, pid, reject=False):
        self.disconnected.append((pid, reject))
        self.connected.discard(pid)

    def forget_peer(self, pid):
        self.forgotten.append(pid)
        self.connected.discard(pid)
        self.addresses.pop(pid, None)

    def allow_peer(self, pid):
        self.allowed.append(pid)

    def send_to_peer(self, pid, data):
        if pid not in self.connected:
            return False
        self.sent.append((pid, decode_message(data)))
        return True

    def broadcast(self, data):
        self.broadcasts.append(decode_message(data))
        return bool(self.connected)


def identity(pid):
    pairing = PairingManager(pid, pid.upper())
    pairing.load_or_create_identity("", "")
    return pairing


def frame(kind, **payload):
    return decode_message(encode_frame({"msg_type": kind, **payload}))


def clip(text, msg_id="message", **kwargs):
    return SyncMessage(
        ClipboardContent({ContentType.TEXT: text.encode(), **kwargs}), msg_id, "forged-source"
    )


@pytest.fixture
def rig():
    config = Config(
        device_id="local",
        device_name="Local",
        encryption_enabled=False,
        retry_capture_enabled=False,
        sync_debounce=0.01,
    )
    pairing = identity("local")
    remote = identity("remote")
    pairing.add_peer("remote", "Remote", remote.get_identity().certificate_pem, paired=False)
    transport, discovery = Transport(), Discovery()
    clipboard, history, events, saves = Clipboard(), History(), EventJournal(), []
    opened = []
    runtime = LanRuntime(
        config,
        pairing,
        None,
        history,
        events,
        lambda: saves.append(deepcopy(config.peers)),
        monitor=Monitor(),
        reader=clipboard,
        writer=clipboard,
        transport=transport,
        discovery=discovery,
        open_url=opened.append,
    )
    # Tests drive refresh deterministically; lifecycle still uses the real loop.
    runtime.REFRESH_INTERVAL = 60
    # Pairing notices wait for a chat invite in production; tests drive the
    # refresh loop by hand, so one extra refresh stands in for that window.
    runtime.PAIRING_NOTICE_DELAY = 0
    runtime.start()
    yield runtime, pairing, transport, discovery, clipboard, history, events, saves, opened
    transport.stop_result = True
    assert runtime.stop()


def test_chat_devices_matches_desktop_contract(rig):
    runtime, *_ = rig
    assert runtime.chat_devices()["devices"] == runtime.devices()["items"]


def test_chat_invitation_uses_certificate_fingerprint_and_consent(rig):
    runtime, pairing, transport, *_ = rig
    transport.connected.add("remote")
    runtime._receive(frame(
        "chat_invite", session_id="abcdef0123456789",
        from_name="Remote", fingerprint_short="FORGED",
    ), "remote")
    session = runtime.chat_sessions()["sessions"][0]
    assert session["status"] == "invited"
    assert session["fingerprint_short"] == "FORGED"
    assert runtime.chat_action("accept", session["session_id"])
    assert runtime.chat_action("send", session["session_id"], "Hello")
    assert any(message.msg_type == "chat_text" for _, message in transport.sent)


def test_chat_file_progress_and_outcome_are_published(rig):
    """Chat attachments report through the journal, not through a read.

    Fired through the manager's own dispatch (`_fire` is what the transfer
    loops call), so this pins the wiring and the payload keys the phone and the
    desktop progress row read.
    """
    runtime, _, _, _, _, _, events, *_ = rig
    runtime.chat._fire("_on_file_progress", "session", "transfer", 0.25)
    runtime.chat._fire("_on_file_done", "session", "transfer", True, "/tmp/a.bin", "success")

    assert events_named(events, "chat.file.progress") == [
        {"session_id": "session", "transfer_id": "transfer", "fraction": 0.25}
    ]
    assert events_named(events, "chat.file.done") == [
        {
            "session_id": "session",
            "transfer_id": "transfer",
            "success": True,
            "saved_path": "/tmp/a.bin",
            "status": "success",
        }
    ]


def test_chat_invite_dials_an_idle_peer_before_inviting(rig):
    """An invite into a link that does not exist yet would go nowhere."""
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.addresses["remote"] = ("Remote", "127.0.0.1", 9999)

    def dial(pid, name, address, port, **kwargs):
        transport.dials.append((pid, name, address, port))
        transport.connected.add(pid)

    transport.connect_to_peer = dial
    session_id = runtime.chat_invite("remote", "Remote")

    assert transport.dials and session_id
    session = runtime.chat_sessions()["sessions"][0]
    assert session["status"] == "inviting"
    # The invite itself then rides the freshly dialed link.
    assert [message.msg_type for _, message in transport.sent] == ["chat_invite"]


def test_a_chat_dial_does_not_offer_to_pair_but_an_ordinary_one_does(rig):
    """Opening a conversation is not a request to pair.

    The chat list is every device on the network, including the ones nobody has
    paired with, and the ordinary dial offers the shared pairing code to exactly
    those — which is how two machines here come to trust each other.  Clicking a
    device in the chat list used to take that path, so a code appeared on both
    screens for a consent neither side had given; chat has its own invite and
    fingerprint consent instead.
    """
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.addresses["remote"] = ("Remote", "127.0.0.1", 9999)

    runtime.chat_invite("remote", "Remote")
    assert transport.dialed_without_pairing == [True]

    # ...and the device list's own connect still asks, or a first pairing could
    # never start from the one place a user goes looking for it.
    transport.connected.discard("remote")
    runtime.connect_device("remote")
    assert transport.dialed_without_pairing == [True, False]


def test_chat_invite_an_internet_only_peer_needs_no_lan_address(rig):
    """A pairing code alone opens the conversation; the relay carries it."""
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    runtime.config.internet_sync_enabled = True
    runtime.config.netpair_secrets["remote"] = "netpair-secret"
    published = []
    runtime.relay = type(
        "Relay",
        (),
        {"publish": staticmethod(lambda frame, topic, key, qos=0: published.append(frame) or True)},
    )()

    try:
        session_id = runtime.chat_invite("remote", "Remote")
    finally:
        runtime.relay = None
    assert session_id
    assert not transport.dials  # nothing to dial, and nothing was dialed
    assert [decode_message(frame).msg_type for frame in published] == ["chat_invite"]


def test_chat_invite_an_unreachable_peer_is_reported(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.addresses["remote"] = ("Remote", "127.0.0.1", 9999)
    runtime.CHAT_CONNECT_TIMEOUT = 0.2  # the dial never completes in this rig

    assert runtime.chat_invite("remote", "Remote") is None
    assert events_named(events, "chat.connect_timeout") == [
        {"peer_id": "remote", "name": "Remote"}
    ]


def test_discovery_hash_resolution_and_only_paired_auto_connect(rig):
    runtime, pairing, transport, discovery, *_ = rig
    hashed = peer_id_hash("remote")
    discovery.found(hashed, "Remote-ad", "127.0.0.1", 9999)
    assert not transport.dials
    assert len(runtime.devices()["items"]) == 1
    assert runtime.devices()["items"][0]["id"] == "remote"
    assert runtime.devices()["items"][0]["connection_state"] == "discovered"
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    discovery.found(hashed, "Remote-ad", "127.0.0.1", 9999)
    assert transport.dials[-1][0] == "remote"
    assert runtime.devices()["items"][0]["connection_state"] == "connecting"
    discovery.found(hashed, "Remote-ad", "127.0.0.1", 9999)
    assert len(transport.dials) == 1
    discovery.found(hashed, "Remote-ad", "127.0.0.2", 9999)
    assert len(transport.dials) == 2
    transport.connected.add("remote")
    discovery.lost(hashed)
    assert "remote" not in transport.connected
    assert runtime.config.peers["remote"].last_ip == "127.0.0.2"
    assert runtime.devices()["items"][0]["connection_state"] == "offline"


def test_new_discovered_device_uses_ui_state_contract(rig):
    runtime, _, transport, discovery, *_ = rig
    discovery.found("new-hash", "New device", "127.0.0.1", 9001)
    row = next(item for item in runtime.devices()["items"] if item["id"] == "new-hash")
    assert row["connection_state"] == "discovered"
    assert not row["paired"]
    assert not transport.dials
    assert runtime.start_pairing("new-hash") == {"accepted": True}
    row = next(item for item in runtime.devices()["items"] if item["id"] == "new-hash")
    assert row["connection_state"] == "connecting"


def events_named(events, name):
    return [event["data"] for event in events.since(0)[0] if event["name"] == name]


def test_a_changed_certificate_prompts_and_can_be_trusted(rig):
    """A peer presenting a new certificate is refused, then re-pinned.

    The transport refuses the handshake before this runs, so what the runtime
    owes is the prompt, the certificate the answer has to pin, and the dial that
    makes the re-pin take effect — the transport resumes reconnecting on its own
    once the pinned fingerprint changes, but not before its next health tick.
    """
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    pinned = pairing.get_peer_certificate("remote")
    fresh = identity("remote").get_identity().certificate_pem
    assert fresh and fresh != pinned
    transport.addresses["remote"] = ("Remote", "127.0.0.1", 9999)

    transport.security_alert("Remote", "remote", "expected-fp", "received-fp", fresh)

    assert events_named(events, "device.security_alert") == [
        {
            "device_id": "remote",
            "name": "Remote",
            "code": "CERTIFICATE_CHANGED",
            "can_trust": True,
        }
    ]
    # Announcing it pins nothing: the decision is the user's.
    assert pairing.get_peer_certificate("remote") == pinned

    assert runtime.retrust_device("remote") == {"trusted": True}
    assert pairing.get_peer_certificate("remote") == fresh
    assert pairing.is_peer_paired("remote") is True
    assert transport.dials and transport.dials[-1][0] == "remote"
    # Answered: a second answer has nothing left to consume.
    with pytest.raises(ApplicationError) as gone:
        runtime.retrust_device("remote")
    assert gone.value.code == "NOT_FOUND"


def test_a_certificate_alert_without_a_certificate_cannot_be_trusted(rig):
    """The dial side knows only the pin it refused, so it presents no cert.

    Trusting that would leave the peer paired against a certificate nobody has
    seen, so the runtime refuses and the prompt stays answerable: the accept side
    alerts again with the real certificate once the device connects.
    """
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    pinned = pairing.get_peer_certificate("remote")

    transport.security_alert("Remote", "remote", "expected-fp", "", "")

    assert events_named(events, "device.security_alert") == [
        {
            "device_id": "remote",
            "name": "Remote",
            "code": "CERTIFICATE_CHANGED",
            "can_trust": False,
        }
    ]
    with pytest.raises(ApplicationError) as refused:
        runtime.retrust_device("remote")
    assert refused.value.code == "VALIDATION_ERROR"
    assert pairing.get_peer_certificate("remote") == pinned
    assert pairing.is_peer_paired("remote") is True


def test_a_reconnecting_peer_is_prompted_once_per_window(rig):
    """A peer that keeps retrying with a changed certificate prompts once.

    Legacy throttled the same way; without it a reconnect loop would stack
    prompts, and the user's answer would be about a certificate that was no
    longer the one on screen.
    """
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    fresh = identity("remote").get_identity().certificate_pem

    transport.security_alert("Remote", "remote", "fp", "fp", fresh)
    transport.security_alert("Remote", "remote", "fp", "fp", fresh)

    assert len(events_named(events, "device.security_alert")) == 1
    # Still exactly the first alert's answer to give.
    assert runtime.retrust_device("remote") == {"trusted": True}


def test_unpairing_answers_a_pending_certificate_prompt(rig):
    """Both answers to the prompt resolve it; the other one is an unpair."""
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    fresh = identity("remote").get_identity().certificate_pem
    transport.security_alert("Remote", "remote", "fp", "fp", fresh)

    runtime.unpair_device("remote")

    assert pairing.is_peer_paired("remote") is False
    with pytest.raises(ApplicationError) as gone:
        runtime.retrust_device("remote")
    assert gone.value.code == "NOT_FOUND"


def test_a_forgotten_device_leaves_no_certificate_prompt_behind(rig):
    """The device is gone, so its prompt and its throttle go with it."""
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    fresh = identity("remote").get_identity().certificate_pem
    transport.security_alert("Remote", "remote", "fp", "fp", fresh)

    runtime.forget_device("remote")

    with pytest.raises(ApplicationError) as gone:
        runtime.retrust_device("remote")
    assert gone.value.code == "NOT_FOUND"
    # The throttle went too: a device re-added under the same id prompts again.
    runtime.pairing.add_peer("remote", "Remote", fresh, paired=True)
    transport.security_alert("Remote", "remote", "fp", "fp", fresh)
    assert len(events_named(events, "device.security_alert")) == 2


def test_send_url_reaches_only_a_connected_paired_peer(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    with pytest.raises(ApplicationError) as unpaired:
        runtime.send_url("remote", "https://example.com")
    assert unpaired.value.code == "NOT_PAIRED"
    with pytest.raises(ApplicationError) as unknown:
        runtime.send_url("ghost", "https://example.com")
    assert unknown.value.code == "NOT_FOUND"
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    with pytest.raises(ApplicationError) as offline:
        runtime.send_url("remote", "https://example.com")
    assert offline.value.code == "SEND_FAILED"
    assert not transport.sent
    transport.connected.add("remote")
    assert runtime.send_url("remote", "https://example.com/page") == {
        "sent": True,
        "device_id": "remote",
    }
    pid, message = transport.sent[-1]
    assert pid == "remote"
    assert message.msg_type == "nav_url"
    assert message._raw_payload["url"] == "https://example.com/page"
    assert message.source_device == "local"
    assert events_named(events, "url.sent") == [
        {"device_id": "remote", "url": "https://example.com/page"}
    ]


def test_send_url_rejects_anything_but_http_and_https(rig):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    for url in ("", "not a url", "https://", "file:///etc/passwd", "javascript:alert(1)"):
        with pytest.raises(ApplicationError) as error:
            runtime.send_url("remote", url)
        assert error.value.code == "INVALID_ARGUMENT"
    assert not transport.sent


def test_received_nav_url_opens_only_paired_http_urls(rig):
    runtime, pairing, _, _, _, _, events, _, opened = rig
    runtime._receive(frame("nav_url", url="https://example.com/page"), "remote")
    assert opened == []
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    runtime._receive(frame("nav_url", url="https://example.com/page"), "remote")
    assert opened == ["https://example.com/page"]
    runtime._receive(frame("nav_url", url="file:///etc/passwd"), "remote")
    runtime._receive(frame("nav_url", url="javascript:alert(1)"), "remote")
    assert opened == ["https://example.com/page"]
    assert events_named(events, "url.received") == [
        {"device_id": "remote", "url": "https://example.com/page"}
    ]


def test_push_text_writes_clipboard_history_and_broadcasts_once(rig):
    runtime, _, transport, _, clipboard, history, events, *_ = rig
    assert runtime.push_text("  hello peers  ") == {"ok": True, "len": 11, "sent": True}
    assert [item.types[ContentType.TEXT] for item in clipboard.writes] == [b"hello peers"]
    assert [item.types[ContentType.TEXT] for item in history.items] == [b"hello peers"]
    assert len(transport.broadcasts) == 1
    message = transport.broadcasts[0]
    assert message.content.types[ContentType.TEXT] == b"hello peers"
    assert message.source_device == "local"
    assert events_named(events, "history.changed")
    # Our own write must not be captured and broadcast a second time.
    assert runtime.sync._monitor.suppress_until > time.time()


def test_push_text_without_sync_still_writes_the_clipboard(rig):
    runtime, _, transport, _, clipboard, history, *_ = rig
    runtime.set_sync_enabled(False)
    assert runtime.push_text("offline") == {"ok": True, "len": 7, "sent": False}
    assert [item.types[ContentType.TEXT] for item in clipboard.writes] == [b"offline"]
    assert [item.types[ContentType.TEXT] for item in history.items] == [b"offline"]
    assert transport.broadcasts == []


def test_push_text_reports_a_refused_clipboard_write(rig):
    runtime, _, transport, _, clipboard, history, *_ = rig
    clipboard.success = False
    with pytest.raises(ApplicationError) as error:
        runtime.push_text("blocked")
    assert error.value.code == "CLIPBOARD_WRITE_FAILED"
    assert error.value.retryable
    assert history.items == []
    assert transport.broadcasts == []


@pytest.mark.parametrize(
    "text",
    ["", "   ", None, 5, ["x"], pytest.param("x" * 100001, id="too-long")],
)
def test_push_text_rejects_anything_but_bounded_text(rig, text):
    runtime, _, transport, _, clipboard, *_ = rig
    with pytest.raises(ApplicationError) as error:
        runtime.push_text(text)
    assert error.value.code == "INVALID_ARGUMENT"
    assert clipboard.writes == []
    assert transport.broadcasts == []


def test_discovery_toggles_publish_the_resulting_state(rig):
    runtime, _, _, discovery, _, _, events, *_ = rig
    assert runtime.discovery_state() == {"enabled": True, "visible": True}
    assert runtime.set_discovery_enabled(False) == {"enabled": False, "visible": True}
    assert runtime.set_discovery_visible(False) == {"enabled": False, "visible": False}
    assert runtime.discovery_state() == {"enabled": False, "visible": False}
    assert runtime.set_discovery_enabled(True) == {"enabled": True, "visible": False}
    assert runtime.set_discovery_visible(True) == {"enabled": True, "visible": True}
    assert events_named(events, "discovery.changed") == [
        {"enabled": False, "visible": True},
        {"enabled": False, "visible": False},
        {"enabled": True, "visible": False},
        {"enabled": True, "visible": True},
    ]
    assert discovery.browsing and discovery.advertising


def test_discovery_toggle_reports_when_the_state_did_not_change():
    class StubbornDiscovery(Discovery):
        def start_browsing(self):
            pass

    runtime = LanRuntime(
        Config(device_id="local", encryption_enabled=False),
        identity("local"),
        None,
        History(),
        EventJournal(),
        lambda: None,
        monitor=Monitor(),
        reader=Clipboard(),
        writer=Clipboard(),
        transport=Transport(),
        discovery=StubbornDiscovery(),
    )
    try:
        runtime.start()
        runtime.set_discovery_enabled(False)
        with pytest.raises(ApplicationError) as error:
            runtime.set_discovery_enabled(True)
        assert error.value.code == "DISCOVERY_TOGGLE_FAILED"
        assert error.value.retryable
        assert runtime.discovery_state() == {"enabled": False, "visible": True}
    finally:
        assert runtime.stop()


def test_two_sided_confirmation_sas_and_persisted_trust(rig):
    runtime, pairing, transport, _, _, _, _, saves, _ = rig
    transport.connected.add("remote")
    assert runtime.start_pairing(peer_id_hash("remote")) == {"accepted": True}
    row = runtime.devices()["items"][0]
    assert row["connection_state"] == "online"
    assert row["pairing_code"] and row["sas"]
    result = runtime.confirm_pairing("remote", row["pairing_code"])
    assert result == {"paired": False, "status": "confirmed_waiting"}
    assert not runtime.config.peers["remote"].paired
    assert transport.sent[-1][1].msg_type == "pairing_confirm"
    transport.message(frame("pairing_confirm"), "remote")
    assert pairing.is_peer_paired("remote")
    assert runtime.devices()["items"][0]["paired"]
    assert saves[-1]["remote"].paired
    assert runtime.devices()["items"][0]["pairing_code"] == ""


def test_peer_first_and_bad_code_do_not_bypass_local_confirmation(rig):
    runtime, pairing, transport, *_ = rig
    code = pairing.generate_shared_pairing_code("remote")
    transport.message(frame("pairing_confirm"), "remote")
    assert not pairing.is_peer_paired("remote")
    assert runtime.confirm_pairing("remote", "wrong") == {
        "paired": False,
        "status": "peer_confirmed",
    }
    assert runtime.confirm_pairing("remote", code)["paired"]


def test_unknown_confirm_cannot_create_trust(rig):
    _, pairing, transport, *_ = rig
    transport.message(frame("pairing_confirm"), "unknown")
    transport.message(frame("pairing_confirm"), "remote")
    assert not pairing.is_peer_paired("remote")
    # A refused confirm leaves no pairing in flight, and records no status to
    # say otherwise: the peer reads back exactly what a merely-discovered
    # device reads back, which is what keeps the devices page from drawing a
    # confirm row for a device nobody has asked to pair with.
    assert pairing.get_pairing_status("remote") == PAIRING_STATUS_NONE


def test_devices_report_a_pairing_only_while_one_is_in_flight(rig):
    """Nothing but a real handshake puts a pairing status on a device row.

    ``get_pairing_status`` answered ``"pending"`` for any peer it had no record
    of, and ``devices()`` copies that answer straight onto the row, so every
    unpaired device on the LAN was described as mid-pairing.  The shell drew a
    confirm/reject pair on those rows and the confirm was dead: no request
    existed, so the row carried no code for it to confirm with.  A device that
    was only ever discovered, and one merely connected, must both report the
    empty status the removed-device rows already use.
    """
    runtime, _, transport, discovery, *_ = rig
    discovery.found(peer_id_hash("remote"), "Remote-ad", "127.0.0.1", 9999)
    row = runtime.devices()["items"][0]
    assert row["id"] == "remote"
    assert not row["paired"]
    assert row["pairing_status"] == PAIRING_STATUS_NONE
    assert row["pairing_code"] == ""
    # Transport state and pairing state are independent: a live connection to a
    # device nobody has asked to pair with says the same thing.
    transport.connected.add("remote")
    runtime._refresh()
    row = runtime.devices()["items"][0]
    assert row["connection_state"] == "online"
    assert row["pairing_status"] == PAIRING_STATUS_NONE
    # A local request is the one thing that does put a pairing in flight, and it
    # is also what supplies the code the confirm needs.
    runtime.start_pairing(peer_id_hash("remote"))
    runtime._refresh()
    row = runtime.devices()["items"][0]
    assert row["pairing_status"] == "pending"
    assert row["pairing_code"]


def test_expiry_before_remote_confirm_rolls_back_and_persists(rig):
    runtime, pairing, transport, *_ = rig
    code = pairing.generate_shared_pairing_code("remote")
    runtime.confirm_pairing("remote", code)
    with pairing._lock:
        pairing._pending_pairings["remote"] = (code, time.time() - PAIRING_TIMEOUT - 1)
        # Exercise rollback of legacy persisted half-trust as well.
        pairing._peers["remote"].paired = True
    runtime.config.peers["remote"].paired = True
    transport.message(frame("pairing_confirm"), "remote")
    assert not pairing.is_peer_paired("remote")
    assert not runtime.config.peers["remote"].paired
    assert runtime.devices()["items"][0]["pairing_status"] == "cancelled"
    runtime._tick()
    assert "remote" not in runtime._deferred


@pytest.mark.parametrize(
    "method,kind",
    [
        ("reject_pairing", "pairing_reject"),
        ("unpair_device", "pairing_unpair"),
    ],
)
def test_local_reject_unpair_notify_before_forget(rig, method, kind):
    runtime, pairing, transport, *_ = rig
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    assert getattr(runtime, method)("remote") == {"accepted": True}
    assert transport.sent[-1][1].msg_type == kind
    assert transport.forgotten[-1] == "remote"
    assert not runtime.config.peers["remote"].paired


@pytest.mark.parametrize("kind", ["pairing_reject", "pairing_unpair"])
def test_remote_reject_unpair_persist_revocation(rig, kind):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.message(frame(kind), "remote")
    assert not pairing.is_peer_paired("remote")
    assert not runtime.config.peers["remote"].paired
    assert "remote" in transport.forgotten


def test_forget_archives_and_restore_returns_unpaired(rig):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    runtime._refresh()

    assert runtime.forget_device("remote") == {"forgotten": True}
    assert "remote" not in runtime.config.peers
    assert runtime.config.removed_peers["remote"].removed_at > 0
    assert not pairing.is_peer_paired("remote")
    assert transport.sent[-1][1].msg_type == "pairing_unpair"
    assert transport.forgotten[-1] == "remote"
    assert runtime.devices()["items"] == [
        {
            "id": "remote",
            "name": "Remote",
            "note": "",
            "paired": False,
            "connection_state": "offline",
            "pairing_status": "",
            "pairing_code": "",
            "sas": "",
            "archived": True,
            "removed_at": runtime.config.removed_peers["remote"].removed_at,
        }
    ]

    assert runtime.restore_device("remote") == {"restored": True}
    assert "remote" not in runtime.config.removed_peers
    assert runtime.config.peers["remote"].paired is False
    assert not pairing.is_peer_paired("remote")
    assert transport.allowed[-1] == "remote"
    assert [item["archived"] for item in runtime.devices()["items"]] == [False]


def test_forget_keeps_discovered_address_and_never_clobbers_archive(rig):
    runtime, pairing, transport, discovery, *_ = rig
    discovery.found(peer_id_hash("remote"), "Remote-ad", "127.0.0.1", 9999)

    assert runtime.forget_device("remote") == {"forgotten": True}
    archived = runtime.config.removed_peers["remote"]
    assert (archived.device_name, archived.last_ip, archived.last_port) == (
        "Remote",
        "127.0.0.1",
        9999,
    )
    assert runtime.devices()["items"][0]["archived"] is True
    # The archive is the recovery path: a second forget must not clobber it.
    runtime.config.removed_peers["remote"].notes = "keep me"
    runtime.forget_device("remote")
    assert runtime.config.removed_peers["remote"].notes == "keep me"


def test_restore_and_purge_reject_unknown_archives(rig):
    runtime, *_ = rig
    for method in ("restore_device", "purge_device"):
        with pytest.raises(ApplicationError) as caught:
            getattr(runtime, method)("ghost")
        assert caught.value.code == "NOT_FOUND"


def test_purge_drops_archive_and_persists(rig):
    runtime, pairing, transport, _, _, _, _, saves, _ = rig
    transport.connected.add("remote")
    runtime.forget_device("remote")
    assert saves[-1] is not None

    assert runtime.purge_device("remote") == {"purged": True}
    assert "remote" not in runtime.config.removed_peers
    assert runtime.devices()["items"] == []


def test_manual_connect_and_disconnect_validate_known_devices(rig):
    runtime, pairing, transport, *_ = rig
    for method in ("connect_device", "disconnect_device"):
        with pytest.raises(ApplicationError) as caught:
            getattr(runtime, method)("ghost")
        assert caught.value.code == "NOT_FOUND"

    assert runtime.connect_device("remote") == {"accepted": False}
    transport.addresses["remote"] = ("Remote", "127.0.0.1", 9000)
    assert runtime.connect_device("remote") == {"accepted": True}
    assert transport.dials[-1] == ("remote", "Remote", "127.0.0.1", 9000)

    transport.connected.add("remote")
    assert runtime.disconnect_device("remote") == {"disconnected": True}
    assert transport.disconnected[-1] == ("remote", True)
    assert "remote" not in transport.connected


def test_device_mutation_save_failure_is_sanitized(rig):
    runtime, pairing, transport, *_ = rig
    transport.connected.add("remote")
    saved = runtime._save_config

    def fail():
        raise OSError("secret-directory-and-password")

    runtime._save_config = fail
    try:
        with pytest.raises(ApplicationError) as caught:
            runtime.forget_device("remote")
        assert caught.value.code == "SAVE_FAILED"
        assert "secret-directory" not in str(caught.value)
    finally:
        runtime._save_config = saved


def test_confirmation_waits_for_real_connection(rig):
    runtime, pairing, transport, discovery, *_ = rig
    discovery.found(peer_id_hash("remote"), "Remote", "127.0.0.1", 9000)
    code = pairing.generate_shared_pairing_code("remote")
    assert not runtime.confirm_pairing("remote", code)["paired"]
    assert not transport.sent
    assert transport.dials
    transport.connected.add("remote")
    runtime._tick()
    assert transport.sent[-1][1].msg_type == "pairing_confirm"
    assert not runtime._deferred


def test_original_history_outgoing_redaction_and_rich_policy(rig):
    runtime, _, transport, _, clipboard, history, *_ = rig
    runtime.config.plain_text_only = True
    clipboard.content = ClipboardContent(
        {
            ContentType.TEXT: b"password=secret-value",
            ContentType.HTML: b"<b>password=secret-value</b>",
        }
    )
    runtime.sync._do_read_and_send()
    assert history.items[-1].types == clipboard.content.types
    outgoing = transport.broadcasts[-1]
    assert outgoing.content.types == {ContentType.TEXT: b"[FILTERED]"}
    assert outgoing.source_device == "local"


def test_app_filter_drops_before_history_and_network(rig):
    runtime, _, transport, _, clipboard, history, *_ = rig
    runtime.config.app_filter_enabled = True
    runtime.config.app_filter_list = ["secret*"]
    runtime.sync._monitor.last_source_app = {"process": "secret.exe"}
    clipboard.content = ClipboardContent({ContentType.TEXT: b"private"})
    runtime.sync._do_read_and_send()
    assert not history.items and not transport.broadcasts


def test_transfer_snapshot_normalizes_progress_and_pause(rig, monkeypatch):
    runtime = rig[0]
    monkeypatch.setattr(runtime.file_transfer, "get_transfers", lambda: [
        {"transfer_id": "half", "state": "receiving", "progress": 0.5, "paused": True},
        {"transfer_id": "request", "state": "pending", "progress": 0},
        {"transfer_id": "overflow", "state": "sending", "progress": 1.2},
    ])
    rows = runtime.transfers()["active"]
    assert rows[0]["progress"] == 50
    assert rows[0]["status"] == "paused"
    assert rows[1]["status"] == "pending"
    assert rows[1]["direction"] == "down"
    assert rows[2]["progress"] == 100


def test_transfer_snapshot_carries_the_live_rate_and_a_localized_eta(rig, monkeypatch):
    runtime = rig[0]
    monkeypatch.setattr(runtime.file_transfer, "get_transfers", lambda: [
        {
            "transfer_id": "big", "file_name": "film.mkv", "file_size": 1 << 30,
            "state": "sending", "progress": 0.5,
            "speed_bytes_per_sec": 1 << 20, "eta_seconds": 3725,
        },
        {"transfer_id": "stalled", "state": "sending", "speed_bytes_per_sec": 0, "eta_seconds": 0},
    ])
    monkeypatch.setattr(runtime.file_transfer, "get_history", lambda: [
        {"transfer_id": "old", "file_name": "notes.txt", "file_size": 512, "timestamp": 1},
    ])
    page = runtime.transfers()
    row = page["active"][0]
    assert row["speed"] == 1 << 20
    # The estimate is the catalog's, in the language the app is running in —
    # a two-hour remainder keeps its hours unit instead of reading "120m 0s".
    assert row["eta"] == "1h 2m"
    # A stalled transfer has no rate and nothing to estimate: both read empty
    # rather than as "0 B/s" and "0s".
    assert page["active"][1]["speed"] == 0
    assert page["active"][1]["eta"] == ""
    # History rows have neither, and an unnamed row is left blank for the shell
    # to label in its own language.
    assert page["history"][0]["eta"] == ""
    assert page["history"][0]["filename"] == "notes.txt"


def test_an_unnamed_transfer_row_has_no_filename_rather_than_an_english_one(rig, monkeypatch):
    runtime = rig[0]
    monkeypatch.setattr(runtime.file_transfer, "get_transfers", lambda: [
        {"transfer_id": "x", "state": "sending"},
    ])
    assert runtime.transfers()["active"][0]["filename"] == ""


def test_cancel_all_cancels_every_active_row_through_the_single_row_path(rig, monkeypatch):
    runtime = rig[0]
    seen = []
    monkeypatch.setattr(runtime.file_transfer, "get_transfers", lambda: [
        {"transfer_id": "a", "direction": "down"},
        {"transfer_id": "b", "direction": "up"},
        {"transfer_id": ""},
    ])

    def cancel(transfer_id, send_fn):
        seen.append(transfer_id)
        # A row that finished between the read and the cancel reports False and
        # must not be counted — "cancelled 2" for one real cancel would be a lie.
        return transfer_id == "a"

    monkeypatch.setattr(runtime.file_transfer, "cancel_transfer", cancel)
    assert runtime.cancel_all_transfers() == 1
    # A row with no id is skipped rather than raising, and every row is offered
    # to the same per-row cancel.
    assert seen == ["a", "b"]


def test_cancel_all_with_nothing_running_does_nothing(rig, monkeypatch):
    runtime = rig[0]
    monkeypatch.setattr(runtime.file_transfer, "get_transfers", lambda: [])
    monkeypatch.setattr(
        runtime.file_transfer, "cancel_transfer",
        lambda *_: pytest.fail("nothing was active, so nothing may be cancelled"),
    )
    assert runtime.cancel_all_transfers() == 0


def _seed_transfer(runtime, monkeypatch, **entry):
    record = {"transfer_id": "t1", "direction": "down", "saved_path": "/tmp/received.zip"}
    record.update(entry)
    monkeypatch.setattr(runtime.file_transfer, "get_history", lambda: [record])
    return record


def test_transfer_open_launches_the_received_file(rig, monkeypatch):
    from internal.system import file_manager

    runtime = rig[0]
    _seed_transfer(runtime, monkeypatch)
    calls = []
    monkeypatch.setattr(
        file_manager, "open_file", lambda path: (calls.append(path), (True, path))[1]
    )
    assert runtime.transfer_action("open", "t1") == {"ok": True, "path": "/tmp/received.zip"}
    assert calls == ["/tmp/received.zip"]


def test_transfer_reveal_opens_the_containing_folder(rig, monkeypatch):
    from internal.system import file_manager

    runtime = rig[0]
    _seed_transfer(runtime, monkeypatch)
    calls = []
    monkeypatch.setattr(
        file_manager, "reveal_folder", lambda path: (calls.append(path), (True, "/tmp"))[1]
    )
    assert runtime.transfer_action("reveal", "t1") == {"ok": True, "path": "/tmp/received.zip"}
    assert calls == ["/tmp/received.zip"]


def test_transfer_open_rejects_an_outgoing_transfer(rig, monkeypatch):
    runtime = rig[0]
    _seed_transfer(runtime, monkeypatch, direction="up", saved_path="")
    with pytest.raises(ApplicationError) as error:
        runtime.transfer_action("open", "t1")
    assert error.value.code == "INVALID_ARGUMENT"


def test_transfer_open_rejects_an_unknown_transfer(rig, monkeypatch):
    runtime = rig[0]
    monkeypatch.setattr(runtime.file_transfer, "get_history", list)
    with pytest.raises(ApplicationError) as error:
        runtime.transfer_action("reveal", "gone")
    assert error.value.code == "NOT_FOUND"


def test_transfer_open_reports_a_file_that_is_gone(rig, monkeypatch):
    from internal.system import file_manager

    runtime = rig[0]
    _seed_transfer(runtime, monkeypatch)
    monkeypatch.setattr(
        file_manager, "open_file", lambda path: (False, file_manager.FILE_NOT_FOUND)
    )
    with pytest.raises(ApplicationError) as error:
        runtime.transfer_action("open", "t1")
    assert error.value.code == "NOT_FOUND"


def test_disabled_filter_keeps_outgoing_content(rig):
    runtime, _, transport, _, clipboard, *_ = rig
    runtime.config.filter_enabled_categories = []
    clipboard.content = ClipboardContent({ContentType.TEXT: b"password=secret"})
    runtime.sync._do_read_and_send()
    assert transport.broadcasts[-1].content.types == clipboard.content.types


def test_live_email_filter_changes_outgoing_payload_without_redacting_local_history(rig):
    runtime, _, transport, _, clipboard, history, *_ = rig
    runtime.config.filter_enabled_categories = ["email"]
    runtime.apply_settings({"filter_enabled_categories": ["email"]})
    clipboard.content = ClipboardContent({ContentType.TEXT: b"first@example.com"})
    runtime.sync._do_read_and_send()
    assert transport.broadcasts[-1].content.types == {ContentType.TEXT: b"[FILTERED]"}
    assert history.items[-1].types == {ContentType.TEXT: b"first@example.com"}
    runtime.config.filter_enabled_categories = []
    runtime.apply_settings({"filter_enabled_categories": []})
    clipboard.content = ClipboardContent({ContentType.TEXT: b"second@example.com"})
    runtime.sync._do_read_and_send()
    assert transport.broadcasts[-1].content.types == {ContentType.TEXT: b"second@example.com"}


def test_apply_encryption_rewires_transport_and_relay_channels(rig):
    """A password/toggle change must reach the live transport and relay.

    Without this, the UI showed encryption on while frames still went out under
    the startup key state.
    """
    runtime, pairing, transport, *_ = rig

    class Relay:
        def __init__(self):
            self.refreshes = 0

        def refresh_channels(self):
            self.refreshes += 1

    relay = Relay()
    runtime.relay = relay
    try:
        manager = EncryptionManager(
            pairing.get_identity().fingerprint, password="Str0ng-Passw0rd!"
        )
        runtime.apply_encryption(manager)
        assert transport.encryption is manager
        assert relay.refreshes == 1
        runtime.apply_encryption(None)
        assert transport.encryption is None
        assert relay.refreshes == 2
    finally:
        runtime.relay = None


def test_apply_settings_clears_a_pending_timed_pause(rig):
    """An explicit sync toggle ends a timed pause (legacy ``_clear_pause_state``).

    The deadline is persisted, so leaving it behind re-arms the pause on the
    next launch even though the user turned sync back on.
    """
    runtime, *_ = rig
    runtime.config.timed_pause_until = time.time() + 600
    runtime.config.sync_enabled = False
    runtime.sync.set_enabled(False)

    runtime.config.sync_enabled = True
    runtime.apply_settings({"sync_enabled": True})
    assert runtime.config.timed_pause_until == 0.0
    assert runtime.sync._enabled is True

    # A change that does not touch sync leaves the deadline alone.
    runtime.config.timed_pause_until = time.time() + 600
    runtime.apply_settings({"device_name": "Renamed"})
    assert runtime.config.timed_pause_until > 0.0


def test_receive_ack_only_after_success_and_source_bound_to_connection(rig):
    runtime, pairing, transport, _, clipboard, history, *_ = rig
    transport.connected.add("remote")
    transport.message(clip("untrusted"), "remote")
    assert not clipboard.writes and not transport.sent
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    clipboard.success = False
    transport.message(clip("valid"), "remote")
    assert not history.items and not transport.sent
    clipboard.success = True
    transport.message(clip("valid"), "remote")
    assert clipboard.writes[-1].source_device == "remote"
    assert transport.sent[-1][1].msg_type == "relay_ack"
    assert transport.sent[-1][1]._raw_payload["msg_id"] == "message"
    transport.message(clip("valid"), "remote")
    assert len(transport.sent) == 1
    runtime.set_sync_enabled(False)
    transport.message(clip("paused", "second"), "remote")
    assert len(transport.sent) == 1


def test_receive_rich_policy_and_no_ack_for_unsupported_protocols(rig):
    runtime, pairing, transport, _, clipboard, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    runtime.config.plain_text_only = True
    message = SyncMessage(ClipboardContent({ContentType.HTML: b"<b>Hello</b>"}), "one")
    transport.message(message, "remote")
    assert clipboard.writes[-1].types == {ContentType.TEXT: b"Hello"}
    # device_ping is a supported probe and is answered with a pong; every other
    # unhandled protocol must still produce neither a clipboard write nor a reply.
    # relay_enroll left this list when it was ported: it is paired-only and
    # refused rather than ignored, and its own cases live in
    # tests/sidecar/test_relay_delivery.py, where a paired peer is what sends it.
    for kind in (
        "chat_text",
        "file_request",
        "file_chunk",
        "relay_ack",
        "aiconfig_inv",
        "nav_url",
    ):
        message = clip("must not write", kind)
        message.msg_type = kind
        transport.message(message, "remote")
    assert len(clipboard.writes) == 1
    assert len(transport.sent) == 1


def test_stop_false_retains_runtime_and_late_callbacks_are_noops(rig):
    runtime, _, transport, discovery, clipboard, history, *_ = rig
    transport.stop_result = False
    assert runtime.stop() is False
    assert runtime.sync_state == "stopping"
    discovery.found("late", "Late", "127.0.0.1", 1)
    clipboard.content = ClipboardContent({ContentType.TEXT: b"late"})
    runtime.sync._do_read_and_send()
    assert not history.items
    assert all(row["id"] != "late" for row in runtime.devices()["items"])
    transport.stop_result = True
    assert runtime.stop()
    assert runtime.stop()
    with pytest.raises(ApplicationError):
        runtime.start()


def test_stop_retains_inflight_capture_until_reader_exits(rig):
    runtime, _, _, _, clipboard, history, *_ = rig
    entered, release = threading.Event(), threading.Event()
    runtime.STOP_TIMEOUT = 0.05

    def read():
        entered.set()
        assert release.wait(3)
        return ClipboardContent({ContentType.TEXT: b"late secret"})

    clipboard.read = read
    worker = threading.Thread(target=runtime.sync._do_read_and_send)
    worker.start()
    try:
        assert entered.wait(2)
        assert runtime.stop() is False
    finally:
        release.set()
        worker.join(2)
        runtime.STOP_TIMEOUT = 2
    assert runtime.stop()
    assert not history.items


def test_snapshot_publication_has_no_runtime_journal_lock_inversion(rig):
    runtime, _, _, discovery, _, _, events, *_ = rig
    entered, proceed = threading.Event(), threading.Event()
    original = events.publish

    def publish(*args, **kwargs):
        entered.set()
        assert proceed.wait(3)
        return original(*args, **kwargs)

    events.publish = publish
    worker = threading.Thread(target=discovery.found, args=("new-hash", "New", "127.0.0.1", 9001))
    worker.start()
    try:
        assert entered.wait(2)
        snapshot = events.snapshot(runtime.devices)
        assert any(row["id"] == "new-hash" for row in snapshot["items"])
    finally:
        proceed.set()
        worker.join(2)
    assert not worker.is_alive()


def test_background_exception_event_never_contains_exception_text(rig):
    runtime, _, transport, discovery, _, _, events, *_ = rig

    def fail(*args):
        raise RuntimeError("password=do-not-publish")

    transport.get_connected_peers = fail
    discovery.found("new", "New", "127.0.0.1", 1)
    emitted, _ = events.since(0)
    assert any(item["name"] == "runtime.error" for item in emitted)
    assert "do-not-publish" not in repr(emitted)
    assert runtime.sync_state == "running"


def test_sync_toggle_persists_and_validates(rig):
    runtime, *_, saves, _ = rig
    assert runtime.set_sync_enabled(False) == {"enabled": False}
    assert runtime.sync_state == "paused"
    assert runtime.set_sync_enabled(True) == {"enabled": True}
    assert runtime.sync_state == "running"
    assert saves
    with pytest.raises(ApplicationError, match="boolean"):
        runtime.set_sync_enabled("false")


def test_saved_peer_reconnect_preserves_notes_and_pin():
    pairing, remote = identity("local"), identity("remote")
    cfg = Config(device_id="local", encryption_enabled=False)
    cfg.peers["remote"] = PeerInfo(
        "remote",
        "Remote",
        remote.get_identity().certificate_pem,
        True,
        notes="keep me",
        last_ip="127.0.0.1",
        last_port=9123,
    )
    transport = Transport()
    runtime = LanRuntime(
        cfg,
        pairing,
        None,
        History(),
        EventJournal(),
        lambda: None,
        monitor=Monitor(),
        reader=Clipboard(),
        writer=Clipboard(),
        transport=transport,
        discovery=Discovery(),
    )
    try:
        runtime.start()
        assert transport.dials[-1] == ("remote", "Remote", "127.0.0.1", 9123)
        runtime._refresh()
        assert cfg.peers["remote"].notes == "keep me"
        assert pairing.get_peer_certificate("remote") == remote.get_identity().certificate_pem
    finally:
        assert runtime.stop()


def test_partial_start_rolls_back_and_sanitizes_error():
    class BrokenDiscovery(Discovery):
        def start(self):
            raise RuntimeError("private-path-or-password")

    transport = Transport()
    runtime = LanRuntime(
        Config(device_id="local", encryption_enabled=False),
        identity("local"),
        None,
        History(),
        EventJournal(),
        lambda: None,
        monitor=Monitor(),
        reader=Clipboard(),
        writer=Clipboard(),
        transport=transport,
        discovery=BrokenDiscovery(),
    )
    with pytest.raises(ApplicationError) as caught:
        runtime.start()
    assert "private-path" not in str(caught.value)
    assert not transport.running
    assert runtime.stop()


@pytest.mark.parametrize("encrypted", [False, True])
def test_real_loopback_two_sided_pairing_and_clipboard(tmp_path, monkeypatch, encrypted):
    monkeypatch.setattr(TransportManager, "_secure_scratch_dir", staticmethod(lambda: tmp_path))
    runtimes = []

    def wait(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    def make(pid):
        pairing = identity(pid)
        clipboard = Clipboard()
        runtime = LanRuntime(
            Config(
                device_id=pid,
                device_name=pid,
                port=0,
                encryption_enabled=encrypted,
                retry_capture_enabled=False,
            ),
            pairing,
            EncryptionManager(pairing.get_identity().fingerprint) if encrypted else None,
            History(),
            EventJournal(),
            lambda: None,
            monitor=Monitor(),
            reader=clipboard,
            writer=clipboard,
            discovery=Discovery(),
        )
        runtimes.append(runtime)
        runtime.start()
        return runtime, clipboard

    try:
        a, _ = make("a")
        b, clipboard = make("b")
        a.discovery.found(
            peer_id_hash("b"), "B", "127.0.0.1", b.transport._server_sock.getsockname()[1]
        )
        assert a.start_pairing(peer_id_hash("b"))["accepted"]
        wait(lambda: "b" in a.transport.get_connected_peers())
        wait(lambda: "a" in b.transport.get_connected_peers())
        a._refresh()
        b._refresh()
        code_a = a.devices()["items"][0]["pairing_code"]
        code_b = b.devices()["items"][0]["pairing_code"]
        assert code_a == code_b
        assert a.devices()["items"][0]["sas"] == b.devices()["items"][0]["sas"]
        assert not a.confirm_pairing("b", code_a)["paired"]
        b.confirm_pairing("a", code_b)
        wait(lambda: a.pairing.is_peer_paired("b") and b.pairing.is_peer_paired("a"))
        a._background(a._on_local_sync, clip("loopback clipboard"))
        wait(lambda: bool(clipboard.writes))
        assert clipboard.writes[-1].types == {ContentType.TEXT: b"loopback clipboard"}
    finally:
        for runtime in runtimes:
            assert runtime.stop()


def test_save_failure_is_sanitized_and_peer_state_retried(rig):
    runtime, pairing, transport, *_ = rig
    transport.connected.add("remote")
    code = pairing.generate_shared_pairing_code("remote")
    pairing.mark_peer_confirmed("remote")
    saved = runtime._save_config

    def fail():
        raise OSError("secret-directory-and-password")

    runtime._save_config = fail
    try:
        with pytest.raises(ApplicationError) as caught:
            runtime.confirm_pairing("remote", code)
        assert caught.value.code == "SAVE_FAILED"
        assert "secret-directory" not in str(caught.value)
    finally:
        runtime._save_config = saved
    runtime._refresh()
    assert not runtime._persist_dirty


def test_stop_from_history_event_does_not_self_join(rig):
    runtime, _, _, _, clipboard, _, events, *_ = rig
    completed = threading.Event()
    results = []
    original = events.publish

    def publish(name, data, *args, **kwargs):
        if name == "history.changed":
            results.append(runtime.stop())
            completed.set()
        return original(name, data, *args, **kwargs)

    events.publish = publish
    clipboard.content = ClipboardContent({ContentType.TEXT: b"local action"})
    worker = threading.Thread(target=runtime.sync._do_read_and_send)
    worker.start()
    assert completed.wait(2)
    worker.join(2)
    assert results == [False]
    assert runtime.stop()


def test_outgoing_background_failure_does_not_log_secret_exception(rig, caplog):
    runtime, _, transport, _, clipboard, *_ = rig

    def fail(*args):
        raise OSError("password=never-log-me")

    transport.broadcast = fail
    clipboard.content = ClipboardContent({ContentType.TEXT: b"ordinary"})
    runtime.sync._do_read_and_send()
    assert "never-log-me" not in caplog.text
    assert "LAN_CALLBACK_FAILED" in caplog.text


def test_hash_alias_merge_preserves_metadata_without_promoting_trust():
    pairing, remote = identity("local"), identity("remote")
    cfg = Config(device_id="local", encryption_enabled=False)
    certificate = remote.get_identity().certificate_pem
    cfg.peers["remote"] = PeerInfo("remote", "Remote", certificate, False)
    alias = peer_id_hash("remote")
    cfg.peers[alias] = PeerInfo(alias, "Remote", certificate, True, notes="alias note")
    pairing.add_peer(alias, "Remote", certificate, True)
    runtime = LanRuntime(
        cfg,
        pairing,
        None,
        History(),
        EventJournal(),
        lambda: None,
        monitor=Monitor(),
        reader=Clipboard(),
        writer=Clipboard(),
        discovery=Discovery(),
        transport=Transport(),
    )
    try:
        runtime.start()
        assert alias not in cfg.peers
        assert cfg.peers["remote"].notes == "alias note"
        assert not pairing.is_peer_paired("remote")
        assert len(runtime.devices()["items"]) == 1
    finally:
        assert runtime.stop()


def test_maintenance_thread_start_failure_is_fully_rolled_back(monkeypatch):
    original = threading.Thread.start

    def start(thread):
        if thread.name == "clipsync-lan-state":
            raise RuntimeError("injected failure")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    transport, discovery, monitor = Transport(), Discovery(), Monitor()
    runtime = LanRuntime(
        Config(device_id="local", encryption_enabled=False),
        identity("local"),
        None,
        History(),
        EventJournal(),
        lambda: None,
        monitor=monitor,
        reader=Clipboard(),
        writer=Clipboard(),
        discovery=discovery,
        transport=transport,
    )
    with pytest.raises(ApplicationError):
        runtime.start()
    assert runtime.stop()
    assert not transport.running
    assert not discovery.running
    assert not monitor.running


def test_save_callback_can_snapshot_while_another_snapshot_is_reading(rig):
    runtime, _, _, _, _, _, events, *_ = rig
    journal_held = threading.Event()
    save_entered = threading.Event()
    snapshot_done = threading.Event()
    command_done = threading.Event()
    failures = []

    def read():
        journal_held.set()
        assert save_entered.wait(2)
        return {"devices": runtime.devices(), "sync_state": runtime.sync_state}

    def snapshot():
        try:
            events.snapshot(read)
        except Exception as exc:
            failures.append(exc)
        finally:
            snapshot_done.set()

    def save():
        save_entered.set()
        events.snapshot(lambda: {"devices": runtime.devices()})

    def command():
        try:
            runtime.set_sync_enabled(False)
        except Exception as exc:
            failures.append(exc)
        finally:
            command_done.set()

    runtime._save_config = save
    reader = threading.Thread(target=snapshot, daemon=True)
    writer = threading.Thread(target=command, daemon=True)
    reader.start()
    assert journal_held.wait(2)
    writer.start()
    assert snapshot_done.wait(2), "save held a lock needed by the journal snapshot"
    assert command_done.wait(2), "save callback could not acquire the journal"
    reader.join(2)
    writer.join(2)
    assert not failures


def test_certs_lists_pinned_fingerprints_for_known_peers(rig):
    runtime, pairing, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    devices = runtime.certs()["devices"]
    assert [row["device_id"] for row in devices] == ["remote"]
    row = devices[0]
    assert row["device_name"] == "Remote"
    assert row["paired"] is True
    assert row["fingerprint_short"] == (
        row["fingerprint"].replace(":", "")[:8] + "..." + row["fingerprint"].replace(":", "")[-8:]
    )
    assert row["fingerprint"].count(":") == 31


def test_device_probe_measures_lan_round_trip(rig):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    original = transport.send_to_peer

    def send(pid, data):
        message = decode_message(data)
        if message.msg_type == "device_ping":
            payload = message._raw_payload
            runtime._receive(
                frame("device_pong", ping_id=payload["ping_id"], ts=payload["ts"]), "remote"
            )
        return original(pid, data)

    transport.send_to_peer = send
    result = runtime.test_device("remote")
    assert result["ok"] is True
    assert [row["channel"] for row in result["results"]] == ["lan"]
    assert result["results"][0]["error"] is None
    assert result["results"][0]["latency_ms"] >= 0
    assert transport.sent[0][1].msg_type == "device_ping"
    assert not runtime._probes


def test_device_probe_marks_unsent_channel_without_waiting_for_timeout(rig):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    transport.send_to_peer = lambda pid, data: False
    started = time.monotonic()
    result = runtime.test_device("remote")
    assert time.monotonic() - started < runtime.DEVICE_PING_TIMEOUT
    assert result == {
        "ok": False,
        "results": [
            {"channel": "lan", "ok": False, "latency_ms": None, "error": "send_failed"}
        ],
    }
    assert not runtime._probes


def test_device_probe_reports_no_channel_for_unreachable_peer(rig):
    runtime, pairing, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    assert runtime.test_device("remote") == {"ok": False, "results": [], "error": "no_channel"}


def test_device_probe_rejects_unknown_devices(rig):
    runtime, *_ = rig
    with pytest.raises(ApplicationError) as caught:
        runtime.test_device("ghost")
    assert caught.value.code == "NOT_FOUND"


def test_device_ping_is_answered_only_for_paired_peers(rig):
    runtime, pairing, transport, *_ = rig
    transport.connected.add("remote")
    transport.message(frame("device_ping", ping_id="probe", ts=1.0), "remote")
    assert not transport.sent
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.message(frame("device_ping", ping_id="probe", ts=1.0), "remote")
    pong = transport.sent[-1][1]
    assert pong.msg_type == "device_pong"
    assert pong._raw_payload["ping_id"] == "probe"
    assert pong._raw_payload["ts"] == 1.0


def test_device_probe_uses_relay_channel_for_internet_peers(rig):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    runtime.config.internet_sync_enabled = True
    runtime.config.netpair_secrets["remote"] = "netpair-secret"
    published = []

    def publish(frame_bytes, topic, key, qos=0):
        message = decode_message(frame_bytes)
        published.append((message.msg_type, topic))
        if message.msg_type == "device_ping":
            payload = message._raw_payload
            runtime._receive_relay(encode_frame(
                {"msg_type": "device_pong", "ping_id": payload["ping_id"], "ts": payload["ts"]},
                source_device="remote",
            ))
        return True

    runtime.relay = type("Relay", (), {"publish": staticmethod(publish)})()
    try:
        result = runtime.test_device("remote")
    finally:
        runtime.relay = None
    assert result["ok"] is True
    assert [row["channel"] for row in result["results"]] == ["relay"]
    assert result["results"][0]["latency_ms"] >= 0
    assert [kind for kind, _ in published] == ["device_ping"]
    assert not transport.sent


def test_a_hello_answering_our_own_code_completes_the_pairing(rig):
    """The frame the whole internet handshake exists to deliver.

    A machine that generated a code is listening on the channel derived from
    that code's secret, so the entering peer's hello arrives here — and then has
    to be *recognized* as belonging to a code we generated rather than to a pair
    we already hold.  That lookup used to see only the confirmed pairs, so this
    frame was thrown away on arrival: nothing was stored, no reply went out, and
    the entering machine was left waiting forever on a hello that was never
    sent.  Nothing failed and nothing was logged; the only symptom was a pairing
    code that did nothing at all.
    """
    from types import SimpleNamespace

    from internal.transport.relay import decode_netpair_code, netpair_topic

    runtime, pairing, transport, _, _, _, events, *_ = rig
    runtime.config.internet_sync_enabled = True
    published = []

    def publish(frame_bytes, topic, key, qos=0):
        published.append((topic, decode_message(frame_bytes)))
        return True

    runtime.relay = SimpleNamespace(state="online", publish=publish, refresh_channels=lambda: None)
    service = runtime.internet_pairing
    # The runtime attaches the live relay to the pairing service when it starts
    # one, so a test that hands the runtime a relay has to hand it to the
    # service too — the service publishes the hello through its own reference.
    service.attach_relay(runtime.relay)
    try:
        code = service.generate()["code"]
        tag, secret = decode_netpair_code(code)
        # The entering machine: its real 12-hex id is what the pair has to end
        # up keyed by, and its hello names *our* tag — the identity it read out
        # of the code — which is how we know we are the generator.
        runtime._receive_relay(
            encode_frame(
                {"msg_type": "netpair_hello", "peer_id": tag, "device_name": "Remote"},
                source_device="b1b2b3b4b5b6",
            ),
            netpair_topic(secret),
        )
    finally:
        runtime.relay = None

    assert runtime.config.netpair_secrets == {"b1b2b3b4b5b6": secret}
    # The code has done its job: left pending, a second device could enter it.
    assert service.status()["generated_code"] is None
    assert service.status()["waiting"] == []
    # ...and the reply owes the enterer our real id, which it can learn from
    # nowhere else.  Addressed with our own tag it would name a machine that is
    # not the one waiting for it.
    assert [message.msg_type for _, message in published] == ["netpair_hello"]
    assert published[0][1]._raw_payload["peer_id"] == "b1b2b3b4b5b6"
    changed = events_named(events, "netpair.peer.changed")
    assert len(changed) == 1
    assert changed[0]["peer_id"] == "b1b2b3b4b5b6"
    assert changed[0]["name"] == "Remote"
    assert changed[0]["status"] == "paired" and changed[0]["online"] is True
    assert isinstance(changed[0]["last_seen"], int)


def test_a_reply_naming_our_device_re_keys_the_waiting_entry(rig):
    """The other half: the machine that entered somebody else's code.

    It can only ever hold the 4-char tag it read out of that code, and it stays
    out of the peer list until the generator's reply names *its* real device id
    and the provisional entry is re-keyed to the generator's.  Until then the
    pairing card shows a wait, not a device.
    """
    from types import SimpleNamespace

    from internal.transport.relay import (
        decode_netpair_code,
        generate_netpair_code,
        generate_netpair_secret,
        netpair_topic,
    )

    runtime, pairing, transport, _, _, _, events, *_ = rig
    runtime.config.internet_sync_enabled = True
    published = []

    def publish(frame_bytes, topic, key, qos=0):
        published.append((topic, decode_message(frame_bytes)))
        return True

    runtime.relay = SimpleNamespace(state="online", publish=publish, refresh_channels=lambda: None)
    service = runtime.internet_pairing
    service.attach_relay(runtime.relay)
    try:
        # Somebody else's code, addressed to their tag.
        code = generate_netpair_code("a1a2a3a4a5a6", generate_netpair_secret())
        tag, secret = decode_netpair_code(code)
        assert service.enter(code) == {"peer_id": tag, "waiting": True}
        # The wait is visible but is not a device: the tag cannot be reached or
        # sent to, and listing it as one showed a phantom that could not be
        # removed either.
        assert service.status()["peers"] == []
        assert [row["peer_id"] for row in service.status()["waiting"]] == [tag]
        assert published[0][1]._raw_payload["peer_id"] == tag

        runtime._receive_relay(
            encode_frame(
                {"msg_type": "netpair_hello", "peer_id": runtime.config.device_id,
                 "device_name": "Generator"},
                source_device="a1a2a3a4a5a6",
            ),
            netpair_topic(secret),
        )
    finally:
        runtime.relay = None

    # Only the real id is left: it is what the aliases, the delivery ledger and
    # the peer row are all keyed by.
    assert runtime.config.netpair_secrets == {"a1a2a3a4a5a6": secret}
    status = service.status()
    assert status["waiting"] == []
    assert [peer["peer_id"] for peer in status["peers"]] == ["a1a2a3a4a5a6"]
    # The enterer owes no reply: the handshake is done in one round trip beyond
    # its own hello.
    assert [message.msg_type for _, message in published] == ["netpair_hello"]
    assert events_named(events, "netpair.peer.changed")[0]["name"] == "Generator"


def test_relay_probe_requires_reachability_and_a_running_relay(rig):
    runtime, pairing, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    frame_bytes = encode_frame({"msg_type": "device_ping"}, source_device="local")
    assert not runtime._peer_is_internet_reachable("remote")
    assert not runtime._relay_publish_to_peer(frame_bytes, "remote")
    runtime.config.internet_sync_enabled = True
    runtime.config.peer_relay_secrets["remote"] = "peer-secret"
    # A LAN-paired peer only counts as relay-reachable while it is still paired.
    assert not runtime._peer_is_internet_reachable("remote")
    runtime.config.peers["remote"] = PeerInfo("remote", "Remote", paired=True)
    assert runtime._peer_is_internet_reachable("remote")
    assert not runtime._relay_publish_to_peer(frame_bytes, "remote")
    runtime.config.relay_secret = "local-secret"
    published = []

    def publish(frame_bytes, topic, key, qos=0):
        published.append((topic, key))
        return True

    runtime.relay = type("Relay", (), {"publish": staticmethod(publish)})()
    try:
        assert runtime._relay_publish_to_peer(frame_bytes, "remote")
    finally:
        runtime.relay = None
    assert published


# ── peer-to-peer update exchange (M2) ────────────────────────────────────
def test_update_request_serves_the_cached_asset_to_a_paired_peer(rig, monkeypatch, tmp_path):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    asset = tmp_path / "clipsync-windows.zip"
    asset.write_bytes(b"release-bytes")
    monkeypatch.setattr(updater, "get_cached_asset", lambda: str(asset))

    transport.message(frame("update_request"), "remote")

    sent = [msg for _, msg in transport.sent]
    assert [msg.msg_type for msg in sent] == ["file_request"]
    assert sent[0]._raw_payload["kind"] == "update"
    assert sent[0]._raw_payload["file_name"] == "clipsync-windows.zip"


def test_update_request_without_a_cached_asset_sends_nothing(rig, monkeypatch):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    monkeypatch.setattr(updater, "get_cached_asset", lambda: None)

    transport.message(frame("update_request"), "remote")

    assert transport.sent == []


def test_an_unpaired_peer_cannot_request_a_cached_update(rig, monkeypatch, tmp_path):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    asset = tmp_path / "clipsync-windows.zip"
    asset.write_bytes(b"release-bytes")
    monkeypatch.setattr(updater, "get_cached_asset", lambda: str(asset))

    transport.message(frame("update_request"), "remote")

    assert transport.sent == []


def test_request_update_from_peers_broadcasts_the_frame(rig):
    runtime, _, transport, *_ = rig
    runtime.request_update_from_peers()
    assert [msg.msg_type for msg in transport.broadcasts] == ["update_request"]


def test_a_peer_sent_update_blob_reaches_the_update_sink(rig, tmp_path):
    runtime, pairing, transport, *_ = rig
    # Received blobs must not land in the real receive folder during a test.
    runtime.file_transfer._output_dir = tmp_path
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    staged = []
    runtime.set_update_sink(staged.append)

    payload = b"verified release archive"
    transport.message(
        decode_message(
            encode_frame(
                {
                    "msg_type": "file_request",
                    "transfer_id": "u1",
                    "file_name": "clipsync-windows.zip",
                    "file_size": len(payload),
                    "mime_type": "application/zip",
                    "kind": "update",
                }
            )
        ),
        "remote",
    )
    transport.message(
        frame(
            "file_chunk",
            transfer_id="u1",
            chunk_index=0,
            total_chunks=1,
            data=base64.b64encode(payload).decode("ascii"),
        ),
        "remote",
    )

    deadline = time.monotonic() + 3
    while not staged and time.monotonic() < deadline:
        time.sleep(0.02)
    assert staged == [str(tmp_path / "clipsync-windows.zip")]
    assert Path(staged[0]).read_bytes() == payload


def test_an_ordinary_received_file_never_reaches_the_update_sink(rig, tmp_path):
    runtime, pairing, transport, *_ = rig
    runtime.file_transfer._output_dir = tmp_path
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    staged = []
    runtime.set_update_sink(staged.append)

    payload = b"ordinary file"
    transport.message(
        frame(
            "file_request",
            transfer_id="f1",
            file_name="notes.txt",
            file_size=len(payload),
            mime_type="text/plain",
        ),
        "remote",
    )
    # An ordinary file waits for the user; an update blob is auto-accepted.
    runtime.file_transfer.accept_transfer(
        "f1", lambda data: transport.send_to_peer("remote", data)
    )
    transport.message(
        frame(
            "file_chunk",
            transfer_id="f1",
            chunk_index=0,
            total_chunks=1,
            data=base64.b64encode(payload).decode("ascii"),
        ),
        "remote",
    )
    deadline = time.monotonic() + 2
    while not (tmp_path / "notes.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "notes.txt").read_bytes() == payload
    assert staged == []


# ── web-host accessors (the phone Companion's host callbacks) ────────────


def test_discovered_peers_shape_matches_the_web_devices_api(rig):
    runtime, _, _, discovery, *_ = rig
    hashed = peer_id_hash("remote")
    discovery.found(hashed, "Remote-ad", "127.0.0.1", 9999)
    assert runtime.discovered_peers() == {
        hashed: {"name": "Remote-ad", "address": "127.0.0.1", "port": 9999}
    }
    discovery.lost(hashed)
    assert runtime.discovered_peers() == {}


def test_resolved_hashes_and_reconnect_states_pass_through(rig):
    runtime, _, transport, *_ = rig
    transport.resolved["hash"] = "remote"
    transport.reconnects["remote"] = {"attempts": 2, "next_in": 5}
    assert runtime.resolved_hashes() == {"hash": "remote"}
    assert runtime.reconnect_states() == {"remote": {"attempts": 2, "next_in": 5}}


def test_pending_pairings_append_the_shared_sas_code(rig):
    from internal.security.fingerprint import sas_code

    runtime, pairing, *_ = rig
    assert runtime.pending_pairings() == []
    code = pairing.generate_shared_pairing_code("remote")
    rows = runtime.pending_pairings()
    assert len(rows) == 1
    peer_id, pending_code, name, status, sas = rows[0]
    assert (peer_id, pending_code, name) == ("remote", code, "Remote")
    assert status == "pending"
    assert sas == sas_code(
        pairing.get_identity().fingerprint, pairing.get_peer_fingerprint("remote")
    )
    assert len(sas) == 9 and "-" in sas  # e.g. "48BF-1A5D"


def test_device_action_maps_legacy_panel_names(rig):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    assert runtime.device_action("edit_note", "remote", "work laptop") is True
    assert runtime.config.peers["remote"].notes == "work laptop"
    assert runtime.device_action("disconnect", "remote") is True
    assert ("remote", True) in transport.disconnected


@pytest.mark.parametrize(
    "action, device_id, args",
    [
        ("bogus", "remote", ()),          # the panel sent an action we do not know
        ("connect", "ghost", ()),         # unknown device
        ("edit_note", "ghost", ("note",)),
    ],
)
def test_device_action_reports_failure_instead_of_raising(rig, action, device_id, args):
    runtime, *_ = rig
    assert runtime.device_action(action, device_id, *args) is False


def test_device_action_unpair_of_an_unknown_device_is_not_an_error(rig):
    """An unknown peer ends pairing as ``{"accepted": False}`` without raising.

    The runtime treats "nothing to unpair" as a normal answer, so the web
    callback reports success exactly like the legacy handler did.
    """
    runtime, *_ = rig
    assert runtime.device_action("unpair", "ghost") is True


def test_web_transfer_action_maps_history_delete_to_delete(rig, monkeypatch):
    runtime = rig[0]
    calls = []
    monkeypatch.setattr(runtime, "transfer_action", lambda action, tid: calls.append((action, tid)))
    assert runtime.web_transfer_action("history_delete", "t1") is True
    assert runtime.web_transfer_action("pause", "t2") is True
    assert calls == [("delete", "t1"), ("pause", "t2")]


def test_web_transfer_action_rejects_unknown_actions_without_dispatching(rig, monkeypatch):
    runtime = rig[0]
    calls = []
    monkeypatch.setattr(runtime, "transfer_action", lambda action, tid: calls.append(action))
    assert runtime.web_transfer_action("rm -rf", "t1") is False
    assert calls == []


def test_web_transfer_action_reports_a_failed_action(rig, monkeypatch):
    runtime = rig[0]

    def boom(action, tid):
        raise ApplicationError("NOT_FOUND", "Transfer not found")

    monkeypatch.setattr(runtime, "transfer_action", boom)
    assert runtime.web_transfer_action("cancel", "gone") is False


def test_forward_file_requires_a_connected_peer(rig, monkeypatch):
    runtime, _, transport, *_ = rig
    sent = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (sent.append((path, send)), "tid")[1],
    )
    assert runtime.forward_file("/tmp/upload.bin", "remote") is False
    assert sent == []
    transport.connected.add("remote")
    assert runtime.forward_file("/tmp/upload.bin", "remote") is True
    path, send = sent[-1]
    assert path == "/tmp/upload.bin"
    send(b"frame")
    assert transport.sent[-1][0] == "remote"


def test_forward_file_reports_a_transfer_that_could_not_start(rig, monkeypatch):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (_ for _ in ()).throw(OSError("disk gone")),
    )
    assert runtime.forward_file("/tmp/upload.bin", "remote") is False


def test_send_files_names_one_peer_and_refuses_an_unreachable_one(rig, monkeypatch):
    runtime, _, transport, *_ = rig
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append((path, send)), "tid")[1],
    )
    transport.connected.add("remote")
    assert runtime.send_files(["/tmp/a.bin"], "remote") == "tid"
    path, send = started[-1]
    assert path == "/tmp/a.bin"
    send(encode_frame({"msg_type": "file_chunk"}))
    assert transport.sent[-1][0] == "remote"
    assert transport.broadcasts == []

    started.clear()
    with pytest.raises(ApplicationError) as offline:
        runtime.send_files(["/tmp/a.bin"], "gone")
    # A named target that is not reachable is refused, never widened to every
    # peer: files landing on machines that were not asked for is the failure
    # this parameter exists to prevent.
    assert offline.value.code == "NOT_CONNECTED"
    assert started == []
    assert transport.broadcasts == []


def test_send_files_resolves_a_hashed_peer_id(rig, monkeypatch):
    runtime, _, transport, *_ = rig
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append((path, send)), "tid")[1],
    )
    transport.connected.add("remote")
    transport.resolved[peer_id_hash("remote")] = "remote"
    assert runtime.send_files(["/tmp/a.bin"], peer_id_hash("remote")) == "tid"
    _, send = started[-1]
    send(encode_frame({"msg_type": "file_chunk"}))
    assert transport.sent[-1][0] == "remote"


def test_send_files_without_a_target_still_reaches_every_peer(rig, monkeypatch):
    runtime, _, transport, *_ = rig
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append((path, send)), "tid")[1],
    )
    assert runtime.send_files(["/tmp/a.bin"]) == "tid"
    _, send = started[-1]
    send(encode_frame({"msg_type": "file_chunk"}))
    # No target named: the broadcast every pre-selector caller used.
    assert transport.broadcasts
    assert transport.sent == []


def temp_archives(folder_name):
    """The archives a folder send left in the temp directory, by its own name."""
    return set(Path(tempfile.gettempdir()).glob(f"{folder_name}-*.zip"))


def test_a_folder_send_archives_it_and_the_archive_outlives_the_transfer(
    rig, monkeypatch, tmp_path
):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    folder = tmp_path / "Photos"
    (folder / "nested").mkdir(parents=True)
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (folder / "nested" / "b.txt").write_text("b", encoding="utf-8")
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append(path), "tid")[1],
    )
    assert runtime.send_files([str(folder)], "remote") == "tid"
    # What the wire carries is the archive, not the folder: the transfer manager
    # takes files, and a receiver cannot be handed a directory.
    sent_path = Path(started[-1])
    assert sent_path.suffix == ".zip" and sent_path.name.startswith("Photos-")
    assert sent_path.is_file()

    assert runtime._outgoing_archives == {"tid": str(sent_path)}
    # The file is still there between the start and the completion, because the
    # receiver reads it for as long as the transfer lasts.
    assert sent_path.is_file()
    runtime._on_transfer_complete("tid", True, False, "completed")
    assert runtime._outgoing_archives == {}
    assert not sent_path.exists()


def test_a_folder_with_nothing_to_send_is_refused_before_anything_starts(
    rig, monkeypatch, tmp_path
):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    folder = tmp_path / "Empty"
    folder.mkdir()
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append(path), "tid")[1],
    )
    with pytest.raises(ApplicationError) as refused:
        runtime.send_files([str(folder)], "remote")
    # Refused rather than sent: an empty archive transfers fine and arrives
    # empty, which reads to the receiver as a broken send rather than as a
    # folder that had nothing in it.
    assert refused.value.code == "INVALID_ARGUMENT"
    assert started == []
    assert runtime._outgoing_archives == {}


def test_a_folder_send_to_an_unreachable_peer_leaves_no_archive_behind(rig, monkeypatch, tmp_path):
    runtime, _, *_ = rig
    folder = tmp_path / "Photos"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(runtime.file_transfer, "send_file", lambda path, send: "tid")
    before = temp_archives("Photos")
    with pytest.raises(ApplicationError) as offline:
        runtime.send_files([str(folder)], "gone")
    # Nothing was started, so no completion event will come to reclaim the
    # archive: the refusal has to take it with it.
    assert offline.value.code == "NOT_CONNECTED"
    assert temp_archives("Photos") == before
    assert runtime._outgoing_archives == {}


def test_a_file_send_registers_no_archive_and_its_completion_still_goes_out(
    rig, monkeypatch, tmp_path
):
    runtime, _, _, _, _, _, events, *_ = rig
    source = tmp_path / "notes.txt"
    source.write_text("x", encoding="utf-8")
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append(path), "tid")[1],
    )
    assert runtime.send_files([str(source)]) == "tid"
    # A file goes as itself; only a folder has an archive to reclaim.
    assert started == [str(source)]
    assert runtime._outgoing_archives == {}
    runtime._on_transfer_complete("tid", True, False, "completed")
    # The completion is reported as it always was, with no archive in the picture.
    assert events_named(events, "transfer.complete")[-1]["transfer_id"] == "tid"


def test_several_picks_go_as_one_archive_and_one_transfer(rig, monkeypatch, tmp_path):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append(path), "tid")[1],
    )
    assert runtime.send_files([str(first), str(second)], "remote") == "tid"
    # One archive for the whole pick, so the receiver sees one transfer rather
    # than one per file -- the legacy 发送文件 dialog's multi-select.
    assert len(started) == 1
    sent_path = Path(started[0])
    assert sent_path.suffix == ".zip" and sent_path.name.startswith("files-2-")
    with zipfile.ZipFile(sent_path) as bundle:
        assert sorted(bundle.namelist()) == ["a.txt", "b.txt"]
    assert runtime._outgoing_archives == {"tid": str(sent_path)}
    runtime._on_transfer_complete("tid", True, False, "completed")
    assert not sent_path.exists()


def test_a_pick_that_is_gone_between_the_pick_and_the_send_is_refused(rig, monkeypatch, tmp_path):
    runtime, _, transport, *_ = rig
    transport.connected.add("remote")
    kept = tmp_path / "kept.txt"
    kept.write_text("a", encoding="utf-8")
    started = []
    monkeypatch.setattr(
        runtime.file_transfer, "send_file",
        lambda path, send: (started.append(path), "tid")[1],
    )
    with pytest.raises(ApplicationError) as refused:
        runtime.send_files([str(kept), str(tmp_path / "vanished.txt")], "remote")
    # Sending the picks that are still there would quietly deliver less than the
    # user chose, which is the failure the panel's silent skip had; the whole
    # send is refused instead.
    assert refused.value.code == "INVALID_ARGUMENT"
    assert started == []
    assert not temp_archives("files-2")


def test_nothing_picked_is_refused(rig):
    runtime, *_ = rig
    with pytest.raises(ApplicationError) as refused:
        runtime.send_files([])
    assert refused.value.code == "INVALID_ARGUMENT"


def test_transfer_lists_and_speed_test_state_pass_raw_manager_rows(rig, monkeypatch):
    runtime = rig[0]
    active, history = [{"transfer_id": "a", "state": "sending"}], [{"transfer_id": "b"}]
    monkeypatch.setattr(runtime.file_transfer, "get_transfers", lambda: active)
    monkeypatch.setattr(runtime.file_transfer, "get_history", lambda: history)
    monkeypatch.setattr(
        runtime.file_transfer, "get_speed_test", lambda: {"running": True, "mbps": 12.5}
    )
    assert runtime.transfer_lists() == (active, history)
    assert runtime.speed_test_state() == {"running": True, "mbps": 12.5}


def test_speed_test_state_degrades_to_an_empty_mapping(rig, monkeypatch):
    runtime = rig[0]
    monkeypatch.setattr(runtime.file_transfer, "get_speed_test", lambda: None)
    assert runtime.speed_test_state() == {}


def test_record_web_upload_delegates_to_the_transfer_manager(rig, monkeypatch):
    runtime = rig[0]
    calls = []
    monkeypatch.setattr(
        runtime.file_transfer, "record_web_upload",
        lambda name, size, path: (calls.append((name, size, path)), "tid")[1],
    )
    assert runtime.record_web_upload("photo.png", 1234, "/tmp/photo.png") == "tid"
    assert calls == [("photo.png", 1234, "/tmp/photo.png")]


def test_current_relay_broker_requires_internet_sync_and_a_live_relay(rig):
    from types import SimpleNamespace

    runtime = rig[0]
    assert runtime.current_relay_broker() == ""
    runtime.config.internet_sync_enabled = True
    assert runtime.current_relay_broker() == ""
    # Restore by hand: this suite's autouse conftest fixture owns the
    # ``monkeypatch`` instance, so its undo runs *after* the rig teardown.
    try:
        runtime.relay = SimpleNamespace(current_broker="mqtts://broker.example:8883")
        assert runtime.current_relay_broker() == "mqtts://broker.example:8883"
        runtime.relay = SimpleNamespace()
        assert runtime.current_relay_broker() == ""
    finally:
        runtime.relay = None


def test_connect_and_disconnect_publish_presence_events(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    runtime._refresh()
    assert events_named(events, "device.connected") == [
        {"device_id": "remote", "name": "Remote"}
    ]
    # A refresh with nothing changed must not repeat the notice.
    runtime._refresh()
    assert len(events_named(events, "device.connected")) == 1
    transport.connected.discard("remote")
    runtime._refresh()
    assert events_named(events, "device.disconnected") == [
        {"device_id": "remote", "name": "Remote"}
    ]


def test_connecting_to_a_peer_that_is_nowhere_publishes_the_reason(rig):
    """A connect that cannot even start is still reported, with a name.

    The route answers ``{accepted: false}`` either way, so a UI that only read
    the response would show a bare failure; the reason rides out as an event
    (legacy's ``connect_unreachable``), named from the config first and from
    the pairing repo otherwise.
    """
    runtime, pairing, transport, _, _, _, events, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    pairing.add_peer("ghost", "", pairing.get_peer_certificate("remote"), paired=True)

    assert runtime.connect_device("remote") == {"accepted": False}
    assert events_named(events, "device.connection_unreachable") == [
        {"device_id": "remote", "name": "Remote"}
    ]
    assert not transport.dials

    # The configured name wins over the one the pairing repo remembers.
    runtime.config.peers["remote"] = PeerInfo(device_id="remote", device_name="Renamed")
    assert runtime.connect_device("remote") == {"accepted": False}
    assert events_named(events, "device.connection_unreachable")[-1] == {
        "device_id": "remote", "name": "Renamed"
    }

    # A peer nobody has a name for is still reported, just without one.
    assert runtime.connect_device("ghost") == {"accepted": False}
    assert events_named(events, "device.connection_unreachable")[-1] == {
        "device_id": "ghost", "name": ""
    }

    # A dial that starts is a plain success — nothing to explain.
    transport.addresses["remote"] = ("Remote", "127.0.0.1", 9000)
    assert runtime.connect_device("remote") == {"accepted": True}
    assert len(events_named(events, "device.connection_unreachable")) == 3


def test_pairing_request_publishes_once_after_the_debounce(rig):
    """One code, one notice — however many times a UI refreshes.

    The maintenance loop ticks once as soon as the runtime starts, so how many
    refreshes it takes to get past the debounce window is not deterministic
    (and the window is zero here); what the debounce guarantees is that the
    notice is never repeated for the same request.
    """
    runtime, pairing, transport, _, _, _, events, *_ = rig
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    code = pairing.get_pending_pairings()[0][1]

    runtime._refresh()
    runtime._refresh()
    published = events_named(events, "pairing.request")
    assert len(published) == 1
    assert published[0] == {
        "device_id": "remote",
        "name": "Remote",
        "code": code,
        # The SAS the user compares on both devices — the same one the device
        # list carries for the phone's confirmation card.
        "sas": runtime.pending_pairings()[0][4],
    }
    assert runtime.pending_pairings()[0][4]  # the rig's peer has a pinned cert
    runtime._refresh()
    assert len(events_named(events, "pairing.request")) == 1


def test_a_resolved_pairing_is_published_with_its_status(rig):
    """The prompt card is settled from the event, not from the next poll."""
    runtime, pairing, transport, _, _, _, events, *_ = rig
    transport.connected.add("remote")
    code = pairing.generate_shared_pairing_code("remote")
    assert runtime.confirm_pairing("remote", code)["status"] == "confirmed_waiting"
    assert events_named(events, "pairing.resolved") == [
        {"device_id": "remote", "status": "confirmed_waiting"}
    ]

    # The peer confirms: the same event carries the paired status.
    runtime._receive(frame("pairing_confirm"), "remote")
    assert events_named(events, "pairing.resolved")[-1] == {
        "device_id": "remote", "status": "paired"
    }

    # Rejecting from either side settles it as well, with no status.
    runtime.reject_pairing("remote")
    assert events_named(events, "pairing.resolved")[-1] == {"device_id": "remote", "status": ""}


def test_a_chat_invite_settles_the_pairing_prompt_it_suppressed(rig):
    """A prompt the invite suppressed must not stay on screen elsewhere."""
    runtime, pairing, transport, _, _, _, events, *_ = rig
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._receive(
        frame("chat_invite", session_id="abcdef0123456789", from_name="Remote"),
        "remote",
    )

    assert events_named(events, "pairing.request") == []
    assert events_named(events, "pairing.resolved") == [
        {"device_id": "remote", "status": ""}
    ]


def test_pairing_notice_waits_for_the_chat_invite_window(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    runtime.PAIRING_NOTICE_DELAY = 60
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert events_named(events, "pairing.request") == []


def test_chat_peer_never_gets_a_pairing_notice(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    transport.connected.add("remote")
    runtime._receive(frame(
        "chat_invite", session_id="abcdef0123456789",
        from_name="Remote", fingerprint_short="FORGED",
    ), "remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert events_named(events, "pairing.request") == []


def test_resolved_pairings_notify_again_but_expired_ones_do_not(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert len(events_named(events, "pairing.request")) == 1
    # Rejecting is terminal: the same deterministic code must notify again.
    runtime.reject_pairing("remote")
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert len(events_named(events, "pairing.request")) == 2
    # Plain expiry is not: an ignored request must not re-prompt.
    code = pairing._pending_pairings["remote"][0]
    pairing._pending_pairings["remote"] = (code, time.time() - PAIRING_TIMEOUT - 1)
    runtime._refresh()
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert len(events_named(events, "pairing.request")) == 2


# ── received-file sound (legacy ``_play_transfer_sound``) ────────────────


def sound_spy(monkeypatch):
    """Record play_sound calls instead of playing audio."""
    from internal.platform.notify import NotificationManager

    played = []
    monkeypatch.setattr(
        NotificationManager, "play_sound", staticmethod(lambda: played.append(True))
    )
    return played


def _no_audio_tool(*_args, **_kwargs):
    raise RuntimeError("no audio tool")


def test_a_received_file_beeps_but_a_peer_update_blob_does_not(rig, tmp_path, monkeypatch):
    runtime, pairing, transport, *_ = rig
    played = sound_spy(monkeypatch)
    runtime.file_transfer._output_dir = tmp_path
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")

    payload = b"ordinary file"
    transport.message(
        frame(
            "file_request",
            transfer_id="f1",
            file_name="notes.txt",
            file_size=len(payload),
            mime_type="text/plain",
        ),
        "remote",
    )
    runtime.file_transfer.accept_transfer(
        "f1", lambda data: transport.send_to_peer("remote", data)
    )
    transport.message(
        frame(
            "file_chunk",
            transfer_id="f1",
            chunk_index=0,
            total_chunks=1,
            data=base64.b64encode(payload).decode("ascii"),
        ),
        "remote",
    )
    deadline = time.monotonic() + 2
    while not (tmp_path / "notes.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "notes.txt").read_bytes() == payload
    assert played == [True]

    staged = []
    runtime.set_update_sink(staged.append)
    transport.message(
        decode_message(
            encode_frame(
                {
                    "msg_type": "file_request",
                    "transfer_id": "u1",
                    "file_name": "clipsync-windows.zip",
                    "file_size": 4,
                    "mime_type": "application/zip",
                    "kind": "update",
                }
            )
        ),
        "remote",
    )
    transport.message(
        frame(
            "file_chunk",
            transfer_id="u1",
            chunk_index=0,
            total_chunks=1,
            data=base64.b64encode(b"blob").decode("ascii"),
        ),
        "remote",
    )
    deadline = time.monotonic() + 3
    while not staged and time.monotonic() < deadline:
        time.sleep(0.02)
    assert staged
    assert played == [True]


def test_both_switches_silence_the_beep_and_a_missing_tool_never_raises(rig, monkeypatch):
    from internal.platform.notify import NotificationManager

    runtime, *_ = rig
    played = sound_spy(monkeypatch)
    runtime._on_file_received("t1", "/tmp/notes.txt", "notes.txt")
    assert played == [True]
    runtime.config.sound_enabled = False
    runtime._on_file_received("t2", "/tmp/notes.txt", "notes.txt")
    assert played == [True]
    runtime.config.sound_enabled = True
    runtime.config.notifications_enabled = False
    runtime._on_file_received("t3", "/tmp/notes.txt", "notes.txt")
    assert played == [True]
    runtime.config.notifications_enabled = True
    # A missing/failing sound tool must not break the receive flow.
    monkeypatch.setattr(NotificationManager, "play_sound", staticmethod(_no_audio_tool))
    runtime._on_file_received("t4", "/tmp/notes.txt", "notes.txt")
    assert played == [True]


def test_a_phone_upload_beeps_even_when_recording_it_fails(rig, monkeypatch):
    runtime, *_ = rig
    played = sound_spy(monkeypatch)
    monkeypatch.setattr(runtime.file_transfer, "record_web_upload", lambda *args: "web-tid")
    assert runtime.record_web_upload("photo.png", 5, "/tmp/photo.png") == "web-tid"
    assert played == [True]

    def _disk_full(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runtime.file_transfer, "record_web_upload", _disk_full)
    with pytest.raises(RuntimeError):
        runtime.record_web_upload("photo.png", 5, "/tmp/photo.png")
    assert played == [True, True]


# ── an invite cancels the pairing prompt for that peer ───────────────────


def invite_frame():
    return frame(
        "chat_invite",
        session_id="abcdef0123456789",
        from_name="Remote",
        fingerprint_short="FORGED",
    )


def test_a_chat_invite_drops_the_pending_pairing_card(rig):
    runtime, pairing, transport, *_ = rig
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    assert len(runtime.pending_pairings()) == 1
    row = next(item for item in runtime.devices()["items"] if item["id"] == "remote")
    assert row["pairing_code"] and row["pairing_status"] == "pending"

    runtime._receive(invite_frame(), "remote")

    # The consent-gated chat is the only thing the peer asked for: no pairing
    # prompt is left behind, but the invite is still there to answer.
    assert runtime.pending_pairings() == []
    row = next(item for item in runtime.devices()["items"] if item["id"] == "remote")
    assert row["pairing_code"] == "" and not row["paired"]
    assert runtime.chat_sessions()["sessions"][0]["status"] == "invited"


def test_a_paired_peer_inviting_to_chat_keeps_its_pairing(rig):
    runtime, pairing, transport, *_ = rig
    pairing.add_peer("remote", "Remote", pairing.get_peer_certificate("remote"), paired=True)
    transport.connected.add("remote")
    runtime._refresh()

    runtime._receive(invite_frame(), "remote")

    row = next(item for item in runtime.devices()["items"] if item["id"] == "remote")
    assert row["paired"] and row["pairing_status"] == "paired"


def test_a_pairing_request_after_a_chat_invite_can_notify_again(rig):
    runtime, pairing, transport, _, _, _, events, *_ = rig
    transport.connected.add("remote")
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert len(events_named(events, "pairing.request")) == 1

    runtime._receive(invite_frame(), "remote")
    assert runtime.pending_pairings() == []
    # While the conversation is live its connection keeps regenerating the same
    # code, so the peer stays out of the notices; once it is closed, a genuine
    # request must notify again — the code is derived from both fingerprints,
    # so the forgotten de-duplication is what keeps it from being swallowed.
    session_id = runtime.chat_sessions()["sessions"][0]["session_id"]
    assert runtime.chat_action("decline", session_id)
    pairing.generate_shared_pairing_code("remote")
    runtime._refresh()
    runtime._refresh()
    assert len(events_named(events, "pairing.request")) == 2
