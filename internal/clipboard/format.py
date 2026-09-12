"""Clipboard content format definitions."""

import enum
import hashlib
import re
import struct
from dataclasses import dataclass, field


def strip_html(text: str) -> str:
    """Remove HTML tags, style/script blocks, comments, and unescape entities.

    Single source of truth for turning an HTML payload into visible text —
    every history/preview path shares it so the same payload renders the same
    preview wherever it is displayed.
    """
    import html as _html

    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    plain = re.sub(r"<[^>]*>", "", text)
    plain = _html.unescape(plain)
    plain = re.sub(r"\s+", " ", plain)
    return plain.strip()


class ContentType(enum.Enum):
    TEXT = 1
    HTML = 2
    RTF = 3
    IMAGE_PNG = 4
    IMAGE_EMF = 5  # Windows Enhanced Metafile (vector)
    FILE = 6  # File paths (CF_HDROP on Windows, NSFilenamesPboardType on macOS)
    URL = 7  # URL / URI (public.url on macOS, text/uri-list on Linux)


@dataclass
class ClipboardContent:
    """A snapshot of clipboard content, possibly containing multiple formats."""

    types: dict[ContentType, bytes] = field(default_factory=dict)
    source_device: str = ""
    timestamp: float = 0.0
    image_fmt: str = ""  # "png", "tiff", "bmp", "" = legacy/unknown
    # How a remote clip reached this device: "lan" for a peer on a direct
    # connection, "relay" for one that came through the internet relay, "web"
    # for a push from this machine's own web server, and "" when there is
    # nothing to say — the clip was captured here, or the row predates the
    # column.  History keeps it so a row can name the route it arrived on
    # rather than only the device it came from, which is the pair a reader
    # needs to tell "the machine next to me sent this" from "this came in over
    # the internet" from "I pushed this from a browser"; see
    # `internal.application.use_cases.history`.
    transport: str = ""

    def hash_key(self) -> str:
        """Content-based dedup key."""
        h = hashlib.sha256()
        for t in sorted(self.types.keys(), key=lambda x: x.value):
            h.update(struct.pack(">I", t.value))
            h.update(self.types[t])
        return h.hexdigest()

    def is_empty(self) -> bool:
        return len(self.types) == 0

    def best_format(self) -> tuple[ContentType, bytes] | None:
        """Return the best available format.

        Priority: HTML > EMF (vector) > RTF > TEXT > FILE > URL > IMAGE_PNG (raster)
        Text-based formats rank above raster images so editable content
        is preferred for paste. EMF sits between HTML and RTF because
        it preserves editable vector shapes.
        """
        for fmt in (
            ContentType.HTML,
            ContentType.IMAGE_EMF,
            ContentType.RTF,
            ContentType.TEXT,
            ContentType.FILE,
            ContentType.URL,
            ContentType.IMAGE_PNG,
        ):
            if fmt in self.types:
                return fmt, self.types[fmt]
        return None


@dataclass
class SyncMessage:
    """Message exchanged between peers."""

    content: ClipboardContent
    msg_id: str = ""
    source_device: str = ""
