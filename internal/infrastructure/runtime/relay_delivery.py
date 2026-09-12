"""Send ledger + offline retry policy for relayed clipboard frames.

The legacy host kept two structures behind one lock: an in-memory ledger of
recent sends per peer (``sent`` → ``delivered`` / ``failed``, with the offline
queue mirrored in as ``queued``) and the persisted queue itself.  This module
carries those semantics out of the host so the LAN runtime owns exactly what
the legacy ``_delivery_*`` methods did — the ACK window, the retry budget, the
retry cadence and the eviction/clear rules — and the queue stays a dumb
crash-safe store.

Semantics worth keeping in mind:

* A relayed clipboard frame is ``sent`` and waits :data:`ACK_WINDOW` for the
  peer's ``relay_ack``.  A publish that could not be handed to the broker is
  ``queued`` instead and persisted.
* A send whose window elapses is ``failed`` — unless the same content already
  reached that peer, which counts as delivered (content-level ack fallback).
* Queued rows are republished on relay-online, on any frame from that peer,
  and on the periodic retry; past :data:`MAX_RETRIES` a row is failed and
  dropped.
* Nothing is dropped silently: both a retry-budget failure and a queue-cap
  eviction are reported as ``failed``.
"""

from __future__ import annotations

import logging
import time
from base64 import b64decode
from collections import OrderedDict
from threading import RLock

logger = logging.getLogger(__name__)

ACK_WINDOW = 15.0  # how long a relayed send waits for its ack
SCAN_INTERVAL = 2.0  # timeout sweep cadence
RETRY_INTERVAL = 60.0  # periodic offline-queue retry cadence
MAX_RETRIES = 5  # per-message retry budget before the row is failed
LEDGER_MAX = 50  # ledger rows kept per peer
STATUS_ROWS = 20  # rows the delivery API reports


class RelayDelivery:
    """Ledger + retry policy over a :class:`RelayDeliveryQueue`.

    *publish* is called as ``publish(frame_bytes, peer_id) -> bool`` and must
    return True only when the frame really reached the broker.  *notify* is
    called as ``notify(peer_id, msg_id, status, content_hash, kind,
    session_id)`` on every ledger transition, outside the lock.
    """

    def __init__(
        self,
        queue,
        publish,
        notify,
        *,
        clock=time.time,
        ack_window=ACK_WINDOW,
        ledger_max=LEDGER_MAX,
        max_retries=MAX_RETRIES,
    ):
        self.queue = queue
        self._publish = publish
        self._notify = notify
        self._clock = clock
        self.ack_window = ack_window
        self.ledger_max = ledger_max
        self.max_retries = max_retries
        self._lock = RLock()
        self._ledger: dict[str, OrderedDict[str, dict]] = {}
        # Legacy ran the first sweep one interval after startup: the relay's
        # own online transition (and any peer frame) is the startup trigger.
        self._scan_due = self._clock() + SCAN_INTERVAL
        self._retry_due = self._clock() + RETRY_INTERVAL
        self._seed_from_queue()

    # ------------------------------------------------------------- ledger API

    def note_sent(self, peer_id, msg_id, content_hash="", preview="", kind="clipboard",
                  session_id=""):
        """Record a freshly published send (awaits its relay ack)."""
        if not peer_id or not msg_id:
            return
        now = self._clock()
        with self._lock:
            delivered = bool(content_hash) and self._content_delivered(
                peer_id, content_hash, skip_msg_id=msg_id
            )
            self._ledger.setdefault(peer_id, OrderedDict())[msg_id] = self._row(
                msg_id, content_hash, now,
                "delivered" if delivered else "sent",
                None if delivered else now + self.ack_window,
                preview, kind, session_id,
            )
            # Defensive: a message is tracked in the queue or the ledger, never both.
            self.queue.remove(peer_id, msg_id)
            self._bounded(peer_id)
        self._emit(peer_id, msg_id, "delivered" if delivered else "sent",
                   content_hash, kind, session_id)

    def enqueue(self, peer_id, msg_id, content_hash, preview, frame, kind="clipboard",
                session_id=""):
        """Persist a clipboard frame that could not be published right now."""
        if not peer_id or not msg_id:
            return
        with self._lock:
            if self.queue.has(peer_id, msg_id):
                return  # already queued
            now = self._clock()
            evicted = self.queue.enqueue(
                peer_id, msg_id, frame, ts=now, kind=kind,
                content_hash=content_hash, preview=preview, session_id=session_id,
            )
            self._ledger.setdefault(peer_id, OrderedDict())[msg_id] = self._row(
                msg_id, content_hash, now, "queued", None, preview, kind, session_id
            )
            self._bounded(peer_id)
        if evicted is not None:
            self._failed(peer_id, evicted, reason="queue cap")
        self._emit(peer_id, msg_id, "queued", content_hash, kind, session_id)

    def note_ack(self, peer_id, msg_id):
        """Route an incoming ``relay_ack`` receipt to its ledger row."""
        if not peer_id or not msg_id:
            return
        with self._lock:
            entry = (self._ledger.get(peer_id) or {}).get(msg_id)
            if entry is None or entry.get("status") == "delivered":
                return  # unknown msg_id / already settled — ignore
            entry["status"] = "delivered"
            entry["deadline"] = None
            content_hash = entry.get("content_hash", "")
            kind = entry.get("kind", "clipboard")
            session_id = entry.get("session_id", "")
            self.queue.remove(peer_id, msg_id)
        self._emit(peer_id, msg_id, "delivered", content_hash, kind, session_id)

    def scan(self):
        """Fail every sent row whose ack window elapsed."""
        now = self._clock()
        with self._lock:
            expired = [
                (peer_id, msg_id)
                for peer_id, ledger in list(self._ledger.items())
                for msg_id, entry in list(ledger.items())
                if entry.get("status") == "sent"
                and entry.get("deadline")
                and entry["deadline"] <= now
            ]
        for peer_id, msg_id in expired:
            self._mark_timeout(peer_id, msg_id)

    # -------------------------------------------------------------- retrying

    def retry_peer(self, peer_id):
        """Republish every queued frame for one peer."""
        if not peer_id:
            return
        for msg_id, entry in self.queue.items(peer_id):
            if not self.queue.has(peer_id, msg_id):
                continue  # changed concurrently — leave it alone
            try:
                frame = b64decode(entry.get("frame_b64", "") or "")
            except Exception:
                # An unreadable payload can never be republished: drop it.
                logger.debug("delivery payload undecodable", exc_info=True)
                self.queue.remove(peer_id, msg_id)
                self._failed(peer_id, entry, reason="undecodable payload")
                continue
            ok = self._publish_safely(frame, peer_id, entry)
            with self._lock:
                if not self.queue.has(peer_id, msg_id):
                    continue  # acked while we were publishing
                if ok:
                    self.queue.remove(peer_id, msg_id)
                    now = self._clock()
                    self._ledger.setdefault(peer_id, OrderedDict())[msg_id] = self._row(
                        msg_id, entry.get("content_hash", ""), now, "sent",
                        now + self.ack_window, entry.get("preview", ""),
                        entry.get("kind", "clipboard"), entry.get("session_id", ""),
                    )
                    status = "sent"
                else:
                    retries = self.queue.bump_retries(peer_id, msg_id)
                    if retries >= self.max_retries:
                        self.queue.remove(peer_id, msg_id)
                        self._fail_locked(peer_id, msg_id, entry)
                        status = "failed"
                    else:
                        continue
                content_hash = entry.get("content_hash", "")
                kind = entry.get("kind", "clipboard")
                session_id = entry.get("session_id", "")
            self._emit(peer_id, msg_id, status, content_hash, kind, session_id)

    def retry_all(self):
        """Retry the whole offline queue (relay-online and the periodic sweep)."""
        for peer_id in self.queue.counts():
            try:
                self.retry_peer(peer_id)
            except Exception:
                logger.debug("delivery retry for %s failed", str(peer_id)[:12], exc_info=True)

    def tick(self):
        """Cadence-gated sweep: 2s timeout scan, 60s queue retry."""
        now = self._clock()
        if now >= self._scan_due:
            self._scan_due = now + SCAN_INTERVAL
            try:
                self.scan()
            except Exception:
                logger.debug("delivery scan failed", exc_info=True)
        if now >= self._retry_due:
            self._retry_due = now + RETRY_INTERVAL
            try:
                self.retry_all()
            except Exception:
                logger.debug("delivery queue retry failed", exc_info=True)

    # -------------------------------------------------------------- reporting

    def status(self, peer_id=""):
        """Delivery view: ``{"pending": N, "items": [row, ...]}``, newest first."""
        with self._lock:
            if peer_id:
                return {
                    "pending": len(self.queue.items(peer_id)),
                    "items": self._render(self._ledger.get(peer_id) or {}, peer_id),
                }
            rows = []
            pending = 0
            for pid, ledger in self._ledger.items():
                pending += len(self.queue.items(pid))
                rows.extend(self._render(ledger, pid))
            rows.sort(key=lambda row: float(row.get("ts") or 0.0), reverse=True)
            return {"pending": pending, "items": rows[:STATUS_ROWS]}

    def counts(self):
        """Per-peer queued counts for device-page badges."""
        return {"peers": self.queue.counts()}

    def clear_peer(self, peer_id):
        """Drop a peer's ledger + queue (unpair/forget) and re-persist."""
        if not peer_id:
            return
        with self._lock:
            self._ledger.pop(peer_id, None)
        self.queue.clear_peer(peer_id)

    # ---------------------------------------------------------------- private

    def _seed_from_queue(self):
        """Queue rows loaded from disk re-enter the ledger as ``queued``."""
        for peer_id in self.queue.counts():
            for msg_id, row in self.queue.items(peer_id):
                self._ledger.setdefault(peer_id, OrderedDict())[msg_id] = self._row(
                    msg_id, row.get("content_hash", ""), row.get("ts") or self._clock(),
                    "queued", None, row.get("preview", ""),
                    row.get("kind", "clipboard"), row.get("session_id", ""),
                )

    @staticmethod
    def _row(msg_id, content_hash, ts, status, deadline, preview, kind, session_id):
        return {
            "msg_id": msg_id,
            "content_hash": content_hash,
            "ts": ts,
            "status": status,
            "deadline": deadline,
            "preview": preview,
            "kind": kind,
            "session_id": session_id,
        }

    def _render(self, ledger, peer_id):
        """API rows for one peer's ledger, newest first, capped like legacy."""
        queued = {msg_id: row for msg_id, row in self.queue.items(peer_id)}
        rows = []
        for msg_id, entry in ledger.items():
            rows.append({
                "msg_id": msg_id,
                "ts": entry.get("ts") or self._clock(),
                "status": entry.get("status", "sent"),
                "preview": entry.get("preview", ""),
                "content_hash": entry.get("content_hash", ""),
                "kind": entry.get("kind", "clipboard"),
                "session_id": entry.get("session_id", ""),
                "retries": int((queued.get(msg_id) or {}).get("retries", 0) or 0),
            })
        rows.sort(key=lambda row: float(row.get("ts") or 0.0), reverse=True)
        return rows[:STATUS_ROWS]

    def _content_delivered(self, peer_id, content_hash, skip_msg_id=""):
        """True when the same content already reached *peer_id*.

        Content-level ack fallback: a redelivery of content already confirmed
        delivered must not be re-reported as failed just because its own ack
        never came back.
        """
        for entry in (self._ledger.get(peer_id) or {}).values():
            if entry.get("msg_id") == skip_msg_id:
                continue
            if entry.get("content_hash") == content_hash and entry.get("status") == "delivered":
                return True
        return False

    def _mark_timeout(self, peer_id, msg_id):
        """Ack window expired: fail the send (unless content-level ack)."""
        with self._lock:
            entry = (self._ledger.get(peer_id) or {}).get(msg_id)
            if entry is None or entry.get("status") != "sent":
                return
            content_hash = entry.get("content_hash", "")
            if content_hash and self._content_delivered(peer_id, content_hash, skip_msg_id=msg_id):
                entry["status"] = "delivered"
                status = "delivered"
            else:
                entry["status"] = "failed"
                status = "failed"
            entry["deadline"] = None
            kind = entry.get("kind", "clipboard")
            session_id = entry.get("session_id", "")
        self._emit(peer_id, msg_id, status, content_hash, kind, session_id)

    def _failed(self, peer_id, row, reason=""):
        """Mark a dropped queue row failed so a tracked send is never lost."""
        msg_id = row.get("msg_id", "")
        if not msg_id:
            return
        logger.debug("Relay delivery dropped %s for %s: %s", msg_id[:12], peer_id[:12], reason)
        with self._lock:
            self._fail_locked(peer_id, msg_id, row)
        self._emit(peer_id, msg_id, "failed", row.get("content_hash", ""),
                   row.get("kind", "clipboard"), row.get("session_id", ""))

    def _fail_locked(self, peer_id, msg_id, row):
        """Ledger half of :meth:`_failed`; caller must hold the lock."""
        ledger = self._ledger.setdefault(peer_id, OrderedDict())
        if msg_id in ledger:
            ledger[msg_id]["status"] = "failed"
            ledger[msg_id]["deadline"] = None
        else:
            ledger[msg_id] = self._row(
                msg_id, row.get("content_hash", ""), row.get("ts") or self._clock(),
                "failed", None, row.get("preview", ""),
                row.get("kind", "clipboard"), row.get("session_id", ""),
            )

    def _bounded(self, peer_id):
        """Keep the ledger within its per-peer cap (the API shows the newest)."""
        ledger = self._ledger.get(peer_id)
        if ledger:
            while len(ledger) > self.ledger_max:
                ledger.popitem(last=False)

    def _publish_safely(self, frame, peer_id, entry):
        try:
            return bool(self._publish(frame, peer_id))
        except Exception:
            logger.debug("delivery republish to %s failed", str(peer_id)[:12], exc_info=True)
            return False

    def _emit(self, peer_id, msg_id, status, content_hash, kind, session_id):
        try:
            self._notify(peer_id, msg_id, status, content_hash, kind, session_id)
        except Exception:
            logger.debug("delivery notification failed", exc_info=True)
