"""Nearby chat between devices that are NOT paired.

ClipSync's clipboard sync requires pairing (8-digit code + cert pinning).
This module adds a lighter channel for devices that merely discovered each
other on the LAN.  It has two modes, and which one is in force is the
user's choice (``ChatManager.set_open_to_all``):

``open_to_all`` (the default)
    One device sends a ``chat_invite`` and the session is live on both
    sides the moment it lands; text and files flow without either user
    confirming anything.

``open_to_all`` off
    The peer must be allowed by hand.  An incoming invitation lands as
    ``invited`` and waits for :meth:`ChatManager.accept_invitation` or
    :meth:`ChatManager.decline_invitation`, and an incoming file waits at
    ``await_accept`` for :meth:`ChatManager.accept_file`.

Both modes exchange short text messages and files (chunked with the same
binary-chunk framing clipboard file transfers use), and both are gated
against *abuse*: rate-limited session opening, per-session text flood
control, sanitized file names confined to the receive directory, and hard
size caps.

Security model
--------------
The default mode has no acceptance step, and that is deliberate.  The reach
of this channel is the LAN: reaching a peer at all means already being on
the same network, and the frames only travel over the encrypted (if
unpinned) session the discovery layer established.  Everything on a home or
office LAN is therefore treated as trusted, and a device that knows the
pairing code is trusted outright -- which is also why an unpaired peer may
send directly.

The consequence to be honest about: on a network you do not control -- a
public Wi-Fi -- anyone who can reach your port can also open a session and
send you a file.  The receive directory, the file-name sanitizer and the
size caps are what bound that, not a dialog.  Turning ``open_to_all`` off
replaces that judgement with a prompt on both the session and each file.  A
short certificate fingerprint travels with the session and is shown in the
UI either way, so a user who wants to check who they are talking to can
compare it out of band.

All application-level rules live here -- the transport only whitelists
``CHAT_MSG_TYPES`` frames through for unpaired peers; content rules are
enforced in :meth:`ChatManager.handle_message`.

Threading
---------
All public methods are thread-safe (one re-entrant lock around state).
Every registered callback fires on a *worker* thread (recv thread, heartbeat
thread, or a transfer thread) -- UI layers MUST marshal onto their own main
loop (Tk: ``root.after(0, ...)``).
"""

import contextlib
import logging
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from internal.fsutil import safe_remove as _safe_remove
from internal.protocol.codec import (
    CHAT_MSG_TYPES,
    encode_binary_chunk,
    encode_frame,
)

# Same-package reuse: received-file names MUST be sanitized exactly like
# clipboard file transfers, so share the one implementation.
from internal.sync.file_transfer import MAX_FILE_SIZE, _sanitize_file_name
from internal.transport.relay import MAX_RELAY_FRAME, frame_limit_for

logger = logging.getLogger(__name__)

SendFn = Callable[[bytes], Any]


class ChatFileTooLargeError(Exception):
    """A file was refused because it exceeds :data:`MAX_FILE_SIZE`.

    The relay no longer has a size cap of its own: what the public broker
    limits is the *message*, and a chat attachment is already cut into chunks
    that fit one (see :meth:`ChatManager.relay_chunk_for`).  Capping the total
    as well refused the files users actually wanted to send — a 9 MB installer
    or an 80 MB PDF — for a reason that is nothing but the sum of chunks the
    transport was built to carry.
    """


def _default_receive_dir() -> Path:
    return Path.home() / "Downloads" / "ClipSync" / "Chat"


@dataclass
class ChatEntry:
    """One row in a conversation: text, file card, or system notice."""

    entry_id: str
    kind: str  # "text" | "file" | "system"
    outgoing: bool
    ts: float
    text: str = ""
    text_key: str = ""  # i18n key for system entries (never literals)
    fmt: dict = field(default_factory=dict)
    file_name: str = ""
    file_size: int = 0
    mime: str = ""
    status: str = "pending"  # pending|await_accept|sending|done|failed|declined|cancelled
    fraction: float = 0.0
    saved_path: str = ""
    transfer_id: str = ""
    # Round 17: the wire frame's protocol ``msg_id`` for OUTGOING entries.  The
    # relay delivery ledger and ``internet_delivery`` WS events are keyed by
    # this id, so carrying it on the entry lets the frontend match a
    # sent/delivered/failed receipt back to the exact chat bubble.  Empty for
    # incoming/system entries (delivery receipts are a sender-side concern).
    msg_id: str = ""

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
            "msg_id": self.msg_id,
        }


@dataclass
class ChatSession:
    """Conversation state for one remote peer (at most one live session)."""

    session_id: str  # 16-hex, minted by the inviter, echoed by the acceptor
    peer_id: str
    peer_name: str
    fingerprint_short: str  # <=32 chars, displayed for out-of-band comparison
    # `inviting` / `invited` only ever appear while open_to_all is off: they
    # are a session waiting on the far end and on this end respectively.
    status: str  # inviting|invited|active|declined_remote|closed
    created_ts: float
    last_seen_mono: float = 0.0  # time.monotonic() of last inbound frame
    last_activity_ts: float = 0.0
    unread: int = 0
    online: bool = True
    entries: list[ChatEntry] = field(default_factory=list)
    last_ping_mono: float = 0.0
    offline_announced: bool = False
    # PEER's typing indicator, receiver side: time.monotonic() deadline after
    # which the flag self-expires (no sweeper needed — to_dict() compares
    # lazily).  0.0 means "not typing".
    peer_typing_until_mono: float = 0.0

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
            # Lazy expiry: no timer needed, the deadline simply passes.
            # Old web frontends ignore the unknown field (backward compatible).
            "peer_typing": self.peer_typing_until_mono > time.monotonic(),
        }


class ChatManager:
    """State machine + wire handlers for direct nearby chat.

    Wire payloads (JSON frames built with
    :func:`internal.protocol.codec.encode_frame`):

    - ``chat_invite``  ``{session_id, from_name, fingerprint_short}``
      -- with ``open_to_all`` on it opens the session on both sides and no
      reply gates it; with it off the receiver holds the session at
      ``invited`` until its user answers.  The fingerprint travels here in
      both modes -- it is the only way the receiver learns who this is
    - ``chat_accept`` / ``chat_close``  ``{session_id}``
      -- the answer to an invitation while the prompt is on, and in both
      modes the carrier that settles which of two crossing session ids wins
      (a session that is already live just reaffirms its id)
    - ``chat_decline`` ``{session_id, reason}``
      -- a refused invitation, or a peer that stopped waiting on one:
      ``reason`` is ``"duplicate"`` when an invitation lost the
      crossing-open tiebreak and ``"busy"`` when the pending-invite cap is
      full, and empty for a user's own refusal.  It never tears down a
      session that is already live
    - ``chat_text``    ``{session_id, text, ts}``
    - ``chat_typing``  ``{session_id, typing}``  -- typing indicator; the
      sender throttles same-state frames to one per ``TYPING_THROTTLE``
      seconds, the receiver expires the flag ``TYPING_TIMEOUT`` after the
      last frame (cleared early by an incoming text)
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
    INVITE_RATE_LIMIT = 5  # session opens per peer (each direction) per window
    INVITE_RATE_WINDOW = 300.0
    TEXT_RATE_LIMIT = 30  # texts per active session per window
    TEXT_RATE_WINDOW = 10.0
    PENDING_INVITE_CAP = 3  # max simultaneous unanswered incoming invites
    MAX_TEXT_LEN = 16000
    MAX_GREETING_LEN = 200
    MAX_SESSIONS = 8  # simultaneously live sessions
    # ---- transfer tuning (mirrors FileTransferManager) ----------------------
    CHUNK_SIZE = 256 * 1024
    # Chunks for internet-only peers must fit inside the relay payload cap, and
    # the cap applies to the published envelope — 4/3 of this chunk, plus the
    # 46-byte binary header, plus the GCM tag and the JSON shell around them.
    # 224 KiB did not: base64 turned it into a ~306 KB envelope, past the
    # broker's 256 KB, which dropped the chunk in flight while the sender
    # counted it sent.  176 KiB lands at ~240 KB with the chunk header, leaving
    # the public broker room for its own framing.  LAN peers keep CHUNK_SIZE so
    # the wire format stays byte-identical for pre-update LAN peers.
    RELAY_CHUNK_SIZE = 176 * 1024
    # How long a session may sit unanswered while open_to_all is off: the
    # sender waits this long for a ``chat_accept``, and the receiver's own
    # ``invited`` row is dropped after the same silence.  In the default mode
    # no session is ever in either state, so this only ever elapses when the
    # user turned the prompt on and then walked away.
    INVITE_ACCEPT_TIMEOUT = 300.0
    TRANSFER_ACCEPT_TIMEOUT = 300.0  # sender waits this long for chat_file_accept
    COMPLETION_WAIT_TIMEOUT = 60.0
    TRANSFER_STALL_TIMEOUT = 600.0  # no chunk progress this long => fail + remove .part
    # A chunk the transport refuses is retried before the transfer is failed.
    # Over the relay a refusal is usually backpressure rather than a dead peer:
    # the relay's outgoing queue is bounded (relay.MAX_QUEUED_MESSAGES), so a
    # receiver on a slow link fills it and the next publish is refused.  Giving
    # it a moment to drain keeps a slow link slow instead of turning it into a
    # failed attachment — while a genuinely offline peer still fails, just
    # CHUNK_SEND_ATTEMPTS * CHUNK_SEND_RETRY_DELAY seconds later.
    CHUNK_SEND_ATTEMPTS = 8
    CHUNK_SEND_RETRY_DELAY = 0.5
    MAX_CONCURRENT_INCOMING_FILES = 3
    MAX_CONCURRENT_OUTGOING_FILES = 3
    # ---- liveness ------------------------------------------------------------
    PING_INTERVAL = 45.0
    OFFLINE_AFTER = 150.0  # silent for ~3 intervals => show offline
    # ---- typing indicator ------------------------------------------------------
    TYPING_THROTTLE = 2.0  # same-state frames closer than this: suppressed
    TYPING_TIMEOUT = 4.0  # receiver clears the indicator after this silence

    MESSAGE_HISTORY_MAX = 500  # entries kept per session (oldest trimmed)
    SEND_FN_CACHE_MAX = 32  # most-recent peers kept in _latest_send_fn

    @classmethod
    def relay_chunk_for(cls, max_message_bytes: int) -> int:
        """The chunk size to send internet peers over a relay of this limit.

        :data:`RELAY_CHUNK_SIZE` is the answer for the 256 KiB broker this app
        ships against, and it stays the answer at that limit exactly — the
        scale below is by the shipped pair, so the default relay's wire format
        is untouched.  A different limit is that pair scaled rather than
        recomputed from the envelope arithmetic: the shipped chunk was measured
        against the shipped frame limit, and the ratio between them holds for
        the GCM tag, the JSON shell and the broker's own framing alike.

        Scaled by the *frame* limit rather than the payload, because the frame
        limit is the number the publish path actually refuses against (see
        ``relay.frame_limit_for``).  Scaling by the payload would leave the
        result depending on a floor that can exceed what a small relay carries,
        which is the one outcome this function exists to prevent.

        Capped at :data:`CHUNK_SIZE`, because the *receiver* refuses to
        reassemble a chunk larger than that: a relay carrying more than 256 KiB
        must not make this side advertise more than the peer will take.
        """
        frame_limit = frame_limit_for(max(1, int(max_message_bytes)))
        scaled = cls.RELAY_CHUNK_SIZE * frame_limit // MAX_RELAY_FRAME
        return max(1, min(scaled, cls.CHUNK_SIZE))

    def __init__(self, device_id: str, device_name: str, receive_dir: str = ""):
        self._device_id = device_id
        self._device_name = device_name
        self._own_fp = ""

        self._lock = threading.RLock()
        self._sessions: dict[str, ChatSession] = {}  # peer_id -> session
        self._session_by_sid: dict[str, ChatSession] = {}
        self._invite_times_in: dict[str, deque] = {}  # peer_id -> mono timestamps
        self._invite_times_out: dict[str, deque] = {}
        self._text_times_out: dict[str, deque] = {}  # session_id -> outgoing mono timestamps
        self._text_times_in: dict[str, deque] = {}  # session_id -> incoming mono timestamps
        self._typing_out: dict[str, tuple[bool, float]] = {}  # sid -> (last state, last send mono)
        self._receives: dict[str, dict] = {}  # transfer_id -> receive state
        self._sends: dict[str, dict] = {}  # transfer_id -> send state
        self._latest_send_fn: dict[str, SendFn] = {}  # peer_id -> newest send_fn
        self._receive_dir: Path | None = None
        # Default mode: anyone who can reach this device may open a session and
        # send a file.  See the module docstring; `set_open_to_all` is the
        # switch a user turns when they want to be asked instead.
        self._open_to_all = True
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
            target=self._heartbeat_loop,
            daemon=True,
            name="chat-heartbeat",
        )
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def set_on_sessions_changed(self, cb: Callable[[], None]) -> None:
        """*cb()* -- coarse "something changed" signal (UI may just re-poll)."""
        self._on_sessions_changed = cb

    def set_on_incoming_invite(self, cb: Callable[[dict], None]) -> None:
        """*cb(invite_dict)* -- an invitation is waiting for this user's answer.

        Only fires while ``open_to_all`` is off; the dict carries
        ``session_id``, ``peer_id``, ``peer_name``, ``fingerprint_short``.
        """
        self._on_incoming_invite = cb

    def set_on_invite_response(self, cb: Callable[[str, str, bool], None]) -> None:
        """*cb(session_id, peer_id, accepted)* -- our own invitation was answered.

        Only fires while ``open_to_all`` is off.
        """
        self._on_invite_response = cb

    @property
    def open_to_all(self) -> bool:
        """Whether nearby devices may send without asking first.

        Read by the front ends through the session list, so a UI knows
        whether an incoming offer is waiting on this user or has already
        been taken -- the two modes look the same in the session rows.
        """
        return self._open_to_all

    def set_open_to_all(self, flag: bool) -> None:
        """Choose whether nearby devices need this user's permission.

        ``True`` (the default) opens a session and takes a file on arrival.
        ``False`` holds both for an explicit accept.  Flipping the switch does
        not disturb sessions that are already live -- they were admitted under
        the rules in force when they opened, and tearing them down would drop
        a conversation the user is in the middle of.
        """
        self._open_to_all = bool(flag)

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

    def _send_frame(self, payload: dict, send_fn: SendFn | None, msg_id: str = "") -> bool:
        if send_fn is None:
            return False
        try:
            # Carry the real device id in the frame: the LAN transport infers
            # the sender from the connection, but a relay-mirrored copy has no
            # connection — the receiving host attributes it via
            # ``source_device`` (Round 16 chat-over-internet).  ``msg_id`` is
            # the Round-17 delivery correlation id: the relay ledger and
            # ``internet_delivery`` events are keyed by the frame's protocol
            # msg_id, so text sends mint it here and stamp the ChatEntry with
            # the same value (empty → encode_frame auto-generates one).
            data = encode_frame(payload, msg_id=msg_id, source_device=self._device_id)
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
        return sum(1 for s in self._sessions.values() if s.status == "active")

    def _resolve_session(self, session_id: str, peer_id: str) -> ChatSession | None:
        """Find a session by id (bound to sender), or fall back to the peer."""
        sess = self._session_by_sid.get(session_id)
        if sess is not None and sess.peer_id == peer_id:
            return sess
        by_peer = self._sessions.get(peer_id)
        if by_peer is not None and by_peer.session_id == session_id:
            return by_peer
        return None

    def _touch_seen(self, session: ChatSession) -> bool:
        """Mark a session as just heard from; True when that brought it back.

        The peer's online state is part of what the session list shows, and a
        peer that had gone quiet and speaks again changes it — so the answer is
        the caller's cue to announce the list.  It is the caller's because this
        runs under the chat lock, where nothing may be fired.
        """
        was_offline = not session.online
        session.last_seen_mono = time.monotonic()
        session.online = True
        session.offline_announced = False
        return was_offline

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
        # Typing bookkeeping is keyed by the never-reused session id too.
        self._typing_out.pop(session_id, None)

    def _session_has_active_transfer(self, session: ChatSession) -> bool:
        """True when *session* has a send or receive still MAKING PROGRESS.

        Only transfers that moved recently count — a genuinely-stalled
        transfer (peer gone) must not pin the peer "online" for the whole
        stall window; a slow-but-moving one still does.
        """
        terminal = ("done", "failed", "declined", "cancelled")
        now = time.monotonic()
        for state in self._sends.values():
            if state["session"] is session and state["entry"].status not in terminal:  # noqa: SIM102
                if now - state.get("last_progress_mono", 0.0) < self.TRANSFER_STALL_TIMEOUT:
                    return True
        for state in self._receives.values():
            if state["session"] is session and state["entry"].status not in terminal:  # noqa: SIM102
                if now - state.get("last_progress_mono", 0.0) < self.TRANSFER_STALL_TIMEOUT:
                    return True
        return False

    # ------------------------------------------------------------------
    # Outgoing actions (UI threads)
    # ------------------------------------------------------------------

    def start_session(
        self,
        peer_id: str,
        peer_name: str,
        fingerprint_short: str,
        send_fn: SendFn,
    ) -> str | None:
        """Open a chat with *peer_id*.

        Returns the session id of the NEW session, or of the EXISTING live
        session when one is already up (callers can simply select it).
        ``None`` means suppressed (rate limit / slots full / bad args).

        The session is born ``active`` in the default mode -- the peer
        receives a ``chat_invite``, but nothing about this side waits for an
        answer, and the user can type into the conversation as soon as this
        returns.  With ``open_to_all`` off it is born ``inviting`` and the
        peer's ``chat_accept`` is what moves it to ``active``; the UI shows
        it as waiting until then.
        """
        peer_id = (peer_id or "").strip()
        if not peer_id or send_fn is None:
            return None
        now = time.monotonic()
        done_fired: list[tuple[str, str, str]] = []
        with self._lock:
            existing = self._sessions.get(peer_id)
            # `inviting` counts as live here: a second click while our
            # invitation is still unanswered must hand back the id we already
            # sent rather than mint a second session the peer has never heard
            # of -- that would orphan the first in `_session_by_sid` and spend
            # another slot against MAX_SESSIONS.
            if existing is not None and existing.status in ("active", "inviting"):
                self._remember_send_fn(peer_id, send_fn)
                return existing.session_id
            out = self._invite_times_out.setdefault(peer_id, deque())
            self._prune_times(out, now, self.INVITE_RATE_WINDOW)
            if len(out) >= self.INVITE_RATE_LIMIT:
                logger.info("chat: outgoing session open to %s rate-limited", peer_id[:12])
                return None
            if self._live_session_count() >= self.MAX_SESSIONS:
                logger.info("chat: session slots full, refusing session open")
                return None
            out.append(now)
            session = ChatSession(
                session_id=uuid.uuid4().hex[:16],
                peer_id=peer_id,
                peer_name=(peer_name or peer_id)[:80],
                fingerprint_short=self.shorten_fingerprint(fingerprint_short)[:32],
                status="active" if self._open_to_all else "inviting",
                created_ts=time.time(),
                last_seen_mono=now,
                last_activity_ts=time.time(),
            )
            self._sessions[peer_id] = session
            self._session_by_sid[session.session_id] = session
            self._remember_send_fn(peer_id, send_fn)
            sent = self._send_frame(
                {
                    "msg_type": "chat_invite",
                    "session_id": session.session_id,
                    "from_name": self._device_name[:80],
                    "fingerprint_short": self.shorten_fingerprint(self._own_fp)[:32],
                },
                send_fn,
            )
            refused = False
            if not sent:
                # The transport refused the invite frame, so the peer was
                # never told this session exists: tear it down NOW instead of
                # leaving an entry that holds one of the MAX_SESSIONS slots
                # and shows a conversation the peer cannot see.  Also roll
                # back the rate timestamp so the user can retry immediately
                # once connectivity is back.
                self._drop_session_locked(session, done_fired)
                if out and out[-1] == now:
                    out.pop()
                refused = True
                logger.debug("chat: invite frame dropped (send_fn refused) -- session discarded")
        for sid_d, tid_d, st_d in done_fired:
            self._fire("_on_file_done", sid_d, tid_d, False, "", st_d)
        if refused:
            self._fire("_on_sessions_changed")
            return None
        self._fire("_on_sessions_changed")
        return session.session_id

    def accept_invitation(self, session_id: str, send_fn: SendFn) -> bool:
        """This user allowed an incoming invitation (``open_to_all`` off).

        Refuses unless the session is actually waiting at ``invited``, so a
        stale click on a row that has since been answered cannot open a
        conversation the peer is not in.
        """
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "invited":
                return False
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            ok = self._send_frame(
                {"msg_type": "chat_accept", "session_id": session.session_id},
                fn,
            )
            if ok:
                # Commit the activation only when the accept frame actually
                # went out -- otherwise this side would show an active
                # conversation while the peer sits on its own "inviting"
                # screen until its timeout, with every message failing.
                session.status = "active"
                # The user actively engaged with the invite -- clear the unread
                # marker raised when it arrived.
                session.unread = 0
                self._touch_seen(session)
                self._remember_send_fn(session.peer_id, fn)
        self._fire("_on_sessions_changed")
        return ok

    def decline_invitation(self, session_id: str, send_fn: SendFn, reason: str = "") -> bool:
        """This user refused an incoming invitation (``open_to_all`` off)."""
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "invited":
                return False
            session.status = "closed"
            session.unread = 0
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            self._send_frame(
                {
                    "msg_type": "chat_decline",
                    "session_id": session.session_id,
                    "reason": (reason or "")[:100],
                },
                fn,
            )
        self._fire("_on_sessions_changed")
        return True

    def close_session(self, session_id: str, notify_peer: bool = True) -> bool:
        done_fired: list[tuple[str, str, str]] = []
        with self._lock:
            session = self._session_by_sid.get(session_id)
            # A session still waiting on an answer is closable too: that is
            # how a user withdraws their own invitation, or drops somebody
            # else's without answering it.
            if session is None or session.status not in ("inviting", "invited", "active"):
                return False
            was_status = session.status
            session.status = "closed"
            session.peer_typing_until_mono = 0.0
            session.unread = 0
            self._fail_transfers_for_session(session, "cancelled", done_fired)
            # A closed session id is never reused; drop its text buckets so
            # the rate-limit dicts cannot grow without bound.
            self._text_times_out.pop(session.session_id, None)
            self._text_times_in.pop(session.session_id, None)
            if notify_peer and was_status == "invited":
                # Dropping an invitation without answering it would leave the
                # sender on its "waiting to be allowed" screen until its
                # INVITE_ACCEPT_TIMEOUT expires; refuse it properly instead.
                self._send_frame(
                    {
                        "msg_type": "chat_decline",
                        "session_id": session.session_id,
                        "reason": "closed",
                    },
                    self._latest_send_fn.get(session.peer_id),
                )
            elif notify_peer and was_status == "active":
                self._send_frame(
                    {"msg_type": "chat_close", "session_id": session.session_id},
                    self._latest_send_fn.get(session.peer_id),
                )
        for sid_d, tid_d, st_d in done_fired:
            self._fire("_on_file_done", sid_d, tid_d, False, "", st_d)
        self._fire("_on_sessions_changed")
        return True

    def send_text(self, session_id: str, text: str, send_fn: SendFn) -> bool:
        # A non-string (e.g. a number from a hand-rolled REST client) would
        # raise on .strip(); refuse it cleanly instead.
        if not isinstance(text, str):
            return False
        text = text.strip()
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
            # Round 17: mint the frame msg_id up front so the entry carries it
            # and the relay delivery ledger/events match this exact bubble.
            frame_msg_id = uuid.uuid4().hex
            entry = ChatEntry(
                entry_id=uuid.uuid4().hex[:16],
                kind="text",
                outgoing=True,
                ts=time.time(),
                text=text,
                status="pending",
                msg_id=frame_msg_id,
            )
            self._append_entry(session, entry)
            ok = self._send_frame(
                {
                    "msg_type": "chat_text",
                    "session_id": session.session_id,
                    "text": text,
                    "ts": entry.ts,
                },
                fn,
                msg_id=frame_msg_id,
            )
            if not ok:  # noqa: SIM102
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

    def report_typing(self, session_id: str, typing: bool, send_fn: SendFn) -> bool:
        """Report OUR typing state for *session_id* (sender side).

        Sends ``chat_typing`` ``{session_id, typing}``.  Duplicate same-state
        frames within :data:`TYPING_THROTTLE` seconds are suppressed so a
        chatty input stream cannot flood the link; a state CHANGE always goes
        out immediately (that is what makes the indicator vanish the moment
        the user stops or clears the box).  The receiver expires the flag
        after :data:`TYPING_TIMEOUT` of silence, so continuous typing only
        needs a keep-alive every ~2s.

        No ``_on_sessions_changed`` fires: our own UI does not display our
        own typing.  Returns True when a frame actually went out on the wire
        (False also covers the throttle window — callers must treat that as
        success, not failure).
        """
        now = time.monotonic()
        want = bool(typing)
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "active" or not session.online:
                return False
            prev = self._typing_out.get(session.session_id)
            if prev is not None and prev[0] == want and now - prev[1] < self.TYPING_THROTTLE:
                return False
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            ok = self._send_frame(
                {
                    "msg_type": "chat_typing",
                    "session_id": session.session_id,
                    "typing": want,
                },
                fn,
            )
            # Remember failed attempts too: a dead transport must not turn
            # every keystroke into an immediate retry; the next throttle
            # window retries naturally.
            self._typing_out[session.session_id] = (want, now)
            if ok:
                self._remember_send_fn(session.peer_id, fn)
            return ok

    def resend_text(self, session_id: str, entry_id: str, send_fn: SendFn) -> bool:
        """Re-transmit a FAILED outgoing text; flips its entry to ``done``.

        Only entries that are this session's own outgoing texts with status
        ``failed`` qualify -- anything else (unknown id, incoming text, an
        already-sent bubble) is refused so a stale UI cannot double-send.
        The retry is charged to the SAME flood budget as a first send and
        rolls its slot back when the wire refuses it again.
        """
        now = time.monotonic()
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "active" or not session.online:
                return False
            entry = next(
                (e for e in session.entries if e.entry_id == entry_id),
                None,
            )
            if (
                entry is None
                or entry.kind != "text"
                or not entry.outgoing
                or entry.status != "failed"
            ):
                return False
            dq = self._text_times_out.setdefault(session.session_id, deque())
            self._prune_times(dq, now, self.TEXT_RATE_WINDOW)
            if len(dq) >= self.TEXT_RATE_LIMIT:
                logger.info("chat: resend flood control engaged for session %s", session_id[:8])
                return False
            dq.append(now)
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            # Round 17: a resend mints a NEW frame msg_id and re-stamps the
            # entry so the fresh delivery receipt matches this bubble.
            frame_msg_id = uuid.uuid4().hex
            entry.msg_id = frame_msg_id
            ok = self._send_frame(
                {
                    "msg_type": "chat_text",
                    "session_id": session.session_id,
                    "text": entry.text,
                    "ts": entry.ts,
                },
                fn,
                msg_id=frame_msg_id,
            )
            if not ok:
                # Same rollback rule as send_text: a failed retry must not
                # permanently consume a rate-limit slot.
                if dq and dq[-1] == now:
                    dq.pop()
            else:
                entry.status = "done"
                self._remember_send_fn(session.peer_id, fn)
            entry_dict = entry.to_dict()
        self._fire("_on_message", session_id, entry_dict)
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
            # The one size limit left, and a real one: refuse early and loudly
            # so the UI can name it instead of reporting a transfer that never
            # started for no stated reason.
            raise ChatFileTooLargeError(f"{path.name}: {size} bytes exceeds the file size cap")
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None or session.status != "active" or not session.online:
                return None
            fn = send_fn or self._latest_send_fn.get(session.peer_id)
            # Internet-only peers ship bytes through the relay in chunks sized
            # by the relay's *message* limit (``relay_chunk_for``, tagged on the
            # closure).  No total-size cap rides along with it: the transport
            # carries any file the app accepts.
            chunk_size = getattr(fn, "chunk_size", None) or self.CHUNK_SIZE
            # Mirror the incoming cap so a UI bug (or a fast-clicking user)
            # cannot spawn an unbounded number of chunk threads per session.
            outgoing_inflight = sum(1 for s in self._sends.values() if s["session"] is session)
            if outgoing_inflight >= self.MAX_CONCURRENT_OUTGOING_FILES:
                logger.info("chat: too many outgoing files for session %s", session_id[:8])
                return None
            transfer_id = uuid.uuid4().hex
            entry = ChatEntry(
                entry_id=transfer_id,
                kind="file",
                outgoing=True,
                ts=time.time(),
                file_name=path.name[:255],
                file_size=size,
                mime="",
                status="await_accept",
                transfer_id=transfer_id,
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
                "total_chunks": math.ceil(size / chunk_size),
                "chunk_size": chunk_size,
                "accept_event": threading.Event(),
                "complete_event": threading.Event(),
                "cancel": False,
                "_done_fired": False,
                "send_fn": fn,
                "last_progress_mono": time.monotonic(),
            }
            self._sends[transfer_id] = state
            ok = self._send_frame(
                {
                    "msg_type": "chat_file_offer",
                    "session_id": session.session_id,
                    "transfer_id": transfer_id,
                    "file_name": entry.file_name,
                    "file_size": size,
                    "chunk_size": chunk_size,
                    "mime": "",
                },
                fn,
            )
            if not ok:
                self._sends.pop(transfer_id, None)
                entry.status = "failed"
                return None
            self._remember_send_fn(session.peer_id, fn)
            threading.Thread(
                target=self._file_sender,
                args=(transfer_id,),
                daemon=True,
                name=f"chat-send-{transfer_id[:8]}",
            ).start()
        self._fire("_on_message", session_id, entry.to_dict())
        self._fire("_on_sessions_changed")
        return transfer_id

    def accept_file(self, session_id: str, transfer_id: str, send_fn: SendFn):
        """Start receiving an offered file.

        With ``open_to_all`` on, :meth:`_handle_chat_file_offer` calls this the
        moment an offer lands -- there is no user accept step -- so it is also
        what answers the sender's ``chat_file_accept``.  With it off the offer
        waits at ``await_accept`` and a front end calls this after the user
        answers.

        Returns ``True`` on success, ``False`` when the offer still exists but
        cannot be accepted right now (wrong session, already past the
        ``await_accept`` stage, or the receive directory refused the file),
        and ``None`` when the offer is already gone (``_receives`` no longer
        tracks it).  ``None`` stays falsy so callers testing truthiness treat
        it as a failed accept; the offer handler only ever tests
        ``is not True``, since either way the sender has to be told.
        """
        notify_peer_offline = False
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
                state["fh"] = open(temp_path, "wb")  # noqa: SIM115
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
            ok = self._send_frame(
                {
                    "msg_type": "chat_file_accept",
                    "session_id": session_id,
                    "transfer_id": transfer_id,
                },
                fn,
            )
            if ok:
                self._remember_send_fn(session.peer_id, fn)
            else:
                # The accept frame never reached the sender — roll back the
                # receive state we just created instead of leaving the write
                # handle open and the entry stuck at "sending" until the
                # stale-transfer sweeper reclaims it minutes later.
                fh = state.get("fh")
                if fh is not None:
                    with contextlib.suppress(OSError):
                        fh.close()
                    state["fh"] = None
                self._receives.pop(transfer_id, None)
                _safe_remove(state.get("temp_path"))
                state["entry"].status = "failed"
                # The wire ack never went out — the SENDER is still waiting
                # in "await_accept".  Fire the done callback (below, outside
                # the lock) so the UI drops the stale offer and the sender's
                # wait times out instead of being pinned online for the whole
                # accept window.
                notify_peer_offline = True
        if notify_peer_offline:
            self._fire(
                "_on_file_done",
                session_id,
                transfer_id,
                False,
                "",
                "peer_offline",
            )
        self._fire("_on_sessions_changed")
        return ok

    def decline_file(self, session_id: str, transfer_id: str, send_fn: SendFn) -> bool:
        """This user refused an offered file (``open_to_all`` off)."""
        with self._lock:
            state = self._receives.pop(transfer_id, None)
            if state is None:
                return False
            state["entry"].status = "declined"
            fn = send_fn or self._latest_send_fn.get(state["session"].peer_id)
            self._send_frame(
                {
                    "msg_type": "chat_file_reject",
                    "session_id": session_id,
                    "transfer_id": transfer_id,
                },
                fn,
            )
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
                with contextlib.suppress(OSError):
                    fh.close()
                state["fh"] = None
            _safe_remove(state.get("temp_path"))
            entry.status = "cancelled"
            entry.fraction = 0.0
            self._sends.pop(entry_id, None)
            self._receives.pop(entry_id, None)
            self._append_entry(
                session,
                ChatEntry(
                    entry_id=uuid.uuid4().hex[:16],
                    kind="system",
                    outgoing=False,
                    ts=time.time(),
                    text_key="chat.system.file_cancelled",
                    fmt={"name": entry.file_name},
                ),
            )
            self._send_frame(
                {
                    "msg_type": "chat_file_cancel",
                    "session_id": session.session_id,
                    "transfer_id": entry_id,
                },
                self._latest_send_fn.get(session.peer_id),
            )
            sid, tid = session.session_id, entry_id
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
            # Wake any sender thread parked in accept/complete waits -- after
            # the state dicts are cleared its lookups return None and the
            # thread exits; without this it would sleep out its full timeout
            # (up to TRANSFER_ACCEPT_TIMEOUT + COMPLETION_WAIT_TIMEOUT).
            for state in self._sends.values():
                state["accept_event"].set()
                state["complete_event"].set()
            for state in self._receives.values():
                fh = state.get("fh")
                if fh is not None:
                    with contextlib.suppress(OSError):
                        fh.close()
                    state["fh"] = None
            self._receives.clear()
            self._sends.clear()

    # ------------------------------------------------------------------
    # Snapshots for the UI
    # ------------------------------------------------------------------

    def get_sessions(self) -> list[dict]:
        with self._lock:
            sessions = sorted(
                self._sessions.values(),
                key=lambda s: s.last_activity_ts,
                reverse=True,
            )
            return [s.to_dict() for s in sessions]

    def get_messages(self, session_id: str) -> list[dict]:
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None:
                return []
            return [e.to_dict() for e in session.entries]

    def session_peer(self, session_id: str) -> dict:
        """The device a conversation belongs to, for a caller holding only its id.

        An entry is published with the session it went into and nothing else:
        the entry knows its conversation but not who is on the other end of it,
        and a surface that has to name the sender in a sentence -- the desktop
        words a notice for each arriving message -- cannot get the name out of a
        session id.  Answers empty for an unknown session rather than raising:
        the entry has already been published, and a closed conversation's last
        entry can arrive after the session behind it is gone.
        """
        with self._lock:
            session = self._session_by_sid.get(session_id)
            if session is None:
                return {}
            return {"peer_id": session.peer_id, "peer_name": session.peer_name}

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
        """Handle one ``CHAT_MSG_TYPES`` frame.  Returns True when the inner
        handler actually processed it so the host can send a ``relay_ack``
        "delivered" receipt; returns False when the frame was DROPPED (invalid
        / control-only text, no active session, flood-controlled, malformed
        offer, …) so the caller skips the ack.  Either way the host stops
        routing this type elsewhere."""
        if msg_type not in CHAT_MSG_TYPES:
            return False
        if not isinstance(payload, dict):
            return True
        sender_device_id = (sender_device_id or "").strip()
        try:
            if msg_type == "chat_invite":
                return bool(
                    self._handle_chat_invite(
                        payload,
                        sender_device_id,
                        sender_fp_short,
                        send_fn,
                    )
                )
            elif msg_type == "chat_accept":
                return bool(
                    self._handle_chat_accept(
                        payload,
                        sender_device_id,
                        sender_fp_short,
                    )
                )
            elif msg_type == "chat_decline":
                return bool(self._handle_chat_decline(payload, sender_device_id))
            elif msg_type == "chat_close":
                return bool(self._handle_chat_close(payload, sender_device_id))
            elif msg_type == "chat_text":
                return bool(self._handle_chat_text(payload, sender_device_id))
            elif msg_type == "chat_typing":
                return bool(self._handle_chat_typing(payload, sender_device_id))
            elif msg_type == "chat_ping":
                return bool(self._handle_chat_ping(payload, sender_device_id, send_fn))
            elif msg_type == "chat_pong":
                return bool(self._handle_chat_pong(payload, sender_device_id))
            elif msg_type == "chat_file_offer":
                return bool(self._handle_file_offer(payload, sender_device_id))
            elif msg_type == "chat_file_accept":
                return bool(self._handle_file_accept(payload, sender_device_id))
            elif msg_type == "chat_file_reject":
                return bool(self._handle_file_reject(payload, sender_device_id))
            elif msg_type == "chat_file_cancel":
                return bool(self._handle_file_cancel_msg(payload, sender_device_id))
            elif msg_type == "chat_file_complete":
                return bool(self._handle_file_complete_msg(payload, sender_device_id))
        except Exception:
            # Never let a malformed frame kill the recv thread.
            logger.warning("chat: error handling %s", msg_type, exc_info=True)
        return True

    # -- invitation lifecycle --------------------------------------------

    def _handle_chat_invite(self, payload, sender_id, fp_short, send_fn) -> bool:
        now = time.monotonic()
        sid = str(payload.get("session_id", ""))
        if len(sid) != 16 or any(c not in "0123456789abcdef" for c in sid):
            logger.debug("chat: invite with malformed session_id from %s", sender_id[:12])
            return False
        # Every callback below fires AFTER the lock releases (defer
        # unification): *done_fired* collects ``_on_file_done`` tuples from
        # any session teardown this invite triggers, and the outcome flags
        # decide which UI callbacks fire once the ``with`` block exits.
        done_fired: list[tuple[str, str, str]] = []
        changed = False
        mutual_accepted: tuple[str, str] | None = None  # our invite lost the race
        invite: dict | None = None
        with self._lock:
            dq = self._invite_times_in.setdefault(sender_id, deque())
            self._prune_times(dq, now, self.INVITE_RATE_WINDOW)
            if len(dq) >= self.INVITE_RATE_LIMIT:
                logger.warning("chat: invite rate limit hit for %s -- ignoring", sender_id[:12])
                return False
            dq.append(now)

            def _clean(s: str) -> str:
                return "".join(ch for ch in s if ch.isprintable())

            mine = self._sessions.get(sender_id)
            # Control chars in peer-supplied strings reach OS notifications
            # and the session list — strip them before use.
            from_name = _clean(str(payload.get("from_name", "")))[:80]
            greeting = _clean(str(payload.get("greeting", "")))[: self.MAX_GREETING_LEN]
            peer_fp = self.shorten_fingerprint(
                str(payload.get("fingerprint_short", "") or fp_short or ""),
            )[:32]

            if not self._open_to_all and mine is not None and mine.status == "inviting":
                # Both of us asked at the same moment and both are waiting.
                # Resolve deterministically: the SMALLER session id wins, so
                # the two devices converge without either user answering a
                # prompt the other cannot see.
                if sid < mine.session_id:
                    self._drop_session_locked(mine, done_fired)
                    session = self._open_incoming_locked(
                        sender_id,
                        from_name or mine.peer_name,
                        peer_fp,
                        sid,
                        send_fn,
                        done_fired,
                    )
                    self._activate_locked(session)
                    mutual_accepted = (session.session_id, session.peer_id)
                else:
                    # Ours wins; tell them to stop waiting on theirs.
                    self._send_frame(
                        {
                            "msg_type": "chat_decline",
                            "session_id": sid,
                            "reason": "duplicate",
                        },
                        send_fn,
                    )
                changed = True

            elif mine is not None and mine.status == "active":
                # Already live with this peer.  Two things can be going on,
                # and both settle the same way --
                #
                #   * the peer restarted its session without telling us (its
                #     old id is gone), or
                #   * the two of us opened sessions at the same moment, so we
                #     each minted an id the other has never heard of.
                #
                # The SMALLER id wins.  Both sides apply the same rule to the
                # same pair of ids, so they converge on one of them without a
                # further round trip; the loser adopts and echoes the winner's
                # id in its chat_accept.  Applying "always adopt theirs" here
                # instead would swap the ids on both sides and leave the two
                # sessions permanently mismatched.
                if sid < mine.session_id:
                    self._adopt_session_id_locked(mine, sid)
                # Reaffirm with whichever id survived, so a peer that lost the
                # tiebreak learns it lost.
                self._send_frame(
                    {"msg_type": "chat_accept", "session_id": mine.session_id},
                    send_fn,
                )
                self._touch_seen(mine)
                self._remember_send_fn(sender_id, send_fn)
                changed = True

            else:
                # Nothing of ours is live with this peer.  With the prompt
                # switched off the invitation stands unanswered until the user
                # answers it; with it on, the session is live the moment the
                # frame lands.
                pending = sum(1 for s in self._sessions.values() if s.status == "invited")
                if not self._open_to_all and pending >= self.PENDING_INVITE_CAP:
                    logger.info(
                        "chat: pending invite cap reached -- auto-declining %s", sender_id[:12]
                    )
                    self._send_frame(
                        {
                            "msg_type": "chat_decline",
                            "session_id": sid,
                            "reason": "busy",
                        },
                        send_fn,
                    )
                else:
                    session = self._open_incoming_locked(
                        sender_id,
                        from_name or sender_id[:12],
                        peer_fp,
                        sid,
                        send_fn,
                        done_fired,
                    )
                    if self._open_to_all:
                        # Live at once: no dialog stands between the peer's
                        # first message and this side reading it.
                        session.status = "active"
                        session.unread = 0
                        self._touch_seen(session)
                        # Echo the id so the peer can settle a crossing open.
                        # We have nothing to lose here -- we just adopted
                        # their id.
                        self._send_frame(
                            {"msg_type": "chat_accept", "session_id": session.session_id},
                            send_fn,
                        )
                    else:
                        invite = {
                            "session_id": sid,
                            "peer_id": sender_id,
                            "peer_name": session.peer_name,
                            "fingerprint_short": peer_fp,
                            "greeting": greeting,
                        }
                    changed = True

        for done_sid, tid, status in done_fired:
            self._fire("_on_file_done", done_sid, tid, False, "", status)
        if mutual_accepted is not None:
            self._fire("_on_invite_response", mutual_accepted[0], mutual_accepted[1], True)
        if invite is not None:
            self._fire("_on_incoming_invite", invite)
        if changed:
            self._fire("_on_sessions_changed")
        return True

    def _adopt_session_id_locked(self, session: ChatSession, new_sid: str) -> None:
        """Re-key *session* under *new_sid* (lock held).

        The rate buckets and the typing bookkeeping follow the id: adoption
        must not reset the flood budget (or let an attacker reset it just by
        re-inviting), and the old sid's entries must not be orphaned.
        """
        old_sid = session.session_id
        if old_sid == new_sid:
            return
        self._session_by_sid.pop(old_sid, None)
        self._session_by_sid[new_sid] = session
        session.session_id = new_sid
        for bucket_name in ("_text_times_out", "_text_times_in"):
            bucket_map = getattr(self, bucket_name)
            old_bucket = bucket_map.pop(old_sid, None)
            if old_bucket:
                bucket_map[new_sid] = old_bucket
        old_typing = self._typing_out.pop(old_sid, None)
        if old_typing is not None:
            self._typing_out[new_sid] = old_typing

    def _open_incoming_locked(
        self,
        peer_id,
        peer_name,
        fp_short,
        sid,
        send_fn,
        done_fired: list,
    ) -> ChatSession:
        session = ChatSession(
            session_id=sid,
            peer_id=peer_id,
            peer_name=(peer_name or peer_id)[:80],
            fingerprint_short=(fp_short or "")[:32],
            # The caller moves this to `active` when the invitation is taken
            # without asking; while the prompt is on it stays `invited` and
            # waits for `accept_invitation`.
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
            self._drop_session_locked(old, done_fired)
        self._sessions[peer_id] = session
        self._session_by_sid[sid] = session
        self._remember_send_fn(peer_id, send_fn)
        return session

    def _activate_locked(self, session: ChatSession) -> None:
        """Take an incoming invitation on the peer's behalf (lock held).

        Only reached while ``open_to_all`` is off: this side's own invitation
        lost the crossing-open race, so it is replaced by theirs and answered
        in the same breath.
        """
        session.status = "active"
        session.unread = 0
        session.online = True
        session.offline_announced = False
        self._touch_seen(session)
        self._send_frame(
            {"msg_type": "chat_accept", "session_id": session.session_id},
            self._latest_send_fn.get(session.peer_id),
        )

    def _handle_chat_accept(self, payload, sender_id, fp_short) -> bool:
        newly_active: tuple[str, str] | None = None
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None:
                return False
            if not self._open_to_all:
                # Our own invitation was answered.  `active` is accepted too
                # so a repeated accept from a peer that lost the tiebreak is
                # tolerated rather than refused.
                if session.status not in ("inviting", "active"):
                    return False
                if session.status == "inviting":
                    session.status = "active"
                    newly_active = (session.session_id, session.peer_id)
                self._touch_seen(session)
                if fp_short and not session.fingerprint_short:
                    session.fingerprint_short = self.shorten_fingerprint(fp_short)[:32]
            else:
                if session.status != "active":
                    return False
                # A chat_accept naming an id SMALLER than ours is the peer
                # losing the crossing-open tiebreak and telling us which id to
                # use.  Ours is normative otherwise: we already told them ours,
                # and a later accept that repeats it changes nothing.
                incoming = str(payload.get("session_id", ""))
                if incoming and incoming < session.session_id:
                    self._adopt_session_id_locked(session, incoming)
                self._touch_seen(session)
                if fp_short and not session.fingerprint_short:
                    session.fingerprint_short = self.shorten_fingerprint(fp_short)[:32]
        if newly_active is not None:
            self._fire("_on_invite_response", newly_active[0], newly_active[1], True)
        self._fire("_on_sessions_changed")
        return True

    def _drop_session_locked(self, session: ChatSession, fired: list) -> None:
        """Tear a session down without notifying the peer (lock held).

        ``_on_file_done`` callbacks for any in-flight transfers are APPENDED
        to *fired* as ``(session_id, transfer_id, status)`` tuples -- the
        caller must fire them after releasing the lock (defer unification).
        """
        self._fail_transfers_for_session(session, "cancelled", fired)
        session.status = "closed"
        self._sessions.pop(session.peer_id, None)
        if self._session_by_sid.get(session.session_id) is session:
            self._session_by_sid.pop(session.session_id, None)
        self._cleanup_rate_buckets_locked(session.peer_id, session.session_id)

    def _handle_chat_decline(self, payload, sender_id) -> bool:
        """The peer refused our invitation, or stopped waiting on one.

        Only reachable while ``open_to_all`` is off: with the prompt switched
        off there is no question for a decline to answer, and a live session
        stays live rather than being torn down by a stale frame.
        """
        refused: tuple[str, str] | None = None
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None:
                return False
            if not self._open_to_all and session.status == "inviting":
                session.status = "declined_remote"
                session.unread = 0
                refused = (session.session_id, session.peer_id)
        if refused is None:
            # Nothing of ours was waiting on an answer -- a stale decline, or
            # one from a peer that never saw the prompt.  Nothing to report.
            return True
        self._fire("_on_invite_response", refused[0], refused[1], False)
        self._fire("_on_sessions_changed")
        return True

    def _handle_chat_close(self, payload, sender_id) -> bool:
        done_fired: list[tuple[str, str, str]] = []
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None:
                # Fall back to the peer's live session regardless of id.
                session = self._sessions.get(sender_id)
            if session is None or session.status != "active":
                return False
            session.status = "closed"
            session.peer_typing_until_mono = 0.0
            self._fail_transfers_for_session(session, "peer_offline", done_fired)
            self._append_entry(
                session,
                ChatEntry(
                    entry_id=uuid.uuid4().hex[:16],
                    kind="system",
                    outgoing=False,
                    ts=time.time(),
                    text_key="chat.system.session_closed_by_peer",
                ),
            )
            sid = session.session_id
            entry = session.entries[-1]
        for sid_d, tid_d, st_d in done_fired:
            self._fire("_on_file_done", sid_d, tid_d, False, "", st_d)
        self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")
        return True

    # -- text + liveness ---------------------------------------------------

    def _handle_chat_text(self, payload, sender_id) -> bool:
        now = time.monotonic()
        text = payload.get("text")
        if not isinstance(text, str) or not text or len(text) > self.MAX_TEXT_LEN:
            logger.debug("chat: dropping invalid text from %s", sender_id[:12])
            return False
        # Strip control characters like the invite strings — this text reaches
        # OS notifications and the session preview, so a peer could otherwise
        # inject bidi-override / ESC tricks into what the user reads.  Unlike
        # the invite fields, line breaks are legitimate message content and
        # must survive (multi-line clips, indentation).
        text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
        if not text.strip():
            logger.debug("chat: dropping control-char-only text from %s", sender_id[:12])
            return False
        with self._lock:
            # By-peer fallback like ping/close: a re-invite-adopted session
            # (or a lost chat_accept) can leave the sender's session_id out
            # of sync; the text must still land instead of being dropped.
            session = self._resolve_session(
                str(payload.get("session_id", "")), sender_id
            ) or self._sessions.get(sender_id)
            if session is None or session.status != "active":
                logger.debug("chat: text from %s without active session -- dropped", sender_id[:12])
                return False
            dq = self._text_times_in.setdefault(session.session_id, deque())
            self._prune_times(dq, now, self.TEXT_RATE_WINDOW)
            if len(dq) >= self.TEXT_RATE_LIMIT:
                logger.warning("chat: text flood from %s -- dropping", sender_id[:12])
                return False
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
                entry_id=uuid.uuid4().hex[:16],
                kind="text",
                outgoing=False,
                ts=raw_ts,
                text=text,
                status="done",
            )
            self._append_entry(session, entry)
            session.unread += 1
            # The message just arrived — whatever typing indicator was showing
            # for this peer must vanish immediately, not after the timeout.
            session.peer_typing_until_mono = 0.0
            sid = session.session_id
        self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")
        return True

    def _handle_chat_typing(self, payload, sender_id) -> bool:
        """Peer's typing indicator frame (recv thread).

        Only an explicit ``typing: true`` starts the indicator; anything else
        (false, absent, malformed) is a stop signal.  The flag self-expires
        via the lazy deadline in ``to_dict`` so keep-alives are the only
        traffic needed while the peer keeps typing.  ``_on_sessions_changed``
        fires only on a VISIBLE state flip — the ~2s keep-alive stream must
        not produce a WS push every 2s.
        """
        raw = payload.get("typing", False)
        typing = raw is True
        now_mono = time.monotonic()
        changed = False
        with self._lock:
            # By-peer fallback like text/ping/close (re-invite adoption can
            # leave the sender's id out of sync).
            session = self._resolve_session(
                str(payload.get("session_id", "")), sender_id
            ) or self._sessions.get(sender_id)
            if session is None or session.status != "active":
                return False
            was = session.peer_typing_until_mono > now_mono
            if typing:
                session.peer_typing_until_mono = time.monotonic() + self.TYPING_TIMEOUT
            else:
                session.peer_typing_until_mono = 0.0
            self._touch_seen(session)
            changed = was != typing
        if changed:
            self._fire("_on_sessions_changed")
        return True

    def _handle_chat_ping(self, payload, sender_id, send_fn) -> bool:
        came_back = False
        with self._lock:
            session = self._resolve_session(
                str(payload.get("session_id", "")), sender_id
            ) or self._sessions.get(sender_id)
            if session is None or session.status != "active":
                return False
            came_back = self._touch_seen(session)
            self._send_frame(
                {"msg_type": "chat_pong", "session_id": session.session_id},
                send_fn or self._latest_send_fn.get(session.peer_id),
            )
        # A peer that had gone silent and is pinging again is 在线 again, and
        # this ping is the only thing that says so: this side stopped pinging it
        # when it went quiet (see the heartbeat's offline edge), so the pong
        # handler never sees it.  Fired below the lock, like every other.
        if came_back:
            self._fire("_on_sessions_changed")
        return True

    def _handle_chat_pong(self, payload, sender_id) -> bool:
        came_back = False
        with self._lock:
            session = self._sessions.get(sender_id)
            if session is None or session.status != "active":
                return False
            came_back = self._touch_seen(session)
        if came_back:
            self._fire("_on_sessions_changed")
        return True

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def _handle_file_offer(self, payload, sender_id) -> bool:
        transfer_id = str(payload.get("transfer_id", ""))
        size = payload.get("file_size")
        raw_name = payload.get("file_name")
        # transfer_id becomes part of the temp-file name on disk
        # (``.chat{transfer_id}.part``), so it must be strict hex — the same
        # validation session_id gets — to keep the receive path confined to
        # the receive directory.
        if (
            not isinstance(transfer_id, str)
            or len(transfer_id) != 32
            or any(c not in "0123456789abcdef" for c in transfer_id)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_FILE_SIZE
            or not isinstance(raw_name, str)
            or not raw_name
        ):
            logger.debug("chat: rejecting malformed file offer from %s", sender_id[:12])
            return False
        with self._lock:
            session = self._resolve_session(str(payload.get("session_id", "")), sender_id)
            if session is None or session.status != "active":
                logger.debug("chat: file offer outside active session -- rejected")
                return False
            if transfer_id in self._receives:
                # Duplicate offer for a receive that is already in flight.
                # Overwriting the state here would orphan its still-open .part
                # file and stall the original transfer until the sweeper, so
                # refuse the duplicate and leave the live transfer untouched.
                logger.info("chat: duplicate file offer %s -- dropping", transfer_id[:8])
                return False
            inflight = sum(1 for s in self._receives.values() if s["session"] is session)
            if inflight >= self.MAX_CONCURRENT_INCOMING_FILES:
                logger.info("chat: too many incoming files from %s -- rejecting", sender_id[:12])
                self._send_frame(
                    {
                        "msg_type": "chat_file_reject",
                        "session_id": session.session_id,
                        "transfer_id": transfer_id,
                    },
                    self._latest_send_fn.get(session.peer_id),
                )
                return False
            entry = ChatEntry(
                entry_id=transfer_id,
                kind="file",
                outgoing=False,
                ts=time.time(),
                file_name=_sanitize_file_name(raw_name),
                file_size=size,
                mime=str(payload.get("mime", ""))[:100],
                status="await_accept",
                transfer_id=transfer_id,
            )
            # The sender advertises the chunk size it will use; internet-only
            # peers send smaller chunks to fit the relay cap.  Old senders omit
            # the field, so default to CHUNK_SIZE.  A bogus value (0, negative,
            # oversized, non-numeric) is ignored rather than trusted for seeks.
            try:
                chunk_size = int(payload.get("chunk_size") or 0)
            except (TypeError, ValueError):
                chunk_size = 0
            if chunk_size <= 0 or chunk_size > self.CHUNK_SIZE:
                chunk_size = self.CHUNK_SIZE
            self._append_entry(session, entry)
            self._receives[transfer_id] = {
                "transfer_id": transfer_id,
                "session": session,
                "entry": entry,
                "file_name": entry.file_name,
                "file_size": size,
                "total_chunks": math.ceil(size / chunk_size),
                "chunk_size": chunk_size,
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
            peer_id = session.peer_id
            open_to_all = self._open_to_all
        self._fire("_on_message", sid, entry.to_dict())
        if not open_to_all:
            # The user answers this one: the offer stays at `await_accept`
            # until `accept_file` or `decline_file` is called for it, and the
            # `_on_message` above is what raises the prompt.
            self._fire("_on_sessions_changed")
            return True
        # No user stands between the offer and the bytes: start receiving at
        # once, which also answers the sender's chat_file_accept in one round
        # trip.  Doing it outside the lock keeps the callbacks accept_file
        # fires off the chat lock, like every other sweep here.
        accepted = self.accept_file(sid, transfer_id, self._latest_send_fn.get(peer_id))
        if accepted is not True:
            # The receive directory is unavailable or was not writable.  Tell
            # the sender outright -- otherwise it sits in await_accept for the
            # whole TRANSFER_ACCEPT_TIMEOUT and reports a stall that is really
            # a refusal.
            self._send_frame(
                {
                    "msg_type": "chat_file_reject",
                    "session_id": sid,
                    "transfer_id": transfer_id,
                },
                self._latest_send_fn.get(peer_id),
            )
        self._fire("_on_sessions_changed")
        return True

    def _handle_file_accept(self, payload, sender_id) -> bool:
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            state = self._sends.get(transfer_id)
            if state is None or state["session"].peer_id != sender_id:
                return False
            if state["entry"].status != "await_accept":
                return False
            state["entry"].status = "sending"
            state["accept_event"].set()
            state["last_progress_mono"] = time.monotonic()
        self._fire("_on_sessions_changed")
        return True

    def _handle_file_reject(self, payload, sender_id) -> bool:
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            state = self._sends.pop(transfer_id, None)
            if state is None or state["session"].peer_id != sender_id:
                return False
            entry = state["entry"]
            if entry.status in ("done", "cancelled"):
                self._sends[transfer_id] = state  # leave terminal state untouched
                return False
            entry.status = "declined"
            state["cancel"] = True
            state["accept_event"].set()
            state["complete_event"].set()
            session = state["session"]
            self._append_entry(
                session,
                ChatEntry(
                    entry_id=uuid.uuid4().hex[:16],
                    kind="system",
                    outgoing=False,
                    ts=time.time(),
                    text_key="chat.system.file_declined",
                    fmt={"name": entry.file_name},
                ),
            )
            sid, tid = session.session_id, transfer_id
            last = session.entries[-1]
        self._fire("_on_file_done", sid, tid, False, "", "rejected")
        self._fire("_on_message", sid, last.to_dict())
        self._fire("_on_sessions_changed")
        return True

    def _handle_file_cancel_msg(self, payload, sender_id) -> bool:
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            state = self._sends.get(transfer_id) or self._receives.get(transfer_id)
            if state is None or state["session"].peer_id != sender_id:
                return False
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
                with contextlib.suppress(OSError):
                    fh.close()
                state["fh"] = None
            _safe_remove(state.get("temp_path"))
            entry.status = "cancelled"
            self._sends.pop(transfer_id, None)
            self._receives.pop(transfer_id, None)
            session = state["session"]
            self._append_entry(
                session,
                ChatEntry(
                    entry_id=uuid.uuid4().hex[:16],
                    kind="system",
                    outgoing=False,
                    ts=time.time(),
                    text_key="chat.system.file_cancelled",
                    fmt={"name": entry.file_name},
                ),
            )
            sid, tid = session.session_id, transfer_id
            last = session.entries[-1]
        self._fire("_on_file_done", sid, tid, False, "", "cancelled_by_peer")
        self._fire("_on_message", sid, last.to_dict())
        self._fire("_on_sessions_changed")
        return True

    def _handle_file_complete_msg(self, payload, sender_id) -> bool:
        transfer_id = str(payload.get("transfer_id", ""))
        status = str(payload.get("status", ""))
        file_done_fired: list[tuple[str, str, bool, str, str]] = []
        processed = False
        with self._lock:
            # Case 1: we are the sender; the receiver confirms delivery.
            send_state = self._sends.get(transfer_id)
            if send_state is not None and send_state["session"].peer_id == sender_id:
                # Only a clean ack is a success.  The receiver can report a
                # real failure (e.g. error_size_mismatch) — surface it so the
                # transcript doesn't claim the file was delivered.
                # (The dead ``peer_completed`` write was removed in v1.0.29.1:
                # nothing reads it — ``_file_sender`` consults ``error_status``
                # alone to decide between "done" and the receiver's own error
                # code.)
                if status not in ("", "ok", "sent"):
                    send_state["error_status"] = status
                send_state["complete_event"].set()
                processed = True
            else:
                # Case 2: we are the receiver; sender says all bytes are sent.
                recv_state = self._receives.get(transfer_id)
                if recv_state is not None and recv_state["session"].peer_id == sender_id:  # noqa: SIM102
                    if recv_state.get("accepted"):
                        file_done_fired = self._finalize_receive(transfer_id)
                        processed = True
        # _finalize_receive defers its _on_file_done fires — run them outside
        # the lock so a slow UI/WS callback can't freeze the recv thread.
        for sid_f, tid_f, ok_f, path_f, st_f in file_done_fired:
            self._fire("_on_file_done", sid_f, tid_f, ok_f, path_f, st_f)
        return processed

    def handle_binary_chunk(
        self, raw_payload: dict, sender_device_id: str, send_fn: SendFn
    ) -> bool:
        """Consume a decoded ``file_chunk`` frame if it belongs to a chat
        receive-in-progress.  Returns False so the host router can fall back
        to clipboard file transfers."""
        if not isinstance(raw_payload, dict):
            return False
        transfer_id = str(raw_payload.get("transfer_id", ""))
        file_done_fired: list[tuple[str, str, bool, str, str]] = []
        with self._lock:
            state = self._receives.get(transfer_id)
            if state is None:
                return False
            if state["session"].peer_id != sender_device_id:
                logger.warning(
                    "chat: chunk for %s from unexpected peer %s -- dropped",
                    transfer_id[:8],
                    sender_device_id[:12],
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
            if (
                not isinstance(index, int)
                or not isinstance(total, int)
                or not isinstance(data, (bytes, bytearray))
                or index < 0
                or index >= state["total_chunks"]
            ):
                logger.warning("chat: malformed chunk for %s -- dropped", transfer_id[:8])
                return True
            try:
                # Chunk size travels with the offer; LAN receivers keep
                # CHUNK_SIZE, internet-relay receivers use the smaller relay
                # chunk.  Fall back to CHUNK_SIZE defensively.
                chunk_size = state.get("chunk_size") or self.CHUNK_SIZE
                state["fh"].seek(index * chunk_size)
                state["fh"].write(data)
            except OSError:
                logger.warning("chat: disk write failed for %s", transfer_id[:8], exc_info=True)
                file_done_fired = self._fail_receive_locked(transfer_id, "error_disk")
            else:
                # Only the FIRST-seen chunk index counts toward received_bytes:
                # a re-sent (duplicate) chunk would otherwise overshoot the
                # count and falsely fail the exact size check in finalize.
                if index in state["chunks_remaining"]:
                    state["received_bytes"] += len(data)
                state["chunks_remaining"].discard(index)
                state["last_progress_mono"] = time.monotonic()
                entry = state["entry"]
                entry.fraction = min(1.0, state["received_bytes"] / max(1, state["file_size"]))
                sid, tid, fraction = state["session"].session_id, transfer_id, entry.fraction
        if file_done_fired:
            for sid_f, tid_f, ok_f, path_f, st_f in file_done_fired:
                self._fire("_on_file_done", sid_f, tid_f, ok_f, path_f, st_f)
            return True
        self._fire("_on_file_progress", sid, tid, fraction)
        with self._lock:
            state = self._receives.get(transfer_id)
            if (
                state is not None
                and not state["chunks_remaining"]
                and state["received_bytes"] >= state["file_size"]
            ):
                file_done_fired = self._finalize_receive(transfer_id)
        for sid_f, tid_f, ok_f, path_f, st_f in file_done_fired:
            self._fire("_on_file_done", sid_f, tid_f, ok_f, path_f, st_f)
        return True

    def _finalize_receive(self, transfer_id: str) -> list:
        """Verify + move the finished temp file to its final name (lock held).

        Never fires callbacks inline: the ``_on_file_done`` fires are collected
        into the returned list and the CALLER fires them after releasing the
        lock (defer unification, see ``_fail_transfers_for_session``).  A slow
        UI/WS callback invoked under the chat lock would freeze the recv thread
        and every other public method behind it.
        """
        fires: list[tuple[str, str, bool, str, str]] = []
        state = self._receives.get(transfer_id)
        if state is None or state.get("_done_fired"):
            return fires
        state["_done_fired"] = True
        session = state["session"]
        entry = state["entry"]
        fh = state.get("fh")
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()
            state["fh"] = None
        temp_path = state.get("temp_path")
        try:
            actual = temp_path.stat().st_size if temp_path else -1
        except OSError:
            actual = -1
        if actual != state["file_size"] or state["received_bytes"] != state["file_size"]:
            logger.error(
                "chat: size mismatch for %s (promised %d, on disk %d, counted %d)",
                transfer_id[:8],
                state["file_size"],
                actual,
                state["received_bytes"],
            )
            _safe_remove(temp_path)
            self._receives.pop(transfer_id, None)
            entry.status = "failed"
            self._send_frame(
                {
                    "msg_type": "chat_file_complete",
                    "session_id": session.session_id,
                    "transfer_id": transfer_id,
                    "status": "error_size_mismatch",
                },
                self._latest_send_fn.get(session.peer_id),
            )
            fires.append((session.session_id, transfer_id, False, "", "error_size_mismatch"))
            return fires

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
            fires.append((session.session_id, transfer_id, False, "", "error_security"))
            return fires
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
            fires.append((session.session_id, transfer_id, False, "", "error_disk"))
            return fires
        self._receives.pop(transfer_id, None)
        entry.status = "done"
        entry.fraction = 1.0
        entry.saved_path = str(dest_path)
        self._send_frame(
            {
                "msg_type": "chat_file_complete",
                "session_id": session.session_id,
                "transfer_id": transfer_id,
                "status": "ok",
            },
            self._latest_send_fn.get(session.peer_id),
        )
        fires.append((session.session_id, transfer_id, True, str(dest_path), "success"))
        return fires

    def _fail_receive_locked(self, transfer_id: str, status: str) -> list:
        """Mark a receive failed from an I/O error (lock held).

        Must tell the SENDER: without an error frame it sends
        ``chat_file_complete`` "sent", waits out the ack timeout, and reports
        its own entry as delivered — leaving the two sides with opposite
        terminal states.

        The ``_on_file_done`` fire is collected into the returned list and
        fired by the caller after the lock releases (defer unification).
        """
        fires: list[tuple[str, str, bool, str, str]] = []
        state = self._receives.pop(transfer_id, None)
        if state is None:
            return fires
        session = state["session"]
        self._send_frame(
            {
                "msg_type": "chat_file_complete",
                "session_id": session.session_id,
                "transfer_id": transfer_id,
                "status": status,
            },
            self._latest_send_fn.get(session.peer_id),
        )
        fh = state.get("fh")
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()
        _safe_remove(state.get("temp_path"))
        entry = state["entry"]
        entry.status = "failed"
        fires.append((session.session_id, transfer_id, False, "", status))
        return fires

    def _file_sender(self, transfer_id: str) -> None:
        """Worker thread: wait for acceptance, then stream the file."""
        with self._lock:
            state = self._sends.get(transfer_id)
            if state is None:
                return
            session = state["session"]
            accept_event = state["accept_event"]
            complete_event = state["complete_event"]
        if not accept_event.wait(timeout=self.TRANSFER_ACCEPT_TIMEOUT):
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
            path = state["file_path"]
            total_chunks = state["total_chunks"]
            chunk_size = state.get("chunk_size") or self.CHUNK_SIZE
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
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    frame = encode_binary_chunk(transfer_id, index, total_chunks, chunk)
                    if not self._send_chunk_with_backoff(transfer_id, frame, send_fn):
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
        self._send_frame(
            {
                "msg_type": "chat_file_complete",
                "session_id": session.session_id,
                "transfer_id": transfer_id,
                "status": "sent",
            },
            send_fn,
        )
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
                # transfer did NOT succeed.  The entry keeps the receiver's own
                # code rather than collapsing to "declined": a decline is a
                # decision the peer's user made, and this is a fault on the
                # receiving side.  Reporting it as 已拒绝 is what sent a reader
                # to ask the other person why they refused, over a transfer
                # nobody had refused.
                state["entry"].status = err_status
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

    def _send_chunk_with_backoff(self, transfer_id: str, frame: bytes, send_fn: SendFn) -> bool:
        """One file chunk, retried while the transport keeps refusing it.

        Returns True once the transport takes it, False after
        ``CHUNK_SEND_ATTEMPTS`` — see the constant for why a refusal is worth
        retrying (relay backpressure) rather than failing on the spot.  A
        cancel during a wait ends the retries: the user said stop, and the
        caller's cancel path is the one that should report it.
        """
        for attempt in range(self.CHUNK_SEND_ATTEMPTS):
            if self._send_frame_raw(frame, send_fn):
                return True
            with self._lock:
                state = self._sends.get(transfer_id)
                cancelled = state is None or state["cancel"]
            if cancelled or attempt == self.CHUNK_SEND_ATTEMPTS - 1:
                return False
            time.sleep(self.CHUNK_SEND_RETRY_DELAY)
        return False

    # ------------------------------------------------------------------
    # Liveness / teardown
    # ------------------------------------------------------------------

    def _fail_transfers_for_session(
        self,
        session: ChatSession,
        status: str,
        done_fired: list,
    ) -> None:
        """Fail all in-flight transfers bound to *session* (lock held).

        Never fires callbacks inline: each failed transfer is APPENDED to
        *done_fired* as a ``(session_id, transfer_id, status)`` tuple and the
        caller fires ``_on_file_done`` after releasing the lock.  A slow WS/UI
        callback invoked under the chat lock would freeze the recv thread and
        every other public method behind it.
        """
        for tid in [t for t, s in self._sends.items() if s["session"] is session]:
            state = self._sends.pop(tid)
            state["cancel"] = True
            state["accept_event"].set()
            state["complete_event"].set()
            state["entry"].status = "failed"
            done_fired.append((session.session_id, tid, status))
        for tid in [t for t, s in self._receives.items() if s["session"] is session]:
            state = self._receives.pop(tid)
            fh = state.get("fh")
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.close()
            _safe_remove(state.get("temp_path"))
            state["entry"].status = "failed"
            done_fired.append((session.session_id, tid, status))

    def mark_peer_disconnected(self, peer_id: str) -> None:
        """The transport lost the connection to *peer_id*."""
        done_fired: list[tuple[str, str, str]] = []
        with self._lock:
            session = self._sessions.get(peer_id)
            if session is None:
                return
            if session.status == "active":
                if not session.offline_announced:
                    session.offline_announced = True
                    session.online = False
                    session.peer_typing_until_mono = 0.0
                    self._append_entry(
                        session,
                        ChatEntry(
                            entry_id=uuid.uuid4().hex[:16],
                            kind="system",
                            outgoing=False,
                            ts=time.time(),
                            text_key="chat.system.peer_offline",
                        ),
                    )
                    sid = session.session_id
                    entry = session.entries[-1]
                    announced = True
                else:
                    announced = False
                    sid = session.session_id
                    entry = None
                self._fail_transfers_for_session(session, "peer_offline", done_fired)
            else:
                # A pending invite/answer can no longer be delivered.
                session.status = "closed"
                announced, sid, entry = False, session.session_id, None
        for sid_d, tid_d, st_d in done_fired:
            self._fire("_on_file_done", sid_d, tid_d, False, "", st_d)
        if announced and entry is not None:
            self._fire("_on_message", sid, entry.to_dict())
        self._fire("_on_sessions_changed")

    def _heartbeat_loop(self) -> None:
        """Ping active sessions and expire stale ones."""
        while not self._heartbeat_stop.wait(timeout=self.PING_INTERVAL / 2):
            now = time.monotonic()
            fired: list[tuple[str, dict]] = []
            file_done_fired: list[tuple[str, str, str]] = []
            recv_done_fired: list[tuple[str, str, str]] = []
            recv_sessions_changed = False
            reap_sessions_changed = False
            # Separate from the reap: this one is a session that is still here
            # and still active, but whose peer has gone quiet.  The list shows
            # that as 离线, and this edge is the only place it is decided.
            offline_sessions_changed = False
            with self._lock:
                for session in list(self._sessions.values()):
                    # Reap long-dead sessions so the map cannot grow without
                    # bound over a multi-day uptime.
                    if (
                        session.status == "closed"
                        and time.time() - session.last_activity_ts > 3600.0
                    ):
                        self._sessions.pop(session.peer_id, None)
                        self._session_by_sid.pop(session.session_id, None)
                        self._cleanup_rate_buckets_locked(session.peer_id, session.session_id)
                        # `get_sessions` still hands closed rows out, so a row
                        # that is dropped here has to be announced or the list
                        # keeps showing a conversation that no longer exists.
                        # Queued, not fired: the fire below is outside the lock.
                        reap_sessions_changed = True
                        continue
                    if session.status in ("inviting", "invited"):
                        # Waiting on an answer that never came.  Only reachable
                        # while open_to_all is off -- the default mode moves a
                        # session out of these two states as it creates it.
                        if now - session.last_seen_mono > self.INVITE_ACCEPT_TIMEOUT:
                            self._drop_session_locked(session, file_done_fired)
                            # Fire below the lock like every other sweep --
                            # a slow WS/UI callback must not freeze the chat
                            # lock (and with it the recv thread).
                            reap_sessions_changed = True
                        continue
                    if session.status != "active":
                        continue
                    # Offline edge detection
                    if session.online and now - session.last_seen_mono > self.OFFLINE_AFTER:  # noqa: SIM102
                        # A session with an in-flight transfer is still alive:
                        # on a slow link (TCP retransmits) a big file can take
                        # far longer than OFFLINE_AFTER without a ping.  Only
                        # declare the peer offline -- and kill its transfers --
                        # once no transfer is actively moving.
                        if not self._session_has_active_transfer(session):
                            session.online = False
                            offline_sessions_changed = True
                            session.offline_announced = True
                            session.peer_typing_until_mono = 0.0
                            self._append_entry(
                                session,
                                ChatEntry(
                                    entry_id=uuid.uuid4().hex[:16],
                                    kind="system",
                                    outgoing=False,
                                    ts=time.time(),
                                    text_key="chat.system.peer_offline",
                                ),
                            )
                            fired.append((session.session_id, session.entries[-1].to_dict()))
                            self._fail_transfers_for_session(
                                session,
                                "peer_offline",
                                file_done_fired,
                            )
                            continue
                    if session.offline_announced and not session.online:
                        continue
                    if now - session.last_ping_mono >= self.PING_INTERVAL:
                        session.last_ping_mono = now
                        self._send_frame(
                            {"msg_type": "chat_ping", "session_id": session.session_id},
                            self._latest_send_fn.get(session.peer_id),
                        )
                # The sweepers only mutate state under the lock and return the
                # callbacks to fire — the actual fires happen BELOW so a slow
                # WS/UI callback can never freeze the chat lock.
                # (.extend, not reassignment: the offline/reap branches above
                # already queued their own _on_file_done tuples.)
                file_done_fired.extend(self._expire_stale_transfers(fired))
                recv_done_fired, recv_sessions_changed = self._expire_stale_receives(
                    defer_fire=True
                )
            for sid, entry_dict in fired:
                self._fire("_on_message", sid, entry_dict)
            for sid, tid, status in file_done_fired:
                self._fire("_on_file_done", sid, tid, False, "", status)
            for sid, tid, status in recv_done_fired:
                self._fire("_on_file_done", sid, tid, False, "", status)
            if reap_sessions_changed or recv_sessions_changed or offline_sessions_changed:
                self._fire("_on_sessions_changed")

    def _expire_stale_receives(self, defer_fire: bool = False) -> tuple[list, bool]:
        """Drop incoming file offers the user never answered (lock held).

        Only ever finds anything while ``open_to_all`` is off -- in the default
        mode an offer is taken on arrival and is past ``await_accept`` before
        the next sweep.  Without this, a few ignored offers would permanently
        pin the per-session incoming-file cap.

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
                    with contextlib.suppress(OSError):
                        fh.close()
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
            self._append_entry(
                session,
                ChatEntry(
                    entry_id=uuid.uuid4().hex[:16],
                    kind="system",
                    outgoing=False,
                    ts=time.time(),
                    text_key="chat.system.peer_offline",
                ),
            )
            fired.append((session.session_id, session.entries[-1].to_dict()))
            done_fired.append((session.session_id, tid, "error_timeout"))
        for tid, state in list(self._receives.items()):
            if now - self._transfer_stall_reference(state, now) <= self.TRANSFER_STALL_TIMEOUT:
                continue
            self._receives.pop(tid, None)
            fh = state.get("fh")
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.close()
                state["fh"] = None
            _safe_remove(state.get("temp_path"))
            entry = state["entry"]
            if entry.status in terminal:
                continue
            entry.status = "failed"
            session = state["session"]
            self._append_entry(
                session,
                ChatEntry(
                    entry_id=uuid.uuid4().hex[:16],
                    kind="system",
                    outgoing=False,
                    ts=time.time(),
                    text_key="chat.system.peer_offline",
                ),
            )
            fired.append((session.session_id, session.entries[-1].to_dict()))
            done_fired.append((session.session_id, tid, "error_timeout"))
        return done_fired
