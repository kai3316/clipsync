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

import contextlib
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


def _quarantine(path: Path) -> None:
    """Keep a copy of an unreadable conversation instead of overwriting it.

    Called only on the load path.  Best-effort by design: a conversation that
    cannot be read is worth preserving, but not worth failing the launch over.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        if path.exists():
            os.replace(path, target)
            logger.warning("Phone chat file kept aside as %s", target.name)
    except OSError:
        logger.warning("Could not quarantine the phone chat file", exc_info=True)


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
            raise ValueError("phone chat file has no message list")
        except FileNotFoundError:
            pass
        except Exception:
            # Quarantined, not overwritten: starting empty and then appending
            # replaced the unreadable file with a one-message conversation, so
            # the user's history was gone with nothing left to try to recover.
            # The history DB keeps the same kind of copy; this one is sized to
            # what this file can hold.
            logger.warning("Phone chat file could not be read; starting empty", exc_info=True)
            _quarantine(self._path)
        return {"next_id": 1, "messages": []}

    def _persist(self, state: dict) -> None:
        """Write *state* over the file atomically, or raise.

        Callers pass the state they *want*, write it, and only then make it the
        live one.  Persisting the live object first — which is what this used
        to do — left the in-memory conversation one message ahead of the disk
        forever when the write failed: the sender was told the send failed, a
        retry appended a second copy, and a restart lost the first.
        """
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps(state, ensure_ascii=False)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise

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
            # Persisted before it is live: on a failed write the id is not
            # spent, nothing is appended, and the caller's error is the truth —
            # the message the user is told did not send really did not.
            candidate = {
                "next_id": self._state["next_id"] + 1,
                "messages": [*self._state["messages"], message][-MAX_MESSAGES:],
            }
            self._persist(candidate)
            self._state = candidate
            return dict(message)

    def messages(self, after_id: int = 0, reader: str = "") -> dict:
        with self._lock:
            state = self._state
            if reader:
                # Reading is what the reader saw; both clients poll, so a
                # message's read flag means "the other side has polled past it".
                # Built on a copy so a failed write leaves the badges alone:
                # the old code marked the live objects first and then saved, so
                # a full disk showed everything read until the next restart put
                # it all back to unread.
                marked = [
                    {**message, "read": True}
                    if message.get("sender") != reader and not message.get("read")
                    else message
                    for message in self._state["messages"]
                ]
                if marked != self._state["messages"]:
                    # Persisted, because the unread badge is the thing the
                    # reader looks at and a sidecar restart used to put every
                    # message back to unread — the flag lived only in memory.
                    candidate = {"next_id": self._state["next_id"], "messages": marked}
                    self._persist(candidate)
                    state = candidate
                    self._state = candidate
            items = [
                dict(m) for m in state["messages"]
                if int(m.get("id", 0)) > int(after_id)
            ]
            return {
                "messages": items,
                "last_id": state["next_id"] - 1,
            }

    def clear(self) -> None:
        """Empty the conversation, file included; used by the factory reset.

        Deleting ``phone_chat.json`` from the reset is not enough on its own:
        this store is one object per process, holds the whole conversation in
        memory, and writes it back on the next ``append`` or read-mark.  A
        late poll arriving between the unlink and the restart therefore put the
        deleted conversation straight back on disk.  Emptying the live object
        under its own lock closes that window.
        """
        with self._lock:
            self._state = {"next_id": 1, "messages": []}
            for path in (self._path, self._path.with_name(self._path.name + ".tmp")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove %s", path, exc_info=True)


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


def reset_phone_chat_store() -> None:
    """Empty the process-wide conversation; the factory reset calls this.

    Kept as a function rather than a method on the caller so the singleton
    stays private to this module: the point of the reset is that nothing
    outside can hold a second copy of the conversation and write it back.
    """
    global _store
    with _store_lock:
        if _store is not None:
            _store.clear()
    logger.info("Phone chat cleared for the factory reset")
