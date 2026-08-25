"""AI-config sync REST backend (Round 12).

Routes (registered as one branch in routes.py; the manager is bound by
src/main.py at startup — no dispatch-signature threading needed):

  GET  /api/aiconfig/inventory[?refresh=1&peer_id=X]
        Cached inventories of known peers:
        {"peers": {pid: {name, entries, fetched_at}}}.
        ?refresh=1 asks the peer (or every paired peer) to re-send its
        inventory now via an inventory_refresh aiconfig_req.

  GET  /api/aiconfig/paths          -> this device's watch list
  POST /api/aiconfig/paths          {"paths": ["~/ai-configs", ...]}
        Normalize + persist the watch list, then recollect and rebroadcast
        the inventory to connected paired peers.

  POST /api/aiconfig/pull           {"peer_id", "items": [{root_index,
        rel_path}...], "mode": "overwrite"|"copy"|"append"}
        Send a aiconfig_req per item.  Landing happens asynchronously when
        each aiconfig_data arrives (mode executed then); clients refresh via
        the `aiconfig_file` WS event.  Responds {"requested": N} immediately.
        mode defaults to "copy" — never overwrite silently.

  POST /api/aiconfig/preview        {"peer_id", "root_index", "rel_path"}
        Fetch one file's content for display only.  Blocks up to 5 s for the
        reply and returns {"ok": true, "content": str(<=64KB), "truncated"}.
        Nothing is written to disk.

All handlers return (data_dict, status_code) and never raise.
"""

import json
import logging

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
        if path == "/api/aiconfig/paths" and method == "GET":
            summary = _mgr.local_summary()
            return {"ok": True, "paths": summary["paths"],
                    "summary": summary}, 200
        if path == "/api/aiconfig/paths" and method == "POST":
            return _set_paths(_body_dict(body))
        if path == "/api/aiconfig/pull" and method == "POST":
            return _pull(_body_dict(body))
        if path == "/api/aiconfig/preview" and method == "POST":
            return _preview(_body_dict(body))
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


def _set_paths(data: dict) -> tuple[dict, int]:
    result = _mgr.set_watch_list(data.get("paths"))
    if not result.get("ok"):
        return result, 400
    # Recollect + rebroadcast so paired peers see the new list immediately.
    sent = _mgr.on_watch_list_changed()
    return {"ok": True, "paths": result["paths"], "broadcast_to": sent}, 200


def _pull(data: dict) -> tuple[dict, int]:
    peer_id = str(data.get("peer_id") or "").strip()
    if not peer_id:
        return {"ok": False, "error": "peer_id required"}, 400
    mode = data.get("mode", "copy")
    result = _mgr.pull(peer_id, data.get("items"), mode=mode)
    result["ok"] = result.get("requested", 0) > 0
    status = 200 if result["ok"] else 400
    return result, status


def _preview(data: dict) -> tuple[dict, int]:
    peer_id = str(data.get("peer_id") or "").strip()
    ri = data.get("root_index")
    rel = data.get("rel_path")
    if not peer_id:
        return {"ok": False, "error": "peer_id required"}, 400
    result = _mgr.preview(peer_id, ri, rel)
    return result, 200 if result.get("ok") else (
        400 if result.get("error") in ("invalid_item", "peer_not_paired",
                                       "peer_offline") else 502)
