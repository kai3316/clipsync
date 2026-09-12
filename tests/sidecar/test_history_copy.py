import base64
import sqlite3
from unittest.mock import Mock

import pytest

from internal.application.errors import ApplicationError
from internal.application.use_cases.history import HistoryUseCase
from internal.clipboard.format import ContentType


def use_case(payload=None, result=True):
    entry = {
        "entry_id": 0,
        "types": payload if payload is not None else {
            "TEXT": base64.b64encode(b"hello").decode(),
            "HTML": base64.b64encode(b"<b>hello</b>").decode(),
            "IMAGE": base64.b64encode(b"image-bytes").decode(),
        },
        "image_fmt": "png",
    }
    repository = Mock()
    repository.find_by_id.return_value = (0, entry)
    writer = Mock(return_value=result)
    prepare = Mock()
    service = HistoryUseCase(repository, writer, prepare)
    return service, repository, writer, prepare


def test_restore_retains_all_formats_and_touches_id_zero_after_success():
    service, repository, writer, prepare = use_case()
    assert service.copy("0") == {"copied": True}
    content = writer.call_args.args[0]
    assert content.types[ContentType.TEXT] == b"hello"
    assert content.types[ContentType.HTML] == b"<b>hello</b>"
    assert content.types[ContentType.IMAGE_PNG] == b"image-bytes"
    assert content.image_fmt == "png"
    prepare.assert_called_once_with()
    repository.touch.assert_called_once_with("0")


@pytest.mark.parametrize("payload", [
    {
        "TEXT": base64.b64encode(b"hello").decode(),
        "HTML": base64.b64encode(b"<b>other</b>").decode(),
    },
    {"HTML": base64.b64encode(b"<b>hello</b>").decode()},
    {"RTF": base64.b64encode(br"{\rtf1\ansi hello}").decode()},
])
def test_plain_restore_writes_only_visible_text(payload):
    service, _, writer, _ = use_case(payload)
    service.copy("0", plain_text=True)
    assert writer.call_args.args[0].types == {ContentType.TEXT: b"hello"}
    assert writer.call_args.args[0].image_fmt == ""


def test_plain_restore_rejects_image_without_modifying_clipboard():
    service, repository, writer, prepare = use_case({"IMAGE": base64.b64encode(b"image").decode()})
    with pytest.raises(ApplicationError, match="no text format"):
        service.copy("0", plain_text=True)
    writer.assert_not_called()
    prepare.assert_not_called()
    repository.touch.assert_not_called()


@pytest.mark.parametrize("result", [False, None])
def test_failed_writer_does_not_touch_history(result):
    service, repository, _, _ = use_case(result=result)
    with pytest.raises(ApplicationError) as error:
        service.copy("0")
    assert error.value.code == "CLIPBOARD_WRITE_FAILED"
    assert error.value.retryable
    repository.touch.assert_not_called()


def test_writer_exception_is_sanitized():
    service, repository, writer, _ = use_case()
    writer.side_effect = OSError("private file path")
    with pytest.raises(ApplicationError) as error:
        service.copy("0")
    assert "private file path" not in str(error.value)
    repository.touch.assert_not_called()


@pytest.mark.parametrize("payload", [{"TEXT": "***"}, {"TEXT": None}])
def test_corrupt_history_is_not_written(payload):
    service, _, writer, prepare = use_case(payload=payload)
    with pytest.raises(ApplicationError) as error:
        service.copy("0")
    assert error.value.code == "DATA_INVALID"
    writer.assert_not_called()
    prepare.assert_not_called()


def test_missing_history_is_not_written():
    service, repository, writer, _ = use_case()
    repository.find_by_id.return_value = (None, None)
    with pytest.raises(ApplicationError) as error:
        service.copy("gone")
    assert error.value.code == "NOT_FOUND"
    writer.assert_not_called()


def test_paste_to_top_disabled():
    service, repository, _, _ = use_case()
    service._paste_to_top = lambda: False
    assert service.copy("0") == {"copied": True}
    repository.touch.assert_not_called()


def test_successful_copy_bumps_the_paste_count():
    service, repository, _, _ = use_case()
    assert service.copy("0") == {"copied": True}
    repository.increment_paste.assert_called_once_with("0")


def test_failed_paste_count_write_still_reports_the_copy():
    service, repository, _, _ = use_case()
    repository.increment_paste.side_effect = OSError("counter unavailable")
    assert service.copy("0") == {"copied": True}
    repository.touch.assert_called_once_with("0")


def test_clear_reports_the_removed_count_and_clears_the_repository():
    repository = Mock()
    repository.get_all.return_value = [{"entry_id": 0}, {"entry_id": 1}]
    service = HistoryUseCase(repository)
    assert service.clear() == {"cleared": 2}
    repository.clear.assert_called_once_with()


def test_clear_sanitizes_storage_failures():
    repository = Mock()
    repository.get_all.side_effect = sqlite3.OperationalError("database is locked")
    service = HistoryUseCase(repository)
    with pytest.raises(ApplicationError) as error:
        service.clear()
    assert error.value.code == "STORAGE_ERROR"
    assert error.value.retryable
    assert "locked" not in str(error.value)
    repository.clear.assert_not_called()
