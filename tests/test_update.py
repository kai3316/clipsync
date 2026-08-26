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
  6. `.old` fallback copies: the Linux helper copies before replace.
  7. Config plumbing: auto_update_check persists and the web settings API
     accepts booleans only.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.system import applier as applier_mod
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


@pytest.mark.parametrize("raw,expected", [
    ("v1.0.4", (1, 0, 4)),
    ("V1.0.4", (1, 0, 4)),
    ("1.0.4", (1, 0, 4)),
    ("1.0.4-beta", (1, 0, 4)),       # non-numeric tail dropped per chunk
    ("1.0.4rc2", (1, 0, 4)),
    ("1.0", (1, 0)),
    ("", ()),
    ("beta", ()),
])
def test_parse_version_boundaries(raw, expected):
    assert updater_mod._parse_version(raw) == expected


@pytest.mark.parametrize("latest,current,expected", [
    ("1.0.5", "1.0.4", True),
    ("1.0.4", "1.0.4", False),        # equal is not newer
    ("v1.0.4", "1.0.4", False),
    ("1.0", "1.0.0", False),          # padded comparison
    ("1.0", "1.0.4", False),          # shorter pads with zeros
    ("1.0.4", "1.0", True),
    ("1.10", "1.9", True),            # numeric, not lexicographic
    ("1.0.4-beta", "1.0.4", False),   # beta suffix parses equal
    ("2.0", "1.99.99", True),
])
def test_is_newer_boundaries(latest, current, expected):
    assert updater_mod._is_newer(latest, current) is expected


# ══════════════════════════════════════════════════════════════════════
# 2 — verify_update_blob policy
# ══════════════════════════════════════════════════════════════════════


def test_verify_p2p_good_hash_newer_version(tmp_path):
    path, payload = _write_blob(tmp_path)
    ok, verdict = updater_mod.verify_update_blob(
        path, _info_for(payload, "2.0.0"), "1.0.0", source="p2p")
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
        str(tmp_path / "gone.zip"), _info_for(b"x", "2.0.0"), "1.0.0", source="p2p")
    assert not ok and verdict == "hash_mismatch"


@pytest.mark.parametrize("release,current", [
    ("1.0.0", "1.0.0"),   # equal
    ("1.0.0", "1.0.1"),   # release older than the running build
    ("0.9", "1.0"),
])
def test_verify_rejects_not_newer_versions(tmp_path, release, current):
    path, payload = _write_blob(tmp_path)
    ok, verdict = updater_mod.verify_update_blob(
        path, _info_for(payload, release), current, source="p2p")
    assert not ok and verdict == "not_newer"


def test_verify_no_release_info_p2p_rejected_github_ok(tmp_path):
    path, _ = _write_blob(tmp_path)
    # P2P blob with nobody to answer to → must not install.
    ok, verdict = updater_mod.verify_update_blob(
        path, None, "1.0.0", source="p2p")
    assert not ok and verdict == "no_release_info"
    # A GitHub download was already size+hash-checked during download.
    ok, verdict = updater_mod.verify_update_blob(
        path, None, "1.0.0", source="github")
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
    monkeypatch.setattr(updater_mod, "_fetch_latest_release", lambda timeout=None: {
        "tag_name": "v2.3.4",
        "assets": [{"name": "clipsync-other.zip", "digest": "sha256:ff"},
                   {"name": "clipsync-test.zip",
                    "digest": "sha256:" + "ab" * 32}],
    })
    info = updater_mod.fetch_latest_asset_info()
    assert info == {"version": "v2.3.4", "asset": "clipsync-test.zip",
                    "sha256": "ab" * 32}


def test_fetch_asset_info_without_digest_is_none(monkeypatch):
    _patch_platform(monkeypatch)
    monkeypatch.setattr(updater_mod, "_fetch_latest_release", lambda timeout=None: {
        "tag_name": "v2.3.4",
        "assets": [{"name": "clipsync-test.zip"}],   # no digest published
    })
    assert updater_mod.fetch_latest_asset_info() is None


def test_fetch_asset_info_no_matching_asset(monkeypatch):
    _patch_platform(monkeypatch)
    monkeypatch.setattr(updater_mod, "_fetch_latest_release", lambda timeout=None: {
        "tag_name": "v2.3.4", "assets": [],
    })
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
        "staged": [],
        "applied": [],
        "cached": [],
        "exited": 0,
        "downloads": [],
    }
    monkeypatch.setattr(main, "notification_mgr", notify)
    monkeypatch.setattr(
        main, "show_error",
        lambda root, title, msg: state["errors"].append(msg))

    def _stage(p):
        state["staged"].append(p)
        return tmp_path / "staged.bin"

    def _apply(staged):
        state["applied"].append(staged)
        return True

    def _cache(p):
        state["cached"].append(p)

    monkeypatch.setattr(applier_mod, "stage_update", _stage)
    monkeypatch.setattr(applier_mod, "apply_and_restart", _apply)
    monkeypatch.setattr(updater_mod, "cache_asset", _cache)

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


def _run_scheduled_exit(app, state):
    """Simulate Tk firing the delayed self-exit after a successful apply."""
    exits = [(d, fn) for d, fn in app.root.scheduled if fn == app._exit_process]
    assert len(exits) == 1 and exits[0][0] == 300
    exits[0][1]()
    assert state["exited"] == 1


def test_finish_install_github_happy_path(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    # win32 dev host: auto-apply only exists on Linux/macOS.
    monkeypatch.setattr(sys, "platform", "linux")
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(payload, "2.0.0"))

    app._finish_update_install(path, None, "github")

    assert state["cached"] == [path]
    assert len(state["staged"]) == 1
    assert len(state["applied"]) == 1
    assert app._skip_save_on_shutdown is True
    assert Path(path).exists()          # consumed, not deleted
    _run_scheduled_exit(app, state)     # the delayed self-exit is armed


def test_finish_install_second_arrival_skipped(app_env, monkeypatch, tmp_path):
    """GitHub download finishing and a P2P blob arriving around the same time
    must produce exactly ONE install."""
    app, main, state = app_env
    # win32 dev host: auto-apply only exists on Linux/macOS.
    monkeypatch.setattr(sys, "platform", "linux")
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(payload, "2.0.0"))

    app._finish_update_install(path, None, "github")   # wins
    app._finish_update_install(path, None, "p2p")      # latecomer

    assert len(state["staged"]) == 1
    assert len(state["applied"]) == 1
    _run_scheduled_exit(app, state)


def test_finish_install_flag_cleared_on_stage_failure(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    # win32 dev host: auto-apply only exists on Linux/macOS.
    monkeypatch.setattr(sys, "platform", "linux")
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(payload, "2.0.0"))
    monkeypatch.setattr(applier_mod, "stage_update", lambda p: None)

    app._finish_update_install(path, None, "github")

    assert state["errors"], "stage failure must surface an error"
    assert app._updating is False      # retry remains possible
    assert state["exited"] == 0


def test_finish_install_flag_cleared_on_apply_failure(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    # win32 dev host: auto-apply only exists on Linux/macOS.
    monkeypatch.setattr(sys, "platform", "linux")
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(payload, "2.0.0"))
    monkeypatch.setattr(applier_mod, "apply_and_restart", lambda s: False)

    app._finish_update_install(path, None, "github")

    assert state["errors"]
    assert app._updating is False
    assert state["exited"] == 0


def test_finish_install_p2p_bad_hash_discarded(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    path, _ = _write_blob(tmp_path, b"TAMPERED")
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(b"original", "2.0.0"))

    app._finish_update_install(path, None, "p2p")

    assert not Path(path).exists(), "rejected blob must be deleted"
    assert state["staged"] == [] and state["applied"] == []
    assert state["cached"] == []       # unverified bytes are never served
    assert state["downloads"] == []    # hash known → no GitHub fallback needed
    assert app._updating is False


def test_finish_install_p2p_old_version_discarded(app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(payload, "0.0.1"))

    app._finish_update_install(path, None, "p2p")

    assert not Path(path).exists()
    assert state["staged"] == []
    assert app._updating is False


def test_finish_install_p2p_without_release_info_falls_back_to_github(
        app_env, monkeypatch, tmp_path):
    app, main, state = app_env
    path, _ = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: None)

    app._finish_update_install(path, None, "p2p")

    assert state["downloads"] == [{"from_peers": False}], (
        "fallback must go straight to GitHub without re-broadcasting to peers")
    assert state["staged"] == []       # unverifiable blob never staged
    assert Path(path).exists()         # ...and not installed either way


def test_finish_install_github_without_release_info_proceeds(
        app_env, monkeypatch, tmp_path):
    """The GitHub file was verified during download; a failed *second* lookup
    must not block a legitimate install."""
    app, main, state = app_env
    # win32 dev host: auto-apply only exists on Linux/macOS.
    monkeypatch.setattr(sys, "platform", "linux")
    path, _ = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: None)

    app._finish_update_install(path, None, "github")

    assert len(state["applied"]) == 1
    _run_scheduled_exit(app, state)
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

    last = time.monotonic() - 7 * 3600   # long overdue
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
    app, fired = _check_app(True, time.monotonic())   # just checked
    app._maybe_auto_update_check()
    assert fired == []


# ══════════════════════════════════════════════════════════════════════
# 6 — .old rollback copies
# ══════════════════════════════════════════════════════════════════════


def test_linux_backup_copies_current_binary(tmp_path):
    cur = tmp_path / "clipsync"
    cur.write_bytes(b"CURRENT-BINARY")
    old = applier_mod._backup_current_binary(cur)
    assert old == tmp_path / "clipsync.old"
    assert old.read_bytes() == b"CURRENT-BINARY"
    assert cur.read_bytes() == b"CURRENT-BINARY"   # copy, original intact


def test_linux_backup_failure_never_raises(tmp_path, monkeypatch):
    cur = tmp_path / "clipsync"
    cur.write_bytes(b"x")

    def _boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(applier_mod.shutil, "copyfile", _boom)
    assert applier_mod._backup_current_binary(cur) is None


# ══════════════════════════════════════════════════════════════════════
# 7 — config + web settings plumbing for auto_update_check
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
    assert cfg.auto_update_check is True          # default ON

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
    resp, status = update_settings(
        json.dumps({"auto_update_check": "yes"}).encode(), cfg)
    assert status == 400
    assert cfg.auto_update_check is True

    # A real boolean is applied and persisted.
    resp, status = update_settings(
        json.dumps({"auto_update_check": False}).encode(), cfg)
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
    en = json.loads((root / "internal/web/static/locales/en.json").read_text(
        encoding="utf-8"))
    zh = json.loads((root / "internal/web/static/locales/zh-CN.json").read_text(
        encoding="utf-8"))
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
# 8 — Windows manual-run flow (no auto-apply) + download progress
# ══════════════════════════════════════════════════════════════════════


def test_finish_install_windows_ready_no_auto_apply(
        app_env, monkeypatch, tmp_path):
    """On Windows the verified asset is never auto-applied: it is prepared as
    a runnable exe at a known path and the user is prompted to run it."""
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(updater_mod, "fetch_latest_asset_info",
                        lambda timeout=None: _info_for(payload, "2.0.0"))
    app._pending_update_version = "2.0.0"
    shown = []
    monkeypatch.setattr(main, "show_info",
                        lambda root, title, msg: shown.append(msg))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _extract(src, dst):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"NEW-EXE")
        return True
    monkeypatch.setattr(updater_mod, "extract_update_exe", _extract)

    app._finish_update_install(path, None, "github")

    assert state["staged"] == [] and state["applied"] == []
    assert state["cached"] == [path]      # still served to peers (M2)
    assert app._updating is False
    assert app._update_state["phase"] == "ready"
    assert app._update_state["path"].endswith("clipsync.exe")
    assert app._update_state["version"] == "2.0.0"
    assert shown, "the manual-run prompt must be shown on the desktop"
    assert "2.0.0" in shown[0] and "clipsync.exe" in shown[0]
    assert state["exited"] == 0           # the app must NOT exit on Windows


def test_finish_install_windows_prepare_failure_surfaces(
        app_env, monkeypatch, tmp_path):
    """If the exe extraction fails the state flips to `failed` and the error
    surfaces — no silent 'ready' with a missing file."""
    app, main, state = app_env
    path, payload = _write_blob(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(updater_mod, "extract_update_exe", lambda s, d: False)

    app._finish_update_install(path, None, "github")

    assert app._update_state["phase"] == "failed"
    assert state["errors"], "extract failure must surface an error"
    assert app._updating is False


def test_extract_update_exe(tmp_path):
    """extract_update_exe unpacks the clipsync.exe member of a release zip."""
    import zipfile
    exe_bytes = b"MZ-NEW-CLIPSYNC-EXE"
    zip_path = tmp_path / "clipsync-windows.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("clipsync.exe", exe_bytes)
        z.writestr("readme.txt", "ignored")

    out = tmp_path / "out" / "clipsync.exe"
    out.parent.mkdir(parents=True, exist_ok=True)
    assert updater_mod.extract_update_exe(str(zip_path), str(out)) is True
    assert out.read_bytes() == exe_bytes


def test_extract_update_exe_missing_member_returns_false(tmp_path):
    import zipfile
    zip_path = tmp_path / "no-exe.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("payload.bin", b"x")
    assert updater_mod.extract_update_exe(
        str(zip_path), str(tmp_path / "clipsync.exe")) is False


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
    monkeypatch.setattr(updater_mod, "_fetch_latest_release", lambda timeout=60.0: {
        "tag_name": "v2.0.0",
        "assets": [{
            # The downloader picks the asset by _platform_asset_name(), which
            # on this win32 host is clipsync-windows.zip.
            "name": "clipsync-windows.zip",
            "browser_download_url": "https://example.com/asset",
            "size": total,
            "digest": digest,
        }],
    })
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    progress = []
    path, reason, version = updater_mod.download_latest_release(
        str(tmp_path), progress_cb=lambda d, t: progress.append((d, t)))

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
