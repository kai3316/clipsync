"""Strict IPC framing and dispatch. stdout belongs exclusively to this adapter."""

import json
import logging
import math
import os
import queue
import threading
from collections import OrderedDict
from typing import BinaryIO

from internal.adapters.sidecar.favorites import METHODS as FAVORITES_METHODS
from internal.adapters.sidecar.favorites import dispatch_favorites, valid_entry_ids
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.application.use_cases.history import KINDS, SORTS

logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 1024 * 1024
PROTOCOL_VERSION = 1
EVENT_BATCH_SIZE = 16
INPUT_POLL_SECONDS = 0.05


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("Non-finite number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite number")
    return number


def decode_frame(raw: bytes) -> dict:
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("Frame too large")
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("Frame must be an object")
    return value


def encode_frame(value: dict) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ApplicationError("RESPONSE_TOO_LARGE", "Narrow the requested result")
    return encoded + b"\n"


def validate_result(result: object, session_id: str) -> None:
    """Refuse a result the host would read as an invalid frame.

    A successful response is read against a fixed profile: an object, whose
    top-level `session_id` is *this* sidecar session — not anything a command
    happens to be about, since the host compares it against the session it
    started so a reply from an earlier process cannot be mistaken for one from
    this one — and whose top-level `seq` is the host's own event cursor.  The
    host enforces the profile in `desktop/src-tauri/src/protocol.rs`, and it
    treats a violation as fatal: it kills the sidecar and restarts it rather
    than failing the one command, so the mistake surfaces only as a restart
    loop naming no method.  Catching it here instead costs one error the caller
    can read.

    A command whose natural answer is a list or a scalar wraps it, because the
    profile is what makes a result extensible — a field can be added to an
    object without a reader having to learn a new shape.
    """
    if not isinstance(result, dict):
        raise ApplicationError("INVALID_RESULT", "A result must be an object")
    claimed = result.get("session_id")
    if claimed is not None and claimed != session_id:
        raise ApplicationError(
            "INVALID_RESULT", "session_id is reserved for the transport session"
        )
    cursor = result.get("seq")
    if cursor is not None and (
        type(cursor) is not int or not 0 <= cursor <= 0xFFFFFFFFFFFFFFFF
    ):
        raise ApplicationError(
            "INVALID_RESULT", "seq is reserved for the event cursor"
        )


def validate_params(params: dict, fields: dict[str, tuple], required: tuple = ()) -> None:
    if params.keys() - fields.keys() or any(key not in params for key in required):
        raise ApplicationError("VALIDATION_ERROR", "Unexpected or missing parameter")
    for key, value in params.items():
        kind, constraint = fields[key]
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if type(value) not in kinds or not constraint(value):
            raise ApplicationError("VALIDATION_ERROR", f"Invalid parameter: {key}")


def _send_paths(value: list) -> bool:
    """The paths one `transfers.send` carries: at least one, each short enough
    to be a path and non-empty.

    A list rather than a single path because the file picker is multi-select —
    several picks become one archive and one transfer — and the bound is on the
    count as well as on each entry, so a malformed frame cannot ask the runtime
    to stat a thousand paths.
    """
    return (
        0 < len(value) <= 64
        and all(isinstance(item, str) and 0 < len(item) <= 4096 for item in value)
    )


def _broker_list(value: list) -> bool:
    """A relay broker list: a few URLs, each short enough to be one.

    Named once because the public and the private relay list take the same
    shape, and a limit written twice is a limit that drifts.
    """
    return len(value) <= 16 and all(
        isinstance(item, str) and 0 < len(item) <= 2048 for item in value
    )


class Dispatcher:
    def __init__(self, app: SidecarApplication):
        self.app = app
        self.shutdown_requested = False

    def call(self, method: str, params: dict) -> dict:
        if method == "app.status":
            validate_params(params, {})
            return self.app.events.snapshot(self.app.status)
        if method == "app.shutdown":
            validate_params(params, {})
            self.shutdown_requested = True
            return {"accepted": True}
        if method == "app.unlock":
            validate_params(
                params, {"password": (str, lambda v: 0 < len(v) <= 1024)}, ("password",)
            )
            return self.app.unlock(params["password"])
        if method == "app.factory_reset":
            validate_params(params, {})
            return self.app.factory_reset()
        if method == "devices.list":
            validate_params(params, {})
            return self.app.events.snapshot(self.app.devices)
        if method == "companion.status":
            validate_params(params, {})
            return self.app.companion_status()
        if method == "companion.configure":
            validate_params(params, {
                "enabled": (bool, lambda _: True),
                "port": (int, lambda value: 1 <= value <= 65535),
                "rotate_token": (bool, lambda _: True),
            }, ("enabled",))
            return self.app.configure_companion(**params)
        if method == "devices.note":
            validate_params(params, {
                "device_id": (str, lambda v: 0 < len(v) <= 128),
                "note": (str, lambda v: len(v) <= 512),
            }, ("device_id", "note"))
            return self.app.require_runtime().set_device_note(params["device_id"], params["note"])
        device_commands = {
            "devices.connect": "connect_device",
            "devices.disconnect": "disconnect_device",
            "devices.forget": "forget_device",
            "devices.restore": "restore_device",
            "devices.purge": "purge_device",
            "devices.test": "test_device",
            # The answer to a certificate-change prompt.  Its "keep unpaired"
            # half is the existing ``pairing.unpair``: unpairing is what that
            # answer means, and the runtime voids the prompt with it.
            "devices.retrust": "retrust_device",
        }
        if method in device_commands:
            validate_params(
                params, {"device_id": (str, lambda v: 0 < len(v) <= 128)}, ("device_id",)
            )
            return getattr(self.app.require_runtime(), device_commands[method])(
                params["device_id"]
            )
        if method == "devices.certs":
            validate_params(params, {})
            return self.app.require_runtime().certs()
        if method == "url.send":
            validate_params(params, {
                "device_id": (str, lambda v: 0 < len(v) <= 128),
                "url": (str, lambda v: 0 < len(v) <= 2048),
            }, ("device_id", "url"))
            return self.app.require_runtime().send_url(
                params["device_id"], params["url"]
            )
        if method == "clipboard.push":
            validate_params(params, {
                "text": (str, lambda v: 0 < len(v) <= 100000),
            }, ("text",))
            return self.app.require_runtime().push_text(params["text"])
        if method == "discovery.status":
            validate_params(params, {})
            return self.app.require_runtime().discovery_state()
        if method in ("discovery.set_enabled", "discovery.set_visible"):
            validate_params(params, {"enabled": (bool, lambda _: True)}, ("enabled",))
            target = (
                self.app.require_runtime().set_discovery_enabled
                if method == "discovery.set_enabled"
                else self.app.require_runtime().set_discovery_visible
            )
            return target(params["enabled"])
        if method == "internet_pairing.status":
            validate_params(params, {})
            return self.app.require_internet_pairing().status()
        if method == "internet_pairing.generate":
            validate_params(params, {})
            return self.app.require_internet_pairing().generate()
        if method == "internet_pairing.enter":
            validate_params(params, {"code": (str, lambda v: 1 <= len(v) <= 64)}, ("code",))
            return self.app.require_internet_pairing().enter(params["code"])
        if method == "internet_pairing.rename":
            validate_params(params, {
                "peer_id": (str, lambda v: 0 < len(v) <= 128),
                "name": (str, lambda v: len(v) <= 120),
            }, ("peer_id", "name"))
            return self.app.require_internet_pairing().rename(
                params["peer_id"], params["name"]
            )
        if method == "internet_pairing.unpair":
            validate_params(params, {"peer_id": (str, lambda v: 0 < len(v) <= 128)}, ("peer_id",))
            return self.app.require_internet_pairing().unpair(params["peer_id"])
        if method == "relay.delivery_status":
            validate_params(params, {"peer_id": (str, lambda v: len(v) <= 128)})
            return self.app.require_runtime().relay_delivery_status(params.get("peer_id", ""))
        if method == "ai.inventory":
            validate_params(params, {
                "refresh": (bool, lambda _: True),
                "peer_id": (str, lambda v: len(v) <= 128),
            })
            return self.app.ai_inventory(params.get("refresh", False), params.get("peer_id", ""))
        if method == "ai.preview":
            validate_params(params, {
                "peer_id": (str, lambda v: 0 < len(v) <= 128),
                "tool": (str, lambda v: 0 < len(v) <= 128),
                "rel_path": (str, lambda v: 0 < len(v) <= 4096),
                "root": (str, lambda v: len(v) <= 256),
            }, ("peer_id", "tool", "rel_path"))
            return self.app.ai_preview(params)
        if method == "ai.pull":
            validate_params(params, {
                "peer_id": (str, lambda v: 0 < len(v) <= 128),
                "items": (list, lambda v: 0 < len(v) <= 100),
                "mode": (str, lambda v: v in ("copy", "overwrite", "append")),
                "batch_id": (str, lambda v: len(v) <= 128),
            }, ("peer_id", "items"))
            return self.app.ai_pull(params)
        if method.startswith("ai.local."):
            action = method[9:]
            if action not in ("listing", "read", "save", "trash", "open"):
                raise ApplicationError("INVALID_ARGUMENT", "Unknown AI config action")
            if action == "listing":
                validate_params(params, {})
            else:
                validate_params(params, {
                    "tool": (str, lambda v: 0 < len(v) <= 128),
                    "rel_path": (str, lambda v: 0 < len(v) <= 4096),
                    "root": (str, lambda v: len(v) <= 256),
                    "content": (str, lambda v: len(v) <= 262144),
                }, ("tool", "rel_path", "content") if action == "save" else
                   (("tool", "rel_path") if action != "listing" else ()))
            return self.app.ai_local(action, params)
        if method == "logs.tail":
            validate_params(params, {"lines": (int, lambda v: 1 <= v <= 1000)})
            return self.app.read_logs(params.get("lines", 200))
        if method == "logs.export":
            # The destination comes from the host's own save dialog, never from
            # the WebView: the sidecar copies its log there and nothing else.
            validate_params(
                params,
                {"dest": (str, lambda v: 0 < len(v) <= 4096)},
                ("dest",),
            )
            return self.app.export_logs(params["dest"])
        if method == "companion.qr":
            validate_params(params, {})
            return self.app.companion_qr()
        if method == "app.open_link":
            # The WebView names a target, never a URL; the table lives in the
            # sidecar so only the app's own links can ever be opened.
            from internal.system.about import LINKS

            validate_params(
                params,
                {"target": (str, lambda v: v in LINKS)},
                ("target",),
            )
            return self.app.open_about_link(params["target"])
        if method == "diagnostics.report":
            validate_params(params, {})
            return self.app.diagnostics()
        if method == "diagnostics.request":
            validate_params(params, {
                "action": (str, lambda v: v in ("firewall", "local_network")),
            }, ("action",))
            return self.app.diagnostics_request(params["action"])
        if method == "update.check":
            validate_params(params, {})
            return self.app.update_check()
        if method == "update.status":
            validate_params(params, {})
            return self.app.update_status()
        if method == "update.download":
            validate_params(params, {})
            return self.app.update_download()
        if method == "update.open_folder":
            validate_params(params, {})
            return self.app.update_open_folder()
        if method == "data.open_folder":
            validate_params(
                params,
                {"which": (str, lambda v: v in ("data", "backups"))},
                ("which",),
            )
            return self.app.open_data_folder(params["which"])
        if method == "settings.get":
            validate_params(params, {})
            return self.app.settings()
        if method == "settings.update":
            fields = {
                "device_name": (str, lambda v: 0 < len(v) <= 128),
                "appearance_mode": (str, lambda v: v in ("system", "light", "dark")),
                "language": (str, lambda v: 0 < len(v) <= 32),
                "plain_text_only": (bool, lambda _: True),
                "notifications_enabled": (bool, lambda _: True),
                # The three per-type switches the legacy panel had.  The host
                # already reads the first two (notifications.rs gates its own
                # OS notifications on them), so accepting them is what makes a
                # switch the user can read actually a switch the user can set.
                # notify_sync is deliberately absent: nothing native reads it,
                # and a control that changes nothing is worse than no control.
                "notify_transfer": (bool, lambda _: True),
                "notify_pairing": (bool, lambda _: True),
                "notify_device_connect": (bool, lambda _: True),
                "app_filter_enabled": (bool, lambda _: True),
                "app_filter_mode": (str, lambda v: v in ("blacklist", "whitelist")),
                "app_filter_list": (list, lambda v: len(v) <= 256 and all(
                    isinstance(item, str) and 0 < len(item) <= 260
                    and item == item.strip() and not any(c in item for c in "\0\r\n")
                    for item in v
                )),
                "filter_enabled_categories": (list, lambda v: len(v) <= 6 and all(
                    isinstance(item, str) and item in {
                        "credit_card", "ssn", "api_key", "email", "private_key", "password",
                    } for item in v
                )),
                "set_translate_key": (str, lambda v: len(v) <= 4096),
                "clear_translate_key": (bool, lambda v: v is True),
                # Security.  The password is strength-checked by the HTTP layer
                # (single source of truth, so the same rules apply to the web
                # panel); here it is only bounded.  Empty means "unchanged" —
                # clear_password removes it.
                "encryption_enabled": (bool, lambda _: True),
                "password": (str, lambda v: len(v) <= 1024),
                "clear_password": (bool, lambda v: v is True),
                "history_max_entries": (int, lambda v: 10 <= v <= 10000),
                "history_max_age_days": ((int, float), lambda v: 0 <= v <= 36500),
                # Advanced and network fields the legacy web panel exposes.
                # Bounds mirror config._FIELD_RANGES and the HTTP API's
                # _RANGE_LIMITS (port keeps the API's privileged-port cut).
                # Most are read when the LAN runtime is built, so they apply
                # on restart — the shell says so next to the control.
                "port": (int, lambda v: 1024 <= v <= 65535),
                "service_type": (str, lambda v: 0 < len(v) <= 128 and v == v.strip()
                                 and not any(c in v for c in "\0\r\n")),
                "web_history_limit": (int, lambda v: 1 <= v <= 500),
                "sync_debounce": ((int, float), lambda v: 0.05 <= v <= 10.0),
                "clipboard_poll_interval": ((int, float), lambda v: 0.1 <= v <= 60.0),
                "file_receive_dir": (str, lambda v: len(v) <= 4096),
                "transfer_timeout": ((int, float), lambda v: 5 <= v <= 3600),
                "max_reconnect_attempts": (int, lambda v: 0 <= v <= 100),
                "log_level": (str, lambda v: v in ("DEBUG", "INFO", "WARNING", "ERROR")),
                "low_memory_mode": (bool, lambda _: True),
                "retry_capture_enabled": (bool, lambda _: True),
                "dedup_method": (str, lambda v: v in ("sha256", "simple")),
                "data_dir": (str, lambda v: len(v) <= 4096),
                "translate_url": (str, lambda v: len(v) <= 2048 and (
                    not v or v.startswith(("https://", "http://"))
                )),
                "auto_start": (bool, lambda _: True),
                "auto_update_check": (bool, lambda _: True),
                "sound_enabled": (bool, lambda _: True),
                "ui_animation_enabled": (bool, lambda _: True),
                "source_tracking_enabled": (bool, lambda _: True),
                "paste_to_top": (bool, lambda _: True),
                "internet_sync_enabled": (bool, lambda _: True),
                "relay_brokers": (list, _broker_list),
                "relay_private_brokers": (list, _broker_list),
                "relay_username": (str, lambda v: len(v) <= 256),
                "relay_password": (str, lambda v: len(v) <= 1024),
            }
            validate_params(params, fields)
            return self.app.update_settings(params)
        if method == "translate.text":
            validate_params(params, {
                "text": (str, lambda v: 0 < len(v) <= 5000),
                "target_lang": (str, lambda v: 0 < len(v) <= 16),
                "source_lang": (str, lambda v: 0 < len(v) <= 16),
            }, ("text",))
            return self.app.translate_text(
                params["text"], params.get("target_lang", "en"), params.get("source_lang", "auto")
            )
        if method == "ai.profiles":
            validate_params(params, {})
            return self.app.ai_profiles()
        if method == "ai.profiles.update":
            validate_params(params, {
                "tools": (list, lambda v: len(v) <= 32),
                "custom_paths": (list, lambda v: len(v) <= 32),
            })
            return self.app.update_ai_profiles(
                params.get("tools", []), params.get("custom_paths", [])
            )
        if method == "transfers.list":
            validate_params(params, {})
            return self.app.require_runtime().transfers()
        if method == "transfers.speed_test":
            validate_params(params, {})
            return {"test_id": self.app.require_runtime().start_speed_test() or ""}
        if method == "transfers.send":
            validate_params(
                params,
                {
                    "paths": (list, _send_paths),
                    "device_id": (str, lambda v: len(v) <= 128),
                },
                ("paths",),
            )
            transfer_id = self.app.require_runtime().send_files(
                params["paths"], params.get("device_id", "")
            )
            return {"transfer_id": transfer_id}
        if method == "transfers.action":
            fields = {
                "action": (
                    str,
                    lambda v: v in ("cancel", "pause", "resume", "accept", "reject",
                                    "delete", "retry", "open", "reveal"),
                ),
                "transfer_id": (str, lambda v: 0 < len(v) <= 128),
            }
            validate_params(params, fields, ("action", "transfer_id"))
            result = self.app.require_runtime().transfer_action(
                params["action"], params["transfer_id"]
            )
            self.app.events.publish("transfers.changed", {"transfer_id": params["transfer_id"]})
            return {"ok": True, "result": result}
        if method == "transfers.cancel_all":
            validate_params(params, {})
            cancelled = self.app.require_runtime().cancel_all_transfers()
            # One event, not one per row: the window re-reads the whole list, and
            # a row it never saw (created after its last refresh) is covered by
            # the same publish.
            self.app.events.publish("transfers.changed", {"cancelled": cancelled})
            return {"cancelled": cancelled}
        if method == "transfers.clear_history":
            validate_params(params, {})
            cleared = self.app.require_runtime().clear_transfer_history()
            self.app.events.publish("transfers.changed", {"cleared": cleared})
            return {"cleared": cleared}
        if method == "chat.devices":
            validate_params(params, {})
            return self.app.require_runtime().chat_devices()
        if method == "chat.sessions":
            validate_params(params, {})
            return self.app.require_runtime().chat_sessions()
        if method == "chat.mute":
            validate_params(params, {
                "peer_id": (str, lambda v: 0 < len(v) <= 128),
                "muted": (bool, lambda _: True),
            }, ("peer_id", "muted"))
            return self.app.require_runtime().set_chat_muted(params["peer_id"], params["muted"])
        if method == "chat.messages":
            validate_params(params, {
                "session_id": (str, lambda v: 0 < len(v) <= 128),
            }, ("session_id",))
            return self.app.require_runtime().chat_messages(params["session_id"])
        if method == "chat.open_file":
            validate_params(params, {
                "session_id": (str, lambda v: 0 < len(v) <= 128),
                "transfer_id": (str, lambda v: 0 < len(v) <= 128),
            }, ("session_id", "transfer_id"))
            return self.app.require_runtime().chat_saved_file(
                params["session_id"], params["transfer_id"]
            )
        if method == "chat.invite":
            fields = {
                "peer_id": (str, lambda v: 0 < len(v) <= 128),
                "peer_name": (str, lambda v: 0 < len(v) <= 256),
            }
            validate_params(params, fields, ("peer_id", "peer_name"))
            # Not `session_id`: that name is the transport session's, and a
            # result returning it under that key is refused as an invalid frame
            # (see `validate_result`).  This is a chat conversation's id, which
            # is a different thing that happens to share the word.
            session_id = self.app.require_runtime().chat_invite(
                params["peer_id"], params["peer_name"]
            )
            # No session yet means the link is still being dialed, not that the
            # invite was refused — the runtime publishes `chat.connect_timeout`
            # when the peer never answers, and the page's own poll picks the
            # session up once it opens.  The web layer reports the same two
            # cases the same way.
            return {"chat_session_id": session_id, "connecting": not session_id}
        if method == "chat.action":
            fields = {
                "action": (str, lambda v: v in (
                    "send", "accept", "decline", "read", "close", "resend"
                )),
                "session_id": (str, lambda v: 0 < len(v) <= 128),
                "text": (str, lambda v: len(v) <= 16000),
            }
            validate_params(params, fields, ("action", "session_id"))
            return {"ok": bool(self.app.require_runtime().chat_action(
                params["action"], params["session_id"], params.get("text", "")
            ))}
        if method == "chat.typing":
            validate_params(params, {
                "session_id": (str, lambda v: 0 < len(v) <= 128),
                "typing": (bool, lambda _: True),
            }, ("session_id", "typing"))
            return {"ok": bool(self.app.require_runtime().chat_typing(
                params["session_id"], params["typing"]
            ))}
        if method == "chat.file":
            fields = {
                "action": (str, lambda v: v in ("send", "accept", "decline", "cancel")),
                "session_id": (str, lambda v: 0 < len(v) <= 128),
                "transfer_id": (str, lambda v: len(v) <= 128),
                "path": (str, lambda v: len(v) <= 4096),
            }
            validate_params(params, fields, ("action", "session_id"))
            result = self.app.require_runtime().chat_file(
                params["action"], params["session_id"],
                params.get("transfer_id", ""), params.get("path", ""),
            )
            return {"ok": bool(result), "transfer_id": result if isinstance(result, str) else ""}
        if method == "backups.list":
            validate_params(params, {})
            return self.app.backups()
        if method == "backups.create":
            validate_params(params, {})
            return self.app.create_backup()
        if method == "backups.restore":
            validate_params(params, {"path": (str, lambda v: 0 < len(v) <= 4096)}, ("path",))
            return self.app.restore_backup(params["path"])
        if method == "history.export":
            validate_params(params, {
                "format": (str, lambda v: v in ("json", "csv", "markdown")),
            }, ("format",))
            return self.app.export_history(params["format"])
        if method == "history.import":
            validate_params(params, {"path": (str, lambda v: 0 < len(v) <= 4096)}, ("path",))
            return self.app.import_history(params["path"])
        if method in FAVORITES_METHODS:
            return dispatch_favorites(self.app, method, params, validate_params)
        if method == "sync.set_enabled":
            validate_params(params, {"enabled": (bool, lambda _: True)}, ("enabled",))
            result = self.app.require_runtime().set_sync_enabled(params["enabled"])
            self.app.events.publish("app.status.changed", {})
            return result
        if method == "sync.pause":
            validate_params(params, {"minutes": (int, lambda v: 1 <= v <= 1440)}, ("minutes",))
            return self.app.require_runtime().pause_sync_for(params["minutes"])
        if method == "sync.resume":
            validate_params(params, {})
            return self.app.require_runtime().resume_sync()
        pairing_commands = {
            "pairing.start": "start_pairing", "pairing.confirm": "confirm_pairing",
            "pairing.reject": "reject_pairing", "pairing.unpair": "unpair_device",
        }
        if method in pairing_commands:
            fields = {"device_id": (str, lambda v: 0 < len(v) <= 128)}
            required = ("device_id",)
            if method == "pairing.confirm":
                fields["code"] = (
                    str, lambda v: len(v) == 8 and v.isascii() and v.isdecimal()
                )
                required = ("device_id", "code")
            validate_params(params, fields, required)
            runtime = self.app.require_runtime()
            return getattr(runtime, pairing_commands[method])(**params)
        if method == "history.list":
            validate_params(
                params,
                {
                    "query": (str, lambda v: len(v) <= 512),
                    "offset": (int, lambda v: 0 <= v <= 1_000_000),
                    "limit": (int, lambda v: 1 <= v <= 100),
                    # The panel's chips and sort toggle, validated against the
                    # sets the use case defines: an unknown chip names no filter
                    # and must be rejected here rather than silently treated as
                    # "all", which would show a user a filter that is not on.
                    "kind": (str, lambda v: v in KINDS),
                    "sort": (str, lambda v: v in SORTS),
                },
            )
            return self.app.events.snapshot(lambda: self.app.require_history().list(**params))
        if method == "history.copy":
            validate_params(
                params, {"entry_id": (str, lambda v: 0 < len(v) <= 64)}, ("entry_id",)
            )
            result = self.app.require_history().copy(params["entry_id"])
            self.app.events.publish("history.changed", {"id": params["entry_id"]})
            return result
        if method == "history.text":
            # A read, so nothing is published: the row is unchanged and the
            # window already holds its list.
            validate_params(
                params, {"entry_id": (str, lambda v: 0 < len(v) <= 64)}, ("entry_id",)
            )
            return self.app.require_history().text(params["entry_id"])
        if method == "history.open_link":
            # Also a read, and the caller sends no URL: the window names the row
            # and the sidecar reads its text, so nothing a WebView holds can
            # reach the browser (internal.system.about::open_web_url decides).
            validate_params(
                params, {"entry_id": (str, lambda v: 0 < len(v) <= 64)}, ("entry_id",)
            )
            return self.app.require_history().open_link(params["entry_id"])
        if method == "history.clear":
            validate_params(params, {})
            result = self.app.require_history().clear()
            self.app.events.publish("history.changed", {"cleared": result["cleared"]})
            return result
        if method in ("history.batch_delete", "history.batch_set_pinned"):
            fields = {"entry_ids": (list, valid_entry_ids)}
            required = ("entry_ids",)
            if method == "history.batch_set_pinned":
                fields["pinned"] = (bool, lambda _: True)
                required = ("entry_ids", "pinned")
            validate_params(params, fields, required)
            history = self.app.require_history()
            if method == "history.batch_delete":
                result = history.batch_delete(params["entry_ids"])
            else:
                result = history.batch_set_pinned(params["entry_ids"], params["pinned"])
            self.app.events.publish("history.changed", {"ids": params["entry_ids"]})
            return result
        if method in ("history.delete", "history.set_pinned"):
            fields = {"entry_id": (str, lambda v: 0 < len(v) <= 64)}
            required = ("entry_id",)
            if method == "history.set_pinned":
                fields["pinned"] = (bool, lambda _: True)
                required = ("entry_id", "pinned")
            validate_params(params, fields, required)
            history = self.app.require_history()
            if method == "history.delete":
                result = history.delete(params["entry_id"])
            else:
                result = history.set_pinned(params["entry_id"], params["pinned"])
            self.app.events.publish("history.changed", {"id": params["entry_id"]})
            return result
        raise ApplicationError("METHOD_NOT_FOUND", "Unknown method")


class RpcServer:
    def __init__(self, app: SidecarApplication, reader: BinaryIO, writer: BinaryIO):
        self.app = app
        self.reader = reader
        self.writer = writer
        self.dispatcher = Dispatcher(app)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._sequence = 0
        self._input: queue.Queue[bytes | OSError] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._read_next = threading.Event()

    def send(self, value: dict) -> None:
        self.writer.write(encode_frame(value))
        self.writer.flush()

    def serve(self) -> int:
        self.send(
            {
                "type": "ready",
                "protocol": PROTOCOL_VERSION,
                "session_id": self.app.events.session_id,
                "pid": os.getpid(),
                "health": self.app.status()["health"],
            }
        )
        input_thread = threading.Thread(
            target=self._read_input, name="clipsync-ipc-input", daemon=True
        )
        input_thread.start()
        try:
            return self._serve_requests()
        finally:
            self._stop.set()
            self._read_next.set()
            # On shutdown/EOF the reader is idle, not reading ahead into stdin.
            # A disconnected output may leave a pending OS read until host pipe closure.
            input_thread.join(timeout=1)

    def _read_input(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.reader.readline(MAX_FRAME_BYTES + 2)
            except OSError as exc:
                self._input.put(exc)
                return
            self._input.put(raw)
            if not raw:
                return
            self._read_next.wait()
            self._read_next.clear()

    def _send_events(self) -> None:
        events, gap = self.app.events.since(self._sequence)
        if gap:
            self.send({"type": "resync", "session_id": self.app.events.session_id})
        for event in events[:EVENT_BATCH_SIZE]:
            try:
                self.send(event)
            except ApplicationError as exc:
                if exc.code != "RESPONSE_TOO_LARGE":
                    raise
                # A large notification must not wedge the stream or grow the frame limit.
                self.send({"type": "resync", "session_id": self.app.events.session_id})
            self._sequence = event["seq"]

    def _serve_requests(self) -> int:
        while not self.dispatcher.shutdown_requested:
            try:
                raw = self._input.get(timeout=INPUT_POLL_SECONDS)
            except queue.Empty:
                self._send_events()
                continue
            if isinstance(raw, OSError):
                raise raw
            if not raw:
                return 0
            try:
                if not raw.endswith(b"\n"):
                    raise ValueError("Truncated or oversized frame")
                request = decode_frame(raw[:-1])
                self._validate_request(request)
            except (ValueError, UnicodeError, RecursionError):
                self.send(
                    {
                        "type": "fatal",
                        "error": {
                            "code": "PROTOCOL_ERROR",
                            "message": "Invalid request frame",
                            "retryable": False,
                        },
                    }
                )
                return 2
            request_id = request["id"]
            response = {"type": "response", "id": request_id}
            if request_id in self._seen:
                error = ApplicationError("DUPLICATE_ID", "Request ID was already used")
                self.send({**response, "ok": False, "error": error.as_dict()})
                self._read_next.set()
                self._send_events()
                continue
            self._seen[request_id] = None
            if len(self._seen) > 4096:
                self._seen.popitem(last=False)
            try:
                result = self.dispatcher.call(request["method"], request["params"])
                validate_result(result, self.app.events.session_id)
                encoded = encode_frame({**response, "ok": True, "result": result})
            except ApplicationError as exc:
                encoded = encode_frame({**response, "ok": False, "error": exc.as_dict()})
            except Exception:
                # Never log params or exception text: requests may contain a password.
                logger.error("IPC command failed: %s", request["method"])
                error = ApplicationError("INTERNAL_ERROR", "Operation failed")
                encoded = encode_frame({**response, "ok": False, "error": error.as_dict()})
            self.writer.write(encoded)
            self.writer.flush()
            if self.dispatcher.shutdown_requested:
                return 0
            self._read_next.set()
            self._send_events()
        return 0

    @staticmethod
    def _validate_request(value: dict) -> None:
        allowed = {"type", "id", "method", "params", "correlation_id"}
        if value.keys() - allowed or value.get("type") != "request":
            raise ValueError("Invalid envelope")
        if not isinstance(value.get("params"), dict):
            raise ValueError("Params must be an object")
        for field in ("id", "method"):
            if not isinstance(value.get(field), str) or not 0 < len(value[field]) <= 128:
                raise ValueError("Invalid identifier")
        correlation = value.get("correlation_id", "")
        if not isinstance(correlation, str) or len(correlation) > 128:
            raise ValueError("Invalid correlation ID")
