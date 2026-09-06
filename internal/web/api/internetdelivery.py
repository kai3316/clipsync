"""Internet delivery REST backend (Round 17).

The delivery ledger / offline queue live in src/main.py (Application methods,
guarded by ``_delivery_lock``).  This module is the self-contained bind-style
branch (same pattern as internetpair.py): routes.py calls ``handle``, which
delegates straight to the bound Application — no dispatch-signature threading.

  GET /api/internetdelivery[?peer_id=X]
        Delivery view (ledger + offline queue merged):
        {"pending": N, "sends": [{"msg_id", "ts", "status", "preview",
          "content_hash", "kind", "session_id"}]}
        With ``peer_id`` the response is scoped to that peer; without it the
        rows are merged across all peers (capped at the 20 most recent).
        status ∈ sent / delivered / failed / queued.
        ``kind`` is "clipboard" for clipboard sends or the chat ``msg_type``
        (e.g. "chat_text") for relayed chat frames; ``session_id`` locates the
        chat session when kind is a chat type (empty for clipboard).

  GET /api/internetdelivery/counts
        Per-peer queued counts for device-page badges:
        {"peers": {"<peer_id>": N, ...}}

The WebSocket manager broadcasts ``internet_delivery`` {peer_id, msg_id,
status, content_hash, kind, session_id} from the host on every ledger
transition.

All handlers return (data_dict, status_code) and never raise.
"""

import logging

logger = logging.getLogger(__name__)

# Bound Application (set once by src/main.py at service creation).
_mgr = None


def bind(mgr) -> None:
    global _mgr
    _mgr = mgr


def _query_param(query_params, name: str) -> str:
    """Read a single-value query parameter, tolerating None / missing."""
    if not query_params:
        return ""
    vals = query_params.get(name)
    if not vals:
        return ""
    first = vals[0]
    return first if isinstance(first, str) else str(first)


def handle(method: str, path: str, query_params, body) -> tuple[dict, int]:
    """Entry point for the routes.py /api/internetdelivery branch."""
    if _mgr is None:
        return {"ok": False, "error": "internet delivery unavailable"}, 503
    try:
        if path == "/api/internetdelivery" and method == "GET":
            data = _mgr._delivery_status(_query_param(query_params, "peer_id"))
            return data, 200
        if path == "/api/internetdelivery/counts" and method == "GET":
            return _mgr._delivery_counts(), 200
        return {"ok": False, "error": "not found"}, 404
    except Exception:
        logger.exception("internetdelivery handler failed: %s %s", method, path)
        return {"ok": False, "error": "handler failed"}, 500
