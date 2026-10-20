"""A small, durable conversation between the phone companion and this machine.

The existing nearby-chat system is peer-to-peer: a session belongs to a paired
device that is connected over the LAN/relay transport, and the computer is one
of its endpoints.  The phone is not a paired peer -- it is a remote view of the
computer -- so "chat with the computer" needs its own little store rather than a
fake transport session.

Messages are text only, capped, and kept in one JSON file beside config.json.
The file is small by construction (500 messages, 4000 characters each); it is
not the clipboard history, and a corrupt file is replaced with an empty one
rather than blocking the app.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_MESSAGES = 500
MAX_TEXT = 4000
SENDERS = ("phone", "desktop")


class PhoneChatStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                return {
                    "next_id": int(data.get("next_id", 1)),
                    "messages": [
                        message for message in data["messages"]
                        if isinstance(message, dict) and message.get("text")
                    ][-MAX_MESSAGES:],
                }
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Phone chat file could not be read; starting empty", exc_info=True)
        return {"next_id": 1, "messages": []}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def append(self, sender: str, text: str) -> dict:
        sender = sender if sender in SENDERS else "desktop"
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("empty message")
        with self._lock:
            message = {
                "id": self._state["next_id"],
                "sender": sender,
                "text": text,
                "ts": time.time(),
                "read": False,
            }
            self._state["next_id"] += 1
            self._state["messages"].append(message)
            self._state["messages"] = self._state["messages"][-MAX_MESSAGES:]
            self._save()
            return dict(message)

    def messages(self, after_id: int = 0, reader: str = "") -> dict:
        with self._lock:
            marked = False
            if reader:
                # Reading is what the reader saw; both clients poll, so a
                # message's read flag means "the other side has polled past it".
                for message in self._state["messages"]:
                    if message.get("sender") != reader and not message.get("read"):
                        message["read"] = True
                        marked = True
            # Persisted, because the unread badge is the thing the reader looks
            # at and a sidecar restart used to put every message back to
            # unread — the flag lived only in memory.
            if marked:
                self._save()
            items = [
                dict(m) for m in self._state["messages"]
                if int(m.get("id", 0)) > int(after_id)
            ]
            return {
                "messages": items,
                "last_id": self._state["next_id"] - 1,
            }


_store: PhoneChatStore | None = None
_store_lock = threading.Lock()


def get_phone_chat_store(path: Path | None = None) -> PhoneChatStore:
    """One store per process, so HTTP routes and RPC share the same lock."""
    global _store
    with _store_lock:
        if _store is None:
            if path is None:
                from internal.config.config import config_dir

                path = config_dir() / "phone_chat.json"
            _store = PhoneChatStore(path)
        return _store
