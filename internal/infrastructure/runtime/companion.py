"""Own the legacy authenticated mobile server without importing the GUI."""

import logging
import threading

from internal.application.errors import ApplicationError
from internal.infrastructure.runtime.internet_panel import InternetPanelHost
from internal.infrastructure.runtime.phone_push import PhonePush
from internal.web.api import internetdelivery as internetdelivery_api
from internal.web.api import internetpair as internetpair_api
from internal.web.server import WebServer

logger = logging.getLogger(__name__)


class MobileCompanion:
    STOP_TIMEOUT = 5.0

    def __init__(
        self, cfg, history, runtime, encryption, server_factory=WebServer,
        on_settings_change=None, on_restart=None,
        get_overview_data=None, get_diagnostics=None, on_diagnostics_request=None,
        on_update_download=None, on_update_status=None, on_update_open_folder=None,
        on_open_file=None, on_open_folder=None, on_web_upload=None,
        on_show_web_qr=None, on_send_url=None, on_window_close=None,
    ):
        """Adapt the LAN runtime and the application to the web server.

        Every callback the phone's panel can reach is wired here: a missing one
        does not fail loudly, it answers ``503 not available`` — so a callback
        left unset is a button on the phone that silently does nothing.
        ``on_settings_change``/``on_restart`` are the host half of the settings
        panel, ``get_diagnostics``/``on_update_*`` the host half of the health
        and update cards, and the rest cover devices, transfers and files.
        """
        self.cfg = cfg
        self.history = history
        self.runtime = runtime
        self.server = server_factory(
            cfg, history, runtime.sync,
            enc_mgr=encryption,
            on_settings_change=on_settings_change,
            on_restart=on_restart,
            get_connected_ids=runtime.transport.get_connected_peers,
            # Device page: live sightings, hash resolution, reconnect progress
            # and pending pairing requests all come from the runtime.
            get_discovered_peers=runtime.discovered_peers,
            get_resolved_hashes=runtime.resolved_hashes,
            get_reconnect_states=runtime.reconnect_states,
            get_pending_pairings=runtime.pending_pairings,
            get_certs=lambda: runtime.certs()["devices"],
            get_relay_state=runtime.relay_state,
            get_current_relay_broker=runtime.current_relay_broker,
            get_overview_data=get_overview_data,
            on_device_action=runtime.device_action,
            on_device_test=runtime.test_device,
            on_transfer_action=runtime.web_transfer_action,
            on_get_transfers=runtime.transfer_lists,
            on_speed_test_start=runtime.start_speed_test,
            on_speed_test_poll=runtime.speed_test_state,
            on_toggle_discovery=runtime.set_discovery_enabled,
            on_toggle_visibility=runtime.set_discovery_visible,
            on_nav_url=self._send_nav_url,
            on_forward_file=runtime.forward_file,
            get_diagnostics=get_diagnostics,
            on_diagnostics_request=on_diagnostics_request,
            on_update_download=on_update_download,
            on_update_status=on_update_status,
            on_update_open_folder=on_update_open_folder,
            on_open_file=on_open_file,
            on_open_folder=on_open_folder,
            on_web_upload=on_web_upload,
            on_show_web_qr=on_show_web_qr,
            on_send_url=on_send_url,
            on_window_close=on_window_close,
            chat_mgr=runtime.chat,
            get_chat_devices=lambda: runtime.chat_devices()["devices"],
            chat_send_fn=runtime._chat_send_fn,
            chat_start_session=runtime.chat_invite,
            get_chat_muted=lambda: runtime.chat_sessions()["muted"],
            set_chat_muted=lambda peer, muted: runtime.set_chat_muted(peer, muted)["muted"],
            on_history_change=self._history_changed,
            on_favorites_change=self._favorites_changed,
        )
        # The internet-pairing panel and the delivery badges are served by two
        # bind-style route modules that hold a host reference set at startup
        # (legacy: the Application).  Binding them here is what keeps those
        # phone buttons from answering 503 "unavailable".
        self.internet_panel = InternetPanelHost(runtime)
        internetpair_api.bind(self.internet_panel)
        internetdelivery_api.bind(self.internet_panel)
        self._push = None
        self._worker = None
        self._stopped = False
        self._lock = threading.Lock()

    def _favorites_changed(self):
        """The phone's own favourite routes changed the shared store.

        The panel writes ``favorites.db`` through ``internal/web/api`` rather
        than through the native request adapter, so nothing published the
        change: a favourite added, edited or deleted on a phone left the
        desktop window's cached list (and every other panel) showing what it
        had loaded.  Legacy had no such split — its desktop *was* this page —
        so this is the migration's own seam.

        Published on the runtime's journal rather than broadcast straight to
        the panels, because the journal is already the path both surfaces
        listen on: the window invalidates on it, and ``PhonePush`` turns it
        back into the snapshot the panels apply.  One event, both surfaces —
        including the one that sent it, whose list is already correct.
        """
        self.runtime.events.publish("favorites.changed", {})

    def _history_changed(self, payload):
        """The phone's own history routes changed the stored history.

        The same seam as :meth:`_favorites_changed`, on the other event, and
        the older half of it: the panel's history routes broadcast straight to
        the panels through ``dialog_mgr.ws_manager`` -- that predates the
        migration -- so a clip deleted, a pin toggled or the whole list cleared
        from a phone published nothing, and the desktop window kept showing
        what it had loaded until something else refreshed it.

        Published with the payload shapes the runtime's own history writes use
        (``{"cleared": n}``, ``{"id": ...}`` for one row, ``{"ids": [...]}``
        for a batch, ``{}`` when the route could only say "something"), so
        ``PhonePush`` answers the panels exactly as it already does for a
        change made in the window: a wipe stays a wipe, everything else is the
        page-1 snapshot their merge path expects.
        """
        self.runtime.events.publish("history.changed", payload if isinstance(payload, dict) else {})

    def _send_nav_url(self, url, device_id):
        """Forward a URL the panel typed to one peer (best effort).

        The runtime refuses an unpaired or offline target by raising; the route
        only reports success/failure, so the reason is logged here.
        """
        try:
            self.runtime.send_url(device_id, url)
        except Exception:
            logger.warning("Web nav URL to %s failed", (device_id or "")[:12], exc_info=True)

    def start(self):
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise ApplicationError(
                    "COMPANION_STOPPING", "Mobile companion is still stopping",
                    retryable=True,
                )
            if not self.server.start():
                raise ApplicationError(
                    "COMPANION_START_FAILED", "Mobile companion could not bind its port",
                    retryable=True,
                )
            # Forward the runtime's events to the page only while it is served:
            # a stopped server has no clients, and the subscription would keep
            # the runtime publishing into a closed manager.
            self._attach_push()
            self._stopped = False
            self._worker = None

    def _attach_push(self):
        if self._push is None:
            self._push = PhonePush(self.server.ws_manager, self.runtime, self.runtime.events)

    def _detach_push(self):
        push, self._push = self._push, None
        if push is not None:
            push.close()

    def _stop(self):
        try:
            # Legacy stop closes WebSockets and the listening socket.
            self._detach_push()
            thread = self.server._thread
            self.server.stop()
            if thread is not None:
                thread.join()
            self._stopped = True
        except Exception:
            # Keep ownership and allow a subsequent lifecycle stop to retry.
            self._stopped = False

    def stop(self):
        with self._lock:
            if self._stopped:
                return True
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._stop, name="clipsync-companion-stop", daemon=True,
                )
                self._worker.start()
            worker = self._worker
        worker.join(self.STOP_TIMEOUT)
        return not worker.is_alive() and self._stopped
