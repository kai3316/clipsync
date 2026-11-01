"""QR codes as data URLs — the image the Companion panel embeds.

``qrcode`` and Pillow are already runtime dependencies (the legacy dashboard and
settings window draw the same codes), so the native panel reuses them instead of
shipping a second QR implementation in JavaScript.
"""

import base64
from io import BytesIO

#: Matches the size the legacy tray dialog encodes.
DEFAULT_SIZE = 220


def png_data_url(text: str, size: int = DEFAULT_SIZE) -> str:
    """Return *text* as a ``data:image/png;base64,...`` QR code.

    Raises ``ValueError`` for empty text and ``RuntimeError`` when the
    qrcode/Pillow stack is unavailable, so a caller can render
    "(QR unavailable)" instead of failing the whole panel.
    """
    if not text:
        raise ValueError("QR text is empty")
    try:
        import qrcode
        from PIL import Image
    except Exception as exc:  # pragma: no cover - depends on the install
        raise RuntimeError("QR support is unavailable") from exc
    image = qrcode.make(text).convert("RGB").resize((size, size), Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
