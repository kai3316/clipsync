"""Clipboard history API handlers.

All handlers return a (data_dict, status_code) tuple.
"""

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)


def get_history(history, cfg, limit_str=None, offset_str=None):
    """Return clipboard history list with device name mapping.

    Args:
        history: ClipboardHistory or ClipboardHistoryDB instance.
        cfg: Config instance.
        limit_str: Optional string from ``?limit=N`` query param.
                   Overrides cfg.web_history_limit when present.
        offset_str: Optional string from ``?offset=N`` query param.
                    Number of items to skip.
    """
    items = history.get_all()

    # Allow callers (e.g. quick-paste window) to request more items
    limit = cfg.web_history_limit
    if limit_str is not None:
        try:
            parsed = int(limit_str)
            if parsed > 0:
                limit = parsed
        except (ValueError, TypeError):
            pass

    offset = 0
    if offset_str is not None:
        try:
            parsed = int(offset_str)
            if parsed > 0:
                offset = parsed
        except (ValueError, TypeError):
            pass

    total = len(items)
    if offset > 0:
        items = items[offset:]
    if limit > 0 and len(items) > limit:
        items = items[:limit]
    device_names = {cfg.device_id: cfg.device_name}
    for peer in list(cfg.peers.values()):
        device_names[peer.device_id] = peer.device_name
    device_names["__web__"] = "\U0001f4f1 Web"
    result = []
    for entry in items:
        sid = entry.get("source_device", "")
        # List responses deliberately omit the full base64 ``types`` dict —
        # it can be large (images, rich text) and is only needed when the
        # user wants to copy/use a specific item.  Clients fetch the full
        # content via GET /api/history/item when required.
        result.append({
            "timestamp": entry.get("timestamp"),
            "content_type": entry.get("content_type", "TEXT"),
            "text_preview": entry.get("text_preview", ""),
            "source_device": sid,
            "source_name": device_names.get(sid, sid),
            "source_app": entry.get("source_app", ""),
            "source_title": entry.get("source_title", ""),
            "entry_id": entry.get("entry_id"),
            "pinned": entry.get("pinned", False),
            "paste_count": entry.get("paste_count", 0),
        })
    return {"items": result, "total": total, "offset": offset, "limit": limit}, 200


def get_history_item(query_params, history, cfg):
    """Return a single history entry's full content, including ``types``.

    This is the counterpart to ``get_history``: list responses ship only
    metadata + ``text_preview`` to keep the payload small, and clients that
    need the full base64 content (copy, add-to-favorites, view detail) fetch
    it here by ``entry_id``.
    """
    entry_id = (query_params.get("entry_id", [""])[0] or "").strip()
    if not entry_id:
        return {"error": "entry_id required"}, 400

    _, entry = history.find_by_id(entry_id)
    if entry is None:
        return {"error": "not found"}, 404

    device_names = {cfg.device_id: cfg.device_name}
    for peer in list(cfg.peers.values()):
        device_names[peer.device_id] = peer.device_name
    sid = entry.get("source_device", "")
    result = {
        "timestamp": entry.get("timestamp"),
        "content_type": entry.get("content_type", "TEXT"),
        "text_preview": entry.get("text_preview", ""),
        "types": entry.get("types", {}),
        "source_device": sid,
        "source_name": device_names.get(sid, sid),
        "source_app": entry.get("source_app", ""),
        "source_title": entry.get("source_title", ""),
        "entry_id": entry.get("entry_id"),
        "pinned": entry.get("pinned", False),
        "paste_count": entry.get("paste_count", 0),
    }
    return {"item": result}, 200


def push_text(body, cfg, sync_mgr, history):
    """Push text to local clipboard and broadcast to peers.

    Replicates the existing POST /api/push logic from server.py
    (original lines 1333-1372).
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    text = data.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}, 400

    from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage

    tee_bytes = text.encode("utf-8")
    WEB_SOURCE = "__web__"
    content = ClipboardContent(
        types={ContentType.TEXT: tee_bytes},
        source_device=WEB_SOURCE,
    )

    from internal.clipboard.platform import create_writer
    writer = create_writer()
    writer.write(content)

    if hasattr(sync_mgr, '_suppress_monitor_until'):
        sync_mgr._suppress_monitor_until = time.time() + 2.0

    try:
        history.add(content)
    except Exception:
        logger.debug("Failed to add web push to history", exc_info=True)

    msg = SyncMessage(
        content=content,
        msg_id=uuid.uuid4().hex,
        source_device=cfg.device_id,
    )
    if sync_mgr.on_send:
        try:
            sync_mgr.on_send(msg)
        except Exception:
            logger.debug("Web push broadcast failed", exc_info=True)

    logger.info("Web push: %d chars", len(text))
    return {"ok": True, "len": len(text)}, 200


def delete_item(body, history):
    """Delete a history item by index.

    Replicates the existing POST /api/delete logic from server.py
    (original lines 1428-1439).
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_id = data.get("entry_id")
    if entry_id is not None:
        count = history.batch_delete([entry_id])
        return {"ok": count > 0}, 200

    idx = data.get("index", -1)
    if not isinstance(idx, int) or idx < 0:
        return {"ok": False, "error": "invalid index"}, 400

    ok = history.delete(idx)
    return {"ok": ok}, 200


def toggle_pin(body, history):
    """Toggle pin state of a history item.

    Replicates the existing POST /api/pin logic from server.py
    (original lines 1441-1460).
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_id = data.get("entry_id")
    if entry_id is not None:
        _, entry = history.find_by_id(entry_id)
        if entry is None:
            return {"ok": False, "error": "not found"}, 404
        pinned = not entry.get("pinned", False)
        count = history.batch_set_pinned([entry_id], pinned)
        return {"ok": count > 0, "pinned": pinned}, 200

    idx = data.get("index", -1)
    if not isinstance(idx, int) or idx < 0:
        return {"ok": False, "error": "invalid index"}, 400

    entry = history.get(idx)
    if entry is None:
        return {"ok": False, "error": "not found"}, 404

    if entry.get("pinned"):
        history.unpin(idx)
        return {"ok": True, "pinned": False}, 200
    else:
        history.pin(idx)
        return {"ok": True, "pinned": True}, 200


def increment_paste_count(body, history):
    """Increment the paste count for a history entry by entry_id.

    Called when the user copies an item to clipboard from the web UI.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_id = data.get("entry_id")
    if entry_id is None:
        return {"ok": False, "error": "entry_id required"}, 400

    new_count = history.increment_paste(entry_id)
    if new_count is not None:
        return {"ok": True, "paste_count": new_count}, 200

    return {"ok": False, "error": "not found"}, 404


def batch_pin(body, history):
    """Set pinned state on multiple history entries at once.

    Expects: {"entry_ids": [...], "pinned": true/false}
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_ids = data.get("entry_ids", [])
    if not isinstance(entry_ids, list) or not entry_ids:
        return {"ok": False, "error": "entry_ids list required"}, 400

    pinned = data.get("pinned", True)

    count = history.batch_set_pinned(entry_ids, pinned)

    return {"ok": True, "count": count}, 200


def batch_delete(body, history):
    """Delete multiple history entries at once.

    Expects: {"entry_ids": [...]}
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_ids = data.get("entry_ids", [])
    if not isinstance(entry_ids, list) or not entry_ids:
        return {"ok": False, "error": "entry_ids list required"}, 400

    count = history.batch_delete(entry_ids)

    return {"ok": True, "count": count}, 200


def batch_favorite(body, history):
    """Add multiple history entries to favorites at once.

    Expects: {"entry_ids": [...], "group": ""}
    """
    import uuid
    import time

    from internal.web.api.favorites import _load_favorites, _save_favorites

    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_ids = data.get("entry_ids", [])
    if not isinstance(entry_ids, list) or not entry_ids:
        return {"ok": False, "error": "entry_ids list required"}, 400

    group = data.get("group", "").strip()

    favorites = _load_favorites()
    count = 0
    for entry_id in entry_ids:
        _, entry = history.find_by_id(entry_id)
        if entry is not None:
            preview = entry.get("text_preview", "")
            fav_entry = {
                "id": uuid.uuid4().hex[:12],
                "title": preview[:50] if preview else "(empty)",
                "content": preview if preview else "",
                "group": group,
                "created": time.time(),
            }
            favorites.insert(0, fav_entry)
            count += 1

    _save_favorites(favorites)
    logger.info("Batch favorite: added %d items", count)
    return {"ok": True, "count": count}, 200


def paste_rich(body, history, on_reset_dedup=None):
    """Paste all available formats for a history entry to the clipboard.

    Looks up the entry by index, decodes all stored format types
    (TEXT, HTML, RTF, IMAGE, IMAGE_EMF, FILE, URL) from base64, and
    writes them all at once to the platform clipboard via the native
    writer.

    *on_reset_dedup* is an optional callback (wired by the host app) that
    clears the sync manager's dedup state before the write, so the monitor
    event from this paste is synced to peers instead of being filtered as
    a duplicate.

    Returns a list of format names that were successfully decoded.
    """
    import base64

    from internal.clipboard.format import ClipboardContent, ContentType

    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400

    entry_id = data.get("entry_id")
    if entry_id is None:
        # Allow index-based lookup as fallback
        idx = data.get("index")
        if isinstance(idx, int) and idx >= 0:
            entry = history.get(idx)
        else:
            return {"ok": False, "error": "entry_id or index required"}, 400
    else:
        _, entry = history.find_by_id(entry_id)

    if entry is None:
        return {"ok": False, "error": "entry not found"}, 404

    raw_types: dict = entry.get("types", {})
    if not raw_types:
        return {"ok": False, "error": "entry has no stored types"}, 400

    _label_map: dict[str, ContentType] = {
        "TEXT": ContentType.TEXT,
        "HTML": ContentType.HTML,
        "RTF": ContentType.RTF,
        "IMAGE": ContentType.IMAGE_PNG,
        "IMAGE_EMF": ContentType.IMAGE_EMF,
        "FILE": ContentType.FILE,
        "URL": ContentType.URL,
    }

    decoded_types: dict[ContentType, bytes] = {}
    decoded_names: list[str] = []
    for label, b64_data in raw_types.items():
        ct = _label_map.get(label)
        if ct is None:
            continue
        try:
            decoded_types[ct] = base64.b64decode(b64_data)
            decoded_names.append(label)
        except Exception:
            logger.debug("Failed to decode base64 for type %s", label)

    if not decoded_types:
        return {"ok": False, "error": "no valid types could be decoded"}, 400

    from internal.clipboard.platform import create_writer

    content = ClipboardContent(
        types=decoded_types,
        source_device=entry.get("source_device", ""),
    )
    # Clear dedup state so the monitor event from this write is not
    # suppressed and the pasted content re-syncs to peers.
    if on_reset_dedup is not None:
        try:
            on_reset_dedup()
        except Exception:
            logger.debug("reset_dedup callback failed", exc_info=True)
    writer = create_writer()
    writer.write(content)

    # Increment paste count
    eid = entry.get("entry_id")
    if eid is not None:
        history.increment_paste(eid)

    logger.info("paste_rich: wrote %d format(s) to clipboard for entry %s",
                len(decoded_names), entry.get("entry_id", "?"))
    return {"ok": True, "formats": decoded_names, "count": len(decoded_names)}, 200
