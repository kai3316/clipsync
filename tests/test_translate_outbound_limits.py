"""What the translation proxy will not connect to, and will not read.

Two gaps in the outbound path, both of which the pre-flight URL check was
believed to cover:

* it resolved the hostname and validated every answer, and then urllib resolved
  it again to connect — so a name the endpoint's owner controls could answer
  "public" for the check and a link-local address for the connection (DNS
  rebinding).  Redirects were already refused; a second DNS answer was not.
* ``resp.read()`` with no argument read whatever came back.  Nothing bounded a
  response, so one oversized body from a misconfigured or hostile instance was
  held in memory whole.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from internal.web.api import security, translate


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # cloud metadata
        "::ffff:169.254.169.254",  # the same, in the form that used to slip past
        "239.1.1.1",  # multicast
        "0.0.0.0",  # unspecified
    ],
)
def test_a_connection_to_a_blocked_address_is_refused(address):
    class _Base:
        def connect(self):
            return None

    class _Probe(translate._GuardedConnectionMixin, _Base):
        def __init__(self, peer):
            self.sock = SimpleNamespace(getpeername=lambda: (peer, 443))
            self.closed = False

        def close(self):
            self.closed = True

    probe = _Probe(address)
    with pytest.raises(OSError):
        probe.connect()
    # The socket is released on the way out; a refused connection that leaks a
    # handle would be a slower version of the same problem.
    assert probe.closed is True


@pytest.mark.parametrize("address", ["93.184.216.34", "2001:4860:4860::8888"])
def test_a_public_address_is_allowed(address):
    class _Base:
        def connect(self):
            return None

    class _Probe(translate._GuardedConnectionMixin, _Base):
        def __init__(self, peer):
            self.sock = SimpleNamespace(getpeername=lambda: (peer, 443))
            self.closed = False

        def close(self):
            self.closed = True

    probe = _Probe(address)
    probe.connect()
    assert probe.closed is False


def test_localhost_and_a_lan_service_are_still_allowed():
    """The policy blocks metadata and reserved space, not the user's own box."""
    assert security.blocked_address_reason("127.0.0.1") == ""
    assert security.blocked_address_reason("192.168.1.10") == ""
    assert security.blocked_address_reason("10.0.0.5") == ""


def _stub_opener(body: bytes):
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    class _Opener:
        def open(self, _req, timeout=None):
            return _Response(body)

    return _Opener()


def test_an_oversized_response_is_refused_not_read(monkeypatch):
    huge = b"{" + b" " * (translate.MAX_RESPONSE_BYTES + 8)
    monkeypatch.setattr(translate, "_NO_REDIRECT_OPENER", _stub_opener(huge))

    result = translate._translate_libretranslate(
        "http://127.0.0.1:5000/translate", "", "hello", "zh", "en"
    )

    assert result["ok"] is False
    assert "too large" in result["error"]


def test_a_normal_response_still_works(monkeypatch):
    body = json.dumps({"translatedText": "你好"}).encode("utf-8")
    monkeypatch.setattr(translate, "_NO_REDIRECT_OPENER", _stub_opener(body))

    result = translate._translate_libretranslate(
        "http://127.0.0.1:5000/translate", "", "hello", "zh", "en"
    )

    assert result["ok"] is True
    assert result["translated"] == "你好"


def test_the_bound_is_checked_against_the_limit_itself():
    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self, n=-1):
            return self.payload[:n]

    assert translate._read_bounded(_Response(b"x" * 10), 10) == b"x" * 10
    with pytest.raises(ValueError):
        translate._read_bounded(_Response(b"x" * 11), 10)
