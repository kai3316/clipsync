"""Tests for clipboard content format — ClipboardContent, SyncMessage, ContentType."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage


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
        c1 = ClipboardContent(types={
            ContentType.TEXT: b"a", ContentType.HTML: b"b",
        })
        c2 = ClipboardContent(types={
            ContentType.HTML: b"b", ContentType.TEXT: b"a",
        })
        assert c1.hash_key() == c2.hash_key()

    def test_returns_string(self):
        c = ClipboardContent(types={ContentType.TEXT: b"data"})
        assert isinstance(c.hash_key(), str)

    def test_includes_all_format_types(self):
        """Hash changes when an additional format type is present."""
        c1 = ClipboardContent(types={ContentType.TEXT: b"same"})
        c2 = ClipboardContent(types={
            ContentType.TEXT: b"same",
            ContentType.HTML: b"other",
        })
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
        c = ClipboardContent(types={
            ContentType.TEXT: b"x",
            ContentType.HTML: b"y",
        })
        assert not c.is_empty()

    def test_only_metadata_returns_true(self):
        """source_device and timestamp alone do not count as content."""
        c = ClipboardContent(source_device="dev", timestamp=1.0)
        assert c.is_empty()


class TestBestFormat:
    """best_format() returns the highest-priority available format."""

    def test_priority_html_top(self):
        """HTML > IMAGE_PNG > RTF > TEXT"""
        c = ClipboardContent(types={
            ContentType.IMAGE_PNG: b"png",
            ContentType.TEXT: b"text",
            ContentType.HTML: b"html",
            ContentType.RTF: b"rtf",
        })
        fmt, data = c.best_format()
        assert fmt == ContentType.HTML
        assert data == b"html"

    def test_returns_none_for_empty(self):
        assert ClipboardContent().best_format() is None

    def test_fallback_to_image_png(self):
        """TEXT/RTF rank above IMAGE_PNG (editable formats preferred)."""
        c = ClipboardContent(types={
            ContentType.IMAGE_PNG: b"png",
            ContentType.TEXT: b"text",
        })
        fmt, data = c.best_format()
        assert fmt == ContentType.TEXT
        assert data == b"text"

    def test_rtf_over_image(self):
        """RTF ranks above IMAGE_PNG."""
        c = ClipboardContent(types={
            ContentType.IMAGE_PNG: b"png",
            ContentType.RTF: b"rtf",
        })
        fmt, data = c.best_format()
        assert fmt == ContentType.RTF
        assert data == b"rtf"

    def test_fallback_to_rtf(self):
        """RTF over TEXT."""
        c = ClipboardContent(types={
            ContentType.RTF: b"rtf",
            ContentType.TEXT: b"text",
        })
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
        c1 = ClipboardContent(types={ContentType.IMAGE_PNG: b"data"},
                              image_fmt="png")
        c2 = ClipboardContent(types={ContentType.IMAGE_PNG: b"data"},
                              image_fmt="bmp")
        assert c1.hash_key() == c2.hash_key()

    def test_is_empty_ignores_image_fmt(self):
        content = ClipboardContent(image_fmt="tiff")
        assert content.is_empty()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
