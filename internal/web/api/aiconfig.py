"""AI-config sync REST backend (tool profiles + v3 root ids).

Routes (registered as one branch in routes.py; the manager is bound by
src/main.py at startup — no dispatch-signature threading needed):

  GET  /api/aiconfig/inventory[?refresh=1&peer_id=X]
        Cached inventories of known peers:
        {"peers": {pid: {name, legacy, entries, fetched_at}}, "local": {...}}.
        Entries are v3 (tool / root / rel_path), v2 (no ``root``) or legacy
        (root_index / path) per peer's ``legacy`` flag.  ``root`` is the stable
        profile-entry id telling two dir roots of one tool apart.  ``?refresh=1``
        asks the peer (or every paired peer) to re-send its inventory now via an
        inventory_refresh aiconfig_req.

  GET  /api/aiconfig/profiles
        {"tools": [{key,label,entries:[{id,path,kind}]}...], "enabled": [keys...],
         "custom_paths": [...]} — single source of truth for the UI; presets
        are never hard-coded client-side.

  POST /api/aiconfig/profiles   {"tools": [keys...], "custom_paths": [paths...]}
        Normalize + persist the enabled tool profiles and custom paths, then
        recollect and rebroadcast the inventory to connected paired peers.

  POST /api/aiconfig/pull       {"peer_id", "items": [{tool, root, rel_path[, is_dir]},
        ...], "mode": "overwrite"|"copy"|"append", "batch_id": str}
        Sends a aiconfig_req per item (folders expanded server-side from the
        peer's cached inventory).  Landing happens asynchronously when each
        aiconfig_data arrives; clients track progress via the `aiconfig_file`
        WS event, which echoes *batch_id*.  mode defaults to "copy" — never
        overwrite silently.  Responds {"requested": N} immediately.

  POST /api/aiconfig/preview    {"peer_id", "tool", "root", "rel_path"}
                                OR {"peer_id", "root_index", "rel_path"} (legacy)
        Fetch one file's content for display only.  Blocks up to 5 s for the
        reply and returns {"ok": true, "content": str(<=64KB), "truncated"}.
        Nothing is written to disk.

Local file manager (no pairing required, a basic file manager over the same
tool-profile roots):

  GET  /api/aiconfig/local
        Fresh local listing: {"collected_at", "tools": [...], "custom_paths":
        [...], "roots": [{tool, root, kind, path, count}], "entries": [{tool,
        root, rel_path, size, mtime, sha256, is_dir}]}.

  GET  /api/aiconfig/local/item?tool=&root=&rel_path=
        Read one file's text content: {"ok", "content" (<=64KB), "truncated"}.
        Binary content (NUL bytes) -> 400 "binary"; missing file -> 404
        "not_found"; traversal -> 400.

  POST /api/aiconfig/local/save     {"tool", "root", "rel_path", "content"}
        Write text back: automatic <rel_path>.bak of the pre-save original,
        atomic temp-file + os.replace, text-only (NUL refused), content capped
        at 256 KB.  Returns {"ok"}.

  POST /api/aiconfig/local/trash    {"tool", "root", "rel_path"}
        MOVE a watched file/dir to <data_dir>/aiconfig_trash/<original
        subpath>/<timestamp>_<name> — never a physical delete.  Returns
        {"ok", "trashed_to"}.

  POST /api/aiconfig/open           {"tool", "root", "rel_path"}
        Open the file (or directory) with the OS default app.

All handlers return (data_dict, status_code) and never raise.
"""

import json
import logging

from internal.sync import ai_profiles

logger = logging.getLogger(__name__)

# Bound AIConfigManager (set once by src/main.py at service creation).
_mgr = None


def bind(mgr) -> None:
    global _mgr
    _mgr = mgr


def _body_dict(body) -> dict:
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def handle(method: str, path: str, query_params, body) -> tuple[dict, int]:
    """Entry point for the routes.py /api/aiconfig branch."""
    if _mgr is None:
        return {"ok": False, "error": "aiconfig unavailable"}, 503
    try:
        if path == "/api/aiconfig/inventory" and method == "GET":
            return _inventory(query_params)
        if path == "/api/aiconfig/profiles" and method == "GET":
            return _profiles(), 200
        if path == "/api/aiconfig/profiles" and method == "POST":
            return _set_profiles(_body_dict(body))
        if path == "/api/aiconfig/pull" and method == "POST":
            return _pull(_body_dict(body))
        if path == "/api/aiconfig/preview" and method == "POST":
            return _preview(_body_dict(body))
        if path == "/api/aiconfig/local" and method == "GET":
            return _mgr.local_listing(), 200
        if path == "/api/aiconfig/local/item" and method == "GET":
            return _local_item(query_params)
        if path == "/api/aiconfig/local/save" and method == "POST":
            return _local_save(_body_dict(body))
        if path == "/api/aiconfig/local/trash" and method == "POST":
            return _local_trash(_body_dict(body))
        if path == "/api/aiconfig/open" and method == "POST":
            return _open_local(_body_dict(body))
        return {"ok": False, "error": "not found"}, 404
    except Exception:
        logger.exception("aiconfig handler failed: %s %s", method, path)
        return {"ok": False, "error": "handler failed"}, 500


def _inventory(query_params) -> tuple[dict, int]:
    peers = _mgr.get_peer_inventories()
    refreshed = []
    if str(query_params.get("refresh", [""])[0]).strip() in ("1", "true"):
        target = str(query_params.get("peer_id", [""])[0]).strip()
        candidates = [target] if target else sorted(peers.keys())
        for pid in candidates:
            if _mgr.request_inventory(pid):
                refreshed.append(pid)
    return {
        "peers": peers,
        "refreshed": refreshed,
        "local": _mgr.local_summary(),
    }, 200


def _profiles() -> dict:
    """Tool profile table (single source of truth) + the current selection."""
    from internal.sync import ai_profiles

    return {
        "ok": True,
        "tools": ai_profiles.TOOLS,
        "enabled": list(getattr(_mgr._cfg, "ai_config_tools", []) or []),
        "custom_paths": list(getattr(_mgr._cfg, "ai_config_custom_paths", []) or []),
    }


def _set_profiles(data: dict) -> tuple[dict, int]:
    result = _mgr.set_profiles(data.get("tools"), data.get("custom_paths"))
    if not result.get("ok"):
        return result, 400
    # Recollect + rebroadcast so paired peers see the new selection immediately.
    sent = _mgr.on_watch_list_changed()
    return {
        "ok": True,
        "tools": result["tools"],
        "custom_paths": result["custom_paths"],
        "broadcast_to": sent,
    }, 200


def _pull(data: dict) -> tuple[dict, int]:
    peer_id = str(data.get("peer_id") or "").strip()
    if not peer_id:
        return {"ok": False, "error": "peer_id required"}, 400
    mode = data.get("mode", "copy")
    batch_id = data.get("batch_id")
    if not isinstance(batch_id, str):
        batch_id = ""
    result = _mgr.pull(peer_id, data.get("items"), mode=mode, batch_id=batch_id[:128])
    result["ok"] = result.get("requested", 0) > 0
    status = 200 if result["ok"] else 400
    return result, status


def _preview(data: dict) -> tuple[dict, int]:
    peer_id = str(data.get("peer_id") or "").strip()
    if not peer_id:
        return {"ok": False, "error": "peer_id required"}, 400
    if "tool" in data and "rel_path" in data:
        root = data.get("root")
        if root is None or root == "":
            root = ""
        elif not ai_profiles.valid_root_id(root):
            return {"ok": False, "error": "invalid_item"}, 400
        result = _mgr.preview(peer_id, data.get("tool"), data.get("rel_path"), root=root)
    elif "root_index" in data and "rel_path" in data:
        result = _mgr.preview_legacy(peer_id, data.get("root_index"), data.get("rel_path"))
    else:
        return {"ok": False, "error": "invalid_item"}, 400
    return result, 200 if result.get("ok") else (
        400
        if result.get("error") in ("invalid_item", "peer_not_paired", "peer_offline", "legacy_peer")
        else 502
    )


# ----------------------------------------------------- local file manager


def _tool_rel(data, query=None) -> tuple | None:
    """Normalize a (tool, rel_path, root) triple from a dict or query params.

    Returns (tool, rel, root) or None when malformed.  tool must be a non-empty
    string (bool rejected); rel must be a non-empty string; root is the v3 root
    id and is optional (""), since a listing row from a single-root tool needs
    no disambiguation.  Query-param values arrive as strings; the first list
    element is taken.
    """
    src = query if query is not None else data
    if query is not None:
        tool_raw = (src.get("tool") or [None])[0]
        rel_raw = (src.get("rel_path") or [None])[0]
        root_raw = (src.get("root") or [None])[0]
    else:
        tool_raw = src.get("tool")
        rel_raw = src.get("rel_path")
        root_raw = src.get("root")
    if isinstance(tool_raw, bool) or not isinstance(tool_raw, str) or not tool_raw:
        return None
    if not isinstance(rel_raw, str) or not rel_raw:
        return None
    if root_raw is None or root_raw == "":
        root = ""
    elif not ai_profiles.valid_root_id(root_raw):
        return None
    else:
        root = root_raw.strip()
    return tool_raw.strip(), rel_raw.strip(), root


def _local_item(query_params) -> tuple[dict, int]:
    item = _tool_rel(None, query=query_params)
    if item is None:
        return {"ok": False, "error": "invalid_item"}, 400
    tool, rel, root = item
    result = _mgr.local_read(tool, rel, root=root)
    status = 404 if result.get("error") == "not_found" else 400
    return result, status if not result.get("ok") else 200


def _local_save(data: dict) -> tuple[dict, int]:
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return {"ok": False, "error": "content_required"}, 400
    item = _tool_rel(data)
    if item is None:
        return {"ok": False, "error": "invalid_item"}, 400
    tool, rel, root = item
    result = _mgr.local_save(tool, rel, data["content"], root=root)
    return result, 200 if result.get("ok") else 400


def _local_trash(data: dict) -> tuple[dict, int]:
    item = _tool_rel(data)
    if item is None:
        return {"ok": False, "error": "invalid_item"}, 400
    tool, rel, root = item
    result = _mgr.local_trash(tool, rel, root=root)
    if not result.get("ok"):
        status = 404 if result.get("error") == "not_found" else 400
        return result, status
    return result, 200


def _open_local(data: dict) -> tuple[dict, int]:
    item = _tool_rel(data)
    if item is None:
        return {"ok": False, "error": "invalid_item"}, 400
    tool, rel, root = item
    result = _mgr.local_open(tool, rel, root=root)
    return result, 200 if result.get("ok") else 400
