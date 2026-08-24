"""Route dispatcher for the ClipSync web server.

Takes (method, path, query_params, body, ...) and returns
(status, content_type, body_bytes).
"""

import json
import logging
import os
import time

from internal.web.api import chat as _chat_api
from internal.web.api.devices import get_devices
from internal.web.api.favorites import (
    add_favorite,
    delete_favorite,
    export_favorites,
    get_favorites,
    update_favorite,
)
from internal.web.api.history import (
    batch_delete,
    batch_favorite,
    batch_pin,
    delete_item,
    get_history,
    get_history_item,
    increment_paste_count,
    paste_rich,
    push_text,
    toggle_pin,
)
from internal.web.api.settings import (
    create_backup_api,
    export_data,
    get_settings,
    import_data,
    list_backups_api,
    restore_backup_api,
    update_settings,
)
from internal.web.api.sync_control import pause_sync, resume_sync
from internal.web.api.transfer import get_speed_test, get_transfers, post_transfer
from internal.web.api.translate import translate_text

logger = logging.getLogger(__name__)

# NOTE: the host's Quick Paste close callback is threaded through ``dispatch``
# as the ``on_quickpaste_done`` parameter (like ``on_send_url`` / ``on_window_close``),
# NOT stored at module scope — a module-level registry survives server re-creation
# and would keep calling a stale bound method on a dead host instance.


def _chat_tmp_dir() -> str:
    """Return the directory purpose=chat web uploads land in (temp only).

    Chat file sends stage the file here instead of the received-files dir so
    the upload never surfaces as a received file / notification / sound.  The
    /api/chat/file route confines absolute paths against this same root, and
    the server's /api/upload purpose=chat branch writes into it.
    """
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "clipsync_chat_uploads")
    os.makedirs(d, exist_ok=True)
    return d


def _json_response(data, status=200):
    """Pack a dict into (status, content_type, body_bytes)."""
    return status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8")


def _ws_manager_for(dialog_mgr):
    """Resolve the WebSocket manager for broadcasting.

    ``dispatch`` receives the ``dialog_mgr`` (never the ws manager itself);
    DialogManager holds a reference to the WebSocketManager, so reach it
    through there.  Returns None when unavailable so the broadcast is a no-op.
    """
    if dialog_mgr is None:
        return None
    try:
        return dialog_mgr.ws_manager
    except Exception:
        return None


def _deleted_entry_ids(body):
    """Best-effort entry_ids a delete / batch-delete body targeted.

    The web UI always deletes by ``entry_id`` (api.js deleteItem/batchDelete),
    so those resolve directly.  The legacy index path cannot be resolved after
    the deletion has already shifted the list, so return [] and let the caller
    fall back to a full-history broadcast.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    eid = data.get("entry_id")
    if eid is not None:
        return [eid]
    ids = data.get("entry_ids")
    if isinstance(ids, list) and ids:
        return ids
    return []


def _broadcast_history_updated(dialog_mgr) -> None:
    """Broadcast the current full history snapshot to all WS clients."""
    mgr = _ws_manager_for(dialog_mgr)
    if mgr is None:
        return
    try:
        mgr.broadcast_history()
    except Exception:
        logger.debug("history broadcast failed", exc_info=True)


def _broadcast_history_deleted(dialog_mgr, entry_ids) -> None:
    """Broadcast a history_item_deleted event for the given entry_ids."""
    mgr = _ws_manager_for(dialog_mgr)
    if mgr is None:
        return
    try:
        mgr.broadcast_history_deleted(entry_ids)
    except Exception:
        logger.debug("history delete broadcast failed", exc_info=True)


def _broadcast_history_clear(dialog_mgr) -> None:
    """Broadcast a history_clear event (whole list wiped)."""
    mgr = _ws_manager_for(dialog_mgr)
    if mgr is None:
        return
    try:
        mgr.broadcast_history_clear()
    except Exception:
        logger.debug("history clear broadcast failed", exc_info=True)


def _redact_sensitive_line(line: str, cfg) -> str:
    """Strip locally-sensitive strings (user home, config dir, web token)
    from a log line before it is served to a web client.

    The raw log contains absolute user paths (e.g. ``C:\\Users\\<name>
    \\AppData\\Roaming\\ClipSync\\...``), stack traces and config values —
    useful reconnaissance that should not leave the device.
    """
    redact: list[str] = []
    home = os.path.expanduser("~")
    if home:
        redact.append(home)
    try:
        from internal.config.config import _config_dir
        config_dir = str(_config_dir())
        if config_dir and config_dir != home:
            redact.append(config_dir)
    except Exception:
        pass
    token = getattr(cfg, "web_token", "")
    if token:
        redact.append(token)
    for r in redact:
        if r:
            line = line.replace(r, "[redacted]")
    return line


def dispatch(method, path, query_params, body, cfg, history, sync_mgr,
             get_connected_ids, on_nav_url, on_forward_file, upload_dir,
             dialog_mgr=None,
             get_overview_data=None,
             on_device_action=None,
             on_transfer_action=None,
             on_get_transfers=None,
             on_speed_test_start=None,
             on_speed_test_poll=None,
             on_window_close=None,
             on_toggle_discovery=None,
             on_toggle_visibility=None,
             on_settings_change=None,
             on_show_web_qr=None, on_send_url=None,
             get_discovered=None,
             get_resolved_hashes=None, get_pending_pairings=None,
             get_reconnect_states=None,
             get_relay_state=None,
             enc_mgr=None, on_open_file=None, on_open_folder=None,
             on_restart=None, on_reset_dedup=None,
             get_certs=None, get_diagnostics=None,
             on_update_download=None,
             on_update_install=None,
             on_diagnostics_request=None,
             chat_mgr=None,
             get_chat_devices=None,
             chat_send_fn=None,
             chat_start_session=None,
             get_chat_muted=None,
             set_chat_muted=None,
             on_quickpaste_done=None):
    """Route an API request to the appropriate handler, never raising.

    Wraps _dispatch in a safety net so an unexpected exception in a handler
    (or a callback into the host app) returns a 500 instead of propagating
    out and killing the request thread.
    """
    # Reject JSON bodies that are valid JSON but not an object before any
    # handler runs: every handler calls ``data.get(...)`` immediately after
    # ``json.loads``, so a ``null`` / array / string / number body would
    # otherwise raise AttributeError and surface as a 500 + full traceback
    # (client-input problem, not a server error).
    if method in ("POST", "PUT", "PATCH") and body:
        stripped = body.strip()
        if stripped:
            try:
                parsed = json.loads(stripped.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass  # malformed JSON: let the handler produce its own 400
            else:
                if not isinstance(parsed, dict):
                    return _json_response(
                        {"ok": False, "error": "invalid json body: expected object"}, 400,
                    )
    try:
        return _dispatch(
            method, path, query_params, body, cfg, history, sync_mgr,
            get_connected_ids, on_nav_url, on_forward_file, upload_dir,
            dialog_mgr, get_overview_data, on_device_action, on_transfer_action,
            on_get_transfers, on_speed_test_start, on_speed_test_poll,
            on_window_close, on_toggle_discovery, on_toggle_visibility,
            on_settings_change, on_show_web_qr, on_send_url, get_discovered,
            get_resolved_hashes, get_pending_pairings, get_reconnect_states,
            get_relay_state,
            enc_mgr, on_open_file, on_open_folder, on_restart, on_reset_dedup,
            get_certs, get_diagnostics, on_update_download,
            on_update_install, on_diagnostics_request,
            chat_mgr, get_chat_devices, chat_send_fn, chat_start_session,
            get_chat_muted, set_chat_muted, on_quickpaste_done,
        )
    except Exception:
        logger.exception("Unhandled error in API route: %s %s", method, path)
        return _json_response({"ok": False, "error": "internal server error"}, 500)


def _dispatch(method, path, query_params, body, cfg, history, sync_mgr,
              get_connected_ids, on_nav_url, on_forward_file, upload_dir,
              dialog_mgr=None,
              get_overview_data=None,
              on_device_action=None,
              on_transfer_action=None,
              on_get_transfers=None,
              on_speed_test_start=None,
              on_speed_test_poll=None,
              on_window_close=None,
              on_toggle_discovery=None,
              on_toggle_visibility=None,
              on_settings_change=None,
              on_show_web_qr=None, on_send_url=None,
              get_discovered=None,
              get_resolved_hashes=None, get_pending_pairings=None,
              get_reconnect_states=None,
              get_relay_state=None,
              enc_mgr=None, on_open_file=None, on_open_folder=None,
              on_restart=None, on_reset_dedup=None,
              get_certs=None, get_diagnostics=None,
              on_update_download=None,
              on_update_install=None,
              on_diagnostics_request=None,
              chat_mgr=None,
              get_chat_devices=None,
              chat_send_fn=None,
              chat_start_session=None,
              get_chat_muted=None,
              set_chat_muted=None,
              on_quickpaste_done=None):
    """Route an API request to the appropriate handler.

    All handler functions return (data_dict, status_code).
    Returns (status, content_type, body_bytes).
    Returns (404, "application/json", ...) for unknown paths.
    """

    # ── GET routes ─────────────────────────────────────────────────

    if method == "GET":
        if path == "/api/history":
            limit_str = query_params.get("limit", [None])[0]
            offset_str = query_params.get("offset", [None])[0]
            data, status = get_history(history, cfg, limit_str, offset_str)
            return _json_response(data, status)

        elif path == "/api/devices":
            data, status = get_devices(
                cfg, get_connected_ids, get_discovered,
                get_resolved_hashes=get_resolved_hashes,
                get_pending_pairings=get_pending_pairings,
                get_reconnect_states=get_reconnect_states,
            )
            return _json_response(data, status)

        elif path == "/api/history/item":
            data, status = get_history_item(query_params, history, cfg)
            return _json_response(data, status)

        elif path == "/api/status":
            from internal.version import __version__
            return _json_response({
                "ok": True,
                "device": cfg.device_name,
                "version": __version__,
            })

        elif path == "/api/files":
            files = []
            try:
                for fname in sorted(os.listdir(upload_dir)):
                    fpath = os.path.join(upload_dir, fname)
                    if os.path.isfile(fpath):
                        st = os.stat(fpath)
                        size_kb = max(1, st.st_size // 1024)
                        mtime = time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)
                        )
                        files.append({
                            "name": fname,
                            "size": f"{size_kb} KB" if size_kb < 1024 else f"{size_kb // 1024:.1f} MB",
                            "time": mtime,
                        })
            except Exception:
                pass
            return _json_response({"files": files})

        elif path == "/api/download":
            # Handled directly in server.py for file streaming
            return _json_response({"error": "not found"}, 404)

        elif path == "/api/favorites":
            data, status = get_favorites()
            return _json_response(data, status)

        elif path == "/api/transfer":
            data, status = get_transfers(on_get_transfers)
            return _json_response(data, status)

        elif path == "/api/settings":
            data, status = get_settings(
                cfg, get_internet_sync_state=get_relay_state)
            return _json_response(data, status)

        elif path == "/api/backups":
            data, status = list_backups_api()
            return _json_response(data, status)

        elif path == "/api/overview":
            if get_overview_data is None:
                return _json_response({"error": "overview not available"}, 503)
            data = get_overview_data()
            return _json_response({"overview": data})

        elif path == "/api/devices/certs":
            if get_certs is None:
                return _json_response({"devices": []})
            try:
                devices = get_certs()
            except Exception:
                logger.exception("get_certs callback failed")
                devices = []
            return _json_response({"devices": devices or []})

        elif path == "/api/logs":
            lines_str = query_params.get("lines", [None])[0]
            if lines_str is None:
                # ?tail= is accepted as an alias for ?lines= — same semantics
                # (number of trailing log lines to return).
                lines_str = query_params.get("tail", ["200"])[0]
            try:
                n = int(lines_str)
            except (TypeError, ValueError):
                n = 200
            n = max(1, min(n, 1000))
            logs: list[str] = []
            try:
                from internal.config.config import _log_dir
                log_path = _log_dir() / "clipsync.log"
                if log_path.exists():
                    # Read only the tail (last 256 KB) so an oversized log is
                    # not fully loaded into memory.
                    try:
                        with open(log_path, "rb") as f:
                            f.seek(0, 2)  # SEEK_END
                            size = f.tell()
                            f.seek(max(0, size - 256 * 1024))
                            tail = f.read().decode("utf-8", errors="replace")
                    except Exception:
                        tail = ""
                    logs = [
                        _redact_sensitive_line(line, cfg)
                        for line in tail.splitlines()[-n:]
                    ]
            except Exception:
                logger.exception("Failed to read log file for /api/logs")
                logs = []
            return _json_response({"logs": logs})

        elif path == "/api/update/check":
            # Manual check for a newer release (the auto_update_check setting
            # only gates the silent periodic check, not this button).  Runs on
            # this request's worker thread (ThreadingHTTPServer), never raises.
            from internal.system.updater import check_for_update
            try:
                result = check_for_update(timeout=8.0)
            except Exception:
                logger.exception("GET /api/update/check failed")
                result = {}
            if not isinstance(result, dict):
                result = {}
            return _json_response({
                "available": bool(result.get("available")),
                "latest": result.get("latest", ""),
                "current": result.get("current", ""),
                "url": result.get("url", ""),
            }, 200)

        elif path == "/api/diagnostics":
            if get_diagnostics is None:
                return _json_response({
                    "summary": "fail",
                    "checks": [
                        {"id": "server_port", "ok": False,
                         "detail": "diagnostics unavailable",
                         "guidance": "Diagnostics are unavailable on this build.",
                         "detail_key": "diag.server_port.unavailable.detail",
                         "guidance_key": "diag.server_port.unavailable.guidance"},
                    ],
                    "discovery_running": False,
                    "server_running": False,
                    "connected_count": 0,
                    "paired_count": 0,
                    "web_companion_running": False,
                    "web_port": 0,
                    "lan_ip": "",
                    "os": "",
                    "version": "",
                })
            try:
                data = get_diagnostics()
            except Exception:
                logger.exception("get_diagnostics callback failed")
                data = {}
            return _json_response(data or {})

        elif path == "/api/speed-test":
            data, status = get_speed_test(on_speed_test_poll)
            return _json_response(data, status)

        elif path == "/api/chat/devices":
            data, status = _chat_api.get_chat_devices(get_chat_devices)
            return _json_response(data, status)

        elif path == "/api/chat/sessions":
            data, status = _chat_api.get_sessions(chat_mgr, get_chat_muted)
            return _json_response(data, status)

        elif path == "/api/chat/messages":
            data, status = _chat_api.get_messages(chat_mgr, query_params)
            return _json_response(data, status)

        elif path == "/api/chat/download":
            # Streaming file download handled directly in server.py; keep the
            # route here so a request that reaches dispatch (rather than being
            # intercepted for streaming) fails cleanly instead of 404ing oddly.
            return _json_response({"error": "not found"}, 404)

    # ── POST routes ────────────────────────────────────────────────

    elif method == "POST":
        if path == "/api/push":
            data, status = push_text(body, cfg, sync_mgr, history)
            return _json_response(data, status)

        elif path == "/api/delete":
            data, status = delete_item(body, history)
            # Broadcast the deletion so every client's list stays in sync —
            # the client-side history_updated merge can't express deletions.
            if data.get("ok"):
                ids = _deleted_entry_ids(body)
                if ids:
                    _broadcast_history_deleted(dialog_mgr, ids)
                else:
                    _broadcast_history_updated(dialog_mgr)
            return _json_response(data, status)

        elif path == "/api/pin":
            data, status = toggle_pin(body, history)
            # A pin toggles an existing row's pinned flag — a full-history
            # snapshot lets every client update it in place (upsert path).
            if data.get("ok"):
                _broadcast_history_updated(dialog_mgr)
            return _json_response(data, status)

        elif path == "/api/paste":
            data, status = increment_paste_count(body, history)
            return _json_response(data, status)

        elif path == "/api/paste-rich":
            data, status = paste_rich(body, history, on_reset_dedup)
            return _json_response(data, status)

        elif path == "/api/batch-pin":
            data, status = batch_pin(body, history)
            # Same sync requirement as single pin: refresh every client's
            # pinned flags from the authoritative snapshot.
            if data.get("ok"):
                _broadcast_history_updated(dialog_mgr)
            return _json_response(data, status)

        elif path == "/api/batch-delete":
            data, status = batch_delete(body, history)
            # Broadcast the removed entry_ids so every client drops them.
            if data.get("ok"):
                ids = _deleted_entry_ids(body)
                if ids:
                    _broadcast_history_deleted(dialog_mgr, ids)
                else:
                    _broadcast_history_updated(dialog_mgr)
            return _json_response(data, status)

        elif path == "/api/batch-favorite":
            data, status = batch_favorite(body, history)
            return _json_response(data, status)

        elif path == "/api/nav":
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            url = data.get("url", "").strip()
            if not url:
                return _json_response({"ok": False, "error": "empty url"}, 400)
            from internal.web.api.security import is_safe_nav_url
            if not is_safe_nav_url(url):
                return _json_response({"ok": False, "error": "only http/https URLs are allowed"}, 400)
            target_device = data.get("device_id", "")
            if target_device and target_device != cfg.device_id and on_nav_url:
                on_nav_url(url, target_device)
            else:
                import webbrowser
                webbrowser.open(url)
            logger.info("Web nav: %s -> %s", url[:80], target_device[:12] or "local")
            return _json_response({"ok": True})

        elif path == "/api/diagnostics/request":
            if on_diagnostics_request is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            action = req.get("action", "").strip()
            if not action:
                return _json_response({"ok": False, "error": "action required"}, 400)
            try:
                data = on_diagnostics_request(action) or {}
            except Exception:
                logger.exception("on_diagnostics_request callback failed")
                data = {"ok": False, "error": "request failed"}
            return _json_response(data)

        elif path == "/api/upload":
            # Handled directly in server.py due to multipart parsing
            return _json_response({"error": "not found"}, 404)

        elif path == "/api/favorites":
            data, status = add_favorite(body)
            return _json_response(data, status)

        elif path == "/api/favorites/export":
            # One-click export of ALL favorites as a Markdown / text file.
            data, status = export_favorites(body)
            return _json_response(data, status)

        elif path == "/api/transfer":
            data, status = post_transfer(body, cfg, on_forward_file)
            return _json_response(data, status)

        elif path == "/api/settings":
            data, status = update_settings(body, cfg, on_settings_change, enc_mgr)
            return _json_response(data, status)

        elif path == "/api/file/open":
            if on_open_file is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            file_path = req.get("path", "").strip()
            if not file_path:
                return _json_response({"ok": False, "error": "path required"}, 400)
            # Only allow opening files inside the received-files directory.
            # Resolve against upload_dir first so a bare filename (as sent by
            # the web UI) is confined correctly, while an absolute path that
            # points outside upload_dir is rejected.
            from internal.web.api.security import confine_path
            safe = confine_path(os.path.join(upload_dir, file_path), upload_dir)
            if safe is None:
                return _json_response(
                    {"ok": False, "error": "path must be inside the received-files directory"},
                    400,
                )
            on_open_file(str(safe))
            return _json_response({"ok": True})

        elif path == "/api/file/reveal":
            # Open the folder containing a received file (same confinement as
            # /api/file/open — the file lives in the upload directory).
            if on_open_folder is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            file_path = req.get("path", "").strip()
            if not file_path:
                return _json_response({"ok": False, "error": "path required"}, 400)
            from internal.web.api.security import confine_path
            safe = confine_path(os.path.join(upload_dir, file_path), upload_dir)
            if safe is None:
                return _json_response(
                    {"ok": False, "error": "path must be inside the received-files directory"},
                    400,
                )
            try:
                on_open_folder(str(safe))
            except Exception as exc:
                logger.error("Failed to reveal file folder: %s", exc)
                return _json_response({"ok": False, "error": str(exc)}, 500)
            return _json_response({"ok": True})

        elif path == "/api/restart":
            if on_restart is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            on_restart()
            return _json_response({"ok": True})

        elif path == "/api/update/download":
            if on_update_download is None:
                return _json_response({"ok": False, "path": "", "error": "not available"}, 503)
            try:
                result = on_update_download()
            except Exception:
                logger.exception("on_update_download callback failed")
                result = None
            if not isinstance(result, dict):
                # Only an unexpected handler failure is a genuine server error.
                return _json_response({"ok": False, "path": "", "error": "download handler failed"}, 500)
            ok = bool(result.get("ok"))
            # Expected failures (no asset for this platform, network failure,
            # verify failed) carry a readable error on HTTP 200 so the
            # frontend can surface the real reason instead of a generic 500.
            return _json_response({
                "ok": ok,
                "path": result.get("path", ""),
                "error": result.get("error"),
            }, 200)

        elif path == "/api/update/install":
            # Runs the full download→staging→apply→restart chain on the host
            # (the same path the tray uses).  The heavy work is marshalled to
            # the UI thread by the callback; this only accepts the request.
            if on_update_install is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                result = on_update_install()
            except Exception:
                logger.exception("on_update_install callback failed")
                result = None
            if not isinstance(result, dict):
                return _json_response(
                    {"ok": False, "error": "install handler failed"}, 500)
            return _json_response(result, 200)

        elif path == "/api/export":
            data, status = export_data(body, cfg, history)
            return _json_response(data, status)

        elif path == "/api/import":
            data, status = import_data(body, cfg, history)
            return _json_response(data, status)

        elif path == "/api/backup":
            data, status = create_backup_api(cfg, history)
            return _json_response(data, status)

        elif path == "/api/data/open-folder":
            # Open a well-known data folder on the host (never a client-supplied
            # path).  "data" = config/data dir, "backups" = its backups/ subdir.
            if on_open_folder is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                req = {}
            which = req.get("which", "data") if isinstance(req, dict) else "data"
            from internal.config.config import _config_dir
            folder = _config_dir() / "backups" if which == "backups" else _config_dir()
            try:
                folder.mkdir(parents=True, exist_ok=True)
                on_open_folder(str(folder))
            except Exception as exc:
                logger.error("Failed to open data folder: %s", exc)
                return _json_response({"ok": False, "error": str(exc)}, 500)
            return _json_response({"ok": True, "folder": str(folder)})

        elif path == "/api/restore":
            data, status = restore_backup_api(body, cfg, history)
            # A restored config can flip the app back to "fresh install"
            # (language_chosen=False in the backup).  __CLIPSYNC_FRESH__ is
            # only interpolated at page-serve time, so a dashboard that was
            # already open keeps its stale false and the onboarding wizard
            # never reappears until a manual refresh.  Tell live clients to
            # re-surface it — but ONLY when the config actually went fresh,
            # so an ordinary same-version restore doesn't pop the wizard.
            if (
                status == 200
                and isinstance(data, dict)
                and data.get("ok")
                and not getattr(cfg, "language_chosen", False)
            ):
                mgr = _ws_manager_for(dialog_mgr)
                if mgr is not None:
                    try:
                        mgr.broadcast("onboarding_required")
                    except Exception:
                        logger.debug("onboarding_required broadcast failed",
                                     exc_info=True)
            return _json_response(data, status)

        elif path == "/api/translate":
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            text = req.get("text", "").strip()
            if not text:
                return _json_response({"ok": False, "error": "no text provided"}, 400)
            target = req.get("target", "en")
            source = req.get("source", "auto")
            result = translate_text(text, target, source, cfg)
            status = 200 if result.get("ok") else 502
            return _json_response(result, status)

        elif path == "/api/speed-test":
            if on_speed_test_start is None:
                return _json_response({"error": "speed test not available"}, 503)
            ok = on_speed_test_start()
            return _json_response({"ok": ok})

        elif path == "/api/device/note":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            note = req.get("note", "")
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("edit_note", peer_id, note)
            return _json_response({"ok": ok})

        elif path == "/api/device/pair":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            code = req.get("code", "")
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("pair", peer_id, code)
            return _json_response({"ok": ok})

        elif path == "/api/device/reject":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("reject", peer_id)
            return _json_response({"ok": ok})

        elif path == "/api/device/unpair":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("unpair", peer_id)
            return _json_response({"ok": ok})

        elif path == "/api/device/connect":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("connect", peer_id)
            return _json_response({"ok": ok})

        elif path == "/api/device/disconnect":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("disconnect", peer_id)
            return _json_response({"ok": ok})

        elif path == "/api/device/forget":
            if on_device_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            peer_id = req.get("peer_id", "").strip()
            if not peer_id:
                return _json_response({"ok": False, "error": "peer_id required"}, 400)
            ok = on_device_action("forget", peer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/cancel":
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            transfer_id = req.get("transfer_id", "")
            if not transfer_id:
                return _json_response({"ok": False, "error": "transfer_id required"}, 400)
            ok = on_transfer_action("cancel", transfer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/pause":
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            transfer_id = req.get("transfer_id", "")
            if not transfer_id:
                return _json_response({"ok": False, "error": "transfer_id required"}, 400)
            ok = on_transfer_action("pause", transfer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/resume":
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            transfer_id = req.get("transfer_id", "")
            if not transfer_id:
                return _json_response({"ok": False, "error": "transfer_id required"}, 400)
            ok = on_transfer_action("resume", transfer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/accept":
            # Accept an incoming P2P file-transfer request from the web/mobile
            # UI (the desktop otherwise only offers a modal dialog).
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            transfer_id = req.get("transfer_id", "")
            if not transfer_id:
                return _json_response({"ok": False, "error": "transfer_id required"}, 400)
            ok = on_transfer_action("accept", transfer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/reject":
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            transfer_id = req.get("transfer_id", "")
            if not transfer_id:
                return _json_response({"ok": False, "error": "transfer_id required"}, 400)
            ok = on_transfer_action("reject", transfer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/retry":
            # Re-send a FAILED OUTGOING transfer from history to its original
            # peer (the transfers panel offers Retry on failed rows).  The
            # host's retry branch resolves the row by its original transfer_id.
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            transfer_id = req.get("transfer_id", "")
            if not transfer_id:
                return _json_response({"ok": False, "error": "transfer_id required"}, 400)
            ok = on_transfer_action("retry", transfer_id)
            return _json_response({"ok": ok})

        elif path == "/api/transfer/cancel-all":
            # Cancel every ACTIVE transfer at once (the transfers panel's
            # "Cancel all" button).  The live list comes from the host's
            # on_get_transfers callback; each row is then cancelled through
            # the SAME per-transfer action a single-row ✕ uses, so peer
            # notification, history rows and once-guarded callbacks are all
            # identical to an individual cancel.
            if on_transfer_action is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            ids: list = []
            if on_get_transfers is not None:
                try:
                    active, _history = on_get_transfers()
                    ids = [
                        t.get("transfer_id", "")
                        for t in (active or [])
                        if isinstance(t, dict)
                    ]
                except Exception:
                    logger.exception("Failed to read transfer state for cancel-all")
                    ids = []
            cancelled = sum(
                1 for tid in ids if tid and on_transfer_action("cancel", tid)
            )
            return _json_response({"ok": True, "cancelled": cancelled})

        elif path == "/api/history/clear":
            try:
                # Capture the count BEFORE clearing so the response reflects
                # how many items were actually removed (not the 0 remaining).
                count = len(history.get_all()) if hasattr(history, 'get_all') else 0
                history.clear()
            except Exception as e:
                return _json_response({"ok": False, "error": str(e)}, 500)
            # Tell every client to wipe its local list too — otherwise only
            # the requesting tab empties and the others keep stale entries.
            _broadcast_history_clear(dialog_mgr)
            return _json_response({"ok": True, "count": count})

        elif path == "/api/window":
            # Window control actions for frameless title bar
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            action = req.get("action", "")
            if action == "close":
                if on_window_close:
                    on_window_close()
                return _json_response({"ok": True})
            return _json_response({"ok": False, "error": f"unknown action: {action}"}, 400)

        elif path == "/api/discovery/toggle":
            if on_toggle_discovery is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            enabled = req.get("enabled", False)
            on_toggle_discovery(enabled)
            return _json_response({"ok": True, "enabled": enabled})

        elif path == "/api/visibility/toggle":
            if on_toggle_visibility is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            enabled = req.get("enabled", False)
            on_toggle_visibility(enabled)
            return _json_response({"ok": True, "enabled": enabled})

        elif path == "/api/sync/pause":
            # Timed pause (web parity with the tray's 15/30/60-min menu):
            # disables sync now and auto-resumes after N minutes.  The timer
            # and its stale-fire guard live in api/sync_control.py; the
            # deadline is mirrored into cfg.timed_pause_until so a restart or
            # auto-update relaunch mid-pause re-arms it host-side.
            data, status = pause_sync(body, cfg, on_settings_change)
            return _json_response(data, status)

        elif path == "/api/sync/resume":
            data, status = resume_sync(body, cfg, on_settings_change)
            return _json_response(data, status)

        elif path == "/api/show_qr":
            if on_show_web_qr is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            on_show_web_qr()
            return _json_response({"ok": True})

        elif path == "/api/send_url":
            if on_send_url is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            on_send_url()
            return _json_response({"ok": True})

        elif path == "/api/quickpaste/done":
            # The Quick Paste popup reports that it finished (paste succeeded,
            # or the 60s abandonment safety net fired).  Ask the host to tear
            # down exactly the --app instance this popup belongs to — the body
            # carries its ``instance`` id, so a stale/abandoned popup can never
            # close a newer instance.  This is the real close mechanism for the
            # popup, since window.close() is blocked in a plain tab.  Token-gated
            # by the server's /api/* POST auth gate like every other route here.
            handler = on_quickpaste_done
            if handler is None:
                return _json_response({"ok": False, "error": "not available"}, 503)
            instance_id = None
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                if isinstance(payload, dict):
                    instance_id = payload.get("instance")
            except (json.JSONDecodeError, UnicodeDecodeError):
                instance_id = None
            # The host keys _quickpaste_instances by int; the page echoes its
            # id back as a JSON number, but an int-like string ("7") is equally
            # valid on the wire.  Anything else (bool, float, object, garbage
            # string) is a malformed request — reject with 400 so a bad id can
            # never reach the host as a confusing value.  A missing instance —
            # and an EMPTY-STRING instance, a legacy client's way of omitting
            # the id — is allowed through: the host no-ops on it.
            if instance_id == "":
                instance_id = None
            if instance_id is not None:
                if isinstance(instance_id, bool) or not isinstance(instance_id, (int, str)):
                    return _json_response({"ok": False, "error": "invalid instance"}, 400)
                if isinstance(instance_id, str):
                    try:
                        instance_id = int(instance_id)
                    except ValueError:
                        return _json_response({"ok": False, "error": "invalid instance"}, 400)
            try:
                handler(instance_id)
            except Exception:
                logger.exception("quickpaste done handler failed")
                return _json_response({"ok": False, "error": "handler failed"}, 500)
            return _json_response({"ok": True})

        elif path == "/api/dialog-response":
            if dialog_mgr is None:
                return _json_response({"ok": False, "error": "dialog manager not available"}, 503)
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            dialog_id = req.get("dialog_id", "").strip()
            action = req.get("action", "").strip()
            value = req.get("value")
            if not dialog_id or not action:
                return _json_response({"ok": False, "error": "dialog_id and action required"}, 400)
            ok = dialog_mgr.handle_response(dialog_id, action, value)
            return _json_response({"ok": ok})

        elif path == "/api/chat/invite":
            data, status = _chat_api.invite(chat_mgr, body, chat_start_session)
            return _json_response(data, status)

        elif path == "/api/chat/text":
            data, status = _chat_api.send_text(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/typing":
            data, status = _chat_api.set_typing(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/resend":
            data, status = _chat_api.resend_text(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/file":
            # The web UI uploads via /api/upload, which returns a bare
            # filename; resolve it against the received-files dir (and reject
            # any traversal) exactly like /api/file/open does.
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                req = None
            chat_staging_path = None
            if isinstance(req, dict):
                fpath = str(req.get("file_path") or "").strip()
                if fpath:
                    from internal.web.api.security import confine_path
                    if os.path.isabs(fpath):
                        # purpose=chat uploads pass an absolute temp path that
                        # lives in the dedicated chat temp dir.
                        safe = confine_path(fpath, _chat_tmp_dir())
                        chat_staging_path = str(safe) if safe is not None else None
                    else:
                        # Bare filename (legacy /api/upload flow) — resolve it
                        # against the received-files dir. When the receive dir
                        # is user-configured, resolve it at request time (the
                        # server's captured upload_dir goes stale after a live
                        # setting change). When unconfigured the default can't
                        # change, so honor the captured root.
                        configured = getattr(cfg, "file_receive_dir", "") or ""
                        base_dir = os.path.expanduser(configured) if configured else upload_dir
                        safe = confine_path(os.path.join(base_dir, fpath), base_dir)
                    if safe is None:
                        return _json_response(
                            {"ok": False, "error": "path must be inside the allowed upload directory"},
                            400,
                        )
                    req["file_path"] = str(safe)
                    body = json.dumps(req).encode("utf-8")
            data, status = _chat_api.send_file(chat_mgr, body, chat_send_fn)
            # A purpose=chat upload is staged in the temp dir; if the send
            # could not be started, remove the staging copy so a failed chat
            # send leaves no orphaned file.
            if chat_staging_path and not data.get("transfer_id"):
                try:
                    os.unlink(chat_staging_path)
                except OSError:
                    pass
            return _json_response(data, status)

        elif path == "/api/chat/file/accept":
            data, status = _chat_api.accept_file(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/file/decline":
            data, status = _chat_api.decline_file(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/file/cancel":
            data, status = _chat_api.cancel_file(chat_mgr, body)
            return _json_response(data, status)

        elif path == "/api/chat/accept":
            data, status = _chat_api.accept_invite(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/decline":
            data, status = _chat_api.decline_invite(chat_mgr, body, chat_send_fn)
            return _json_response(data, status)

        elif path == "/api/chat/close":
            data, status = _chat_api.close_session(chat_mgr, body)
            return _json_response(data, status)

        elif path == "/api/chat/read":
            data, status = _chat_api.mark_read(chat_mgr, body)
            return _json_response(data, status)

        elif path == "/api/chat/mute":
            data, status = _chat_api.set_muted(set_chat_muted, body)
            return _json_response(data, status)

    # ── DELETE routes ──────────────────────────────────────────────

    elif method == "DELETE":
        if path == "/api/favorites":
            data, status = delete_favorite(body)
            return _json_response(data, status)

        elif path == "/api/files":
            # Delete a phone-uploaded file from the received-files directory.
            # The web UI sends the bare filename it got back from GET
            # /api/files; basename strips any directory components and
            # confine_path resolves + re-validates so traversal or symlinks can
            # never target a file outside the upload dir.
            try:
                req = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response({"ok": False, "error": "invalid json"}, 400)
            # Do NOT strip the name: GET /api/files returns the raw directory
            # entry, so a file whose name has leading/trailing spaces (legal on
            # macOS/Linux) must be deleted by that exact name — stripping would
            # either fail to match it or delete a different file.  basename +
            # confine_path still neutralize any directory components.
            fname = str(req.get("name") or "") if isinstance(req, dict) else ""
            if not fname:
                return _json_response({"ok": False, "error": "filename required"}, 400)
            from internal.web.api.security import confine_path
            safe = confine_path(
                os.path.join(upload_dir, os.path.basename(fname)), upload_dir,
            )
            if safe is None:
                return _json_response(
                    {"ok": False, "error": "path must be inside the received-files directory"},
                    400,
                )
            try:
                if os.path.isdir(safe):
                    return _json_response({"ok": False, "error": "is a directory"}, 400)
                os.unlink(safe)
            except FileNotFoundError:
                return _json_response({"ok": False, "error": "file not found"}, 404)
            except OSError as exc:
                logger.warning("Failed to delete uploaded file %s: %s", safe, exc)
                return _json_response({"ok": False, "error": "delete failed"}, 500)
            return _json_response({"ok": True, "name": os.path.basename(str(safe))})

    # ── PATCH / PUT routes (for favorites update) ───────────────────

    elif method in ("PATCH", "PUT"):
        if path == "/api/favorites":
            data, status = update_favorite(body)
            return _json_response(data, status)

    return _json_response({"error": "not found"}, 404)
