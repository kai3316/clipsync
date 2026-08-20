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
from datetime import datetime, timezone
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
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
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

    with tempfile.TemporaryDirectory(prefix="clipsync_restore_") as tmp:
        tmpdir = Path(tmp)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
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


def _apply_config(data: dict, cfg: Config) -> None:
    """Apply a subset of config fields from backup data."""
    safe_fields = {
        "device_name", "port", "service_type", "sync_enabled",
        "auto_start", "filter_enabled_categories", "relay_url",
        "history_max_entries", "file_receive_dir", "sync_debounce",
        "clipboard_poll_interval", "max_reconnect_attempts",
        "transfer_timeout", "log_level", "notifications_enabled",
        "encryption_enabled", "appearance_mode", "language",
        "paste_to_top", "low_memory_mode", "retry_capture_enabled",
        "dedup_method", "app_filter_enabled", "app_filter_mode",
        "app_filter_list", "source_tracking_enabled", "ui_backend",
        "ui_animation_enabled", "sound_enabled", "web_enabled",
        "web_port", "web_history_limit",
    }
    for key in safe_fields:
        if key in data and hasattr(cfg, key):
            setattr(cfg, key, data[key])
