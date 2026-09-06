"""Round 6 — auto-update closing-gap tests.

Groups covered:
  1. Version parsing/comparison boundaries (_parse_version/_is_newer):
     v-prefix, beta suffix, uneven segment counts, equality.
  2. verify_update_blob policy: bad hash, old/equal version, missing release
     info (P2P rejects + GitHub proceeds), GitHub double-checks too.
  3. fetch_latest_asset_info: digest present / missing / unreachable.
  4. Install re-entry protection: concurrent GitHub + P2P arrivals collapse
     to one install; failure paths clear the flag again.
  5. auto_update_check switch OFF → the periodic loop makes zero requests
     (network layer guarded), ON → fires when due, throttle respected.
  6. Config plumbing: auto_update_check persists and the web settings API
     accepts booleans only.
  7. Manual-install archive flow: the verified asset is cached for peers and
     stashed under ``~/Downloads/clipsync-update/`` with a ready state + prompt
     telling the user to replace the old install — never auto-applied, and a
     stash failure surfaces ``failed`` and clears the flag.
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
# 1 — version parsing / comparison boundaries
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v1.0.4", (1, 0, 4)),
        ("V1.0.4", (1, 0, 4)),
        ("1.0.4", (1, 0, 4)),
        ("1.0.4-beta", (1, 0, 4)),  # non-numeric tail dropped per chunk
        ("1.0.4rc2", (1, 0, 4)),
        ("1.0", (1, 0)),
        ("", ()),
        ("beta", ()),
    ],
)
def test_parse_version_boundaries(raw, expected):
    assert updater_mod._parse_version(raw) == expected


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("1.0.5", "1.0.4", True),
        ("1.0.4", "1.0.4", False),  # equal is not newer
        ("v1.0.4", "1.0.4", False),
        ("1.0", "1.0.0", False),  # padded comparison
        ("1.0", "1.0.4", False),  # shorter pads with zeros
        ("1.0.4", "1.0", True),
        ("1.10", "1.9", True),  # numeric, not lexicographic
        ("1.0.4-beta", "1.0.4", False),  # beta suffix parses equal
        ("2.0", "1.99.99", True),
    ],
)
def test_is_newer_boundaries(latest, current, expected):
    assert updater_mod._is_newer(latest, current) is expected


# ══════════════════════════════════════════════════════════════════════
# 2 — verify_update_blob policy
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


def test_verify_missing_file_is_hash_mismatch(tmp_path):
    ok, verdict = updater_mod.verify_update_blob(
        str(tmp_path / "gone.zip"), _info_for(b"x", "2.0.0"), "1.0.0", source="p2p"
    )
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


def test_verify_empty_digest_behaves_like_no_info(tmp_path):
    path, payload = _write_blob(tmp_path)
    info = {"version": "2.0.0", "asset": "a.zip", "sha256": ""}
    ok, verdict = updater_mod.verify_update_blob(path, info, "1.0.0", source="p2p")
    assert not ok and verdict == "no_release_info"


# ══════════════════════════════════════════════════════════════════════
# 3 — fetch_latest_asset_info
# ══════════════════════════════════════════════════════════════════════


def _patch_platform(monkeypatch, name="clipsync-test.zip"):
    monkeypatch.setattr(updater_mod, "_platform_asset_name", lambda: name)


def test_fetch_asset_info_with_digest(monkeypatch):
    _patch_platform(monkeypatch)
    monkeypatch.setattr(
        updater_mod,
        "_fetch_latest_release",
        lambda timeout=None: {
            "tag_name": "v2.3.4",
            "assets": [
                {"name": "clipsync-other.zip", "digest": "sha256:ff"},
                {"name": "clipsync-test.zip", "digest": "sha256:" + "ab" * 32},
            ],
        },
    )
    info = updater_mod.fetch_latest_asset_info()
    assert info == {"version": "v2.3.4", "asset": "clipsync-test.zip", "sha256": "ab" * 32}


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


def test_fetch_asset_info_no_matching_asset(monkeypatch):
    _patch_platform(monkeypatch)
    monkeypatch.setattr(
        updater_mod,
        "_fetch_latest_release",
        lambda timeout=None: {
            "tag_name": "v2.3.4",
            "assets": [],
        },
    )
    assert updater_mod.fetch_latest_asset_info() is None
    monkeypatch.setattr(updater_mod, "_fetch_latest_release", lambda timeout=None: None)
    assert updater_mod.fetch_latest_asset_info() is None


def test_fetch_asset_info_never_raises(monkeypatch):
    def _boom(timeout=None):
        raise RuntimeError("network down")

    _patch_platform(monkeypatch)
    monkeypatch.setattr(updater_mod, "_fetch_latest_release", _boom)
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


def test_finish_install_second_arrival_skipped(app_env, monkeypatch, tmp_path):
    """A GitHub download finishing while a P2P blob is mid-verify must
    collapse to exactly ONE stashed archive and one prompt."""
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(
        updater_mod, "fetch_latest_asset_info", lambda timeout=None: _info_for(payload, "2.0.0")
    )

    seen = []
    real_cache = updater_mod.cache_asset

    def _cache_and_reenter(p):
        seen.append(p)
        # A concurrent arrival lands while this install is still in flight —
        # it must be skipped by the re-entry guard, not installed again.
        app._finish_update_install(path, None, "p2p")
        real_cache(p)

    monkeypatch.setattr(updater_mod, "cache_asset", _cache_and_reenter)

    app._finish_update_install(path, None, "github")

    assert seen == [path]  # first arrival cached once
    assert state["cached"] == [path]
    assert len(state["shown"]) == 1  # exactly one ready prompt
    assert app._update_state["phase"] == "ready"


def test_finish_install_archive_stash_failure_surfaces(app_env, monkeypatch, tmp_path):
    """If the archive cannot be stashed the state flips to `failed` and the
    error surfaces — no silent 'ready' with a missing file, and the flag is
    cleared so a retry stays possible."""
    import shutil

    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(
        updater_mod, "fetch_latest_asset_info", lambda timeout=None: _info_for(payload, "2.0.0")
    )
    monkeypatch.setattr(
        shutil, "move", lambda src, dst: (_ for _ in ()).throw(OSError("disk full"))
    )

    app._finish_update_install(path, None, "github")

    assert app._update_state["phase"] == "failed"
    assert state["errors"], "stash failure must surface an error"
    assert app._updating is False  # retry remains possible
    assert state["exited"] == 0


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


def test_finish_install_p2p_old_version_discarded(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(
        updater_mod, "fetch_latest_asset_info", lambda timeout=None: _info_for(payload, "0.0.1")
    )

    app._finish_update_install(path, None, "p2p")

    assert not Path(path).exists()
    assert state["cached"] == []
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


def test_finish_install_github_without_release_info_proceeds(app_env, monkeypatch, tmp_path):
    """The GitHub file was verified during download; a failed *second* lookup
    must not block a legitimate install."""
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info", lambda timeout=None: None)
    app._pending_update_version = "2.0.0"

    app._finish_update_install(path, None, "github")

    assert state["cached"] == [path]
    assert app._update_state["phase"] == "ready"
    assert state["shown"]
    assert state["errors"] == []


# ══════════════════════════════════════════════════════════════════════
# 5 — auto_update_check OFF means zero requests
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


def test_auto_check_on_fires_when_due():
    app, fired = _check_app(True, time.monotonic() - 7 * 3600)
    app._maybe_auto_update_check()
    assert fired == [1]


def test_auto_check_on_respects_throttle():
    app, fired = _check_app(True, time.monotonic())  # just checked
    app._maybe_auto_update_check()
    assert fired == []


# ══════════════════════════════════════════════════════════════════════
# 6 — config + web settings plumbing for auto_update_check
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


def test_web_settings_accept_bool_only(tmp_path, monkeypatch):
    from internal.web.api.settings import get_settings, update_settings

    config_module = _point_config_at(tmp_path, monkeypatch, {})
    cfg = config_module.load()
    assert cfg.auto_update_check is True

    # Wrong type (string instead of bool) is rejected without touching the field.
    resp, status = update_settings(json.dumps({"auto_update_check": "yes"}).encode(), cfg)
    assert status == 400
    assert cfg.auto_update_check is True

    # A real boolean is applied and persisted.
    resp, status = update_settings(json.dumps({"auto_update_check": False}).encode(), cfg)
    assert status == 200 and resp["updated"]["auto_update_check"] is False

    # The field is exposed to clients…
    data, _ = get_settings(cfg)
    assert data["settings"]["auto_update_check"] is False
    # …and survives a reload from disk.
    assert config_module.load().auto_update_check is False


# ══════════════════════════════════════════════════════════════════════
# i18n parity for the new keys
# ══════════════════════════════════════════════════════════════════════


def test_i18n_key_parity_for_new_update_keys():
    import internal.i18n as i18n

    new_keys = [
        "notify.update_rejected_hash",
        "notify.update_rejected_old",
        "settings_window.auto_update_check",
        "settings_window.auto_update_check_hint",
        "settings_window.update_downloading_progress",
        "settings_window.update_ready",
        "settings_window.update_ready_hint",
        "settings_window.update_open_folder",
        "tray.update_ready_prompt",
    ]
    for key in new_keys:
        assert key in i18n._EN, key
        assert key in i18n._ZH, key


def test_web_locale_parity_for_new_keys():
    root = Path(__file__).resolve().parent.parent
    en = json.loads((root / "internal/web/static/locales/en.json").read_text(encoding="utf-8"))
    zh = json.loads((root / "internal/web/static/locales/zh-CN.json").read_text(encoding="utf-8"))
    new_keys = [
        "settings_window.auto_update_check",
        "settings_window.auto_update_check_hint",
        "settings_window.update_check_now",
        "settings_window.up_to_date",
        "settings_window.update_check_failed",
        "settings_window.update_available_found",
        "settings_window.update_install_now",
        "settings_window.update_installing",
        "settings_window.update_downloading_progress",
        "settings_window.update_ready",
        "settings_window.update_ready_hint",
        "settings_window.update_open_folder",
        "tray.update_ready_prompt",
    ]
    for key in new_keys:
        assert key in en and key in zh, key


# ══════════════════════════════════════════════════════════════════════
# 7 — manual-install archive flow + download progress
# ══════════════════════════════════════════════════════════════════════


def test_download_latest_release_reports_progress(monkeypatch, tmp_path):
    """The downloader streams chunk-wise and reports monotonic progress."""
    import hashlib as _hashlib
    import urllib.request

    chunk = b"x" * 64 * 1024
    payload = chunk * 3
    total = len(payload)

    class _FakeResp:
        headers = {"Content-Length": str(total)}

        def __init__(self):
            self._buf = bytearray(payload)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def _fake_urlopen(req, timeout=None, context=None):
        return _FakeResp()

    digest = "sha256:" + _hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        updater_mod,
        "_fetch_latest_release",
        lambda timeout=60.0: {
            "tag_name": "v2.0.0",
            "assets": [
                {
                    # The downloader picks the asset by _platform_asset_name(), so the
                    # mock must expose this host's asset: clipsync-windows.zip on win32,
                    # clipsync-linux.tar.gz on Linux, clipsync-macos-arm64.zip on macOS.
                    "name": updater_mod._platform_asset_name(),
                    "browser_download_url": "https://example.com/asset",
                    "size": total,
                    "digest": digest,
                }
            ],
        },
    )
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    progress = []
    path, reason, version = updater_mod.download_latest_release(
        str(tmp_path), progress_cb=lambda d, t: progress.append((d, t))
    )

    assert path and os.path.isfile(path)
    assert version == "v2.0.0"
    assert reason is None
    assert os.path.getsize(path) == total  # size + sha256 verified on the way in
    assert progress and progress[-1] == (total, total)
    seen = -1
    for d, t in progress:
        assert d >= seen, "progress must be monotonic"
        seen = d
        assert t == total
