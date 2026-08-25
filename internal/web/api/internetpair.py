"""Internet pairing-code REST backend (Round 14).

Routes (registered as one branch in routes.py; the Application is bound by
src/main.py at startup — no dispatch-signature threading needed):

  POST /api/internetpair/generate
        Generate a fresh pairing code for THIS device:
        {"ok": true, "code": "XXXX-XXXX-XXXX"}.  Requires internet sync on.

  POST /api/internetpair/enter   {"code": "XXXX-XXXX-XXXX"}
        Enter a code from another device:
        {"ok": true, "peer_id": "..."}  (400 on an invalid / typo'd code).

  GET  /api/internetpair/status
        {"generated_code": "..."?, "peers": [{"peer_id", "name", "alias",
          "online", "last_seen", "paired"}]}
        (Round 15: name = peer device name, alias = our memo, online = frame
         seen within the last 90s, last_seen = epoch seconds or null.)

  POST /api/internetpair/rename   {"peer_id", "name"}
        Set (or clear, when name is empty) this device's alias for a paired
        peer: {"ok": true}  (400 on unknown peer / invalid name).

  POST /api/internetpair/unpair   {"peer_id"}
        One-sided unpair: {"ok": true}  (400 on unknown peer).  Removes the
        secret + alias + state and unsubscribes the relay channel.

The WebSocket manager broadcasts ``netpair_peer`` {peer_id, name,
status: "paired"} from the host once a code handshake confirms identity.

All handlers return (data_dict, status_code) and never raise.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Bound Application (set once by src/main.py at service creation).
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
    """Entry point for the routes.py /api/internetpair branch."""
    if _mgr is None:
        return {"ok": False, "error": "internet pairing unavailable"}, 503
    try:
        if path == "/api/internetpair/generate" and method == "POST":
            return _mgr._netpair_generate()
        if path == "/api/internetpair/enter" and method == "POST":
            return _mgr._netpair_enter(_body_dict(body).get("code", ""))
        if path == "/api/internetpair/status" and method == "GET":
            return _mgr._netpair_status()
        if path == "/api/internetpair/rename" and method == "POST":
            b = _body_dict(body)
            return _mgr._netpair_rename(b.get("peer_id", ""), b.get("name"))
        if path == "/api/internetpair/unpair" and method == "POST":
            return _mgr._netpair_unpair(_body_dict(body).get("peer_id", ""))
        if path == "/api/internetpair/test" and method == "POST":
            return _mgr._netpair_test(_body_dict(body))
        return {"ok": False, "error": "not found"}, 404
    except Exception:
        logger.exception("internetpair handler failed: %s %s", method, path)
        return {"ok": False, "error": "handler failed"}, 500
