"""History DTOs deliberately exclude raw clipboard payloads and source paths.

``text`` is the one exception, and a deliberate one: it reads back the words the
user copied, not the stored base64 bag, because a window that wants to show or
translate a whole clip has no other way to get them — the list ships a cut-down
preview and the clipboard is already holding something else.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from collections.abc import Callable
from io import BytesIO
from typing import Protocol

from internal.application.errors import ApplicationError
from internal.clipboard import file_ref
from internal.clipboard.format import (
    HISTORY_ONLY_TYPES,
    ClipboardContent,
    ContentType,
    split_paths,
    strip_html,
)

logger = logging.getLogger(__name__)

#: ``source_device`` stamped on a clip the phone's panel pushed to this device.
#: The same marker ``internal.web.api.history.push_text`` writes, which is where
#: these rows come from; the phone is not a paired peer, so it has no device id
#: to be named by and gets a label of its own instead.
WEB_SOURCE = "__web__"
WEB_SOURCE_LABEL = "\U0001f4f1 Web"

#: Longest ``source_title`` a list response ships.  A window title is a browser
#: tab's or an editor's, so it is short in practice; a cap is here because the
#: title is chosen by whichever application happened to be in front and every
#: row of every page would otherwise carry it in full.
SOURCE_TITLE_LIMIT = 200

#: How long a hover card's picture may be on its longest edge.  A card is a
#: glance, not the image: past this the picture is the thing itself, and the
#: user who wants that has 复制 and the clipboard for it.
PREVIEW_EDGE = 480

#: What an encoded picture may weigh.  A frame is capped at 1 MiB by the IPC
#: profile and base64 costs a third on top, so this leaves the frame room for
#: its own keys.  A screenshot at card width lands far under it; a photograph
#: is what goes over, and is what the JPEG fallback below is for.
PREVIEW_BUDGET = 320 * 1024

#: How many files a card lists.  A clipboard entry can carry a directory's
#: worth of paths — ``file_ref.MAX_OFFER_ENTRIES`` is about what may cross the
#: wire, not about what fits under a cursor — so the list stops here and the
#: count carries the rest.
PREVIEW_FILES = 8

#: Largest source a card will read to make a picture of.  A pointer crossing a
#: row must not turn into a 200 MB read.
PREVIEW_SOURCE_BYTES = 32 * 1024 * 1024

#: The extensions a *file* clip is thumbnailed from.  A clip that carries a
#: picture as bytes is the image branch's business; this is the other way a
#: picture arrives in the history — copied in a file manager, so the entry
#: holds a path and the bytes are read here.
IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
)

#: The stored names a bitmap travels under.  ``IMAGE`` is what rows written
#: before the wire name settled carry; ``decode_formats`` maps the same pair.
IMAGE_NAMES = ("IMAGE_PNG", "IMAGE")

#: The empty card.  Returned for every entry there is nothing to show for, and
#: returned rather than raised — see ``preview``.
NO_PREVIEW: dict = {"kind": "", "image": "", "width": 0, "height": 0, "files": [], "total": 0}


def _bitmap_over_budget(stored: dict) -> bool:
    """Whether this entry's bitmap is larger than a hover may decode.

    Read off the stored base64 rather than off the decoded bytes, because the
    decode is the cost being avoided: an image clip's bitmap is the one member
    of the bag that can be tens of megabytes while the rest of it is kilobytes,
    and a pointer crossing a row cannot pay to decode all of it into memory in
    order to downscale one of it.
    """
    ceiling = PREVIEW_SOURCE_BYTES * 4 // 3 + 4
    return any(
        isinstance(stored.get(name), str) and len(stored[name]) > ceiling
        for name in IMAGE_NAMES
    )


def _encode_picture(picture) -> tuple[bytes, str]:
    """*picture* as bytes and the format they are in.

    PNG first, because the picture a hover card is usually asked about is a
    screenshot: flat colours, crisp edges, and small.  A photograph is the case
    PNG cannot hold inside the budget, and it is also the case where the loss
    does not show, so the fallback is a JPEG of the same pixels.
    """
    out = BytesIO()
    picture.save(out, format="PNG", optimize=True)
    if out.tell() <= PREVIEW_BUDGET:
        return out.getvalue(), "png"
    out = BytesIO()
    picture.convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue(), "jpeg"


def picture_of(data: bytes) -> tuple[str, int, int]:
    """*data* as a data URL a card can show, with the picture's own size.

    Empty and zeroes for anything this build cannot decode — a vector image, a
    bitmap in a container PIL does not read — which is not a failure: the card
    simply has no picture to add, and the row it is over says what kind of clip
    it is either way.  The size that comes back is the *source's*, so the card
    can caption a 4000 px screenshot as one after downscaling it.
    """
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
            keeps_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            scaled = image.convert("RGBA" if keeps_alpha else "RGB")
            scaled.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.LANCZOS)
            encoded, fmt = _encode_picture(scaled)
    except Exception:
        logger.debug("History preview could not be decoded", exc_info=True)
        return "", 0, 0
    return f"data:image/{fmt};base64,{base64.b64encode(encoded).decode('ascii')}", width, height


def _read_picture(path: str, limit: int) -> bytes:
    """At most *limit* bytes of *path*, or nothing if it cannot be read.

    Bounded rather than ``read()``: the size was checked against the same limit
    a moment ago, and a file growing between the two must not be able to turn a
    hover into an unbounded read.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except (OSError, ValueError):
        return b""


def _file_card(paths: list[str]) -> dict:
    """A file clip as a card: what the files are, and the picture if there is one.

    The paths are stat'd, never listed and never sent.  A history DTO carries no
    source paths — this module's own rule — and it holds here for a reason the
    rule already gives: the card is read, not acted on, and everything a reader
    would do with a path is already a row action (在文件夹中显示, 下载).  What the
    row cannot say and the card can is whether the file still exists, which is
    the answer to "why did this download fail" a user is most often after.
    """
    files = []
    for path in paths[:PREVIEW_FILES]:
        try:
            stat = os.stat(path)
        except (OSError, ValueError):
            files.append(
                {"name": file_ref.file_name(path), "size": 0, "kind": "file", "exists": False}
            )
            continue
        is_dir = os.path.isdir(path)
        files.append(
            {
                "name": file_ref.file_name(path),
                "size": 0 if is_dir else int(stat.st_size),
                "kind": "dir" if is_dir else "file",
                "exists": True,
            }
        )
    card = dict(NO_PREVIEW, kind="files", files=files, total=len(paths))
    # One path is the case a picture answers: a clip of a single file is how a
    # picture copied in a file manager arrives, and one path is also what keeps
    # the read bounded to the thing under the cursor rather than to a drop.
    if (
        len(paths) == 1
        and files[0]["exists"]
        and files[0]["kind"] == "file"
        and files[0]["size"] <= PREVIEW_SOURCE_BYTES
        and os.path.splitext(paths[0])[1].lower() in IMAGE_EXTS
    ):
        url, width, height = picture_of(_read_picture(paths[0], PREVIEW_SOURCE_BYTES))
        card["image"], card["width"], card["height"] = url, width, height
    return card


def _remote_file_card(payload: bytes) -> dict:
    """A file clip that lives on another device, as a card.

    There are no bytes to thumbnail and no path to stat — this machine holds the
    file's *description*, which is what ``file_ref`` put on the wire: a name, a
    size and whether it is a folder.  So the list is the whole card, and the
    sizes are the ones the publishing device reported rather than anything read
    here.
    """
    remote = file_ref.parse(payload)
    if remote is None:
        return dict(NO_PREVIEW)
    offered = remote.get("files") or []
    files = [
        {
            "name": str(item.get("name", "")),
            "size": int(item.get("size") or 0),
            "kind": "dir" if item.get("kind") == "dir" else "file",
            # Not "whether it is there" — it never was, on this machine — so the
            # card shows no missing marker for one of these.
            "exists": True,
        }
        for item in offered[:PREVIEW_FILES]
    ]
    total = int(remote.get("total") or len(offered))
    return dict(NO_PREVIEW, kind="files", files=files, total=total)


#: The kind chips the panel's history page offered, in the panel's order.
KINDS = ("all", "text", "image", "file", "link")

#: The two orders the panel's sort toggle switched between.
SORTS = ("newest", "oldest")

#: A link, as the panel's 链接 chip read it: a clip whose *text* is a URL.
_LINK = re.compile(r"^https?://", re.IGNORECASE)


def source_name(source_device: str, names: dict, local_name: str) -> str:
    """The name a row's ``source_device`` is shown as.

    ``names`` maps a device id to the name the user knows it by.  A clip
    captured locally carries an *empty* ``source_device`` — the clipboard
    monitor never stamps this device's own id — so an unknown source falls back
    to this device's name rather than to a raw id or an "unknown" nobody
    recognises.  That is the legacy route's own rule
    (``internal.web.api.history._source_label``), kept because the window shows
    the same rows the panel did.
    """
    return names.get(source_device, source_device) or local_name


def matches_kind(entry: dict, kind: str) -> bool:
    """Whether an entry belongs to one of the panel's kind chips.

    The panel's own membership, kept because a chip means the same thing to a
    user either way: 文本 is every kind that carries words rather than the
    ``TEXT`` type alone, and 链接 is a clip whose text *is* a link rather than a
    type of its own, because a link copied out of a browser usually arrives as
    plain text.  Two names are added to the panel's list and one rule is
    widened, each for a reason this build has and the panel's did not:

    * ``IMAGE_PNG`` is what this build stores a bitmap as; the panel only knew
      ``IMAGE``, which older rows still carry.
    * a row *typed* as ``URL`` counts as a link whichever way its text reads,
      which is what keeps the chip and the row's own 在浏览器打开 control in
      agreement — the panel matched the text alone, so a link clip with an empty
      preview fell outside its own chip.
    """
    content_type = str(entry.get("content_type", "")).upper()
    preview = str(entry.get("text_preview", ""))
    if kind == "text":
        return content_type in ("TEXT", "HTML", "RTF")
    if kind == "image":
        return content_type in ("IMAGE", "IMAGE_PNG", "IMAGE_EMF")
    if kind == "file":
        # A file on another device belongs under 文件 as much as one copied
        # here: the chip is what the user is looking for, not where it is.
        return content_type in ("FILE", "FILE_REMOTE")
    if kind == "link":
        return content_type in ("URL", "LINK") or bool(_LINK.match(preview))
    return True


def searchable(entry: dict) -> str:
    """The text a search query is matched against, lowercased.

    Everything the row *shows*: the words, the kind, the device it synced from,
    the application it was copied in and that window's title.  The panel matched
    the first three and not the two source badges its row also displayed, so a
    user could see "chrome" on a row and get nothing for searching it — the one
    place this deliberately searches more than the panel did.
    """
    return " ".join(
        str(entry.get(field, ""))
        for field in ("text_preview", "content_type", "source_device", "source_app", "source_title")
    ).lower()


# How much of a clip a window may read back in one response.  The IPC frame is
# capped at 1 MiB and a response travels as JSON, so a pathological clip — a
# pasted log, a minified bundle — is cut rather than allowed to exceed the frame
# and fail the read outright.  The caller is told, so a cut clip is never shown
# as the whole of one.  Nothing is lost by it: the clipboard is uncapped, and
# this is a read for showing and translating, not for moving the bytes.
TEXT_READ_LIMIT = 100_000


def row_dto(entry: dict, source_label=None, preview_limit: int = 1000) -> dict:
    """One stored entry as a window reads it.

    The list page and the overview's activity feed are the same rows — the feed
    is the newest few of them — so they are built here once.  The feed used to
    project its own four keys (``text``/``type``/``time``/``pinned``) out of the
    same entries, which left it unable to say *which* entry a row was: the
    window could show a recent clip and do nothing with it, because there was
    nothing to name it by.  A row built here carries its id, so every action the
    history page offers a row — copy, pin, favourite, translate, delete — is
    offered on the feed too, and the two cannot drift apart.

    ``preview_limit`` is how much of the clip travels.  The list page ships a
    full one because a reader can select the text there; the feed caps it at what
    its single line can show, and copies go through the id, so nothing a reader
    can act on is lost by the shorter preview.
    """
    return {
        "id": str(entry["entry_id"]),
        "timestamp": entry.get("timestamp", 0),
        "preview": str(entry.get("text_preview", ""))[:preview_limit],
        "content_type": str(entry.get("content_type", "")),
        "pinned": bool(entry.get("pinned", False)),
        # Where the clip came from, which the legacy row showed and a window
        # with no other source of it cannot work out: the *device* it synced
        # from (this one, a peer, or the phone), the application it was copied
        # in, and that window's title.
        "source_name": source_label(str(entry.get("source_device", "")))
        if source_label is not None
        else "",
        # The device *id* behind that name.  A row's file lives on the machine
        # that published it, and asking for it means naming that machine — the
        # label is for reading, this is what a 下载 request carries.  Empty for a
        # clip captured here, which is exactly the row a file offer resolves
        # against on the other side.
        "source_device": str(entry.get("source_device", "")),
        "source_app": str(entry.get("source_app", "")),
        "source_title": str(entry.get("source_title", ""))[:SOURCE_TITLE_LIMIT],
        # Which of the three routes carried it: "lan" for a peer on a direct
        # connection, "relay" for one that came through the internet relay,
        # "web" for a push from this machine's own web server, and "" for a clip
        # captured here or a row written before the route was recorded.  The
        # device name alone cannot answer it — a peer paired on both paths sends
        # over either, and a pushed row's name says only "Web" — and the window
        # shows it as a chip beside that name.
        "transport": str(entry.get("transport", "")),
        # The paste count the row already keeps; legacy showed it as a badge,
        # and a copy made here bumps it, so the window's own copies come back as
        # the same count the panel would show.
        "paste_count": int(entry.get("paste_count", 0) or 0),
    }


def decode_formats(entry: dict) -> dict:
    """An entry's stored formats, base64-decoded and keyed by ``ContentType``.

    Raises ``ValueError``/``TypeError``/``binascii.Error`` on a payload this
    build cannot decode; the callers turn that into ``DATA_INVALID``, which is
    the repair path's cue rather than a message for the window.
    """
    formats = {kind.name: kind for kind in ContentType}
    # Stored rows predate IMAGE_PNG being the wire name for a bitmap.
    formats["IMAGE"] = ContentType.IMAGE_PNG
    stored = entry.get("types")
    if not isinstance(stored, dict):
        raise ValueError("Invalid stored formats")
    return {
        formats[name]: base64.b64decode(payload, validate=True)
        for name, payload in stored.items() if name in formats
    }


def text_of(types: dict) -> bytes | None:
    """The text payload of a decoded clip, or None when it carries none.

    One definition, because a copy asked for plain text and a window reading a
    clip back are the same question about the same row, and two answers would be
    two behaviours.  ``TEXT`` passes through untouched — re-encoding bytes the
    user copied would rewrite them — and a clip that carries only markup is
    converted the way the preview is built.
    """
    if ContentType.TEXT in types:
        return types[ContentType.TEXT]
    if ContentType.HTML in types:
        return strip_html(
            types[ContentType.HTML].decode("utf-8", errors="replace")
        ).encode("utf-8")
    if ContentType.RTF in types:
        from internal.clipboard.filter import _rtf_to_text

        return _rtf_to_text(types[ContentType.RTF]).encode("utf-8")
    # A link's payload *is* text, and for a URL-typed row it is the only payload
    # there is — without this branch such a row had no text at all, so 在浏览器
    # 打开 resolved it to an empty URL and refused while its own button said it
    # would work, and a plain-text copy of it answered that the row carried no
    # text.  Last, so a clip that also carries words still answers with the
    # words.
    if ContentType.URL in types:
        return types[ContentType.URL]
    return None


class HistoryRepository(Protocol):
    def get_all(self) -> list[dict]: ...
    def search(self, query: str) -> list[dict]: ...
    def find_by_id(self, entry_id: str) -> tuple: ...
    def delete_by_id(self, entry_id: str) -> bool: ...
    def batch_set_pinned(self, entry_ids: list, pinned: bool) -> int: ...
    def batch_delete(self, entry_ids: list) -> int: ...
    def touch(self, entry_id: str) -> bool: ...
    def clear(self) -> None: ...
    def increment_paste(self, entry_id: str) -> int | None: ...


class HistoryUseCase:
    def __init__(
        self, repository: HistoryRepository,
        write_clipboard: Callable[[ClipboardContent], bool] | None = None,
        prepare_restore: Callable[[], None] | None = None,
        paste_to_top: Callable[[], bool] = lambda: True,
        open_url: Callable[[str], tuple[bool, str]] | None = None,
        source_label: Callable[[str], str] | None = None,
    ):
        self.repository = repository
        self._write_clipboard = write_clipboard
        self._prepare_restore = prepare_restore
        self._paste_to_top = paste_to_top
        self._open_url = open_url
        self._source_label = source_label

    def copy(self, entry_id: str, plain_text: bool = False) -> dict:
        _, entry = self.repository.find_by_id(entry_id)
        if entry is None:
            raise ApplicationError("NOT_FOUND", "History item no longer exists")
        if self._write_clipboard is None:
            raise ApplicationError("NOT_SUPPORTED", "Clipboard writer is not available")
        try:
            types = decode_formats(entry)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ApplicationError("DATA_INVALID", "History content requires recovery") from exc
        if not types:
            raise ApplicationError(
                "NOT_SUPPORTED", "History item has no supported clipboard format"
            )
        # A row naming a file on another device is not a clipboard payload: no
        # platform's writer has a branch for it, so copying one would clear the
        # clipboard and then report success for having written nothing.  Refused
        # with something the caller can act on instead — the row's own action is
        # 下载, and the window does not offer 复制 for it.
        if any(t in HISTORY_ONLY_TYPES for t in types):
            raise ApplicationError(
                "NOT_SUPPORTED",
                "This file is on the device that published it — download it first",
            )
        if plain_text:
            text = text_of(types)
            if text is None:
                raise ApplicationError("NOT_SUPPORTED", "History item has no text format")
            types = {ContentType.TEXT: text}
        content = ClipboardContent(
            types=types, image_fmt="" if plain_text else entry.get("image_fmt") or ""
        )
        if self._prepare_restore is not None:
            self._prepare_restore()
        try:
            written = self._write_clipboard(content)
        except Exception as exc:
            raise ApplicationError(
                "CLIPBOARD_WRITE_FAILED", "Could not write clipboard", retryable=True
            ) from exc
        if written is not True:
            raise ApplicationError(
                "CLIPBOARD_WRITE_FAILED", "Could not write clipboard", retryable=True
            )
        if self._paste_to_top():
            self.repository.touch(entry_id)
        # The legacy web UI's per-item copy calls /api/paste-rich, which bumps
        # the paste count; a native copy is the same user action, so keep the
        # counter moving. Best-effort: a counter write must not fail the copy.
        try:
            self.repository.increment_paste(entry_id)
        except Exception:
            logger.debug("Paste count increment failed", exc_info=True)
        return {"copied": True}

    def text(self, entry_id: str) -> dict:
        """Read one entry's own text back, for a caller that must not paste it.

        ``list`` ships a truncated preview and the clipboard is the other way
        out of this process, so a window that wants to show or translate a whole
        clip has neither: reading it that way would overwrite whatever the user
        is holding.  This is the third way, and it is the native counterpart of
        the legacy ``/api/history/item``, whose only desktop consumer was the
        context menu's translate action.

        An entry with no text of its own — an image — reports an empty string
        rather than its preview: the preview of a text clip is a cut-down copy
        of the same words, and the preview of an image is a label like
        ``[Image]``.  Neither is what the user asked to translate, so the window
        is left to say there is nothing to translate.
        """
        _, entry = self.repository.find_by_id(entry_id)
        if entry is None:
            raise ApplicationError("NOT_FOUND", "History item no longer exists")
        try:
            types = decode_formats(entry)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ApplicationError("DATA_INVALID", "History content requires recovery") from exc
        payload = text_of(types) if types else None
        text = payload.decode("utf-8", errors="replace") if payload else ""
        # No content_type: the caller already has the row's own classification
        # from the list, and it is not the same thing as which format the text
        # was read out of — a clip stored as HTML whose TEXT payload won is
        # still an "HTML" row.
        return {
            "id": entry_id,
            "text": text[:TEXT_READ_LIMIT],
            "truncated": len(text) > TEXT_READ_LIMIT,
        }

    def preview(self, entry_id: str) -> dict:
        """What a hover card can add about one row: its picture, or its files.

        The card over a text row shows the words the row had to clamp.  An image
        and a file row have no words — their "preview" is the kind's own label
        (``[Image]``) and a file name, both of which the row already shows — so
        the card was empty for exactly the two kinds a glance is worth most for.

        This is the *small* read behind it, and it is small in two ways: a
        picture is downscaled to a card's width before it travels, so hovering
        across a page costs a few kilobytes a row rather than the entries
        themselves; and a file clip is stat'd rather than opened, with the one
        exception of a single picture, which is the case a thumbnail answers.

        **Nothing here raises.**  Every other read in this class turns a row it
        cannot make sense of into ``NOT_FOUND`` or ``DATA_INVALID``, and every
        one of them is answering something the user asked for.  A hover is not
        asked for — it is where the pointer happens to be — so a row that was
        deleted, or whose payload needs recovery, answers with an empty card
        rather than with an error the user never requested and cannot act on.
        """
        _, entry = self.repository.find_by_id(entry_id)
        if entry is None:
            return dict(NO_PREVIEW)
        stored = entry.get("types")
        if isinstance(stored, dict) and _bitmap_over_budget(stored):
            logger.debug("History preview of %s: bitmap past the preview budget", entry_id[:8])
            return dict(NO_PREVIEW)
        try:
            types = decode_formats(entry)
        except (ValueError, TypeError, binascii.Error):
            logger.debug("History preview of %s needs recovery", entry_id[:8])
            return dict(NO_PREVIEW)
        if ContentType.IMAGE_PNG in types:
            url, width, height = picture_of(types[ContentType.IMAGE_PNG])
            if not url:
                # A bitmap this build cannot decode.  The card says nothing
                # about it rather than promising a picture it has not got.
                return dict(NO_PREVIEW)
            return dict(NO_PREVIEW, kind="image", image=url, width=width, height=height)
        if ContentType.FILE in types:
            raw = types[ContentType.FILE].decode("utf-8", errors="replace")
            return _file_card(split_paths(raw))
        if ContentType.FILE_REMOTE in types:
            return _remote_file_card(types[ContentType.FILE_REMOTE])
        # Text, a link, markup, a vector image: the row's own preview is already
        # the whole of what there is to show.
        return dict(NO_PREVIEW)

    def open_link(self, entry_id: str) -> dict:
        """Open an entry's own text in the browser when that text is a web link.

        Legacy's history menu offered 在浏览器打开 on any row whose text looked
        like a link, and the desktop window has no other way to reach the
        browser: a window that can only copy a URL leaves the user pasting it
        into an address bar by hand.

        The URL comes from the entry, never from the caller — the window names a
        row and the app opens what the row already holds, which is also why this
        reads the clip's whole text rather than its preview (legacy opened the
        preview, and a preview is a cut-down copy of the same string).
        """
        _, entry = self.repository.find_by_id(entry_id)
        if entry is None:
            raise ApplicationError("NOT_FOUND", "History item no longer exists")
        if self._open_url is None:
            raise ApplicationError("NOT_SUPPORTED", "No URL opener is available")
        try:
            types = decode_formats(entry)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ApplicationError("DATA_INVALID", "History content requires recovery") from exc
        payload = text_of(types) if types else None
        ok, detail = self._open_url(
            (payload.decode("utf-8", errors="replace") if payload else "").strip()
        )
        if not ok:
            if detail == "INVALID_URL":
                # Nothing was opened, and that is the clip's own answer: the row
                # looked like a link to the window, and its text is not one.
                raise ApplicationError(
                    "INVALID_URL", "History item is not an openable web link"
                )
            raise ApplicationError("OPEN_FAILED", "Could not open the link in the browser")
        return {"opened": True, "url": detail}

    def list(
        self,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
        kind: str = "all",
        sort: str = "newest",
    ) -> dict:
        """A page of history, filtered the way the panel's toolbar filtered it.

        The search runs here rather than through ``repository.search`` because
        the window shows a row's source application and window title (see
        ``searchable``), and the repository's own search cannot see those
        columns.  That costs a full read per page; history is bounded by
        retention, and the alternative is a row whose badge the user can read
        but not search for.

        ``counts`` is the chip badges: how many entries each chip *would* show
        under the current search — the panel's own rule, so a chip's number is
        the number of rows clicking it yields, not a fixed total that stops
        matching once a search is typed.  ``has_history`` says whether there is
        anything to filter at all: the chip row stays on screen while a search
        matches nothing, because a chip bar that disappears the moment a typo
        yields no rows takes the way back out with it.
        """
        entries = self.repository.get_all()
        has_history = bool(entries)
        needle = query.strip().lower()
        if needle:
            entries = [entry for entry in entries if needle in searchable(entry)]
        counts = {
            name: sum(1 for entry in entries if matches_kind(entry, name)) for name in KINDS
        }
        matched = (
            entries if kind == "all" else [e for e in entries if matches_kind(e, kind)]
        )
        # Two stable passes rather than one compound key: the order the user
        # asked for first, then pinned entries floated to the top of it.  A
        # compound key would have to reverse the pinned half along with the
        # timestamps, which puts pinned entries at the *bottom* in one of the two
        # orders — the panel floated them in both directions, and a pinned entry
        # the user has to scroll for does not read as pinned.
        matched.sort(key=lambda entry: entry.get("timestamp", 0), reverse=sort != "oldest")
        matched.sort(key=lambda entry: not entry.get("pinned", False))
        return {
            "total": len(matched),
            "offset": offset,
            "kind": kind,
            "sort": sort,
            "counts": counts,
            "has_history": has_history,
            "items": [
                row_dto(entry, self._source_label)
                for entry in matched[offset : offset + limit]
            ],
        }

    def delete(self, entry_id: str) -> dict:
        if not self.repository.delete_by_id(entry_id):
            raise ApplicationError("NOT_FOUND", "History item no longer exists")
        return {"id": entry_id, "deleted": True}

    def set_pinned(self, entry_id: str, pinned: bool) -> dict:
        if not self.repository.batch_set_pinned([entry_id], pinned):
            raise ApplicationError("NOT_FOUND", "History item no longer exists")
        return {"id": entry_id, "pinned": pinned}

    def batch_set_pinned(self, entry_ids: list[str], pinned: bool) -> dict:
        return {"updated": self.repository.batch_set_pinned(entry_ids, pinned)}

    def batch_delete(self, entry_ids: list[str]) -> dict:
        return {"deleted": self.repository.batch_delete(entry_ids)}

    def clear(self) -> dict:
        """Remove every history entry, reporting how many were removed."""
        try:
            count = len(self.repository.get_all())
            self.repository.clear()
        except ApplicationError:
            raise
        except Exception as exc:
            raise ApplicationError(
                "STORAGE_ERROR", "History storage is unavailable", retryable=True
            ) from exc
        return {"cleared": count}
