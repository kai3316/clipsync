"""Settings API handlers.

Only safe-to-expose settings are returned. Secrets (tokens, keys,
passwords, hashes) are never sent to clients.

All handlers return a (data_dict, status_code) tuple.
"""

import json
import logging
import tempfile
import os

from internal.config.config import save as save_config

logger = logging.getLogger(__name__)

# Fields that are safe to expose to web clients
_SAFE_FIELDS = {
    "device_id",
    "device_name",
    "language",
    "appearance_mode",
    "sync_enabled",
    "encryption_enabled",
    "notifications_enabled",
    "web_enabled",
    "web_port",
    "web_history_limit",
    "history_max_entries",
    "file_receive_dir",
    "sync_debounce",
    "clipboard_poll_interval",
    "max_reconnect_attempts",
    "transfer_timeout",
    "log_level",
    "auto_start",
    "port",
    "service_type",
    "relay_url",
    "app_filter_enabled",
    "app_filter_mode",
    "app_filter_list",
    "filter_enabled_categories",
    "source_tracking_enabled",
    "sound_enabled",
    "ui_animation_enabled",
    "ui_backend",
}

# Fields that the client is allowed to modify
_MUTABLE_FIELDS = {
    "device_name",
    "language",
    "appearance_mode",
    "sync_enabled",
    "encryption_enabled",
    "notifications_enabled",
    "web_enabled",
    "web_port",
    "web_history_limit",
    "history_max_entries",
    "file_receive_dir",
    "sync_debounce",
    "clipboard_poll_interval",
    "max_reconnect_attempts",
    "transfer_timeout",
    "log_level",
    "port",
    "service_type",
    "relay_url",
    "auto_start",
    "app_filter_enabled",
    "app_filter_mode",
    "app_filter_list",
    "filter_enabled_categories",
    "source_tracking_enabled",
    "sound_enabled",
    "ui_animation_enabled",
    "ui_backend",
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
}

# Keys the host application may return from ``on_settings_change`` that are
# safe to echo back to the web client.  Secrets — notably ``token`` — are
# deliberately absent, so a regenerate/clear action can never leak a
# credential through this API (see the module docstring).
_SAFE_RESPONSE_KEYS = {
    "password_set",
    "ok",
    "token_updated",
}


def get_settings(cfg):
    """Return safe-to-expose settings (exclude secrets)."""
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


def update_settings(body, cfg, on_settings_change=None):
    """Update settings from request body (only safe fields).

    If *on_settings_change* is provided, it is called with the ``updated``
    dict plus any ``special`` action keys after persistence so the host
    application can apply the changes to its live services immediately
    (rather than on next restart).
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
            setattr(cfg, field, new_val)
            updated[field] = new_val
            logger.info("Settings updated: %s = %s", field, new_val)

    # Action keys the host application handles (not plain config fields).
    special = {k: data[k] for k in _SPECIAL_ACTIONS if k in data}

    if not updated and not special:
        return {"ok": False, "error": "no valid fields to update"}, 400

    try:
        save_config(cfg)
        logger.debug("Config persisted after settings update")
    except Exception as e:
        logger.error("Failed to persist settings: %s", e)

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
    """Export clipboard history to a temp file (JSON or CSV).

    Request body: {"format": "json" | "csv"}
    Returns the path to the exported file.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    fmt = data.get("format", "json").lower()
    if fmt not in ("json", "csv"):
        return {"ok": False, "error": "unsupported format (use json or csv)"}, 400

    suffix = ".json" if fmt == "json" else ".csv"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="clipsync_export_")
    os.close(fd)

    try:
        from internal.data.export import export_history_json, export_history_csv
        if fmt == "json":
            count = export_history_json(history, tmp_path)
        else:
            count = export_history_csv(history, tmp_path)
        return {"ok": True, "filepath": tmp_path, "count": count, "format": fmt}, 200
    except Exception as exc:
        logger.exception("Export failed")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return {"ok": False, "error": str(exc)}, 500


def import_data(body, cfg, history):
    """Import clipboard history from a JSON or CSV file path.

    Request body: {"filepath": "/path/to/file.json"}
    Returns the count of imported items.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    filepath = data.get("filepath", "").strip()
    if not filepath:
        return {"ok": False, "error": "missing filepath"}, 400

    # Only allow importing from ClipSync's own data directory — a client
    # supplied path must not read arbitrary files on the host.
    from internal.config.config import _config_dir
    from internal.web.api.security import confine_path
    safe_path = confine_path(filepath, _config_dir())
    if safe_path is None:
        return {"ok": False, "error": "filepath must be inside the ClipSync data directory"}, 400
    filepath = str(safe_path)

    if not os.path.isfile(filepath):
        return {"ok": False, "error": "file not found"}, 404

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in (".json", ".csv"):
        return {"ok": False, "error": "unsupported file format (use json or csv)"}, 400

    try:
        from internal.data.export import import_history_json, import_history_csv
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
    Returns a summary of restored items.
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

    try:
        from internal.data.backup import restore_backup
        summary = restore_backup(backup_path, cfg, history)
        return {"ok": True, "summary": summary}, 200
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
