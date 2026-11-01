"""A settings answer says what actually happened.

Two directions, both invisible to the user under the old shapes:

* ``update_settings`` applied each field with ``setattr`` and only then tried
  to save.  When the save failed it answered 500 — but the process kept running
  the values the caller had been told were not saved, and the next successful
  save wrote them to disk as a side effect of something else.
* a raising ``on_settings_change`` was logged and the answer was still
  ``ok: true``: the disk had the new port, the listener still had the old one,
  and the panel had no way to say so.

It also covers the pair the per-field range check cannot see: the LAN listener
and the companion both want ``host:port`` on the same address family, so equal
numbers saved fine and failed to bind at the next start.
"""

from __future__ import annotations

import json
import types

import pytest

from internal.config.config import Config
from internal.sync.ai_config import AIConfigManager
from internal.web.api import settings as settings_api


@pytest.fixture
def cfg():
    return Config()


def _update(cfg, values, on_settings_change=None):
    body = json.dumps(values).encode("utf-8")
    return settings_api.update_settings(body, cfg, on_settings_change=on_settings_change)


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    """Save to nothing: these tests are about the answer, not the file."""
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key", lambda cfg: None
    )


def test_equal_ports_are_refused_while_the_companion_is_on(cfg):
    cfg.web_enabled = True
    data, status = _update(cfg, {"port": 21000, "web_port": 21000})

    assert status == 400
    assert data == {"ok": False, "error": "port_conflict"}
    # Nothing was kept: the pair that cannot bind is not the running config.
    assert (cfg.port, cfg.web_port) == (19990, 19991)


def test_equal_ports_are_fine_when_only_one_listener_exists(cfg):
    cfg.web_enabled = False
    data, status = _update(cfg, {"port": 21000, "web_port": 21000})

    assert status == 200 and data["ok"] is True
    assert (cfg.port, cfg.web_port) == (21000, 21000)


def test_a_port_set_alone_is_still_checked_against_the_other(cfg):
    """The conflict can be created by moving one port onto the other."""
    cfg.web_enabled = True
    cfg.web_port = 21000
    data, status = _update(cfg, {"port": 21000})

    assert status == 400 and data["error"] == "port_conflict"
    assert cfg.port == 19990


def test_a_failed_save_puts_memory_back(monkeypatch, cfg):
    def refuse(_cfg):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(settings_api, "_persist_preserving_at_rest_private_key", refuse)
    original_name = cfg.device_name
    data, status = _update(cfg, {"device_name": "renamed", "sync_enabled": False})

    assert status == 500 and data["ok"] is False
    # The caller was told the save failed, so the process must not be running
    # the values it failed to save.
    assert cfg.device_name == original_name
    assert cfg.sync_enabled is True


def test_a_failed_live_apply_is_not_reported_as_fully_successful(cfg):
    def explode(_updated, _special):
        raise RuntimeError("could not rebind the listener")

    data, status = _update(cfg, {"web_port": 21000}, on_settings_change=explode)

    assert status == 200
    assert data["ok"] is True          # the file was written
    assert data["saved"] is True
    assert data["applied"] is False    # and the process is not using it
    assert data["error"] == "settings_saved_not_applied"


def test_a_successful_apply_says_so(cfg):
    seen = []

    def apply(updated, special):
        seen.append(updated)
        return {"password_set": False}

    data, status = _update(cfg, {"web_port": 21000}, on_settings_change=apply)

    assert status == 200
    assert data["applied"] is True
    assert "error" not in data
    # The whitelisted fields the live apply reports still travel.
    assert data["password_set"] is False
    assert seen == [{"web_port": 21000}]


def test_an_ai_profile_save_failure_is_not_reported_as_saved():
    """N7 in the review: the same lie through the other settings door."""
    saved = []
    cfg = types.SimpleNamespace(
        ai_config_tools=[], ai_config_custom_paths=[], data_dir="", peers={}
    )

    def refuse():
        raise OSError(13, "permission denied")

    manager = AIConfigManager(
        cfg,
        send_fn=lambda peer_id, frame: True,
        connected_fn=lambda: [],
        event_fn=None,
        save_fn=lambda: saved.append(1) or refuse(),
    )
    result = manager.set_profiles(["claude_code"], ["~/notes"])

    assert result["ok"] is False
    assert result["error"] == "save_failed"
    # Rolled back, so the inventory built next does not describe a config the
    # disk does not have.
    assert cfg.ai_config_tools == []
    assert cfg.ai_config_custom_paths == []
