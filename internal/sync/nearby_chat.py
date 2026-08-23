"""Consent-gated nearby chat between devices that are NOT paired.

ClipSync's clipboard sync requires pairing (8-digit code + cert pinning).
This module adds a lightweight, explicitly consensual channel for devices
that merely discovered each other on the LAN:

* One device sends a ``chat_invite``; nothing at all flows until the other
  user explicitly accepts.
* Accepted sessions exchange short text messages and files (chunked with the
  same binary-chunk framing clipboard file transfers use).
* Every stage is gated: rate-limited invitations, per-session text flood
  control, per-file user acceptance, sanitized file names confined to the
  receive directory, and hard size caps.

Security model
--------------
Unpaired TLS sessions are encrypted but *not* authenticated against a pinned
certificate (trust-on-first-use at best).  This channel therefore treats
every remote peer as untrusted until a human accepts the invitation, and
surfaces a short certificate fingerprint in the UI so users can visually
compare codes out-of-band.  All application-level gates live here -- the
transport only whitelists ``CHAT_MSG_TYPES`` frames through for unpaired
peers; content rules are enforced in :meth:`ChatManager.handle_message`.

Threading
---------
All public methods are thread-safe (one re-entrant lock around state).
Every registered callback fires on a *worker* thread (recv thread, heartbeat
thread, or a transfer thread) -- UI layers MUST marshal onto their own main
loop (Tk: ``root.after(0, ...)``).
"""

import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from internal.protocol.codec import (
    CHAT_MSG_TYPES,
    encode_binary_chunk,
    encode_frame,
)
# Same-package reuse: received-file names MUST be sanitized exactly like
# clipboard file transfers, so share the one implementation.
from internal.sync.file_transfer import MAX_FILE_SIZE, _sanitize_file_name

logger = logging.getLogger(__name__)

SendFn = Callable[[bytes], Any]


def _default_receive_dir() -> Path:
    return Path.home() / "Downloads" / "ClipSync" / "Chat"


def _safe_remove(path: Path | None) -> None:
    """Best-effort removal of a temp file; never raises."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove temp file %s", path, exc_info=True)


@dataclass
class ChatEntry:
    """One row in a conversation: text, file card, or system notice."""

    entry_id: str
    kind: str                      # "text" | "file" | "system"
    outgoing: bool
    ts: float
    text: str = ""
    text_key: str = ""             # i18n key for system entries (never literals)
    fmt: dict = field(default_factory=dict)
    file_name: str = ""
    file_size: int = 0
    mime: str = ""
    status: str = "pending"        # pending|await_accept|sending|done|failed|declined|cancelled
    fraction: float = 0.0
    saved_path: str = ""
    transfer_id: str = ""

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "outgoing": self.outgoing,
            "ts": self.ts,
            "text": self.text,
            "text_key": self.text_key,
            "fmt": dict(self.fmt),
            "file_name": self.file_name,
            "file_size": self.file_size,
            "mime": self.mime,
            "status": self.status,
            "fraction": self.fraction,
            "saved_path": self.saved_path,
            "transfer_id": self.transfer_id,
        }


@dataclass
class ChatSession:
    """Conversation state for one remote peer (at most one live session)."""

    session_id: str                # 16-hex, minted by the inviter, echoed by the acceptor
    peer_id: str
    peer_name: str
    fingerprint_short: str         # <=32 chars, displayed for out-of-band comparison
    status: str                    # inviting|invited|active|declined_remote|closed
    created_ts: float
    last_seen_mono: float = 0.0    # time.monotonic() of last inbound frame
    last_activity_ts: float = 0.0
    unread: int = 0
    online: bool = True
    entries: list[ChatEntry] = field(default_factory=list)
    last_ping_mono: float = 0.0
    offline_announced: bool = False

    def last_preview(self, limit: int = 60) -> str:
        for entry in reversed(self.entries):
            if entry.kind == "text":
                return entry.text.replace("\n", " ").strip()[:limit]
            if entry.kind == "file":
                arrow = "↑ " if entry.outgoing else "↓ "
                return (arrow + entry.file_name)[:limit]
        return ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "peer_id": self.peer_id,
            "peer_name": self.peer_name,
            "fingerprint_short": self.fingerprint_short,
            "status": self.status,
            "created_ts": self.created_ts,
            "last_activity_ts": self.last_activity_ts,
            "unread": self.unread,
            "online": self.online,
            "last_preview": self.last_preview(),
        }


class ChatManager:
    """State machine + wire handlers for consent-gated nearby chat.

    Wire payloads (JSON frames built with
    :func:`internal.protocol.codec.encode_frame`):

    - ``chat_invite``  ``{session_id, from_name, fingerprint_short, greeting}``
    - ``chat_accept`` / ``chat_close``  ``{session_id}``
    - ``chat_decline`` ``{session_id, reason}``
    - ``chat_text``    ``{session_id, text, ts}``
    - ``chat_ping`` / ``chat_pong``  ``{session_id}``
    - ``chat_file_offer``  ``{session_id, transfer_id, file_name, file_size, mime}``
    - ``chat_file_accept`` / ``chat_file_reject`` / ``chat_file_cancel``
      / ``chat_file_complete``  ``{session_id, transfer_id[, status]}``

    File bytes ride the existing compact binary chunk framing
    (:func:`encode_binary_chunk`); the host router offers every decoded
    ``file_chunk`` frame to :meth:`handle_binary_chunk` FIRST and falls back
    to clipboard file transfers when it returns ``False``.
    """

    # ---- anti-abuse caps ---------------------------------------------------
    INVITE_RATE_LIMIT = 5             # invites per peer (each direction) per window
    INVITE_RATE_WINDOW = 300.0
    TEXT_RATE_LIMIT = 30              # texts per active session per window
    TEXT_RATE_WINDOW = 10.0
    PENDING_INVITE_CAP = 3            # max simultaneous unanswered incoming invites
    MAX_TEXT_LEN = 16000
    MAX_GREETING_LEN = 200
    MAX_SESSIONS = 8                  # simultaneously live sessions
    # ---- transfer tuning (mirrors FileTransferManager) ----------------------
    CHUNK_SIZE = 256 * 1024
    INVITE_ACCEPT_TIMEOUT = 300.0     # sender waits this long for chat_file_accept
    COMPLETION_WAIT_TIMEOUT = 60.0
    TRANSFER_STALL_TIMEOUT = 600.0    # no chunk progress this long => fail + remove .part
    MAX_CONCURRENT_INCOMING_FILES = 3
    MAX_CONCURRENT_OUTGOING_FILES = 3
    # ---- liveness ------------------------------------------------------------
    PING_INTERVAL = 45.0
    OFFLINE_AFTER = 150.0             # silent for ~3 intervals => show offline

    MESSAGE_HISTORY_MAX = 500         # entries kept per session (oldest trimmed)
    SEND_FN_CACHE_MAX = 32            # most-recent peers kept in _latest_send_fn

    def __init__(self, device_id: str, device_name: str, receive_dir: str = ""):
        self._device_id = device_id
        self._device_name = device_name
        self._own_fp = ""

        self._lock = threading.RLock()
        self._sessions: dict[str, ChatSession] = {}          # peer_id -> session
        self._session_by_sid: dict[str, ChatSession] = {}
        self._invite_times_in: dict[str, deque] = {}         # peer_id -> mono timestamps
        self._invite_times_out: dict[str, deque] = {}
        self._text_times_out: dict[str, deque] = {}          # session_id -> outgoing mono timestamps
        self._text_times_in: dict[str, deque] = {}           # session_id -> incoming mono timestamps
        self._receives: dict[str, dict] = {}                 # transfer_id -> receive state
        self._sends: dict[str, dict] = {}                    # transfer_id -> send state
        self._latest_send_fn: dict[str, SendFn] = {}         # peer_id -> newest send_fn
        self._receive_dir: Path | None = None
        if receive_dir:
            self.set_receive_dir(receive_dir)

        # ---- UI callbacks (fired on worker threads!) ----
        self._on_incoming_invite: Callable[[dict], None] | None = None
        self._on_invite_response: Callable[[str, str, bool], None] | None = None
        self._on_sessions_changed: Callable[[], None] | None = None
        self._on_message: Callable[[str, dict], None] | None = None
        self._on_file_progress: Callable[[str, str, float], None] | None = None
        self._on_file_done: Callable[[str, str, bool, str, str], None] | None = None

        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="chat-heartbeat",
        )
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def set_on_incoming_invite(self, cb: Callable[[dict], None]) -> None:
        """*cb(invite)* -- someone asks to chat; ``invite`` holds
        ``session_id / peer_id / peer_name / fingerprint_short / greeting``."""
        self._on_incoming_invite = cb

    def set_on_invite_response(self, cb: Callable[[str, str, bool], None]) -> None:
        """*cb(session_id, peer_id, accepted)* -- answer to OUR invitation."""
        self._on_invite_response = cb

    def set_on_sessions_changed(self, cb: Callable[[], None]) -> None:
        """*cb()* -- coarse "something changed" signal (UI may just re-poll)."""
        self._on_sessions_changed = cb

    def set_on_message(self, cb: Callable[[str, dict], None]) -> None:
        """*cb(session_id, entry_dict)* -- a new entry was appended."""
        self._on_message = cb

    def set_on_file_progress(self, cb: Callable[[str, str, float], None]) -> None:
        """*cb(session_id, transfer_id, fraction)* -- per chunk, both directions."""
        self._on_file_progress = cb

    def set_on_file_done(self, cb: Callable[[str, str, bool, str, str], None]) -> None:
        """*cb(session_id, transfer_id, success, saved_path, status)*."""
        self._on_file_done = cb

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def shorten_fingerprint(fingerprint: str) -> str:
        """Reduce a colon-hex fingerprint to its last 8 hex chars (display)."""
        compact = "".join(c for c in (fingerprint or "") if c.isalnum())
        return compact[-8:].upper() if len(compact) >= 8 else compact.upper()

    def set_own_fingerprint(self, fingerprint: str) -> None:
        """Let the host advertise its own short fingerprint in invites."""
        self._own_fp = fingerprint or ""

    def _fire(self, attr: str, *args: Any) -> None:
        """Invoke a callback; a bad UI callback must never kill a network thread."""
        cb = getattr(self, attr, None)
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            logger.warning("chat callback %s raised", attr, exc_info=True)

    def _send_frame(self, payload: dict, send_fn: SendFn | None) -> bool:
        if send_fn is None:
            return False
        try:
            data = encode_frame(payload)
        except Exception:
            logger.debug("chat: encode failed for %s", payload.get("msg_type"), exc_info=True)
            return False
        try:
            result = send_fn(data)
            return result is True
        except Exception:
            logger.debug("chat: send_fn raised for %s", payload.get("msg_type"), exc_info=True)
            return False

    def _append_entry(self, session: ChatSession, entry: ChatEntry) -> None:
        session.entries.append(entry)
        overflow = len(session.entries) - self.MESSAGE_HISTORY_MAX
        if overflow > 0:
            del session.entries[:overflow]
        session.last_activity_ts = entry.ts

    def _live_session_count(self) -> int:
        return sum(
            1 for s in self._sessions.values()
            if s.status in ("inviting", "invited", "active")
        )

    def _resolve_session(self, session_id: str, peer_id: str) -> ChatSession | None:
        """Find a session by id (bound to sender), or fall back to the peer."""
        sess = self._session_by_sid.get(session_id)
        if sess is not None and sess.peer_id == peer_id:
            return sess
        by_peer = self._sessions.get(peer_id)
        if by_peer is not None and by_peer.session_id == session_id:
            return by_peer
        return None

    def _touch_seen(self, session: ChatSession) -> None:
        session.last_seen_mono = time.monotonic()
        session.online = True
        session.offline_announced = False

    def _prune_times(self, dq: deque, now: float, window: float) -> None:
        while dq and now - dq[0] > window:
            dq.popleft()

    def _remember_send_fn(self, peer_id: str, send_fn: SendFn) -> None:
        """Cache the newest working send_fn per peer, capped to SEND_FN_CACHE_MAX.

        Pop-then-reinsert so an active chat partner is moved to the end of the
        insertion-ordered dict: a plain assignment would keep its original
        position, so eviction by ``next(iter(...))`` would drop the oldest
        *inserted* peer — even one still in use — instead of the least
        recently *used*.
        """
        if send_fn is None:
            return
        self._latest_send_fn.pop(peer_id, None)
        self._latest_send_fn[peer_id] = send_fn
        if len(self._latest_send_fn) > self.SEND_FN_CACHE_MAX:
            # Insertion-ordered dict: the front is the least-recently-used peer.
            self._latest_send_fn.pop(next(iter(self._latest_send_fn)), None)

    def _cleanup_rate_buckets_locked(self, peer_id: str, session_id: str) -> None:
        """Drop rate-limit buckets that can no longer accumulate (lock held).

        Invite buckets are keyed by peer_id and only removed once the deque is
        empty after pruning (the peer may still knock again).  Text buckets are
        keyed by session_id, which is never reused, so they are removed outright.
        Without this the invite/text dicts grow without bound as every past peer
        (including each ``__anon__`` connection) and every closed session leaves
        a permanent entry.
        """
        for bucket in (self._invite_times_in, self._invite_times_out):
            dq = bucket.get(peer_id)
            if dq is not None:
                self._prune_times(dq, time.monotonic(), self.INVITE_RATE_WINDOW)
                if not dq:
                    bucket.pop(peer_id, None)
        for bucket in (self._text_times_out, self._text_times_in):
            bucket.pop(session_id, None)

    def _session_has_active_transfer(self, session: ChatSession) -> bool:
        """True when *session* has a send or receive still MAKING PROGRESS.

        Only transfers that moved recently count — a genuinely-stalled
        transfer (peer gone) must not pin the peer "online" for the whole
        stall window; a slow-but-moving one still does.
        """
        terminal = ("done", "failed", "declined", "cancelled")
        now = time.monotonic()
        for state in self._sends.values():
            if state["session"] is session and state["entry"].status not in terminal:
                if now - state.get("last_progress_mono", 0.0) < self.TRANSFER_STALL_TIMEOUT:
                    return True
        for state in self._receives.values():
            if state["session"] is session and state["entry"].status not in terminal:
                if now - state.get("last_progress_mono", 0.0) < self.TRANSFER_STALL_TIMEOUT:
                    return True
        return False

    # ------------------------------------------------------------------
    # Outgoing actions (UI threads)
    # ------------------------------------------------------------------

    def start_session(
        self, peer_id: str, peer_name: str, fingerprint_short: str, send_fn: SendFn,
    ) -> str | None:
        """Invite *peer_id* to chat.

        Returns the session id of the NEW session, or of the EXISTING live
        session when one is already up (callers can simply select it).
        ``None`` means suppressed (rate limit / slots full / bad args).
        """
        peer_id = (peer_id or "").strip()
        if not peer_id or send_fn is None:
            return None
        now = time.monotonic()
        with self._lock:
            existing = self._sessions.get(peer_id)
            if existing is not None and existing.status in ("inviting", "invited", "active"):
                self._remember_send_fn(peer_id, send_fn)
                return existing.session_id
            out = self._invite_times_out.setdefault(peer_id, deque())
            self._prune_times(out, now, self.INVITE_RATE_WINDOW)
            if len(out) >= self.INVITE_RATE_LIMIT:
                logger.info("chat: outgoing invite to %s rate-limited", peer_id[:12])
                return None
            if self._live_session_count() >= self.MAX_SESSIONS:
                logger.info("chat: session slots full, refusing invite")
                return None
            out.append(now)
            session = ChatSession(
                session_id=uuid.uuid4().hex[:16],
                peer_id=peer_id,
                peer_name=(peer_name or peer_id)[:80],
                fingerprint_short=self.shorten_fingerprint(fingerprint_short)[:32],
                status="inviting",
                created_ts=time.time(),
                last_seen_mono=now,
                last_activity_ts=time.time(),
            )
            self._sessions[peer_id] = session
            self._session_by_sid[session.session_id] = session
            self._remember_send_fn(peer_id, send_fn)
            sent = self._send_frame({
                "msg_type": "chat_invite",
                "session_id": session.session_id,
                "from_name": self._device_name[:80],
                "fingerprint_short": self.shorten_fingerprint(self._own_fp)[:32],
                "greeting": "",
            }, send_fn)
            if not sent:
                logger.debug("chat: invite frame dropped (send_fn refused)")
        self._fire("_on_sessions_changed")
        return session.session_id

    def accept_invitation(self, session_id: str, send_fn: SendFn) -> bool:
        """User accepted an incoming chat invitation."""
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "invited":
                return False
            session.status = "active"
            # The user actively engaged with the invite — clear the unread
            # marker raised when it arrived.
            session.unread = 0
            self._touch_seen(session)
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            ok = self._send_frame(
                {"msg_type": "chat_accept", "session_id": session.session_id}, fn,
            )
            if ok:
                self._remember_send_fn(session.peer_id, fn)
        self._fire("_on_sessions_changed")
        return ok

    def decline_invitation(self, session_id: str, send_fn: SendFn, reason: str = "") -> bool:
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "invited":
                return False
            session.status = "closed"
            session.unread = 0
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            self._send_frame({
                "msg_type": "chat_decline",
                "session_id": session.session_id,
                "reason": (reason or "")[:100],
            }, fn)
        self._fire("_on_sessions_changed")
        return True

    def close_session(self, session_id: str, notify_peer: bool = True) -> bool:
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status not in ("inviting", "invited", "active"):
                return False
            was_active = session.status == "active"
            session.status = "closed"
            self._fail_transfers_for_session(session, "cancelled")
            # A closed session id is never reused; drop its text buckets so
            # the rate-limit dicts cannot grow without bound.
            self._text_times_out.pop(session.session_id, None)
            self._text_times_in.pop(session.session_id, None)
            if notify_peer and was_active:
                self._send_frame(
                    {"msg_type": "chat_close", "session_id": session.session_id},
                    self._latest_send_fn.get(session.peer_id),
                )
        self._fire("_on_sessions_changed")
        return True

    def send_text(self, session_id: str, text: str, send_fn: SendFn) -> bool:
        text = (text or "").strip()
        if not text or len(text) > self.MAX_TEXT_LEN:
            return False
        now = time.monotonic()
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "active" or not session.online:
                return False
            # Outgoing texts have their own budget so a peer flooding us with
            # incoming texts cannot silently halve what we may send back.
            dq = self._text_times_out.setdefault(session.session_id, deque())
            self._prune_times(dq, now, self.TEXT_RATE_WINDOW)
            if len(dq) >= self.TEXT_RATE_LIMIT:
                logger.info("chat: text flood control engaged for session %s", session_id[:8])
                return False
            dq.append(now)
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            entry = ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="text", outgoing=True,
                ts=time.time(), text=text, status="pending",
            )
            self._append_entry(session, entry)
            ok = self._send_frame({
                "msg_type": "chat_text",
                "session_id": session.session_id,
                "text": text,
                "ts": entry.ts,
            }, fn)
            if not ok:
                # A failed send must not permanently consume a rate-limit slot:
                # roll back the timestamp we just charged.
                if dq and dq[-1] == now:
                    dq.pop()
            # Surface delivery failure in the transcript instead of silently
            # showing a message that never reached the peer.
            entry.status = "done" if ok else "failed"
            if ok:
                self._remember_send_fn(session.peer_id, fn)
        self._fire("_on_message", session_id, entry.to_dict())
        self._fire("_on_sessions_changed")
        return ok

    def send_file(self, session_id: str, file_path: str, send_fn: SendFn) -> str | None:
        """Offer *file_path* inside the session; returns transfer_id or None.

        Bytes only start flowing once the peer answers ``chat_file_accept``.
        """
        path = Path(file_path)
        try:
            size = path.stat().st_size
        except OSError:
            logger.debug("chat: send_file stat failed for %s", file_path)
            return None
        if size > MAX_FILE_SIZE:
            logger.info("chat: refusing to send %s (%d bytes > cap)", path.name, size)
            return None
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "active" or not session.online:
                return None
            # Mirror the incoming cap so a UI bug (or a fast-clicking user)
            # cannot spawn an unbounded number of chunk threads per session.
            outgoing_inflight = sum(
                1 for s in self._sends.values() if s["session"] is session
            )
            if outgoing_inflight >= self.MAX_CONCURRENT_OUTGOING_FILES:
                logger.info("chat: too many outgoing files for session %s", session_id[:8])
                return None
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            transfer_id = uuid.uuid4().hex
            entry = ChatEntry(
                entry_id=transfer_id, kind="file", outgoing=True, ts=time.time(),
                file_name=path.name[:255], file_size=size, mime="",
                status="await_accept", transfer_id=transfer_id,
            )
            self._append_entry(session, entry)
            state = {
                "transfer_id": transfer_id,
                "session": session,
                "entry": entry,
                "file_path": path,
                "file_size": size,
                # 0-byte files have zero chunks; the "sent" completion frame
                # below is what lets the receiver finalize them.
                "total_chunks": math.ceil(size / self.CHUNK_SIZE),
                "accept_event": threading.Event(),
                "complete_event": threading.Event(),
                "cancel": False,
                "_done_fired": False,
                "send_fn": fn,
                "last_progress_mono": time.monotonic(),
            }
            self._sends[transfer_id] = state
            ok = self._send_frame({
                "msg_type": "chat_file_offer",
                "session_id": session.session_id,
                "transfer_id": transfer_id,
                "file_name": entry.file_name,
                "file_size": size,
                "mime": "",
            }, fn)
            if not ok:
                self._sends.pop(transfer_id, None)
                entry.status = "failed"
                return None
            self._remember_send_fn(session.peer_id, fn)
            threading.Thread(
                target=self._file_sender, args=(transfer_id,),
                daemon=True, name=f"chat-send-{transfer_id[:8]}",
            ).start()
        self._fire("_on_message", session_id, entry.to_dict())
        self._fire("_on_sessions_changed")
        return transfer_id

    def accept_file(self, session_id: str, transfer_id: str, send_fn: SendFn):
        """Accept an incoming file offer; start receiving.

        Returns ``True`` on success, ``False`` when the offer still exists but
        cannot be accepted right now (wrong session, or already past the
        ``await_accept`` stage), and ``None`` when the offer is already gone
        (``_receives`` no longer tracks it).  The ``None`` case is what the web
        REST handler surfaces as an explicit "expired" error — the offer was
        swept by the stale-receive reaper while the UI still showed its Accept
        button, so the user should learn it expired rather than see a generic
        failure.  ``None`` stays falsy, so callers that only test truthiness
        (``if mgr.accept_file(...)``) still treat it as a failed accept.
        """
        with self._lock:
            state = self._receives.get(transfer_id)
            if state is None:
                return None
            session = state["session"]
            if session.session_id != session_id or state["entry"].status != "await_accept":
                return False
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            receive_dir = self._receive_dir or _default_receive_dir()
            try:
                receive_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("chat: cannot create receive dir %s", receive_dir, exc_info=True)
                state["entry"].status = "failed"
                self._receives.pop(transfer_id, None)
                return False
            temp_path = receive_dir / f".chat{transfer_id}.part"
            # Defense-in-depth: the offer handler already requires hex
            # transfer_ids, but accept_file can be reached with arbitrary ids
            # from UI code — never let the temp write escape the receive dir.
            try:
                if temp_path.resolve().parent != receive_dir.resolve():
                    logger.error("chat: receive path escape blocked for %s", transfer_id[:8])
                    state["entry"].status = "failed"
                    self._receives.pop(transfer_id, None)
                    return False
            except OSError:
                state["entry"].status = "failed"
                self._receives.pop(transfer_id, None)
                return False
            try:
                state["fh"] = open(temp_path, "wb")
            except OSError:
                logger.warning("chat: cannot open temp file %s", temp_path, exc_info=True)
                state["entry"].status = "failed"
                self._receives.pop(transfer_id, None)
                return False
            state["temp_path"] = temp_path
            state["accepted"] = True
            state["received_bytes"] = 0
            state["chunks_remaining"] = set(range(state["total_chunks"]))
            state["entry"].status = "sending"
            state["last_progress_mono"] = time.monotonic()
            ok = self._send_frame({
                "msg_type": "chat_file_accept",
                "session_id": session_id,
                "transfer_id": transfer_id,
            }, fn)
            if ok:
                self._remember_send_fn(session.peer_id, fn)
            else:
                # The accept frame never reached the sender — roll back the
                # receive state we just created instead of leaving the write
                # handle open and the entry stuck at "sending" until the
                # stale-transfer sweeper reclaims it minutes later.
                fh = state.get("fh")
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    state["fh"] = None
                self._receives.pop(transfer_id, None)
                _safe_remove(state.get("temp_path"))
                state["entry"].status = "failed"
                # The wire ack never went out — the SENDER is still waiting
                # in "await_accept".  Fire the done callback so the UI drops
                # the stale offer and the sender's wait times out instead of
                # being pinned online for the whole accept window.
                self._fire(
                    "_on_file_done", session_id, transfer_id, False, "", "peer_offline",
                )
        self._fire("_on_sessions_changed")
        return ok

    def decline_file(self, session_id: str, transfer_id: str, send_fn: SendFn) -> bool:
        """User declined an incoming file offer."""
        with self._lock:
            state = self._receives.pop(transfer_id, None)
            if state is None:
                return False
            state["entry"].status = "declined"
            fn = send_fn or self._latest_send_fn.get(state["session"].peer_id)
            self._send_frame({
                "msg_type": "chat_file_reject",
                "session_id": session_id,
                "transfer_id": transfer_id,
            }, fn)
        self._fire("_on_sessions_changed")
        return True

    def cancel_file(self, session_id: str, entry_id: str) -> bool:
        """User-cancelled an in-flight transfer (either direction)."""
        with self._lock:
            state = self._sends.get(entry_id) or self._receives.get(entry_id)
            if state is None:
                return False
            session = state["session"]
            entry = state["entry"]
            if entry.status in ("done", "declined", "cancelled"):
                return False
            state["cancel"] = True
            if "accept_event" in state:
                state["accept_event"].set()
            if "complete_event" in state:
                state["complete_event"].set()
            fh = state.get("fh")
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                state["fh"] = None
            _safe_remove(state.get("temp_path"))
            entry.status = "cancelled"
            entry.fraction = 0.0
            self._sends.pop(entry_id, None)
            self._receives.pop(entry_id, None)
            self._append_entry(session, ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                ts=time.time(), text_key="chat.system.file_cancelled",
                fmt={"name": entry.file_name},
            ))
            self._send_frame({
                "msg_type": "chat_file_cancel",
                "session_id": session.session_id,
                "transfer_id": entry_id,
            }, self._latest_send_fn.get(session.peer_id))
            sid, tid, name = session.session_id, entry_id, entry.file_name
        self._fire("_on_file_done", sid, tid, False, "", "cancelled")
        self._fire("_on_sessions_changed")
        return True

    def mark_session_read(self, session_id: str) -> None:
        with self._lock:
            session = self._session_by_sid.get(session_id)
            changed = session is not None and session.unread > 0
            if changed:
                session.unread = 0
        if changed:
            self._fire("_on_sessions_changed")

    def set_receive_dir(self, path: str) -> None:
        try:
            directory = Path(path)
            directory.mkdir(parents=True, exist_ok=True)
            self._receive_dir = directory
        except OSError:
            logger.warning("chat: bad receive dir %s -- keeping previous", path, exc_info=True)

    def shutdown(self) -> None:
        """Stop background threads and release file handles (idempotent)."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.5)
        with self._lock:
            for state in self._receives.values():
                fh = state.get("fh")
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    state["fh"] = None
            self._receives.clear()
            self._sends.clear()

    # ------------------------------------------------------------------
    # Snapshots for the UI
    # ------------------------------------------------------------------

    def get_sessions(self) -> list[dict]:
        with self._lock:
            sessions = sorted(
                self._sessions.values(), key=lambda s: s.last_activity_ts, reverse=True,
            )
            return [s.to_dict() for s in sessions]

    def get_messages(self, session_id: str) -> list[dict]:
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None:
                return []
            return [e.to_dict() for e in session.entries]

    # ------------------------------------------------------------------
    # Incoming JSON frames (transport recv thread)
    # ------------------------------------------------------------------

    def handle_message(
        self,
        msg_type: str,
        payload: dict,
        sender_device_id: str,
        sender_fp_short: str,
        send_fn: SendFn,
    ) -> bool:
        """Handle one ``CHAT_MSG_TYPES`` frame.  Returns True when the type
        belongs to this module (even if the individual frame was dropped) so
        the host router stops routing it elsewhere."""
        if msg_type not in CHAT_MSG_TYPES:
            return False
        if not isinstance(payload, dict):
            return True
        sender_device_id = (sender_device_id or "").strip()
        try:
            if msg_type == "chat_invite":
                self._handle_chat_invite(payload, sender_device_id, sender_fp_short, send_fn)
            elif msg_type == "chat_accept":
                self._handle_chat_accept(payload, sender_device_id, sender_fp_short)
            elif msg_type == "chat_decline":
                self._handle_chat_decline(payload, sender_device_id)
            elif msg_type == "chat_close":
                self._handle_chat_close(payload, sender_device_id)
            elif msg_type == "chat_text":
                self._handle_chat_text(payload, sender_device_id)
            elif msg_type == "chat_ping":
                self._handle_chat_ping(payload, sender_device_id, send_fn)
            elif msg_type == "chat_pong":
                self._handle_chat_pong(payload, sender_device_id)
            elif msg_type == "chat_file_offer":
                self._handle_file_offer(payload, sender_device_id)
            elif msg_type == "chat_file_accept":
                self._handle_file_accept(payload, sender_device_id)
            elif msg_type == "chat_file_reject":
                self._handle_file_reject(payload, sender_device_id)
            elif msg_type == "chat_file_cancel":
                self._handle_file_cancel_msg(payload, sender_device_id)
            elif msg_type == "chat_file_complete":
                self._handle_file_complete_msg(payload, sender_device_id)
        except Exception:
            # Never let a malformed frame kill the recv thread.
            logger.warning("chat: error handling %s", msg_type, exc_info=True)
        return True

    # -- invitation lifecycle --------------------------------------------

    def _handle_chat_invite(self, payload, sender_id, fp_short, send_fn) -> None:
        now = time.monotonic()
        sid = str(payload.get("session_id", ""))
        if len(sid) != 16 or any(c not in "0123456789abcdef" for c in sid):
            logger.debug("chat: invite with malformed session_id from %s", sender_id[:12])
            return
        with self._lock:
            dq = self._invite_times_in.setdefault(sender_id, deque())
            self._prune_times(dq, now, self.INVITE_RATE_WINDOW)
            if len(dq) >= self.INVITE_RATE_LIMIT:
                logger.warning("chat: invite rate limit hit for %s -- ignoring", sender_id[:12])
                return
            dq.append(now)
            def _clean(s: str) -> str:
                return "".join(ch for ch in s if ch.isprintable())

            mine = self._sessions.get(sender_id)
            # Control chars in peer-supplied strings reach OS notifications
            # and invite dialogs — strip them before use.
            greeting = _clean(str(payload.get("greeting", "")))[:self.MAX_GREETING_LEN]
            from_name = _clean(str(payload.get("from_name", "")))[:80]
            peer_fp = self.shorten_fingerprint(
                str(payload.get("fingerprint_short", "") or fp_short or ""),
            )[:32]

            if mine is not None and mine.status == "inviting":
                # Mutual invite.  Resolve deterministically: the SMALLER
                # session id wins, so both devices converge without a dialog.
                if sid < mine.session_id:
                    self._drop_session_locked(mine)
                    session = self._open_incoming_locked(
                        sender_id, from_name or mine.peer_name, peer_fp, sid, send_fn,
                    )
                    self._activate_locked(session)
                    sid_out, pid_out = session.session_id, session.peer_id
                else:
                    # Ours wins; tell them to stop waiting on theirs.
                    self._send_frame({
                        "msg_type": "chat_decline", "session_id": sid, "reason": "duplicate",
                    }, send_fn)
                    return
                self._fire("_on_invite_response", sid_out, pid_out, True)
                self._fire("_on_sessions_changed")
                return

            if mine is not None and mine.status == "active":
                # Already chatting.  If this invite carries a NEW session id
                # (the peer closed its old session without telling us, then
                # restarted), adopt the new id — otherwise both sides stay
                # "active" with mismatched ids and every later message is
                # dropped as an unknown session.
                if mine.session_id != sid:
                    old_sid = mine.session_id
                    self._session_by_sid.pop(old_sid, None)
                    self._session_by_sid[sid] = mine
                    mine.session_id = sid
                    # Carry the text-rate buckets to the new sid: adoption
                    # must not reset the flood budget (or let an attacker
                    # reset it by re-inviting) and must not orphan the old
                    # buckets (unbounded growth).
                    for bucket_name in ("_text_times_out", "_text_times_in"):
                        bucket_map = getattr(self, bucket_name)
                        old_bucket = bucket_map.pop(old_sid, None)
                        if old_bucket:
                            bucket_map[sid] = old_bucket
                # Reaffirm so their client converges too.
                self._send_frame({"msg_type": "chat_accept", "session_id": sid}, send_fn)
                self._touch_seen(mine)
                self._fire("_on_sessions_changed")
                return

            pending = sum(1 for s in self._sessions.values() if s.status == "invited")
            if pending >= self.PENDING_INVITE_CAP:
                logger.info("chat: pending invite cap reached -- auto-declining %s", sender_id[:12])
                self._send_frame({
                    "msg_type": "chat_decline", "session_id": sid, "reason": "busy",
                }, send_fn)
                return
            session = self._open_incoming_locked(
                sender_id, from_name or sender_id[:12], peer_fp, sid, send_fn,
            )
            invite = {
                "session_id": sid,
                "peer_id": sender_id,
                "peer_name": session.peer_name,
                "fingerprint_short": peer_fp,
                "greeting": greeting,
            }
        self._fire("_on_incoming_invite", invite)
        self._fire("_on_sessions_changed")

    def _open_incoming_locked(self, peer_id, peer_name, fp_short, sid, send_fn) -> ChatSession:
        session = ChatSession(
            session_id=sid,
            peer_id=peer_id,
            peer_name=(peer_name or peer_id)[:80],
            fingerprint_short=(fp_short or "")[:32],
            status="invited",
            created_ts=time.time(),
            last_seen_mono=time.monotonic(),
            last_activity_ts=time.time(),
            # A freshly-arrived invite is unread so the web badge / session
            # row is immediately visible to the user.
            unread=1,
        )
        old = self._sessions.get(peer_id)
        if old is not None:
            self._drop_session_locked(old)
        self._sessions[peer_id] = session
        self._session_by_sid[sid] = session
        self._remember_send_fn(peer_id, send_fn)
        return session

    def _activate_locked(self, session: ChatSession) -> None:
        session.status = "active"
        session.online = True
        session.offline_announced = False
        self._send_frame(
            {"msg_type": "chat_accept", "session_id": session.session_id},
            self._latest_send_fn.get(session.peer_id),
        )

    def _drop_session_locked(self, session: ChatSession) -> None:
        """Tear a session down without notifying the peer."""
        self._fail_transfers_for_session(session, "cancelled")
        session.status = "closed"
        self._sessions.pop(session.peer_id, None)
        if self._session_by_sid.get(session.session_id) is session:
            self._session_by_sid.pop(session.session_id, None)
        self._cleanup_rate_buckets_locked(session.peer_id, session.session_id)

    def _handle_chat_accept(self, payload, sender_id, fp_short) -> None:
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None or session.status not in ("inviting", "active"):
                return
            newly_active = session.status == "inviting"
            session.status = "active"
            self._touch_seen(session)
            if fp_short and not session.fingerprint_short:
                session.fingerprint_short = self.shorten_fingerprint(fp_short)[:32]
            sid, pid = session.session_id, session.peer_id
        if newly_active:
            self._fire("_on_invite_response", sid, pid, True)
        self._fire("_on_sessions_changed")

    def _handle_chat_decline(self, payload, sender_id) -> None:
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None or session.status != "inviting":
                return
            session.status = "declined_remote"
            sid, pid = session.session_id, session.peer_id
        self._fire("_on_invite_response", sid, pid, False)
        self._fire("_on_sessions_changed")

    def _handle_chat_close(self, payload, sender_id) -> None:
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None:
                # Fall back to the peer's live session regardless of id.
                session = self._sessions.get(sender_id)
            if session is None or session.status not in ("inviting", "invited", "active"):
                return
            session.status = "closed"
            self._fail_transfers_for_session(session, "peer_offline")
            self._append_entry(session, ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                ts=time.time(), text_key="chat.system.session_closed_by_peer",
            ))
            sid = session.session_id
            entry = session.entries[-1]
        self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")

    # -- text + liveness ---------------------------------------------------

    def _handle_chat_text(self, payload, sender_id) -> None:
        now = time.monotonic()
        text = payload.get("text")
        if not isinstance(text, str) or not text or len(text) > self.MAX_TEXT_LEN:
            logger.debug("chat: dropping invalid text from %s", sender_id[:12])
            return
        # Strip control characters like the invite strings — this text reaches
        # OS notifications and the session preview, so a peer could otherwise
        # inject bidi-override / ESC tricks into what the user reads.  Unlike
        # the invite fields, line breaks are legitimate message content and
        # must survive (multi-line clips, indentation).
        text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
        if not text.strip():
            logger.debug("chat: dropping control-char-only text from %s", sender_id[:12])
            return
        with self._lock:
            # By-peer fallback like ping/close: a re-invite-adopted session
            # (or a lost chat_accept) can leave the sender's session_id out
            # of sync; the text must still land instead of being dropped.
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id) \
                or self._sessions.get(sender_id)
            if session is None or session.status != "active":
                logger.debug("chat: text from %s without active session -- dropped", sender_id[:12])
                return
            dq = self._text_times_in.setdefault(session.session_id, deque())
            self._prune_times(dq, now, self.TEXT_RATE_WINDOW)
            if len(dq) >= self.TEXT_RATE_LIMIT:
                logger.warning("chat: text flood from %s -- dropping", sender_id[:12])
                return
            dq.append(now)
            self._touch_seen(session)
            now_ts = time.time()
            try:
                raw_ts = float(payload.get("ts") or now_ts)
            except (TypeError, ValueError):
                raw_ts = now_ts
            # A peer-controlled timestamp must not pin last_activity_ts into
            # the future (that would defeat the dead-session reaper).
            if not (now_ts - 3600.0 <= raw_ts <= now_ts + 60.0):
                raw_ts = now_ts
            entry = ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="text", outgoing=False,
                ts=raw_ts, text=text, status="done",
            )
            self._append_entry(session, entry)
            session.unread += 1
            sid = session.session_id
        self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")

    def _handle_chat_ping(self, payload, sender_id, send_fn) -> None:
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id) \
                or self._sessions.get(sender_id)
            if session is None or session.status != "active":
                return
            self._touch_seen(session)
            self._send_frame(
                {"msg_type": "chat_pong", "session_id": session.session_id},
                send_fn or self._latest_send_fn.get(session.peer_id),
            )

    def _handle_chat_pong(self, payload, sender_id) -> None:
        with self._lock:
            session = self._sessions.get(sender_id)
            if session is not None and session.status == "active":
                self._touch_seen(session)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def _handle_file_offer(self, payload, sender_id) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        size = payload.get("file_size")
        raw_name = payload.get("file_name")
        # transfer_id becomes part of the temp-file name on disk
        # (``.chat{transfer_id}.part``), so it must be strict hex — the same
        # validation session_id gets — to keep the receive path confined to
        # the receive directory.
        if not isinstance(transfer_id, str) or len(transfer_id) != 32 \
                or any(c not in "0123456789abcdef" for c in transfer_id) \
                or not isinstance(size, int) or isinstance(size, bool) \
                or size < 0 or size > MAX_FILE_SIZE \
                or not isinstance(raw_name, str) or not raw_name:
            logger.debug("chat: rejecting malformed file offer from %s", sender_id[:12])
            return
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None or session.status != "active":
                logger.debug("chat: file offer outside active session -- rejected")
                return
            inflight = sum(
                1 for s in self._receives.values() if s["session"] is session
            )
            if inflight >= self.MAX_CONCURRENT_INCOMING_FILES:
                logger.info("chat: too many incoming files from %s -- rejecting", sender_id[:12])
                self._send_frame({
                    "msg_type": "chat_file_reject",
                    "session_id": session.session_id,
                    "transfer_id": transfer_id,
                }, self._latest_send_fn.get(session.peer_id))
                return
            entry = ChatEntry(
                entry_id=transfer_id, kind="file", outgoing=False, ts=time.time(),
                file_name=_sanitize_file_name(raw_name), file_size=size,
                mime=str(payload.get("mime", ""))[:100],
                status="await_accept", transfer_id=transfer_id,
            )
            self._append_entry(session, entry)
            self._receives[transfer_id] = {
                "transfer_id": transfer_id,
                "session": session,
                "entry": entry,
                "file_name": entry.file_name,
                "file_size": size,
                "total_chunks": math.ceil(size / self.CHUNK_SIZE),
                "accepted": False,
                "fh": None,
                "temp_path": None,
                "received_bytes": 0,
                "chunks_remaining": set(),
                "cancel": False,
                "_done_fired": False,
                "last_progress_mono": time.monotonic(),
            }
            sid = session.session_id
        self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")

    def _handle_file_accept(self, payload, sender_id) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            state = self._sends.get(transfer_id)
            if state is None or state["session"].peer_id != sender_id:
                return
            if state["entry"].status != "await_accept":
                return
            state["entry"].status = "sending"
            state["accept_event"].set()
            state["last_progress_mono"] = time.monotonic()
        self._fire("_on_sessions_changed")

    def _handle_file_reject(self, payload, sender_id) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            state = self._sends.pop(transfer_id, None)
            if state is None or state["session"].peer_id != sender_id:
                return
            entry = state["entry"]
            if entry.status in ("done", "cancelled"):
                self._sends[transfer_id] = state  # leave terminal state untouched
                return
            entry.status = "declined"
            state["cancel"] = True
            state["accept_event"].set()
            state["complete_event"].set()
            session = state["session"]
            self._append_entry(session, ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                ts=time.time(), text_key="chat.system.file_declined",
                fmt={"name": entry.file_name},
            ))
            sid, tid = session.session_id, transfer_id
            last = session.entries[-1]
        self._fire("_on_file_done", sid, tid, False, "", "rejected")
        self._fire("_on_message", sid, last.to_dict())
        self._fire("_on_sessions_changed")

    def _handle_file_cancel_msg(self, payload, sender_id) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            state = self._sends.get(transfer_id) or self._receives.get(transfer_id)
            if state is None or state["session"].peer_id != sender_id:
                return
            entry = state["entry"]
            if entry.status in ("done", "declined", "cancelled"):
                return
            state["cancel"] = True
            if "accept_event" in state:
                state["accept_event"].set()
            if "complete_event" in state:
                state["complete_event"].set()
            fh = state.get("fh")
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                state["fh"] = None
            _safe_remove(state.get("temp_path"))
            entry.status = "cancelled"
            self._sends.pop(transfer_id, None)
            self._receives.pop(transfer_id, None)
            session = state["session"]
            self._append_entry(session, ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                ts=time.time(), text_key="chat.system.file_cancelled",
                fmt={"name": entry.file_name},
            ))
            sid, tid = session.session_id, transfer_id
            last = session.entries[-1]
        self._fire("_on_file_done", sid, tid, False, "", "cancelled_by_peer")
        self._fire("_on_message", sid, last.to_dict())
        self._fire("_on_sessions_changed")

    def _handle_file_complete_msg(self, payload, sender_id) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        status = str(payload.get("status", ""))
        with self._lock:
            # Case 1: we are the sender; the receiver confirms delivery.
            send_state = self._sends.get(transfer_id)
            if send_state is not None and send_state["session"].peer_id == sender_id:
                # Only a clean ack is a success.  The receiver can report a
                # real failure (e.g. error_size_mismatch) — surface it so the
                # transcript doesn't claim the file was delivered.
                # (The dead ``peer_completed`` write was removed in v1.0.29.1:
                # nothing reads it — ``_file_sender`` consults ``error_status``
                # alone to decide between "done" and "declined".)
                if status not in ("", "ok", "sent"):
                    send_state["error_status"] = status
                send_state["complete_event"].set()
                return
            # Case 2: we are the receiver; sender says all bytes are sent.
            recv_state = self._receives.get(transfer_id)
            if recv_state is not None and recv_state["session"].peer_id == sender_id:
                if recv_state.get("accepted"):
                    self._finalize_receive(transfer_id)

    def handle_binary_chunk(self, raw_payload: dict, sender_device_id: str, send_fn: SendFn) -> bool:
        """Consume a decoded ``file_chunk`` frame if it belongs to a chat
        receive-in-progress.  Returns False so the host router can fall back
        to clipboard file transfers."""
        if not isinstance(raw_payload, dict):
            return False
        transfer_id = str(raw_payload.get("transfer_id", ""))
        with self._lock:
            state = self._receives.get(transfer_id)
            if state is None:
                return False
            if state["session"].peer_id != sender_device_id:
                logger.warning(
                    "chat: chunk for %s from unexpected peer %s -- dropped",
                    transfer_id[:8], sender_device_id[:12],
                )
                return True
            if not state.get("accepted") or state.get("fh") is None:
                # Chunks before acceptance are protocol violations; drop them.
                return True
            if state.get("cancel"):
                return True
            index = raw_payload.get("chunk_index")
            total = raw_payload.get("total_chunks")
            data = raw_payload.get("_raw_data")
            if not isinstance(index, int) or not isinstance(total, int) \
                    or not isinstance(data, (bytes, bytearray)) \
                    or index < 0 or index >= state["total_chunks"]:
                logger.warning("chat: malformed chunk for %s -- dropped", transfer_id[:8])
                return True
            try:
                state["fh"].seek(index * self.CHUNK_SIZE)
                state["fh"].write(data)
            except OSError:
                logger.warning("chat: disk write failed for %s", transfer_id[:8], exc_info=True)
                self._fail_receive_locked(transfer_id, "error_disk")
                return True
            state["received_bytes"] += len(data)
            state["chunks_remaining"].discard(index)
            state["last_progress_mono"] = time.monotonic()
            entry = state["entry"]
            entry.fraction = min(1.0, state["received_bytes"] / max(1, state["file_size"]))
            sid, tid, fraction = state["session"].session_id, transfer_id, entry.fraction
        self._fire("_on_file_progress", sid, tid, fraction)
        with self._lock:
            state = self._receives.get(transfer_id)
            if state is not None and not state["chunks_remaining"] \
                    and state["received_bytes"] >= state["file_size"]:
                self._finalize_receive(transfer_id)
        return True

    def _finalize_receive(self, transfer_id: str) -> None:
        """Verify + move the finished temp file to its final name (lock held)."""
        state = self._receives.get(transfer_id)
        if state is None or state.get("_done_fired"):
            return
        state["_done_fired"] = True
        session = state["session"]
        entry = state["entry"]
        fh = state.get("fh")
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
            state["fh"] = None
        temp_path = state.get("temp_path")
        try:
            actual = temp_path.stat().st_size if temp_path else -1
        except OSError:
            actual = -1
        if actual != state["file_size"] or state["received_bytes"] != state["file_size"]:
            logger.error(
                "chat: size mismatch for %s (promised %d, on disk %d, counted %d)",
                transfer_id[:8], state["file_size"], actual, state["received_bytes"],
            )
            _safe_remove(temp_path)
            self._receives.pop(transfer_id, None)
            entry.status = "failed"
            self._send_frame({
                "msg_type": "chat_file_complete",
                "session_id": session.session_id,
                "transfer_id": transfer_id,
                "status": "error_size_mismatch",
            }, self._latest_send_fn.get(session.peer_id))
            self._fire("_on_file_done", session.session_id, transfer_id, False, "", "error_size_mismatch")
            return

        receive_dir = self._receive_dir or _default_receive_dir()
        dest_path = receive_dir / state["file_name"]
        try:
            if dest_path.resolve().parent != receive_dir.resolve():
                raise ValueError("path traversal blocked")
        except (OSError, ValueError):
            logger.error("chat: path traversal blocked for %s", state["file_name"])
            _safe_remove(temp_path)
            self._receives.pop(transfer_id, None)
            entry.status = "failed"
            self._fire("_on_file_done", session.session_id, transfer_id, False, "", "error_security")
            return
        if dest_path.exists():
            stem, suffix = dest_path.stem, dest_path.suffix
            counter = 1
            while dest_path.exists():
                dest_path = receive_dir / f"{stem} ({counter}){suffix}"
                counter += 1
        try:
            temp_path.replace(dest_path)
        except OSError:
            logger.error("chat: finalize rename failed for %s", transfer_id[:8], exc_info=True)
            _safe_remove(temp_path)
            self._receives.pop(transfer_id, None)
            entry.status = "failed"
            self._fire("_on_file_done", session.session_id, transfer_id, False, "", "error_disk")
            return
        self._receives.pop(transfer_id, None)
        entry.status = "done"
        entry.fraction = 1.0
        entry.saved_path = str(dest_path)
        self._send_frame({
            "msg_type": "chat_file_complete",
            "session_id": session.session_id,
            "transfer_id": transfer_id,
            "status": "ok",
        }, self._latest_send_fn.get(session.peer_id))
        self._fire("_on_file_done", session.session_id, transfer_id, True, str(dest_path), "success")

    def _fail_receive_locked(self, transfer_id: str, status: str) -> None:
        """Mark a receive failed from an I/O error (lock held).

        Must tell the SENDER: without an error frame it sends
        ``chat_file_complete`` "sent", waits out the ack timeout, and reports
        its own entry as delivered — leaving the two sides with opposite
        terminal states.
        """
        state = self._receives.pop(transfer_id, None)
        if state is None:
            return
        session = state["session"]
        self._send_frame({
            "msg_type": "chat_file_complete",
            "session_id": session.session_id,
            "transfer_id": transfer_id,
            "status": status,
        }, self._latest_send_fn.get(session.peer_id))
        fh = state.get("fh")
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        _safe_remove(state.get("temp_path"))
        entry = state["entry"]
        entry.status = "failed"
        self._fire("_on_file_done", session.session_id, transfer_id, False, "", status)

    def _file_sender(self, transfer_id: str) -> None:
        """Worker thread: wait for acceptance, then stream the file."""
        with self._lock:
            state = self._sends.get(transfer_id)
            if state is None:
                return
            session = state["session"]
            entry = state["entry"]
            accept_event = state["accept_event"]
            complete_event = state["complete_event"]
        if not accept_event.wait(timeout=self.INVITE_ACCEPT_TIMEOUT):
            with self._lock:
                state = self._sends.pop(transfer_id, None)
                if state is None or state["entry"].status != "await_accept":
                    return
                state["entry"].status = "failed"
                sid, tid = state["session"].session_id, transfer_id
            self._fire("_on_file_done", sid, tid, False, "", "error_timeout")
            return
        with self._lock:
            state = self._sends.get(transfer_id)
            if state is None or state["cancel"]:
                return
            path, size = state["file_path"], state["file_size"]
            total_chunks = state["total_chunks"]
            send_fn = state["send_fn"]
            session = state["session"]
            # Bind early: the failure paths below use these, and the stall
            # sweeper may have already popped the send state, so the inner
            # `state["session"].session_id` rebinds would NameError on sid.
            sid, tid = session.session_id, transfer_id
        try:
            with open(path, "rb") as fh:
                for index in range(total_chunks):
                    with self._lock:
                        state = self._sends.get(transfer_id)
                        if state is None or state["cancel"]:
                            return
                    chunk = fh.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    frame = encode_binary_chunk(transfer_id, index, total_chunks, chunk)
                    if not self._send_frame_raw(frame, send_fn):
                        with self._lock:
                            state = self._sends.pop(transfer_id, None)
                            if state is not None:
                                state["entry"].status = "failed"
                                sid, tid = state["session"].session_id, transfer_id
                        self._fire("_on_file_done", sid, tid, False, "", "peer_offline")
                        return
                    fraction = (index + 1) / total_chunks
                    with self._lock:
                        state = self._sends.get(transfer_id)
                        if state is not None:
                            state["entry"].fraction = fraction
                            state["last_progress_mono"] = time.monotonic()
                    self._fire("_on_file_progress", session.session_id, transfer_id, fraction)
        except OSError:
            logger.error("chat: read failed for %s", path, exc_info=True)
            with self._lock:
                state = self._sends.pop(transfer_id, None)
                if state is not None:
                    state["entry"].status = "failed"
                    sid, tid = state["session"].session_id, transfer_id
            self._fire("_on_file_done", sid, tid, False, "", "error_disk")
            return
        # Tell the receiver the byte stream is finished (also the only
        # trigger an empty-file receive ever gets), then wait briefly for
        # the receiver's delivery ack.
        self._send_frame({
            "msg_type": "chat_file_complete",
            "session_id": session.session_id,
            "transfer_id": transfer_id,
            "status": "sent",
        }, send_fn)
        complete_event.wait(timeout=self.COMPLETION_WAIT_TIMEOUT)
        with self._lock:
            state = self._sends.pop(transfer_id, None)
            if state is None:
                return
            err_status = state.get("error_status", "")
            if state["cancel"]:
                return
            if err_status:
                # The receiver reported a failure (e.g. size mismatch) — the
                # transfer did NOT succeed.  Mark the sender's entry declined
                # so a REST refetch matches the live card (the WS maps the
                # "rejected" code to the declined label) instead of "done".
                state["entry"].status = "declined"
            else:
                state["entry"].status = "done"
                state["entry"].fraction = 1.0
            sid, tid = state["session"].session_id, transfer_id
        if err_status:
            logger.warning("chat: receiver rejected file %s (%s)", transfer_id[:8], err_status)
            self._fire("_on_file_done", sid, tid, False, err_status, "rejected")
            return
        # The receiver may not ack within the wait window — the bytes were
        # still handed to the transport and the entry is already "done", so
        # report success either way.  (No separate "unconfirmed" status is
        # surfaced; every UI renders the entry as delivered.)
        self._fire("_on_file_done", sid, tid, True, "", "success")

    def _send_frame_raw(self, data: bytes, send_fn: SendFn) -> bool:
        """Send pre-encoded bytes; False when the transport refused."""
        if send_fn is None:
            return False
        try:
            result = send_fn(data)
            return result is True
        except Exception:
            logger.debug("chat: chunk send failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Liveness / teardown
    # ------------------------------------------------------------------

    def _fail_transfers_for_session(self, session: ChatSession, status: str) -> None:
        """Fail all in-flight transfers bound to *session* (lock held)."""
        for tid in [t for t, s in self._sends.items() if s["session"] is session]:
            state = self._sends.pop(tid)
            state["cancel"] = True
            state["accept_event"].set()
            state["complete_event"].set()
            state["entry"].status = "failed"
            self._fire("_on_file_done", session.session_id, tid, False, "", status)
        for tid in [t for t, s in self._receives.items() if s["session"] is session]:
            state = self._receives.pop(tid)
            fh = state.get("fh")
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            _safe_remove(state.get("temp_path"))
            state["entry"].status = "failed"
            self._fire("_on_file_done", session.session_id, tid, False, "", status)

    def mark_peer_disconnected(self, peer_id: str) -> None:
        """The transport lost the connection to *peer_id*."""
        with self._lock:
            session = self._sessions.get(peer_id)
            if session is None:
                return
            if session.status == "active":
                if not session.offline_announced:
                    session.offline_announced = True
                    session.online = False
                    self._append_entry(session, ChatEntry(
                        entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                        ts=time.time(), text_key="chat.system.peer_offline",
                    ))
                    sid = session.session_id
                    entry = session.entries[-1]
                    announced = True
                else:
                    announced = False
                    sid = session.session_id
                    entry = None
                self._fail_transfers_for_session(session, "peer_offline")
            else:
                # A pending invite/answer can no longer be delivered.
                session.status = "closed"
                announced, sid, entry = False, session.session_id, None
        if announced and entry is not None:
            self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")

    def _heartbeat_loop(self) -> None:
        """Ping active sessions and expire stale pending ones."""
        while not self._heartbeat_stop.wait(timeout=self.PING_INTERVAL / 2):
            now = time.monotonic()
            fired: list[tuple[str, dict]] = []
            file_done_fired: list[tuple[str, str, str]] = []
            recv_done_fired: list[tuple[str, str, str]] = []
            recv_sessions_changed = False
            with self._lock:
                for session in list(self._sessions.values()):
                    # Reap long-dead sessions so the map cannot grow without
                    # bound over a multi-day uptime.
                    if session.status in ("closed", "declined_remote") \
                            and time.time() - session.last_activity_ts > 3600.0:
                        self._sessions.pop(session.peer_id, None)
                        self._session_by_sid.pop(session.session_id, None)
                        self._cleanup_rate_buckets_locked(session.peer_id, session.session_id)
                        continue
                    if session.status in ("inviting", "invited"):
                        if now - session.last_seen_mono > self.INVITE_ACCEPT_TIMEOUT:
                            self._drop_session_locked(session)
                            self._fire("_on_sessions_changed")
                        continue
                    if session.status != "active":
                        continue
                    # Offline edge detection
                    if session.online and now - session.last_seen_mono > self.OFFLINE_AFTER:
                        # A session with an in-flight transfer is still alive:
                        # on a slow link (TCP retransmits) a big file can take
                        # far longer than OFFLINE_AFTER without a ping.  Only
                        # declare the peer offline -- and kill its transfers --
                        # once no transfer is actively moving.
                        if not self._session_has_active_transfer(session):
                            session.online = False
                            session.offline_announced = True
                            self._append_entry(session, ChatEntry(
                                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                                ts=time.time(), text_key="chat.system.peer_offline",
                            ))
                            fired.append((session.session_id, session.entries[-1].to_dict()))
                            self._fail_transfers_for_session(session, "peer_offline")
                            continue
                    if session.offline_announced and not session.online:
                        continue
                    if now - session.last_ping_mono >= self.PING_INTERVAL:
                        session.last_ping_mono = now
                        self._send_frame(
                            {"msg_type": "chat_ping", "session_id": session.session_id},
                            self._latest_send_fn.get(session.peer_id),
                        )
                # Both sweepers only mutate state under the lock and return
                # the callbacks to fire — the actual fires happen BELOW so a
                # slow WS/UI callback can never freeze the chat lock.
                recv_done_fired, recv_sessions_changed = self._expire_stale_receives(
                    defer_fire=True,
                )
                file_done_fired = self._expire_stale_transfers(fired)
            for sid, entry_dict in fired:
                self._fire("_on_message", sid, entry_dict)
            for sid, tid, status in file_done_fired:
                self._fire("_on_file_done", sid, tid, False, "", status)
            for sid, tid, status in recv_done_fired:
                self._fire("_on_file_done", sid, tid, False, "", status)
            if recv_sessions_changed:
                self._fire("_on_sessions_changed")

    def _expire_stale_receives(self, defer_fire: bool = False) -> tuple[list, bool]:
        """Drop incoming file offers the user never answered (lock held).

        Without this, a few ignored offers would permanently pin the
        per-session incoming-file cap, and an accepted-but-silent sender
        could leak an open temp handle for the life of the session.

        Returns ``(done_fired, sessions_changed)`` where *done_fired* holds
        ``(session_id, transfer_id, status)`` tuples for ``_on_file_done``.
        When *defer_fire* is true the callbacks are NOT invoked inline — the
        caller fires them after releasing the lock.  The heartbeat passes
        ``defer_fire=True`` so a slow UI/WS callback can't freeze the chat
        lock; direct callers (tests) keep the historical inline behavior.
        """
        now = time.time()
        done_fired: list[tuple[str, str, str]] = []
        sessions_changed = False
        for tid, state in list(self._receives.items()):
            entry = state["entry"]
            if entry.status == "await_accept" and now - entry.ts > self.INVITE_ACCEPT_TIMEOUT:
                fh = state.get("fh")
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    state["fh"] = None
                _safe_remove(state.get("temp_path"))
                session = state["session"]
                self._receives.pop(tid, None)
                entry.status = "declined"
                # Tell the UI the offer expired so the Accept/Decline card
                # stops offering actions that can no longer succeed.
                done_fired.append((session.session_id, tid, "declined"))
                sessions_changed = True
        if defer_fire:
            return done_fired, sessions_changed
        for sid, tid, status in done_fired:
            self._fire("_on_file_done", sid, tid, False, "", status)
        if sessions_changed:
            self._fire("_on_sessions_changed")
        return done_fired, sessions_changed

    @staticmethod
    def _transfer_stall_reference(state: dict, now_mono: float) -> float:
        """Monotonic reference for stall detection (lock held).

        Uses the explicit per-chunk progress marker when present; otherwise
        falls back to the entry's wall-clock creation time (i.e. the transfer
        has not moved since it was created).
        """
        last = state.get("last_progress_mono")
        if last:
            return last
        return state["entry"].ts - (time.time() - now_mono)

    def _expire_stale_transfers(self, fired: list[tuple[str, dict]]) -> list[tuple[str, str, str]]:
        """Fail transfers that have made no progress for too long (lock held).

        A peer that crashes (or whose TCP disconnect never surfaces as a
        callback) can strand a half-written ``.chat<id>.part`` on the receiver
        and pin an in-flight slot on the sender forever -- the heartbeat only
        used to sweep ``_sessions``, never the transfer maps.  Any transfer
        with no progress for ``TRANSFER_STALL_TIMEOUT`` is failed, its temp
        file removed, and the UI notified with an appended system entry (added
        to *fired*) plus a returned ``_on_file_done`` call for the caller to
        fire outside the lock.
        """
        now = time.monotonic()
        done_fired: list[tuple[str, str, str]] = []
        terminal = ("done", "failed", "declined", "cancelled")
        for tid, state in list(self._sends.items()):
            if now - self._transfer_stall_reference(state, now) <= self.TRANSFER_STALL_TIMEOUT:
                continue
            self._sends.pop(tid, None)
            state["cancel"] = True
            state["accept_event"].set()
            state["complete_event"].set()
            entry = state["entry"]
            if entry.status in terminal:
                continue
            entry.status = "failed"
            session = state["session"]
            self._append_entry(session, ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                ts=time.time(), text_key="chat.system.peer_offline",
            ))
            fired.append((session.session_id, session.entries[-1].to_dict()))
            done_fired.append((session.session_id, tid, "error_timeout"))
        for tid, state in list(self._receives.items()):
            if now - self._transfer_stall_reference(state, now) <= self.TRANSFER_STALL_TIMEOUT:
                continue
            self._receives.pop(tid, None)
            fh = state.get("fh")
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                state["fh"] = None
            _safe_remove(state.get("temp_path"))
            entry = state["entry"]
            if entry.status in terminal:
                continue
            entry.status = "failed"
            session = state["session"]
            self._append_entry(session, ChatEntry(
                entry_id=uuid.uuid4().hex[:16], kind="system", outgoing=False,
                ts=time.time(), text_key="chat.system.peer_offline",
            ))
            fired.append((session.session_id, session.entries[-1].to_dict()))
            done_fired.append((session.session_id, tid, "error_timeout"))
        return done_fired
