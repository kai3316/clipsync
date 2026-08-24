"""Round 10 classic→web parity tests.

Covers the two features ported from the classic desktop UI to the web UI:

  1. Timed sync pause (tray menu parity) — POST /api/sync/pause and
     POST /api/sync/resume:
       - pausing flips sync off through the standard settings live-apply
         path, persists a deadline in cfg.timed_pause_until, and arms the
         auto-resume timer;
       - pausing over an already-manual pause attaches the timer without a
         redundant flip (mirrors the tray menu);
       - resume re-enables sync via the same live-apply callback and clears
         the persisted deadline;
       - a stale auto-resume timer whose deadline was superseded (an explicit
         toggle anywhere zeroes timed_pause_until) is a no-op;
       - input validation (minutes bounds, non-integer, invalid JSON);
       - GET /api/settings exposes timed_pause_until read-only.
  2. Settings search box (classic round-8 parity, frontend-only) plus the
     new i18n keys — wiring guards over routes/api.js/components/locales.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.web.routes import dispatch
from internal.web.api import sync_control
from internal.web.api import settings as settings_api


# ── Helpers ────────────────────────────────────────────────────────────


class _Cfg:
    """Minimal config stand-in for the settings/sync-control handlers."""

    def __init__(self):
        self.device_id = "dev1"
        self.device_name = "Dev"
        self.web_token = ""
        self.sync_enabled = True
        self.timed_pause_until = 0.0
        self.private_key_pem = ""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Keep every test off the real config.json and leak no timers."""
    # update_settings persists through this helper (enc_mgr is None on this
    # path); a no-op keeps the stub _Cfg from overwriting real config data.
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key",
        lambda cfg: None,
    )
    yield
    sync_control._cancel_timer()


def _noop_persist(monkeypatch):
    monkeypatch.setattr(sync_control, "_persist_cfg", lambda c: True)


def _post_pause(cfg, minutes=15, on_settings_change=None):
    return dispatch(
        "POST", "/api/sync/pause",
        {}, json.dumps({"minutes": minutes}).encode("utf-8"),
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None,
        on_settings_change=on_settings_change,
    )


def _post_resume(cfg, on_settings_change=None):
    return dispatch(
        "POST", "/api/sync/resume", {}, b"{}",
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None,
        on_settings_change=on_settings_change,
    )


# ── 1a. Pause endpoint ────────────────────────────────────────────────

def test_pause_disables_sync_and_sets_deadline(monkeypatch):
    cfg = _Cfg()
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_pause(
        cfg, minutes=15,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )
    data = json.loads(body_b)

    assert status == 200
    assert data["ok"] is True and data["minutes"] == 15
    # Sync went off through the standard settings path (live-apply callback).
    assert applied == [{"sync_enabled": False}]
    assert cfg.sync_enabled is False
    # Deadline persisted ≈ now + 15 min.
    assert cfg.timed_pause_until == pytest.approx(time.time() + 15 * 60, abs=5)
    assert data["until"] == pytest.approx(cfg.timed_pause_until, abs=1e-6)


def test_pause_when_already_paused_attaches_timer_without_flip(monkeypatch):
    """A manual pause with no deadline: pause attaches the timed resume but
    must NOT call sync_enabled=False again (mirrors the tray menu)."""
    cfg = _Cfg()
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_pause(
        cfg, minutes=30,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )

    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert applied == []
    assert cfg.timed_pause_until == pytest.approx(time.time() + 30 * 60, abs=5)


def test_pause_replaces_previous_deadline_and_timer(monkeypatch):
    cfg = _Cfg()
    _noop_persist(monkeypatch)

    _post_pause(cfg, minutes=60)
    first = cfg.timed_pause_until

    _post_pause(cfg, minutes=15)
    second = cfg.timed_pause_until

    assert first != second
    assert second == pytest.approx(time.time() + 15 * 60, abs=5)


@pytest.mark.parametrize("minutes", [0, -5, 1441, "abc", None])
def test_pause_rejects_out_of_range_minutes(monkeypatch, minutes):
    cfg = _Cfg()
    _noop_persist(monkeypatch)
    before = cfg.timed_pause_until

    status, _ct, body_b = _post_pause(cfg, minutes=minutes)
    data = json.loads(body_b)

    assert status == 400
    assert data["ok"] is False
    assert cfg.sync_enabled is True, "no flip may happen on invalid input"
    assert cfg.timed_pause_until == before


def test_pause_invalid_json_400():
    cfg = _Cfg()
    status, _ct, body_b = dispatch(
        "POST", "/api/sync/pause", {}, b"{not-json",
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None,
    )
    assert status == 400
    assert json.loads(body_b)["ok"] is False


# ── 1b. Resume endpoint ───────────────────────────────────────────────

def test_resume_reenables_sync_via_live_apply(monkeypatch):
    cfg = _Cfg()
    cfg.sync_enabled = False
    cfg.timed_pause_until = time.time() + 900
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_resume(
        cfg,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )
    data = json.loads(body_b)

    assert status == 200
    assert data["ok"] is True and data["resumed"] is True
    assert applied == [{"sync_enabled": True}]
    assert cfg.timed_pause_until == 0.0


def test_resume_with_sync_already_on_only_clears_deadline(monkeypatch):
    cfg = _Cfg()
    cfg.sync_enabled = True
    cfg.timed_pause_until = time.time() + 900  # leftover field, e.g. restored
    _noop_persist(monkeypatch)
    applied = []

    status, _ct, body_b = _post_resume(
        cfg,
        on_settings_change=lambda updated, special: applied.append(
            dict(updated)),
    )

    assert status == 200
    assert json.loads(body_b)["resumed"] is True
    assert applied == [], "must not force another enable when already on"
    assert cfg.timed_pause_until == 0.0


# ── 1c. Stale-timer guard ─────────────────────────────────────────────

def test_stale_timer_is_noop_after_explicit_toggle(monkeypatch):
    """An explicit toggle anywhere zeroes timed_pause_until; a stale armed
    timer firing later must not resurrect/kill that state."""
    cfg = _Cfg()
    cfg.timed_pause_until = 12345.0   # some other (newer/cleared) state
    cfg.sync_enabled = True           # user re-enabled manually meanwhile
    _noop_persist(monkeypatch)
    calls = []

    sync_control._fire_resume(
        cfg, lambda u, s: calls.append(dict(u)), deadline=999.0)

    assert calls == []


def test_timer_fires_resume_only_for_matching_deadline(monkeypatch):
    cfg = _Cfg()
    cfg.timed_pause_until = time.time() - 1  # expired while we watch
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    calls = []

    sync_control._fire_resume(
        cfg, lambda u, s: calls.append(dict(u)),
        deadline=cfg.timed_pause_until,
    )

    assert calls == [{"sync_enabled": True}]
    assert cfg.timed_pause_until == 0.0


def test_armed_timer_resumes_after_expiry(monkeypatch):
    """End-to-end: arm the daemon timer for a fraction of a second, wait,
    and it resumes sync through the live-apply callback exactly once."""
    cfg = _Cfg()
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    seen = []

    def _fake_resume(body, c, cb=None):
        seen.append(cb)
        if cb is not None:
            cb({"sync_enabled": True}, {})
        return {"ok": True}, 200

    monkeypatch.setattr(sync_control, "resume_sync", _fake_resume)

    deadline = time.time() + 0.05
    cfg.timed_pause_until = deadline
    sync_control._arm_timer(cfg, lambda u, s: None, deadline, 0.05)

    give_up = time.time() + 3
    while not seen and time.time() < give_up:
        time.sleep(0.02)

    assert len(seen) == 1 and seen[0] is not None


def test_superseded_timer_does_not_fire(monkeypatch):
    """A newer pause replaced the armed timer: the OLD firing must be inert
    even if it already left the gate."""
    cfg = _Cfg()
    cfg.sync_enabled = False
    _noop_persist(monkeypatch)
    seen = []
    monkeypatch.setattr(
        sync_control, "resume_sync",
        lambda body, c, cb=None: seen.append(True) or ({"ok": True}, 200),
    )

    old_deadline = time.time() - 0.01
    sync_control._fire_resume(cfg, lambda u, s: None, deadline=old_deadline)
    # cfg.deadline is 0 (no pause) ≠ old_deadline → nothing fired.
    assert seen == []


# ── 1d. Settings exposure ─────────────────────────────────────────────

def test_get_settings_exposes_timed_pause_until_readonly():
    cfg = _Cfg()
    cfg.timed_pause_until = 1234.5

    data, status = settings_api.get_settings(cfg)
    assert status == 200
    assert data["settings"]["timed_pause_until"] == 1234.5

    # Not mutable through POST /api/settings: only /api/sync/pause|resume may
    # move it (they own the live-apply + timer bookkeeping).
    payload = json.dumps({"timed_pause_until": 999.0}).encode()
    _data, status2 = settings_api.update_settings(payload, cfg, None)
    assert cfg.timed_pause_until == 1234.5
    # The request carried no valid fields → rejected outright.
    assert status2 == 400


# ── 1e. Onboarding wizard freshness bug (user-reported) ──────────────

class _RecordingWsManager:
    def __init__(self):
        self.calls = []

    def broadcast(self, message_type, data=None):
        self.calls.append((message_type, data))


class _FakeDialogMgr:
    def __init__(self):
        self.ws_manager = _RecordingWsManager()


def test_restore_broadcasts_onboarding_when_config_goes_fresh(monkeypatch):
    """Restoring a backup whose config has language_chosen=False flips the
    app back to fresh-install state.  __CLIPSYNC_FRESH__ is baked in at page
    serve time, so a live dashboard would never re-surface the wizard without
    this WS nudge (the reported 'wizard only appears after refresh' bug)."""
    from internal.web import routes as routes_module

    cfg = _Cfg()
    cfg.language_chosen = True

    def _fake_restore(body, c, history):
        # The restored config is fresh: no language chosen yet.
        c.language_chosen = False
        return {"ok": True, "summary": {}}, 200

    monkeypatch.setattr(routes_module, "restore_backup_api", _fake_restore)
    dlg = _FakeDialogMgr()

    status, _ct, body_b = dispatch(
        "POST", "/api/restore", {},
        json.dumps({"backup_path": "whatever.zip"}).encode("utf-8"),
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None, dialog_mgr=dlg,
    )

    assert status == 200
    assert json.loads(body_b)["ok"] is True
    assert dlg.ws_manager.calls == [("onboarding_required", None)] or [
        c[0] for c in dlg.ws_manager.calls] == ["onboarding_required"]


def test_restore_no_broadcast_when_config_stays_chosen(monkeypatch):
    """An ordinary restore that keeps language_chosen=True must NOT pop the
    wizard on every connected client."""
    from internal.web import routes as routes_module

    cfg = _Cfg()
    cfg.language_chosen = True

    def _fake_restore(body, c, history):
        return {"ok": True, "summary": {}}, 200

    monkeypatch.setattr(routes_module, "restore_backup_api", _fake_restore)
    dlg = _FakeDialogMgr()

    dispatch(
        "POST", "/api/restore", {},
        json.dumps({"backup_path": "whatever.zip"}).encode("utf-8"),
        cfg=cfg, history=None, sync_mgr=None,
        get_connected_ids=lambda: [], upload_dir=".",
        on_nav_url=None, on_forward_file=None, dialog_mgr=dlg,
    )

    assert dlg.ws_manager.calls == []


def test_web_language_choice_marks_language_chosen():
    """Choosing a language through POST /api/settings counts as a real
    first-run choice: without this, language_chosen stayed False and the
    desktop picker kept re-nagging while every later page load read as
    'fresh'."""
    cfg = _Cfg()
    cfg.language = "en"
    cfg.language_chosen = False

    data, status = settings_api.update_settings(
        json.dumps({"language": "zh-CN"}).encode(), cfg, None)

    assert status == 200
    assert data["updated"]["language"] == "zh-CN"
    assert cfg.language_chosen is True


def test_web_language_choice_idempotent_when_already_chosen():
    cfg = _Cfg()
    cfg.language = "en"
    cfg.language_chosen = True

    settings_api.update_settings(
        json.dumps({"language": "en"}).encode(), cfg, None)

    assert cfg.language_chosen is True


def test_onboarding_wiring_end_to_end():
    routes = _read_repo_file("internal/web/routes.py")
    assert '"onboarding_required"' in routes
    ws_js = _read_repo_file("internal/web/static/js/ws.js")
    assert "case 'onboarding_required':" in ws_js
    assert "store.showOnboarding = true;" in ws_js
    assert "clipsync_onboarded" in ws_js


# ── 2. Frontend wiring guards ─────────────────────────────────────────


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_repo_file(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_routes_registered_and_wired():
    routes = _read_repo_file("internal/web/routes.py")
    assert 'path == "/api/sync/pause"' in routes
    assert 'path == "/api/sync/resume"' in routes
    assert "pause_sync, resume_sync" in routes


def test_api_js_wrappers_exist():
    api_js = _read_repo_file("internal/web/static/js/api.js")
    assert "pauseSync: function (minutes)" in api_js
    assert "'/api/sync/pause'" in api_js
    assert "resumeSync: function ()" in api_js
    assert "'/api/sync/resume'" in api_js


def test_overview_panel_pause_ui_wired():
    panel = _read_repo_file(
        "internal/web/static/components/overview-panel.js")
    # Countdown computed from the exposed setting + local tick.
    assert "timed_pause_until" in panel
    assert "pauseLeftMin" in panel
    assert "pauseSync(15)" in panel
    assert "ClipsyncAPI.resumeSync()" in panel
    # Expired countdown resyncs server truth once.
    assert "getSettings" in panel


def test_settings_search_box_wired():
    panel = _read_repo_file(
        "internal/web/static/components/settings-panel.js")
    assert "searchQuery" in panel
    assert "SETTINGS_SEARCH_KEYS" in panel
    assert "onSearchEnter" in panel
    assert "settings-dialog__search" in panel
    html = _read_repo_file("internal/web/static/index.html")
    assert ".settings-dialog__search" in html
    assert ".settings-dialog__tab--dim" in html
    assert ".overview-pause-btn" in html


def test_new_i18n_keys_present_in_both_locales():
    keys = [
        "overview.pause_for",
        "overview.paused_left",
        "overview.resume_now",
        "overview.pause_15m",
        "overview.pause_30m",
        "overview.pause_1h",
        "overview.paused_toast",
        "overview.resumed",
        "overview.pause_failed",
        "settings.search_placeholder",
        "settings.search_no_matches",
    ]
    base = os.path.join(_ROOT, "internal", "web", "static", "locales")
    with open(os.path.join(base, "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(base, "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    for k in keys:
        assert k in en, f"missing {k} in en.json"
        assert k in zh, f"missing {k} in zh-CN.json"
        assert isinstance(en[k], str) and isinstance(zh[k], str)
