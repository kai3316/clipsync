"""Tests for protocol codec — encode/decode roundtrip and edge cases."""

import base64
import json
import os
import struct
import sys
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import (
    ClipboardContent,
    ContentType,
    SyncMessage,
)
from internal.protocol.codec import (
    HEADER_SIZE,
    MAGIC,
    VERSION,
    decode_message,
    encode_message,
)


class TestEncodeDecode:
    """Roundtrip tests for the binary protocol."""

    def test_roundtrip_text_only(self):
        msg = SyncMessage(
            content=ClipboardContent(
                types={ContentType.TEXT: b"Hello World"},
                timestamp=1234567890.0,
            ),
            msg_id="abc123",
            source_device="test-device",
        )
        data = encode_message(msg)
        decoded = decode_message(data)
        assert decoded is not None
        assert decoded.msg_id == "abc123"
        assert decoded.source_device == "test-device"
        assert ContentType.TEXT in decoded.content.types
        assert decoded.content.types[ContentType.TEXT] == b"Hello World"
        assert decoded.content.timestamp == 1234567890.0

    def test_roundtrip_html(self):
        msg = SyncMessage(
            content=ClipboardContent(
                types={
                    ContentType.HTML: b"<b>Bold</b>",
                    ContentType.TEXT: b"Bold",
                },
                timestamp=0.0,
            ),
            msg_id="html-test",
            source_device="mac",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.types[ContentType.HTML] == b"<b>Bold</b>"
        assert decoded.content.types[ContentType.TEXT] == b"Bold"

    def test_roundtrip_image(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # minimal PNG-like data
        msg = SyncMessage(
            content=ClipboardContent(
                types={ContentType.IMAGE_PNG: png},
            ),
            msg_id="img-test",
            source_device="linux",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.types[ContentType.IMAGE_PNG] == png

    def test_roundtrip_rtf(self):
        rtf = rb"{\rtf1\ansi Hello}"
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.RTF: rtf}),
            msg_id="rtf-test",
            source_device="win",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.types[ContentType.RTF] == rtf

    def test_roundtrip_multi_format(self):
        """All four formats in one message."""
        msg = SyncMessage(
            content=ClipboardContent(
                types={
                    ContentType.TEXT: b"text",
                    ContentType.HTML: b"<p>html</p>",
                    ContentType.RTF: b"{\\rtf1 rtf}",
                    ContentType.IMAGE_PNG: b"\x89PNG\x00",
                },
            ),
            msg_id="multi",
            source_device="test",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert len(decoded.content.types) == 4
        assert decoded.content.types[ContentType.TEXT] == b"text"
        assert decoded.content.types[ContentType.HTML] == b"<p>html</p>"
        assert decoded.content.types[ContentType.RTF] == b"{\\rtf1 rtf}"
        assert decoded.content.types[ContentType.IMAGE_PNG] == b"\x89PNG\x00"

    def test_unicode_text(self):
        """Chinese, emoji, and special characters should survive roundtrip."""
        msg = SyncMessage(
            content=ClipboardContent(
                types={ContentType.TEXT: "你好世界 🌍 émoji test".encode()},
            ),
            msg_id="unicode",
            source_device="test",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.types[ContentType.TEXT].decode("utf-8") == "你好世界 🌍 émoji test"

    def test_device_name_truncation(self):
        """Very long device names should be truncated to fit 1-byte length field."""
        long_name = "a" * 300  # longer than 255 UTF-8 bytes
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"x"}),
            msg_id="trunc",
            source_device=long_name,
        )
        data = encode_message(msg)
        decoded = decode_message(data)
        assert decoded is not None
        # Should be truncated to fit
        assert len(decoded.source_device.encode("utf-8")) <= 255


class TestDecodeErrors:
    """Edge cases that should return None from decode_message."""

    def test_empty_data(self):
        assert decode_message(b"") is None

    def test_too_short(self):
        assert decode_message(b"\x00\x00") is None

    def test_wrong_magic(self):
        data = encode_message(SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"x"}),
            msg_id="t", source_device="t",
        ))
        # Corrupt magic bytes
        corrupted = bytearray(data)
        corrupted[0] = 0xFF
        corrupted[1] = 0xFF
        assert decode_message(bytes(corrupted)) is None

    def test_wrong_version(self):
        data = encode_message(SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"x"}),
            msg_id="t", source_device="t",
        ))
        corrupted = bytearray(data)
        corrupted[2] = 99  # wrong version
        assert decode_message(bytes(corrupted)) is None

    def test_truncated_frame(self):
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"test data"}),
            msg_id="abc", source_device="dev",
        )
        data = encode_message(msg)
        # Truncate at various points
        for cut in range(1, len(data)):
            result = decode_message(data[:cut])
            if result is not None:
                # If decode succeeds, validate it
                assert cut == len(data), f"Decode should only succeed with full data, got success at cut={cut}"

    def test_invalid_json_payload(self):
        """Manually construct frame with garbage JSON payload."""
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"x"}),
            msg_id="t", source_device="t",
        )
        data = encode_message(msg)
        # Corrupt the JSON payload (after header)
        corrupted = bytearray(data)
        # Replace JSON bytes with garbage
        corrupted[HEADER_SIZE + 4 + 1 + 1 + 1:] = b"not valid json {"
        assert decode_message(bytes(corrupted)) is None

    def test_invalid_base64_in_payload(self):
        """JSON is valid but base64 data is corrupt — should skip gracefully."""
        import struct
        bad_json = json.dumps({"types": {"TEXT": "!!!not-base64!!!"}, "timestamp": 0})
        payload = bad_json.encode("utf-8")
        msg_id = b"abc"
        src = b"dev"
        buf = bytearray()
        buf.extend(struct.pack(">H B I", MAGIC, VERSION, len(payload)))
        buf.extend(struct.pack(">I", len(msg_id)))
        buf.extend(msg_id)
        buf.extend(struct.pack(">B", len(src)))
        buf.extend(src)
        buf.extend(payload)
        # Invalid base64 is skipped; content has no types
        result = decode_message(bytes(buf))
        assert result is not None
        assert result.msg_id == "abc"
        assert result.source_device == "dev"
        assert len(result.content.types) == 0  # bad base64 skipped


class TestProtocolVersionCompat:
    """Version 2 accepts v1 frames; version >2 is rejected."""

    def test_decode_version_1_frame(self):
        """VERSION=2 decoders must accept legacy v1 frames."""
        msg_id = b"v1test"
        src = b"dev"
        payload = json.dumps({
            "msg_type": "clipboard",
            "types": {"TEXT": base64.b64encode(b"hello").decode("ascii")},
            "timestamp": 1.0,
        }).encode("utf-8")

        buf = BytesIO()
        buf.write(struct.pack(">H B I", MAGIC, 1, len(payload)))  # version 1
        buf.write(struct.pack(">I", len(msg_id)))
        buf.write(msg_id)
        buf.write(struct.pack(">B", len(src)))
        buf.write(src)
        buf.write(payload)

        result = decode_message(buf.getvalue())
        assert result is not None
        assert result.msg_id == "v1test"
        assert result.content.types[ContentType.TEXT] == b"hello"

    def test_decode_rejects_version_3(self):
        """Versions beyond VERSION=2 must be rejected."""
        import struct
        msg_id = b"v3test"
        src = b"dev"
        payload = json.dumps({
            "msg_type": "clipboard",
            "types": {"TEXT": base64.b64encode(b"hello").decode("ascii")},
        }).encode("utf-8")

        buf = BytesIO()
        buf.write(struct.pack(">H B I", MAGIC, 3, len(payload)))  # version 3
        buf.write(struct.pack(">I", len(msg_id)))
        buf.write(msg_id)
        buf.write(struct.pack(">B", len(src)))
        buf.write(src)
        buf.write(payload)

        assert decode_message(buf.getvalue()) is None


class TestImageFmtCodec:
    """image_fmt roundtrip through encode/decode."""

    def test_roundtrip_with_image_fmt(self):
        content = ClipboardContent(
            types={ContentType.IMAGE_PNG: b"\x89PNG\r\n\x1a\n" + b"\x00" * 50},
            image_fmt="tiff",
        )
        msg = SyncMessage(content=content, msg_id="img-fmt", source_device="mac")
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.image_fmt == "tiff"
        assert decoded.content.types[ContentType.IMAGE_PNG] == content.types[ContentType.IMAGE_PNG]

    def test_legacy_no_image_fmt(self):
        """Old payloads without image_fmt default to ''."""
        content = ClipboardContent(types={ContentType.IMAGE_PNG: b"pngdata"})
        msg = SyncMessage(content=content, msg_id="legacy", source_device="old")
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.image_fmt == ""

    def test_image_fmt_empty_not_in_json(self):
        """When image_fmt is empty, the key should not appear in JSON."""
        content = ClipboardContent(
            types={ContentType.IMAGE_PNG: b"\x89PNG\r\n\x1a\n" + b"\x00" * 10},
            image_fmt="",
        )
        msg = SyncMessage(content=content, msg_id="no-fmt", source_device="a")
        wire = encode_message(msg)
        assert b'"image_fmt"' not in wire

    def test_zlib_compression_roundtrip(self):
        """Non-PNG image formats are zlib compressed on wire."""
        # Simulate a BMP image payload (image_fmt="bmp" triggers zlib)
        bmp_data = b"BM" + b"\x00" * 500  # fake BMP
        content = ClipboardContent(
            types={ContentType.IMAGE_PNG: bmp_data},
            image_fmt="bmp",
        )
        msg = SyncMessage(content=content, msg_id="zlib", source_device="win")
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.content.image_fmt == "bmp"
        assert decoded.content.types[ContentType.IMAGE_PNG] == bmp_data

    def test_legacy_uncompressed_tiff_tolerated(self):
        """Legacy uncompressed data (bad zlib) is passed through."""
        import struct
        tiff_data = b"II" + b"\x00" * 100  # fake little-endian TIFF header
        payload = json.dumps({
            "msg_type": "clipboard",
            "image_fmt": "tiff",
            "types": {"IMAGE_PNG": base64.b64encode(tiff_data).decode("ascii")},
            "timestamp": 0.0,
        }).encode("utf-8")

        msg_id = b"legacy"
        src = b"dev"
        buf = BytesIO()
        buf.write(struct.pack(">H B I", MAGIC, VERSION, len(payload)))
        buf.write(struct.pack(">I", len(msg_id)))
        buf.write(msg_id)
        buf.write(struct.pack(">B", len(src)))
        buf.write(src)
        buf.write(payload)

        result = decode_message(buf.getvalue())
        assert result is not None
        assert result.content.types[ContentType.IMAGE_PNG] == tiff_data


class TestClipboardContent:
    def test_hash_key_deterministic(self):
        c1 = ClipboardContent(types={ContentType.TEXT: b"hello"})
        c2 = ClipboardContent(types={ContentType.TEXT: b"hello"})
        assert c1.hash_key() == c2.hash_key()

    def test_hash_key_differs(self):
        c1 = ClipboardContent(types={ContentType.TEXT: b"hello"})
        c2 = ClipboardContent(types={ContentType.TEXT: b"world"})
        assert c1.hash_key() != c2.hash_key()

    def test_hash_order_independent(self):
        """Hash should be the same regardless of insert order."""
        c1 = ClipboardContent(types={
            ContentType.TEXT: b"a", ContentType.HTML: b"b",
        })
        c2 = ClipboardContent(types={
            ContentType.HTML: b"b", ContentType.TEXT: b"a",
        })
        assert c1.hash_key() == c2.hash_key()

    def test_is_empty(self):
        assert ClipboardContent().is_empty()
        assert not ClipboardContent(types={ContentType.TEXT: b"x"}).is_empty()

    def test_best_format_priority(self):
        """HTML > RTF > TEXT > IMAGE_PNG"""
        c = ClipboardContent(types={
            ContentType.IMAGE_PNG: b"png",
            ContentType.TEXT: b"text",
            ContentType.HTML: b"html",
            ContentType.RTF: b"rtf",
        })
        fmt, data = c.best_format()
        assert fmt == ContentType.HTML
        assert data == b"html"

    def test_best_format_fallback(self):
        c = ClipboardContent(types={ContentType.IMAGE_PNG: b"png"})
        fmt, data = c.best_format()
        assert fmt == ContentType.IMAGE_PNG


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
