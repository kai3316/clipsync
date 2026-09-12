"""Clipboard history export / import (JSON and CSV).

All functions operate on the ClipboardHistoryDB model (thread-safe, lock-based).
"""

import base64
import contextlib
import csv
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from internal.clipboard.history_db import ClipboardHistoryDB
from internal.i18n import T

logger = logging.getLogger(__name__)

_DEDUP_WINDOW = 5.0  # seconds

_HistoryType = ClipboardHistoryDB


def _coerce_csv_float(value, default: float) -> float:
    """Coerce a CSV cell to float.

    An empty or missing cell falls back to *default*; a present but
    non-numeric cell raises ValueError carrying the localized
    invalid-content message instead of a bare ``could not convert`` error.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(T("web.import_invalid_content")) from None


def _coerce_csv_int(value, default: int) -> int:
    """Coerce a CSV cell to int (same rules as :func:`_coerce_csv_float`)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(T("web.import_invalid_content")) from None


def _decode_types(entry: dict) -> dict:
    """Decode base64-encoded type values to readable text strings.

    Text payloads (TEXT/HTML/RTF/URL) decode to readable UTF-8 text.
    Binary payloads (IMAGE/IMAGE_EMF/FILE) are not valid UTF-8, so they are
    kept as a marked ``{"_b64": ...}`` base64 wrapper instead — decoding
    them with errors="replace" used to corrupt images irreversibly.
    """
    decoded = {}
    for key, val in entry.get("types", {}).items():
        try:
            raw = base64.b64decode(val)
        except Exception:
            decoded[key] = val
            continue
        try:
            decoded[key] = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded[key] = {"_b64": val}
    return decoded


# ---------------------------------------------------------------------------
# JSON export / import
# ---------------------------------------------------------------------------


def export_history_json(history: _HistoryType, filepath: str) -> int:
    """Export all history entries to a JSON file.

    Each entry includes: timestamp, content_type, text_preview, types (base64
    decoded to readable text), source_device, transport, app/title, pinned,
    paste_count.  Entries are streamed to disk one json.dumps call at a time —
    building a single payload string for a large image-heavy history peaked at
    several times the exported size in RAM.  Returns the number of exported
    items.
    """
    entries = history.get_all()
    export_list = []
    for entry in entries:
        export_list.append(
            {
                "timestamp": entry.get("timestamp", 0),
                "content_type": entry.get("content_type", ""),
                "text_preview": entry.get("text_preview", ""),
                "types": _decode_types(entry),
                "source_device": entry.get("source_device", ""),
                # Carried through the round trip: a restored history that lost
                # the route would show rows whose chips changed meaning, and
                # this file is the documented way to move one.
                "transport": entry.get("transport", ""),
                "source_app": entry.get("source_app", ""),
                "source_title": entry.get("source_title", ""),
                "pinned": entry.get("pinned", False),
                "paste_count": entry.get("paste_count", 0),
            }
        )

    out = Path(filepath)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write to <name>.part and rename, so an interrupted export never leaves
    # a truncated file at the final path that looks like a successful export.
    part = out.with_name(out.name + ".part")
    try:
        with part.open("w", encoding="utf-8") as f:
            f.write("[")
            for i, item in enumerate(export_list):
                if i:
                    f.write(",")
                f.write(json.dumps(item, indent=2, ensure_ascii=False))
            f.write("]")
        os.replace(part, out)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    # Clipboard content is sensitive; don't leave the export world-readable.
    with contextlib.suppress(OSError):
        os.chmod(out, 0o600)
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

    # Export files are ordered newest-first and _insert_imported() prepends
    # each entry, so walk the file in reverse to keep the original order —
    # forward iteration used to leave the restored history oldest-first.
    for item in reversed(data):
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
            "source_device": str(item.get("source_device", "") or ""),
            "transport": str(item.get("transport", "") or ""),
            "source_app": str(item.get("source_app", "") or ""),
            "source_title": str(item.get("source_title", "") or ""),
            "pinned": item.get("pinned", False),
            "paste_count": item.get("paste_count", 0),
        }
        _insert_imported(history, entry)
        imported += 1

    _finalize_import(history, imported)

    logger.info("Imported %d history entries from %s", imported, filepath)
    return imported


# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------


def export_history_csv(history: _HistoryType, filepath: str) -> int:
    """Export history entries to a CSV file.

    Columns: timestamp (epoch seconds), time_iso (local ISO-8601),
    content_type, text_preview, source_device, transport, source_app,
    source_title, pinned, paste_count, byte_size (approximate decoded payload
    size).  Returns the number of exported items.
    """
    entries = history.get_all()
    out = Path(filepath)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Write to <name>.part and rename: an interrupted CSV export must not
    # leave a truncated file at the final path.
    part = out.with_name(out.name + ".part")
    try:
        with part.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "time_iso",
                    "content_type",
                    "text_preview",
                    "source_device",
                    "transport",
                    "source_app",
                    "source_title",
                    "pinned",
                    "paste_count",
                    "byte_size",
                ],
                extrasaction="ignore",
            )
            writer.writeheader()
            for entry in entries:
                ts = entry.get("timestamp", 0) or 0
                writer.writerow(
                    {
                        "timestamp": ts,
                        "time_iso": _iso_timestamp(ts),
                        "content_type": entry.get("content_type", ""),
                        "text_preview": entry.get("text_preview", ""),
                        "source_device": entry.get("source_device", ""),
                        "transport": entry.get("transport", ""),
                        "source_app": entry.get("source_app", ""),
                        "source_title": entry.get("source_title", ""),
                        "pinned": entry.get("pinned", False),
                        "paste_count": entry.get("paste_count", 0),
                        "byte_size": _types_byte_size(entry.get("types", {})),
                    }
                )
        os.replace(part, out)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise

    # Clipboard content is sensitive; don't leave the export world-readable.
    with contextlib.suppress(OSError):
        os.chmod(out, 0o600)
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
        # Reversed like the JSON import: our CSVs are newest-first and
        # _insert_imported() prepends, so backwards iteration keeps order.
        rows = reversed(list(reader))
        for row in rows:
            if _is_duplicate(row, existing):
                continue
            entry = {
                "timestamp": _coerce_csv_float(row.get("timestamp"), time.time()),
                "content_type": row.get("content_type", "TEXT"),
                "text_preview": row.get("text_preview", ""),
                "types": {},
                "source_device": str(row.get("source_device", "") or ""),
                # A CSV exported before this column existed simply has no key
                # here, and the row comes back with no route — which is what
                # an empty value means everywhere else.
                "transport": str(row.get("transport", "") or ""),
                "source_app": str(row.get("source_app", "") or ""),
                "source_title": str(row.get("source_title", "") or ""),
                "pinned": (row.get("pinned", "false").lower() == "true"),
                "paste_count": _coerce_csv_int(row.get("paste_count"), 0),
            }
            _insert_imported(history, entry)
            imported += 1

    _finalize_import(history, imported)

    logger.info("Imported %d history entries from %s", imported, filepath)
    return imported


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


def export_history_markdown(history: _HistoryType, filepath: str) -> int:
    """Export history entries to a human-readable Markdown file.

    Entries are grouped by local calendar day (most recent first), one
    bullet per clip carrying time, content type, source app/device and
    paste count.  Sorted strictly by time (not the pinned-first display
    order) so each day appears exactly once.  Returns the number of
    exported items.
    """
    entries = history.get_all()
    entries.sort(
        key=lambda e: e.get("timestamp", 0) or 0,
        reverse=True,
    )
    out = Path(filepath)
    out.parent.mkdir(parents=True, exist_ok=True)

    part = out.with_name(out.name + ".part")
    count = 0
    try:
        with part.open("w", encoding="utf-8") as f:
            f.write("# ClipSync History Export\n\n")
            current_day = None
            for entry in entries:
                dt = _local_datetime(entry.get("timestamp", 0))
                if dt is None:
                    continue
                day = dt.strftime("%Y-%m-%d")
                if day != current_day:
                    current_day = day
                    f.write(f"## {day}\n\n")
                ctype = _CONTENT_LABELS.get(
                    entry.get("content_type", ""),
                    entry.get("content_type", "") or "Clip",
                )
                source = entry.get("source_app", "") or entry.get("source_device", "")
                # The route joins the source on the same line: a reader
                # comparing this file against the window's rows needs the same
                # pair of facts, and the device name alone cannot say which
                # path a clip took.
                route = _TRANSPORT_LABELS.get(str(entry.get("transport", "")), "")
                origin = " · ".join(part for part in (source, route) if part)
                origin = f" · {origin}" if origin else ""
                pastes = entry.get("paste_count", 0) or 0
                pasted = f" · {pastes} paste(s)" if pastes else ""
                preview = entry.get("text_preview", "") or ""
                preview = preview.replace("\r", " ").replace("\n", " ").strip()
                f.write(f"- {dt.strftime('%H:%M')} · {ctype}{origin}{pasted}: {preview}\n")
                count += 1
        os.replace(part, out)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise

    # Clipboard content is sensitive; don't leave the export world-readable.
    with contextlib.suppress(OSError):
        os.chmod(out, 0o600)
    logger.info("Exported %d history entries to %s", count, filepath)
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_duplicate(item: dict, existing: list[dict]) -> bool:
    """Return True if *item* duplicates an existing entry (same preview
    within the dedup window)."""
    preview = (item.get("text_preview") or "").strip()
    try:
        ts = float(item.get("timestamp", 0))
    except (TypeError, ValueError):
        ts = 0.0
    if not preview or not ts:
        return False
    for e in existing:
        if (e.get("text_preview") or "").strip() != preview:
            continue
        e_ts = float(e.get("timestamp", 0))
        if abs(ts - e_ts) <= _DEDUP_WINDOW:
            return True
    return False


# Human-readable content-type labels for the Markdown export.
_CONTENT_LABELS: dict[str, str] = {
    "TEXT": "Text",
    "HTML": "HTML",
    "RTF": "Rich text",
    "IMAGE": "Image",
    "IMAGE_EMF": "Vector image",
    "FILE": "File",
    "URL": "Link",
}

#: How the Markdown report names each route a clip can arrive on.  Only routes
#: that were recorded are listed: a clip captured here, and one whose route was
#: never recorded, both say nothing rather than claiming a path.  "web" is the
#: push from this machine's own web server — a route like the other two, and the
#: one a reader would otherwise have to guess at, since its source name says
#: "Web" and not where the browser was.
_TRANSPORT_LABELS: dict[str, str] = {
    "lan": "local link",
    "relay": "internet relay",
    "web": "web push",
}


def _local_datetime(ts) -> datetime | None:
    """Local datetime for an epoch timestamp, or None when unset/invalid."""
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        return None
    if ts_f <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts_f)
    except (OSError, OverflowError):
        return None


def _iso_timestamp(ts) -> str:
    """Local ISO-8601 rendering of an epoch timestamp ('' when unset)."""
    dt = _local_datetime(ts)
    return dt.isoformat(timespec="seconds") if dt else ""


def _types_byte_size(types: dict) -> int:
    """Approximate decoded payload size from base64 lengths (no decode)."""
    total = 0
    for v in types.values():
        if isinstance(v, str):
            total += max(0, len(v) * 3 // 4 - v.count("=", -2))
    return total


def _insert_imported(history: _HistoryType, entry: dict) -> None:
    """Insert one imported entry at the top of *history* (no persist).

    Shared by the JSON and CSV import paths.  Callers batch rows first and
    persist once via :func:`_finalize_import` — persisting (a full-table
    rewrite) after every single row made a large import quadratic.
    """
    with history._lock:
        entry["entry_id"] = history._next_id
        history._next_id += 1
        history._entries.insert(0, entry)


def _finalize_import(history: _HistoryType, imported: int) -> None:
    """Trim over-limit entries once and persist an imported batch.

    The over-limit trim mirrors add() — pinned entries survive, unpinned
    ones beyond MAX_ENTRIES are dropped.  Trimming once at the end yields
    the same final state as trimming after every insert: both keep the
    newest ``allowed`` unpinned entries in insertion order, so intermediate
    trims only ever drop entries the final trim would drop anyway.
    """
    if imported <= 0:
        return
    with history._lock:
        if len(history._entries) > history.MAX_ENTRIES:
            pinned = [e for e in history._entries if e.get("pinned")]
            unpinned = [e for e in history._entries if not e.get("pinned")]
            allowed = max(0, history.MAX_ENTRIES - len(pinned))
            history._entries = pinned + unpinned[:allowed]
        history._save()


def _encode_types(types: dict) -> dict:
    """Re-encode type values back to base64 for storage.

    Plain strings (the readable-text form written by _decode_types, and the
    already-lossy output of older backups) are base64-encoded as-is — old
    backups stay exactly as they were, never corrupted further.  Binary
    payloads written as ``{"_b64": ...}`` wrappers are stored verbatim, so
    the export→import round-trip for images is byte-exact.
    """
    encoded = {}
    for key, val in types.items():
        if isinstance(val, dict) and isinstance(val.get("_b64"), str):
            encoded[key] = val["_b64"]
        elif isinstance(val, str):
            encoded[key] = base64.b64encode(val.encode("utf-8", errors="replace")).decode("ascii")
        else:
            # Unknown / malformed value — drop it rather than crash the import.
            continue
    return encoded
