"""Favorites API handlers with SQLite persistent storage.

Favorites are stored in an SQLite database at
  {config_dir}/favorites.db

Auto-creates the database and table on first use.  Auto-migrates from
the legacy ``favorites.json`` file when the DB is empty and the JSON
exists.

All public API function signatures are unchanged from the JSON-based
version so existing callers (routes, batch_favorite) work without
modification.
"""

import json
import logging
import os
import sqlite3
import time
import uuid

from internal.config.config import _config_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_FAV_DB_PATH: str | None = None

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS favorites (
        id       TEXT PRIMARY KEY,
        title    TEXT NOT NULL DEFAULT '',
        content  TEXT NOT NULL DEFAULT '',
        "group"  TEXT NOT NULL DEFAULT '',
        position INTEGER NOT NULL DEFAULT 0,
        created  REAL NOT NULL,
        updated  REAL
    );
"""

_MIGRATIONS = [
    # Migration 1: add position column (added 2026-05)
    "ALTER TABLE favorites ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
]


def _get_db_path() -> str:
    """Return the path to the favorites SQLite database (cached)."""
    global _FAV_DB_PATH
    if _FAV_DB_PATH is None:
        _FAV_DB_PATH = os.path.join(_config_dir(), "favorites.db")
    return _FAV_DB_PATH


def _get_json_path() -> str:
    """Return the path to the legacy favorites JSON file."""
    return os.path.join(_config_dir(), "favorites.json")


def _get_conn() -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode and safe threading."""
    conn = sqlite3.connect(_get_db_path(), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_db() -> None:
    """Create the database directory and schema if they don't exist."""
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _run_migrations(conn)
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations that can't be expressed in CREATE TABLE IF NOT EXISTS."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(favorites)").fetchall()}
    for sql in _MIGRATIONS:
        try:
            if "ADD COLUMN" in sql:
                col = sql.split("ADD COLUMN")[1].strip().split()[0].strip('"')
                if col not in existing:
                    conn.execute(sql)
                    conn.commit()
                    logger.info("Applied favorites migration: %s", col)
        except Exception as exc:
            logger.warning("Skipping migration: %s", exc)


def _maybe_migrate() -> int:
    """Migrate from legacy favorites.json if the DB is empty and JSON exists.

    Returns the number of migrated entries.
    """
    json_path = _get_json_path()
    if not os.path.exists(json_path):
        return 0

    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        _run_migrations(conn)
        row_count = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        if row_count > 0:
            return 0  # DB already has data — skip migration

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list) or not data:
            return 0

        count = 0
        with conn:
            for item in data:
                fav_id = item.get("id", uuid.uuid4().hex[:12])
                conn.execute(
                    "INSERT OR IGNORE INTO favorites "
                    "(id, title, content, \"group\", position, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        fav_id,
                        item.get("title", ""),
                        item.get("content", ""),
                        item.get("group", ""),
                        item.get("position", 0),
                        item.get("created", time.time()),
                        item.get("updated"),
                    ),
                )
                count += 1

        if count > 0:
            logger.info(
                "Migrated %d favorites from %s to SQLite", count, json_path,
            )
        return count
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to migrate favorites from JSON: %s", exc)
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers (used by batch_favorite in history.py)
# ---------------------------------------------------------------------------


def _load_favorites() -> list:
    """Load all favorites from the database.

    Returns a list of dicts, newest first (by creation time).
    Maintained for backward compatibility — called by batch_favorite
    in ``internal/web/api/history.py``.
    """
    _ensure_db()
    _maybe_migrate()

    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, content, \"group\", position, created, updated "
            "FROM favorites ORDER BY position ASC, created DESC"
        ).fetchall()
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
        return favorites
    except Exception as exc:
        logger.error("Failed to load favorites from DB: %s", exc)
        return []
    finally:
        conn.close()


def _save_favorites(favorites: list) -> None:
    """Replace all favorites in the database with the given list.

    Maintained for backward compatibility — called by batch_favorite
    in ``internal/web/api/history.py``.
    """
    _ensure_db()

    conn = _get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM favorites")
            for entry in favorites:
                conn.execute(
                    "INSERT OR REPLACE INTO favorites "
                    "(id, title, content, \"group\", position, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.get("id", uuid.uuid4().hex[:12]),
                        entry.get("title", ""),
                        entry.get("content", ""),
                        entry.get("group", ""),
                        entry.get("position", 0),
                        entry.get("created", time.time()),
                        entry.get("updated"),
                    ),
                )
    except Exception as exc:
        logger.error("Failed to save favorites to DB: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API (unchanged signatures)
# ---------------------------------------------------------------------------


def get_favorites():
    """Load and return favorites from the database."""
    return {"favorites": _load_favorites()}, 200


def add_favorite(body):
    """Add a new favorite item."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    title = data.get("title", "").strip()
    content = data.get("content", "")
    if not title and not content:
        return {"ok": False, "error": "title or content is required"}, 400

    _ensure_db()
    _maybe_migrate()

    entry = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "content": content,
        "group": data.get("group", "").strip(),
        "created": time.time(),
    }

    conn = _get_conn()
    try:
        # Append to the end (max position + 1) so insertion order is preserved
        # even after drag-reorder assigns explicit non-zero positions.
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM favorites"
        ).fetchone()
        position = (row[0] + 1) if row else 0
        entry["position"] = position
        conn.execute(
            "INSERT INTO favorites (id, title, content, \"group\", position, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry["id"], entry["title"], entry["content"],
             entry["group"], position, entry["created"]),
        )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to add favorite: %s", exc)
        return {"ok": False, "error": "database error"}, 500
    finally:
        conn.close()

    logger.info("Favorite added: %s", title[:60] if title else "(no title)")
    return {"ok": True, "favorite": entry}, 200


def delete_favorite(body):
    """Delete a favorite item by id."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    fav_id = data.get("id", "").strip()
    if not fav_id:
        return {"ok": False, "error": "id is required"}, 400

    _ensure_db()

    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM favorites WHERE id = ?", (fav_id,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"ok": False, "error": "not found"}, 404
    except Exception as exc:
        logger.error("Failed to delete favorite: %s", exc)
        return {"ok": False, "error": "database error"}, 500
    finally:
        conn.close()

    logger.info("Favorite deleted: %s", fav_id)
    return {"ok": True}, 200


def update_favorite(body):
    """Update a favorite item (title, group, etc.)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    fav_id = data.get("id", "").strip()
    if not fav_id:
        return {"ok": False, "error": "id is required"}, 400

    _ensure_db()

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, title, content, \"group\", position, created, updated "
            "FROM favorites WHERE id = ?", (fav_id,)
        ).fetchone()

        if row is None:
            return {"ok": False, "error": "not found"}, 404

        # Build updated entry
        entry = {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "group": row[3],
            "position": row[4],
            "created": row[5],
            "updated": row[6],
        }

        if "title" in data:
            entry["title"] = data["title"].strip()
        if "content" in data:
            entry["content"] = data["content"]
        if "group" in data:
            entry["group"] = data["group"].strip()
        if "position" in data:
            try:
                entry["position"] = int(data["position"])
            except (ValueError, TypeError):
                return {"ok": False, "error": "position must be an integer"}, 400
        entry["updated"] = time.time()

        conn.execute(
            "UPDATE favorites SET title = ?, content = ?, \"group\" = ?, "
            "position = ?, updated = ? WHERE id = ?",
            (entry["title"], entry["content"], entry["group"],
             entry["position"], entry["updated"], fav_id),
        )
        conn.commit()

        logger.info("Favorite updated: %s", fav_id)
        return {"ok": True, "favorite": entry}, 200
    except Exception as exc:
        logger.error("Failed to update favorite: %s", exc)
        return {"ok": False, "error": "database error"}, 500
    finally:
        conn.close()
