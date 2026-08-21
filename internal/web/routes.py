"""Route dispatcher for the ClipSync web server.

Takes (method, path, query_params, body, ...) and returns
(status, content_type, body_bytes).
"""

import json
import logging
import os
import time

from internal.web.api.history import (
    get_history, get_history_item, push_text, delete_item, toggle_pin,
    increment_paste_count, batch_pin, batch_delete, batch_favorite,
    paste_rich,
)
from internal.web.api.devices import get_devices
from internal.web.api.transfer import get_transfers, post_transfer, get_speed_test
from internal.web.api.favorites import get_favorites, add_favorite, delete_favorite, update_favorite
from internal.web.api.settings import get_settings, update_settings
from internal.web.api.settings import (
    export_data, import_data,
    create_backup_api, restore_backup_api, list_backups_api,
)
from internal.web.api.translate import translate_text

logger = logging.getLogger(__name__)


def _json_response(data, status=200):
    """Pack a dict into (status, content_type, body_bytes)."""
    return status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8")


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
             enc_mgr=None, on_open_file=None, on_open_folder=None,
             on_restart=None, on_reset_dedup=None,
             get_certs=None, get_diagnostics=None,
             on_update_download=None,
             on_diagnostics_request=None):
    """Route an API request to the appropriate handler, never raising.

    Wraps _dispatch in a safety net so an unexpected exception in a handler
    (or a callback into the host app) returns a 500 instead of propagating
    out and killing the request thread.
    """
    try:
        return _dispatch(
            method, path, query_params, body, cfg, history, sync_mgr,
            get_connected_ids, on_nav_url, on_forward_file, upload_dir,
            dialog_mgr, get_overview_data, on_device_action, on_transfer_action,
            on_get_transfers, on_speed_test_start, on_speed_test_poll,
            on_window_close, on_toggle_discovery, on_toggle_visibility,
            on_settings_change, on_show_web_qr, on_send_url, get_discovered,
            get_resolved_hashes, get_pending_pairings, enc_mgr,
            on_open_file, on_open_folder, on_restart, on_reset_dedup,
            get_certs, get_diagnostics, on_update_download, on_diagnostics_request,
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
              enc_mgr=None, on_open_file=None, on_open_folder=None,
              on_restart=None, on_reset_dedup=None,
              get_certs=None, get_diagnostics=None,
              on_update_download=None,
              on_diagnostics_request=None):
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
            )
            return _json_response(data, status)

        elif path == "/api/history/item":
            data, status = get_history_item(query_params, history, cfg)
            return _json_response(data, status)

        elif path == "/api/status":
            return _json_response({"ok": True, "device": cfg.device_name})

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
            data, status = get_settings(cfg)
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
            lines_str = query_params.get("lines", ["200"])[0]
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
                    content = log_path.read_text(encoding="utf-8", errors="replace")
                    logs = content.splitlines()[-n:]
            except Exception:
                logger.exception("Failed to read log file for /api/logs")
                logs = []
            return _json_response({"logs": logs})

        elif path == "/api/diagnostics":
            if get_diagnostics is None:
                return _json_response({
                    "summary": "fail",
                    "checks": [
                        {"id": "server_port", "ok": False,
                         "detail": "diagnostics unavailable",
                         "guidance": "Diagnostics are unavailable on this build."},
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

    # ── POST routes ────────────────────────────────────────────────

    elif method == "POST":
        if path == "/api/push":
            data, status = push_text(body, cfg, sync_mgr, history)
            return _json_response(data, status)

        elif path == "/api/delete":
            data, status = delete_item(body, history)
            return _json_response(data, status)

        elif path == "/api/pin":
            data, status = toggle_pin(body, history)
            return _json_response(data, status)

        elif path == "/api/paste":
            data, status = increment_paste_count(body, history)
            return _json_response(data, status)

        elif path == "/api/paste-rich":
            data, status = paste_rich(body, history, on_reset_dedup)
            return _json_response(data, status)

        elif path == "/api/batch-pin":
            data, status = batch_pin(body, history)
            return _json_response(data, status)

        elif path == "/api/batch-delete":
            data, status = batch_delete(body, history)
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
                return _json_response({"ok": False, "path": "", "error": "download handler failed"}, 500)
            ok = bool(result.get("ok"))
            return _json_response({
                "ok": ok,
                "path": result.get("path", ""),
                "error": result.get("error"),
            }, 200 if ok else 500)

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

        elif path == "/api/history/clear":
            try:
                # Capture the count BEFORE clearing so the response reflects
                # how many items were actually removed (not the 0 remaining).
                count = len(history.get_all()) if hasattr(history, 'get_all') else 0
                history.clear()
            except Exception as e:
                return _json_response({"ok": False, "error": str(e)}, 500)
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

    # ── DELETE routes ──────────────────────────────────────────────

    elif method == "DELETE":
        if path == "/api/favorites":
            data, status = delete_favorite(body)
            return _json_response(data, status)

    # ── PATCH / PUT routes (for favorites update) ───────────────────

    elif method in ("PATCH", "PUT"):
        if path == "/api/favorites":
            data, status = update_favorite(body)
            return _json_response(data, status)

    return _json_response({"error": "not found"}, 404)
