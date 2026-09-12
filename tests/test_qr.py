"""The QR image the native Companion panel embeds."""

import base64
import builtins
from io import BytesIO

import pytest
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


def test_png_data_url_changes_with_the_text():
    assert png_data_url("http://a/mobile.html") != png_data_url("http://b/mobile.html")


def test_png_data_url_rejects_empty_text():
    with pytest.raises(ValueError):
        png_data_url("")


def test_png_data_url_reports_a_missing_qr_stack(monkeypatch):
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "qrcode":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError):
        png_data_url("http://example.com/mobile.html")
