"""SQLite-backed clipboard history storage.

Replaces the JSON-file persistence in ClipboardHistory with a local
SQLite database while keeping the identical public API and internal
attribute signatures (``_entries``, ``_lock``, ``_save()``, etc.).

Database location: {config_dir}/clipboard_history.db
Auto-creates tables on first use.  Auto-migrates from the legacy
``clipboard_history.json`` when the DB is empty and the JSON exists.
"""

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from internal.clipboard.format import ClipboardContent, ContentType
from internal.config.config import _config_dir

if TYPE_CHECKING:
    from internal.security.encryption import EncryptionManager

logger = logging.getLogger(__name__)

_CONTENT_TYPE_LABELS: dict[ContentType, str] = {
    ContentType.TEXT: "TEXT",
    ContentType.HTML: "HTML",
    ContentType.RTF: "RTF",
    ContentType.IMAGE_PNG: "IMAGE",
    ContentType.IMAGE_EMF: "IMAGE_EMF",
    ContentType.FILE: "FILE",
    ContentType.URL: "URL",
}


def _safe_decode(data: bytes) -> str:
    """Decode bytes to string, trying common encodings."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("gbk", "gb2312", "gb18030", "big5", "shift-jis", "euc-kr"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    """Remove HTML tags, style/script blocks, comments, and unescape entities."""
    import html as _html
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    plain = re.sub(r"<[^>]*>", "", text)
    plain = _html.unescape(plain)
    plain = re.sub(r"\s+", " ", plain)
    return plain.strip()


def _make_dedup_key(content: ClipboardContent) -> str:
    """Build a stable dedup key from the 'primary' content."""
    if ContentType.TEXT in content.types:
        text = content.types[ContentType.TEXT].decode("utf-8", errors="replace")
        return "text:" + text[:500]
    if ContentType.IMAGE_PNG in content.types:
        return "png:" + hashlib.sha256(
            content.types[ContentType.IMAGE_PNG]
        ).hexdigest()
    if ContentType.IMAGE_EMF in content.types:
        return "emf:" + hashlib.sha256(
            content.types[ContentType.IMAGE_EMF]
        ).hexdigest()
    if ContentType.HTML in content.types:
        return "html:" + hashlib.sha256(
            content.types[ContentType.HTML]
        ).hexdigest()
    if ContentType.RTF in content.types:
        return "rtf:" + hashlib.sha256(
            content.types[ContentType.RTF]
        ).hexdigest()
    return "other:" + str(time.time())


def _build_preview(types: dict[ContentType, bytes]) -> str:
    """Build a human-readable preview from clipboard content."""
    if ContentType.TEXT in types:
        text = _safe_decode(types[ContentType.TEXT])
        return text[:200]
    if ContentType.HTML in types:
        html = _safe_decode(types[ContentType.HTML])
        plain = _strip_html(html)
        return plain[:200] if plain else "[HTML]"
    if ContentType.IMAGE_EMF in types:
        return "[Vector Image]"
    if ContentType.IMAGE_PNG in types:
        return "[Image]"
    if ContentType.RTF in types:
        return "[Rich Text]"
    return ""


def _map_type_to_label(content_type: ContentType) -> str:
    return _CONTENT_TYPE_LABELS.get(content_type, "TEXT")


def _map_label_to_type(label: str) -> ContentType:
    for ct, lbl in _CONTENT_TYPE_LABELS.items():
        if lbl == label:
            return ct
    return ContentType.TEXT


class ClipboardHistoryDB:
    """Thread-safe clipboard history persisted to a local SQLite database.

    Mirrors the public API of ``ClipboardHistory`` exactly so all
    existing callers (routes, API handlers, sync manager, export,
    backup) continue to work without changes.

    Maintains an in-memory ``_entries`` list for fast access and full
    backward compatibility with code that accesses ``_entries``,
    ``_lock``, ``_save()``, ``_next_id``, and ``MAX_ENTRIES`` directly.
    """

    # Minimum interval (seconds) between entries with identical primary content.
    DEDUP_WINDOW = 2.0

    # Fields encrypted at rest — excludes timestamp and content_type
    _ENCRYPTED_FIELDS = ("types", "text_preview", "source_device")

    # ── schema ──────────────────────────────────────────────────────

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS history (
            entry_id    INTEGER PRIMARY KEY,
            timestamp   REAL    NOT NULL,
            content_type TEXT   NOT NULL DEFAULT '',
            text_preview TEXT   NOT NULL DEFAULT '',
            types       TEXT    NOT NULL DEFAULT '{}',
            source_device TEXT  NOT NULL DEFAULT '',
            source_app   TEXT   NOT NULL DEFAULT '',
            source_title TEXT   NOT NULL DEFAULT '',
            pinned       INTEGER NOT NULL DEFAULT 0,
            paste_count  INTEGER NOT NULL DEFAULT 0
        );
    """

    def __init__(self, storage_path: str | None = None, max_entries: int = 50,
                 enc_mgr: "EncryptionManager | None" = None):
        if storage_path:
            self._db_path = Path(storage_path)
        else:
            self._db_path = _config_dir() / "clipboard_history.db"

        # Path to the legacy JSON file (for auto-migration)
        self._json_path = self._db_path.parent / "clipboard_history.json"

        self.MAX_ENTRIES = max_entries
        self._entries: list[dict] = []
        self._lock = threading.RLock()
        self._enc_mgr = enc_mgr
        self._last_dedup_key: str = ""
        self._last_dedup_time: float = 0.0
        self._next_id: int = 0

        self._ensure_db_dir()
        self._load()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _ensure_db_dir(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        """Apply performance and safety pragmas to a connection."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA busy_timeout=5000")

    def _get_conn(self) -> sqlite3.Connection:
        """Return a new SQLite connection for the current thread.

        ``check_same_thread=False`` is required because a connection
        created in one thread may be used by another (the HTTP server
        creates connections in worker threads).
        """
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._apply_pragmas(conn)
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self._SCHEMA)
        conn.commit()

    # ------------------------------------------------------------------
    # Public API (identical to ClipboardHistory)
    # ------------------------------------------------------------------

    def add(self, content: ClipboardContent,
            source_app: dict | None = None) -> None:
        """Add a clipboard entry. Silently ignores empty content.

        Deduplicates: entries with the same primary content (text body or
        image bytes) within a short window are coalesced into one record.
        """
        if content.is_empty():
            return
        best = content.best_format()
        if best is None:
            return
        best_type, _best_data = best

        # -- dedup ---------------------------------------------------
        dedup_key = _make_dedup_key(content)
        now = content.timestamp or time.time()

        with self._lock:
            if dedup_key == self._last_dedup_key:
                if now - self._last_dedup_time < self.DEDUP_WINDOW:
                    return
            self._last_dedup_key = dedup_key
            self._last_dedup_time = now

            preview = _build_preview(content.types)
            entry: dict = {
                "timestamp": now,
                "content_type": _map_type_to_label(best_type),
                "text_preview": preview,
                "types": {
                    _map_type_to_label(t): base64.b64encode(data).decode("ascii")
                    for t, data in content.types.items()
                },
                "source_device": content.source_device,
                "source_app": (source_app.get("name") if source_app else ""),
                "source_title": (source_app.get("title") if source_app else ""),
                "pinned": False,
                "entry_id": self._next_id,
                "paste_count": 0,
            }
            self._next_id += 1

            self._entries.insert(0, entry)
            if len(self._entries) > self.MAX_ENTRIES:
                # Remove oldest unpinned entries beyond the limit.
                # Keep all pinned entries; trim only unpinned ones.
                pinned = [e for e in self._entries if e.get("pinned")]
                unpinned = [e for e in self._entries if not e.get("pinned")]
                allowed_unpinned = max(0, self.MAX_ENTRIES - len(pinned))
                self._entries = pinned + unpinned[:allowed_unpinned]
            self._save()

    def get_all(self) -> list[dict]:
        """Return all entries, pinned first, then newest first within each group."""
        with self._lock:
            pinned = [e for e in self._entries if e.get("pinned")]
            unpinned = [e for e in self._entries if not e.get("pinned")]
            return pinned + unpinned

    def search(self, query: str) -> list[dict]:
        """Case-insensitive search in text previews. Returns matching entries, newest first."""
        q = query.lower()
        with self._lock:
            return [e for e in self._entries if q in e.get("text_preview", "").lower()]

    def get(self, index: int) -> dict | None:
        """Get a single entry by display index (matching get_all() order). Returns None if out of bounds."""
        with self._lock:
            internal = self._display_to_internal(index)
            if internal is not None:
                return dict(self._entries[internal])
            return None

    def _display_to_internal(self, display_index: int) -> int | None:
        """Convert a get_all() display index to internal _entries index."""
        all_entries = self.get_all()
        if 0 <= display_index < len(all_entries):
            target = all_entries[display_index]
            eid = target.get("entry_id")
            for i, e in enumerate(self._entries):
                if e.get("entry_id") == eid:
                    return i
        return None

    def delete(self, index: int) -> bool:
        """Delete an entry by display index (matching get_all() order). Returns True if deleted."""
        with self._lock:
            internal = self._display_to_internal(index)
            if internal is not None:
                self._entries.pop(internal)
                self._save()
                return True
            return False

    def pin(self, index: int) -> bool:
        """Pin an entry by display index (matching get_all() order). Pinned items stay at the top."""
        with self._lock:
            internal = self._display_to_internal(index)
            if internal is not None:
                self._entries[internal]["pinned"] = True
                self._save()
                return True
            return False

    def unpin(self, index: int) -> bool:
        """Unpin an entry by display index (matching get_all() order)."""
        with self._lock:
            internal = self._display_to_internal(index)
            if internal is not None:
                self._entries[internal]["pinned"] = False
                self._save()
                return True
            return False

    def clear(self) -> None:
        """Delete all history entries and persist the empty state."""
        with self._lock:
            self._entries.clear()
            self._save()

    def find_by_id(self, entry_id: str) -> tuple[int, dict] | tuple[None, None]:
        """Find an entry by its ``entry_id``. Returns (index, entry) or (None, None)."""
        with self._lock:
            for i, entry in enumerate(self._entries):
                if entry.get("entry_id") == entry_id:
                    return i, dict(entry)
            return None, None

    def increment_paste(self, entry_id: str) -> int | None:
        """Increment the paste count for an entry. Returns new count or None if not found."""
        with self._lock:
            for entry in self._entries:
                if entry.get("entry_id") == entry_id:
                    entry["paste_count"] = entry.get("paste_count", 0) + 1
                    self._save()
                    return entry["paste_count"]
            return None

    def batch_set_pinned(self, entry_ids: list, pinned: bool) -> int:
        """Set pinned state on entries matching the given IDs. Returns count of entries updated."""
        id_set = set(entry_ids)
        count = 0
        with self._lock:
            for entry in self._entries:
                if entry.get("entry_id") in id_set:
                    entry["pinned"] = pinned
                    count += 1
            if count:
                self._save()
        return count

    def batch_delete(self, entry_ids: list) -> int:
        """Delete entries matching the given IDs. Returns count of entries deleted."""
        id_set = set(entry_ids)
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("entry_id") not in id_set]
            removed = before - len(self._entries)
            if removed:
                self._save()
            return removed

    # ------------------------------------------------------------------
    # Persistence (SQLite)
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load entries from the SQLite database.

        Automatically migrates from the legacy JSON file if the database
        is empty and the JSON file exists.
        """
        conn = None
        try:
            conn = self._get_conn()
            self._init_schema(conn)

            row_count = conn.execute(
                "SELECT COUNT(*) FROM history"
            ).fetchone()[0]

            if row_count == 0:
                # Check for legacy JSON and auto-migrate
                migrated = self._migrate_from_json_file(conn)
                if not migrated:
                    return  # no data — DB is empty, JSON didn't exist

            # Load all entries ordered by entry_id DESC (newest first)
            rows = conn.execute(
                "SELECT entry_id, timestamp, content_type, text_preview, "
                "types, source_device, source_app, source_title, "
                "pinned, paste_count "
                "FROM history ORDER BY entry_id DESC"
            ).fetchall()

            self._entries = []
            for row in rows:
                entry = {
                    "entry_id": row[0],
                    "timestamp": row[1],
                    "content_type": row[2],
                    "text_preview": row[3],
                    "types": json.loads(row[4]) if row[4] else {},
                    "source_device": row[5],
                    "source_app": row[6],
                    "source_title": row[7],
                    "pinned": bool(row[8]),
                    "paste_count": row[9] if row[9] else 0,
                }
                self._entries.append(entry)

            if self._entries:
                self._next_id = max(
                    e.get("entry_id", 0) for e in self._entries
                ) + 1

            if self._enc_mgr:
                for entry in self._entries:
                    self._decrypt_entry(entry)
                logger.debug(
                    "History load: decrypted %d entries from DB",
                    len(self._entries),
                )

        except Exception as exc:
            logger.warning("Failed to load history from DB: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _save(self) -> None:
        """Persist all in-memory entries to the SQLite database.

        Uses a DELETE + INSERT approach within a single transaction for
        atomicity.  With MAX_ENTRIES=50 this is fast and simple.
        """
        self._ensure_db_dir()
        entries_to_save = self._entries
        if self._enc_mgr:
            entries_to_save = [self._encrypt_entry(e) for e in self._entries]
            logger.debug(
                "History save: encrypted %d entries for at-rest storage",
                len(entries_to_save),
            )

        conn = None
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("DELETE FROM history")
                conn.executemany(
                    "INSERT INTO history "
                    "(entry_id, timestamp, content_type, text_preview, types, "
                    "source_device, source_app, source_title, pinned, paste_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            e.get("entry_id", 0),
                            e.get("timestamp", 0.0),
                            e.get("content_type", ""),
                            e.get("text_preview", ""),
                            json.dumps(e.get("types", {}), ensure_ascii=False),
                            e.get("source_device", ""),
                            e.get("source_app", ""),
                            e.get("source_title", ""),
                            1 if e.get("pinned") else 0,
                            e.get("paste_count", 0),
                        )
                        for e in entries_to_save
                    ),
                )
        except Exception as exc:
            logger.error("Failed to save history to DB: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def migrate_from_json(self) -> int:
        """Public migration entry point.

        Import entries from the legacy ``clipboard_history.json`` file
        into the SQLite database.  Returns the number of entries migrated.
        Safe to call multiple times — skips if no JSON file exists or
        the database already has entries.
        """
        conn = self._get_conn()
        try:
            self._init_schema(conn)
            row_count = conn.execute(
                "SELECT COUNT(*) FROM history"
            ).fetchone()[0]
            if row_count > 0:
                logger.debug("DB already has %d entries, skipping migration", row_count)
                return 0
            return self._migrate_from_json_file(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _migrate_from_json_file(self, conn: sqlite3.Connection) -> int:
        """Read the legacy JSON file and insert its entries into the DB.

        Returns the number of entries migrated.  Decrypts entries if an
        encryption manager is configured.
        """
        if not self._json_path.exists():
            return 0

        try:
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read legacy history JSON: %s", exc)
            return 0

        if not isinstance(data, list):
            return 0

        entries = data[: self.MAX_ENTRIES]
        if not entries:
            return 0

        # Decrypt if encryption is active
        if self._enc_mgr:
            for entry in entries:
                self._decrypt_entry(entry)
            logger.debug(
                "Migration: decrypted %d entries from legacy JSON", len(entries)
            )
            # Re-encrypt so migrated data is stored encrypted at rest,
            # matching what _save() writes for newly added entries.
            entries = [self._encrypt_entry(e) for e in entries]

        # Ensure paste_count exists on migrated entries
        for entry in entries:
            if "paste_count" not in entry:
                entry["paste_count"] = 0

        with conn:
            conn.executemany(
                "INSERT INTO history "
                "(entry_id, timestamp, content_type, text_preview, types, "
                "source_device, source_app, source_title, pinned, paste_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        e.get("entry_id", 0),
                        e.get("timestamp", 0.0),
                        e.get("content_type", ""),
                        e.get("text_preview", ""),
                        json.dumps(e.get("types", {}), ensure_ascii=False),
                        e.get("source_device", ""),
                        e.get("source_app", ""),
                        e.get("source_title", ""),
                        1 if e.get("pinned") else 0,
                        e.get("paste_count", 0),
                    )
                    for e in entries
                ),
            )

        logger.info(
            "Migrated %d entries from %s to SQLite",
            len(entries), self._json_path.name,
        )
        return len(entries)

    # ------------------------------------------------------------------
    # Encryption helpers (identical to ClipboardHistory)
    # ------------------------------------------------------------------

    def _encrypt_entry(self, entry: dict) -> dict:
        """Return a copy of entry with sensitive fields encrypted for at-rest storage."""
        enc = self._enc_mgr
        if not enc:
            return entry
        e = dict(entry)
        for field in self._ENCRYPTED_FIELDS:
            if field in e:
                if field == "types":
                    e["types"] = {
                        k: enc.encrypt_storage(v) for k, v in e["types"].items()
                    }
                else:
                    val = e[field]
                    if isinstance(val, str):
                        e[field] = enc.encrypt_storage(val)
        return e

    def _decrypt_entry(self, entry: dict) -> None:
        """Decrypt sensitive fields in-place. Legacy plaintext is passed through."""
        enc = self._enc_mgr
        if not enc:
            return
        for field in self._ENCRYPTED_FIELDS:
            if field not in entry:
                continue
            if field == "types":
                for k, v in list(entry["types"].items()):
                    pt = enc.decrypt_storage(v)
                    if pt is not None:
                        entry["types"][k] = pt
            else:
                val = entry[field]
                if isinstance(val, str):
                    pt = enc.decrypt_storage(val)
                    if pt is not None:
                        entry[field] = pt
