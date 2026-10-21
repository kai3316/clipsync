"""Remote-access configuration and the QR fragment, without a live companion.

The tunnel itself is covered in ``test_companion_tunnel.py``; these tests hold
the small application-level surface around it: the remote QR must carry the
token and brokers only in the fragment, and rotating the remote token must not
touch the LAN token.
"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from internal.application import bootstrap
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.application.events import EventJournal
from internal.config.config import Config


def application():
    app = SidecarApplication.__new__(SidecarApplication)
    app.config = Config()
    app.config.companion_remote_page_url = "https://example.pages.dev/"
    app.config.companion_remote_token = "remote-token-123"
    app.config.companion_remote_enabled = True
    app.events = EventJournal()
    app.identity = SimpleNamespace(encryption=None)
    app.companion = None
    app.companion_tunnel = None
    app._tunnel_signature = None
    app._tunnel_unsubscribe = None
    app._companion_port = lambda: None
    app._sync_companion_tunnel = lambda: None
    return app


def test_the_remote_qr_keeps_the_token_and_brokers_in_the_fragment():
    app = application()
    result = app.companion_remote_qr()
    assert result["ok"] is True
    url = result["url"]
    base, fragment = url.split("#", 1)
    assert base == "https://example.pages.dev/"
    assert result["qr"].startswith("data:image/png;base64,")
    fields = parse_qs(fragment)
    assert fields["v"] == ["1"]
    assert fields["t"] == ["remote-token-123"]
    assert json.loads(fields["b"][0]) == app.config.companion_remote_brokers
    # The token must not appear before the fragment: a static host logs paths
    # and query strings, and this one must never see the secret.
    assert "remote-token-123" not in base


def test_the_remote_qr_explains_the_normal_reasons_it_cannot_be_drawn():
    app = application()
    app.config.companion_remote_enabled = False
    assert app.companion_remote_qr()["error"] == "REMOTE_DISABLED"

    app.config.companion_remote_enabled = True
    app.config.companion_remote_page_url = ""
    assert app.companion_remote_qr()["error"] == "REMOTE_PAGE_NOT_SET"

    app.config.companion_remote_page_url = "https://example.pages.dev/"
    app.config.companion_remote_token = ""
    app.config.web_token = ""
    assert app.companion_remote_qr()["error"] == "REMOTE_TOKEN_MISSING"


def test_rotating_the_remote_token_does_not_move_the_lan_token(monkeypatch):
    app = application()
    calls = []
    monkeypatch.setattr(bootstrap, "save", lambda cfg, encryption: calls.append(cfg))
    app.identity = SimpleNamespace(encryption=object())
    original_lan = app.config.web_token = "lan-token"

    status = app.configure_companion_remote(rotate_token=True)

    assert status["enabled"] is True
    assert app.config.web_token == original_lan
    assert app.config.companion_remote_token
    assert app.config.companion_remote_token != "remote-token-123"
    assert calls == [app.config]


def test_saving_the_page_url_belongs_to_remote_configuration(monkeypatch):
    app = application()
    monkeypatch.setattr(bootstrap, "save", lambda cfg, encryption: None)
    status = app.configure_companion_remote(page_url="https://other.pages.dev/")
    assert status["page_url"] == "https://other.pages.dev/"
    assert app.config.companion_remote_page_url == "https://other.pages.dev/"

    with pytest.raises(ApplicationError):
        app.configure_companion_remote(page_url="javascript:alert(1)")


def test_clearing_the_remote_token_does_not_fall_back_to_the_lan_token():
    app = application()
    app.config.companion_remote_token = ""
    app.config.web_token = "lan-token"
    app._sync_companion_tunnel = lambda: None
    # The LAN token is not a substitute: a remote QR must never carry it.
    assert app.companion_remote_status()["token_set"] is False


def test_enabling_remote_access_mints_a_separate_token(monkeypatch):
    app = application()
    monkeypatch.setattr(bootstrap, "save", lambda cfg, encryption: None)
    app.config.companion_remote_token = ""
    app.config.web_token = "lan-token"
    status = app.configure_companion_remote(enabled=True)
    assert status["token_set"] is True
    assert app.config.companion_remote_token
    assert app.config.companion_remote_token != "lan-token"


def test_remote_mode_is_readonly_by_default_and_validated():
    app = application()
    assert app.companion_remote_status()["mode"] == "readonly"
    app.config.companion_remote_mode = "full"
    assert app.companion_remote_status()["mode"] == "full"
    app._sync_companion_tunnel = lambda: None
    with pytest.raises(ApplicationError):
        app.configure_companion_remote(mode="everything")


def test_remote_status_is_disabled_by_default():
    app = application()
    app.config.companion_remote_enabled = False
    status = app.companion_remote_status()
    assert status["enabled"] is False
    assert status["connected"] is False
    assert status["token_set"] is True
    assert status["error"] == ""


def test_the_relay_list_is_editable_and_defaults_are_offered_alongside_it(monkeypatch):
    """The window offers "restore defaults", so it needs the default list from
    the same place the tunnel would fall back to -- a second copy in the
    frontend is a list that drifts."""
    from internal.config.config import DEFAULT_COMPANION_REMOTE_BROKERS

    app = application()
    monkeypatch.setattr(bootstrap, "save", lambda cfg, encryption: None)

    status = app.companion_remote_status()
    assert status["brokers"] == list(DEFAULT_COMPANION_REMOTE_BROKERS)
    assert status["default_brokers"] == list(DEFAULT_COMPANION_REMOTE_BROKERS)

    custom = ["wss://relay.example.com:8084/mqtt", "wss://backup.example.com:8084/mqtt"]
    status = app.configure_companion_remote(brokers=custom)
    assert status["brokers"] == custom
    assert app.config.companion_remote_brokers == custom
    # The defaults are a constant, not "whatever is configured now".
    assert status["default_brokers"] == list(DEFAULT_COMPANION_REMOTE_BROKERS)


def test_an_empty_relay_list_is_refused_rather_than_silently_disabling_remote(monkeypatch):
    """An empty list would leave the tunnel with nothing to dial and no way to
    say why, while the phone still shows a QR code that can never connect."""
    app = application()
    monkeypatch.setattr(bootstrap, "save", lambda cfg, encryption: None)
    before = list(app.config.companion_remote_brokers)
    for empty in ([], [""], ["   "]):
        with pytest.raises(ApplicationError):
            app.configure_companion_remote(brokers=empty)
    assert app.config.companion_remote_brokers == before


def test_the_qr_carries_the_edited_relay_list(monkeypatch):
    """The phone learns its brokers from the QR fragment, so an edited list
    that never reaches the code would be a setting that does nothing."""
    app = application()
    monkeypatch.setattr(bootstrap, "save", lambda cfg, encryption: None)
    custom = ["wss://mine.example.com:8084/mqtt"]
    app.configure_companion_remote(brokers=custom)

    fields = parse_qs(app.companion_remote_qr()["url"].split("#", 1)[1])
    assert json.loads(fields["b"][0]) == custom
