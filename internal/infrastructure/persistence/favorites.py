"""Path-injected access to the existing favorites store."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    "group" TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL
)
"""

# One statement per entry, because `sqlite3` executes one at a time.
SCHEMAS = (
    SCHEMA,
    """
CREATE TABLE IF NOT EXISTS favorite_groups (
    name TEXT PRIMARY KEY,
    created REAL NOT NULL
)
""",
)
FIELDS = 'id, title, content, "group", position, created, updated'


class FavoritesRepository:
    """Version 1 records completed legacy adoption; the JSON remains untouched."""

    def __init__(self, db_path, json_path=None):
        self.db_path = Path(db_path)
        self.json_path = (
            Path(json_path) if json_path is not None else self.db_path.with_suffix(".json")
        )
        self._initialized = False

    def connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _check_version(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("Unsupported favorites database version")
        return version

    @staticmethod
    def run_migrations(conn):
        FavoritesRepository._check_version(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(favorites)")}
        if "position" not in columns:
            conn.execute("ALTER TABLE favorites ADD COLUMN position INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._check_version(conn)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            for statement in SCHEMAS:
                conn.execute(statement)
            self.run_migrations(conn)

    def maybe_migrate(self):
        """Adopt legacy JSON once, committing its completion marker with its rows.

        Existing SQLite rows are authoritative. An absent or empty JSON source
        also completes adoption, so later JSON files are not implicitly imported.
        """
        self.ensure_db()
        with self.transaction() as conn:
            if self._check_version(conn) == 1:
                return 0
            data = []
            if (
                not conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
                and self.json_path.exists()
            ):
                with self.json_path.open(encoding="utf-8") as source:
                    data = json.load(source)
                if not isinstance(data, list):
                    raise ValueError("Invalid legacy favorites")
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("Invalid legacy favorite")
                conn.execute(
                    f"INSERT INTO favorites ({FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.get("id", uuid.uuid4().hex[:12]),
                        item.get("title", ""),
                        item.get("content", ""),
                        item.get("group", ""),
                        item.get("position", 0),
                        item.get("created", time.time()),
                        item.get("updated"),
                    ),
                )
            conn.execute("PRAGMA user_version=1")
            return len(data)

    def initialize(self):
        if not self._initialized:
            self.maybe_migrate()
            self._initialized = True

    @staticmethod
    def entry(row):
        if row is None:
            return None
        return dict(
            zip(
                ("id", "title", "content", "group", "position", "created", "updated"),
                row,
                strict=True,
            )
        )

    def get_all(self):
        self.initialize()
        with self.transaction() as conn:
            return [
                self.entry(row)
                for row in conn.execute(
                    f"SELECT {FIELDS} FROM favorites ORDER BY position ASC, created DESC"
                )
            ]

    def get(self, favorite_id):
        self.initialize()
        with self.transaction() as conn:
            return self.entry(
                conn.execute(
                    f"SELECT {FIELDS} FROM favorites WHERE id = ?", (favorite_id,)
                ).fetchone()
            )

    def add(self, title, content, group, validate=None):
        self.initialize()
        with self.transaction() as conn:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM favorites"
            ).fetchone()[0]
            entry = dict(
                id=uuid.uuid4().hex[:12],
                title=title.strip(),
                content=content,
                group=group.strip(),
                position=position,
                created=time.time(),
                updated=None,
            )
            if validate:
                validate(entry)
            conn.execute(
                f"INSERT INTO favorites ({FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(entry.values()),
            )
            return entry

    def update(self, favorite_id, changes, validate=None, validate_existing=None):
        self.initialize()
        with self.transaction() as conn:
            entry = self.entry(
                conn.execute(
                    f"SELECT {FIELDS} FROM favorites WHERE id = ?", (favorite_id,)
                ).fetchone()
            )
            if entry is None:
                return None
            if validate_existing:
                validate_existing(entry)
            for name in ("title", "content", "group", "position"):
                if name in changes:
                    entry[name] = (
                        changes[name].strip() if name in ("title", "group") else changes[name]
                    )
            if validate:
                validate(entry)
            entry["updated"] = time.time()
            conn.execute(
                'UPDATE favorites SET title=?, content=?, "group"=?, '
                "position=?, updated=? WHERE id=?",
                (
                    entry["title"],
                    entry["content"],
                    entry["group"],
                    entry["position"],
                    entry["updated"],
                    favorite_id,
                ),
            )
            return entry

    def delete(self, favorite_id):
        self.initialize()
        with self.transaction() as conn:
            return conn.execute("DELETE FROM favorites WHERE id=?", (favorite_id,)).rowcount > 0

    # ── Group registry ────────────────────────────────────────────────
    #
    # A group is a name, not a row in `favorites`: a group with nothing in it
    # is still a group the user made, and it has to survive until they delete
    # it.  The legacy panel kept this list in the browser's localStorage,
    # which is why it could show groups the data folder no longer had — the
    # two lived in different places and only one of them was reset.  Here the
    # registry sits beside the favourites it names, so a wipe clears both.

    def group_names(self):
        self.initialize()
        with self.transaction() as conn:
            return [
                row[0]
                for row in conn.execute("SELECT name FROM favorite_groups ORDER BY name")
            ]

    def register_group(self, name):
        """Remember a group name so an empty one stays visible."""
        name = name.strip()
        if not name:
            return False
        self.initialize()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO favorite_groups (name, created) VALUES (?, ?)",
                (name, time.time()),
            )
            return cursor.rowcount > 0

    def forget_group(self, name):
        self.initialize()
        with self.transaction() as conn:
            return conn.execute(
                "DELETE FROM favorite_groups WHERE name=?", (name,)
            ).rowcount > 0

    def rename_group(self, name, new_name):
        """Move the registry entry, leaving the favourites in it to the caller.

        Renaming the group and moving its members are one user action, so they
        belong in one request — but not one transaction: the members are
        ordinary favourite updates that go through `update()`, whose
        per-entry validation must still run.
        """
        self.initialize()
        with self.transaction() as conn:
            conn.execute("DELETE FROM favorite_groups WHERE name=?", (name,))
            conn.execute(
                "INSERT OR IGNORE INTO favorite_groups (name, created) VALUES (?, ?)",
                (new_name.strip(), time.time()),
            )
