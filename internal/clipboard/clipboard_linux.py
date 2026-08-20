"""Linux clipboard implementation.

Uses xclip (X11) or wl-paste/wl-copy (Wayland) for clipboard access.
Auto-detects the display server in use.
"""

import hashlib
import logging
import os
import subprocess
import threading
import time
from io import BytesIO

from internal.clipboard.clipboard import ClipboardMonitor, ClipboardReader, ClipboardWriter
from internal.clipboard.format import ClipboardContent, ContentType

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.4


def _detect_display_backend() -> str:
    """Detect whether we're on X11 or Wayland."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    # Try to detect
    try:
        result = subprocess.run(
            ["loginctl", "show-session", "self", "-p", "Type"],
            capture_output=True, text=True, timeout=2,
        )
        if "wayland" in result.stdout.lower():
            return "wayland"
    except Exception:
        pass
    return "x11"  # default


_BACKEND = _detect_display_backend()
logger.info("Detected display backend: %s", _BACKEND)


_xclip_warned = False


def _has_xclip() -> bool:
    global _xclip_warned
    try:
        subprocess.run(["xclip", "-version"], capture_output=True, timeout=2)
        return True
    except Exception:
        if not _xclip_warned:
            logger.warning("xclip not found — install it: sudo apt install xclip")
            _xclip_warned = True
        return False


_wl_warned = False


def _has_wl_copy() -> bool:
    global _wl_warned
    try:
        subprocess.run(["wl-copy", "--version"], capture_output=True, timeout=2)
        return True
    except Exception:
        if not _wl_warned:
            logger.warning("wl-copy not found — install it: sudo apt install wl-clipboard")
            _wl_warned = True
        return False


def _can_read() -> bool:
    """Check if any clipboard tool is available."""
    return _has_xclip() or _has_wl_copy()


def _can_write() -> bool:
    """Check if any clipboard tool is available."""
    return _has_xclip() or _has_wl_copy()


_clipboard_warned = False


def _warn_no_clipboard() -> None:
    global _clipboard_warned
    if not _clipboard_warned:
        logger.warning(
            "No clipboard tool found (xclip or wl-clipboard). "
            "Clipboard read/write is disabled. Install xclip (X11) or wl-clipboard (Wayland)."
        )
        _clipboard_warned = True


class _ClipboardReader(ClipboardReader):
    def read(self) -> ClipboardContent:
        content = ClipboardContent(timestamp=time.time())
        self._image_fmt = ""

        if not _can_read():
            _warn_no_clipboard()
            return content

        text = self._get_text()
        if text:
            content.types[ContentType.TEXT] = text

        html = self._get_html()
        if html:
            content.types[ContentType.HTML] = html

        rtf = self._get_rtf()
        if rtf:
            content.types[ContentType.RTF] = rtf

        img = self._get_image()
        if img:
            content.types[ContentType.IMAGE_PNG] = img
            content.image_fmt = self._image_fmt

        files = self._get_files()
        if files:
            content.types[ContentType.FILE] = files

        url_data = self._get_url()
        if url_data:
            content.types[ContentType.URL] = url_data

        logger.debug("Read %d format(s) from clipboard", len(content.types))
        return content

    def _get_text(self) -> bytes:
        # Try wl-paste first on Wayland, xclip first on X11, but fall back to the other
        tools = (
            [(["wl-paste", "--no-newline"], "wl-paste"), (["xclip", "-selection", "clipboard", "-o"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o"], "xclip"), (["wl-paste", "--no-newline"], "wl-paste")]
        )
        for args, _name in tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout:
                    return result.stdout
            except Exception:
                continue
        return b""

    def _get_html(self) -> bytes:
        if not _can_read():
            return b""
        tools = (
            [(["wl-paste", "--type", "text/html"], "wl-paste"), (["xclip", "-selection", "clipboard", "-o", "-t", "text/html"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o", "-t", "text/html"], "xclip"), (["wl-paste", "--type", "text/html"], "wl-paste")]
        )
        for args, _name in tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    data = result.stdout
                    if b"<" in data:
                        return data
            except Exception:
                continue
        logger.debug("Failed to read HTML from clipboard")
        return b""

    def _get_rtf(self) -> bytes:
        if not _can_read():
            return b""
        tools = (
            [(["wl-paste", "--type", "text/rtf"], "wl-paste"), (["xclip", "-selection", "clipboard", "-o", "-t", "text/rtf"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o", "-t", "text/rtf"], "xclip"), (["wl-paste", "--type", "text/rtf"], "wl-paste")]
        )
        for args, _name in tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    data = result.stdout
                    head = data[:200]
                    if b"\\rtf" in head or b"{\\rtf" in head:
                        return data
            except Exception:
                continue
        logger.debug("Failed to read RTF from clipboard")
        return b""

    def _get_image(self) -> bytes:
        # Try PIL ImageGrab first (works on some Linux desktops)
        try:
            from PIL import ImageGrab
        except ImportError:
            pass
        else:
            try:
                img = ImageGrab.grabclipboard()
                if img:
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    self._image_fmt = "png"
                    return buf.getvalue()
            except Exception:
                logger.debug("ImageGrab.grabclipboard failed")

        if not _can_read():
            return b""
        # Try both CLI tools for image/png
        tools = (
            [(["wl-paste", "--type", "image/png"], "wl-paste"), (["xclip", "-selection", "clipboard", "-o", "-t", "image/png"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o", "-t", "image/png"], "xclip"), (["wl-paste", "--type", "image/png"], "wl-paste")]
        )
        for args, _name in tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout and result.stdout[:8] == b"\x89PNG\r\n\x1a\n":
                    self._image_fmt = "png"
                    return result.stdout
            except Exception:
                continue
        logger.debug("Failed to read image from clipboard")
        return b""

    def _get_files(self) -> bytes:
        """Read file URIs from clipboard (e.g. files copied in a file manager).

        Many Linux file managers copy file paths as ``text/uri-list``.
        Also tries ``x-special/gnome-copied-files`` for Nautilus metadata.
        """
        if not _can_read():
            return b""
        # Try text/uri-list (most file managers: Dolphin, Thunar, PCManFM)
        tools = (
            [(["wl-paste", "--type", "text/uri-list"], "wl-paste"),
             (["xclip", "-selection", "clipboard", "-o", "-t", "text/uri-list"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o", "-t", "text/uri-list"], "xclip"),
                  (["wl-paste", "--type", "text/uri-list"], "wl-paste")]
        )
        for args, _name in tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout.decode("utf-8", errors="replace")
                    # Convert file:// URIs to paths
                    paths = []
                    for line in text.split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            from urllib.parse import unquote, urlparse
                            parsed = urlparse(line)
                            if parsed.path:
                                paths.append(unquote(parsed.path))
                    if paths:
                        return "\n".join(paths).encode("utf-8")
            except Exception:
                continue
        return b""

    def _get_url(self) -> bytes:
        """Read URL from clipboard as text/uri-list (browser link copies)."""
        if not _can_read():
            return b""
        tools = (
            [(["wl-paste", "--type", "text/uri-list"], "wl-paste"),
             (["xclip", "-selection", "clipboard", "-o", "-t", "text/uri-list"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o", "-t", "text/uri-list"], "xclip"),
                  (["wl-paste", "--type", "text/uri-list"], "wl-paste")]
        )
        for args, _name in tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout.decode("utf-8", errors="replace").strip()
                    if text and not text.startswith("file://"):
                        return text.encode("utf-8")
            except Exception:
                continue
        return b""


class _ClipboardWriter(ClipboardWriter):
    def write(self, content: ClipboardContent):
        # Write formats sorted so TEXT lands last — each xclip/wl-copy
        # call replaces the entire clipboard, and plain text is the
        # most important fallback for the receiving side.
        _TEXT_LAST = {ContentType.TEXT: 1}
        for fmt_type, data in sorted(
            content.types.items(),
            key=lambda item: _TEXT_LAST.get(item[0], 0),
        ):
            logger.debug("Writing %s to clipboard (%d bytes)", fmt_type.name, len(data))
            if fmt_type == ContentType.TEXT:
                self._set_text(data)
            elif fmt_type == ContentType.HTML:
                self._set_html(data)
            elif fmt_type == ContentType.RTF:
                self._set_rtf(data)
            elif fmt_type == ContentType.IMAGE_PNG:
                self._set_image(data, content.image_fmt)
            elif fmt_type == ContentType.FILE:
                self._set_files(data)
            elif fmt_type == ContentType.URL:
                self._set_url(data)
            # IMAGE_EMF is Windows-only, skip on Linux

    def _set_text(self, data: bytes):
        if not _can_write():
            _warn_no_clipboard()
            return
        tools = (
            [(["wl-copy"], "wl-copy"), (["xclip", "-selection", "clipboard", "-in"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-in"], "xclip"), (["wl-copy"], "wl-copy")]
        )
        for args, _name in tools:
            try:
                subprocess.run(args, input=data, timeout=2)
                return  # success
            except Exception:
                continue
        logger.debug("Failed to write text to clipboard")

    def _set_html(self, data: bytes):
        if not _can_write():
            _warn_no_clipboard()
            return
        tools = (
            [(["wl-copy", "--type", "text/html"], "wl-copy"), (["xclip", "-selection", "clipboard", "-in", "-t", "text/html"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-in", "-t", "text/html"], "xclip"), (["wl-copy", "--type", "text/html"], "wl-copy")]
        )
        for args, _name in tools:
            try:
                subprocess.run(args, input=data, timeout=2)
                return
            except Exception:
                continue
        logger.debug("Failed to write HTML to clipboard")

    def _set_rtf(self, data: bytes):
        if not _can_write():
            _warn_no_clipboard()
            return
        tools = (
            [(["wl-copy", "--type", "text/rtf"], "wl-copy"), (["xclip", "-selection", "clipboard", "-in", "-t", "text/rtf"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-in", "-t", "text/rtf"], "xclip"), (["wl-copy", "--type", "text/rtf"], "wl-copy")]
        )
        for args, _name in tools:
            try:
                subprocess.run(args, input=data, timeout=2)
                return
            except Exception:
                continue
        logger.debug("Failed to write RTF to clipboard")

    def _set_image(self, data: bytes, image_fmt: str = ""):
        png_data = data
        if image_fmt in ("bmp", "tiff"):
            try:
                from PIL import Image
                img = Image.open(BytesIO(data))
                buf = BytesIO()
                img.save(buf, format="PNG")
                png_data = buf.getvalue()
            except Exception:
                logger.debug("Failed to convert %s image to PNG for Linux", image_fmt)
                return

        if not _can_write():
            _warn_no_clipboard()
            return
        tools = (
            [(["wl-copy", "--type", "image/png"], "wl-copy"), (["xclip", "-selection", "clipboard", "-in", "-t", "image/png"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-in", "-t", "image/png"], "xclip"), (["wl-copy", "--type", "image/png"], "wl-copy")]
        )
        for args, _name in tools:
            try:
                subprocess.run(args, input=png_data, timeout=2)
                return
            except Exception:
                continue
        logger.debug("Failed to write image to clipboard")

    def _set_files(self, data: bytes):
        """Write file paths to clipboard as text/uri-list.

        Expects newline-separated UTF-8 paths. Each path is converted
        to a ``file://`` URI so file managers can paste them as files.
        """
        try:
            paths = [p.strip() for p in data.decode("utf-8").split("\n") if p.strip()]
        except Exception:
            return
        if not paths:
            return

        from urllib.parse import quote as urllib_quote_path
        uri_list = "\n".join(
            "file://" + urllib_quote_path(p) for p in paths
        ).encode("utf-8")

        if not _can_write():
            _warn_no_clipboard()
            return
        tools = (
            [(["wl-copy", "--type", "text/uri-list"], "wl-copy"),
             (["xclip", "-selection", "clipboard", "-in", "-t", "text/uri-list"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-in", "-t", "text/uri-list"], "xclip"),
                  (["wl-copy", "--type", "text/uri-list"], "wl-copy")]
        )
        for args, _name in tools:
            try:
                subprocess.run(args, input=uri_list, timeout=2)
                return
            except Exception:
                continue
        logger.debug("Failed to write file URIs to clipboard")

    def _set_url(self, data: bytes):
        """Write a URL to the clipboard as text/uri-list."""
        try:
            url = data.decode("utf-8").strip()
        except Exception:
            return
        if not url:
            return

        uri_data = url.encode("utf-8")
        if not _can_write():
            _warn_no_clipboard()
            return
        tools = (
            [(["wl-copy", "--type", "text/uri-list"], "wl-copy"),
             (["xclip", "-selection", "clipboard", "-in", "-t", "text/uri-list"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-in", "-t", "text/uri-list"], "xclip"),
                  (["wl-copy", "--type", "text/uri-list"], "wl-copy")]
        )
        for args, _name in tools:
            try:
                subprocess.run(args, input=uri_data, timeout=2)
                return
            except Exception:
                continue
        logger.debug("Failed to write URL to clipboard")


class LinuxClipboardMonitor(ClipboardMonitor):
    """Poll-based clipboard monitor for Linux."""

    def __init__(self, poll_interval: float = POLL_INTERVAL):
        self._running = False
        self._thread = None
        self._callback = None
        self._poll_interval = poll_interval

    def start(self, callback):
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Clipboard monitor started")

    def stop(self):
        self._running = False
        logger.info("Clipboard monitor stopped")

    def _poll_loop(self):
        last_hash = self._get_content_hash()
        while self._running:
            time.sleep(self._poll_interval)
            current = self._get_content_hash()
            if current and last_hash and current != last_hash:
                last_hash = current
                if time.time() < self.suppress_until:
                    continue
                if self._callback:
                    try:
                        # Capture source app info before the callback fires.
                        self.last_source_app = self.get_active_app()
                        self._callback()
                    except Exception:
                        pass
            elif current and not last_hash:
                last_hash = current

    def _get_content_hash(self) -> str:
        if not _can_read():
            return ""

        # Try both tools for plain text
        text_tools = (
            [(["wl-paste", "--no-newline"], "wl-paste"), (["xclip", "-selection", "clipboard", "-o"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o"], "xclip"), (["wl-paste", "--no-newline"], "wl-paste")]
        )
        for args, _name in text_tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout:
                    return hashlib.sha256(result.stdout).hexdigest()
            except Exception:
                continue

        # Text read returned nothing — clipboard may contain an image.
        img_tools = (
            [(["wl-paste", "--type", "image/png"], "wl-paste"), (["xclip", "-selection", "clipboard", "-o", "-t", "image/png"], "xclip")]
            if _BACKEND == "wayland"
            else [(["xclip", "-selection", "clipboard", "-o", "-t", "image/png"], "xclip"), (["wl-paste", "--type", "image/png"], "wl-paste")]
        )
        for args, _name in img_tools:
            try:
                result = subprocess.run(args, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout:
                    return hashlib.sha256(result.stdout).hexdigest()
            except Exception:
                continue
        return ""


_startup_warning_shown = False


def check_clipboard_tools() -> str | None:
    """Return a warning message if no clipboard tool is available, or None if OK.
    Only shows the warning once per process."""
    global _startup_warning_shown
    if _startup_warning_shown:
        return None
    _startup_warning_shown = True
    if not _can_read():
        return (
            "No clipboard tool found (xclip or wl-clipboard).\n\n"
            "Clipboard sync will not work until you install one:\n\n"
            "  sudo apt install xclip         (X11)\n"
            "  sudo apt install wl-clipboard  (Wayland)"
        )
    return None


def create_monitor(poll_interval: float = POLL_INTERVAL) -> ClipboardMonitor:
    return LinuxClipboardMonitor(poll_interval=poll_interval)


def create_reader() -> ClipboardReader:
    return _ClipboardReader()


def create_writer() -> ClipboardWriter:
    return _ClipboardWriter()
