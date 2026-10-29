"""Render a Windows enhanced metafile to PNG for previews.

Windows publishes vector clips (an SVG copied out of a browser, a shape copied
out of Office) as ``CF_ENHMETAFILE`` bytes.  The desktop stores them as
``IMAGE_EMF`` because pasting them back keeps the vector; no browser on the
phone can draw EMF, and a history row that only carries EMF therefore reads as
the label "Vector Image" with nothing to look at.

This module turns those bytes into a bitmap once, at capture time, and lets the
existing ``IMAGE`` path carry it beside the vector.  It is intentionally small
and best-effort: any failure returns ``None`` and the vector stays exactly as it
was, because losing a clip is worse than losing its thumbnail.
"""

from __future__ import annotations

import ctypes
import io
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

_LOGPIXELSX = 88
_DIB_RGB_COLORS = 0
_BI_RGB = 0
_MAX_DIMENSION = 4096


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _ENHMETAHEADER(ctypes.Structure):
    _fields_ = [
        ("iType", wintypes.DWORD),
        ("nSize", wintypes.DWORD),
        ("rclBounds", _RECT),
        ("rclFrame", _RECT),
        ("dSignature", wintypes.DWORD),
        ("nVersion", wintypes.DWORD),
        ("nBytes", wintypes.DWORD),
        ("nRecords", wintypes.DWORD),
        ("nHandles", wintypes.WORD),
        ("sReserved", wintypes.WORD),
        ("nDescription", wintypes.DWORD),
        ("offDescription", wintypes.DWORD),
        ("nPalEntries", wintypes.DWORD),
        ("szlDevice", wintypes.LONG * 2),
        ("szlMillimeters", wintypes.LONG * 2),
        ("cbPixelFormat", wintypes.DWORD),
        ("offPixelFormat", wintypes.DWORD),
        ("bOpenGL", wintypes.DWORD),
        ("szlMicrometers", wintypes.LONG * 2),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _bind():
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    gdi32.SetEnhMetaFileBits.argtypes = [wintypes.UINT, ctypes.c_char_p]
    gdi32.SetEnhMetaFileBits.restype = ctypes.c_void_p
    gdi32.GetEnhMetaFileHeader.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
    gdi32.GetEnhMetaFileHeader.restype = wintypes.UINT
    gdi32.DeleteEnhMetaFile.argtypes = [ctypes.c_void_p]
    gdi32.DeleteEnhMetaFile.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.GetDeviceCaps.restype = ctypes.c_int
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.CreateSolidBrush.restype = ctypes.c_void_p
    user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_void_p]
    user32.FillRect.restype = ctypes.c_int
    gdi32.PlayEnhMetaFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT)]
    gdi32.PlayEnhMetaFile.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = wintypes.BOOL
    return gdi32, user32


def render_emf_to_png(data: bytes, max_dimension: int = _MAX_DIMENSION) -> bytes | None:
    """Best-effort EMF → PNG.  ``None`` on any failure or non-Windows hosts."""
    if not _IS_WINDOWS or not data:
        return None
    gdi32, user32 = _bind()
    hemf = None
    dc = None
    bitmap = None
    old = None
    brush = None
    try:
        hemf = gdi32.SetEnhMetaFileBits(len(data), data)
        if not hemf:
            return None

        header = _ENHMETAHEADER()
        if not gdi32.GetEnhMetaFileHeader(hemf, ctypes.sizeof(header), ctypes.byref(header)):
            return None
        frame = header.rclFrame
        width_mm = abs(frame.right - frame.left) / 100.0
        height_mm = abs(frame.bottom - frame.top) / 100.0
        if width_mm <= 0 or height_mm <= 0:
            frame = header.rclBounds
            width_mm = abs(frame.right - frame.left) / 2540.0
            height_mm = abs(frame.bottom - frame.top) / 2540.0
        if width_mm <= 0 or height_mm <= 0:
            return None

        dc = gdi32.CreateCompatibleDC(None)
        if not dc:
            return None
        dpi = gdi32.GetDeviceCaps(dc, _LOGPIXELSX) or 96
        width = max(1, min(max_dimension, int(round(width_mm / 25.4 * dpi))))
        height = max(1, min(max_dimension, int(round(height_mm / 25.4 * dpi))))

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down, matching Pillow's raw rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(info), _DIB_RGB_COLORS,
                                        ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            return None
        old = gdi32.SelectObject(dc, bitmap)
        rect = _RECT(0, 0, width, height)
        # EMF drawing does not paint the background itself.
        brush = gdi32.CreateSolidBrush(0x00FFFFFF)
        user32.FillRect(dc, ctypes.byref(rect), brush)
        if not gdi32.PlayEnhMetaFile(dc, hemf, ctypes.byref(rect)):
            return None

        raw = ctypes.string_at(bits, width * height * 4)
        try:
            from PIL import Image
        except Exception:
            return None
        image = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return None
    finally:
        if old and dc:
            gdi32.SelectObject(dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if brush:
            gdi32.DeleteObject(brush)
        if dc:
            gdi32.DeleteDC(dc)
        if hemf:
            gdi32.DeleteEnhMetaFile(hemf)
