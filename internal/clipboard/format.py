"""Clipboard content format definitions."""

import enum
import hashlib
import re
import struct
from dataclasses import dataclass, field, replace
from urllib.parse import unquote_to_bytes, urlparse


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


def encode_path(path: str) -> bytes:
    """One path as the bytes a stored FILE payload holds.

    ``surrogateescape``, spelled out rather than left to ``os.fsencode``, for a
    reason that only shows on Windows: ``os.fsdecode``/``os.fsencode`` follow
    ``sys.getfilesystemencodeerrors()``, which is ``surrogateescape`` on POSIX
    but ``surrogatepass`` on Windows — where decoding a name that is not valid
    UTF-8 *raises* instead of spelling it, and encoding one back produces the
    surrogate's own bytes rather than the byte it stands for.  So ``os.`` is
    neither lossless on every platform nor even usable on one of them, and a
    payload written on one machine may well be read on another — a restored
    backup is the plain case, and this machine's own tests read a Mac's capture
    on Windows.  With the rule spelled out, the bytes survive wherever they are
    read and only the *reading* of a name is a guess.
    """
    return path.encode("utf-8", "surrogateescape")


def decode_path(raw: bytes) -> str:
    """The inverse of `encode_path`: a path a file system call will resolve.

    Bytes that are not valid UTF-8 come back as surrogates, which is what makes
    this lossless and what lets ``os.stat`` find such a file at all.  What it
    does *not* give is something to show a user — a surrogate is not a
    character — so a name meant for display goes through
    `file_ref.file_name`, which reads it back the way it reads any other
    non-UTF-8 clipboard text.
    """
    return raw.decode("utf-8", "surrogateescape")


def encode_paths(paths: list[str]) -> bytes:
    """A stored FILE payload: one absolute path per line."""
    return encode_path("\n".join(paths))


def decode_paths(raw: bytes) -> list[str]:
    """The paths in a stored FILE payload, by the rules above."""
    return split_paths(decode_path(raw))


# An RFC 3986 scheme at the start of a line, and the one spelling that looks
# like a scheme but is a path.  Both are only asked of a line that is not a
# ``file:`` address; see `file_url_paths`.
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def file_url_paths(raw: bytes) -> list[str]:
    """The local paths named by the bytes a pasteboard publishes for a file.

    One definition for two readers, because macOS's ``public.file-url`` and
    Linux's ``text/uri-list`` are the same thing written by different hands: a
    ``file://`` address per file, one per line.  Three shapes are accepted,
    because between the platforms and their fallbacks all three turn up — a
    percent-encoded ``file://`` URL, a bare path (``pbpaste -Prefer`` answers
    with one when the type it was asked for is absent, and some file managers
    write one into a URI list), and a ``#`` comment, which the ``text/uri-list``
    spec allows and which is skipped.  Anything carrying a scheme of its own is
    dropped here: an ``https://`` URI has a path too, and one read as a local
    file is a ghost reference at the far end.

    Percent-decoding happens on *bytes*, and the path is decoded by
    `decode_path`, both on purpose.  ``unquote`` decodes as UTF-8 and replaces
    what it cannot read, so a name in any other encoding becomes U+FFFD before
    this machine has looked at the file — and the path it then spells matches
    nothing, so a copy with a perfectly good file underneath it produces no
    offer at all and the peer is left with a file it cannot download.  The URL
    is split as text only to find the path within it; the path itself goes back
    to bytes, so what comes out is the bytes the file is really named.

    An address naming another machine (``file://nas/share/x``) is refused
    rather than reduced to ``/share/x``: that is a path on *that* machine, and
    keeping it would stat an unrelated local file or, worse, serve one.
    """
    paths = []
    for line in raw.decode("utf-8", "surrogateescape").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("file:"):
            # A bare path, taken as the bytes it is: no percent-decoding, since
            # a ``%`` in a path spelled this way is a character in the name.
            # ``urlparse`` is not asked about it either — it would take a ``?``
            # or ``#`` in the name for a query, and a Linux file may have one.
            #
            # A URI of another scheme is dropped, but ``C:\x`` is not one: a
            # drive letter and a one-character scheme are spelled the same, so
            # the second pattern is what tells a Windows path from an address.
            if _URI_SCHEME.match(line) and not _WINDOWS_DRIVE.match(line):
                continue
            paths.append(line)
            continue
        parsed = urlparse(line)
        if parsed.netloc not in ("", "localhost"):
            continue
        # Back to the bytes the URL spelled, so the percent-decoding below is
        # the only translation of them.
        path = decode_path(unquote_to_bytes(encode_path(parsed.path)))
        if path:
            paths.append(path)
    return paths


def decode_text(data: bytes) -> str:
    """Decode clipboard text bytes, which are not guaranteed to be UTF-8.

    The wire carries TEXT as bytes, not as a string — ``encode_message``
    base64s the payload rather than re-encoding it — so whatever the capturing
    platform handed over arrives intact, and on two of the three that is not
    always UTF-8.  macOS's ``pbpaste`` converts to the *system* encoding, which
    on a zh_CN Mac is GBK, and a Windows legacy application writes ``CF_TEXT``
    in the ANSI code page.  A file name copied in the Finder on such a Mac
    therefore reaches this code as GBK bytes.

    Decoding those as UTF-8 with ``errors="replace"`` does not merely show them
    wrong, it destroys them: every byte the decoder cannot use becomes U+FFFD,
    which is a character like any other — it is written back to a clipboard,
    stored in a row, and synced on.  That is the whole of the "history shows
    ���֤������.pdf" report: a GBK name turned into U+FFFD by the Windows
    writer, kept that way by the poll that read its own clipboard back.

    UTF-8 is self-validating, which makes a strict attempt a reliable detector:
    real GBK text almost never decodes cleanly as UTF-8.  The CJK candidates
    follow and cover what the three platforms actually emit.  ``latin-1`` is
    last, and it is the one that always answers — it maps every byte to a code
    point, so anything below it is unreachable.  That is the point of having it:
    a byte it reads can be written back out, where the U+FFFD an
    ``errors="replace"`` leaves behind cannot be turned back into the original
    text at all.  Text read this way shows as accented Latin rather than as
    replacement characters, and either way it is recoverable; only the lossy
    read is not.

    One definition, so a payload reads the same everywhere it is read: the
    stored preview, the text a window copies back out, and the bytes handed to
    this machine's own clipboard writer.
    """
    return decode_text_with_encoding(data)[0]


def decode_text_with_encoding(data: bytes) -> tuple[str, str]:
    """`decode_text`, plus the name of the encoding that read it.

    Split out so a reader can say *what* it had to guess at — the one detail
    that tells a report of a garbled clipboard apart from a guess that was
    wrong — without a second decode pass or a re-implementation of the order
    below.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # Very old entries (or a peer platform that did not normalise) may
        # store wide text raw.  Without this the bytes fall through to the
        # CJK attempts and render as mojibake — the "history became garbled
        # after update" report.
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("gbk", "gb2312", "gb18030", "big5", "shift-jis", "euc-kr", "latin-1"):
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    # Unreachable: latin-1 decodes any byte string.  Kept so that editing the
    # list above cannot turn this function into one that returns None.
    return data.decode("utf-8", errors="replace"), "utf-8/replace"


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

        Priority: HTML > EMF (vector) > RTF > FILE > FILE_REMOTE > TEXT > URL >
        IMAGE_PNG (raster).  Text-based formats rank above raster images so
        editable content is preferred for paste.  EMF sits between HTML and RTF
        because it preserves editable vector shapes.

        A file ranks above TEXT, and that is not a preference between the two:
        a file copy arrives as both on every platform that publishes a file, and
        the file is the *whole* of what was copied.  macOS is the clearest case
        — the Finder puts the name on the pasteboard as plain text beside the
        file and its ``file://`` address, so a copy of 报告.pdf reaches a peer as
        TEXT "报告.pdf" plus the offer — and the ranking is what decides whether
        that row is a file row or a text row.  It has to be a file row: the row's
        kind is what the window reads to choose between 复制 and 下载, so ranking
        TEXT first hid the download button on a row that could be downloaded.

        FILE_REMOTE sits with FILE rather than last, for the same reason and one
        more of its own.  It used to rank below everything, on the assumption
        that an offer is only ever the whole clip; a Mac file copy disproves that
        by carrying the name as text too.  It still has to be *in* the list at
        all because this is what labels a row: an offer-only clip would
        otherwise have no best format, and the history store drops a clip with
        none — leaving the row the user is meant to download from nowhere to be
        found.
        """
        for fmt in (
            ContentType.HTML,
            ContentType.IMAGE_EMF,
            ContentType.RTF,
            ContentType.FILE,
            ContentType.FILE_REMOTE,
            ContentType.TEXT,
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
