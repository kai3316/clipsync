"""Server-pushed dialog manager for the web UI.

Allows backend code to push a modal dialog to the web frontend and
block-wait for the user's response.  Used to replace CTk dialogs in
webview mode.

Usage::

    # In webview mode:
    result = dialog_mgr.show("transfer_request", title="Incoming File",
                             file_name="doc.pdf", file_size=123456)
    if result.get("action") == "accept":
        ...

    # Simple alert (non-blocking):
    dialog_mgr.toast("Clipboard updated", duration=3000)

The manager requires a reference to the WebSocket manager for broadcast.
Set it via ``dialog_mgr.ws_manager = ...`` after both are created.
"""

import logging
import threading
import uuid

logger = logging.getLogger(__name__)


class DialogManager:
    """Manages server→client dialog requests and collects responses."""

    def __init__(self):
        self._ws_manager = None
        self._pending: dict[str, dict] = {}  # dialog_id → {event, response}
        # Dialogs queued while no web client was connected.  They are flushed
        # when a client attaches (see flush_pending) instead of being silently
        # rejected.
        self._queued_dialogs: list[dict] = []
        self._lock = threading.Lock()

    @property
    def ws_manager(self):
        return self._ws_manager

    @ws_manager.setter
    def ws_manager(self, mgr):
        self._ws_manager = mgr

    # ── Public API ──────────────────────────────────────────────────

    def show(self, dialog_type: str, *, title: str = "", message: str = "",
             timeout: float = 120.0, **kwargs) -> dict | None:
        """Push a dialog to the web UI and wait for the user's response.

        Returns the response dict: {"action": "accept", "value": ...}
        or None on timeout / no clients connected.

        Supported dialog_types and their extra kwargs:

        - ``alert``: message only, returns {"action": "ok"}
        - ``confirm``: accept_label, reject_label
        - ``transfer_request``: file_name, file_size, sender
        - ``pick_peer``: peers (list of {device_id, device_name})
        - ``url_input``: prefill (optional URL hint)
        - ``qr_code``: qr_data_url, url
        - ``progress``: progress (0.0–1.0), progress_text; call
          ``update_progress()`` while the dialog is open
        """
        dialog_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        response_holder = {}

        with self._lock:
            self._pending[dialog_id] = {
                "event": event,
                "response": response_holder,
            }

        data = {
            "dialog_id": dialog_id,
            "dialog_type": dialog_type,
            "title": title,
            "message": message,
            **kwargs,
        }

        sent = self._broadcast("show_dialog", data)
        if not sent:
            # No client connected — hold the dialog as pending so it is shown
            # when a client attaches instead of being silently rejected (e.g.
            # an incoming file transfer arriving while the web UI is closed).
            with self._lock:
                self._queued_dialogs.append(data)

        if event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(dialog_id, None)
                self._queued_dialogs[:] = [
                    q for q in self._queued_dialogs
                    if q.get("dialog_id") != dialog_id
                ]
            return response_holder
        else:
            # Timeout — send close and clean up
            self._broadcast("close_dialog", {"dialog_id": dialog_id})
            with self._lock:
                self._pending.pop(dialog_id, None)
                self._queued_dialogs[:] = [
                    q for q in self._queued_dialogs
                    if q.get("dialog_id") != dialog_id
                ]
            logger.warning("Dialog %s timed out after %.0fs", dialog_id, timeout)
            return None

    def update_progress(self, dialog_id: str, progress: float,
                        progress_text: str = "") -> None:
        """Update the progress bar on an open progress dialog."""
        self._broadcast("update_dialog", {
            "dialog_id": dialog_id,
            "progress": progress,
            "progress_text": progress_text,
        })

    def close(self, dialog_id: str) -> None:
        """Force-close a dialog on the client."""
        self._broadcast("close_dialog", {"dialog_id": dialog_id})
        with self._lock:
            self._pending.pop(dialog_id, None)
            self._queued_dialogs[:] = [
                q for q in self._queued_dialogs
                if q.get("dialog_id") != dialog_id
            ]

    def push(self, dialog_type: str, **kwargs) -> str | None:
        """Push a dialog without blocking for a response.

        Returns the dialog_id, or None if no clients are connected.
        Use this for dialogs that are updated/closed by the server
        (e.g. progress dialogs).  Cancel responses are still tracked
        so ``is_cancelled()`` can be polled.
        """
        dialog_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        response_holder = {}
        with self._lock:
            self._pending[dialog_id] = {
                "event": event,
                "response": response_holder,
            }

        data = {"dialog_id": dialog_id, "dialog_type": dialog_type, **kwargs}
        sent = self._broadcast("show_dialog", data)
        if not sent:
            # Hold for when a client attaches (see flush_pending) instead of
            # dropping the dialog silently.
            with self._lock:
                self._queued_dialogs.append(data)
        return dialog_id

    def is_cancelled(self, dialog_id: str) -> bool:
        """Return True if the user cancelled the pushed dialog."""
        with self._lock:
            pending = self._pending.get(dialog_id)
            if pending and pending["response"].get("action") == "cancel":
                return True
        return False

    def toast(self, message: str, duration: int = 3000) -> None:
        """Push a non-blocking toast notification to all clients."""
        self._broadcast("toast", {
            "message": message,
            "duration": duration,
        })

    def flush_pending(self) -> None:
        """Send dialogs that were queued while no client was connected.

        Called when a new WebSocket client attaches (wired via
        ``WebSocketManager.on_client_attached``) so dialogs that arrived
        "blind" — e.g. an incoming file transfer — are shown to the client
        instead of being silently rejected.
        """
        if self._ws_manager is None:
            return
        with self._lock:
            queued = list(self._queued_dialogs)
            self._queued_dialogs.clear()
        for data in queued:
            self._ws_manager.broadcast("show_dialog", data)

    # ── Response handler (called from API route) ────────────────────

    def handle_response(self, dialog_id: str, action: str,
                        value=None) -> bool:
        """Handle a dialog response from the client.  Returns True if the
        dialog was found and the response was stored.
        """
        with self._lock:
            pending = self._pending.get(dialog_id)
            if pending is None:
                return False
            pending["response"]["action"] = action
            if value is not None:
                pending["response"]["value"] = value
            pending["event"].set()
            return True

    # ── Internal ────────────────────────────────────────────────────

    def _broadcast(self, msg_type: str, data: dict) -> bool:
        """Send a message to all WebSocket clients. Returns True if any
        clients are connected.
        """
        if self._ws_manager is None:
            return False
        if self._ws_manager.client_count == 0:
            return False
        self._ws_manager.broadcast(msg_type, data)
        return True
