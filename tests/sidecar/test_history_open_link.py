"""Opening a clip that is a web link, for a window that has no browser.

Legacy's history menu offered 在浏览器打开 on a row whose text looked like a
link.  The URL always comes from the row, never from the caller, so the app can
only open something the user already had on their clipboard.
"""

import base64

import pytest

from internal.application.errors import ApplicationError
from internal.application.use_cases.history import HistoryUseCase
from internal.system.about import MAX_URL_CHARS, is_openable_url, open_web_url


def use_case(payload, opener=None):
    """A history use case wired to a recorder instead of a real browser.

    The default recorder applies the real gate (:func:`is_openable_url`) and
    notes what actually reached the browser, so "nothing opened" is what these
    tests assert rather than "the use case said no".
    """
    opened = []

    def record(url):
        if opener is not None:
            return opener(url)
        if not is_openable_url(url):
            return (False, "INVALID_URL")
        opened.append(url)
        return (True, url)

    from unittest.mock import Mock

    repository = Mock()
    repository.find_by_id.return_value = (
        0,
        {"entry_id": 0, "types": payload, "content_type": "URL"},
    )
    return HistoryUseCase(repository, open_url=record), opened


def stored(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_a_link_clip_is_opened_from_its_own_text():
    service, opened = use_case({"TEXT": stored(b"https://example.com/page")})
    assert service.open_link("0") == {"opened": True, "url": "https://example.com/page"}
    assert opened == ["https://example.com/page"]


def test_a_clip_that_is_not_a_link_opens_nothing():
    service, opened = use_case({"TEXT": stored(b"see https://example.com for details")})
    with pytest.raises(ApplicationError) as error:
        service.open_link("0")
    assert error.value.code == "INVALID_URL"
    assert opened == []


def test_a_browser_that_will_not_open_the_link_is_reported():
    # The clip was a link and the OS refused it: not the clip's fault, so the
    # caller is told the open failed rather than that the row is not a link.
    service, _ = use_case(
        {"TEXT": stored(b"https://example.com")}, opener=lambda _url: (False, "OPEN_FAILED")
    )
    with pytest.raises(ApplicationError) as error:
        service.open_link("0")
    assert error.value.code == "OPEN_FAILED"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hello",
        "https://",
        "http://example.com\nhttps://evil.example",
        "/local/path",
        "",
        "https://example.com/" + "x" * MAX_URL_CHARS,
    ],
)
def test_only_short_whitespace_free_web_urls_are_openable(url):
    # The opener is the last gate before the OS: a non-web scheme, a missing
    # host, an embedded newline or a URL nobody could have pasted is refused.
    assert is_openable_url(url) is False
    assert open_web_url(url) == (False, "INVALID_URL")
