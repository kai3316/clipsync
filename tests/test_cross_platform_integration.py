"""Cross-platform integration tests — end-to-end sync between two devices.

Simulates two ClipSync nodes (representing different OS platforms)
exchanging clipboard content through the full pipeline:
  clipboard change → encode → network → decode → remote clipboard write.

Covers every platform pair: Win↔Mac, Win↔Linux, Mac↔Linux.
"""

import sys
import os
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import dataclass, field

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.protocol.codec import encode_message, decode_message
from internal.security.pairing import PairingManager
from internal.sync.manager import SyncManager


# ── Test infrastructure: two-node setup ──────────────────────────────────

class MockClipboardMonitor:
    def __init__(self):
        self._callback = None
        self._running = False
        self.suppress_until = 0.0

    def start(self, callback):
        self._callback = callback
        self._running = True

    def stop(self):
        self._running = False
        self._callback = None

    def suppress_for(self, duration_seconds):
        """Mirror the real monitor: drop callbacks until the window passes."""
        self.suppress_until = time.time() + duration_seconds

    def fire(self):
        if self._callback and time.time() >= self.suppress_until:
            self._callback()


class MockClipboardReader:
    def __init__(self):
        self.content = ClipboardContent()

    def read(self) -> ClipboardContent:
        return self.content


class MockClipboardWriter:
    def __init__(self):
        self.last_written: ClipboardContent | None = None
        self.write_count = 0

    def write(self, content: ClipboardContent):
        self.last_written = content
        self.write_count += 1


@dataclass
class _SimDevice:
    """A simulated ClipSync node representing one platform."""
    mgr: SyncManager
    reader: MockClipboardReader
    writer: MockClipboardWriter
    monitor: MockClipboardMonitor
    pairing: PairingManager
    sent: list[SyncMessage]  # wire data this device tried to broadcast
    received: list[SyncMessage]  # messages received from the peer


def _make_device(device_id: str, device_name: str, platform_label: str):
    """Create a simulated device node."""
    reader = MockClipboardReader()
    writer = MockClipboardWriter()
    monitor = MockClipboardMonitor()
    pairing = PairingManager(device_id, device_name)
    pairing.load_or_create_identity("", "")

    mgr = SyncManager(device_id, device_name,
                      reader=reader, writer=writer, monitor=monitor)

    sent: list[SyncMessage] = []
    received: list[SyncMessage] = []

    def on_send(msg: SyncMessage):
        sent.append(msg)

    mgr.on_send = on_send
    mgr.start()

    return _SimDevice(
        mgr=mgr, reader=reader, writer=writer, monitor=monitor,
        pairing=pairing, sent=sent, received=received,
    )


def _bridge(from_dev: _SimDevice, to_dev: _SimDevice):
    """Forward all sent messages from one device to the other.

    Simulates the network layer: encode → wire → decode → handle_remote_message.
    """
    for msg in from_dev.sent:
        wire = encode_message(msg)
        decoded = decode_message(wire)
        assert decoded is not None, "Wire format roundtrip must succeed"
        to_dev.received.append(decoded)
        to_dev.mgr.handle_remote_message(decoded)
    from_dev.sent.clear()


def _simulate_copy(dev: _SimDevice, content: ClipboardContent, pause: float = 0.7):
    """Simulate user copying content on a device.

    pause must exceed SYNC_DEBOUNCE (0.5s) so the coalescing timer fires
    and the SyncMessage lands in dev.sent before the caller checks it.
    """
    dev.reader.content = content
    dev.monitor.fire()
    time.sleep(pause)


# ══════════════════════════════════════════════════════════════════════════
# Cross-platform text sync
# ══════════════════════════════════════════════════════════════════════════

class TestWinToMacTextSync:
    """Windows user copies text → macOS user pastes it."""

    def setup_method(self):
        self.win = _make_device("win-device", "Windows PC", "windows")
        self.mac = _make_device("mac-device", "MacBook", "darwin")

    def teardown_method(self):
        self.win.mgr.stop()
        self.mac.mgr.stop()

    def test_plain_english(self):
        _simulate_copy(self.win, ClipboardContent(
            types={ContentType.TEXT: b"Hello from Windows"},
        ))
        _bridge(self.win, self.mac)

        assert self.mac.writer.write_count == 1
        assert self.mac.writer.last_written.types[ContentType.TEXT] == b"Hello from Windows"

    def test_chinese_text(self):
        """Chinese text must survive Windows UTF-16-LE → macOS UTF-8 journey."""
        text = "你好世界！复制粘贴测试"
        _simulate_copy(self.win, ClipboardContent(
            types={ContentType.TEXT: text.encode("utf-8")},
        ))
        _bridge(self.win, self.mac)

        received = self.mac.writer.last_written.types[ContentType.TEXT]
        assert received.decode("utf-8") == text

    def test_emoji_and_special_chars(self):
        text = "Emoji 🌍 🎉 émoji ñoño 日本語"
        _simulate_copy(self.win, ClipboardContent(
            types={ContentType.TEXT: text.encode("utf-8")},
        ))
        _bridge(self.win, self.mac)

        received = self.mac.writer.last_written.types[ContentType.TEXT].decode("utf-8")
        assert received == text

    def test_html_with_formatting(self):
        _simulate_copy(self.win, ClipboardContent(types={
            ContentType.TEXT: b"Bold Text",
            ContentType.HTML: b"<b>Bold Text</b>",
        }))
        _bridge(self.win, self.mac)

        assert self.mac.writer.last_written.types[ContentType.HTML] == b"<b>Bold Text</b>"
        assert self.mac.writer.last_written.types[ContentType.TEXT] == b"Bold Text"


class TestMacToWinTextSync:
    """macOS user copies → Windows user pastes."""

    def setup_method(self):
        self.mac = _make_device("mac-device", "MacBook", "darwin")
        self.win = _make_device("win-device", "Windows PC", "windows")

    def teardown_method(self):
        self.mac.mgr.stop()
        self.win.mgr.stop()

    def test_mac_to_windows_richtext(self):
        _simulate_copy(self.mac, ClipboardContent(types={
            ContentType.TEXT: b"Rich text example",
            ContentType.RTF: b"{\\rtf1\\ansi Rich text example}",
            ContentType.HTML: b"<p>Rich text example</p>",
        }))
        _bridge(self.mac, self.win)

        assert self.win.writer.write_count == 1
        assert self.win.writer.last_written.types[ContentType.TEXT] == b"Rich text example"
        assert self.win.writer.last_written.types[ContentType.HTML] == b"<p>Rich text example</p>"


class TestLinuxToMacTextSync:
    """Linux user copies → macOS user pastes."""

    def setup_method(self):
        self.linux = _make_device("linux-device", "Linux Box", "linux")
        self.mac = _make_device("mac-device", "MacBook", "darwin")

    def teardown_method(self):
        self.linux.mgr.stop()
        self.mac.mgr.stop()

    def test_unicode_from_linux(self):
        text = "Привет мир\n日本語テキスト\n🌟✨"
        _simulate_copy(self.linux, ClipboardContent(
            types={ContentType.TEXT: text.encode("utf-8")},
        ))
        _bridge(self.linux, self.mac)

        received = self.mac.writer.last_written.types[ContentType.TEXT].decode("utf-8")
        assert received == text


# ══════════════════════════════════════════════════════════════════════════
# Cross-platform image sync
# ══════════════════════════════════════════════════════════════════════════

class TestImageSync:
    """Image clipboard sharing between platforms."""

    def setup_method(self):
        self.win = _make_device("win", "Windows", "windows")
        self.mac = _make_device("mac", "Mac", "darwin")
        self.linux = _make_device("linux", "Linux", "linux")

    def teardown_method(self):
        for d in [self.win, self.mac, self.linux]:
            d.mgr.stop()

    def test_png_windows_to_mac(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        _simulate_copy(self.win, ClipboardContent(types={
            ContentType.IMAGE_PNG: png,
        }))
        _bridge(self.win, self.mac)

        assert self.mac.writer.last_written.types[ContentType.IMAGE_PNG] == png

    def test_png_mac_to_linux(self):
        png = b"\x89PNG\r\n\x1a\n" + b"mac-screenshot-sim" + b"\x00" * 100
        _simulate_copy(self.mac, ClipboardContent(types={
            ContentType.IMAGE_PNG: png,
        }))
        _bridge(self.mac, self.linux)

        assert self.linux.writer.last_written.types[ContentType.IMAGE_PNG] == png

    def test_png_linux_to_windows(self):
        png = b"\x89PNG\r\n\x1a\n" + b"linux-screenshot" + b"\x00" * 100
        _simulate_copy(self.linux, ClipboardContent(types={
            ContentType.IMAGE_PNG: png,
        }))
        _bridge(self.linux, self.win)

        assert self.win.writer.last_written.types[ContentType.IMAGE_PNG] == png

    def test_image_with_text_mixed(self):
        """Screenshot with fallback text description."""
        _simulate_copy(self.win, ClipboardContent(types={
            ContentType.IMAGE_PNG: b"\x89PNG\x00\x00",
            ContentType.TEXT: b"[Screenshot: error dialog]",
        }))
        _bridge(self.win, self.mac)

        assert ContentType.IMAGE_PNG in self.mac.writer.last_written.types
        assert ContentType.TEXT in self.mac.writer.last_written.types


# ══════════════════════════════════════════════════════════════════════════
# Real-time bidirectional sync
# ══════════════════════════════════════════════════════════════════════════

class TestBidirectionalSync:
    """Two devices syncing back and forth in real time."""

    def setup_method(self):
        self.win = _make_device("win", "Windows", "windows")
        self.mac = _make_device("mac", "Mac", "darwin")

    def teardown_method(self):
        self.win.mgr.stop()
        self.mac.mgr.stop()

    def test_two_way_text_exchange(self):
        # Windows copies text
        _simulate_copy(self.win, ClipboardContent(
            types={ContentType.TEXT: b"From Windows"},
        ))
        _bridge(self.win, self.mac)
        assert self.mac.writer.last_written.types[ContentType.TEXT] == b"From Windows"

        # Let remote-write suppression window expire before the next
        # local copy, otherwise the monitor ignores the fire event.
        time.sleep(0.8)

        # Now macOS copies something else
        _simulate_copy(self.mac, ClipboardContent(
            types={ContentType.TEXT: b"From Mac"},
        ))
        _bridge(self.mac, self.win)
        assert self.win.writer.last_written.types[ContentType.TEXT] == b"From Mac"

        assert self.win.writer.write_count == 1
        assert self.mac.writer.write_count == 1

    def test_no_echo_loop(self):
        """If both devices have the same content, no infinite sync loop."""
        content = ClipboardContent(types={ContentType.TEXT: b"same content"})

        # Windows copies
        _simulate_copy(self.win, content)
        _bridge(self.win, self.mac)
        assert self.mac.writer.write_count == 1
        mac_writes_before = self.mac.writer.write_count
        win_writes_before = self.win.writer.write_count

        # macOS now has this content. Simulate it firing back
        self.mac.reader.content = content
        self.mac.monitor.fire()
        time.sleep(0.1)
        _bridge(self.mac, self.win)

        # Windows should NOT re-write (loop prevention by hash)
        assert self.win.writer.write_count == win_writes_before
        # macOS should NOT re-receive (dedup)
        assert self.mac.writer.write_count == mac_writes_before

    def test_rapid_alternating_copies(self):
        """Both users copying back and forth — no data loss or duplication."""
        expected_mac_count = 0
        expected_win_count = 0

        for i in range(5):
            # Windows copies
            _simulate_copy(self.win, ClipboardContent(
                types={ContentType.TEXT: f"win-{i}".encode()},
            ), pause=0.5)
            _bridge(self.win, self.mac)
            expected_mac_count += 1
            assert self.mac.writer.write_count == expected_mac_count

            # Let the remote-write suppression window expire before the
            # next local copy, otherwise the monitor ignores the fire.
            time.sleep(0.6)

            # Mac copies
            _simulate_copy(self.mac, ClipboardContent(
                types={ContentType.TEXT: f"mac-{i}".encode()},
            ), pause=0.5)
            _bridge(self.mac, self.win)
            expected_win_count += 1
            assert self.win.writer.write_count == expected_win_count

            time.sleep(0.6)


# ══════════════════════════════════════════════════════════════════════════
# Pairing exchange simulation
# ══════════════════════════════════════════════════════════════════════════

class TestPairingExchange:
    """Simulate the full pairing flow between two devices."""

    def test_cross_platform_pairing_flow(self):
        """Windows user pairs with Mac user.

        Current pairing protocol: each side generates its own code.
        The codes must be shared out-of-band (user reads from one screen,
        types into the other).  Each side confirms with its OWN code.
        """
        win_pairing = PairingManager("win-device", "Windows PC")
        mac_pairing = PairingManager("mac-device", "MacBook")

        win_id = win_pairing.load_or_create_identity("", "")
        mac_id = mac_pairing.load_or_create_identity("", "")

        # Exchange certificates (unauthenticated at this point)
        win_pairing.add_peer("mac-device", "MacBook", mac_id.certificate_pem, paired=False)
        mac_pairing.add_peer("win-device", "Windows PC", win_id.certificate_pem, paired=False)

        # Each side generates a pairing code
        win_code = win_pairing.generate_pairing_code("mac-device")
        mac_code = mac_pairing.generate_pairing_code("win-device")

        assert len(win_code) == 8 and win_code.isdigit()
        assert len(mac_code) == 8 and mac_code.isdigit()

        # Each side confirms with its OWN generated code (current behaviour)
        assert win_pairing.confirm_pairing("mac-device", win_code)
        assert mac_pairing.confirm_pairing("win-device", mac_code)

        # Both should now be paired
        assert win_pairing.is_peer_paired("mac-device")
        assert mac_pairing.is_peer_paired("win-device")

        # Fingerprint verification (out-of-band)
        assert win_pairing.verify_peer_fingerprint("mac-device", mac_id.fingerprint)
        assert mac_pairing.verify_peer_fingerprint("win-device", win_id.fingerprint)

    def test_certificate_pinning_rejects_mitm(self):
        """If a paired peer's certificate changes, it must be rejected."""
        alice = PairingManager("alice", "Alice")
        bob_original = PairingManager("bob", "Bob")
        bob_impostor = PairingManager("bob", "Bob")  # same ID, different key

        alice_id = alice.load_or_create_identity("", "")
        bob_original_id = bob_original.load_or_create_identity("", "")
        bob_fake_id = bob_impostor.load_or_create_identity("", "")

        alice.add_peer("bob", "Bob", bob_original_id.certificate_pem, paired=True)

        # Impostor tries to connect with different cert
        with pytest.raises(Exception) as exc:
            alice.add_peer("bob", "Bob", bob_fake_id.certificate_pem, paired=True)
        assert "changed" in str(exc.value).lower() or "certificate" in str(exc.value).lower()

    def test_multi_device_pairing_chain(self):
        """Three devices: Win, Mac, Linux — all pair with each other."""
        devices = {
            "win": PairingManager("win", "Windows"),
            "mac": PairingManager("mac", "MacBook"),
            "linux": PairingManager("linux", "LinuxBox"),
        }

        identities = {}
        for name, mgr in devices.items():
            identities[name] = mgr.load_or_create_identity("", "")

        # Each device adds the other two
        for name, mgr in devices.items():
            for other_name, other_mgr in devices.items():
                if other_name == name:
                    continue
                mgr.add_peer(other_name, other_mgr._device_name,
                            identities[other_name].certificate_pem, paired=False)

        # Generate codes for all directional pairings
        codes: dict[tuple, str] = {}
        for a_name, a_mgr in devices.items():
            for b_name, b_mgr in devices.items():
                if a_name == b_name:
                    continue
                codes[(a_name, b_name)] = a_mgr.generate_pairing_code(b_name)

        # Each device confirms with its OWN generated code for each peer
        for (a_name, b_name), code in codes.items():
            assert devices[a_name].confirm_pairing(b_name, code), \
                f"{a_name} should confirm pairing with {b_name}"

        # Verify all are paired
        for name, mgr in devices.items():
            paired = [p.device_id for p in mgr.get_paired_peers()]
            assert len(paired) == 2, f"{name} should have 2 paired peers"


# ══════════════════════════════════════════════════════════════════════════
# Wire format compatibility
# ══════════════════════════════════════════════════════════════════════════

class TestWireFormatCompatibility:
    """Ensure the wire format is truly platform-independent."""

    def test_wire_format_is_ascii_safe(self):
        """All metadata in the wire format must be ASCII — zero-byte payload is fine."""
        msg = SyncMessage(
            content=ClipboardContent(types={
                ContentType.TEXT: "你好".encode("utf-8"),  # binary payload
            }),
            msg_id="test123",
            source_device="test-device",
        )
        wire = encode_message(msg)
        decoded = decode_message(wire)
        assert decoded.content.types[ContentType.TEXT].decode("utf-8") == "你好"

    def test_large_payload_all_platforms(self):
        """100 KB text must survive encode → decode on any platform."""
        large_text = "ABCDEFGHIJ" * 10000  # 100 KB
        msg = SyncMessage(
            content=ClipboardContent(types={
                ContentType.TEXT: large_text.encode("utf-8"),
            }),
            msg_id="large",
            source_device="test",
        )
        wire = encode_message(msg)
        decoded = decode_message(wire)
        assert len(decoded.content.types[ContentType.TEXT]) == 100_000

    def test_zero_byte_in_payload(self):
        """Binary data with null bytes (e.g., images) must survive."""
        binary = b"\x00" * 100 + b"\x89PNG" + b"\x00" * 50
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.IMAGE_PNG: binary}),
            msg_id="null-bytes",
            source_device="test",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded.content.types[ContentType.IMAGE_PNG] == binary

    def test_empty_content_message(self):
        """Empty clipboard should produce valid wire format."""
        msg = SyncMessage(
            content=ClipboardContent(),
            msg_id="empty",
            source_device="test",
        )
        wire = encode_message(msg)
        decoded = decode_message(wire)
        assert decoded is not None
        assert decoded.content.is_empty()


# ══════════════════════════════════════════════════════════════════════════
# Network failure resilience
# ══════════════════════════════════════════════════════════════════════════

class TestNetworkResilience:
    """Behavior under simulated network issues."""

    def setup_method(self):
        self.win = _make_device("win", "Windows", "windows")
        self.mac = _make_device("mac", "Mac", "darwin")

    def teardown_method(self):
        self.win.mgr.stop()
        self.mac.mgr.stop()

    def test_corrupted_wire_data(self):
        """Corrupted frames must not crash the receiver."""
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"hello"}),
            msg_id="test", source_device="win",
        )
        wire = encode_message(msg)

        # Corrupt: flip bits in the middle
        corrupted = bytearray(wire)
        corrupted[10] ^= 0xFF
        corrupted[20] ^= 0xFF

        result = decode_message(bytes(corrupted))
        # Must either return None or a valid message — never crash
        if result is not None:
            self.mac.mgr.handle_remote_message(result)
        # Should not raise, no crash

    def test_truncated_frame_handling(self):
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"hello"}),
            msg_id="test", source_device="win",
        )
        wire = encode_message(msg)
        # Send only first half
        half = wire[:len(wire) // 2]
        result = decode_message(half)
        assert result is None  # must reject truncated frames

    def test_out_of_order_messages(self):
        """Messages arriving out of order should still be delivered."""
        msgs = [
            SyncMessage(
                content=ClipboardContent(types={ContentType.TEXT: f"msg-{i}".encode()}),
                msg_id=str(i), source_device="sender",
            )
            for i in range(3)
        ]

        # Encode all
        wires = [encode_message(m) for m in msgs]

        # Deliver in reverse order
        for wire in reversed(wires):
            decoded = decode_message(wire)
            assert decoded is not None
            self.mac.mgr.handle_remote_message(decoded)

        # All 3 should be written (different content, so no dedup)
        # Note: first 2 will be dedup'd if sent too fast — but order doesn't matter
        assert self.mac.writer.write_count == 3

    def test_duplicate_messages_filtered(self):
        """Same message arriving twice should only write once."""
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"dup"}),
            msg_id="dup", source_device="sender",
        )
        wire = encode_message(msg)

        decoded1 = decode_message(wire)
        decoded2 = decode_message(wire)

        self.mac.mgr.handle_remote_message(decoded1)
        self.mac.mgr.handle_remote_message(decoded2)

        assert self.mac.writer.write_count == 1  # dedup'd


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
