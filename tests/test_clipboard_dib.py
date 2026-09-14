"""The byte shapes `clipboard_windows` publishes, held without a clipboard.

`internal.clipboard.dib` and `format.png_payload` exist so that this part of
the Windows write path can be tested at all: the writer itself calls Win32 and
needs a live clipboard, but the arithmetic it feeds those calls is where a
mistake hides.  A DIB whose header disagrees with its pixels still pastes —
just wrong, mirrored, or with the alpha silently dropped — so the invariants
below are about the header and the pixel offset agreeing with each other, not
about a function returning a value.
"""

import struct

import pytest

from internal.clipboard.dib import (
    BI_BITFIELDS,
    BI_RGB,
    BITMAPINFOHEADER_SIZE,
    BITMAPV5HEADER_SIZE,
    CHANNEL_MASKS,
    LCS_WINDOWS_COLOR_SPACE,
    as_bitmapinfoheader,
    bitmapv5_header,
    carries_alpha,
    header_size,
)
from internal.clipboard.format import PNG_SIGNATURE, png_payload

BODY = bytes(range(256)) * 4  # 1024 bytes: 8×32 pixels of BGRA


def v5_dib(width=8, height=32):
    return bitmapv5_header(width, height) + BODY


def test_the_header_declares_its_own_length():
    """The field a consumer reads the pixel offset out of.

    Every consumer computes "pixels start at the header's end" from this one
    field, so a header that names a length it does not have moves the pixels
    for every reader at once.
    """
    header = bitmapv5_header(8, 32)

    assert len(header) == BITMAPV5HEADER_SIZE
    assert header_size(header) == len(header)
    # A V5 header is a BITMAPINFOHEADER (40 bytes) with the V5 tail after it,
    # which is what lets `as_bitmapinfoheader` cut it at 40.
    assert struct.calcsize("<IiiHHIIiiII") == BITMAPINFOHEADER_SIZE


def test_the_header_names_the_width_height_depth_and_alpha_mask():
    header = bitmapv5_header(8, 32)

    assert struct.unpack_from("<ii", header, 4) == (8, -32)
    assert struct.unpack_from("<HH", header, 12) == (1, 32)
    assert struct.unpack_from("<I", header, 16)[0] == BI_BITFIELDS
    assert struct.unpack_from("<I", header, 20)[0] == 8 * 32 * 4  # bV5SizeImage
    assert struct.unpack_from("<IIII", header, 40) == CHANNEL_MASKS
    assert struct.unpack_from("<I", header, 56)[0] == LCS_WINDOWS_COLOR_SPACE


def test_the_pixels_begin_exactly_where_the_header_ends():
    """The invariant the whole module is arranged around.

    A V5 header keeps its channel masks inside itself, so there is no mask
    array between header and pixels — which is only true as long as the header
    is exactly as long as it says it is.
    """
    dib = v5_dib()

    assert dib[header_size(dib) :] == BODY


def test_a_top_down_image_is_a_negative_height():
    """Pillow hands over rows top-first; a positive height would mirror them."""
    assert struct.unpack_from("<i", bitmapv5_header(4, 9), 8)[0] == -9
    assert struct.unpack_from("<i", bitmapv5_header(4, 9, top_down=False), 8)[0] == 9


def test_dimensions_that_are_not_dimensions_are_refused():
    """Better a refused write than a header no consumer can parse."""
    for width, height in ((0, 4), (4, 0), (-4, 4)):
        with pytest.raises(ValueError):
            bitmapv5_header(width, height)


def test_alpha_is_claimed_only_when_the_header_says_so():
    """Both halves are required, and neither is enough alone.

    A V5 header sized for 32 bits whose alpha mask is zero still means XRGB —
    publishing it as CF_DIBV5 would promise transparency the sender never
    offered.
    """
    assert carries_alpha(v5_dib())

    no_alpha = bytearray(v5_dib())
    struct.pack_into("<I", no_alpha, 52, 0)  # bV5AlphaMask
    assert not carries_alpha(bytes(no_alpha))

    v3 = as_bitmapinfoheader(v5_dib())
    assert not carries_alpha(v3)

    # 24-bit V5: a header long enough, and still no alpha byte to speak of.
    sixteen_bit = bytearray(v5_dib())
    struct.pack_into("<H", sixteen_bit, 14, 24)
    assert not carries_alpha(bytes(sixteen_bit))


def test_a_payload_too_short_to_hold_a_header_is_not_one():
    """Both are asked of whatever arrived, including a truncated capture."""
    assert header_size(b"") == 0
    assert header_size(b"\x7c\x00") == 0
    assert not carries_alpha(b"")
    assert not carries_alpha(b"\x7c\x00\x00\x00")


def test_downgrading_keeps_every_pixel_and_drops_only_the_claim():
    """What a consumer that reads CF_DIB and knows nothing of V5 receives.

    The alpha byte is ignored there by definition, so the pixels cross
    untouched; only the header stops saying they mean something.
    """
    dib = v5_dib()
    flat = as_bitmapinfoheader(dib)

    assert header_size(flat) == BITMAPINFOHEADER_SIZE
    assert len(flat) == BITMAPINFOHEADER_SIZE + len(BODY)
    assert struct.unpack_from("<ii", flat, 4) == (8, -32)
    assert struct.unpack_from("<H", flat, 14)[0] == 32
    # BI_RGB, not BI_BITFIELDS: masks outside the header would need a 16-byte
    # array before the pixels, and the pixels are already past the header.
    assert struct.unpack_from("<I", flat, 16)[0] == BI_RGB
    assert struct.unpack_from("<I", flat, 20)[0] == len(BODY)
    assert flat[BITMAPINFOHEADER_SIZE:] == BODY


def test_a_dib_that_is_already_short_comes_back_untouched():
    """An image that never had an alpha channel is not rewritten."""
    already = as_bitmapinfoheader(v5_dib())

    assert as_bitmapinfoheader(already) is already


def test_the_png_format_only_ever_gets_a_png():
    """The registered ``PNG`` format carries a PNG file, or it carries nothing.

    Publishing a payload there that is not a PNG would be worse than skipping
    it: the format's name is a promise about the bytes, and an application that
    asks for it trusts that promise over any sniffing of its own.
    """
    assert png_payload(PNG_SIGNATURE + BODY) == PNG_SIGNATURE + BODY
    assert png_payload(b"BM" + BODY) is None
    assert png_payload(b"GIF89a" + BODY) is None
    assert png_payload(PNG_SIGNATURE[:-1] + BODY) is None  # seven of eight bytes
    assert png_payload(b"") is None
