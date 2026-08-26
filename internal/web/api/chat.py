"""Nearby Chat API handlers.

All handlers return a (data_dict, status_code) tuple, matching the style of
the other ``internal/web/api/*.py`` modules.  Every handler guards on
``chat_mgr is None`` (webview-less hosts that never started a ChatManager)
and returns ``{error: "chat unavailable"}, 503`` in that case.

Handlers that need a per-peer transport send closure (text, file offers,
invite answers) receive a ``send_fn_for_peer`` callback from the host — the
web equivalent of the desktop's ``_chat_send_fn(peer_id)``.  The invite
endpoint receives the host's ``chat_start_session`` callback, which reuses
the desktop connect-first logic (see ``src/main.py:_chat_start_session``).
"""

import json
import logging

from internal.sync.nearby_chat import ChatFileTooLarge

logger = logging.getLogger(__name__)

_UNAVAILABLE = {"error": "chat unavailable"}


def _require_chat(chat_mgr):
    """Return an error tuple when *chat_mgr* is unavailable, else None."""
    if chat_mgr is None:
        return _UNAVAILABLE, 503
    return None


def _json_body(body):
    """Parse a JSON request body into a dict, or None when invalid."""
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _peer_for_session(chat_mgr, session_id: str) -> str | None:
    """Resolve the peer_id that owns *session_id* (for building a send_fn)."""
    try:
        for s in chat_mgr.get_sessions() or []:
            if s.get("session_id") == session_id:
                return s.get("peer_id")
    except Exception:
        logger.debug("chat: peer lookup for session failed", exc_info=True)
    return None


def _send_fn_for(chat_mgr, session_id: str, send_fn_for_peer):
    """Build the per-peer send closure for *session_id*, or None."""
    if send_fn_for_peer is None:
        return None
    peer_id = _peer_for_session(chat_mgr, session_id)
    if not peer_id:
        return None
    try:
        return send_fn_for_peer(peer_id)
    except Exception:
        logger.debug("chat: send_fn_for_peer callback failed", exc_info=True)
        return None


def _require_fields(data, *fields) -> str | None:
    """Return the name of the first missing/blank field, else None."""
    for field in fields:
        if not str(data.get(field) or "").strip():
            return field
    return None


# ── Reads ──────────────────────────────────────────────────────────

def get_chat_devices(get_devices_cb):
    """GET /api/chat/devices → {devices: [...]}.

    The host merges paired (always listed) with unpaired discovered peers,
    exactly like the desktop dashboard's ``get_chat_devices``.
    """
    if get_devices_cb is None:
        return _UNAVAILABLE, 503
    try:
        devices = get_devices_cb() or []
    except Exception:
        logger.debug("chat: get_chat_devices callback failed", exc_info=True)
        devices = []
    return {"devices": devices}, 200


def get_sessions(chat_mgr, get_chat_muted=None):
    """GET /api/chat/sessions → {sessions: [...], muted: [...]}.

    ``muted`` is the current per-peer chat mute set, so the web chat UI can
    render the bell state from the same request that lists the sessions.
    """
    err = _require_chat(chat_mgr)
    if err:
        return err
    try:
        resp = {"sessions": chat_mgr.get_sessions() or []}
        if get_chat_muted is not None:
            try:
                resp["muted"] = get_chat_muted() or []
            except Exception:
                resp["muted"] = []
        return resp, 200
    except Exception:
        logger.debug("chat: get_sessions failed", exc_info=True)
        return {"sessions": [], "muted": []}, 200


def get_messages(chat_mgr, query_params):
    """GET /api/chat/messages?session_id=X → {messages: [...]}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    session_id = (query_params.get("session_id", [""])[0] or "").strip()
    if not session_id:
        return {"error": "session_id required"}, 400
    try:
        return {"messages": chat_mgr.get_messages(session_id) or []}, 200
    except Exception:
        logger.debug("chat: get_messages failed", exc_info=True)
        return {"messages": []}, 200


def find_saved_path(chat_mgr, transfer_id: str) -> str | None:
    """Locate the saved_path of a DONE incoming file transfer by id.

    Used by the streaming download endpoint; returns None when the transfer
    is unknown, not yet finished, or chat is unavailable.
    """
    if chat_mgr is None:
        return None
    try:
        for session in chat_mgr.get_sessions() or []:
            for entry in chat_mgr.get_messages(session.get("session_id", "")) or []:
                if entry.get("transfer_id") == transfer_id and entry.get("saved_path"):
                    return entry.get("saved_path")
    except Exception:
        logger.debug("chat: find_saved_path failed", exc_info=True)
    return None


# ── Actions ────────────────────────────────────────────────────────

def invite(chat_mgr, body, chat_start_session):
    """POST /api/chat/invite {peer_id, peer_name} → {session_id}.

    When the peer is offline the host starts an async connect (worker thread);
    this returns ``{connecting: true, session_id: null}`` and a ``chat_sessions``
    push arrives once the invited session lands.
    """
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    peer_id = (data.get("peer_id") or "").strip()
    peer_name = (data.get("peer_name") or peer_id).strip()
    if not peer_id:
        return {"ok": False, "error": "peer_id required"}, 400
    if chat_start_session is None:
        return _UNAVAILABLE, 503
    try:
        result = chat_start_session(peer_id, peer_name)
    except Exception:
        logger.exception("chat: chat_start_session callback failed")
        return {"ok": False, "error": "start failed"}, 500
    # The web host returns a rich result so "still connecting" (poll the
    # upcoming chat_sessions push) is distinguishable from a refused invite.
    if isinstance(result, dict):
        if result.get("session_id"):
            return {"session_id": result["session_id"]}, 200
        if result.get("connecting"):
            return {"connecting": True, "session_id": None}, 200
        return {"ok": False, "error": result.get("error", "invite_failed")}, 400
    if result:
        return {"session_id": result}, 200
    # Legacy callbacks return bare None for both cases; assume connecting.
    return {"connecting": True, "session_id": None}, 200


def send_text(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/text {session_id, text} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id", "text")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    text = data.get("text") or ""
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        ok = chat_mgr.send_text(session_id, text, send_fn)
    except Exception:
        logger.debug("chat: send_text failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def set_typing(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/typing {session_id[, typing]} → {ok}.

    Typing-indicator report for the composer.  ``typing`` defaults to True
    (the endpoint means "the user is typing").  ``ok`` is False both when the
    frame was refused AND when the sender-side throttle suppressed a
    duplicate same-state frame within the 2s window — callers must treat
    False as "nothing sent", never as a user-visible error.
    """
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    typing = bool(data.get("typing", True))
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        ok = chat_mgr.report_typing(session_id, typing, send_fn)
    except Exception:
        logger.debug("chat: report_typing failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def resend_text(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/resend {session_id, entry_id} → {ok}.

    Re-send a FAILED outgoing chat text (the ⟳ button on a failed bubble).
    ``ChatManager.resend_text`` refuses anything that is not that session's
    own failed text (unknown id, incoming, already sent) and re-charges the
    per-session flood budget for the attempt.
    """
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id", "entry_id")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    entry_id = (data.get("entry_id") or "").strip()
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        ok = chat_mgr.resend_text(session_id, entry_id, send_fn)
    except Exception:
        logger.debug("chat: resend_text failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def send_file(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/file {session_id, file_path} → {transfer_id}.

    ``file_path`` is a path already on this device (the web UI uploads via
    /api/upload into a local temp dir and passes that path here).
    """
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id", "file_path")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    file_path = (data.get("file_path") or "").strip()
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        transfer_id = chat_mgr.send_file(session_id, file_path, send_fn)
    except ChatFileTooLarge:
        # The peer is internet-only and the file exceeds the relay cap; tell
        # the UI so it can show a specific message instead of a generic one.
        return {"ok": False, "error": "internet_file_cap"}, 400
    except Exception:
        logger.debug("chat: send_file failed", exc_info=True)
        transfer_id = None
    if not transfer_id:
        return {"ok": False, "error": "could not start file transfer"}, 400
    return {"transfer_id": transfer_id}, 200


def accept_file(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/file/accept {session_id, transfer_id} → {ok}.

    ``ChatManager.accept_file`` returns ``None`` when the offer is already
    gone (e.g. it expired under the stale-receive reaper while the UI still
    showed its Accept button).  Translate that into an explicit ``expired``
    error so the frontend can tell the user the offer lapsed instead of
    showing a generic failure — this was the widening accept-race window.
    """
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id", "transfer_id")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    transfer_id = (data.get("transfer_id") or "").strip()
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        result = chat_mgr.accept_file(session_id, transfer_id, send_fn)
    except Exception:
        logger.debug("chat: accept_file failed", exc_info=True)
        result = False
    if result is None:
        return {"ok": False, "error": "expired"}, 200
    return {"ok": bool(result)}, 200


def decline_file(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/file/decline {session_id, transfer_id} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id", "transfer_id")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    transfer_id = (data.get("transfer_id") or "").strip()
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        ok = chat_mgr.decline_file(session_id, transfer_id, send_fn)
    except Exception:
        logger.debug("chat: decline_file failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def cancel_file(chat_mgr, body):
    """POST /api/chat/file/cancel {session_id, transfer_id} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    missing = _require_fields(data, "session_id", "transfer_id")
    if missing:
        return {"ok": False, "error": f"{missing} required"}, 400
    session_id = (data.get("session_id") or "").strip()
    transfer_id = (data.get("transfer_id") or "").strip()
    try:
        ok = chat_mgr.cancel_file(session_id, transfer_id)
    except Exception:
        logger.debug("chat: cancel_file failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def accept_invite(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/accept {session_id} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}, 400
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        ok = chat_mgr.accept_invitation(session_id, send_fn)
    except Exception:
        logger.debug("chat: accept_invitation failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def decline_invite(chat_mgr, body, send_fn_for_peer):
    """POST /api/chat/decline {session_id} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}, 400
    send_fn = _send_fn_for(chat_mgr, session_id, send_fn_for_peer)
    try:
        ok = chat_mgr.decline_invitation(session_id, send_fn)
    except Exception:
        logger.debug("chat: decline_invitation failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def close_session(chat_mgr, body):
    """POST /api/chat/close {session_id} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}, 400
    try:
        ok = chat_mgr.close_session(session_id)
    except Exception:
        logger.debug("chat: close_session failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def mark_read(chat_mgr, body):
    """POST /api/chat/read {session_id} → {ok}."""
    err = _require_chat(chat_mgr)
    if err:
        return err
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}, 400
    try:
        chat_mgr.mark_session_read(session_id)
        ok = True
    except Exception:
        logger.debug("chat: mark_session_read failed", exc_info=True)
        ok = False
    return {"ok": ok}, 200


def set_muted(set_chat_muted, body):
    """POST /api/chat/mute {peer_id, muted} → {ok, muted: [...]}.

    ``muted`` is the full updated mute set after the change.  The host
    persists it and consults it before showing the desktop message
    notification, so a muted peer never rings.
    """
    if set_chat_muted is None:
        return _UNAVAILABLE, 503
    data = _json_body(body)
    if data is None:
        return {"ok": False, "error": "invalid json"}, 400
    peer_id = (data.get("peer_id") or "").strip()
    if not peer_id:
        return {"ok": False, "error": "peer_id required"}, 400
    muted = bool(data.get("muted"))
    try:
        muted_list = set_chat_muted(peer_id, muted) or []
    except Exception:
        logger.debug("chat: set_muted callback failed", exc_info=True)
        return {"ok": False, "error": "mute failed"}, 500
    return {"ok": True, "muted": muted_list}, 200
