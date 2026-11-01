"""Vector clips must still have a picture the phone can draw."""

import ctypes
import sys
from ctypes import wintypes

import pytest

from internal.clipboard.format import ContentType
from internal.clipboard.history_db import _build_preview

WINDOWS = sys.platform == "win32"


def _emf_bytes() -> bytes:
    gdi32 = ctypes.windll.gdi32
    gdi32.CreateEnhMetaFileW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPCWSTR,
    ]
    gdi32.CreateEnhMetaFileW.restype = ctypes.c_void_p
    gdi32.Rectangle.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    gdi32.Ellipse.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    gdi32.CloseEnhMetaFile.argtypes = [ctypes.c_void_p]
    gdi32.CloseEnhMetaFile.restype = ctypes.c_void_p
    gdi32.GetEnhMetaFileBits.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_char_p]
    gdi32.GetEnhMetaFileBits.restype = wintypes.UINT
    gdi32.DeleteEnhMetaFile.argtypes = [ctypes.c_void_p]

    dc = gdi32.CreateEnhMetaFileW(None, None, None, None)
    gdi32.Rectangle(dc, 10, 10, 300, 200)
    gdi32.Ellipse(dc, 40, 40, 260, 160)
    handle = gdi32.CloseEnhMetaFile(dc)
    try:
        size = gdi32.GetEnhMetaFileBits(handle, 0, None)
        buffer = ctypes.create_string_buffer(size)
        gdi32.GetEnhMetaFileBits(handle, size, buffer)
        return buffer.raw[:size]
    finally:
        gdi32.DeleteEnhMetaFile(handle)


@pytest.mark.skipif(not WINDOWS, reason="EMF is a Windows clipboard format")
def test_a_vector_clip_is_rendered_to_a_png_beside_the_vector():
    from internal.clipboard.emf import render_emf_to_png

    png = render_emf_to_png(_emf_bytes())
    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 100


def test_a_row_with_both_formats_prefers_the_bitmap_label():
    # The vector remains for a rich paste, but the row should say 图片, not
    # 矢量图: that is the label the phone will be able to show.
    preview = _build_preview({
        ContentType.IMAGE_EMF: b"not a real emf",
        ContentType.IMAGE_PNG: b"not a real png",
    })
    assert preview == "[Image]"


def test_garbage_emf_is_none_rather_than_an_exception():
    from internal.clipboard.emf import render_emf_to_png

    assert render_emf_to_png(b"garbage") is None
    assert render_emf_to_png(b"") is None
