"""Clipboard history export / import (JSON and CSV).

All functions operate on the ClipboardHistory model (thread-safe, lock-based).
Accepts both ``ClipboardHistory`` and ``ClipboardHistoryDB`` instances.
"""

import base64
import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Union

from internal.clipboard.history import ClipboardHistory
from internal.clipboard.history_db import ClipboardHistoryDB

logger = logging.getLogger(__name__)

_DEDUP_WINDOW = 5.0  # seconds

_HistoryType = Union[ClipboardHistory, ClipboardHistoryDB]


def _decode_types(entry: dict) -> dict:
    """Decode base64-encoded type values to readable text strings."""
    decoded = {}
    for key, val in entry.get("types", {}).items():
        try:
            decoded[key] = base64.b64decode(val).decode("utf-8", errors="replace")
        except Exception:
            decoded[key] = val
    return decoded


# ---------------------------------------------------------------------------
# JSON export / import
# ---------------------------------------------------------------------------


def export_history_json(history: _HistoryType, filepath: str) -> int:
    """Export all history entries to a JSON file.

    Each entry includes: timestamp, content_type, text_preview, types (base64
    decoded to readable text), source_device, pinned, paste_count.
    Returns the number of exported items.
    """
    entries = history.get_all()
    export_list = []
    for entry in entries:
        export_list.append({
            "timestamp": entry.get("timestamp", 0),
            "content_type": entry.get("content_type", ""),
            "text_preview": entry.get("text_preview", ""),
            "types": _decode_types(entry),
            "source_device": entry.get("source_device", ""),
            "pinned": entry.get("pinned", False),
            "paste_count": entry.get("paste_count", 0),
        })

    out = Path(filepath)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(export_list, indent=2, ensure_ascii=False), encoding="utf-8")
    # Clipboard content is sensitive; don't leave the export world-readable.
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    logger.info("Exported %d history entries to %s", len(export_list), filepath)
    return len(export_list)


def import_history_json(filepath: str, history: _HistoryType) -> int:
    """Import history entries from a JSON file.

    Skips duplicates (same text_preview within 5 seconds).
    Returns the number of imported items.
    """
    src = Path(filepath)
    if not src.is_file():
        logger.warning("Import file not found: %s", filepath)
        return 0

    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        logger.warning("Import JSON is not a list: %s", filepath)
        return 0

    existing = history.get_all()
    imported = 0

    for item in data:
        if _is_duplicate(item, existing):
            continue
        # Build a minimal entry compatible with ClipboardHistory.add()
        # ClipboardHistory.add() expects a ClipboardContent, but we only
        # have the serialised entry dict.  We inject via the internal list
        # and persist to avoid depending on the full ClipboardContent model.
        entry = {
            "timestamp": item.get("timestamp", time.time()),
            "content_type": item.get("content_type", "TEXT"),
            "text_preview": item.get("text_preview", ""),
            "types": _encode_types(item.get("types", {})),
            "source_device": item.get("source_device", ""),
            "pinned": item.get("pinned", False),
            "paste_count": item.get("paste_count", 0),
        }
        # Assign a fresh entry_id
        with history._lock:
            entry["entry_id"] = history._next_id
            history._next_id += 1
            history._entries.insert(0, entry)
            # Trim if over limit
            if len(history._entries) > history.MAX_ENTRIES:
                history._entries = history._entries[: history.MAX_ENTRIES]
            history._save()
        imported += 1

    logger.info("Imported %d history entries from %s", imported, filepath)
    return imported


# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------


def export_history_csv(history: _HistoryType, filepath: str) -> int:
    """Export history entries to a CSV file.

    Columns: timestamp, content_type, text_preview, source_device, pinned, paste_count.
    Returns the number of exported items.
    """
    entries = history.get_all()
    out = Path(filepath)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "content_type", "text_preview",
                        "source_device", "pinned", "paste_count"],
            extrasaction="ignore",
        )
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "timestamp": entry.get("timestamp", 0),
                "content_type": entry.get("content_type", ""),
                "text_preview": entry.get("text_preview", ""),
                "source_device": entry.get("source_device", ""),
                "pinned": entry.get("pinned", False),
                "paste_count": entry.get("paste_count", 0),
            })

    # Clipboard content is sensitive; don't leave the export world-readable.
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    logger.info("Exported %d history entries to %s", len(entries), filepath)
    return len(entries)


def import_history_csv(filepath: str, history: _HistoryType) -> int:
    """Import history entries from a CSV file.

    Skips duplicates (same text_preview within 5 seconds).
    Returns the number of imported items.
    """
    src = Path(filepath)
    if not src.is_file():
        logger.warning("Import file not found: %s", filepath)
        return 0

    existing = history.get_all()
    imported = 0

    with src.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if _is_duplicate(row, existing):
                continue
            entry = {
                "timestamp": float(row.get("timestamp", time.time())),
                "content_type": row.get("content_type", "TEXT"),
                "text_preview": row.get("text_preview", ""),
                "types": {},
                "source_device": row.get("source_device", ""),
                "pinned": (row.get("pinned", "false").lower() == "true"),
                "paste_count": int(row.get("paste_count", 0)),
            }
            with history._lock:
                entry["entry_id"] = history._next_id
                history._next_id += 1
                history._entries.insert(0, entry)
                if len(history._entries) > history.MAX_ENTRIES:
                    history._entries = history._entries[: history.MAX_ENTRIES]
                history._save()
            imported += 1

    logger.info("Imported %d history entries from %s", imported, filepath)
    return imported


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_duplicate(item: dict, existing: list[dict]) -> bool:
    """Return True if *item* duplicates an existing entry (same preview
    within the dedup window)."""
    preview = (item.get("text_preview") or "").strip()
    ts = float(item.get("timestamp", 0))
    if not preview or not ts:
        return False
    for e in existing:
        if (e.get("text_preview") or "").strip() != preview:
            continue
        e_ts = float(e.get("timestamp", 0))
        if abs(ts - e_ts) <= _DEDUP_WINDOW:
            return True
    return False


def _encode_types(types: dict) -> dict:
    """Re-encode type values back to base64 for storage."""
    encoded = {}
    for key, val in types.items():
        encoded[key] = base64.b64encode(
            val.encode("utf-8", errors="replace")
        ).decode("ascii")
    return encoded
