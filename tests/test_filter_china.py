"""The phone-number/ID/card rules a mainland user actually pastes.

The filter is opt-in, so these tests only check the patterns once a category is
enabled.  The shape checks matter more than the labels: an 18-digit order number
must not become an identity card, and an 11-digit number that is not a mobile
number must not become a phone.
"""

from __future__ import annotations

from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent, ContentType


def _filter_text(categories: list[str], value: str) -> str:
    content = ClipboardContent(types={ContentType.TEXT: value.encode("utf-8")})
    result = ContentFilter(categories).filter_content(content)
    return result.types[ContentType.TEXT].decode("utf-8")


def test_a_mainland_mobile_number_is_redacted():
    text = _filter_text(["phone"], "电话 13800138000，或者 +86 138-0013-8000")
    assert "13800138000" not in text
    assert text.count("[FILTERED]") == 2


def test_a_valid_resident_id_is_redacted_and_an_invalid_one_is_not():
    text = _filter_text(["ssn"], "身份证 11010519491231002X")
    assert "11010519491231002X" not in text
    assert "[FILTERED]" in text

    # Same shape, wrong checksum: it is not an identity card and must survive.
    text = _filter_text(["ssn"], "编号 110105194912310021")
    assert "110105194912310021" in text


def test_unionpay_card_is_a_valid_card_shape():
    text = _filter_text(["credit_card"], "卡号 6222 0212 3456 7894")
    assert "6222 0212 3456 7894" not in text
    assert "[FILTERED]" in text
