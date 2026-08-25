"""SQLite-backed clipboard history storage.

Replaces the JSON-file persistence in ClipboardHistory with a local
SQLite database while keeping the identical public API and internal
attribute signatures (``_entries``, ``_lock``, ``_save()``, etc.).

Database location: {config_dir}/clipboard_history.db
Auto-creates tables on first use.  Auto-migrates from the legacy
``clipboard_history.json`` when the DB is empty and the JSON exists.
"""

import base64
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from internal.clipboard.dedup import (
    CONTENT_TYPE_LABELS,
    adds_new_flavors,
    labels_to_types,
    make_dedup_key as _make_dedup_key,
    merge_types,
)
from internal.clipboard.format import ClipboardContent, ContentType, strip_html
from internal.config.config import _config_dir

if TYPE_CHECKING:
    from internal.security.encryption import EncryptionManager

logger = logging.getLogger(__name__)

def _safe_decode(data: bytes) -> str:
    """Decode bytes to string, trying common encodings.

    A UTF-16 BOM is checked first: very old entries (or a peer platform that
    didn't normalize clipboard text to UTF-8) may store wide text raw.  Without
    this the bytes fall through to the CJK single-byte attempts and render as
    mojibake (the "history became garbled after update" report).
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
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




# Dedup hash algorithm now lives in internal.clipboard.dedup.DEDUP_ALGO
# (single source; make_dedup_key reads it there).

# Max age (days) for unpinned history entries; 0 disables age-based cleanup.
# Wired from cfg.history_max_age_days at startup by the application (same
# wiring pattern as DEDUP_ALGO above), giving history a dual limit: by
# entry count (MAX_ENTRIES) and by age.
MAX_AGE_DAYS = 0.0


def set_max_age_days(days) -> None:
    """Set the age-based cleanup limit in days.  0 or less disables it."""
    global MAX_AGE_DAYS
    try:
        MAX_AGE_DAYS = max(0.0, float(days))
    except (TypeError, ValueError):
        MAX_AGE_DAYS = 0.0


def _build_preview(types: dict[ContentType, bytes]) -> str:
    """Build a human-readable preview from clipboard content."""
    if ContentType.TEXT in types:
        text = _safe_decode(types[ContentType.TEXT])
        return text[:200]
    if ContentType.HTML in types:
        html = _safe_decode(types[ContentType.HTML])
        plain = strip_html(html)
        return plain[:200] if plain else "[HTML]"
    if ContentType.IMAGE_EMF in types:
        return "[Vector Image]"
    if ContentType.IMAGE_PNG in types:
        return "[Image]"
    if ContentType.RTF in types:
        return "[Rich Text]"
    return ""


def _map_type_to_label(content_type: ContentType) -> str:
    return CONTENT_TYPE_LABELS.get(content_type, "TEXT")


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

    # Same-text captures within this window merge into the newest entry
    # instead of stacking a second (possibly flavor-poorer) record — see
    # ClipboardHistory.FLAVOR_MERGE_WINDOW for the rationale.  Mirrors the
    # sync manager's DEDUP_RING_TTL (90 s).
    FLAVOR_MERGE_WINDOW = 90.0

    # Fields encrypted at rest — excludes timestamp and content_type
    _ENCRYPTED_FIELDS = ("types", "text_preview", "source_device", "source_title", "source_app")

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
            paste_count  INTEGER NOT NULL DEFAULT 0,
            status      TEXT    NOT NULL DEFAULT '',
            image_fmt   TEXT    NOT NULL DEFAULT ''
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
        self._conn: sqlite3.Connection | None = None

        self._ensure_db_dir()
        # Open one long-lived connection (applies PRAGMAs once, secures
        # file permissions) and make sure the schema exists.  A corrupt DB
        # file ("file is not a database", truncated by a full disk, …) must
        # not silently downgrade the app to memory-only history forever —
        # quarantine the broken file and start fresh instead.
        try:
            self._get_conn()
            if self._conn is not None:
                self._init_schema(self._conn)
        except Exception as exc:
            logger.warning("Failed to initialize history DB: %s", exc)
            self._quarantine_corrupt_db()
        self._load()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _ensure_db_dir(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _quarantine_corrupt_db(self) -> None:
        """Move an unreadable DB file aside and retry opening it once.

        Without this, a corrupt clipboard_history.db leaves ``_conn`` as
        None forever: every write then fails (logged per copy) and the whole
        history silently evaporates on the next restart.  The broken file is
        preserved as ``<name>.corrupt-<timestamp>`` for inspection.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("-wal", "-shm", ""):
            src = Path(str(self._db_path) + suffix)
            if src.exists():
                try:
                    src.rename(Path(str(self._db_path) + f".corrupt-{stamp}{suffix}"))
                except OSError:
                    logger.warning("Could not quarantine corrupt DB file %s", src)
                    return
        try:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._apply_pragmas(conn)
            self._init_schema(conn)
            self._conn = conn
            logger.warning(
                "History DB was corrupt and has been quarantined; "
                "starting a fresh database"
            )
        except Exception as exc:
            logger.error("Fresh history DB also failed to initialize: %s", exc)

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        """Apply performance and safety pragmas to a connection."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA busy_timeout=5000")

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared long-lived SQLite connection, creating it on first use.

        ``check_same_thread=False`` is required because a connection
        created in one thread may be used by another (the HTTP server
        creates connections in worker threads).  WAL + ``busy_timeout``
        make concurrent access safe; all DB writes are serialized under
        ``self._lock``.
        """
        if self._conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._apply_pragmas(conn)
            self._conn = conn
        self._secure_db_files()
        return self._conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self._SCHEMA)
        self._migrate_schema(conn)
        conn.commit()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial schema (idempotent).

        ``CREATE TABLE IF NOT EXISTS`` never touches an existing table, so a
        database created before a column was added keeps working only if we
        ``ALTER TABLE`` it here.  Wrapped in a column-existence check so it is
        safe to run on every startup and on already-migrated databases.
        """
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(history)").fetchall()}
            if "status" not in cols:
                conn.execute(
                    "ALTER TABLE history ADD COLUMN status TEXT NOT NULL DEFAULT ''"
                )
            if "image_fmt" not in cols:
                conn.execute(
                    "ALTER TABLE history ADD COLUMN image_fmt TEXT NOT NULL DEFAULT ''"
                )
        except Exception as exc:
            logger.warning("Failed to migrate history schema: %s", exc)

    def _secure_db_files(self) -> None:
        """Restrict DB/WAL/SHM permissions to the owner (0600)."""
        if os.name != "posix":
            return
        for path in (
            self._db_path,
            Path(str(self._db_path) + "-wal"),
            Path(str(self._db_path) + "-shm"),
        ):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _parse_types_json(raw) -> dict:
        """Parse one row's ``types`` JSON column defensively.

        A single corrupt row (truncated write, bit rot) must degrade to an
        empty format map instead of raising out of the loop and aborting
        the whole history load — that left the app with an empty in-memory
        list while the DB kept its rows, so every later insert collided
        with surviving primary keys and no capture persisted again.
        """
        try:
            parsed = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _entry_row(self, entry: dict) -> tuple:
        """Serialize (and encrypt, if configured) one entry into a DB row."""
        e = self._encrypt_entry(entry) if self._enc_mgr else entry
        return (
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
            e.get("status", ""),
            e.get("image_fmt", ""),
        )

    def _insert_row(self, entry: dict) -> None:
        """Insert a single history row (incremental write)."""
        try:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT INTO history "
                    "(entry_id, timestamp, content_type, text_preview, types, "
                    "source_device, source_app, source_title, pinned, paste_count, status, image_fmt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._entry_row(entry),
                )
            self._secure_db_files()
        except Exception as exc:
            logger.error("Failed to insert history row: %s", exc)

    def _update_row(self, entry_id: int, **fields) -> None:
        """Update a single history row's columns (incremental write)."""
        if not fields:
            return
        set_clause = ", ".join(f"{col} = ?" for col in fields)
        values = list(fields.values())
        try:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    f"UPDATE history SET {set_clause} WHERE entry_id = ?",
                    (*values, entry_id),
                )
            self._secure_db_files()
        except Exception as exc:
            logger.error("Failed to update history row %s: %s", entry_id, exc)

    def _delete_rows(self, entry_ids) -> None:
        """Delete rows matching the given entry IDs (incremental write)."""
        ids = list(entry_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        try:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    f"DELETE FROM history WHERE entry_id IN ({placeholders})",
                    ids,
                )
            self._secure_db_files()
        except Exception as exc:
            logger.error("Failed to delete history rows: %s", exc)

    def _delete_all_rows(self) -> None:
        """Delete every row in the history table (incremental clear)."""
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("DELETE FROM history")
            self._secure_db_files()
        except Exception as exc:
            logger.error("Failed to clear history table: %s", exc)

    def _vacuum_db(self) -> None:
        """Reclaim disk space after a mass deletion (Clear / factory reset).

        SQLite never shrinks the file on its own, and image payloads are
        stored inline as base64 TEXT — without this, clearing a large
        image-heavy history left clipboard_history.db at its old size
        forever.  VACUUM cannot run inside a transaction, so commit first;
        checkpointing the WAL afterwards makes the space visible on disk.
        """
        try:
            conn = self._get_conn()
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            logger.debug("Failed to vacuum history DB: %s", exc)

    def _prune_by_age(self) -> None:
        """Delete unpinned rows older than the configured max age.

        Called on startup and after each capture; a no-op unless
        set_max_age_days() wired a positive limit.  Pinned entries are
        never age-pruned.
        """
        if MAX_AGE_DAYS <= 0:
            return
        cutoff = time.time() - MAX_AGE_DAYS * 86400.0
        try:
            conn = self._get_conn()
            old_ids = [
                r[0] for r in conn.execute(
                    "SELECT entry_id FROM history "
                    "WHERE pinned = 0 AND timestamp < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if not old_ids:
                return
            self._delete_rows(old_ids)
            with self._lock:
                id_keys = {str(i) for i in old_ids}
                self._entries = [
                    e for e in self._entries
                    if str(e.get("entry_id")) not in id_keys
                ]
            logger.debug("Age-pruned %d history row(s)", len(old_ids))
        except Exception as exc:
            logger.debug("Age-based history prune failed: %s", exc)

    def _trim_db(self) -> None:
        """Delete oldest unpinned rows beyond MAX_ENTRIES (pinned preserved)."""
        try:
            conn = self._get_conn()
            total = conn.execute(
                "SELECT COUNT(*) FROM history"
            ).fetchone()[0]
            if total <= self.MAX_ENTRIES:
                return
            pinned = conn.execute(
                "SELECT COUNT(*) FROM history WHERE pinned = 1"
            ).fetchone()[0]
            allowed_unpinned = max(0, self.MAX_ENTRIES - pinned)
            with conn:
                # Order by timestamp (then entry_id as a tiebreaker) so the
                # DB trims the same entries the in-memory list drops.  A
                # paste-to-top touch() re-stamps an old entry as newest, and
                # trimming by entry_id alone would delete that just-pasted
                # entry from the DB while the in-memory list keeps it.
                conn.execute(
                    "DELETE FROM history WHERE pinned = 0 AND entry_id NOT IN ("
                    "  SELECT entry_id FROM history WHERE pinned = 0 "
                    "  ORDER BY timestamp DESC, entry_id DESC LIMIT ?)",
                    (allowed_unpinned,),
                )
            self._secure_db_files()
        except Exception as exc:
            logger.error("Failed to trim history table: %s", exc)

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
        # Use the local clock for the dedup window so a sender's clock
        # skew can neither defeat dedup (window looks too old) nor break
        # it (window looks negative).  Keep the sender's timestamp for
        # display, when one was supplied.
        dedup_key = _make_dedup_key(content)
        now = time.time()
        captured_at = content.timestamp or now

        with self._lock:
            if (dedup_key == self._last_dedup_key
                    and now - self._last_dedup_time < self.DEDUP_WINDOW):
                # Same primary content within the tight coalesce window.
                # A capture that adds a format the surviving entry lacks
                # must upgrade the entry, not be dropped (that drop used to
                # lose the rich flavor forever); a true duplicate stays dropped.
                top = self._entries[0] if self._entries else None
                if (top is not None
                        and self._stored_text_key(top) == dedup_key
                        and adds_new_flavors(top.get("types"), content.types)):
                    self._merge_into_top(top, content, source_app, captured_at)
                return
            self._last_dedup_key = dedup_key
            self._last_dedup_time = now

            # Same-text re-copy beyond the coalesce window: merge into the
            # newest entry (keeping its richer flavors) instead of stacking
            # a plain-text duplicate that would bury the rich one.  Only
            # text-body keys merge; image/file keys are byte-exact so a
            # different hash there is genuinely different content.
            if dedup_key.startswith("text:") and self._entries:
                top = self._entries[0]
                if (self._stored_text_key(top) == dedup_key
                        and now - (top.get("timestamp") or 0.0)
                        < self.FLAVOR_MERGE_WINDOW):
                    self._merge_into_top(top, content, source_app, captured_at)
                    return

            preview = _build_preview(content.types)
            entry: dict = {
                "timestamp": captured_at,
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
                # Wire-level image format hint ("png"/"bmp"/"tiff") so
                # clients can pick the right MIME for the stored bytes.
                "image_fmt": content.image_fmt or "",
            }
            self._next_id += 1

            self._entries.insert(0, entry)
            # Incremental write: insert only this row (avoids re-encrypting
            # the whole table on every clipboard capture).
            self._insert_row(entry)
            # Age-based cleanup (no-op unless a max-age limit is wired).
            self._prune_by_age()
            if len(self._entries) > self.MAX_ENTRIES:
                # Remove oldest unpinned entries beyond the limit.
                # Keep all pinned entries; trim only unpinned ones.
                pinned = [e for e in self._entries if e.get("pinned")]
                unpinned = [e for e in self._entries if not e.get("pinned")]
                allowed_unpinned = max(0, self.MAX_ENTRIES - len(pinned))
                # Pick survivors with the same key the DB trim uses
                # (timestamp DESC, entry_id DESC) so both sides drop the
                # same entries even when remote timestamps (sender clock)
                # skew arrival order.  Kept entries stay in their existing
                # display positions.
                keep_ids = {
                    id(e) for e in sorted(
                        unpinned,
                        key=lambda e: (e.get("timestamp") or 0.0,
                                       e.get("entry_id") or 0),
                        reverse=True,
                    )[:allowed_unpinned]
                }
                self._entries = pinned + [e for e in unpinned if id(e) in keep_ids]
                self._trim_db()

    # ------------------------------------------------------------------
    # Same-text flavor merge (mirrors ClipboardHistory)
    # ------------------------------------------------------------------

    def _stored_text_key(self, entry: dict) -> str:
        """Rebuild an entry's dedup key from its stored base64 types.

        Recomputing (rather than caching on the row) works for entries
        loaded from disk and legacy records alike, with no schema change.
        """
        types = labels_to_types(entry.get("types"))
        if not types:
            return ""
        return _make_dedup_key(ClipboardContent(types=types))

    def _merge_into_top(self, entry: dict, content: ClipboardContent,
                        source_app: dict | None, captured_at: float) -> None:
        """Fold a same-text re-capture into *entry*, preserving flavors.

        The union of formats keeps every flavor either side had (rich text
        survives a plain re-copy); per format the newer bytes win.  The
        merged entry is re-stamped as the most recent copy — mirroring the
        paste-to-top semantics of re-using an item.
        """
        existing_ct = labels_to_types(entry.get("types"))
        merged_ct, changed = merge_types(existing_ct, content.types)
        if changed:
            entry["types"] = {
                _map_type_to_label(t): base64.b64encode(d).decode("ascii")
                for t, d in merged_ct.items()
            }
            best = ClipboardContent(types=merged_ct).best_format()
            if best is not None:
                entry["content_type"] = _map_type_to_label(best[0])
            entry["text_preview"] = _build_preview(merged_ct)
        entry["source_device"] = (content.source_device
                                  or entry.get("source_device", ""))
        if source_app:
            entry["source_app"] = source_app.get("name", "")
            entry["source_title"] = source_app.get("title", "")
        # Never move backwards: a sender's clock skew must not reorder
        # history relative to entries captured in between.
        entry["timestamp"] = max(captured_at, entry.get("timestamp") or 0.0)
        try:
            self._entries.remove(entry)
        except ValueError:
            pass
        self._entries.insert(0, entry)
        self._persist_entry_update(entry)

    def _persist_entry_update(self, entry: dict) -> None:
        """Incrementally write a merged entry's text columns back to the DB.

        Text-bearing fields are encrypted through ``_encrypt_entry`` when an
        encryption manager is configured, matching how ``_insert_row`` stores
        them (a raw UPDATE would leave those cells as the only plaintext on
        an otherwise-encrypted at-rest store).
        """
        text_fields = ("types", "text_preview",
                       "source_device", "source_app", "source_title")
        fields = {
            "timestamp": entry.get("timestamp", 0.0),
            "content_type": entry.get("content_type", ""),
            "paste_count": entry.get("paste_count", 0),
        }
        if self._enc_mgr:
            enc = self._encrypt_entry(
                {k: (entry.get(k) or ({} if k == "types" else ""))
                 for k in text_fields},
            )
        else:
            enc = {k: (entry.get(k) or ({} if k == "types" else ""))
                   for k in text_fields}
        fields["text_preview"] = enc["text_preview"]
        fields["source_device"] = enc["source_device"]
        fields["source_app"] = enc["source_app"]
        fields["source_title"] = enc["source_title"]
        fields["types"] = json.dumps(enc["types"], ensure_ascii=False)
        self._update_row(entry.get("entry_id"), **fields)

    def get_all(self) -> list[dict]:
        """Return all entries, pinned first, then newest first within each group."""
        with self._lock:
            pinned = [e for e in self._entries if e.get("pinned")]
            unpinned = [e for e in self._entries if not e.get("pinned")]
            return pinned + unpinned

    def search(self, query: str) -> list[dict]:
        """Case-insensitive search in text previews.

        Returns matching entries in the same order as ``get_all()``:
        pinned first, then newest first within each group.
        """
        q = query.lower()
        with self._lock:
            matches = [
                e for e in self._entries
                if q in e.get("text_preview", "").lower()
            ]
            pinned = [e for e in matches if e.get("pinned")]
            unpinned = [e for e in matches if not e.get("pinned")]
            return pinned + unpinned

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
                entry_id = self._entries[internal]["entry_id"]
                self._entries.pop(internal)
                self._delete_rows([entry_id])
                return True
            return False

    def delete_by_id(self, entry_id) -> bool:
        """Delete an entry by its ``entry_id`` (type-tolerant). Returns True if deleted."""
        with self._lock:
            for i, entry in enumerate(self._entries):
                if str(entry.get("entry_id")) == str(entry_id):
                    eid = self._entries[i]["entry_id"]
                    self._entries.pop(i)
                    self._delete_rows([eid])
                    return True
            return False

    def pin(self, index: int) -> bool:
        """Pin an entry by display index (matching get_all() order). Pinned items stay at the top."""
        with self._lock:
            internal = self._display_to_internal(index)
            if internal is not None:
                self._entries[internal]["pinned"] = True
                self._update_row(self._entries[internal]["entry_id"], pinned=1)
                return True
            return False

    def unpin(self, index: int) -> bool:
        """Unpin an entry by display index (matching get_all() order)."""
        with self._lock:
            internal = self._display_to_internal(index)
            if internal is not None:
                self._entries[internal]["pinned"] = False
                self._update_row(self._entries[internal]["entry_id"], pinned=0)
                return True
            return False

    def clear(self) -> None:
        """Delete all history entries and persist the empty state."""
        with self._lock:
            self._entries.clear()
            self._delete_all_rows()
            self._vacuum_db()

    def find_by_id(self, entry_id: str) -> tuple[int, dict] | tuple[None, None]:
        """Find an entry by its ``entry_id``. Returns (index, entry) or (None, None).

        Comparison is type-tolerant: ``entry_id`` may arrive as an int (JSON
        number from the web panel) or a str (URL query parameter).
        """
        with self._lock:
            for i, entry in enumerate(self._entries):
                if str(entry.get("entry_id")) == str(entry_id):
                    return i, dict(entry)
            return None, None

    def increment_paste(self, entry_id: str) -> int | None:
        """Increment the paste count for an entry. Returns new count or None if not found."""
        with self._lock:
            for entry in self._entries:
                if str(entry.get("entry_id")) == str(entry_id):
                    entry["paste_count"] = entry.get("paste_count", 0) + 1
                    self._update_row(entry_id, paste_count=entry["paste_count"])
                    return entry["paste_count"]
            return None

    def touch(self, entry_id: str) -> bool:
        """Move an entry to the top of the newest-first list (paste-to-top).

        Re-using a history item should surface it as the most recent entry.
        Pinned entries keep their pinned section but move to the front of it.
        """
        now = time.time()
        with self._lock:
            for entry in self._entries:
                if str(entry.get("entry_id")) == str(entry_id):
                    entry["timestamp"] = now
                    self._update_row(entry_id, timestamp=now)
                    # Re-sort in-memory list so the UI reflects the new order.
                    self._entries.sort(
                        key=lambda e: (0 if e.get("pinned") else 1, -e.get("timestamp", 0)),
                    )
                    return True
        return False

    def batch_set_pinned(self, entry_ids: list, pinned: bool) -> int:
        """Set pinned state on entries matching the given IDs. Returns count of entries updated.

        Comparison is type-tolerant (str vs int), matching find_by_id():
        the web panel sends ids as JSON numbers while query params arrive
        as strings, and an exact set-membership check matched neither
        against the other.
        """
        id_keys = {str(i) for i in entry_ids}
        count = 0
        matched_ids: list = []
        with self._lock:
            for entry in self._entries:
                if str(entry.get("entry_id")) in id_keys:
                    entry["pinned"] = pinned
                    matched_ids.append(entry.get("entry_id"))
                    count += 1
            if count:
                for entry_id in matched_ids:
                    self._update_row(entry_id, pinned=1 if pinned else 0)
        return count

    def batch_delete(self, entry_ids: list) -> int:
        """Delete entries matching the given IDs. Returns count of entries deleted.

        Type-tolerant id comparison — see batch_set_pinned().
        """
        id_keys = {str(i) for i in entry_ids}
        with self._lock:
            removed_ids = [
                e.get("entry_id") for e in self._entries
                if str(e.get("entry_id")) in id_keys
            ]
            before = len(self._entries)
            self._entries = [
                e for e in self._entries
                if str(e.get("entry_id")) not in id_keys
            ]
            removed = before - len(self._entries)
            if removed:
                self._delete_rows(removed_ids)
            return removed

    # ------------------------------------------------------------------
    # Persistence (SQLite)
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load entries from the SQLite database.

        Automatically migrates from the legacy JSON file if the database
        is empty and the JSON file exists.
        """
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

            # Load in display order (pinned first, then newest by timestamp)
            # so the restored in-memory list matches get_all() and survives a
            # paste-to-top touch() with the same semantics as the live list.
            rows = conn.execute(
                "SELECT entry_id, timestamp, content_type, text_preview, "
                "types, source_device, source_app, source_title, "
                "pinned, paste_count, status, image_fmt "
                "FROM history ORDER BY pinned DESC, timestamp DESC, entry_id DESC"
            ).fetchall()

            self._entries = []
            for row in rows:
                entry = {
                    "entry_id": row[0],
                    "timestamp": row[1],
                    "content_type": row[2],
                    "text_preview": row[3],
                    "types": self._parse_types_json(row[4]),
                    "source_device": row[5],
                    "source_app": row[6],
                    "source_title": row[7],
                    "pinned": bool(row[8]),
                    "paste_count": row[9] if row[9] else 0,
                    "status": row[10] if len(row) > 10 else "",
                    "image_fmt": row[11] if len(row) > 11 else "",
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

            # Apply the age-based cleanup limit (no-op unless wired) once
            # at startup so stale entries don't linger until the next copy.
            self._prune_by_age()

        except Exception as exc:
            logger.warning("Failed to load history from DB: %s", exc)

    def _save(self) -> None:
        """Persist all in-memory entries to the SQLite database.

        Full DELETE + INSERT resync of the in-memory ``_entries`` list.
        Kept for bulk import/restore and for external callers that mutate
        ``_entries`` directly; the per-capture hot paths use incremental
        single-row operations instead.
        """
        self._ensure_db_dir()
        if self._enc_mgr:
            logger.debug(
                "History save: encrypting %d entries for at-rest storage",
                len(self._entries),
            )
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("DELETE FROM history")
                conn.executemany(
                    "INSERT INTO history "
                    "(entry_id, timestamp, content_type, text_preview, types, "
                    "source_device, source_app, source_title, pinned, paste_count, status, image_fmt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self._entry_row(e) for e in self._entries),
                )
            self._secure_db_files()
        except Exception as exc:
            logger.error("Failed to save history to DB: %s", exc)

    def migrate_from_json(self) -> int:
        """Public migration entry point.

        Import entries from the legacy ``clipboard_history.json`` file
        into the SQLite database.  Returns the number of entries migrated.
        Safe to call multiple times — skips if no JSON file exists or
        the database already has entries.
        """
        try:
            conn = self._get_conn()
            self._init_schema(conn)
            row_count = conn.execute(
                "SELECT COUNT(*) FROM history"
            ).fetchone()[0]
            if row_count > 0:
                logger.debug("DB already has %d entries, skipping migration", row_count)
                return 0
            return self._migrate_from_json_file(conn)
        except Exception as exc:
            logger.error("Failed to migrate from JSON: %s", exc)
            return 0

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
                "source_device, source_app, source_title, pinned, paste_count, status, image_fmt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        e.get("status", ""),
                        e.get("image_fmt", ""),
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
