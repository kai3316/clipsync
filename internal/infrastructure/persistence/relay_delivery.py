"""Crash-safe offline Relay delivery queue compatible with the legacy format."""

from __future__ import annotations

import base64
import json
import os
from collections import OrderedDict
from pathlib import Path
from threading import RLock


class RelayDeliveryQueue:
    def __init__(self, path: Path, max_per_peer: int = 100):
        self.path = Path(path)
        self.max_per_peer = max_per_peer
        self._lock = RLock()
        self._peers: dict[str, OrderedDict[str, dict]] = {}
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            peers = payload.get("peers", {}) if isinstance(payload, dict) else {}
            if not isinstance(peers, dict):
                return
            with self._lock:
                for peer, rows in peers.items():
                    if not isinstance(peer, str) or not isinstance(rows, dict):
                        continue
                    valid = OrderedDict()
                    for msg_id, row in rows.items():
                        if isinstance(msg_id, str) and isinstance(row, dict):
                            valid[msg_id] = row
                    self._peers[peer] = valid
        except (OSError, ValueError, TypeError):
            return

    def enqueue(self, peer_id: str, msg_id: str, frame: bytes, **meta):
        """Persist a frame for later delivery.

        Returns the row evicted by the per-peer cap (or None): the caller
        reports it as failed, so a tracked send is never dropped silently.
        """
        if not peer_id or not msg_id:
            return None
        with self._lock:
            queue = self._peers.setdefault(peer_id, OrderedDict())
            if msg_id in queue:
                return None
            queue[msg_id] = {
                "msg_id": msg_id,
                "ts": meta.get("ts"),
                "retries": 0,
                "kind": meta.get("kind", "clipboard"),
                "content_hash": meta.get("content_hash", ""),
                "preview": meta.get("preview", ""),
                "session_id": meta.get("session_id", ""),
                "frame_b64": base64.b64encode(frame).decode("ascii"),
            }
            evicted = None
            while len(queue) > self.max_per_peer:
                _msg_id, evicted = queue.popitem(last=False)
            self.persist()
            return evicted

    def items(self, peer_id: str):
        with self._lock:
            return list(self._peers.get(peer_id, {}).items())

    def has(self, peer_id: str, msg_id: str) -> bool:
        with self._lock:
            return msg_id in self._peers.get(peer_id, {})

    def bump_retries(self, peer_id: str, msg_id: str) -> int:
        """Count one failed republish; 0 when the row is already gone."""
        with self._lock:
            row = self._peers.get(peer_id, {}).get(msg_id)
            if row is None:
                return 0
            row["retries"] = int(row.get("retries", 0) or 0) + 1
            self.persist()
            return row["retries"]

    def counts(self) -> dict[str, int]:
        """Queued message count per peer, omitting peers with an empty queue."""
        with self._lock:
            return {peer: len(queue) for peer, queue in self._peers.items() if queue}

    def remove(self, peer_id: str, msg_id: str):
        with self._lock:
            queue = self._peers.get(peer_id)
            if not queue or msg_id not in queue:
                return
            del queue[msg_id]
            if not queue:
                self._peers.pop(peer_id, None)
            self.persist()

    def clear_peer(self, peer_id: str):
        with self._lock:
            self._peers.pop(peer_id, None)
            self.persist()

    def persist(self):
        # Nothing queued and no stale file to clear: a fresh install must not
        # create an empty relay_pending.json.
        if not self._peers and not self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": 1, "peers": self._peers}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
