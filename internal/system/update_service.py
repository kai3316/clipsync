"""The update lifecycle, shared by the legacy web panel and the native desktop.

``internal.system.updater`` owns the network primitives (release lookup,
verified download, digest check, peer cache).  This module owns the *lifecycle*
around them: the phase state machine both surfaces render (idle / downloading /
ready / failed), the worker that drives a download to a verified archive, the
periodic silent check, and the reveal action.

Nothing here is auto-applied *by this module*: it verifies an archive and stages
it.  Which of those the host then applies is the host's decision, and both of
this app's hosts answer the same way — a staged archive is installed by itself,
whether it came from a peer or from this machine's own download, because a
machine that has the newer build on disk and is still running the old one is the
state 自动更新 exists to avoid.  The manual paths are still here for the cases
that need them: a platform with no silent installer, an install that failed and
left the card showing a ready archive, and a download the user started by hand.
"""

import contextlib
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
# The silent periodic check gets a shorter bound than the manual one.  Nobody is
# watching it, so a slow answer is worth less than a prompt quit -- and stop()
# must be able to wait it out, which it can only do if the request is bounded by
# something it knows.  Raising this raises the worst-case shutdown delay.
AUTO_CHECK_TIMEOUT = 3.0
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


# Every verdict `updater.verify_update_blob` documents, and the sentence each one earns.
#
# Module level rather than a literal inside `_discard` so a test can hold it against that set: the
# whole point is that no verdict silently borrows another one's words, and that cannot be checked
# from inside the function that does the borrowing.
_REJECTION_KEYS = {
    "hash_mismatch": "notify.update_rejected_hash",
    "not_newer": "notify.update_rejected_old",
    "no_release_info": "notify.update_unverifiable",
}

# A verdict with no sentence of its own -- added to the verifier before it was added here.  It
# must not claim to know which of the three it was, because naming the wrong one sends the reader
# to check the network over a file that was fine.
_REJECTION_UNKNOWN_KEY = "notify.update_rejected_unknown"


def _verdict_is_rejection(verdict: str) -> bool:
    """Whether *verdict* means the receiver did not take the file."""
    return verdict not in ("ok", "peer_verified")


def _rejection_key(verdict: str) -> str:
    """The sentence a verdict earns, or the one that admits it has none."""
    return _REJECTION_KEYS.get(verdict, _REJECTION_UNKNOWN_KEY)


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
        verdict_reporter: Callable[[str, str, str, str], None] | None = None,
    ):
        self._publish = publish
        self._config = config
        self._home = home
        # Told what became of a peer-sent blob: (peer_id, transfer_id, verdict, filename).  The
        # runtime supplies it -- the sending device is the one that can stop offering a file the
        # receiver keeps refusing, and only the runtime can reach it.
        self._verdict_reporter = verdict_reporter
        self._lock = threading.RLock()
        self._state = {
            "phase": "idle",
            "fraction": 0,
            "downloaded": 0,
            "total": 0,
            "error": "",
            "version": "",
            "path": "",
            # Where the archive came from: "github" for this machine's own
            # download, "p2p" for one a peer sent.  The host reads it because
            # the two are installed differently -- a peer-sent archive is the
            # only one whose whole point is that the release endpoint may be
            # out of reach, so it is installed from the file already staged
            # rather than by fetching the same bytes again.
            "source": "",
            # Which digest settled the archive, for the card that draws the
            # ready state: "release" when the published asset's digest matched,
            # "peer_verified" when only the sending device's own digest was
            # available to check against.  Empty while nothing is staged.
            "verified": "",
        }
        self._downloading = False
        self._installing = False
        self._pending_version = ""
        self._last_progress = 0.0
        self._last_auto_check: float | None = None
        self._shutting_down = False
        self._timer: threading.Thread | None = None
        # The tick loop schedules a *separate* thread for the request itself, so
        # that a slow lookup never delays the next tick.  That second thread has
        # to be held here: stop() can only wait for a thread it can name, and a
        # thread left running into interpreter finalization is not a leak but a
        # crash -- see stop().
        self._worker: threading.Thread | None = None
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
            # Travels with the answer so the client can say why there is none.
            # The keys above are a projection, and a projection that drops the
            # reason turns every failure into the same four-word sentence.
            "error": result.get("error", ""),
            # The same failure twice more: as a code, and as the raw line the
            # server or the OS produced.  `error` is what to show a reader --
            # short, and in this app's language -- and these two are what to
            # put in a report about it.
            "reason": result.get("reason", ""),
            "detail": result.get("detail", ""),
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
        self._set_state(
            phase="downloading",
            fraction=0,
            downloaded=0,
            total=0,
            error="",
            source="github",
            verified="",
            signature="",
        )
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

    def finish_from_peer(
        self,
        path: str,
        sha256: str = "",
        peer_id: str = "",
        transfer_id: str = "",
        signature: str = "",
    ) -> None:
        """A peer sent us its cached update asset (M2 P2P update).

        *sha256* is the digest the sending device declared for the file, and it
        is what the bytes are checked against when the release endpoint cannot
        be reached -- see :func:`updater.verify_update_blob`.  Empty for a peer
        too old to declare one.

        *signature* is the release's minisign signature, if the sending device
        had it cached.  Nothing here trusts it: it is kept beside the staged
        archive and in the state so the host can verify it offline against the
        embedded public key before anything runs.

        Runs on the caller's thread: the release lookup inside :meth:`_finish`
        can block, so the runtime hands this off the transfer receive thread.
        """
        self._finish(
            path,
            None,
            "p2p",
            peer_digest=sha256,
            peer_id=peer_id,
            transfer_id=transfer_id,
            signature=signature,
        )

    def _finish(
        self,
        path: str | None,
        reason: str | None,
        source: str = "github",
        peer_digest: str = "",
        peer_id: str = "",
        transfer_id: str = "",
        signature: str = "",
    ) -> None:
        """Verify, cache and stage an arrived asset, or surface the failure."""
        with self._lock:
            if self._installing:
                logger.info(
                    "Update install already in progress — skipping %s arrival", source
                )
                return
        if not path:
            self._set_state(
                phase="failed", error=reason or T("tray.update_install_failed"), source=source
            )
            return

        # The GitHub path was already size- and hash-checked while downloading.
        # A peer-sent blob has nobody to answer to, so it must be checked — a
        # lookup that can block for ~30s and therefore never runs on a UI or
        # transfer thread.
        release_info = None
        if source != "github":
            try:
                release_info = updater.fetch_latest_asset_info()
            except Exception as exc:  # defensive — the helper never raises
                logger.debug("Release info lookup failed: %s", exc)
        ok, verdict = False, "no_release_info"
        try:
            ok, verdict = updater.verify_update_blob(
                path, release_info, __version__, source=source, peer_digest=peer_digest
            )
        except Exception:
            logger.exception("Update verification crashed")
            verdict = "hash_mismatch"
        if not ok:
            if verdict == "no_release_info":
                logger.warning(
                    "Peer-sent update rejected: no published digest and no digest from "
                    "the sending device to check it against"
                )
            self._discard(path, verdict, source)
            self._report_verdict(peer_id, transfer_id, verdict, path)
            return
        if release_info:
            # The digest match pins this blob to that release, so its version is
            # authoritative for the ready card (a peer blob carries no version).
            self._pending_version = str(release_info.get("version", "")) or self._pending_version
        elif not self._pending_version:
            # Checked against the sender's own digest, so there is no release to
            # name the build; the installer's filename carries it, and the card
            # would otherwise show a version-less ready archive.
            self._pending_version = updater.version_in_asset_name(os.path.basename(path))

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
        # The signature travels beside the archive as well as in the state: a
        # person can hand the pair on, and the host's offline check reads the
        # state.  A peer that sent none leaves any stale file removed so it
        # cannot be paired with different bytes later.
        signature = str(signature or "").strip()
        signature_path = f"{dest}.sig"
        try:
            if signature:
                with open(signature_path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(signature)
                updater.cache_signature(os.path.basename(dest), signature)
            else:
                with contextlib.suppress(OSError):
                    os.remove(signature_path)
        except OSError:
            logger.warning("Could not keep the transferred update signature", exc_info=True)
        # The sending device is told the archive was taken.  Reported here rather than at the
        # end of the method so "ok" means the file is on disk and staged, which is what the
        # sender needs to know to stop offering it.
        self._report_verdict(peer_id, transfer_id, verdict, path)
        self._set_state(
            phase="ready",
            version=self._pending_version or "",
            path=dest,
            fraction=1,
            source=source,
            # Which digest settled it, so the card can say what was checked:
            # "release" is the published asset, "peer_verified" is the sending
            # device's own word and worth saying out loud.
            verified="release" if verdict == "ok" else verdict,
            signature=signature,
        )

    def _report_verdict(
        self, peer_id: str, transfer_id: str, verdict: str, path: str | None
    ) -> None:
        """Tell the sender what happened, and never let that failure matter.

        The update has already been staged or discarded by the time this runs, so a reporter that
        raises would report a failure that did not happen.  `_verdict_is_rejection` is the one
        judgement here, and it is shared with anything that wants to read a verdict the same way.
        """
        reporter = self._verdict_reporter
        if reporter is None or not peer_id:
            return
        try:
            reporter(peer_id, transfer_id, verdict, os.path.basename(path or ""))
        except Exception:
            logger.debug("Could not report an update verdict", exc_info=True)

    def _discard(self, path: str, verdict: str, source: str = "github") -> None:
        """Throw away a rejected blob and report why.

        The file goes, every time.  A blob this machine refused to install is
        not something to leave lying in a Downloads folder under the name of a
        release: it is a file a person could double-click, and the answer to
        "why not" is the one thing about it worth keeping.
        """
        for candidate in (path, f"{path}.sig"):
            try:
                os.remove(candidate)
            except OSError:
                logger.debug("Could not remove rejected update file", exc_info=True)
        self._set_state(
            phase="failed",
            error=T(_rejection_key(verdict)),
            source=source,
            signature="",
        )

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
        """Stop the periodic check and wait for the request it may have started.

        "In-flight daemon workers die with the app" is true of the thread and
        false of the process.  CPython finalizes the interpreter underneath a
        daemon thread that is still inside a blocking C call, and tearing the
        `ssl` module down under a live request is a SIGSEGV rather than a clean
        exit -- which is what turned an ordinary quit into a crash on any host
        whose uptime let the first automatic check fire.  So the request thread
        is waited for instead.  That is affordable only because the silent
        lookup is bounded by AUTO_CHECK_TIMEOUT, not by the manual CHECK_TIMEOUT.
        """
        self._shutting_down = True
        self._wake.set()
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.join(timeout=1.0)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=AUTO_CHECK_TIMEOUT + 0.5)

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
        # Held on the instance so stop() can wait for it.  Overwriting a handle
        # is safe here only because the throttle above guarantees the previous
        # request is six hours gone.
        worker = threading.Thread(
            target=self._auto_check_worker, name="auto-update-request", daemon=True
        )
        self._worker = worker
        worker.start()
        return True

    def _auto_check_worker(self) -> None:
        """Silent check: fetch and stage what it finds, or say nothing.

        The check stops at the announcement: it publishes ``update.available`` and
        leaves the download to a click in the update card.

        It used to continue -- the check found a build, the worker fetched it, and
        the host installed what :meth:`_finish` staged, so nobody had to look at a
        page.  That is the right shape on a network where the release server is
        fast and the wrong one where it is not: reported from a network in China,
        an automatic download spends minutes on a transfer that may time out
        anyway, and the timeout delays the peer that already has the file.

        A peer on the same LAN is a different matter and is still fetched without
        being asked, by the runtime rather than by this loop: it answers in
        seconds, and it is the source that works when the release server does not.
        """
        try:
            result = updater.check_for_update(timeout=AUTO_CHECK_TIMEOUT)
        except Exception as exc:
            logger.debug("Auto update check failed: %s", exc)
            return
        finally:
            # Release the handle only if stop() has not already taken it, so a
            # request that finishes during shutdown is not re-joined.
            if self._worker is threading.current_thread():
                self._worker = None
        if not result.get("available") or self._shutting_down:
            return
        self._notify_available(result)
        # Announced, not fetched.  The download is a click in the update card.
        logger.info(
            "Automatic update check found %s; it can be downloaded from the update card",
            result.get("latest", ""),
        )
