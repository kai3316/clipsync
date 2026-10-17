"""The QR image the native Companion panel embeds.

One test: the phone scanners are the consumers, so the payload's shape is the
contract — a data-URL PNG of the requested size, in RGB (indexed PNGs are
rejected by some scanners).
"""

import base64
from io import BytesIO

from PIL import Image

from internal.system.qr import png_data_url


def test_png_data_url_is_a_decodable_png_of_the_requested_size():
    data_url = png_data_url("http://192.168.1.5:8080/mobile.html?token=abc", size=120)
    assert data_url.startswith("data:image/png;base64,")
    image = Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
    assert image.format == "PNG"
    assert image.size == (120, 120)
    # RGB, not a palette mode: some phone scanners reject indexed PNGs.
    assert image.mode == "RGB"
