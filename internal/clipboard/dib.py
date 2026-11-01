"""Windows DIB byte layouts, built and rewritten without a clipboard.

`clipboard_windows` publishes images as a *DIB* — a header with the pixels
immediately after it and no BMP file header.  Two shapes matter and they are
not interchangeable:

* ``CF_DIB`` is documented as carrying no alpha.  A 32-bit CF_DIB is XRGB: the
  fourth byte of every pixel is undefined and consumers ignore it.
* ``CF_DIBV5`` carries a ``BITMAPV5HEADER`` whose ``bV5AlphaMask`` says that
  byte *is* alpha.  That declaration is the whole difference: without it a
  transparent PNG pastes as whatever colour sits under a zero alpha.

The masks live *inside* a V5 header, so its pixels start at offset 124.  A
shorter header under ``BI_BITFIELDS`` would need a separate 16-byte mask array
before the pixels, which is why the downgrade below sets ``BI_RGB`` instead —
the pixel offset stays put and the layout is the one every consumer agrees on.

This module is the arithmetic only: no ctypes, no clipboard, importable and
testable on any platform.  A DIB whose header disagrees with its pixels still
pastes, just wrong, which is exactly the kind of bug a test has to hold —
so the shapes live here and `clipboard_windows` does the Windows calls.
"""

import struct

BITMAPINFOHEADER_SIZE = 40
BITMAPV5HEADER_SIZE = 124

BI_RGB = 0
BI_BITFIELDS = 3

# 'sRGB', as a big-endian four-character code.  What every Windows app that
# writes a V5 header writes, and what tells a consumer the pixels are not
# premultiplied — the contract the alpha mask rides on.
LCS_WINDOWS_COLOR_SPACE = 0x73524742

# Red, green, blue, alpha, in the order BITMAPV5HEADER stores them.  Their byte
# order in memory is BGRA, which is what `Image.tobytes("raw", "BGRA")` emits —
# so the pixel buffer feeds the header without a channel shuffle.
CHANNEL_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)


def header_size(dib: bytes) -> int:
    """The DIB's own header size, or 0 when it is too short to read one."""
    if len(dib) < 4:
        return 0
    return struct.unpack_from("<I", dib, 0)[0]


def carries_alpha(dib: bytes) -> bool:
    """Whether this DIB declares an alpha channel.

    Both halves matter and neither is enough alone: a V5 header sized for 32
    bits whose alpha mask is all zeroes is a V5 header that still means XRGB,
    and writing one as CF_DIBV5 would claim alpha the sender never promised.
    """
    if header_size(dib) < BITMAPV5HEADER_SIZE or len(dib) < BITMAPV5HEADER_SIZE:
        return False
    bit_count = struct.unpack_from("<H", dib, 14)[0]
    alpha_mask = struct.unpack_from("<I", dib, 52)[0]
    return bit_count == 32 and alpha_mask != 0


def bitmapv5_header(width: int, height: int, top_down: bool = True) -> bytes:
    """A 124-byte ``BITMAPV5HEADER`` for a 32-bit BGRA image.

    ``top_down`` writes a negative height, which is legal for ``BI_RGB`` and
    lets a top-down pixel buffer go in untouched — no row flipping, which is
    the step that silently mirrors an image when it is got wrong.

    ``bV5SizeImage`` is filled in because the header claims ``BI_BITFIELDS``,
    and a consumer is entitled to trust it over arithmetic of its own.
    """
    if width <= 0 or height <= 0:
        raise ValueError("DIB dimensions must be positive")
    red, green, blue, alpha = CHANNEL_MASKS
    return struct.pack(
        "<IiiHHIIiiII"  # the BITMAPINFOHEADER fields, declaring 124 bytes
        "IIII"  # the four channel masks
        "I"  # colour space
        "iiiiiiiii"  # endpoints: three CIEXYZ triples
        "III"  # gamma, per channel
        "IIII",  # render intent, profile offset, profile size, reserved
        BITMAPV5HEADER_SIZE,
        width,
        -height if top_down else height,
        1,
        32,
        BI_BITFIELDS,
        width * height * 4,
        0,
        0,
        0,
        0,
        red,
        green,
        blue,
        alpha,
        LCS_WINDOWS_COLOR_SPACE,
        0, 0, 0, 0, 0, 0, 0, 0, 0,  # endpoints
        0, 0, 0,  # gamma
        0, 0, 0, 0,  # intent, profile offset, profile size, reserved
    )


def as_bitmapinfoheader(dib: bytes) -> bytes:
    """The same DIB under a 40-byte ``BITMAPINFOHEADER``.

    For a consumer that reads CF_DIB and knows nothing of V5.  The alpha byte
    is ignored there by definition, so the pixels cross untouched and only the
    header stops claiming they mean something.

    A DIB that is already 40 bytes comes back unchanged: rewriting a header
    that was never longer would be a change with no reader behind it.
    """
    size = header_size(dib)
    if size <= BITMAPINFOHEADER_SIZE:
        return dib
    head = bytearray(dib[:BITMAPINFOHEADER_SIZE])
    width, height = struct.unpack_from("<ii", head, 4)
    struct.pack_into("<I", head, 0, BITMAPINFOHEADER_SIZE)  # biSize
    struct.pack_into("<I", head, 16, BI_RGB)  # biCompression
    struct.pack_into("<I", head, 20, width * abs(height) * 4)  # biSizeImage
    return bytes(head) + dib[size:]
