"""The update lifecycle, shared by the legacy web panel and the native desktop.

``internal.system.updater`` owns the network primitives (release lookup,
verified download, digest check, peer cache).  This module owns the *lifecycle*
around them: the phase state machine both surfaces render (idle / downloading /
ready / failed), the worker that drives a download to a verified archive, the
periodic silent check, and the reveal action.

Nothing is auto-applied on any platform — the verified archive is moved to
``~/Downloads/clipsync-update/`` and the user replaces the old install by hand.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from internal.i18n import T
from internal.system import updater
from internal.system.file_manager import FILE_NOT_FOUND, FOLDER_NOT_FOUND, reveal_folder
from internal.version import __version__

logger = logging.getLogger(__name__)

PHASES = ("idle", "downloading", "ready", "failed")
# Mirrors the legacy /api/update/check route: the lookup can block for ~25s
# when GitHub is unreachable, so bound it and report "no update" instead of
# pinning the caller.
CHECK_TIMEOUT = 8.0
CHECK_WALL_BOUND = 8.0
# Silent periodic check window, matching the legacy device-status loop.
AUTO_CHECK_INTERVAL = 6 * 3600
AUTO_CHECK_TICK = 3.0
# Progress events are throttled; the in-memory state still tracks every chunk.
PROGRESS_MIN_INTERVAL = 0.25


def ready_archive_dir(home: str | None = None) -> Path:
    """Where a verified update archive is stashed for a manual install."""
    return (Path(home) if home else Path.home()) / "Downloads" / "clipsync-update"


def stage_ready_archive(asset_path: str, home: str | None = None) -> str:
    """Move a verified release archive where the user can find it.

    Returns the archive's new path.  Raises on any filesystem failure so the
    caller can surface a failed state rather than a "ready" that is not there.
    """
    dest_dir = ready_archive_dir(home)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / os.path.basename(asset_path)
    shutil.move(asset_path, str(dest))
    return str(dest)


class UpdateService:
    """Serialized update lifecycle for one application instance.

    *publish* is called as ``publish(name, data)`` for every state change;
    *config* is a callable returning the live configuration (the periodic check
    reads ``auto_update_check`` from it) or None when no configuration exists.
    """

    def __init__(
        self,
        publish: Callable[[str, dict], None] | None = None,
        config: Callable[[], object] | None = None,
        home: str | None = None,
    ):
        self._publish = publish
        self._config = config
        self._home = home
        self._lock = threading.RLock()
        self._state = {
            "phase": "idle",
            "fraction": 0,
            "downloaded": 0,
            "total": 0,
            "error": "",
            "version": "",
            "path": "",
        }
        self._downloading = False
        self._installing = False
        self._pending_version = ""
        self._last_progress = 0.0
        self._last_auto_check: float | None = None
        self._shutting_down = False
        self._timer: threading.Thread | None = None
        self._wake = threading.Event()

    # ── state ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            return {"state": dict(self._state)}

    def _set_state(self, **updates) -> None:
        with self._lock:
            self._state.update(updates)
            snapshot = dict(self._state)
        self._publish_state(snapshot)

    def _publish_state(self, snapshot: dict) -> None:
        if self._publish is None:
            return
        try:
            # Same shape as status(), so a client can apply either to one field.
            self._publish("update.state", {"state": snapshot})
        except Exception:
            logger.debug("Update state publish failed", exc_info=True)

    def _notify_available(self, result: dict) -> None:
        if self._publish is None:
            return
        try:
            self._publish(
                "update.available",
                {
                    "latest": result.get("latest", ""),
                    "current": result.get("current", ""),
                    "url": result.get("url", ""),
                },
            )
        except Exception:
            logger.debug("Update availability publish failed", exc_info=True)

    # ── manual check ─────────────────────────────────────────────────────
    def check(self) -> dict:
        """Query GitHub for a newer release, bounded; never raises."""
        result: dict = {}
        done = threading.Event()

        def _run():
            nonlocal result
            try:
                result = updater.check_for_update(timeout=CHECK_TIMEOUT)
            except Exception:
                logger.exception("Update check failed")
            finally:
                done.set()

        threading.Thread(target=_run, name="update-check", daemon=True).start()
        done.wait(timeout=CHECK_WALL_BOUND)
        if not isinstance(result, dict):
            result = {}
        return {
            "available": bool(result.get("available")),
            "latest": result.get("latest", ""),
            "current": result.get("current", ""),
            "url": result.get("url", ""),
        }

    # ── download ─────────────────────────────────────────────────────────
    def start_download(self) -> dict:
        """Start the download in the background; progress arrives as events."""
        with self._lock:
            if self._shutting_down:
                return {"ok": False, "started": False, "error": "app is shutting down"}
            if self._installing or self._downloading:
                return {"ok": False, "started": False, "error": "update already in progress"}
            self._downloading = True
        self._set_state(phase="downloading", fraction=0, downloaded=0, total=0, error="")
        threading.Thread(
            target=self._download_worker, name="update-download", daemon=True
        ).start()
        return {"ok": True, "started": True, "error": None}

    def _on_progress(self, downloaded: int, total: int) -> None:
        fraction = (downloaded / total) if total > 0 else 0
        with self._lock:
            self._state.update(
                {"fraction": fraction, "downloaded": downloaded, "total": total}
            )
            snapshot = dict(self._state)
            now = time.monotonic()
            due = (now - self._last_progress) >= PROGRESS_MIN_INTERVAL
            if due:
                self._last_progress = now
        if due:
            self._publish_state(snapshot)

    def _download_worker(self) -> None:
        dest_dir = tempfile.mkdtemp(prefix="clipsync_update_")
        try:
            path, reason, version = updater.download_latest_release(
                dest_dir, progress_cb=self._on_progress
            )
        except Exception as exc:
            logger.exception("Update download failed")
            path, reason, version = None, str(exc), ""
        finally:
            with self._lock:
                self._downloading = False
        self._pending_version = version
        self._finish(path, reason, "github")

    def finish_from_peer(self, path: str) -> None:
        """A peer sent us its cached update asset (M2 P2P update).

        Runs on the caller's thread: the release lookup inside :meth:`_finish`
        can block, so the runtime hands this off the transfer receive thread.
        """
        self._finish(path, None, "p2p")

    def _finish(self, path: str | None, reason: str | None, source: str = "github") -> None:
        """Verify, cache and stage an arrived asset, or surface the failure."""
        with self._lock:
            if self._installing:
                logger.info(
                    "Update install already in progress — skipping %s arrival", source
                )
                return
        if not path:
            self._set_state(phase="failed", error=reason or T("tray.update_install_failed"))
            return

        # The GitHub path was already size- and hash-checked while downloading.
        # A peer-sent blob has nobody to answer to, so it must be checked against
        # the published digest — a lookup that can block for ~30s and therefore
        # never runs on a UI or transfer thread.
        release_info = None
        if source != "github":
            try:
                release_info = updater.fetch_latest_asset_info()
            except Exception as exc:  # defensive — the helper never raises
                logger.debug("Release info lookup failed: %s", exc)
        ok, verdict = False, "no_release_info"
        try:
            ok, verdict = updater.verify_update_blob(path, release_info, __version__, source=source)
        except Exception:
            logger.exception("Update verification crashed")
            verdict = "hash_mismatch"
        if not ok:
            if verdict == "no_release_info":
                # Nothing authoritative to verify a peer blob against — never
                # install it. Fall back to the release-server download without
                # asking peers again, so an unverifiable blob cannot ping-pong
                # between devices.
                logger.warning(
                    "P2P update rejected: no release info to verify against — "
                    "falling back to the release server"
                )
                self._pending_version = ""
                self.start_download()
                return
            self._discard(path, verdict)
            return
        if release_info:
            # The digest match pins this blob to that release, so its version is
            # authoritative for the ready card (a peer blob carries no version).
            self._pending_version = str(release_info.get("version", "")) or self._pending_version

        with self._lock:
            self._installing = True
        try:
            updater.cache_asset(path)
            dest = stage_ready_archive(path, self._home)
        except Exception:
            logger.exception("Stashing ready update archive failed")
            with self._lock:
                self._installing = False
            self._set_state(phase="failed", error=T("tray.update_install_failed"))
            return
        with self._lock:
            self._installing = False
        self._set_state(
            phase="ready",
            version=self._pending_version or "",
            path=dest,
            fraction=1,
        )

    def _discard(self, path: str, verdict: str) -> None:
        """Throw away a rejected blob and report why."""
        try:
            os.remove(path)
        except OSError:
            logger.debug("Could not remove rejected update blob", exc_info=True)
        key = (
            "notify.update_rejected_hash"
            if verdict == "hash_mismatch"
            else "notify.update_rejected_old"
        )
        self._set_state(phase="failed", error=T(key))

    # ── reveal ───────────────────────────────────────────────────────────
    def open_folder(self) -> dict:
        """Reveal the ready archive's folder; the path never comes from a client."""
        with self._lock:
            state = dict(self._state)
        if state.get("phase") != "ready":
            return {"ok": False, "error": "no ready update"}
        path = state.get("path", "")
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "ready file missing"}
        ok, detail = reveal_folder(path)
        if ok:
            return {"ok": True}
        message = {
            FILE_NOT_FOUND: T("ui.file_not_found_msg", path=path),
            FOLDER_NOT_FOUND: T("ui.folder_not_found_msg", path=path),
        }.get(detail, T("ui.open_failed_msg", path=path))
        return {"ok": False, "error": message}

    # ── periodic silent check ────────────────────────────────────────────
    def start(self) -> None:
        """Start (or restart) the periodic check loop; safe to call twice."""
        self._shutting_down = False
        self._wake.clear()
        if self._timer is not None and self._timer.is_alive():
            return
        self._timer = threading.Thread(
            target=self._auto_check_loop, name="auto-update-check", daemon=True
        )
        self._timer.start()

    def stop(self) -> None:
        """Stop scheduling new work; in-flight daemon workers die with the app."""
        self._shutting_down = True
        self._wake.set()
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.join(timeout=1.0)

    def _auto_check_loop(self) -> None:
        while not self._shutting_down:
            try:
                self.maybe_auto_check()
            except Exception:
                logger.exception("Auto update check tick failed")
            # Wait on the event, not a sleep, so stop() returns immediately.
            if self._wake.wait(AUTO_CHECK_TICK):
                self._wake.clear()
                return

    def maybe_auto_check(self) -> bool:
        """Fire the silent check when enabled and due; returns whether it fired.

        With ``auto_update_check`` off this touches neither the network nor the
        throttle timestamp, so toggling back ON starts a fresh window.
        """
        cfg = self._config() if self._config is not None else None
        if cfg is None or not getattr(cfg, "auto_update_check", True):
            return False
        now = time.monotonic()
        # None is "never checked", and it cannot be spelled 0.0: monotonic()'s
        # zero is boot, not the epoch, so on a host up for less than
        # AUTO_CHECK_INTERVAL the sentinel reads as "checked a moment ago" and
        # silently suppresses the first automatic check -- which is why this
        # passed on a dev box up for 85h and failed on every fresh CI runner.
        last = self._last_auto_check
        if last is not None and now - last < AUTO_CHECK_INTERVAL:
            return False
        self._last_auto_check = now
        threading.Thread(
            target=self._auto_check_worker, name="auto-update-check", daemon=True
        ).start()
        return True

    def _auto_check_worker(self) -> None:
        """Silent check: only surfaces a result when an update is available."""
        try:
            result = updater.check_for_update(timeout=CHECK_TIMEOUT)
        except Exception as exc:
            logger.debug("Auto update check failed: %s", exc)
            return
        if result.get("available") and not self._shutting_down:
            self._notify_available(result)
