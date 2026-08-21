"""Clipboard history with local JSON persistence.

Stores up to 50 most recent clipboard entries in
  {config_dir}/clipboard_history.json
"""

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
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
    # Remove <style> and <script> blocks (including their content)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # Strip remaining tags
    plain = re.sub(r"<[^>]*>", "", text)
    # Unescape HTML entities
    plain = _html.unescape(plain)
    # Collapse whitespace
    plain = re.sub(r"\s+", " ", plain)
    return plain.strip()


def _make_dedup_key(content: ClipboardContent) -> str:
    """Build a stable dedup key from the 'primary' content.

    Uses the text body (when available) rather than hashing all types,
    so multi-step clipboard writes (TEXT → HTML → RTF) that produce
    different ``hash_key()`` values are still recognised as the same
    user action.  Falls back to image-data hashes for image-only copies.
    """
    if ContentType.TEXT in content.types:
        text = content.types[ContentType.TEXT].decode("utf-8", errors="replace")
        # Hash the full body so two long texts sharing a prefix are not
        # wrongly coalesced within the dedup window.
        return "text:" + hashlib.sha256(
            text.encode("utf-8", errors="replace")
        ).hexdigest()
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
    if ContentType.FILE in content.types:
        # FILE content is the newline-joined file paths — hash them so
        # file-only copies dedup instead of falling through to a unique key.
        return "file:" + hashlib.sha256(
            content.types[ContentType.FILE]
        ).hexdigest()
    if ContentType.URL in content.types:
        return "url:" + hashlib.sha256(
            content.types[ContentType.URL]
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


class ClipboardHistory:
    """Thread-safe clipboard history persisted to a local JSON file."""

    # Minimum interval (seconds) between entries with identical primary content.
    # Multi-step clipboard writes (TEXT → HTML → RTF) can trigger multiple
    # monitor events that each produce a read with slightly different format
    # sets.  This dedup window coalesces them into one entry.
    DEDUP_WINDOW = 2.0

    def __init__(self, storage_path: str | None = None, max_entries: int = 50,
                 enc_mgr: "EncryptionManager | None" = None):
        if storage_path:
            self._path = Path(storage_path)
        else:
            self._path = _config_dir() / "clipboard_history.json"
        self.MAX_ENTRIES = max_entries
        self._entries: list[dict] = []
        self._lock = threading.RLock()
        self._enc_mgr = enc_mgr
        self._last_dedup_key: str = ""
        self._last_dedup_time: float = 0.0
        self._next_id: int = 0
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, content: ClipboardContent,
            source_app: dict | None = None) -> None:
        """Add a clipboard entry. Silently ignores empty content.

        Deduplicates: entries with the same primary content (text body or
        image bytes) within a short window are coalesced into one record.
        This handles multi-step clipboard writes where each format triggers
        a separate monitor event.

        *source_app* is an optional dict with ``name``, ``process``, and
        ``title`` keys from the source tracker.
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
            if dedup_key == self._last_dedup_key:
                if now - self._last_dedup_time < self.DEDUP_WINDOW:
                    return  # same primary content within window — coalesce
            self._last_dedup_key = dedup_key
            self._last_dedup_time = now

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
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load history: %s", exc)
            return

        if isinstance(data, list):
            self._entries = data[: self.MAX_ENTRIES]
            if self._entries:
                self._next_id = max(e.get("entry_id", 0) for e in self._entries) + 1
            if self._enc_mgr:
                for entry in self._entries:
                    self._decrypt_entry(entry)
                logger.debug("History load: decrypted %d entries from disk",
                           len(self._entries))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            entries_to_save = self._entries
            if self._enc_mgr:
                entries_to_save = [self._encrypt_entry(e) for e in self._entries]
                logger.debug("History save: encrypted %d entries for at-rest storage",
                           len(entries_to_save))
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".history_tmp_", suffix=".json",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(entries_to_save, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.error("Failed to save history: %s", exc)
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # Fields encrypted at rest — excludes timestamp and content_type
    # so the file structure is still human-readable for debugging.
    _ENCRYPTED_FIELDS = ("types", "text_preview", "source_device")

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
