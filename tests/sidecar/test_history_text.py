"""Reading one clip's own text back, for a window that must not paste it.

The list DTO ships a truncated preview and the clipboard is the other way out
of the sidecar, so this is the path a window uses to show or translate a whole
clip without overwriting whatever the user is holding.
"""

import base64
from unittest.mock import Mock

import pytest

from internal.application.errors import ApplicationError
from internal.application.use_cases.history import TEXT_READ_LIMIT, HistoryUseCase


def use_case(payload):
    entry = {"entry_id": 0, "types": payload, "content_type": "TEXT"}
    repository = Mock()
    repository.find_by_id.return_value = (0, entry)
    return HistoryUseCase(repository), repository


def stored(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_the_whole_clip_is_read_not_its_preview():
    # The preview stops at 200 characters; a translator needs the words.
    text = "word " * 200
    service, _ = use_case({"TEXT": stored(text.encode())})
    assert service.text("0")["text"] == text


def test_a_clip_that_only_carries_markup_is_read_as_its_visible_text():
    service, _ = use_case({"HTML": stored(b"<p>hello <b>there</b></p>")})
    assert service.text("0")["text"] == "hello there"


def test_an_image_has_no_text_to_translate():
    # Not its preview: for an image the preview is the label "[Image]", and
    # handing that to a translator would show the user something that is not
    # their clip.
    service, _ = use_case({"IMAGE": stored(b"image-bytes")})
    assert service.text("0")["text"] == ""


def test_a_long_clip_is_cut_at_the_read_limit_and_says_so():
    # The IPC frame is capped, so a pathological clip is cut rather than allowed
    # to fail the read outright — and the caller is told, so a cut clip is never
    # shown as the whole of one.
    service, _ = use_case({"TEXT": stored(b"x" * (TEXT_READ_LIMIT + 10))})
    result = service.text("0")
    assert len(result["text"]) == TEXT_READ_LIMIT
    assert result["truncated"] is True


def test_a_clip_that_exactly_fits_is_not_flagged_as_cut():
    service, _ = use_case({"TEXT": stored(b"x" * TEXT_READ_LIMIT)})
    result = service.text("0")
    assert len(result["text"]) == TEXT_READ_LIMIT
    assert result["truncated"] is False


def test_a_broken_byte_does_not_fail_the_read():
    # A clip is bytes the user copied, and the read is for showing it: an
    # undecodable byte becomes a replacement character rather than an error.
    service, _ = use_case({"TEXT": stored(b"hi\xff\xfe")})
    assert "hi" in service.text("0")["text"]


@pytest.mark.parametrize("payload", [{"TEXT": "***"}, {"TEXT": None}])
def test_a_row_this_build_cannot_decode_is_not_read(payload):
    service, _ = use_case(payload)
    with pytest.raises(ApplicationError) as error:
        service.text("0")
    assert error.value.code == "DATA_INVALID"


def test_a_missing_row_is_not_read():
    service, repository = use_case({"TEXT": stored(b"hello")})
    repository.find_by_id.return_value = (None, None)
    with pytest.raises(ApplicationError) as error:
        service.text("gone")
    assert error.value.code == "NOT_FOUND"


def test_reading_a_clip_reports_its_id_and_type():
    service, _ = use_case({"TEXT": stored(b"hello")})
    assert service.text("0") == {"id": "0", "text": "hello", "truncated": False}
