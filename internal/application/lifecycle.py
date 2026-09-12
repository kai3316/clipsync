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


class ApplicationLifecycle:
    def __init__(self, resources: list[Resource]):
        self._resources = tuple(resources)
        self._started: list[Resource] = []
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
                    # Register before start: a resource may fail after acquiring a handle.
                    self._started.append(resource)
                    resource.start()
            except BaseException:
                self.stop()
                raise
            self.state = "running"

    def stop(self) -> bool:
        with self._lock:
            if self.state == "stopped":
                return True
            self.state = "stopping"
            while self._started:
                resource = self._started[-1]
                try:
                    if resource.stop() is False:
                        return False
                except Exception:
                    logger.exception("Resource shutdown failed: %s", resource.name)
                    if resource.blocks_dependencies:
                        return False
                self._started.pop()
            self.state = "stopped"
            return True
