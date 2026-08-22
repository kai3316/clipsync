"""Backup and restore for ClipSync data.

Creates timestamped zip archives containing config.json, history.json,
and favorites.json.  Favorites are exported from SQLite when the DB is
in use, falling back to the legacy JSON file when the DB doesn't exist.
"""

import json
import logging
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from internal.config.config import Config

if TYPE_CHECKING:
    from internal.clipboard.history import ClipboardHistory
    from internal.clipboard.history_db import ClipboardHistoryDB

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR_NAME = "backups"


def _get_backup_dir(backup_dir: str | None = None) -> Path:
    """Return the backup directory, creating it if necessary."""
    from internal.config.config import _config_dir
    base = Path(backup_dir) if backup_dir else _config_dir() / DEFAULT_BACKUP_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _get_favorites_path() -> Path:
    """Return the path to the legacy favorites JSON file."""
    from internal.config.config import _config_dir
    return _config_dir() / "favorites.json"


def _get_favorites_db_path() -> Path:
    """Return the path to the SQLite favorites database."""
    from internal.config.config import _config_dir
    return _config_dir() / "favorites.db"


def _export_favorites_to_json(filepath: Path) -> bool:
    """Export favorites from SQLite to a JSON file for backup.

    Returns True if any favorites were written, False if the DB is
    empty or inaccessible.
    """
    import sqlite3
    db_path = _get_favorites_db_path()
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id, title, content, \"group\", position, created, updated "
            "FROM favorites ORDER BY created DESC"
        ).fetchall()
        conn.close()
    except Exception:
        return False

    if not rows:
        return False

    favorites = []
    for row in rows:
        entry = {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "group": row[3],
            "position": row[4],
            "created": row[5],
        }
        if row[6] is not None:
            entry["updated"] = row[6]
        favorites.append(entry)

    filepath.write_text(
        json.dumps(favorites, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return True


def _import_favorites_from_json(filepath: Path) -> int:
    """Import favorites from a JSON file into the SQLite database.

    Returns the number of imported entries.
    """
    import sqlite3
    if not filepath.is_file():
        return 0

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(data, list):
        return 0

    db_path = _get_favorites_db_path()
    os.makedirs(db_path.parent, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS favorites (
                id       TEXT PRIMARY KEY,
                title    TEXT NOT NULL DEFAULT '',
                content  TEXT NOT NULL DEFAULT '',
                "group"  TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created  REAL NOT NULL,
                updated  REAL
            );
        """)
        conn.commit()

        count = 0
        with conn:
            for item in data:
                conn.execute(
                    "INSERT OR REPLACE INTO favorites "
                    "(id, title, content, \"group\", position, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.get("id", ""),
                        item.get("title", ""),
                        item.get("content", ""),
                        item.get("group", ""),
                        item.get("position", 0),
                        item.get("created", 0),
                        item.get("updated"),
                    ),
                )
                count += 1
        return count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_backup(
    cfg: Config,
    history: "ClipboardHistory | ClipboardHistoryDB",
    backup_dir: str | None = None,
) -> str:
    """Create a timestamped backup zip file.

    The archive contains:
      - config.json   (full config export)
      - history.json  (full history export)
      - favorites.json (exported from SQLite DB or copied from JSON file)

    Returns the absolute path to the backup file.
    """
    from internal.data.export import export_history_json

    base = _get_backup_dir(backup_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    zip_path = base / f"clipsync_backup_{ts}.zip"

    with tempfile.TemporaryDirectory(prefix="clipsync_backup_") as tmp:
        tmpdir = Path(tmp)

        # --- config.json ---
        data = {
            "device_id": cfg.device_id,
            "device_name": cfg.device_name,
            "port": cfg.port,
            "service_type": cfg.service_type,
            "sync_enabled": cfg.sync_enabled,
            "auto_start": cfg.auto_start,
            "filter_enabled_categories": cfg.filter_enabled_categories,
            "relay_url": cfg.relay_url,
            "history_max_entries": cfg.history_max_entries,
            "file_receive_dir": cfg.file_receive_dir,
            "sync_debounce": cfg.sync_debounce,
            "clipboard_poll_interval": cfg.clipboard_poll_interval,
            "max_reconnect_attempts": cfg.max_reconnect_attempts,
            "transfer_timeout": cfg.transfer_timeout,
            "log_level": cfg.log_level,
            "notifications_enabled": cfg.notifications_enabled,
            "encryption_enabled": cfg.encryption_enabled,
            "appearance_mode": cfg.appearance_mode,
            "language": cfg.language,
            "paste_to_top": cfg.paste_to_top,
            "low_memory_mode": cfg.low_memory_mode,
            "retry_capture_enabled": cfg.retry_capture_enabled,
            "dedup_method": cfg.dedup_method,
            "app_filter_enabled": cfg.app_filter_enabled,
            "app_filter_mode": cfg.app_filter_mode,
            "app_filter_list": cfg.app_filter_list,
            "source_tracking_enabled": cfg.source_tracking_enabled,
            "ui_backend": cfg.ui_backend,
            "ui_animation_enabled": cfg.ui_animation_enabled,
            "sound_enabled": cfg.sound_enabled,
            "web_enabled": cfg.web_enabled,
            "web_port": cfg.web_port,
            "web_history_limit": cfg.web_history_limit,
            "peers": [
                {
                    "device_id": p.device_id,
                    "device_name": p.device_name,
                    "paired": p.paired,
                    "notes": p.notes,
                }
                for p in cfg.peers.values()
            ],
        }
        (tmpdir / "config.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
        )

        # --- history.json ---
        hist_path = str(tmpdir / "history.json")
        export_history_json(history, hist_path)

        # --- favorites.json ---
        # Try SQLite export first, fall back to copying legacy JSON file
        fav_json_path = tmpdir / "favorites.json"
        exported = _export_favorites_to_json(fav_json_path)
        if not exported:
            # Fall back: copy legacy JSON file if it exists
            leg_path = _get_favorites_path()
            if leg_path.is_file():
                import shutil
                shutil.copy2(str(leg_path), str(fav_json_path))

        # --- Create zip ---
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for child in tmpdir.iterdir():
                zf.write(str(child), arcname=child.name)

    # The archive contains plaintext clipboard history and config data; keep
    # it private (not world-readable).
    try:
        os.chmod(str(zip_path), 0o600)
    except OSError:
        pass

    logger.info("Backup created: %s", zip_path)
    return str(zip_path)


def restore_backup(
    backup_path: str,
    cfg: Config,
    history: "ClipboardHistory | ClipboardHistoryDB",
) -> dict:
    """Restore from a backup zip file.

    Returns a summary dict: {"config": bool, "history": int, "favorites": int}.
    The 'config' field is True if config.json was found in the backup.
    """
    from internal.data.export import import_history_json

    result: dict = {"config": False, "history": 0, "favorites": 0}

    zip_path = Path(backup_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    if not zipfile.is_zipfile(str(zip_path)):
        raise ValueError(f"Not a valid zip file: {backup_path}")

    def _assert_safe_zip_member(name: str) -> None:
        """Reject zip members that would escape the extraction dir.

        Defense-in-depth against zip-slip: the backup is created by our own
        create_backup, but a restored archive could be hand-crafted.  Reject
        absolute paths, drive letters and any ``..`` path component.
        """
        norm = name.replace("\\", "/")
        if norm.startswith("/") or norm.startswith("\\"):
            raise ValueError(f"Unsafe absolute path in backup: {name!r}")
        # Drive letter (Windows): e.g. "C:/..."
        if len(norm) >= 2 and norm[1] == ":":
            raise ValueError(f"Unsafe drive path in backup: {name!r}")
        if ".." in [part for part in norm.split("/")]:
            raise ValueError(f"Unsafe '..' path in backup: {name!r}")

    with tempfile.TemporaryDirectory(prefix="clipsync_restore_") as tmp:
        tmpdir = Path(tmp)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            for info in zf.infolist():
                _assert_safe_zip_member(info.filename)
            zf.extractall(str(tmpdir))

        # --- Restore config ---
        config_file = tmpdir / "config.json"
        if config_file.is_file():
            try:
                raw = json.loads(config_file.read_text(encoding="utf-8"))
                _apply_config(raw, cfg)
                result["config"] = True
                logger.info("Restored config from backup")
            except Exception as exc:
                logger.warning("Failed to restore config: %s", exc)

        # --- Restore history ---
        history_file = tmpdir / "history.json"
        if history_file.is_file():
            try:
                count = import_history_json(str(history_file), history)
                result["history"] = count
                logger.info("Restored %d history entries from backup", count)
            except Exception as exc:
                logger.warning("Failed to restore history: %s", exc)

        # --- Restore favorites ---
        fav_file = tmpdir / "favorites.json"
        if fav_file.is_file():
            # Try SQLite import first
            imported = _import_favorites_from_json(fav_file)
            if imported > 0:
                result["favorites"] = imported
                logger.info("Restored %d favorites from backup", imported)
            else:
                # Fall back: copy to legacy JSON file
                import shutil
                target = _get_favorites_path()
                try:
                    shutil.copy2(str(fav_file), str(target))
                    result["favorites"] = 1
                    logger.info("Restored favorites (legacy JSON) from backup")
                except Exception as exc:
                    logger.warning("Failed to restore favorites: %s", exc)

    return result


def list_backups(backup_dir: str | None = None) -> list[dict]:
    """List available backups.

    Returns a list of dicts: {"filename": str, "size": int, "date": str}
    Sorted by date, newest first.
    """
    base = _get_backup_dir(backup_dir)
    backups: list[dict] = []
    if not base.is_dir():
        return backups

    for f in base.iterdir():
        if not f.is_file() or not zipfile.is_zipfile(str(f)):
            continue
        try:
            st = f.stat()
            dt = datetime.fromtimestamp(st.st_mtime)
            backups.append({
                "filename": f.name,
                # Absolute path so the frontend can pass it straight back to
                # restore_backup() without relying on the process CWD.
                "path": str(f),
                "size": st.st_size,
                "date": dt.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except OSError:
            continue

    backups.sort(key=lambda b: b["date"], reverse=True)
    return backups


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Value sets for enum-constrained string fields.  Field names and defaults
# mirror the Config dataclass in internal/config/config.py — keep them in
# sync when the Config schema changes.
_APPEARANCE_MODES = {"system", "light", "dark"}
_LANGUAGES = {"en", "zh-CN"}
_DEDUP_METHODS = {"sha256", "simple"}
_APP_FILTER_MODES = {"blacklist", "whitelist"}
_UI_BACKENDS = {"ctk", "webview"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Per-field validation rules applied during restore.  A backup's config.json
# is untrusted input, so every field is type- and range-checked before it is
# written onto the live Config.  A missing rule means the field is simply not
# applied (unknown / not part of the known schema).  Rule shapes:
#   ("int", lo, hi)      Python int (bool rejected); clamped to [lo, hi]
#   ("float",)           int or float (bool rejected); coerced to float
#   ("bool",)            only a Python bool
#   ("str",)             any string
#   ("enum", set)        string in the given set
#   ("enum", set, True)  string normalized to upper-case, then checked
#   ("strlist",)         list of strings, or None (the Config default for
#                        filter_enabled_categories meaning "all enabled")
_APPLY_SCHEMA: dict[str, tuple] = {
    "device_name": ("str",),
    "port": ("int", 1, 65535),
    "service_type": ("str",),
    "sync_enabled": ("bool",),
    "auto_start": ("bool",),
    "filter_enabled_categories": ("strlist",),
    "relay_url": ("str",),
    "history_max_entries": ("int", 1, 100000),
    "file_receive_dir": ("str",),
    "sync_debounce": ("float",),
    "clipboard_poll_interval": ("float",),
    "max_reconnect_attempts": ("int", 0, 1000),
    "transfer_timeout": ("float",),
    "log_level": ("enum", _LOG_LEVELS, True),
    "notifications_enabled": ("bool",),
    "encryption_enabled": ("bool",),
    "appearance_mode": ("enum", _APPEARANCE_MODES),
    "language": ("enum", _LANGUAGES),
    "paste_to_top": ("bool",),
    "low_memory_mode": ("bool",),
    "retry_capture_enabled": ("bool",),
    "dedup_method": ("enum", _DEDUP_METHODS),
    "app_filter_enabled": ("bool",),
    "app_filter_mode": ("enum", _APP_FILTER_MODES),
    "app_filter_list": ("strlist",),
    "source_tracking_enabled": ("bool",),
    "ui_backend": ("enum", _UI_BACKENDS),
    "ui_animation_enabled": ("bool",),
    "sound_enabled": ("bool",),
    "web_enabled": ("bool",),
    "web_port": ("int", 1, 65535),
    "web_history_limit": ("int", 1, 100000),
}

# Sentinel returned by _validate_config_value when a field must be skipped.
_SKIP = object()


def _validate_config_value(value: object, rule: tuple):
    """Validate/coerce one backup config value against a schema rule.

    Returns the value to apply, or ``_SKIP`` when the value is invalid and the
    field must be left untouched.  Never raises.
    """
    kind = rule[0]
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return _SKIP
        lo, hi = rule[1], rule[2]
        return max(lo, min(hi, value))
    if kind == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _SKIP
        return float(value)
    if kind == "bool":
        return value if isinstance(value, bool) else _SKIP
    if kind == "str":
        return value if isinstance(value, str) else _SKIP
    if kind == "enum":
        if not isinstance(value, str):
            return _SKIP
        if len(rule) > 2 and rule[2]:  # normalize (e.g. log_level -> upper)
            value = value.strip().upper()
        return value if value in rule[1] else _SKIP
    if kind == "strlist":
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            return _SKIP
        return value
    return _SKIP


def _apply_config(data: dict, cfg: Config) -> None:
    """Apply validated config fields from backup data.

    Every field in *data* is type- and range-checked against ``_APPLY_SCHEMA``
    before being written onto *cfg*.  Unknown fields and fields whose value has
    the wrong type are skipped with a warning; numeric fields that are out of
    range are clamped to the nearest boundary.  A malformed backup can
    therefore never put the Config into an unusable state or crash a transport
    server on the next start.
    """
    for key, value in data.items():
        if key not in _APPLY_SCHEMA or not hasattr(cfg, key):
            continue
        validated = _validate_config_value(value, _APPLY_SCHEMA[key])
        if validated is _SKIP:
            logger.warning(
                "Backup restore skipped invalid config field '%s' (%s)",
                key, type(value).__name__,
            )
            continue
        setattr(cfg, key, validated)
