"""Settings API handlers.

Only safe-to-expose settings are returned. Secrets (tokens, keys,
passwords, hashes) are never sent to clients.

All handlers return a (data_dict, status_code) tuple.
"""

import csv
import json
import logging
import os
from datetime import datetime

from internal.config.config import _config_path
from internal.config.config import save as save_config
from internal.i18n import T

logger = logging.getLogger(__name__)

# Fields that are safe to expose to web clients
_SAFE_FIELDS = {
    "device_id",
    "device_name",
    "language",
    "appearance_mode",
    "sync_enabled",
    "plain_text_only",
    "encryption_enabled",
    "notifications_enabled",
    "notify_device_connect",
    "notify_transfer",
    "notify_pairing",
    "notify_sync",
    "web_enabled",
    "web_port",
    "web_history_limit",
    "history_max_entries",
    "history_max_age_days",
    "file_receive_dir",
    "sync_debounce",
    "clipboard_poll_interval",
    "max_reconnect_attempts",
    "transfer_timeout",
    "log_level",
    "auto_start",
    "port",
    "service_type",
    "app_filter_enabled",
    "app_filter_mode",
    "app_filter_list",
    "filter_enabled_categories",
    "source_tracking_enabled",
    "sound_enabled",
    "ui_animation_enabled",
    "ui_backend",
    "translate_url",
    "translate_key_set",
    "paste_to_top",
    "low_memory_mode",
    "retry_capture_enabled",
    "dedup_method",
    "auto_update_check",
    # Internet (cross-network) relay sync.  The secrets (relay_secret,
    # peer_relay_secrets) are deliberately NOT exposed: they never leave the
    # device except over the TLS LAN channel, and a token holder must not be
    # able to read or overwrite them through this API.
    "internet_sync_enabled",
    "relay_brokers",
    "data_dir",
    "favorites_path",
    "hotkeys",
    "hotkeys_enabled",
    # AI-config sync watch list (Round 12): root directories whose config
    # files are inventoried and shareable with paired peers.
    "ai_config_paths",
    # Wall-clock epoch of a pending timed sync pause (0 = none).  Read-only:
    # the pause/resume actions go through /api/sync/pause|resume, which run
    # their own live-apply + timer bookkeeping; a direct client write here
    # could never arm the auto-resume timer.
    "timed_pause_until",
}

# Numeric fields the API accepts, with inclusive (lo, hi) bounds.  The web
# frontend validates its own forms, but this endpoint is reachable from any
# LAN client holding the token — a nonsense value (web_port = 1,
# sync_debounce = 99) gets persisted and can leave the network layer
# unable to start after a restart, so validate on the server too.
_RANGE_LIMITS = {
    "web_port": (1024, 65535),
    "port": (1024, 65535),
    "web_history_limit": (1, 500),
    "history_max_entries": (10, 10000),
    # Retention window in days; 0 disables age-based pruning entirely.
    "history_max_age_days": (0, 36500),
    "sync_debounce": (0.05, 10.0),
    "clipboard_poll_interval": (0.1, 60.0),
    "max_reconnect_attempts": (0, 100),
    "transfer_timeout": (5, 3600),
}

# Fields that the client is allowed to modify
_MUTABLE_FIELDS = {
    "device_name",
    "language",
    "appearance_mode",
    "sync_enabled",
    "plain_text_only",
    "encryption_enabled",
    "notifications_enabled",
    "notify_device_connect",
    "notify_transfer",
    "notify_pairing",
    "notify_sync",
    "web_enabled",
    "web_port",
    "web_history_limit",
    "history_max_entries",
    "history_max_age_days",
    "file_receive_dir",
    "sync_debounce",
    "clipboard_poll_interval",
    "max_reconnect_attempts",
    "transfer_timeout",
    "log_level",
    "port",
    "service_type",
    "auto_start",
    "app_filter_enabled",
    "app_filter_mode",
    "app_filter_list",
    "filter_enabled_categories",
    "source_tracking_enabled",
    "sound_enabled",
    "ui_animation_enabled",
    "ui_backend",
    "translate_url",
    "paste_to_top",
    "low_memory_mode",
    "retry_capture_enabled",
    "dedup_method",
    "auto_update_check",
    "internet_sync_enabled",
    "relay_brokers",
    "data_dir",
    "favorites_path",
    "hotkeys",
    "hotkeys_enabled",
    "ai_config_paths",
}

# Action keys that trigger a host-side operation rather than a plain
# config-field mutation.  These are forwarded to on_settings_change so the
# application can regenerate/clear the web token, set/clear the encryption
# password, or perform a factory reset.
_SPECIAL_ACTIONS = {
    "password",
    "clear_password",
    "factory_reset",
    "regenerate_web_token",
    "clear_web_token",
    "set_translate_key",
    "clear_translate_key",
}

# Keys the host application may return from ``on_settings_change`` that are
# safe to echo back to the web client.  Secrets — notably the encryption
# password and its verification hash — are deliberately absent.
# ``web_token`` IS included deliberately: only an authenticated caller can
# reach this endpoint, and after a regenerate the dashboard needs the new
# token to rewrite its own URL before reloading — without it the page
# reloads against the old (now invalid) token and lands on the
# "link expired" dead end instead of staying usable.
_SAFE_RESPONSE_KEYS = {
    "password_set",
    "ok",
    "token_updated",
    "web_token",
    "translate_key_set",
}


def get_settings(cfg, get_internet_sync_state=None):
    """Return safe-to-expose settings (exclude secrets).

    *get_internet_sync_state*, when provided, returns the live internet-sync
    relay state (one of off/connecting/online/error) so a freshly loaded
    dashboard shows the current state without waiting for the next WS
    ``relay_state`` transition event.
    """
    result = {}
    for field in _SAFE_FIELDS:
        if hasattr(cfg, field):
            result[field] = getattr(cfg, field)

    # Expose only *whether* an encryption password is configured, never the
    # password or its verification hash. The web UI uses this to show the
    # correct "set / not set" state and enable the clear-password action.
    result["password_set"] = bool(
        getattr(cfg, "encryption_password_hash", "") or getattr(cfg, "encryption_password", "")
    )

    # Expose only *whether* a translation API key is configured, never the
    # key itself (see module docstring).
    result["translate_key_set"] = bool(getattr(cfg, "translate_api_key", ""))

    # Live internet-sync state (never a secret).  When sync is disabled the
    # state is definitively "off" — no callback needed; otherwise fall back to
    # "connecting" when the host cannot be asked (callback absent/failed).
    if not getattr(cfg, "internet_sync_enabled", False):
        result["internet_sync_state"] = "off"
    else:
        state = ""
        if get_internet_sync_state is not None:
            try:
                state = str(get_internet_sync_state() or "")
            except Exception:
                logger.debug("get_internet_sync_state callback failed",
                             exc_info=True)
        result["internet_sync_state"] = state if state in (
            "off", "connecting", "online", "error") else "connecting"

    return {"settings": result}, 200


def _type_mismatch(old, new) -> bool:
    """Return True when ``new`` can't safely replace ``old`` in the config.

    Guards against a client writing a string into an int field (e.g.
    ``{"web_port": "not-a-port"}``), which would otherwise corrupt the config
    and crash a live service on its next read.  ``bool`` is a subclass of
    ``int``, so int fields reject booleans explicitly.
    """
    if old is None:
        return False
    if isinstance(old, bool):
        return not isinstance(new, bool)
    if isinstance(old, int):
        return not isinstance(new, int) or isinstance(new, bool)
    if isinstance(old, float):
        return not isinstance(new, (int, float)) or isinstance(new, bool)
    if isinstance(old, str):
        return not isinstance(new, str)
    if isinstance(old, (list, tuple, dict, set)):
        return not isinstance(new, type(old))
    return False


def _persist_preserving_at_rest_private_key(cfg) -> None:
    """Persist cfg to config.json without writing the private key in plaintext.

    ``save_config(cfg)`` writes ``cfg.private_key_pem`` verbatim when no
    encryption manager is supplied.  In memory that value is the *plaintext*
    key loaded at startup, so doing that would downgrade an encrypted config
    to plaintext on disk.  Instead, when no encryption manager is available,
    preserve whatever is already stored on disk (the encrypted blob when
    encryption is enabled) and only update the non-secret fields.
    """
    stored = {}
    try:
        stored = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    original = cfg.private_key_pem
    cfg.private_key_pem = stored.get("private_key_pem", "")
    try:
        save_config(cfg)
    finally:
        cfg.private_key_pem = original


def update_settings(body, cfg, on_settings_change=None, enc_mgr=None):
    """Update settings from request body (only safe fields).

    If *on_settings_change* is provided, it is called with the ``updated``
    dict plus any ``special`` action keys after persistence so the host
    application can apply the changes to its live services immediately
    (rather than on next restart).

    *enc_mgr* is the encryption manager used to re-encrypt the device private
    key at rest.  When it is None (callback not wired yet), persistence keeps
    the already-stored at-rest private key instead of writing the in-memory
    plaintext value (see ``_persist_preserving_at_rest_private_key``).
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    updated = {}
    for field in _MUTABLE_FIELDS:
        if field in data:
            new_val = data[field]
            old_val = getattr(cfg, field, None)
            if _type_mismatch(old_val, new_val):
                logger.warning(
                    "Settings update rejected for %s: type mismatch (%s -> %s)",
                    field, type(old_val).__name__, type(new_val).__name__,
                )
                continue
            limits = _RANGE_LIMITS.get(field)
            if limits is not None:
                try:
                    in_range = limits[0] <= float(new_val) <= limits[1]
                except (TypeError, ValueError):
                    in_range = False
                if not in_range:
                    logger.warning(
                        "Settings update rejected for %s: value %r outside "
                        "%s..%s", field, new_val, limits[0], limits[1],
                    )
                    continue
            setattr(cfg, field, new_val)
            updated[field] = new_val
            logger.info("Settings updated: %s = %s", field, new_val)

    # A language change made through the web UI is a REAL first-run choice:
    # mark it so the desktop picker (gated by cfg.language_chosen) and the
    # web wizard gate (__CLIPSYNC_FRESH__ = not language_chosen) stop
    # disagreeing with the web UI's own language selector.  Without this,
    # picking a language in the web settings left language_chosen False, the
    # native picker re-nagged on every launch, and the config kept reading as
    # "fresh" to every later page load.
    if "language" in updated and not getattr(cfg, "language_chosen", False):
        cfg.language_chosen = True
        logger.info("language_chosen set via web language selection")

    # Action keys the host application handles (not plain config fields).
    special = {k: data[k] for k in _SPECIAL_ACTIONS if k in data}

    if not updated and not special:
        return {"ok": False, "error": "no valid fields to update"}, 400

    try:
        if enc_mgr is not None:
            save_config(cfg, enc_mgr)
        else:
            _persist_preserving_at_rest_private_key(cfg)
        logger.debug("Config persisted after settings update")
    except Exception as e:
        logger.error("Failed to persist settings: %s", e)
        return {"ok": False, "error": "failed to persist settings"}, 500

    result = None
    if on_settings_change is not None:
        try:
            result = on_settings_change(updated, special)
        except Exception as e:
            logger.error("Failed to apply settings live: %s", e)

    response = {"ok": True, "updated": updated}
    if isinstance(result, dict):
        # Only whitelisted (non-secret) keys may be merged into the response.
        for key in _SAFE_RESPONSE_KEYS:
            if key in result:
                response[key] = result[key]
    return response, 200


# ---------------------------------------------------------------------------
# Data export / import
# ---------------------------------------------------------------------------


def export_data(body, cfg, history):
    """Export clipboard history to a persistent file (JSON, CSV, or Markdown).

    Request body: {"format": "json" | "csv" | "markdown"}
    Writes a uniquely-named file to the user's Downloads folder (falling back
    to the app data directory) and leaves it in place so the user can open or
    move it.  Returns the durable path and filename.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    # Coerce to str first: a non-string "format" (number/null) would raise
    # AttributeError on .lower() and surface as a misleading 500.
    fmt = str(data.get("format", "json")).lower()
    if fmt not in ("json", "csv", "markdown"):
        return {"ok": False, "error": "unsupported format (use json, csv or markdown)"}, 400

    suffix = {"json": ".json", "csv": ".csv", "markdown": ".md"}[fmt]
    from internal.config.config import _config_dir
    from pathlib import Path
    # Downloads is where users expect exported files; if it can't be
    # determined, fall back to the app data dir (always present).
    downloads = Path.home() / "Downloads"
    dest_dir = downloads if downloads.is_dir() else Path(_config_dir())
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        dest_dir = Path(_config_dir())
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"clipsync-history-{ts}{suffix}"
    dest_path = str(dest_dir / filename)

    try:
        from internal.data.export import (
            export_history_csv,
            export_history_json,
            export_history_markdown,
        )
        if fmt == "json":
            count = export_history_json(history, dest_path)
        elif fmt == "markdown":
            count = export_history_markdown(history, dest_path)
        else:
            count = export_history_csv(history, dest_path)
        # The export functions chmod the file 0600 (plaintext clipboard
        # content).  Do NOT delete it — the whole point is that the user can
        # find and use this file.
        return {"ok": True, "filepath": dest_path, "filename": filename,
                "count": count, "format": fmt}, 200
    except Exception as exc:
        logger.exception("Export failed")
        # Remove a partial export on failure so no broken file is left behind.
        try:
            os.unlink(dest_path)
        except OSError:
            pass
        return {"ok": False, "error": str(exc)}, 500


def import_data(body, cfg, history):
    """Import clipboard history from a JSON or CSV file path.

    Request body: {"filepath": "/path/to/file.json"}
    Accepts any existing regular-file path the user points at (no confinement
    to the app data dir), still caps the file at ~10 MB and validates that it
    parses as a ClipSync export before applying anything.  The source file is
    only ever read, never written.  Returns the count of imported items.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    filepath = data.get("filepath", "").strip()
    if not filepath:
        return {"ok": False, "error": T("web.import_missing_path")}, 400

    filepath = os.path.expanduser(filepath)
    if not os.path.isfile(filepath):
        return {"ok": False, "error": T("web.import_file_not_found", path=filepath)}, 404

    # Confine import to where a legitimate export/backup actually lands — the
    # app data dir (backups) and the user's Downloads folder (manual exports) —
    # so a token holder can't read arbitrary files off the disk by importing
    # them into history and reading the content back out.
    from internal.config.config import _config_dir
    data_root = os.path.realpath(_config_dir())
    downloads_root = os.path.realpath(os.path.join(os.path.expanduser("~"), "Downloads"))
    real = os.path.realpath(filepath)
    if not any(real == r or real.startswith(r + os.sep) for r in (data_root, downloads_root)):
        return {"ok": False, "error": T("web.import_path_not_allowed")}, 400

    # Cap at ~10 MB so a huge or mistaken file can't be slurped into memory.
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return {"ok": False, "error": T("web.import_file_not_found", path=filepath)}, 404
    if size > 10 * 1024 * 1024:
        return {"ok": False, "error": T("web.import_file_too_large")}, 413

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in (".json", ".csv"):
        return {"ok": False, "error": T("web.import_unsupported_format")}, 415

    # Validate before applying so a malformed file fails with a clear
    # localized error instead of a generic 500 mid-import.
    try:
        if ext == ".json":
            with open(filepath, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
                return {"ok": False, "error": T("web.import_invalid_content")}, 400
            # Require ClipSync export fields so a foreign JSON array (e.g. a
            # browser/app config dump) can't be ingested and read back through
            # the history API.  An empty list is a valid no-op import.
            if not all(("text_preview" in item or "types" in item or "content_type" in item)
                       for item in parsed):
                return {"ok": False, "error": T("web.import_invalid_content")}, 400
        else:
            with open(filepath, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                expected = {"timestamp", "content_type", "text_preview"}
                if not expected.intersection(reader.fieldnames or []):
                    return {"ok": False, "error": T("web.import_invalid_content")}, 400
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Import validation failed for %s", filepath, exc_info=True)
        return {"ok": False, "error": T("web.import_invalid_content")}, 400

    try:
        from internal.data.export import import_history_csv, import_history_json
        if ext == ".json":
            count = import_history_json(filepath, history)
        else:
            count = import_history_csv(filepath, history)
        return {"ok": True, "imported": count}, 200
    except Exception as exc:
        logger.exception("Import failed")
        return {"ok": False, "error": str(exc)}, 500


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


def create_backup_api(cfg, history):
    """Create a full backup zip and return its path."""
    try:
        from internal.data.backup import create_backup
        path = create_backup(cfg, history)
        return {"ok": True, "backup_path": path}, 200
    except Exception as exc:
        logger.exception("Backup creation failed")
        return {"ok": False, "error": str(exc)}, 500


def restore_backup_api(body, cfg, history):
    """Restore from a backup zip file.

    Request body: {"backup_path": "/path/to/backup.zip"}
    Creates a pre-restore backup of the current state first (a safety net so
    the user can undo a restore), then restores.  Returns a summary of
    restored items plus the pre-restore backup path.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    backup_path = data.get("backup_path", "").strip()
    if not backup_path:
        return {"ok": False, "error": "missing backup_path"}, 400

    from internal.config.config import _config_dir
    from internal.web.api.security import confine_path
    safe_path = confine_path(backup_path, _config_dir())
    if safe_path is None:
        return {"ok": False, "error": "backup_path must be inside the ClipSync data directory"}, 400
    backup_path = str(safe_path)

    # Safety net: snapshot the current state before overwriting it, so a
    # restore can always be undone from the backups list.  A failure to
    # snapshot must not block the restore itself — just leave the field None.
    pre_restore_backup = None
    try:
        from internal.data.backup import create_backup
        pre_restore_backup = create_backup(cfg, history)
    except Exception as exc:
        logger.warning("Failed to create pre-restore backup: %s", exc)
        pre_restore_backup = None

    try:
        from internal.data.backup import restore_backup
        summary = restore_backup(backup_path, cfg, history)
        # Persist the restored config immediately.  A restore that only mutates
        # the in-memory Config is silently discarded if the process crashes
        # before some unrelated save happens, and the restored values would be
        # lost on the next start.  This handler has no encryption manager, so
        # reuse the at-rest-private-key-preserving helper used by
        # update_settings to avoid downgrading an encrypted key to plaintext.
        try:
            _persist_preserving_at_rest_private_key(cfg)
            logger.info("Restored config persisted to disk")
        except Exception as exc:
            # A save failure must not fail the restore request — the in-memory
            # restore already succeeded — but it must not pass silently either.
            logger.error("Failed to persist restored config: %s", exc)
        return {
            "ok": True,
            "summary": summary,
            "restored": summary,
            "pre_restore_backup": pre_restore_backup,
            "needs_restart": True,
        }, 200
    except FileNotFoundError:
        return {"ok": False, "error": "backup file not found"}, 404
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    except Exception as exc:
        logger.exception("Restore failed")
        return {"ok": False, "error": str(exc)}, 500


def list_backups_api(backup_dir: str | None = None):
    """Return a list of available backups."""
    try:
        from internal.data.backup import list_backups
        backups = list_backups(backup_dir)
        return {"ok": True, "backups": backups}, 200
    except Exception as exc:
        logger.exception("List backups failed")
        return {"ok": False, "error": str(exc)}, 500
