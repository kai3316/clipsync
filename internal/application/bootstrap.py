"""Sidecar composition root; never imports the legacy Tk application."""

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

from internal.application.errors import ApplicationError
from internal.application.events import EventJournal
from internal.application.lifecycle import ApplicationLifecycle, Resource
from internal.application.use_cases.favorites import FavoritesUseCase
from internal.application.use_cases.history import (
    WEB_SOURCE,
    WEB_SOURCE_LABEL,
    HistoryUseCase,
    source_name,
)
from internal.application.use_cases.overview import build_overview
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import config_dir, load, save
from internal.data.logs import export_log, read_log_tail
from internal.data.recovery import (
    config_problem,
    history_problem,
    records_without_identity,
)
from internal.data.reset import reset_data_dir
from internal.diagnostics.localize import localize
from internal.diagnostics.report import build_report
from internal.i18n import set_locale
from internal.infrastructure.persistence.favorites import FavoritesRepository
from internal.infrastructure.persistence.instance_lock import DataInUseError, InstanceLock
from internal.infrastructure.security.device_identity import (
    IdentityInvalidError,
    IdentitySession,
    prepare_identity,
)
from internal.security.encryption import EncryptionManager, make_password_hash, verify_password
from internal.security.pairing import fingerprint_pem
from internal.system.qr import png_data_url
from internal.system.update_service import UpdateService
from internal.version import __version__
from internal.web.api.settings import get_settings, update_settings

logger = logging.getLogger(__name__)


class SidecarApplication:
    def __init__(self, clipboard_writer_factory=None, runtime_factory=None, companion_factory=None):
        self.events = EventJournal()
        # Owns the update lifecycle (check / download / stage / reveal). It needs
        # no lock state, so it exists before the first configuration load; the
        # periodic silent check reads auto_update_check through this callback.
        self.updates = UpdateService(
            publish=self.events.publish, config=lambda: self.config
        )
        self.config = None
        self.history: HistoryUseCase | None = None
        self.favorites: FavoritesUseCase | None = None
        self._repository: ClipboardHistoryDB | None = None
        self.identity: IdentitySession | None = None
        self._clipboard_writer_factory = clipboard_writer_factory
        self._runtime_factory = runtime_factory
        self.runtime = None
        self.companion = None
        self._companion_factory = companion_factory
        # Internet pairing is deliberately owned by the runtime boundary.
        # Keeping the optional service here lets the sidecar expose the same
        # use cases without importing the legacy Tk application.
        self.internet_pairing = None
        self._health = "starting"
        self._start_time = time.time()
        directory = config_dir()
        lock = InstanceLock(directory)

        def acquire():
            try:
                lock.start()
            except DataInUseError as exc:
                raise ApplicationError("DATA_IN_USE", str(exc)) from exc

        self.lifecycle = ApplicationLifecycle(
            [
                Resource("data lock", acquire, lock.stop),
                Resource("configuration and history", self._load, self._close),
                Resource(
                    "LAN runtime", self._start_runtime, self._stop_runtime,
                    blocks_dependencies=True,
                ),
                Resource(
                    "mobile companion", self._start_companion, self._stop_companion,
                    blocks_dependencies=True,
                ),
            ]
        )

    def _load(self) -> None:
        # Shared with `internal.data.recovery`, which has to set aside exactly
        # what this refuses to start on — two definitions would drift.
        path = config_dir() / "config.json"
        if config_problem(path) is not None:
            raise ApplicationError("DATA_INVALID", "Configuration requires recovery")
        self.config = load()
        # A password stored without the identity it was salted with locks the app
        # for good, and `internal.data.recovery` deliberately leaves it alone: the
        # hash records no fingerprint, so that state is indistinguishable from a
        # working lock. See the module docstring there.
        # The sidecar produces user-facing strings of its own (update failure
        # reasons, repair hints), so it follows the configured language exactly
        # like the legacy entry does at startup.
        set_locale(self.config.language)
        self.updates.start()
        if self.config.encryption_enabled and self.config.encryption_password_hash:
            self._health = "locked"
        else:
            self._open_history(self.config.encryption_password)

    def _open_history(self, password: str = "") -> None:
        cfg = self.config
        path = config_dir() / "clipboard_history.db"
        # Refuse corruption before the legacy repository's quarantine/recreate fallback.
        self._check_database(path)
        if cfg.encryption_enabled and not cfg.certificate_pem:
            self._check_identityless_history(path)
        try:
            identity = prepare_identity(cfg, password)
        except IdentityInvalidError as exc:
            raise ApplicationError("DATA_INVALID", str(exc)) from exc
        from internal.clipboard.history_db import set_max_age_days
        set_max_age_days(cfg.history_max_age_days or 0)
        repository = ClipboardHistoryDB(
            storage_path=str(path),
            max_entries=cfg.history_max_entries,
            enc_mgr=identity.encryption,
        )
        try:
            if identity.needs_save:
                save(identity.config, identity.encryption)
        except Exception as exc:
            repository.close()
            raise ApplicationError(
                "SAVE_FAILED", "Could not save device identity", retryable=True
            ) from exc
        self.config = identity.config
        self.identity = identity
        self._repository = repository
        # The opener is injected rather than imported by the use case: opening a
        # URL is the app's own decision (internal.system.about owns the rules),
        # and a test can pass a recorder instead of launching a browser.
        from internal.system.about import open_web_url

        self.history = HistoryUseCase(
            repository, write_clipboard=self._write_clipboard,
            prepare_restore=self._prepare_history_restore,
            paste_to_top=lambda: self.config.paste_to_top,
            open_url=open_web_url,
            source_label=self._history_source_name,
        )
        self.favorites = FavoritesUseCase(
            FavoritesRepository(config_dir() / "favorites.db"),
            write_clipboard=self._write_favorite,
            history_lookup=lambda entry_id: self._repository.find_by_id(entry_id)[1],
            export_dir=config_dir(),
        )
        self._health = "ready"

    def _write_favorite(self, content):
        self._prepare_history_restore()
        return self._write_clipboard(ClipboardContent(types={ContentType.TEXT: content.encode()}))

    def _write_clipboard(self, content):
        factory = self._clipboard_writer_factory
        if factory is None:
            from internal.clipboard.platform import create_writer

            factory = create_writer
        return factory().write(content)

    def _prepare_history_restore(self):
        if self.runtime is not None:
            self.runtime.sync.reset_dedup_for_restore()

    def _history_source_name(self, source_device: str) -> str:
        """Name the device a history row synced from, as the panel did.

        Read from the config on every call rather than captured once: a peer
        paired after startup has clips arriving with its id on them, and the
        window would otherwise label them with a raw id until the next restart.
        """
        names = {WEB_SOURCE: WEB_SOURCE_LABEL}
        config = self.config
        if config is not None:
            names[config.device_id] = config.device_name
            names.update({peer.device_id: peer.device_name for peer in config.peers.values()})
        return source_name(
            source_device, names, config.device_name if config is not None else ""
        )

    def _start_runtime(self):
        if self._runtime_factory is None or self.identity is None:
            return
        if self.runtime is not None:
            return
        identity = self.identity
        self.runtime = self._runtime_factory(
            self.config, identity.pairing, identity.encryption, self._repository,
            self.events, lambda: save(self.config, identity.encryption),
        )
        # A peer can answer our update_request with its cached asset; the runtime
        # hands that blob to the update service for verification and staging.
        set_sink = getattr(self.runtime, "set_update_sink", None)
        if set_sink is not None:
            set_sink(self.updates.finish_from_peer)
        self.runtime.start()
        self.internet_pairing = getattr(self.runtime, "internet_pairing", None)

    def _stop_runtime(self):
        if self.runtime is None:
            return True
        if self.runtime.stop() is False:
            return False
        self.runtime = None
        self.internet_pairing = None
        return True

    def _start_companion(self):
        if (self.identity is None or self.runtime is None
                or not self.config.web_enabled or self.companion is not None):
            return
        if not self.config.web_token:
            self.config.web_token = secrets.token_urlsafe(32)
            save(self.config, self.identity.encryption)
        factory = self._companion_factory
        if factory is None:
            from internal.infrastructure.runtime.companion import MobileCompanion

            factory = MobileCompanion
        self.companion = factory(
            self.config, self._repository, self.runtime, self.identity.encryption,
            on_settings_change=self._on_companion_settings_change,
            on_restart=lambda: self._request_host_restart("restart"),
            get_overview_data=self.overview,
            get_diagnostics=self.diagnostics,
            on_diagnostics_request=self.diagnostics_request,
            on_update_download=self.update_download,
            on_update_status=self.update_status,
            on_update_open_folder=self.update_open_folder,
            on_open_file=self._open_received_file,
            on_open_folder=self._reveal_folder,
            on_web_upload=self._record_web_upload,
            # The phone asks the desktop to show its own QR / send-URL UI; the
            # sidecar cannot draw either, so the host window does it.
            on_show_web_qr=lambda: self.events.publish("app.qr_requested", {}),
            on_send_url=lambda: self.events.publish("app.send_url_requested", {}),
            on_window_close=lambda: self.events.publish("app.window_close_requested", {}),
        )
        self.companion.start()

    def _stop_companion(self):
        if self.companion is None:
            return True
        if self.companion.stop() is False:
            return False
        self.companion = None
        return True

    def companion_status(self):
        cfg = self.config
        owner = self.companion
        server = owner.server if owner is not None else None
        listener = server._httpd if server is not None else None
        thread = server._thread if server is not None else None
        worker = owner._worker if owner is not None else None
        stopping = worker is not None and worker.is_alive()
        running = bool(listener is not None and thread is not None
                       and thread.is_alive() and not stopping)
        port = listener.server_address[1] if running else None
        token = cfg.web_token if running and self.identity is not None else None
        host = server._get_lan_ip() if running else None
        return {
            "enabled": bool(cfg and cfg.web_enabled),
            "port": cfg.web_port if cfg else None,
            "running": running,
            "state": "stopping" if stopping else "running" if running else "stopped",
            "actual_port": port,
            "url": f"http://{host}:{port}/mobile.html" if running else None,
            "access_url": (
                f"http://{host}:{port}/mobile.html?token={quote(token, safe='')}"
                if token else None
            ),
            "token": token,
        }

    def companion_qr(self) -> dict:
        """The Companion access URL and its QR image, for the native dialog.

        Mirrors the legacy tray dialog: the same ``mobile.html?token=…`` address
        (token included — a phone has to carry it), or a reason the code is
        missing.  Never raises: a stopped companion and a missing qrcode stack
        are both normal states the dialog renders.
        """
        status = self.companion_status()
        url = status.get("access_url") or status.get("url")
        if not url:
            return {"ok": False, "error": "COMPANION_NOT_RUNNING", "url": None, "qr": None}
        try:
            qr = png_data_url(url)
        except Exception:
            logger.debug("QR code generation failed", exc_info=True)
            return {"ok": False, "error": "QR_UNAVAILABLE", "url": url, "qr": None}
        return {"ok": True, "url": url, "qr": qr}

    def configure_companion(self, enabled, port=None, rotate_token=False):
        # Serialize service replacement with lifecycle shutdown/rollback.
        with self.lifecycle._lock:
            if self.lifecycle.state != "running":
                raise ApplicationError("APP_NOT_READY", "Application is not running")
            self.require_runtime()
            cfg = self.config
            old = (cfg.web_enabled, cfg.web_port, cfg.web_token)
            desired_port = cfg.web_port if port is None else port
            status = self.companion_status()
            replace = (desired_port != cfg.web_port or rotate_token
                       or not enabled or not status["running"])
            if replace and not self._stop_companion():
                raise ApplicationError(
                    "COMPANION_STOP_FAILED", "Mobile companion is still stopping",
                    retryable=True,
                )
            cfg.web_enabled = enabled
            cfg.web_port = desired_port
            if rotate_token or (enabled and not cfg.web_token):
                cfg.web_token = secrets.token_urlsafe(32)
            try:
                # Persist credentials before exposing the listener.
                save(cfg, self.identity.encryption)
            except Exception:
                cfg.web_enabled, cfg.web_port, cfg.web_token = old
                self.events.publish("companion.changed", {})
                raise ApplicationError(
                    "SAVE_FAILED", "Companion configuration could not be saved",
                    retryable=True,
                ) from None
            try:
                if enabled:
                    self._start_companion()
            except Exception:
                # Keep configured intent for explicit retry, but never advertise
                # a failed bind as running. Retain ownership if cleanup times out.
                self._stop_companion()
                raise ApplicationError(
                    "COMPANION_START_FAILED", "Mobile companion could not start",
                    retryable=True,
                ) from None
            finally:
                # No URLs/tokens in the broadcast event.
                self.events.publish("companion.changed", {})
            return self.companion_status()

    def require_internet_pairing(self):
        self.require_runtime()
        service = self.internet_pairing
        if service is None:
            raise ApplicationError(
                "INTERNET_PAIRING_UNAVAILABLE",
                "Internet pairing is not configured",
                retryable=True,
            )
        return service

    def require_runtime(self):
        if self.identity is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to access devices")
        if self.runtime is None:
            raise ApplicationError("LAN_NOT_READY", "LAN runtime is not available", retryable=True)
        return self.runtime

    @staticmethod
    def _check_identityless_history(path: Path) -> None:
        # Never change the fingerprint-derived storage key underneath existing data.
        # Early sidecar prototypes could create history before creating an identity.
        if records_without_identity(path):
            raise ApplicationError(
                "DATA_INVALID", "History has no device identity; recovery is required"
            )

    @staticmethod
    def _check_database(path: Path) -> None:
        # Refuse corruption before the legacy repository's quarantine/recreate
        # fallback; the same predicate decides what recovery moves aside.
        if history_problem(path) is not None:
            raise ApplicationError("DATA_INVALID", "History requires recovery")

    def unlock(self, password: str) -> dict:
        if self._health != "locked":
            return {"unlocked": self._health == "ready"}
        cfg = self.config
        try:
            fingerprint = fingerprint_pem(cfg.certificate_pem) if cfg.certificate_pem else ""
        except ValueError as exc:
            raise ApplicationError("DATA_INVALID", "Device identity requires recovery") from exc
        if not verify_password(password, fingerprint, cfg.encryption_password_hash):
            raise ApplicationError("INVALID_PASSWORD", "Incorrect password")
        self._open_history(password)
        try:
            self._start_runtime()
            self._start_companion()
        except Exception:
            # Retain the owned instance for lifecycle cleanup; do not start a second runtime.
            self._health = "ready"
            raise
        self.events.publish("app.status.changed", {"health": self._health})
        return {"unlocked": True}

    def status(self) -> dict:
        cfg = self.config
        return {
            "version": __version__,
            "health": self._health,
            "device_name": cfg.device_name if cfg else "",
            "device_id": cfg.device_id if cfg else "",
            "language": cfg.language if cfg else "zh-CN",
            # Configuration is not proof that the sync engine is running.
            "sync_state": self.runtime.sync_state if self.runtime is not None else "not_started",
            "runtime_error": (
                getattr(self.runtime, "_last_error", "") if self.runtime is not None else ""
            ),
            # Every name here is a method the dispatcher routes, and nothing
            # else: the window turns some of these strings into controls it
            # enables, so a name with no method behind it is a control that can
            # never come alive.  `chat.resend` was the one that had drifted --
            # resending is a verb of ``chat.action`` ("resend"), which this list
            # already carries, so the ability was reachable the whole time under
            # a name nothing could answer.  ``tests/sidecar/test_rpc.py``
            # reads this list back against the dispatcher's own source, so the
            # next one to drift fails there instead of in a disabled button.
            #
            # The list is split in two, and the split is a fact rather than a
            # style: the block below is the abilities that answer with the sync
            # engine down, and the second block is the ones whose handler calls
            # ``require_runtime()`` and can only answer with it up.  The first
            # block had collected a family that cannot answer without the engine
            # -- chat, transfers, the two sync pause verbs, the AI config editor,
            # a device's note and the companion's own configuration -- because it
            # had been read as "the app's abilities" rather than as "what works
            # before the engine starts".  A window that gates a control on one of
            # those gets a live control and a ``LAN_NOT_READY`` error, and the
            # phone panel reads none of this, so nothing but the list said
            # otherwise.  Membership is decided by the handler in both
            # directions; ``tests/sidecar/test_rpc.py`` reads the dispatcher's
            # source to hold both blocks to it.
            "capabilities": [
                "history.list", "history.delete", "history.set_pinned", "history.copy",
                "history.text", "history.open_link",
                "history.batch_delete", "history.batch_set_pinned", "history.clear",
                "favorites.list", "favorites.get", "favorites.add", "favorites.update",
                "favorites.delete", "favorites.copy", "favorites.batch_add", "favorites.export",
                "settings.get", "settings.update",
                "companion.status", "companion.qr",
                "backups.list", "backups.create", "backups.restore",
                "history.export", "history.import",
                "translate.text", "ai.profiles", "ai.profiles.update",
                "logs.tail", "logs.export",
                "app.open_link", "app.factory_reset",
                "diagnostics.report", "diagnostics.request",
                "update.check", "update.status", "update.download", "update.open_folder",
                "data.open_folder",
            # The runtime's own half -- and the internet-pairing family, the
            # delivery ledger, the transfer history's clear and the answer to a
            # certificate prompt, which the host has been calling all along
            # without the list ever naming them.  The coarsest thing the window
            # can check is that the engine is up, so that is the granularity
            # these carry; a service that can still be absent with the engine
            # running (the companion, the relay) answers with its own error.
            ] + ([
                "sync.set_enabled", "sync.pause", "sync.resume",
                "pairing.start", "pairing.confirm", "pairing.reject", "pairing.unpair",
                "devices.note", "devices.connect", "devices.disconnect", "devices.forget",
                "devices.restore", "devices.purge", "devices.test", "devices.certs",
                "devices.retrust",
                "companion.configure",
                "url.send", "clipboard.push", "discovery.status",
                "discovery.set_enabled", "discovery.set_visible",
                "transfers.list", "transfers.send", "transfers.action",
                "transfers.cancel_all", "transfers.speed_test", "transfers.clear_history",
                "chat.devices", "chat.sessions", "chat.messages", "chat.invite",
                "chat.action", "chat.file", "chat.typing", "chat.mute", "chat.open_file",
                "ai.inventory", "ai.preview", "ai.pull",
                "ai.local.listing", "ai.local.read", "ai.local.save",
                "ai.local.trash", "ai.local.open",
                "internet_pairing.status", "internet_pairing.generate",
                "internet_pairing.enter", "internet_pairing.rename",
                "internet_pairing.unpair",
                "relay.delivery_status",
            ] if self.runtime is not None and self.runtime.sync_state in ("running", "paused")
                else []),
        }

    def read_logs(self, lines=200) -> dict:
        """Tail the application log, redacted, for the native log viewer."""
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to read logs")
        return {"logs": read_log_tail(self.config, lines)}

    def export_logs(self, dest: str) -> dict:
        """Copy the log file to a destination the user chose in a save dialog.

        The host opens the dialog and passes the chosen path; the sidecar owns
        the log location, so neither side has to know the other's paths.
        """
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to export logs")
        result = export_log(dest)
        if not result.get("ok"):
            raise ApplicationError(
                result.get("error", "EXPORT_FAILED"), "Could not export the log file"
            )
        return result

    def open_about_link(self, target: str) -> dict:
        """Open one of the About dialog's fixed links in the default browser."""
        from internal.system.about import open_link

        ok, detail = open_link(target)
        if not ok:
            if detail == "UNKNOWN_TARGET":
                raise ApplicationError("VALIDATION_ERROR", "Unknown link target")
            raise ApplicationError("OPEN_FAILED", "Could not open the link")
        return {"ok": True, "url": detail}

    def diagnostics(self) -> dict:
        """Live health snapshot for the native diagnostics page.

        Built by the shared ``internal.diagnostics.report`` from whatever is
        actually alive: the runtime and the companion may both be absent (a
        locked or degraded application) and every group still renders, with the
        missing data surfaced as warn items rather than an error.
        """
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to run diagnostics")
        runtime = self.runtime
        companion = self.companion
        web = companion.server if companion is not None else None
        try:
            from internal.web.server import WebServer

            lan_ip = WebServer._get_lan_ip()
        except Exception:
            lan_ip = ""
        report = build_report(
            cfg=self.config,
            lan_ip=lan_ip,
            web=web,
            web_running=bool(web is not None and getattr(web, "is_running", False)),
            transport=getattr(runtime, "transport", None) if runtime is not None else None,
            discovery=getattr(runtime, "discovery", None) if runtime is not None else None,
            pairing=getattr(runtime, "pairing", None) if runtime is not None else None,
            chat=getattr(runtime, "chat", None) if runtime is not None else None,
            ai_config=getattr(runtime, "ai_config", None) if runtime is not None else None,
            file_transfer=(
                getattr(runtime, "file_transfer", None) if runtime is not None else None
            ),
            history=self._repository,
            start_time=self._start_time,
            relay_state=getattr(runtime, "relay_state", "off") if runtime is not None else "off",
            delivery_counts=(
                getattr(runtime, "delivery_counts", None) if runtime is not None else None
            ),
        )
        # These strings live in the web panel's catalog, so resolve them here for
        # the saved language rather than shipping the panel's table to the shell.
        return localize(report, getattr(self.config, "language", ""))

    def overview(self) -> dict:
        """Dashboard counters for the phone Companion's overview page.

        The phone renders the same status bar, counters and activity feed the
        legacy web dashboard did, so the aggregation lives in one shared use
        case instead of being re-derived by the web layer.
        """
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to read the overview")
        try:
            from internal.web.server import WebServer

            lan_ip = WebServer._get_lan_ip()
        except Exception:
            lan_ip = ""
        return build_overview(
            cfg=self.config,
            history=self._repository,
            runtime=self.runtime,
            start_time=self._start_time,
            lan_ip=lan_ip,
        )

    def update_check(self) -> dict:
        """Manual release check, bounded like the legacy /api/update/check."""
        return self.updates.check()

    def update_status(self) -> dict:
        """Current update phase, so a reloaded window can hydrate its card."""
        return self.updates.status()

    def update_download(self) -> dict:
        """Start the background download; progress arrives as update.state.

        Peers are asked first for a copy they already downloaded (M2); the
        release-server download runs in parallel as the fallback.
        """
        runtime = self.runtime
        if runtime is not None:
            request = getattr(runtime, "request_update_from_peers", None)
            if request is not None:
                try:
                    request()
                except Exception:
                    logger.debug("Peer update request failed", exc_info=True)
        return self.updates.start_download()

    def update_open_folder(self) -> dict:
        """Reveal the staged archive's folder (ready phase only)."""
        return self.updates.open_folder()

    def open_data_folder(self, which: str) -> dict:
        """Reveal one of the app's own data folders (never a client path).

        Mirrors ``/api/data/open-folder``: ``data`` is the config/data
        directory, ``backups`` its backups subdirectory.
        """
        from internal.config.config import _config_dir
        from internal.system.file_manager import reveal_folder

        folder = _config_dir() / "backups" if which == "backups" else _config_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ApplicationError("OPEN_FAILED", f"Could not create {folder}: {exc}") from None
        ok, detail = reveal_folder(str(folder))
        if not ok:
            raise ApplicationError("OPEN_FAILED", f"Could not open {folder} ({detail})")
        return {"ok": True, "folder": str(folder)}

    @staticmethod
    def _open_received_file(path: str) -> bool:
        """Open a received file on this machine (phone ``/api/file/open``).

        The route confines the path to the received-files directory before
        calling, so this never has to validate it again.
        """
        from internal.system.file_manager import open_file

        ok, detail = open_file(path)
        if not ok:
            logger.debug("Could not open a received file (%s)", detail)
        return ok

    @staticmethod
    def _reveal_folder(path: str) -> bool:
        """Reveal a folder in the OS file manager (phone ``/api/file/reveal``)."""
        from internal.system.file_manager import reveal_folder

        ok, detail = reveal_folder(path)
        if not ok:
            logger.debug("Could not reveal a folder (%s)", detail)
        return ok

    def _record_web_upload(self, file_name: str, file_size: int, saved_path: str) -> None:
        """A phone uploaded a file to this computer via the web companion.

        The upload bypasses the peer-to-peer transfer manager, so it is
        recorded by hand — otherwise the file exists on disk but never appears
        in the transfers panel.
        """
        runtime = self.runtime
        if runtime is None:
            return
        try:
            transfer_id = runtime.record_web_upload(file_name, file_size, saved_path)
        except Exception:
            logger.debug("Recording a phone upload failed", exc_info=True)
            return
        self.events.publish("transfers.changed", {"transfer_id": transfer_id})
        # The legacy app notified the user on a phone upload; the host cannot
        # see this transfer any other way, so the fact travels as an event.
        self.events.publish(
            "transfer.web_upload",
            {"transfer_id": transfer_id, "name": file_name, "size": file_size},
        )

    def diagnostics_request(self, action: str) -> dict:
        """Open the OS firewall/permission settings the report's hints point at."""
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to repair diagnostics")
        from internal.diagnostics.actions import request

        return request(action, self.config)

    def require_history(self) -> HistoryUseCase:
        if self.history is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to access history")
        return self.history

    def require_favorites(self) -> FavoritesUseCase:
        if self.favorites is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to access favorites")
        return self.favorites

    def settings(self) -> dict:
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to access settings")
        return get_settings(self.config)[0]

    def update_settings(self, values: dict) -> dict:
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to access settings")
        key_actions = {"set_translate_key", "clear_translate_key"}
        if key_actions.intersection(values):
            if len(values) != 1 or self.identity is None:
                raise ApplicationError(
                    "VALIDATION_ERROR", "Submit translation key changes separately"
                )
            from internal.config.config import config_lock
            with config_lock:
                previous = self.config.translate_api_key
                value = values.get("set_translate_key", "")
                if not isinstance(value, str) or len(value) > 4096:
                    raise ApplicationError("VALIDATION_ERROR", "Invalid translation key")
                self.config.translate_api_key = value.strip()
                try:
                    save(self.config, self.identity.encryption)
                except Exception as exc:
                    self.config.translate_api_key = previous
                    raise ApplicationError("SAVE_FAILED", "Could not save translation key") from exc
            self.events.publish("settings.changed", {"fields": ["translate_key_set"]})
            return {"ok": True, "translate_key_set": bool(self.config.translate_api_key)}
        result, status = update_settings(
            json.dumps(values, ensure_ascii=False).encode("utf-8"),
            self.config,
            on_settings_change=self._on_settings_change,
            enc_mgr=self.identity.encryption if self.identity else None,
        )
        if status >= 400 or not result.get("ok"):
            raise ApplicationError("VALIDATION_ERROR", result.get("error", "Invalid settings"))
        self.events.publish("settings.changed", {"fields": list(result.get("updated", {}))})
        return result

    def translate_text(self, text, target_lang="en", source_lang="auto"):
        from internal.web.api.translate import translate_text
        result = translate_text(text, target_lang, source_lang, self.config)
        if not result.get("ok"):
            raise ApplicationError(
                "TRANSLATE_FAILED", result.get("error", "Translation failed"), retryable=True
            )
        return result

    def ai_profiles(self):
        from internal.sync import ai_profiles
        return {
            "ok": True,
            "tools": ai_profiles.TOOLS,
            "enabled": list(getattr(self.config, "ai_config_tools", []) or []),
            "custom_paths": list(getattr(self.config, "ai_config_custom_paths", []) or []),
        }

    def update_ai_profiles(self, tools, custom_paths):
        from internal.config.config import config_lock
        from internal.sync import ai_profiles
        if self.identity is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to change AI profiles")
        normalized_tools = ai_profiles.validate_tool_keys(tools)
        normalized_paths = ai_profiles.validate_custom_paths(custom_paths)
        manager = getattr(self.runtime, "ai_config", None) if self.runtime else None
        with config_lock:
            previous_tools = self.config.ai_config_tools
            previous_paths = self.config.ai_config_custom_paths
            self.config.ai_config_tools = normalized_tools
            self.config.ai_config_custom_paths = normalized_paths
            try:
                save(self.config, self.identity.encryption)
            except Exception as exc:
                self.config.ai_config_tools = previous_tools
                self.config.ai_config_custom_paths = previous_paths
                raise ApplicationError(
                    "SAVE_FAILED", "Could not save AI profiles", retryable=True
                ) from exc
        if manager is not None:
            manager.on_watch_list_changed()
        return self.ai_profiles()

    def ai_inventory(self, refresh=False, peer_id=""):
        manager = getattr(self.require_runtime(), "ai_config", None)
        if manager is None:
            raise ApplicationError("AICONFIG_UNAVAILABLE", "AI config is unavailable")
        peers = manager.get_peer_inventories()
        refreshed = []
        if refresh:
            targets = [peer_id] if peer_id else sorted(peers)
            refreshed = [pid for pid in targets if manager.request_inventory(pid)]
        return {"peers": peers, "refreshed": refreshed, "local": manager.local_summary()}

    def ai_preview(self, params):
        manager = getattr(self.require_runtime(), "ai_config", None)
        if manager is None:
            raise ApplicationError("AICONFIG_UNAVAILABLE", "AI config is unavailable")
        return manager.preview(
            params["peer_id"], params.get("tool"), params["rel_path"], root=params.get("root", "")
        )

    def ai_pull(self, params):
        manager = getattr(self.require_runtime(), "ai_config", None)
        if manager is None:
            raise ApplicationError("AICONFIG_UNAVAILABLE", "AI config is unavailable")
        return manager.pull(
            params["peer_id"], params["items"],
            params.get("mode", "copy"), params.get("batch_id", ""),
        )

    def ai_local(self, action, params):
        manager = getattr(self.require_runtime(), "ai_config", None)
        if manager is None:
            raise ApplicationError("AICONFIG_UNAVAILABLE", "AI config is unavailable")
        if action == "listing":
            return manager.local_listing()
        method = {
            "read": "local_read", "save": "local_save",
            "trash": "local_trash", "open": "local_open",
        }[action]
        if action == "save":
            return getattr(manager, method)(
                params["tool"], params["rel_path"], params["content"], root=params.get("root", "")
            )
        return getattr(manager, method)(
            params["tool"], params["rel_path"], root=params.get("root", "")
        )

    def _on_settings_change(self, updated, special):
        # Every applier below reads the live config, so mirror ``updated`` into
        # it first.  The HTTP and RPC settings paths already mutated the config
        # before calling this, but the timed-pause resume calls it directly with
        # just {"sync_enabled": True} — without this, the sync engines would
        # read the still-paused value and nothing would resume.
        for field, value in updated.items():
            if hasattr(self.config, field):
                setattr(self.config, field, value)
        if "language" in updated:
            # Sidecar-generated strings (update reasons, repair hints) follow
            # the new language immediately, as in the legacy entry.
            set_locale(str(updated["language"]))
        if self._repository is not None and "history_max_entries" in updated:
            self._repository.MAX_ENTRIES = int(updated["history_max_entries"])
        if "history_max_age_days" in updated:
            from internal.clipboard.history_db import set_max_age_days
            set_max_age_days(updated["history_max_age_days"])
        applied = self._apply_encryption_change(updated, special)
        if self.runtime is not None:
            self.runtime.apply_settings(updated, special)
        return applied

    def _on_companion_settings_change(self, updated, special):
        """Apply a settings change made from the phone's web panel to live services.

        The companion's HTTP layer has already mutated and persisted the
        configuration; this is the host half the legacy entry passed as
        ``on_settings_change`` (``src/main.py::_on_web_settings_change``).  It
        runs the web-only action keys the settings API cannot perform itself —
        token regeneration, the translation key and the factory reset — and then
        the same live application the desktop path gets.  Without it the panel
        showed a saved value that never took effect.
        """
        if self.identity is None:  # The companion only runs with an identity.
            return {}
        response = {}
        if "regenerate_web_token" in special or "clear_web_token" in special:
            rotate = "regenerate_web_token" in special
            self.config.web_token = secrets.token_urlsafe(16) if rotate else ""
            self._persist_config("Could not save the web access token")
            response["token_updated"] = True
            response["web_token"] = self.config.web_token
            logger.info("Web token %s via web UI", "regenerated" if rotate else "cleared")
        if "set_translate_key" in special or "clear_translate_key" in special:
            key = special.get("set_translate_key", "")
            # A blank/whitespace — or oversized — value means "clear it", the
            # same way the legacy handler treated a blank key.
            if "clear_translate_key" in special or not isinstance(key, str) or len(key) > 4096:
                key = ""
            self.config.translate_api_key = key.strip()
            self._persist_config("Could not save the translation key")
            response["translate_key_set"] = bool(self.config.translate_api_key)
            logger.info(
                "Translation API key %s via web UI",
                "set" if self.config.translate_api_key else "cleared",
            )
        if "factory_reset" in special:
            # Deferred: this runs on the HTTP handler thread and the reset stops
            # the very server answering the request.  The legacy handler put it
            # on the Tk main loop for the same reason.
            self._defer(self._factory_reset_and_restart)
            response["ok"] = True
        response.update(self._on_settings_change(updated, special) or {})
        self._apply_companion_settings(updated)
        return response

    def _apply_companion_settings(self, updated):
        """Start/stop/rebind the companion after a web-panel change to it.

        The panel's remote-access section can switch off the server that is
        serving the request, so this is deferred off the handler thread: the
        response leaves first, then the listener moves.
        """
        if not {"web_enabled", "web_port"} & set(updated):
            return
        self._defer(self._reconfigure_companion)

    def _reconfigure_companion(self):
        try:
            cfg = self.config
            status = self.companion_status()
            if not cfg.web_enabled:
                if status["running"] or status["state"] == "stopping":
                    self.configure_companion(False)
                return
            if not status["running"] or status["actual_port"] != cfg.web_port:
                self.configure_companion(True, port=cfg.web_port)
        except Exception:
            logger.exception("Could not apply the web companion setting live")

    def _request_host_restart(self, reason):
        """Ask the native host to relaunch the app; it owns the process tree.

        The sidecar cannot restart itself — the Tauri host spawned it and holds
        its stdio — so the request travels back as an event and the host stops
        this process and relaunches it.
        """
        self.events.publish("app.restart_requested", {"reason": reason})

    def _factory_reset_and_restart(self):
        try:
            self.factory_reset()
        except Exception:
            logger.exception("Factory reset requested from the web panel failed")
            return
        self._request_host_restart("factory_reset")

    @staticmethod
    def _defer(callback, delay=0.4):
        """Run *callback* shortly on its own thread (never the HTTP handler)."""
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()

    def _persist_config(self, message):
        try:
            save(self.config, self.identity.encryption)
        except Exception as exc:
            raise ApplicationError("SAVE_FAILED", message, retryable=True) from exc

    def _apply_encryption_change(self, updated, special):
        """Set/clear the encryption password and re-wire live encryption.

        Mirrors the legacy web handler (``src/main.py``: the ``password`` /
        ``clear_password`` actions and the ``encryption_enabled`` toggle) and
        returns the response fields the web API is allowed to echo back.
        """
        password = special.get("password")
        clearing = bool(special.get("clear_password"))
        if password:
            self.config.encryption_enabled = True
            self.config.encryption_password = password
            # Unification: the single password also derives the netpair channel
            # keys.  Mirror it into the legacy field so pairing keeps working
            # across a restart — the password itself is runtime-only.
            self.config.netpair_password = password
        elif clearing:
            self.config.encryption_password = ""
            self.config.encryption_password_hash = ""
            self.config.netpair_password = ""
        if not (password or clearing or "encryption_enabled" in updated):
            return {}
        self._rewire_encryption()
        if password:
            return {"password_set": True}
        if clearing:
            return {"password_set": False}
        return {}

    def _rewire_encryption(self) -> None:
        """Rebuild the live encryption manager after a password/toggle change.

        Only mutating the config left the running app on its startup key state:
        the UI showed encryption on while frames still went out under the old
        key, and a peer on a different key state tore the link down after a few
        frames.  The stored verification hash follows the password exactly as
        the legacy ``_make_save_enc`` does — a stale hash would reject the new
        password at the next launch's unlock.
        """
        if self.identity is None:
            return
        cfg = self.config
        fingerprint = self.identity.pairing.get_identity().fingerprint
        if cfg.encryption_enabled:
            cfg.encryption_password_hash = (
                make_password_hash(cfg.encryption_password, fingerprint)
                if cfg.encryption_password else ""
            )
            encryption = EncryptionManager(fingerprint, password=cfg.encryption_password)
        else:
            encryption = None
        self.identity.encryption = encryption
        try:
            save(cfg, encryption)
        except Exception as exc:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save the encryption setting", retryable=True
            ) from exc
        if self.runtime is not None:
            self.runtime.apply_encryption(encryption)

    def factory_reset(self) -> dict:
        """Delete all user data and report that the host must relaunch.

        The host owns this process's lifecycle, so the sidecar only stops its
        own services — which is also what releases the file handles Windows
        needs released before it will unlink the data files — and clears the
        data directory; the shell restarts the app afterwards.
        """
        if self.config is None:
            raise ApplicationError("APP_LOCKED", "Unlock ClipSync to reset ClipSync")
        if self.lifecycle.stop() is False:
            raise ApplicationError(
                "RESET_FAILED", "ClipSync could not stop its services", retryable=True
            )
        deleted = reset_data_dir(config_dir())
        logger.warning("Factory reset: deleted %s", ", ".join(deleted) or "no files")
        return {"ok": True, "factory_reset": True, "deleted": len(deleted)}

    def backups(self) -> dict:
        from internal.data.backup import list_backups
        return {"backups": list_backups()}

    def create_backup(self) -> dict:
        from internal.data.backup import create_backup
        return {"backup_path": str(create_backup(self.config, self._repository))}

    def restore_backup(self, path: str) -> dict:
        from copy import deepcopy

        from internal.data.backup import restore_backup
        live_fields = (
            "device_name", "source_tracking_enabled", "sync_enabled",
            "filter_enabled_categories", "history_max_entries", "history_max_age_days",
        )
        previous = {key: deepcopy(getattr(self.config, key)) for key in live_fields}
        result = restore_backup(path, self.config, self._repository)
        if result.get("config"):
            try:
                save(self.config, self.identity.encryption if self.identity else None)
            except Exception:
                result.setdefault("errors", []).append("config persistence")
            else:
                updated = {
                    key: getattr(self.config, key) for key in live_fields
                    if previous[key] != getattr(self.config, key)
                }
                try:
                    self._on_settings_change(updated, {})
                except Exception:
                    result.setdefault("errors", []).append("live configuration")
        self.events.publish("data.changed", result)
        if result.get("errors"):
            raise ApplicationError(
                "RESTORE_PARTIAL_FAILED",
                "Backup restore was incomplete; some data may have changed. Failed sections: "
                + ", ".join(result["errors"]),
            )
        return result

    def export_history(self, fmt: str) -> dict:
        from internal.web.api.settings import export_data
        body = json.dumps({"format": fmt}).encode("utf-8")
        result, status = export_data(body, self.config, self._repository)
        if status >= 400:
            raise ApplicationError("EXPORT_FAILED", result.get("error", "Export failed"))
        return result

    def import_history(self, path: str) -> dict:
        from internal.web.api.settings import import_data
        body = json.dumps({"filepath": path}).encode("utf-8")
        result, status = import_data(body, self.config, self._repository)
        if status >= 400:
            raise ApplicationError("IMPORT_FAILED", result.get("error", "Import failed"))
        self.events.publish("history.changed", {})
        return result

    def devices(self) -> dict:
        if self.runtime is not None:
            return self.runtime.devices()
        return {
            "items": [
                {
                    "id": peer.device_id,
                    "name": peer.device_name,
                    "paired": peer.paired,
                    "connection_state": "unknown",
                }
                for peer in self.config.peers.values()
            ],
        }

    def _close(self) -> None:
        self.updates.stop()
        if self._repository is not None:
            self._repository.close()
            self._repository = None
        self.history = None
        self.favorites = None
        self.identity = None
        if self.config is not None:
            self.config.encryption_password = ""
            self.config.private_key_pem = ""
        self._health = "stopped"
