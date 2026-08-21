"""macOS clipboard implementation.

Clipboard access via two paths:
1. pbpaste / pbcopy for text/HTML/RTF (signed Apple binaries, no TCC issues)
2. ctypes + Objective-C runtime → NSPasteboard for image data (bypasses
   osascript TCC restrictions on macOS ≥14 Sonoma/Sequoia)

Clipboard monitoring polls NSPasteboard.changeCount via the ctypes bridge,
since macOS provides no event-driven clipboard API.
"""

import ctypes
import ctypes.util
import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
from io import BytesIO

from internal.clipboard.clipboard import ClipboardMonitor, ClipboardReader, ClipboardWriter
from internal.clipboard.format import ClipboardContent, ContentType

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.4

# ---------------------------------------------------------------------------
# ctypes → Objective-C runtime bridge for NSPasteboard
# ---------------------------------------------------------------------------

_nspasteboard_objc = None
_nspasteboard_instance = None
_objc_lock = threading.Lock()

_IMAGE_UTIS = frozenset({
    b"public.tiff", b"public.png", b"public.jpeg",
    b"public.jpeg-2000", b"com.apple.pasteboard.image",
    b"NSTIFFPboardType", b"com.compuserve.gif",
    b"public.heic", b"public.heif", b"public.avci",
})


def _init_nspasteboard():
    """Load NSPasteboard via ctypes + libobjc.  Cached on first success."""
    global _nspasteboard_objc, _nspasteboard_instance
    if _nspasteboard_instance is not None:
        return _nspasteboard_objc, _nspasteboard_instance

    try:
        lib_path = ctypes.util.find_library("objc")
        if not lib_path:
            logger.debug("libobjc not found via find_library")
            return None, None

        objc = ctypes.cdll.LoadLibrary(lib_path)
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        objc.objc_msgSend.restype = ctypes.c_void_p

        ns_pasteboard = objc.objc_getClass(b"NSPasteboard")
        sel_general = objc.sel_registerName(b"generalPasteboard")
        pb = objc.objc_msgSend(ns_pasteboard, sel_general)
        if not pb:
            logger.debug("NSPasteboard.generalPasteboard returned nil")
            return None, None

        _nspasteboard_objc = objc
        _nspasteboard_instance = pb
        logger.debug("NSPasteboard bridge initialized via ctypes")
    except Exception:
        logger.debug("Failed to init NSPasteboard via ctypes", exc_info=True)
        return None, None

    return _nspasteboard_objc, _nspasteboard_instance


def _pb_types() -> set[bytes]:
    """Return the set of UTI strings currently on the general pasteboard."""
    objc, pb = _init_nspasteboard()
    if not pb:
        return set()

    with _objc_lock:
        try:
            sel_types = objc.sel_registerName(b"types")
            types_arr = objc.objc_msgSend(pb, sel_types)
            if not types_arr:
                return set()

            sel_count = objc.sel_registerName(b"count")
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = ctypes.c_void_p
            count = objc.objc_msgSend(types_arr, sel_count)

            sel_object = objc.sel_registerName(b"objectAtIndex:")
            sel_utf8 = objc.sel_registerName(b"UTF8String")

            result = set()
            for i in range(count):
                objc.objc_msgSend.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                ]
                objc.objc_msgSend.restype = ctypes.c_void_p
                ns_str = objc.objc_msgSend(types_arr, sel_object, i)
                if ns_str:
                    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                    objc.objc_msgSend.restype = ctypes.c_void_p
                    c_str = objc.objc_msgSend(ns_str, sel_utf8)
                    if c_str:
                        result.add(ctypes.c_char_p(c_str).value)
            return result
        except Exception:
            logger.debug("_pb_types failed", exc_info=True)
            return set()


def _pb_data_for_type(uti: bytes) -> bytes | None:
    """Read raw data for a UTI from the general pasteboard."""
    objc, pb = _init_nspasteboard()
    if not pb:
        return None

    with _objc_lock:
        try:
            ns_uti = _nsstring(objc, uti)
            if not ns_uti:
                return None

            sel_data = objc.sel_registerName(b"dataForType:")
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = ctypes.c_void_p
            ns_data = objc.objc_msgSend(pb, sel_data, ns_uti)
            if not ns_data:
                return None

            sel_length = objc.sel_registerName(b"length")
            sel_bytes = objc.sel_registerName(b"bytes")
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = ctypes.c_void_p

            length = objc.objc_msgSend(ns_data, sel_length)
            if not length:
                return None

            ptr = objc.objc_msgSend(ns_data, sel_bytes)
            if not ptr:
                return None

            return ctypes.string_at(ptr, length)
        except Exception:
            logger.debug("_pb_data_for_type(%s) failed", uti, exc_info=True)
            return None


def _nsstring(objc, s: bytes):
    """Create an NSString from a Python bytes string via ctypes.

    Caller must release the returned object when done (not needed for
    short-lived calls — the Objective-C autorelease pool handles it).
    """
    sel_alloc = objc.sel_registerName(b"alloc")
    sel_init = objc.sel_registerName(b"initWithUTF8String:")
    ns_string_cls = objc.objc_getClass(b"NSString")
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    objc.objc_msgSend.restype = ctypes.c_void_p
    alloced = objc.objc_msgSend(ns_string_cls, sel_alloc)
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
    objc.objc_msgSend.restype = ctypes.c_void_p
    return objc.objc_msgSend(alloced, sel_init, s)


def _pb_has_image() -> bool:
    """Check whether any image UTI is present on the pasteboard."""
    types = _pb_types()
    return bool(types & _IMAGE_UTIS)


def _pb_change_count() -> int | None:
    """Read NSPasteboard.changeCount via KVC.  Returns None on failure."""
    objc, pb = _init_nspasteboard()
    if not pb:
        return None

    with _objc_lock:
        try:
            key = _nsstring(objc, b"changeCount")
            if not key:
                return None

            sel_value = objc.sel_registerName(b"valueForKey:")
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = ctypes.c_void_p
            ns_number = objc.objc_msgSend(pb, sel_value, key)
            if not ns_number:
                return None

            sel_int = objc.sel_registerName(b"integerValue")
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = ctypes.c_long
            return objc.objc_msgSend(ns_number, sel_int)
        except Exception:
            logger.debug("_pb_change_count failed", exc_info=True)
            return None


def _pb_clear_contents() -> bool:
    """Clear all items from the general pasteboard."""
    objc, pb = _init_nspasteboard()
    if not pb:
        return False

    with _objc_lock:
        try:
            sel_clear = objc.sel_registerName(b"clearContents")
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend(pb, sel_clear)
            return True
        except Exception:
            logger.debug("_pb_clear_contents failed", exc_info=True)
            return False


def _pb_set_data_for_type(uti: bytes, data: bytes) -> bool:
    """Set pasteboard data for a UTI type."""
    objc, pb = _init_nspasteboard()
    if not pb:
        return False

    with _objc_lock:
        try:
            ns_uti = _nsstring(objc, uti)
            if not ns_uti:
                return False

            sel_data_with_bytes = objc.sel_registerName(b"dataWithBytes:length:")
            ns_data_cls = objc.objc_getClass(b"NSData")
            objc.objc_msgSend.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong,
            ]
            objc.objc_msgSend.restype = ctypes.c_void_p
            ns_data = objc.objc_msgSend(ns_data_cls, sel_data_with_bytes, data, len(data))
            if not ns_data:
                return False

            sel_set_data = objc.sel_registerName(b"setData:forType:")
            objc.objc_msgSend.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ]
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend(pb, sel_set_data, ns_data, ns_uti)
            return True
        except Exception:
            logger.debug("_pb_set_data_for_type(%s) failed", uti, exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Clipboard reader
# ---------------------------------------------------------------------------

class _ClipboardReader(ClipboardReader):
    def read(self) -> ClipboardContent:
        content = ClipboardContent(timestamp=time.time())
        self._image_fmt = ""

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

        return content

    # -- text / html / rtf via pbpaste (no TCC issues) ---------------------

    def _get_text(self) -> bytes:
        # Prefer ctypes NSPasteboard → public.utf8-plain-text (guaranteed UTF-8).
        # pbpaste -Prefer txt can return bytes in a legacy encoding (e.g. GBK)
        # for CJK text, producing garbled characters when decoded as UTF-8.
        data = _pb_data_for_type(b"public.utf8-plain-text")
        if data:
            return data

        # Fallback: pbpaste
        try:
            result = subprocess.run(
                ["pbpaste", "-Prefer", "txt"],
                capture_output=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            logger.debug("pbpaste text read failed", exc_info=True)
        return b""

    def _get_html(self) -> bytes:
        # Method 1: pbpaste -Prefer html
        try:
            result = subprocess.run(
                ["pbpaste", "-Prefer", "html"],
                capture_output=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = result.stdout
                if b"<" in data and b">" in data:
                    return data
        except Exception:
            logger.debug("pbpaste html read failed", exc_info=True)

        # Method 2: ctypes NSPasteboard (no TCC issues)
        data = _pb_data_for_type(b"public.html")
        if data and b"<" in data and b">" in data:
            logger.debug("Read HTML via ctypes NSPasteboard (%d bytes)", len(data))
            return data

        # Method 3: osascript NSPasteboard (legacy fallback, may need TCC)
        return self._get_html_via_osascript()

    def _get_html_via_osascript(self) -> bytes:
        try:
            script = (
                'use framework "AppKit"\n'
                'set pb to current application\'s NSPasteboard\'s generalPasteboard()\n'
                'set htmlData to pb\'s dataForType:"public.html"\n'
                'if htmlData = missing value then\n'
                '    return "CLIPSYNC_NO_HTML"\n'
                'end if\n'
                'set htmlStr to current application\'s NSString\'s alloc()\'s '
                'initWithData:htmlData encoding:current application\'s NSUTF8StringEncoding\n'
                'if htmlStr = missing value then\n'
                '    return "CLIPSYNC_NO_HTML"\n'
                'end if\n'
                'return htmlStr as text'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
            if result.returncode == 0 and result.stdout:
                data = result.stdout
                if data != b"CLIPSYNC_NO_HTML" and b"<" in data and b">" in data:
                    logger.debug("Read HTML via osascript fallback (%d bytes)", len(data))
                    return data
        except Exception:
            logger.debug("osascript html read failed", exc_info=True)
        return b""

    def _get_rtf(self) -> bytes:
        try:
            result = subprocess.run(
                ["pbpaste", "-Prefer", "rtf"],
                capture_output=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = result.stdout
                head = data[:200]
                if b"\\rtf" in head or b"{\\rtf" in head:
                    return data
        except Exception:
            logger.debug("pbpaste rtf read failed", exc_info=True)
        return b""

    # -- image via ctypes NSPasteboard ------------------------------------

    def _get_image(self) -> bytes:
        self._image_fmt = ""

        # Method 1: ctypes NSPasteboard (primary — no TCC issues)
        data = self._get_image_via_ctypes()
        if data:
            return data

        # Method 2: PIL.ImageGrab.grabclipboard()
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
            if img is not None:
                buf = BytesIO()
                img.save(buf, format="PNG")
                self._image_fmt = "png"
                logger.info("Read image via ImageGrab.grabclipboard (%d bytes)", buf.tell())
                return buf.getvalue()
        except NotImplementedError:
            logger.debug("ImageGrab.grabclipboard not implemented on this platform")
        except ImportError:
            logger.debug("PIL import failed, image read unavailable")
        except Exception:
            logger.debug("ImageGrab read failed", exc_info=True)

        # Method 3: plain AppleScript (no AppKit)
        data = self._get_image_via_applescript()
        if data:
            return data

        # Method 4: NSPasteboard via AppleScript-ObjC (may need TCC)
        return self._get_image_via_nspasteboard()

    def _get_image_via_ctypes(self) -> bytes:
        """Read raw image data from NSPasteboard via ctypes.

        Tries public.png first (passthrough, no re-encoding), then
        public.tiff (native macOS clipboard format).  Returns raw bytes
        without PIL decode/encode — the caller sets the format tag.
        """
        for uti in [b"public.png", b"public.tiff"]:
            raw = _pb_data_for_type(uti)
            if not raw:
                continue
            fmt = "png" if uti == b"public.png" else "tiff"
            if fmt == "png" and raw[:8] != b'\x89PNG\r\n\x1a\n':
                continue  # not valid PNG, try next UTI
            self._image_fmt = fmt
            logger.info("Read raw %s image via ctypes NSPasteboard (%d bytes)",
                       fmt, len(raw))
            return raw
        return b""

    def _get_image_via_applescript(self) -> bytes:
        """Read clipboard image using plain AppleScript — no AppKit needed.

        This avoids macOS TCC (Transparency, Consent, and Control)
        permission issues that ``use framework "AppKit"`` can trigger.
        """
        try:
            from PIL import Image
        except ImportError:
            return b""

        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tiff")
            os.close(tmp_fd)

            script = (
                f'set f to open for access (POSIX file "{tmp_path}") '
                'with write permission\n'
                'set eof f to 0\n'
                'write (the clipboard as «class TIFF») to f\n'
                'close access f\n'
                'return "OK"'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if "-1700" in stderr:
                    logger.debug("AppleScript: no image on clipboard (expected)")
                else:
                    logger.warning("AppleScript image read failed (permissions?): %s",
                                  stderr[:200] if stderr else "unknown error")
                return b""

            file_size = os.path.getsize(tmp_path)
            if file_size == 0:
                logger.debug("AppleScript wrote empty image file — no image on clipboard")
                return b""

            img = Image.open(tmp_path)
            buf = BytesIO()
            img.save(buf, format="PNG")
            self._image_fmt = "png"
            logger.info("Read image via plain AppleScript (%d bytes)", buf.tell())
            return buf.getvalue()
        except Exception:
            logger.warning("AppleScript image read exception", exc_info=True)
            return b""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _get_image_via_nspasteboard(self) -> bytes:
        """Read image from NSPasteboard via AppleScript-ObjC bridge.

        May require macOS Accessibility permissions for the terminal /
        Python launcher on macOS ≥14.
        """
        try:
            from PIL import Image
        except ImportError:
            return b""

        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".png")
            os.close(tmp_fd)

            script = (
                'use framework "AppKit"\n'
                'set pb to current application\'s NSPasteboard\'s generalPasteboard()\n'
                'set theClasses to current application\'s NSArray\'s '
                'arrayWithObject:(current application\'s NSImage\'s class)\n'
                'set results to pb\'s readObjectsForClasses:theClasses '
                'options:(missing value)\n'
                'if results\'s |count|() = 0 then\n'
                '    return "NO_IMAGE"\n'
                'end if\n'
                'set img to results\'s firstObject()\n'
                'set tiffRep to img\'s TIFFRepresentation()\n'
                'set pngRep to current application\'s NSBitmapImageRep\'s '
                'imageRepWithData:tiffRep\n'
                'if pngRep = missing value then\n'
                '    return "NO_IMAGE"\n'
                'end if\n'
                'set pngData to pngRep\'s representationUsingType:'
                '(current application\'s NSPNGFileType) |properties|:(missing value)\n'
                'if pngData = missing value then\n'
                '    return "NO_IMAGE"\n'
                'end if\n'
                f'set tmpPath to "{tmp_path}"\n'
                'pngData\'s writeToFile:tmpPath atomically:true\n'
                'return "OK"'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                logger.warning("NSPasteboard osascript failed (permissions?): %s",
                              stderr[:200] if stderr else "unknown error")
                return b""
            if b"NO_IMAGE" in (result.stdout or b""):
                return b""

            file_size = os.path.getsize(tmp_path)
            if file_size == 0:
                logger.debug("NSPasteboard wrote empty image file")
                return b""

            img = Image.open(tmp_path)
            buf = BytesIO()
            img.save(buf, format="PNG")
            self._image_fmt = "png"
            logger.info("Read image via NSPasteboard osascript (%d bytes)", buf.tell())
            return buf.getvalue()
        except Exception:
            logger.warning("NSPasteboard image read exception", exc_info=True)
            return b""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


    # -- file paths via NSPasteboard -------------------------------------

    def _get_files(self) -> bytes:
        """Read file paths from the pasteboard (Finder copies, etc.).

        On modern macOS, Finder copies files as ``public.file-url``.
        Older apps may use ``NSFilenamesPboardType``.
        """
        # Method 1: public.file-url (macOS 10.13+)
        raw = _pb_data_for_type(b"public.file-url")
        if raw:
            try:
                url_str = raw.decode("utf-8", errors="replace").strip()
                from urllib.parse import unquote, urlparse
                parsed = urlparse(url_str)
                path = unquote(parsed.path)
                if path:
                    return path.encode("utf-8")
            except Exception:
                logger.debug("Failed to parse public.file-url", exc_info=True)

        # Method 2: NSFilenamesPboardType (legacy, pre-10.13)
        raw = _pb_data_for_type(b"NSFilenamesPboardType")
        if raw:
            try:
                # Property list serialization — array of file path strings
                import plistlib
                paths = plistlib.loads(raw)
                if isinstance(paths, list) and paths:
                    return "\n".join(str(p) for p in paths).encode("utf-8")
            except Exception:
                logger.debug("Failed to parse NSFilenamesPboardType", exc_info=True)

        # Method 3: Try pbpaste for filenames (some apps)
        try:
            result = subprocess.run(
                ["pbpaste", "-Prefer", "public.file-url"],
                capture_output=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

        return b""

    # -- URL via NSPasteboard -------------------------------------------

    def _get_url(self) -> bytes:
        """Read URL from pasteboard (copied links from browsers, etc.).

        macOS stores URLs as ``public.url`` (UTF-8 string).
        Some apps also use ``public.utf8-plain-text`` with a URL pattern,
        but we already capture that as TEXT.
        """
        raw = _pb_data_for_type(b"public.url")
        if raw:
            try:
                url = raw.decode("utf-8", errors="replace").strip()
                if url:
                    return url.encode("utf-8")
            except Exception:
                pass
        return b""


# ---------------------------------------------------------------------------
# Clipboard writer
# ---------------------------------------------------------------------------

class _ClipboardWriter(ClipboardWriter):
    def write(self, content: ClipboardContent):
        # Try atomic multi-format write via ctypes NSPasteboard bridge.
        if self._write_atomic(content):
            return

        # Fallback: write formats individually (best-effort, TEXT last).
        # Each subprocess call replaces the entire clipboard, so write
        # the most important format (TEXT) last so it survives.
        _TEXT_LAST = {ContentType.TEXT: 1}
        for fmt_type, data in sorted(
            content.types.items(),
            key=lambda item: _TEXT_LAST.get(item[0], 0),
        ):
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

    def _write_atomic(self, content: ClipboardContent) -> bool:
        """Write all formats atomically via ctypes NSPasteboard.

        Uses clearContents + setData:forType: so every format lands
        on the pasteboard together.  Returns False (fall through to
        subprocess fallback) if the ctypes bridge is unavailable.
        """
        objc, pb = _init_nspasteboard()
        if not pb:
            return False

        # Build list of (UTI, data) pairs with format conversion.
        write_ops = []
        for fmt_type, data in content.types.items():
            if fmt_type == ContentType.TEXT:
                write_ops.append((b"public.utf8-plain-text", data))
            elif fmt_type == ContentType.HTML:
                write_ops.append((b"public.html", data))
            elif fmt_type == ContentType.RTF:
                write_ops.append((b"public.rtf", data))
            elif fmt_type == ContentType.IMAGE_PNG:
                if content.image_fmt == "tiff":
                    write_ops.append((b"public.tiff", data))
                elif content.image_fmt == "bmp":
                    try:
                        from PIL import Image
                        img = Image.open(BytesIO(data))
                        buf = BytesIO()
                        img.save(buf, format="PNG")
                        write_ops.append((b"public.png", buf.getvalue()))
                    except Exception:
                        logger.debug("BMP→PNG conversion failed for atomic write")
                        continue
                else:
                    write_ops.append((b"public.png", data))
            elif fmt_type == ContentType.FILE:
                paths = data.decode("utf-8").split("\n")
                for path in paths:
                    path = path.strip()
                    if path:
                        from urllib.parse import quote as urllib_quote_path
                        encoded = ("file://" + urllib_quote_path(path)).encode("utf-8")
                        write_ops.append((b"public.file-url", encoded))
            elif fmt_type == ContentType.URL:
                write_ops.append((b"public.url", data))
            # IMAGE_EMF is Windows-only, skip.

        if not write_ops:
            return False

        if not _pb_clear_contents():
            logger.debug("atomic write: clearContents failed, falling back")
            return False

        for uti, fmt_data in write_ops:
            logger.debug("Atomic write: %s (%d bytes)", uti, len(fmt_data))
            _pb_set_data_for_type(uti, fmt_data)

        logger.debug("Atomic multi-format write: %d format(s)", len(write_ops))
        return True

    def _set_text(self, data: bytes):
        try:
            result = subprocess.run(["pbcopy"], input=data, timeout=2)
            if result.returncode != 0:
                logger.warning("pbcopy returned non-zero exit code: %d", result.returncode)
        except Exception:
            logger.debug("pbcopy write failed", exc_info=True)

    def _set_html(self, data: bytes):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".html", delete=False,
            ) as f:
                f.write(data)
                tmp_path = f.name

            script = (
                f'set the clipboard to (read (POSIX file "{tmp_path}") '
                f'as «class HTML»)'
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            logger.debug("osascript html write failed", exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _set_rtf(self, data: bytes):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".rtf", delete=False,
            ) as f:
                f.write(data)
                tmp_path = f.name

            script = (
                f'set the clipboard to (read (POSIX file "{tmp_path}") '
                f'as «class RTF »)'
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            logger.debug("osascript rtf write failed", exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _set_image(self, data: bytes, image_fmt: str = ""):
        tmp_path = None
        try:
            if image_fmt == "tiff":
                with tempfile.NamedTemporaryFile(
                    suffix=".tiff", delete=False,
                ) as f:
                    f.write(data)
                    tmp_path = f.name
                script = (
                    f'set the clipboard to (read (POSIX file "{tmp_path}") '
                    f'as «class TIFF»)'
                )
            elif image_fmt == "bmp":
                from PIL import Image
                img = Image.open(BytesIO(data))
                with tempfile.NamedTemporaryFile(
                    suffix=".png", delete=False,
                ) as f:
                    img.save(f, format="PNG")
                    tmp_path = f.name
                script = (
                    f'set the clipboard to (read (POSIX file "{tmp_path}") '
                    f'as «class PNGf»)'
                )
            else:
                from PIL import Image
                Image.open(BytesIO(data))
                with tempfile.NamedTemporaryFile(
                    suffix=".png", delete=False,
                ) as f:
                    f.write(data)
                    tmp_path = f.name
                script = (
                    f'set the clipboard to (read (POSIX file "{tmp_path}") '
                    f'as «class PNGf»)'
                )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            logger.debug("osascript image write failed", exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _set_files(self, data: bytes):
        """Write file paths to pasteboard via osascript.

        Expects newline-separated UTF-8 paths. Writes both
        public.file-url and the legacy NSFilenamesPboardType so
        Finder and older apps can pick up the file references.
        """
        try:
            paths = [p.strip() for p in data.decode("utf-8").split("\n") if p.strip()]
        except Exception:
            return
        if not paths:
            return

        # Use osascript to create a file URL list on the pasteboard
        path_list = ", ".join(f'"{p}"' for p in paths)
        script = (
            f'set theFiles to {{{path_list}}}\n'
            'set pb to current application\'s NSPasteboard\'s generalPasteboard()\n'
            'pb\'s clearContents()\n'
            'repeat with f in theFiles\n'
            '    set fileURL to current application\'s NSURL\'s fileURLWithPath:f\n'
            '    pb\'s writeObjects:{fileURL}\n'
            'end repeat\n'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            logger.debug("osascript file write failed", exc_info=True)

    def _set_url(self, data: bytes):
        """Write a URL to the pasteboard as public.url."""
        try:
            url = data.decode("utf-8").strip()
            if not url:
                return
        except Exception:
            return

        # Use osascript to set both public.url and public.utf8-plain-text
        script = (
            f'set the clipboard to "{url}"\n'
            f'set pb to current application\'s NSPasteboard\'s generalPasteboard()\n'
            f'set nsStr to current application\'s NSString\'s stringWithString:"{url}"\n'
            f'pb\'s setString:nsStr forType:"public.url"\n'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            logger.debug("osascript URL write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Clipboard monitor
# ---------------------------------------------------------------------------

class DarwinClipboardMonitor(ClipboardMonitor):
    """Poll-based clipboard monitor for macOS.

    Uses NSPasteboard.changeCount via the ctypes bridge as the primary
    change-detection mechanism.  Falls back to content hashing (pbpaste)
    if the ctypes bridge fails to initialise.
    """

    def __init__(self, poll_interval: float = POLL_INTERVAL):
        self._running = False
        self._thread = None
        self._callback = None
        self._poll_interval = poll_interval

    def start(self, callback):
        logger.info("Clipboard monitor started")
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        logger.info("Clipboard monitor stopped")
        self._running = False

    # -- poll loop --------------------------------------------------------

    def _poll_loop(self):
        # Prefer ctypes changeCount (detects all content types, no TCC issues)
        last_cc = _pb_change_count()
        if last_cc is not None:
            logger.debug("Monitor using ctypes NSPasteboard.changeCount")
            self._poll_change_count(last_cc)
        else:
            logger.debug(
                "Monitor falling back to content hashing "
                "(ctypes bridge unavailable)"
            )
            self._poll_hash()

    def _poll_change_count(self, last_cc: int):
        while self._running:
            time.sleep(self._poll_interval)
            current = _pb_change_count()
            if current is not None and current != last_cc:
                last_cc = current
                self._fire_callback()

    def _poll_hash(self):
        last_hash = self._get_content_hash()
        while self._running:
            time.sleep(self._poll_interval)
            current = self._get_content_hash()
            if current == last_hash:
                continue
            last_hash = current
            if not current:
                # Clipboard became empty — reset so the next copy (even of
                # previously-seen content) is treated as a change.
                continue
            # Content changed, including the first copy after an empty
            # clipboard (last_hash == "" → current).
            self._fire_callback()

    def _fire_callback(self):
        if time.time() < self.suppress_until:
            return
        if self._callback:
            try:
                # Capture source app info before the callback fires,
                # so we know which app produced the clipboard content.
                self.last_source_app = self.get_active_app()
                self._callback()
            except Exception:
                logger.warning(
                    "Clipboard change callback failed", exc_info=True,
                )

    # -- fallback content hash --------------------------------------------

    def _get_content_hash(self) -> str:
        """Hash text + HTML pasteboard content for change detection.

        Also checks for image-only content so image copies are not missed.
        """
        try:
            text = subprocess.run(
                ["pbpaste", "-Prefer", "txt"],
                capture_output=True, timeout=3,
            )
            html = subprocess.run(
                ["pbpaste", "-Prefer", "html"],
                capture_output=True, timeout=3,
            )
            txt_data = text.stdout if text.returncode == 0 else b""
            html_data = html.stdout if html.returncode == 0 else b""
            combined = txt_data + html_data
            if combined:
                return hashlib.sha256(combined).hexdigest()

            # No text/HTML — check for image-only content.
            if _pb_has_image():
                raw = _pb_data_for_type(b"public.tiff") or _pb_data_for_type(b"public.png")
                if raw:
                    return hashlib.sha256(raw).hexdigest()
                return hashlib.sha256(str(time.time()).encode()).hexdigest()
        except Exception:
            pass
        return ""


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def create_monitor(poll_interval: float = POLL_INTERVAL) -> ClipboardMonitor:
    return DarwinClipboardMonitor(poll_interval=poll_interval)


def create_reader() -> ClipboardReader:
    return _ClipboardReader()


def create_writer() -> ClipboardWriter:
    return _ClipboardWriter()
