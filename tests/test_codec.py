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
    encode_frame,
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
        data = encode_message(
            SyncMessage(
                content=ClipboardContent(types={ContentType.TEXT: b"x"}),
                msg_id="t",
                source_device="t",
            )
        )
        # Corrupt magic bytes
        corrupted = bytearray(data)
        corrupted[0] = 0xFF
        corrupted[1] = 0xFF
        assert decode_message(bytes(corrupted)) is None

    def test_wrong_version(self):
        data = encode_message(
            SyncMessage(
                content=ClipboardContent(types={ContentType.TEXT: b"x"}),
                msg_id="t",
                source_device="t",
            )
        )
        corrupted = bytearray(data)
        corrupted[2] = 99  # wrong version
        assert decode_message(bytes(corrupted)) is None

    def test_truncated_frame(self):
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"test data"}),
            msg_id="abc",
            source_device="dev",
        )
        data = encode_message(msg)
        # Truncate at various points
        for cut in range(1, len(data)):
            result = decode_message(data[:cut])
            if result is not None:
                # If decode succeeds, validate it
                assert cut == len(data), (
                    f"Decode should only succeed with full data, got success at cut={cut}"
                )

    def test_invalid_json_payload(self):
        """Manually construct frame with garbage JSON payload."""
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"x"}),
            msg_id="t",
            source_device="t",
        )
        data = encode_message(msg)
        # Corrupt the JSON payload (after header)
        corrupted = bytearray(data)
        # Replace JSON bytes with garbage
        corrupted[HEADER_SIZE + 4 + 1 + 1 + 1 :] = b"not valid json {"
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
        payload = json.dumps(
            {
                "msg_type": "clipboard",
                "types": {"TEXT": base64.b64encode(b"hello").decode("ascii")},
                "timestamp": 1.0,
            }
        ).encode("utf-8")

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
        payload = json.dumps(
            {
                "msg_type": "clipboard",
                "types": {"TEXT": base64.b64encode(b"hello").decode("ascii")},
            }
        ).encode("utf-8")

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
        payload = json.dumps(
            {
                "msg_type": "clipboard",
                "image_fmt": "tiff",
                "types": {"IMAGE_PNG": base64.b64encode(tiff_data).decode("ascii")},
                "timestamp": 0.0,
            }
        ).encode("utf-8")

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
        c1 = ClipboardContent(
            types={
                ContentType.TEXT: b"a",
                ContentType.HTML: b"b",
            }
        )
        c2 = ClipboardContent(
            types={
                ContentType.HTML: b"b",
                ContentType.TEXT: b"a",
            }
        )
        assert c1.hash_key() == c2.hash_key()

    def test_is_empty(self):
        assert ClipboardContent().is_empty()
        assert not ClipboardContent(types={ContentType.TEXT: b"x"}).is_empty()

    def test_best_format_priority(self):
        """HTML > RTF > TEXT > IMAGE_PNG"""
        c = ClipboardContent(
            types={
                ContentType.IMAGE_PNG: b"png",
                ContentType.TEXT: b"text",
                ContentType.HTML: b"html",
                ContentType.RTF: b"rtf",
            }
        )
        fmt, data = c.best_format()
        assert fmt == ContentType.HTML
        assert data == b"html"

    def test_best_format_fallback(self):
        c = ClipboardContent(types={ContentType.IMAGE_PNG: b"png"})
        fmt, data = c.best_format()
        assert fmt == ContentType.IMAGE_PNG


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# merged from test_format.py
# ══════════════════════════════════════════════════

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestClipboardContentDefaults:
    """Default values for ClipboardContent fields."""

    def test_default_types_empty_dict(self):
        c = ClipboardContent()
        assert c.types == {}
        assert isinstance(c.types, dict)

    def test_default_source_device_empty_string(self):
        c = ClipboardContent()
        assert c.source_device == ""

    def test_default_timestamp_zero(self):
        c = ClipboardContent()
        assert c.timestamp == 0.0


class TestHashKey:
    """hash_key() produces a content-based dedup key."""

    def test_deterministic_same_content_produces_same_hash(self):
        c1 = ClipboardContent(types={ContentType.TEXT: b"hello"})
        c2 = ClipboardContent(types={ContentType.TEXT: b"hello"})
        assert c1.hash_key() == c2.hash_key()

    def test_different_content_produces_different_hash(self):
        c1 = ClipboardContent(types={ContentType.TEXT: b"hello"})
        c2 = ClipboardContent(types={ContentType.TEXT: b"world"})
        assert c1.hash_key() != c2.hash_key()

    def test_order_independent(self):
        """Hash is the same regardless of the order types were inserted."""
        c1 = ClipboardContent(
            types={
                ContentType.TEXT: b"a",
                ContentType.HTML: b"b",
            }
        )
        c2 = ClipboardContent(
            types={
                ContentType.HTML: b"b",
                ContentType.TEXT: b"a",
            }
        )
        assert c1.hash_key() == c2.hash_key()

    def test_returns_string(self):
        c = ClipboardContent(types={ContentType.TEXT: b"data"})
        assert isinstance(c.hash_key(), str)

    def test_includes_all_format_types(self):
        """Hash changes when an additional format type is present."""
        c1 = ClipboardContent(types={ContentType.TEXT: b"same"})
        c2 = ClipboardContent(
            types={
                ContentType.TEXT: b"same",
                ContentType.HTML: b"other",
            }
        )
        assert c1.hash_key() != c2.hash_key()

    def test_large_content_consistent(self):
        """1 MB of data produces a consistent, repeatable hash."""
        large = b"x" * (1024 * 1024)
        c1 = ClipboardContent(types={ContentType.TEXT: large})
        c2 = ClipboardContent(types={ContentType.TEXT: large})
        assert c1.hash_key() == c2.hash_key()

    def test_hash_ignores_metadata(self):
        """Hash depends only on types, not source_device or timestamp."""
        c1 = ClipboardContent(
            types={ContentType.TEXT: b"data"},
            source_device="dev-a",
            timestamp=999.0,
        )
        c2 = ClipboardContent(
            types={ContentType.TEXT: b"data"},
            source_device="dev-b",
            timestamp=0.0,
        )
        assert c1.hash_key() == c2.hash_key()


class TestIsEmpty:
    """is_empty() reports whether any content is present."""

    def test_empty_returns_true(self):
        assert ClipboardContent().is_empty()

    def test_single_type_returns_false(self):
        assert not ClipboardContent(types={ContentType.TEXT: b"x"}).is_empty()

    def test_multiple_types_returns_false(self):
        c = ClipboardContent(
            types={
                ContentType.TEXT: b"x",
                ContentType.HTML: b"y",
            }
        )
        assert not c.is_empty()

    def test_only_metadata_returns_true(self):
        """source_device and timestamp alone do not count as content."""
        c = ClipboardContent(source_device="dev", timestamp=1.0)
        assert c.is_empty()


class TestBestFormat:
    """best_format() returns the highest-priority available format."""

    def test_priority_html_top(self):
        """HTML > IMAGE_PNG > RTF > TEXT"""
        c = ClipboardContent(
            types={
                ContentType.IMAGE_PNG: b"png",
                ContentType.TEXT: b"text",
                ContentType.HTML: b"html",
                ContentType.RTF: b"rtf",
            }
        )
        fmt, data = c.best_format()
        assert fmt == ContentType.HTML
        assert data == b"html"

    def test_returns_none_for_empty(self):
        assert ClipboardContent().best_format() is None

    def test_fallback_to_image_png(self):
        """TEXT/RTF rank above IMAGE_PNG (editable formats preferred)."""
        c = ClipboardContent(
            types={
                ContentType.IMAGE_PNG: b"png",
                ContentType.TEXT: b"text",
            }
        )
        fmt, data = c.best_format()
        assert fmt == ContentType.TEXT
        assert data == b"text"

    def test_rtf_over_image(self):
        """RTF ranks above IMAGE_PNG."""
        c = ClipboardContent(
            types={
                ContentType.IMAGE_PNG: b"png",
                ContentType.RTF: b"rtf",
            }
        )
        fmt, data = c.best_format()
        assert fmt == ContentType.RTF
        assert data == b"rtf"

    def test_fallback_to_rtf(self):
        """RTF over TEXT."""
        c = ClipboardContent(
            types={
                ContentType.RTF: b"rtf",
                ContentType.TEXT: b"text",
            }
        )
        fmt, data = c.best_format()
        assert fmt == ContentType.RTF
        assert data == b"rtf"

    def test_fallback_to_text(self):
        """TEXT is the last resort."""
        c = ClipboardContent(types={ContentType.TEXT: b"plain"})
        fmt, data = c.best_format()
        assert fmt == ContentType.TEXT
        assert data == b"plain"

    def test_image_only_returns_image(self):
        c = ClipboardContent(types={ContentType.IMAGE_PNG: b"png"})
        fmt, data = c.best_format()
        assert fmt == ContentType.IMAGE_PNG


class TestSyncMessageDefaults:
    """Default values and field assignment for SyncMessage."""

    def test_default_msg_id_is_empty_string(self):
        msg = SyncMessage(content=ClipboardContent())
        assert msg.msg_id == ""

    def test_default_source_device_is_empty_string(self):
        msg = SyncMessage(content=ClipboardContent())
        assert msg.source_device == ""

    def test_all_fields_can_be_set(self):
        content = ClipboardContent(types={ContentType.TEXT: b"data"})
        msg = SyncMessage(content=content, msg_id="abc123", source_device="my-device")
        assert msg.content is content
        assert msg.msg_id == "abc123"
        assert msg.source_device == "my-device"

    def test_repr_does_not_raise(self):
        """repr() should not fail on a SyncMessage."""
        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"test"}),
            msg_id="m1",
            source_device="d1",
        )
        rep = repr(msg)
        assert "SyncMessage" in rep


class TestContentTypeEnum:
    """ContentType enum values."""

    def test_values_are_distinct(self):
        assert ContentType.TEXT.value == 1
        assert ContentType.HTML.value == 2
        assert ContentType.RTF.value == 3
        assert ContentType.IMAGE_PNG.value == 4

    def test_no_duplicate_values(self):
        values = [m.value for m in ContentType]
        assert len(values) == len(set(values))

    def test_lookup_by_value(self):
        assert ContentType(1) == ContentType.TEXT
        assert ContentType(2) == ContentType.HTML
        assert ContentType(3) == ContentType.RTF
        assert ContentType(4) == ContentType.IMAGE_PNG


class TestImageFmt:
    """image_fmt field on ClipboardContent."""

    def test_default_is_empty_string(self):
        content = ClipboardContent()
        assert content.image_fmt == ""

    def test_set_png(self):
        content = ClipboardContent(image_fmt="png")
        assert content.image_fmt == "png"

    def test_set_tiff(self):
        content = ClipboardContent(image_fmt="tiff")
        assert content.image_fmt == "tiff"

    def test_set_bmp(self):
        content = ClipboardContent(image_fmt="bmp")
        assert content.image_fmt == "bmp"

    def test_hash_key_ignores_image_fmt(self):
        c1 = ClipboardContent(types={ContentType.IMAGE_PNG: b"data"}, image_fmt="png")
        c2 = ClipboardContent(types={ContentType.IMAGE_PNG: b"data"}, image_fmt="bmp")
        assert c1.hash_key() == c2.hash_key()

    def test_is_empty_ignores_image_fmt(self):
        content = ClipboardContent(image_fmt="tiff")
        assert content.is_empty()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# split from test_round3_core.py — codec malformed / JSON tolerance
# ══════════════════════════════════════════════════


class TestCodecMalformedPayloads:
    def _frame(self, payload: dict) -> bytes:
        return encode_frame(payload, msg_id="deadbeef", source_device="peer-a")

    def test_non_string_msg_type_is_dropped(self):
        # A dict/list msg_type is unhashable: routers do ``msg_type in <set>``
        # which raises TypeError -- inside the recv loop's catch-all that used
        # to tear down the whole connection.  It must be dropped as a bad
        # frame instead.
        for bad in ({}, [1, 2], {"x": 1}):
            data = self._frame({"msg_type": bad, "types": {}})
            assert decode_message(data) is None, f"msg_type={bad!r}"

    def test_non_numeric_timestamp_coerced_to_zero(self):
        for bad in ("oops", None, [1], float("nan"), float("inf")):
            data = self._frame(
                {
                    "msg_type": "clipboard",
                    "types": {"TEXT": base64.b64encode(b"hi").decode()},
                    "timestamp": bad,
                }
            )
            msg = decode_message(data)
            assert msg is not None, f"timestamp={bad!r}"
            assert msg.content.timestamp == 0.0

    def test_bool_timestamp_coerced_to_zero(self):
        data = self._frame({"msg_type": "clipboard", "types": {}, "timestamp": True})
        msg = decode_message(data)
        assert msg is not None and msg.content.timestamp == 0.0

    def test_non_string_image_fmt_tolerated(self):
        data = self._frame(
            {
                "msg_type": "clipboard",
                "types": {},
                "image_fmt": 42,
            }
        )
        msg = decode_message(data)
        assert msg is not None and msg.content.image_fmt == ""

    def test_valid_clipboard_still_decodes(self):
        msg = SyncMessage(
            content=ClipboardContent(
                timestamp=1234.5,
                types={ContentType.TEXT: b"hello"},
            ),
            msg_id="abc123",
            source_device="dev-x",
        )
        decoded = decode_message(encode_message(msg))
        assert decoded is not None
        assert decoded.msg_type == "clipboard"
        assert decoded.content.types[ContentType.TEXT] == b"hello"
        assert decoded.content.timestamp == 1234.5
        assert decoded.source_device == "dev-x"


class TestCodecJsonTolerance:
    def test_payload_with_unexpected_extra_fields_decodes(self):
        data = encode_frame(
            {
                "msg_type": "clipboard",
                "types": {"TEXT": base64.b64encode(b"x").decode()},
                "unknown_future_field": {"nested": [1, 2, 3]},
            }
        )
        msg = decode_message(data)
        assert msg is not None
        assert msg.content.types[ContentType.TEXT] == b"x"
        assert msg._raw_payload["unknown_future_field"] == {"nested": [1, 2, 3]}

    def test_json_array_payload_dropped_not_crash(self):
        data = encode_frame([1, 2, 3])
        assert decode_message(data) is None
