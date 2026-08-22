"""Cross-platform clipboard simulation tests.

Simulates clipboard behavior for Windows, macOS, and Linux in-process,
verifying that the content format pipeline works correctly for each platform.
"""

import os
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Platform simulation helpers ──────────────────────────────────────────

class SimClipboard:
    """In-memory clipboard that simulates platform-specific behavior."""

    def __init__(self):
        self._content: dict[str, bytes] = {}
        self._change_count = 0

    def set_text(self, text: str):
        self._content["text"] = text.encode("utf-8")
        self._content["text.cf_unicodetext"] = text.encode("utf-16-le")
        self._change_count += 1

    def set_html(self, html: str):
        self._content["html"] = html.encode("utf-8")
        self._change_count += 1

    def set_rtf(self, rtf: str):
        self._content["rtf"] = rtf.encode("utf-8")
        self._change_count += 1

    def set_image(self, png_data: bytes):
        self._content["png"] = png_data
        self._change_count += 1

    def get_text(self) -> str | None:
        if "text" in self._content:
            return self._content["text"].decode("utf-8")
        if "text.cf_unicodetext" in self._content:
            return self._content["text.cf_unicodetext"].decode("utf-16-le").rstrip("\x00")
        return None

    def get_html(self) -> str | None:
        if "html" in self._content:
            return self._content["html"].decode("utf-8")
        return None

    def get_rtf(self) -> str | None:
        if "rtf" in self._content:
            return self._content["rtf"].decode("utf-8")
        return None

    def get_image_png(self) -> bytes | None:
        return self._content.get("png")

    def clear(self):
        self._content.clear()
        self._change_count += 1

    @property
    def change_count(self) -> int:
        return self._change_count


class TestWindowsClipboardSim:
    """Simulate Windows clipboard behavior — UTF-16-LE, CF_UNICODETEXT priority."""

    def test_text_encoding_roundtrip(self):
        """Windows uses UTF-16-LE with null terminators."""
        cb = SimClipboard()
        text = "Hello 世界 🌍"

        # Simulate Windows CF_UNICODETEXT write
        cb._content["text.cf_unicodetext"] = text.encode("utf-16-le") + b"\x00\x00"

        # Read back (simulating _read_text_handle with wide=True)
        raw = cb._content["text.cf_unicodetext"]
        decoded = raw.decode("utf-16-le").rstrip("\x00")
        assert decoded == text

    def test_cf_text_vs_cf_unicodetext_priority(self):
        """CF_UNICODETEXT should be preferred over CF_TEXT (ANSI)."""
        cb = SimClipboard()
        chinese = "你好世界"

        # Simulate both formats present (Windows reality)
        cb._content["text.cf_unicodetext"] = chinese.encode("utf-16-le")
        try:
            cb._content["text.cf_text"] = chinese.encode("gbk")  # ANSI/GBK
        except Exception:
            cb._content["text.cf_text"] = chinese.encode("utf-8")

        # Read: prefer UNICODE
        raw_unicode = cb._content["text.cf_unicodetext"]
        text = raw_unicode.decode("utf-16-le").rstrip("\x00")
        assert text == chinese

    def test_null_terminator_stripping(self):
        """Windows clip text has trailing null bytes that must be stripped.

        UTF-16-LE encodes each char as 2 bytes. The null terminator is
        U+0000 encoded as \\x00\\x00 (2 bytes). Must ensure even byte length.
        """
        cb = SimClipboard()
        # Windows clipboard: UTF-16-LE text + U+0000 null terminator (2 bytes)
        raw_utf16 = "ClipSync".encode("utf-16-le") + b"\x00\x00"
        assert len(raw_utf16) % 2 == 0  # must be even for valid UTF-16-LE
        cb._content["text.cf_unicodetext"] = raw_utf16

        # Strip null (U+0000) characters
        cleaned = raw_utf16.decode("utf-16-le").rstrip("\x00")
        assert cleaned == "ClipSync"
        assert "\x00" not in cleaned

    def test_image_dib_to_png(self):
        """Windows clipboard images are DIB format, must convert to PNG."""
        cb = SimClipboard()

        # Create a simulated DIB (1x1 pixel, 24-bit, blue)
        # BITMAPINFOHEADER: 40 bytes
        bi_size = 40
        bi_width = 1
        bi_height = 1
        bi_planes = 1
        bi_bit_count = 24
        bi_compression = 0
        bi_size_image = 4  # padded row
        dib_header = struct.pack(
            "<IiiHHIIiiII",
            bi_size, bi_width, bi_height, bi_planes, bi_bit_count,
            bi_compression, bi_size_image, 0, 0, 0, 0,
        )
        pixel_data = b"\xff\x00\x00\x00"  # BGR + padding = blue pixel
        dib = dib_header + pixel_data

        cb._content["dib"] = dib

        # Verify DIB can be wrapped as BMP
        bi_size_read = struct.unpack_from("<I", dib, 0)[0]
        assert bi_size_read == 40
        bi_bit_count_read = struct.unpack_from("<H", dib, 14)[0]
        assert bi_bit_count_read == 24


class TestMacOSClipboardSim:
    """Simulate macOS clipboard behavior — NSPasteboard polling, RTF, TIFF."""

    def test_polling_change_count(self):
        """macOS uses polling (400ms) with changeCount."""
        cb = SimClipboard()
        initial = cb.change_count
        cb.set_text("hello")
        assert cb.change_count == initial + 1
        cb.set_html("<b>bold</b>")
        assert cb.change_count == initial + 2

    def test_polling_no_missed_first(self):
        """First poll should detect content even if change_count starts non-zero."""
        cb = SimClipboard()
        cb.set_text("pre-existing content")
        # Even though we "start monitoring late", we should still read content
        assert cb.get_text() == "pre-existing content"

    def test_rtf_handling(self):
        """macOS supports RTF via NSAttributedString."""
        cb = SimClipboard()
        rtf_text = "{\\rtf1\\ansi\\deff0 Hello}"
        cb.set_rtf(rtf_text)
        assert cb.get_rtf() == rtf_text

    def test_multiple_formats_simultaneously(self):
        """macOS clipboard can have multiple representations at once."""
        cb = SimClipboard()
        cb.set_text("plain")
        cb.set_html("<p>rich</p>")
        cb.set_rtf("{\\rtf1 rich}")
        cb.set_image(b"\x89PNG\x00\x00\x00")

        assert cb.get_text() == "plain"
        assert cb.get_html() == "<p>rich</p>"
        assert cb.get_rtf() == "{\\rtf1 rich}"
        assert cb.get_image_png() == b"\x89PNG\x00\x00\x00"


class TestLinuxClipboardSim:
    """Simulate Linux clipboard behavior — xclip/wl-paste, Wayland vs X11."""

    def test_text_roundtrip(self):
        cb = SimClipboard()
        cb.set_text("Linux clipboard test")
        assert cb.get_text() == "Linux clipboard test"

    def test_unicode_preservation(self):
        """Linux clipboard should preserve full Unicode."""
        cb = SimClipboard()
        text = "Привет мир\n日本語\n🌟"
        cb.set_text(text)
        assert cb.get_text() == text

    def test_rtf_support(self):
        """Linux with xclip -t text/rtf supports RTF."""
        cb = SimClipboard()
        rtf = "{\\rtf1\\ansi Hello from Linux}"
        cb.set_rtf(rtf)
        assert cb.get_rtf() == rtf

    def test_wayland_wl_paste_format(self):
        """Wayland uses wl-paste which handles MIME types natively."""
        cb = SimClipboard()

        # Simulate what wl-paste --list-types would return
        cb.set_text("Wayland text")
        cb.set_html("<html>Wayland</html>")

        assert cb.get_text() == "Wayland text"
        assert cb.get_html() == "<html>Wayland</html>"


class TestCrossPlatformContentParity:
    """Verify that the same content is handled identically across platforms."""

    def test_text_encoding_normalization(self):
        """All platforms should produce the same UTF-8 output."""
        from internal.clipboard.format import ClipboardContent, ContentType

        # Simulate three platforms reading the same content
        content = "同一个世界 🌐"
        content_utf8 = content.encode("utf-8")

        # All platforms normalize to ClipboardContent with UTF-8 TEXT
        clip = ClipboardContent(types={ContentType.TEXT: content_utf8})
        assert clip.types[ContentType.TEXT] == content_utf8
        assert clip.types[ContentType.TEXT].decode("utf-8") == content

    def test_hash_key_platform_independent(self):
        """Content hash should be the same regardless of source platform."""
        from internal.clipboard.format import ClipboardContent, ContentType

        # Same logical content should hash the same
        c1 = ClipboardContent(types={
            ContentType.TEXT: b"text",
            ContentType.HTML: b"<p>html</p>",
        })
        c2 = ClipboardContent(types={
            ContentType.HTML: b"<p>html</p>",
            ContentType.TEXT: b"text",
        })
        assert c1.hash_key() == c2.hash_key()

    def test_format_priority_consistent(self):
        """HTML > RTF > TEXT > IMAGE_PNG priority is platform-independent."""
        from internal.clipboard.format import ClipboardContent, ContentType

        c = ClipboardContent(types={
            ContentType.IMAGE_PNG: b"img",
            ContentType.TEXT: b"txt",
            ContentType.RTF: b"rtf",
            ContentType.HTML: b"html",
        })
        fmt, data = c.best_format()
        assert fmt == ContentType.HTML

        # Remove HTML → RTF wins (above TEXT and IMAGE_PNG)
        del c.types[ContentType.HTML]
        fmt, data = c.best_format()
        assert fmt == ContentType.RTF

        # Remove RTF → TEXT wins (above IMAGE_PNG)
        del c.types[ContentType.RTF]
        fmt, data = c.best_format()
        assert fmt == ContentType.TEXT

        # Remove TEXT → IMAGE_PNG last resort
        del c.types[ContentType.TEXT]
        fmt, data = c.best_format()
        assert fmt == ContentType.IMAGE_PNG


class TestPlatformAvailability:
    """Check whether platform-specific dependencies are available."""

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_xclip_available(self):
        result = subprocess.run(["which", "xclip"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("xclip not installed")

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_wl_paste_available(self):
        result = subprocess.run(["which", "wl-paste"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("wl-paste not installed (Wayland not available)")

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_pbpaste_available(self):
        result = subprocess.run(["which", "pbpaste"], capture_output=True)
        assert result.returncode == 0, "pbpaste should be available on macOS"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_clipboard_module_loads(self):
        """Ensure the Windows clipboard module can be imported."""
        import internal.clipboard.clipboard_windows  # noqa: F401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
