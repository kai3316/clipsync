"""Clipboard content format definitions."""

import enum
import hashlib
import re
import struct
from dataclasses import dataclass, field, replace


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


# The eight bytes every PNG file opens with.  A payload that starts with them
# is a whole PNG file, which is what Windows' registered ``PNG`` clipboard
# format carries — the sender's own bytes, not a re-encode of them.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_payload(data: bytes) -> bytes | None:
    """``data`` when it already is a PNG file, else None.

    Asked before any conversion, because publishing a PNG under the registered
    ``PNG`` format is a copy: an image that came from a macOS or Linux peer
    goes back out byte for byte, and the trip through this machine costs it
    nothing — not a decode, not a re-encode, not a colour profile.
    """
    return data if data[: len(PNG_SIGNATURE)] == PNG_SIGNATURE else None


def split_paths(raw: str) -> list[str]:
    """The paths in a stored FILE payload: one per line, blanks dropped.

    All three platforms normalise to this before they store, so it is the
    stored shape rather than a guess at one: Windows flattens ``CF_HDROP`` and
    joins with newlines, macOS joins the ``public.file-url`` paths, and Linux
    converts ``text/uri-list`` URIs to paths and joins them the same way —
    which is also what every writer expects back.  A payload that is not that
    shape (an older row, a hand-edited backup) yields nothing here.

    Shared so that everything reading a file list reads it identically: the
    history preview, the encoder that turns an entry into an offer for another
    device, and anything that has to count the files in one.
    """
    return [line.strip() for line in raw.split("\n") if line.strip()]


class ContentType(enum.Enum):
    TEXT = 1
    HTML = 2
    RTF = 3
    IMAGE_PNG = 4
    IMAGE_EMF = 5  # Windows Enhanced Metafile (vector)
    FILE = 6  # File paths (CF_HDROP on Windows, NSFilenamesPboardType on macOS)
    URL = 7  # URL / URI (public.url on macOS, text/uri-list on Linux)
    # A file that lives on *another* device: name, size, and the id of the entry
    # that published it — never a path, because a path from the peer's machine
    # would mean nothing here.  Distinct from FILE rather than a flag on it: a
    # FILE payload is a copy instruction and this is not, so every consumer that
    # walks the enum (the paste path, the writer dispatch, the filter) gets the
    # distinction for free instead of having to ask about it.  See
    # `internal.clipboard.file_ref` for the payload.
    FILE_REMOTE = 8


# Types that are a history row and nothing else.  No platform's clipboard can
# hold a reference to a file it does not have, so these are dropped before a
# received clip is written to the local clipboard — writing one through would
# clear the clipboard and then report success, the exact failure the Windows URL
# branch used to have.  A clip that carries only these is history-only: nothing
# is written, and that is a success rather than a write that produced nothing.
HISTORY_ONLY_TYPES = frozenset({ContentType.FILE_REMOTE})


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
    # The history entry this clip was stored as, when it was captured here.  A
    # file entry travels as an offer naming this id, and a peer's request is
    # matched back against it — which is what lets the machine that *owns* the
    # file, rather than the one asking for it, decide which paths a request may
    # resolve to.  Empty for a clip that was never stored, and for one that
    # arrived from a peer (its id belongs to the other machine's history).
    entry_id: str = ""

    def hash_key(self) -> str:
        """Content-based dedup key."""
        h = hashlib.sha256()
        for t in sorted(self.types.keys(), key=lambda x: x.value):
            h.update(struct.pack(">I", t.value))
            h.update(self.types[t])
        return h.hexdigest()

    def is_empty(self) -> bool:
        return len(self.types) == 0

    def without(self, types) -> "ClipboardContent":
        """A copy of this clip with ``types`` left out, every other field kept.

        What separates content that can live on a clipboard from content that
        can only live in history: a clip naming a file on another device has to
        reach the history row while never reaching the writer, which would clear
        the clipboard and then report success for having written nothing.
        ``replace`` rather than a hand-built copy so a field added later cannot
        be dropped by this one silently.

        The clip itself comes back when there was nothing to leave out, rather
        than an identical copy: this runs on every remote clip before the write,
        where the ordinary case drops nothing, and the receiver's own checks
        compare the written object against the message it came from.
        """
        kept = {t: d for t, d in self.types.items() if t not in types}
        if len(kept) == len(self.types):
            return self
        return replace(self, types=kept)

    def best_format(self) -> tuple[ContentType, bytes] | None:
        """Return the best available format.

        Priority: HTML > EMF (vector) > RTF > TEXT > FILE > URL > IMAGE_PNG
        (raster) > FILE_REMOTE.  Text-based formats rank above raster images so
        editable content is preferred for paste.  EMF sits between HTML and RTF
        because it preserves editable vector shapes.

        FILE_REMOTE ranks last, and that is not a judgement about it: nothing
        ranks below it, so last means "only when it is the whole clip", which is
        the only time it is there.  It has to be *in* the list at all because
        this is what labels a row: a clip that is only an offer would otherwise
        have no best format, and the history store drops a clip with none —
        leaving the row the user is supposed to be able to download from
        nowhere to be found.
        """
        for fmt in (
            ContentType.HTML,
            ContentType.IMAGE_EMF,
            ContentType.RTF,
            ContentType.TEXT,
            ContentType.FILE,
            ContentType.URL,
            ContentType.IMAGE_PNG,
            ContentType.FILE_REMOTE,
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
