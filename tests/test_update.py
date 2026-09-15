"""Update verification and install policy.

Covered here:
  1. verify_update_blob: bad hash, old/equal version, missing release info
     (P2P rejects + GitHub proceeds), GitHub double-checks too.
  2. fetch_latest_asset_info: a release without a published digest.
  3. The install path: archive stashed under ``~/Downloads/clipsync-update/``
     with a ready state, never auto-applied; a peer blob with a bad hash or
     without a release reference is discarded / falls back to the server.
  4. auto_update_check switch OFF → the periodic loop makes zero requests
     (network layer guarded); the setting persists.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.system import updater as updater_mod

# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


class _FakeRoot:
    """Tk-root stand-in: runs after(0) inline, records later timers."""

    def __init__(self):
        self.scheduled = []

    def after(self, delay, fn, *args):
        if delay == 0:
            fn(*args)
        else:
            self.scheduled.append((delay, fn))
        return 1


def _write_blob(tmp_path, payload=b"CLIPSYNC-UPDATE-PAYLOAD"):
    blob = tmp_path / "clipsync-windows.zip"
    blob.write_bytes(payload)
    return str(blob), payload


def _info_for(payload, version="999.0.0"):
    return {
        "version": version,
        "asset": "clipsync-windows.zip",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


# ══════════════════════════════════════════════════════════════════════
# verify_update_blob policy
# ══════════════════════════════════════════════════════════════════════


def test_verify_p2p_good_hash_newer_version(tmp_path):
    path, payload = _write_blob(tmp_path)
    ok, verdict = updater_mod.verify_update_blob(
        path, _info_for(payload, "2.0.0"), "1.0.0", source="p2p"
    )
    assert ok and verdict == "ok"


def test_verify_rejects_bad_hash(tmp_path):
    path, _ = _write_blob(tmp_path)
    info = _info_for(b"different-bytes", "2.0.0")
    ok, verdict = updater_mod.verify_update_blob(path, info, "1.0.0", source="p2p")
    assert not ok and verdict == "hash_mismatch"
    # The GitHub-downloaded file is double-checked against the digest too.
    ok, verdict = updater_mod.verify_update_blob(path, info, "1.0.0", source="github")
    assert not ok and verdict == "hash_mismatch"


@pytest.mark.parametrize(
    "release,current",
    [
        ("1.0.0", "1.0.0"),  # equal
        ("1.0.0", "1.0.1"),  # release older than the running build
        ("0.9", "1.0"),
    ],
)
def test_verify_rejects_not_newer_versions(tmp_path, release, current):
    path, payload = _write_blob(tmp_path)
    ok, verdict = updater_mod.verify_update_blob(
        path, _info_for(payload, release), current, source="p2p"
    )
    assert not ok and verdict == "not_newer"


def test_verify_no_release_info_p2p_rejected_github_ok(tmp_path):
    path, _ = _write_blob(tmp_path)
    # P2P blob with nobody to answer to → must not install.
    ok, verdict = updater_mod.verify_update_blob(path, None, "1.0.0", source="p2p")
    assert not ok and verdict == "no_release_info"
    # A GitHub download was already size+hash-checked during download.
    ok, verdict = updater_mod.verify_update_blob(path, None, "1.0.0", source="github")
    assert ok and verdict == "ok"


# ══════════════════════════════════════════════════════════════════════
# fetch_latest_asset_info
# ══════════════════════════════════════════════════════════════════════


def _patch_platform(monkeypatch, name="clipsync-test.zip"):
    monkeypatch.setattr(updater_mod, "_platform_asset_name", lambda: name)


def test_fetch_asset_info_without_digest_is_none(monkeypatch):
    _patch_platform(monkeypatch)
    monkeypatch.setattr(
        updater_mod,
        "_fetch_latest_release",
        lambda timeout=None: {
            "tag_name": "v2.3.4",
            "assets": [{"name": "clipsync-test.zip"}],  # no digest published
        },
    )
    assert updater_mod.fetch_latest_asset_info() is None


# ══════════════════════════════════════════════════════════════════════
# 4 — install re-entry protection + P2P verification wiring
# ══════════════════════════════════════════════════════════════════════


class _NotifyStub:
    def __init__(self):
        self.shown = []

    def show(self, title, message):
        self.shown.append((title, message))


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    """A bare Application with every external effect of the install path
    stubbed out.  Returns (app, main_module, state)."""
    import src.main as main_mod

    main = main_mod
    notify = _NotifyStub()
    state = {
        "errors": [],
        "cached": [],
        "shown": [],
        "exited": 0,
        "downloads": [],
    }
    monkeypatch.setattr(main, "notification_mgr", notify)
    monkeypatch.setattr(main, "show_error", lambda root, title, msg: state["errors"].append(msg))
    monkeypatch.setattr(main, "show_info", lambda root, title, msg: state["shown"].append(msg))
    monkeypatch.setattr(updater_mod, "cache_asset", lambda p: state["cached"].append(p))
    # Keep the archive stash out of the real Downloads during tests.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    app = main.Application.__new__(main.Application)
    app.root = _FakeRoot()
    app._shutting_down = False
    app._skip_save_on_shutdown = False
    app._updating = False
    app._update_state = {"phase": "idle"}
    app._update_downloading = False
    app.web_server = None
    app._exit_process = lambda: state.__setitem__("exited", state["exited"] + 1)
    app._download_and_install_update = lambda **kw: state["downloads"].append(kw)

    return app, main, state


def test_finish_install_github_happy_path(app_env, monkeypatch, tmp_path):
    """A verified GitHub asset is cached for peers, stashed as the ready
    archive under ~/Downloads/clipsync-update/, and prompted for manual
    install — nothing is auto-applied and the app never self-exits."""
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(
        updater_mod, "fetch_latest_asset_info", lambda timeout=None: _info_for(payload, "2.0.0")
    )
    app._pending_update_version = "2.0.0"

    app._finish_update_install(path, None, "github")

    assert state["cached"] == [path]  # still served to peers (M2)
    assert app._updating is False  # next cycle can install
    assert not Path(path).exists()  # moved, not copied
    assert app._update_state["phase"] == "ready"
    assert app._update_state["version"] == "2.0.0"
    assert app._update_state["path"].endswith(
        os.path.join("clipsync-update", "clipsync-windows.zip")
    )
    assert Path(app._update_state["path"]).exists()
    assert state["shown"], "the manual-install prompt must be shown"
    assert "2.0.0" in state["shown"][0]
    assert state["exited"] == 0  # never self-exits
    assert state["errors"] == []


def test_finish_install_p2p_bad_hash_discarded(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    path, _ = _write_blob(tmp_path, b"TAMPERED")
    monkeypatch.setattr(
        updater_mod, "fetch_latest_asset_info", lambda timeout=None: _info_for(b"original", "2.0.0")
    )

    app._finish_update_install(path, None, "p2p")

    assert not Path(path).exists(), "rejected blob must be deleted"
    assert state["cached"] == []  # unverified bytes are never served
    assert state["shown"] == []  # no ready prompt for a bad blob
    assert state["downloads"] == []  # hash known → no GitHub fallback needed
    assert app._updating is False


def test_finish_install_p2p_without_release_info_falls_back_to_github(
    app_env, monkeypatch, tmp_path
):
    app, main, state = app_env
    path, _ = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info", lambda timeout=None: None)

    app._finish_update_install(path, None, "p2p")

    assert state["downloads"] == [{"from_peers": False}], (
        "fallback must go straight to GitHub without re-broadcasting to peers"
    )
    assert state["cached"] == []  # unverifiable blob never served
    assert Path(path).exists()  # ...and not installed either way


# ══════════════════════════════════════════════════════════════════════
# auto_update_check OFF means zero requests
# ══════════════════════════════════════════════════════════════════════


class _Cfg:
    def __init__(self, enabled):
        self.auto_update_check = enabled


def _check_app(enabled, last_check):
    import src.main as main_mod

    app = main_mod.Application.__new__(main_mod.Application)
    app.cfg = _Cfg(enabled)
    app._last_auto_update_check = last_check
    fired = []
    app._auto_check_for_update = lambda: fired.append(1)
    return app, fired


def test_auto_check_off_never_requests(monkeypatch):
    def _network_touched(*a, **kw):
        raise AssertionError("network layer must not be entered while OFF")

    monkeypatch.setattr(updater_mod, "check_for_update", _network_touched)

    last = time.monotonic() - 7 * 3600  # long overdue
    app, fired = _check_app(False, last)
    app._maybe_auto_update_check()

    assert fired == []
    # Timestamp untouched while OFF so toggling ON starts a fresh window
    # instead of firing an immediate request.
    assert app._last_auto_update_check == last


# ══════════════════════════════════════════════════════════════════════
# config plumbing for auto_update_check
# ══════════════════════════════════════════════════════════════════════


def _point_config_at(tmp_path, monkeypatch, data):
    import internal.config.config as config_module

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: cfg_path)
    return config_module


def test_config_roundtrip_auto_update_check(tmp_path, monkeypatch):
    config_module = _point_config_at(tmp_path, monkeypatch, {})
    cfg = config_module.load()
    assert cfg.auto_update_check is True  # default ON

    cfg.auto_update_check = False
    config_module.save(cfg)

    cfg2 = config_module.load()
    assert cfg2.auto_update_check is False



# ══════════════════════════════════════════════════════════════════════
# 5 — why a check has no answer
#
# "Could not reach the update server" used to be the whole of it: the reason
# was known inside _fetch_latest_release and dropped one frame up, so a rate
# limit, a blocked API host and no network at all were one sentence -- and
# only the last of those is the user's to fix.
# ══════════════════════════════════════════════════════════════════════


class _Response:
    """The part of urlopen's return value that the lookup uses."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _no_retry_delay(monkeypatch):
    """Skip the backoff between attempts: three tries sleep 1.5s in total."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _urlopen_raises(monkeypatch, exc):
    def explode(*args, **kwargs):
        raise exc

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", explode)
    _no_retry_delay(monkeypatch)


def test_a_network_failure_names_itself_in_the_answer(monkeypatch):
    import urllib.error

    _urlopen_raises(monkeypatch, urllib.error.URLError(OSError(10054, "reset by peer")))

    result = updater_mod.check_for_update()

    assert result["available"] is False
    assert result["latest"] == ""
    assert result["reason"] == updater_mod.UNREACHABLE
    # Said in the reader's language, and the OS-level reason is still there for
    # whoever has to work out why -- it is just no longer what is shown.
    assert result["error"] == updater_mod.T("update.error_unreachable")
    assert "10054" in result["detail"]
    assert result["url"] == updater_mod._RELEASES_PAGE


def test_a_rate_limit_is_not_retried(monkeypatch):
    """Retrying a refusal spends the budget the refusal is about.

    The check retried every failure three times, which for a rate limit is
    three requests against the 60 an hour GitHub allows an IP -- from a click
    that could not have worked, and with the third refusal as the answer.
    """
    import io
    import urllib.error

    calls = []

    def explode(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError(
            updater_mod._LATEST_URL,
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps({"message": "API rate limit exceeded"}).encode()),
        )

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", explode)
    _no_retry_delay(monkeypatch)

    result = updater_mod.check_for_update()

    # One request, and the refusal is named rather than quoted: GitHub's own
    # sentence for this is written for a developer and ends with an aside about
    # authenticating, which is not something to put in a status bar.
    assert len(calls) == 1
    assert result["reason"] == updater_mod.RATE_LIMITED
    assert result["error"] == updater_mod.T("update.error_rate_limited")
    assert result["detail"] == "HTTP 403: API rate limit exceeded"


def test_a_release_with_no_tag_is_not_reported_as_a_network_failure(monkeypatch):
    # A 200 that says nothing useful is a different problem with a different
    # answer, and calling it "could not reach the server" would send the user
    # to look at a network that is working.
    monkeypatch.setattr(
        updater_mod.urllib.request, "urlopen", lambda *a, **k: _Response({"assets": []})
    )

    result = updater_mod.check_for_update()

    assert result["available"] is False
    assert result["reason"] == updater_mod.MALFORMED
    assert result["error"] == updater_mod.T("update.error_malformed")
    assert "URLError" not in result["error"]


def test_a_real_answer_carries_no_error(monkeypatch):
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        lambda *a, **k: _Response(
            {"tag_name": "v99.0.0", "html_url": "https://example.invalid/rel"}
        ),
    )

    result = updater_mod.check_for_update()

    assert result["error"] == ""
    assert result["available"] is True
    assert result["latest"] == "v99.0.0"
    assert result["url"] == "https://example.invalid/rel"
