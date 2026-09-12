"""Bounded replay of application notifications, never the source of business state."""

import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


class EventJournal:
    def __init__(self, capacity: int = 256):
        self.session_id = str(uuid.uuid4())
        self._sequence = 0
        self._events: deque[dict] = deque(maxlen=capacity)
        self._listeners: list[Callable[[str, dict], None]] = []
        self._lock = threading.RLock()

    def subscribe(self, listener: Callable[[str, dict], None]) -> Callable[[], None]:
        """Call *listener* with ``(name, data)`` after every publish.

        The journal itself stays a replay buffer — this is how a live consumer
        that is not the UI event stream (the phone's WebSocket pushes) taps it
        without polling.  The listener runs outside the journal lock, so a slow
        or raising one can neither corrupt the buffer nor stall a publisher;
        a raising listener is logged and skipped.  Returns an unsubscribe
        callable.
        """
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def publish(self, name: str, data: dict, correlation_id: str = "") -> None:
        with self._lock:
            self._sequence += 1
            self._events.append(
                {
                    "type": "event",
                    "name": name,
                    "session_id": self.session_id,
                    "seq": self._sequence,
                    "event_id": str(uuid.uuid4()),
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "correlation_id": correlation_id,
                    "schema_version": 1,
                    "data": deepcopy(data),
                }
            )
            listeners = list(self._listeners)
        self._notify(listeners, name, data)

    def _notify(self, listeners, name, data) -> None:
        for listener in listeners:
            try:
                listener(name, data)
            except Exception:
                logger.exception("Event listener failed for %s", name)

    def snapshot(self, read: Callable[[], dict]) -> dict:
        with self._lock:
            return {"session_id": self.session_id, "seq": self._sequence, **read()}

    def since(self, sequence: int) -> tuple[list[dict], bool]:
        with self._lock:
            oldest = self._events[0]["seq"] if self._events else self._sequence + 1
            gap = sequence < oldest - 1
            return [deepcopy(event) for event in self._events if event["seq"] > sequence], gap
