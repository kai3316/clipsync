"""Forward the runtime's events to the phone's WebSocket clients.

The phone page is served by the legacy web server, which knows nothing about
the runtime: legacy, the Application pushed these events itself
(``self.web_server.ws_manager.broadcast(...)``) from the same code that is now
the sidecar's runtime.  Nothing did under the sidecar, so the phone's transfer
bars, chat bubbles, relay badge and internet-delivery badges only moved when
their REST route was next polled — a chat message appeared only after a reload.

This maps the runtime's event stream back onto the legacy WS contract: the
message names and payload shapes ``internal/web/static/js/ws.js`` already
handles, built through the shared ``WebSocketManager`` helpers so the shapes
stay owned in one place.  ``devices.changed`` is deliberately not forwarded:
the server's own fingerprint poll already pushes that snapshot, so the phone
would render the same thing twice.  ``history.changed`` looked like the same
case and is not — see :meth:`PhonePush._history_changed`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PhonePush:
    """Bridge from the application event journal to ``ws_manager``."""

    def __init__(self, ws, runtime, journal):
        """Attach to *journal*; call :meth:`close` when the server goes away."""
        self._ws = ws
        self._runtime = runtime
        self._unsubscribe = journal.subscribe(self._forward)

    def close(self):
        """Stop forwarding (the companion's server is stopping or stopped)."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _forward(self, name, data):
        handler = _HANDLERS.get(name)
        if handler is None:
            return
        try:
            handler(self, data if isinstance(data, dict) else {})
        except Exception:
            # The journal already swallows a raising listener; this keeps the
            # failure next to the event that caused it instead of one level up.
            logger.exception("Phone push failed for %s", name)

    # ------------------------------------------------------ file transfers

    def _transfer_progress(self, data):
        self._ws.broadcast_transfer_progress(
            str(data.get("transfer_id") or ""), data.get("progress") or 0.0
        )

    def _transfer_complete(self, data):
        self._ws.broadcast_transfer_complete(
            str(data.get("transfer_id") or ""),
            bool(data.get("success")),
            bool(data.get("cancelled")),
        )

    # ------------------------------------------------------------- history

    def _history_changed(self, data):
        """A row was added, pinned, deleted or the list wiped — say so.

        Legacy wired ``sync_mgr.on_history_change`` straight to
        ``ws_manager.broadcast_history()``, with a comment saying outright that
        this is what made newly copied items appear in the panel.  Under the
        sidecar that hook was rewired to the event journal (which is what the
        window refreshes on) and the panel half was not replaced.  The web layer
        broadcasts a snapshot only from its own history routes, so everything
        else — a clip copied on this machine, one arriving from a peer over the
        cable or the relay, a delete made in the native window, a history
        import — left a phone sitting on a stale list until the page reloaded.

        A clear is the one shape the snapshot cannot express: the client's
        ``history_updated`` merge rebuilds the visible window but keeps its
        pagination, so a wiped list would still offer "Load more" over nothing.
        Everything else is a snapshot, including a delete — a batch delete's
        ``ids`` are the ones a batch pin sends and a single delete's ``id`` the
        one a single pin sends, so neither can be told apart here, and the
        snapshot is right either way: the client replaces the window in place,
        and its ``total`` drives the ghost calibration that catches a deletion
        in a paged list.
        """
        if "cleared" in data:
            self._ws.broadcast_history_clear()
            return
        self._ws.broadcast_history()

    # ---------------------------------------------------------------- chat

    def _chat_sessions(self, _data):
        self._ws.broadcast_chat_sessions(self._runtime.chat_sessions().get("sessions") or [])

    def _chat_message(self, data):
        self._ws.broadcast_chat_message(
            str(data.get("session_id") or ""), data.get("entry") or {}
        )

    def _chat_file_progress(self, data):
        self._ws.broadcast_chat_progress(
            str(data.get("session_id") or ""),
            str(data.get("transfer_id") or ""),
            data.get("fraction") or 0.0,
        )

    def _chat_file_done(self, data):
        self._ws.broadcast_chat_file_done(
            str(data.get("session_id") or ""),
            str(data.get("transfer_id") or ""),
            bool(data.get("success")),
            str(data.get("saved_path") or ""),
            str(data.get("status") or ""),
        )

    # --------------------------------------------------- relay and pairing

    def _relay_state(self, data):
        payload = {"state": data.get("state") or ""}
        broker = self._runtime.current_relay_broker()
        if broker:
            payload["broker"] = broker
        self._ws.broadcast("relay_state", payload)

    def _delivery_changed(self, data):
        # Same shape legacy's _delivery_ws broadcast: the phone folds it into
        # the chat bubble's delivery mark and the device card's pending count.
        self._ws.broadcast(
            "internet_delivery",
            {
                "peer_id": str(data.get("peer_id") or ""),
                "msg_id": str(data.get("msg_id") or ""),
                "status": str(data.get("status") or ""),
                "content_hash": data.get("content_hash") or "",
                "kind": data.get("kind") or "clipboard",
                "session_id": data.get("session_id") or "",
            },
        )

    def _netpair_peer(self, data):
        self._ws.broadcast(
            "netpair_peer",
            {
                "peer_id": str(data.get("peer_id") or ""),
                "name": data.get("name") or "",
                "status": data.get("status") or "paired",
            },
        )

    def _connect_rejected(self, data):
        # The refusing peer told us its name in the handshake, so it is passed
        # through as-is (legacy did not substitute the id here).
        self._ws.broadcast(
            "connect_rejected",
            {"peer_id": str(data.get("device_id") or ""), "name": data.get("name") or ""},
        )

    def _connect_unreachable(self, data):
        peer_id = str(data.get("device_id") or "")
        # Legacy showed the name when it had one and the short id otherwise.
        self._ws.broadcast(
            "connect_unreachable",
            {"peer_id": peer_id, "name": data.get("name") or peer_id[:12]},
        )

    def _pairing_request(self, data):
        # The phone's prompt card is keyed on peer_id/peer_name and shows the
        # SAS next to the code; the runtime's own event uses device_id/name.
        self._ws.broadcast(
            "pairing_request",
            {
                "peer_id": str(data.get("device_id") or ""),
                "peer_name": data.get("name") or "",
                "code": data.get("code") or "",
                "sas": data.get("sas") or "",
            },
        )

    def _pairing_resolved(self, data):
        self._ws.broadcast(
            "pairing_resolved",
            {"peer_id": str(data.get("device_id") or ""), "status": data.get("status") or ""},
        )

    def _aiconfig_file(self, data):
        self._ws.broadcast("aiconfig_file", data.get("event") or {})

    # ---------------------------------------------------------- favourites

    def _favorites_changed(self, _data):
        """A favourite was added, edited, reordered or deleted — say so.

        The event exists for the native window, which invalidates its cached
        list on it; nothing told the phone, so a favourite added from the
        desktop appeared on the phone only after a reload.  Legacy had no such
        push and needed none: the desktop *was* this page, so there was one
        surface and one list.  Two surfaces sharing `favorites.db` is what the
        migration created, and this is the missing half of it.

        A snapshot rather than a delta: the panel's list is small, and a
        favourite carries a user-edited title, a group and a position, so an
        edit would otherwise have to re-state whatever else moved with it.

        Only the native surface's writes travel this path.  The panel's own
        favourite routes write the database directly and publish nothing, so a
        favourite added from a phone still reaches no other client — the same
        shape of gap in the other direction, recorded as open rather than
        closed here.
        """
        self._ws.broadcast_favorites()

    # ------------------------------------------------------------ updates

    def _update_state(self, data):
        """The update lifecycle, unwrapped into the flat shape the page merges.

        Legacy pushed this from ``_set_update_state``, which merged each change
        into one dict — ``phase``, ``fraction``, ``downloaded``, ``total``,
        ``version``, ``path``, ``error`` — and broadcast the whole thing, so the
        page could fold a tick into its own copy with a single ``Object.assign``.
        That is what ``ws.js`` still does, and why it ignores a payload with no
        ``phase``.

        The runtime publishes the same dict wrapped as ``{"state": snapshot}``,
        deliberately: it is the shape ``status()`` returns, so the native window
        can apply a pushed event and a fetched status to one field.  Nothing
        took it back out of the wrapper for the phone, so a download started
        from the phone's own settings card reported nothing at all — while the
        component that starts it has a comment saying live progress arrives as
        these events.

        Only a payload that looks like a state object is pushed, matching the
        client's own gate: a message the page would drop on arrival is noise.
        """
        state = data.get("state")
        if not isinstance(state, dict) or not state.get("phase"):
            return
        self._ws.broadcast("update_state", state)


# Event name → handler.  Anything absent here is either already pushed by the
# web server (``devices.changed``, whose fingerprint poll owns that snapshot) or
# has no phone-side meaning.
_HANDLERS = {
    "transfer.progress": PhonePush._transfer_progress,
    "transfer.complete": PhonePush._transfer_complete,
    "history.changed": PhonePush._history_changed,
    "chat.sessions.changed": PhonePush._chat_sessions,
    "chat.message": PhonePush._chat_message,
    "chat.file.progress": PhonePush._chat_file_progress,
    "chat.file.done": PhonePush._chat_file_done,
    "relay.state.changed": PhonePush._relay_state,
    "relay.delivery.changed": PhonePush._delivery_changed,
    "netpair.peer.changed": PhonePush._netpair_peer,
    "pairing.request": PhonePush._pairing_request,
    "pairing.resolved": PhonePush._pairing_resolved,
    "device.connection_rejected": PhonePush._connect_rejected,
    "device.connection_unreachable": PhonePush._connect_unreachable,
    "aiconfig.file": PhonePush._aiconfig_file,
    "update.state": PhonePush._update_state,
    "favorites.changed": PhonePush._favorites_changed,
}
