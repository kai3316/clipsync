"""Codec regressions: tell "nothing to sync" from "the file offer failed".

The review's 中-8: a file clip whose offer could not be built encoded to an
empty frame (and the runtime silently skipped it), or degraded to a text row
with no download button.  `encode_message_ex` is the seam that lets the capture
path report the second case — and keeps quiet about the first.
"""

from __future__ import annotations

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.protocol.codec import (
    ENCODE_REASON_EMPTY,
    ENCODE_REASON_FILE_UNSERVABLE,
    ENCODE_REASON_NO_SYNCABLE,
    ENCODE_REASON_OK,
    decode_message,
    encode_message,
    encode_message_ex,
)


def message(types, entry_id=""):
    return SyncMessage(ClipboardContent(types=types, entry_id=entry_id), "m1", "dev-a")


def test_a_file_whose_paths_cannot_be_described_reports_itself():
    content = {ContentType.FILE: b"/no/such/file/anywhere.bin"}

    data, reason = encode_message_ex(message(content, entry_id="h-1"))

    assert data == b""
    assert reason == ENCODE_REASON_FILE_UNSERVABLE
    # The plain encoder still answers exactly what it used to.
    assert encode_message(message(content, entry_id="h-1")) == b""


def test_a_file_without_a_history_row_reports_itself(tmp_path):
    path = tmp_path / "notes.md"
    path.write_bytes(b"hello")
    content = {ContentType.FILE: f"{path}".encode()}

    data, reason = encode_message_ex(message(content, entry_id=""))

    assert data == b""
    assert reason == ENCODE_REASON_FILE_UNSERVABLE


def test_a_file_copy_that_degrades_to_its_name_still_reports_the_failed_offer():
    """A frame may exist (the file's name as text) while the offer did not.

    The caller must not read a non-empty frame as "the file is fine": the
    reason travels with the bytes.
    """
    content = {
        ContentType.FILE: b"/no/such/file/anywhere.bin",
        ContentType.TEXT: b"anywhere.bin",
    }

    data, reason = encode_message_ex(message(content, entry_id="h-1"))

    assert data
    assert reason == ENCODE_REASON_FILE_UNSERVABLE
    decoded = decode_message(data)
    assert ContentType.TEXT in decoded.content.types
    assert ContentType.FILE_REMOTE not in decoded.content.types


def test_a_describable_file_with_a_row_produces_an_offer_and_no_reason(tmp_path):
    path = tmp_path / "notes.md"
    path.write_bytes(b"hello")

    data, reason = encode_message_ex(
        message({ContentType.FILE: f"{path}".encode()}, entry_id="h-1")
    )

    assert data and reason == ENCODE_REASON_OK
    decoded = decode_message(data)
    assert ContentType.FILE_REMOTE in decoded.content.types


def test_a_clip_with_no_formats_is_a_quiet_skip():
    data, reason = encode_message_ex(message({}))

    assert data == b""
    assert reason == ENCODE_REASON_NO_SYNCABLE


def test_a_clip_whose_only_format_is_a_path_url_is_an_empty_payload():
    data, reason = encode_message_ex(
        message({ContentType.URL: b"file:///home/someone/notes.md"})
    )

    assert data == b""
    assert reason == ENCODE_REASON_EMPTY


def test_an_ordinary_text_clip_reports_no_reason():
    data, reason = encode_message_ex(message({ContentType.TEXT: b"hello"}))

    assert data and reason == ENCODE_REASON_OK
    assert decode_message(data).content.types[ContentType.TEXT] == b"hello"
