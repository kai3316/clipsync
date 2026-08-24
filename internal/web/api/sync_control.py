"""Timed sync-pause control API (web parity with the tray's pause menu).

The classic UI pauses sync for 15/30/60 minutes from the tray; this module
exposes the same capability to the web dashboard:

  POST /api/sync/pause   {"minutes": 15}   -> pause now, auto-resume later
  POST /api/sync/resume  {}                 -> end the pause immediately

Design notes
------------
* The sync flip itself goes through ``update_settings({"sync_enabled": ...})``
  so the host's live-apply callback (``_on_web_settings_change``) runs the
  exact same path a manual toggle uses — it clears any pending classic pause
  bookkeeping, updates the tray checkbox, and keeps cfg/config.json in step.
* The auto-resume timer lives HERE (the web server shares the app process),
  but every fire re-validates against the persisted deadline
  (``cfg.timed_pause_until``): an explicit toggle anywhere (tray, dashboard,
  another tab) zeroes that field through the host's ``_clear_pause_state``,
  which turns a stale timer into a no-op instead of resurrecting a pause.
* The deadline is persisted like the classic path does, so an app restart or
  auto-update relaunch inside the pause window re-arms the remaining time via
  the host's own ``_restore_timed_pause`` on next launch.

All handlers return a (data_dict, status_code) tuple.
"""

import json
import logging
import threading
import time

from internal.web.api.settings import (
    _persist_preserving_at_rest_private_key,
    update_settings,
)

logger = logging.getLogger(__name__)

# Inclusive bounds for the pause duration, mirroring src/main.py's
# _pause_sync_for_minutes clamp of 1 minute .. 24 hours.
MIN_MINUTES = 1
MAX_MINUTES = 24 * 60

_lock = threading.Lock()
_resume_timer: threading.Timer | None = None


def _persist_cfg(cfg) -> bool:
    """Persist cfg without downgrading an encrypted at-rest private key."""
    try:
        _persist_preserving_at_rest_private_key(cfg)
        return True
    except Exception:
        logger.debug("Failed to persist timed-pause state", exc_info=True)
        return False


def _cancel_timer() -> None:
    """Drop the pending auto-resume timer (idempotent, thread-safe)."""
    global _resume_timer
    with _lock:
        timer = _resume_timer
        _resume_timer = None
    if timer is not None:
        timer.cancel()


def _arm_timer(cfg, on_settings_change, deadline: float, delay_s: float) -> None:
    """Arm the auto-resume timer for *deadline*, replacing any previous one."""
    global _resume_timer
    with _lock:
        previous = _resume_timer
        _resume_timer = None
    if previous is not None:
        previous.cancel()
    timer = threading.Timer(
        max(0.0, delay_s),
        _fire_resume,
        args=(cfg, on_settings_change, deadline),
    )
    timer.daemon = True
    with _lock:
        _resume_timer = timer
    timer.start()


def _fire_resume(cfg, on_settings_change, deadline: float) -> None:
    """Timer body: resume only if THIS pause is still the authoritative one.

    Any explicit sync toggle (from the tray, the desktop dashboard, or another
    web tab) runs the host's ``_clear_pause_state`` / our ``resume_sync``, both
    of which zero ``cfg.timed_pause_until``.  A stale timer whose deadline no
    longer matches therefore does nothing — it must never override a state the
    user chose since.
    """
    try:
        stored = float(getattr(cfg, "timed_pause_until", 0.0) or 0.0)
    except (TypeError, ValueError):
        stored = 0.0
    if stored != deadline:
        return  # superseded by a newer pause or an explicit toggle
    if getattr(cfg, "sync_enabled", True):
        # Sync is already on (e.g. resumed elsewhere without clearing the
        # field) — just drop the leftover deadline so no other consumer sees
        # a phantom countdown.
        cfg.timed_pause_until = 0.0
        _persist_cfg(cfg)
        return
    logger.info("Timed sync pause elapsed (started from web) — resuming")
    resume_sync(b"{}", cfg, on_settings_change)


def pause_sync(body, cfg, on_settings_change=None):
    """POST /api/sync/pause — pause clipboard sync for N minutes.

    Body: {"minutes": <int 1..1440>}.  Pausing over an already-manual pause
    simply attaches the timed resume, matching the tray behavior.
    """
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400
    minutes = data.get("minutes") if isinstance(data, dict) else None
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return {"ok": False, "error": "minutes must be an integer"}, 400
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        return {
            "ok": False,
            "error": f"minutes must be between {MIN_MINUTES} and {MAX_MINUTES}",
        }, 400

    # Flip sync off through the standard settings path so the host applies it
    # live exactly like a manual toggle (and clears its own pause bookkeeping
    # first).  When sync is already off there is nothing to flip — attaching a
    # timed resume to a manual pause mirrors the tray menu.
    if getattr(cfg, "sync_enabled", False):
        payload = json.dumps({"sync_enabled": False}).encode("utf-8")
        result, status = update_settings(payload, cfg, on_settings_change)
        if status != 200 or not isinstance(result, dict) or not result.get("ok"):
            return {
                "ok": False,
                "error": (result or {}).get("error", "failed to disable sync"),
            }, 500

    deadline = time.time() + minutes * 60
    cfg.timed_pause_until = deadline
    _persist_cfg(cfg)
    _arm_timer(cfg, on_settings_change, deadline, minutes * 60.0)

    logger.info("Sync paused for %d minute(s) via web", minutes)
    return {"ok": True, "minutes": minutes, "until": deadline}, 200


def resume_sync(body, cfg, on_settings_change=None):
    """POST /api/sync/resume — end a timed (or manual) pause right now."""
    _cancel_timer()
    had_pause = False
    try:
        had_pause = float(getattr(cfg, "timed_pause_until", 0.0) or 0.0) > 0.0
    except (TypeError, ValueError):
        had_pause = False
    cfg.timed_pause_until = 0.0
    _persist_cfg(cfg)

    resumed = False
    if not getattr(cfg, "sync_enabled", True):
        # Route the re-enable through the host's live-apply callback so the
        # sync manager, tray checkbox, and config all move together (this is
        # what _on_web_settings_change already does for a settings save).
        if on_settings_change is not None:
            try:
                on_settings_change({"sync_enabled": True}, {})
                resumed = True
            except Exception:
                logger.exception("Live resume after timed pause failed")
        else:
            cfg.sync_enabled = True
            resumed = True
    elif had_pause:
        # Sync was already on; an explicit resume still clears the deadline.
        resumed = True

    logger.info(
        "Sync resumed via web (%s)",
        "timed pause" if had_pause else "no active pause",
    )
    return {"ok": True, "resumed": resumed}, 200
