"""Explicit ownership and partial-start rollback, without a GUI event loop."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resource:
    name: str
    start: Callable[[], None]
    stop: Callable[[], bool | None]
    blocks_dependencies: bool = False
    # Start this resource on a thread of its own and let the rest of the
    # lifecycle run on.  For subsystems whose startup cost the window does not
    # depend on -- the LAN runtime's mDNS work, the phone companion's firewall
    # and port work -- the cost belongs beside the path that answers the host's
    # readiness, not in front of it.  A deferred start that fails is released
    # and logged (see _start_deferred); it cannot be raised out of start(),
    # because nothing upstream is waiting on it to catch that.
    defer: bool = False


class ApplicationLifecycle:
    def __init__(self, resources: list[Resource]):
        self._resources = tuple(resources)
        self._started: list[Resource] = []
        self._workers: list[threading.Thread] = []
        self._lock = threading.RLock()
        self.state = "created"

    def start(self) -> None:
        with self._lock:
            if self.state == "running":
                return
            if self.state != "created":
                raise RuntimeError("A stopped lifecycle cannot be restarted")
            self.state = "starting"
        try:
            for resource in self._resources:
                if resource.defer:
                    self._start_deferred(resource)
                else:
                    # Registered before its start runs: a resource can acquire a
                    # handle and then fail, and a rollback that skipped it would
                    # leave that handle owned by nobody.
                    with self._lock:
                        self._started.append(resource)
                    try:
                        resource.start()
                    except BaseException:
                        # Its own stop releases what the failed start acquired.
                        # Only this one -- whatever was already running is torn
                        # down by the stop() below, in reverse order.
                        try:
                            resource.stop()
                        except Exception:
                            logger.exception(
                                "Cleanup after a failed start failed: %s", resource.name
                            )
                        with self._lock:
                            if self._started and self._started[-1] is resource:
                                self._started.pop()
                        raise
        except BaseException:
            self.stop()
            raise
        with self._lock:
            self.state = "running"

    def _start_deferred(self, resource: Resource) -> None:
        """Start a resource out of band, so it cannot hold up readiness."""

        def run() -> None:
            try:
                resource.start()
            except Exception as error:
                logger.exception("Deferred resource failed to start: %s", resource.name)
                # Nothing upstream can catch this, so release whatever the
                # failed start acquired rather than leaving it behind.  Both
                # stop callbacks used here are idempotent.
                try:
                    resource.stop()
                except Exception:
                    logger.exception(
                        "Cleanup after a failed start failed: %s", resource.name
                    )
                self._on_deferred_failure(resource, error)
                return
            with self._lock:
                self._started.append(resource)

        worker = threading.Thread(
            target=run,
            name="clipsync-start-" + resource.name.replace(" ", "-"),
            daemon=True,
        )
        with self._lock:
            self._workers.append(worker)
        worker.start()

    def _on_deferred_failure(self, resource: Resource, error: BaseException) -> None:
        """Hook for a subclass that can report a background start failure.

        The base implementation has nowhere to report to; it has already logged
        and released the resource by the time this runs.
        """
        return None

    def stop(self) -> bool:
        # A deferred start owns handles as soon as it begins, while stop()
        # walks what it owns in reverse.  Let every one of them finish first,
        # or teardown can run against a half-built subsystem.
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join()

        with self._lock:
            if self.state == "stopped":
                return True
            self.state = "stopping"
            while self._started:
                resource = self._started[-1]
                try:
                    if resource.stop() is False:
                        # Naming it is what makes this useful: "a subsystem
                        # refused" says nothing about which one, and the log is
                        # often the only account a packaged app leaves behind.
                        logger.warning(
                            "Resource did not release ownership: %s", resource.name
                        )
                        return False
                except Exception:
                    logger.exception("Resource shutdown failed: %s", resource.name)
                    if resource.blocks_dependencies:
                        return False
                self._started.pop()
            self.state = "stopped"
            return True
