"""Internet pairing use cases for the sidecar runtime.

This module owns only persisted pairing state and relay channel management.
Clipboard delivery is intentionally supplied by the runtime callbacks.
"""

from __future__ import annotations

import logging
import time

from internal.application.errors import ApplicationError
from internal.transport.relay import (
    decode_netpair_code,
    generate_netpair_code,
    generate_netpair_secret,
    netpair_device_tag,
)

logger = logging.getLogger(__name__)

# A peer that sent a frame within this many seconds counts as online.  Same
# window the legacy host used for the panel's per-peer badge.
NETPAIR_ONLINE_WINDOW = 90.0
# Longest alias the panel accepts (matches the legacy rename validation).
ALIAS_MAX = 120
# A relay secret is 32 random bytes as hex.  Checked on receipt because the
# frame that carries it arrives over the LAN from a peer, and a value that is
# short or non-hex would become a channel nobody can derive — the pairing would
# look enrolled and sync nothing.
RELAY_SECRET_LEN = 64
RELAY_SECRET_CHARS = "0123456789abcdef"

# --- handshake retry / expiry -------------------------------------------------
#
# The two frames that complete a code pairing (the enterer's hello and the
# generator's reply) used to be single QoS-0 publishes.  One lost frame on a
# public broker left the two sides permanently disagreeing about whether they
# were paired, with no retry and no timeout — the user's "配对上了但不同步".
# They now ride QoS 1 *and* this bounded retry: each wait retransmits the same
# frame at 1s/2s/4s/… up to the cap, and the wait itself has a deadline, after
# which the card says so instead of waiting forever.
HELLO_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
#: How long a code entered on this machine keeps retrying its hello.
WAIT_TIMEOUT = 300.0
#: How long the generator keeps re-sending the confirmation reply (a lost reply
#: is the mirror-image failure: our card says paired, the partner still waits).
CONFIRM_TIMEOUT = 300.0
#: How long a confirmed peer's frames may still ride the code-derived key while
#: it has not yet sent anything under the agreed session key.  The reply that
#: carries our public half may be lost; in that window the peer's ordinary
#: frames are accepted under the code key instead of being dropped by the
#: "already moved to key agreement" rule.  Bounded in time, and ended for good
#: as soon as the peer proves it has the session key.
CODE_KEY_GRACE = 300.0
#: Consecutive undecryptable frames from one peer before a mismatched password
#: is reported; and how long after that the report is allowed to repeat.
DECRYPT_FAILURES = 3
DECRYPT_REPORT_COOLDOWN = 60.0


def is_provisional_key(peer_id) -> bool:
    """True for the 4-char base32 device tag ``enter`` stores before the
    partner's confirmation hello re-keys it to a real id.

    A real 12-hex device id can never match that shape, so this reliably
    separates "we entered a code and are still waiting for the peer to prove
    its identity" from a confirmed internet pair.
    """
    from internal.transport.relay import NETPAIR_ALPHABET

    return isinstance(peer_id, str) and len(peer_id) == 4 and all(
        c in NETPAIR_ALPHABET for c in peer_id
    )


def is_provisional_owner(owner) -> bool:
    """True while a netpair secret's owner is not a confirmed device id.

    A code this machine generated is owned by ``pending:<code>`` and a code it
    typed is owned by the 4-char tag ``enter`` wrote; neither names a device
    yet, so the hello that answers the code is what re-keys the entry to the
    sender's real id.  A real 12-hex device id matches neither shape, which is
    what separates a confirmed channel from a pairing still in flight.
    """
    return isinstance(owner, str) and (
        owner.startswith("pending:") or is_provisional_key(owner)
    )


class InternetPairingService:
    PENDING_TTL = 10 * 60

    def __init__(self, config, save_config, relay=None, relay_state_fn=None):
        self.config = config
        self._save_config = save_config
        self.relay = relay
        # The runtime's own relay accessor (off/connecting/online/error).  It
        # comes in as a callback rather than being worked out here, because the
        # four states and their order are the runtime's fact and a second copy
        # of them here would be free to drift from the one the diagnostics and
        # the relay's own reconnect logic use.
        self.relay_state_fn = relay_state_fn
        # Set by the runtime: called with a peer id when its internet pairing
        # is severed, so the relay send ledger/queue for that peer is dropped.
        self.on_unpair = None
        # Set by the runtime: hands one enroll payload to a peer over its LAN
        # link and answers whether it went out.  Injected because the wire is
        # the runtime's, while the decision to offer is this service's — the
        # same split as ``on_unpair``.
        self.send_enroll = None
        # Peers this run has already offered our own relay secret to.  It is
        # what makes the answer below terminate; see ``handle_enroll``.
        self._enrolled: set[str] = set()
        # Codes this machine generated, now persisted in
        # ``config.netpair_pending`` (``{code: {"secret", "created_at"}}``) so a
        # restart does not forget a code the user already wrote down, and so the
        # card can show how long it has left instead of it vanishing silently.
        # At most one is live: ``generate`` retires the previous one and says so.
        self._names: dict[str, str] = {}
        self._last_seen: dict[str, float] = {}
        # When a code was entered, by the provisional tag it wrote.  Only the
        # window it has been waiting; nothing is expired off it, because the
        # entry is the user's own request and the panel shows it until they
        # undo it or the partner answers.
        #
        # Which is also why legacy's startup sweep is deliberately not ported.
        # ``_netpair_drop_provisional`` dropped every tag-keyed secret when the
        # config was loaded, and gave two reasons: the entry is "a
        # few-seconds-long placeholder", and it is one "the UI cannot show or
        # remove".  The second stopped being true when the panel grew its
        # waiting row, and the first was never true for the user -- a code
        # entered and never answered *is* the wait they are looking at, and the
        # row ends when the partner's hello arrives or they cancel it, not when
        # the process restarts.  So the entry outlives a restart: ``status``
        # reports it with no name and no clock (both were the dead process's),
        # ``channels`` keeps listening on the topic that code derives, and a
        # hello turning up days later still completes the pairing it began.
        # The cost the sweep avoided is real and is the reason the row carries
        # a Cancel: a code somebody read off the screen stays claimable until
        # the user ends it, rather than until the next restart.
        self._waiting_since: dict[str, float] = {}
        # Live handshake retry state, keyed by the target of the hello:
        # the provisional tag while we wait for a code we entered, or the peer id
        # while we feed a confirmation reply / password-change hello to a peer.
        # See ``_schedule_hello`` and ``tick``.
        self._hellos: dict[str, dict] = {}
        # Peers that have sent a frame under the channel's *agreed* key (or any
        # frame we could decrypt at all): from that moment their code-keyed
        # ordinary frames are refused again (``accepts_code_key``).
        self._session_seen: set[str] = set()
        # Peers whose pairing is still young enough to accept code-keyed
        # ordinary frames from, because the confirmation reply may have been
        # lost.  Value is the epoch the grace ends.
        self._code_key_until: dict[str, float] = {}
        # Consecutive undecryptable frames per peer, and when we last reported
        # the mismatch, for ``note_undecryptable``.
        self._decrypt_failures: dict[str, int] = {}
        self._decrypt_reported: dict[str, float] = {}
        # ``None`` until the first tick: whether the relay was online *before*
        # this process's first look is unknown, and forcing every outstanding
        # hello out on that first look would ignore the backoff it was started
        # with.  Afterwards, False→True is the retransmission trigger.
        self._relay_online_seen: bool | None = None


    def attach_relay(self, relay):
        self.relay = relay
        return self

    # ------------------------------------------------ pending pairing codes

    def _pending_records(self) -> dict[str, dict]:
        """The persisted ``{code: {"secret", "created_at"}}`` table.

        Read from the config rather than held in a second in-memory copy: the
        code has to survive a restart (the user may carry it to another machine
        and come back long after this process is gone), and the card has to show
        how long it has left.  A config object from an older build answers with
        ``{}``; assigning the empty dict keeps every writer on the same table.
        """
        records = getattr(self.config, "netpair_pending", None)
        if not isinstance(records, dict):
            records = {}
            self.config.netpair_pending = records
        return records

    def _pending_remaining(self, record, now) -> float | None:
        """Seconds a pending record has left, or None when it has none."""
        if not isinstance(record, dict):
            return None
        try:
            created = float(record.get("created_at") or 0.0)
        except (TypeError, ValueError):
            return None
        return max(0.0, created + self.PENDING_TTL - now)

    def _live_pending(self, now=None) -> dict[str, str]:
        """``{code: secret}`` for the codes that are still inside their TTL."""
        if now is None:
            now = time.time()
        records = self._pending_records()
        live = {}
        for code, record in records.items():
            if not isinstance(record, dict):
                continue
            secret = str(record.get("secret") or "")
            if not secret:
                continue
            remaining = self._pending_remaining(record, now)
            if remaining is None or remaining <= 0:
                continue
            live[code] = secret
        return live

    def _prune_pending(self, now=None) -> list[str]:
        """Drop expired codes; returns the codes that expired.

        Pure state, deliberately: this is reached from ``channels()`` (through
        ``_all_secrets``) while the relay is re-reading its subscriptions, and
        calling ``refresh_channels`` from there would recurse.  Callers that own
        a maintenance moment (``tick``/``status``) do the resubscription; every
        read path filters expired codes out anyway, so no ghost subscription is
        created in between.
        """
        if now is None:
            now = time.time()
        records = self._pending_records()
        expired = [
            code
            for code, record in records.items()
            if self._pending_remaining(record, now) is not None
            and self._pending_remaining(record, now) <= 0
        ]
        for code in expired:
            records.pop(code, None)
        return expired

    def _all_secrets(self) -> dict[str, str]:
        """Every netpair secret this device listens on, keyed by its owner.

        Persisted pairs are keyed by the peer's id; a code this machine
        generated and nobody has entered yet is keyed ``pending:<code>``.  One
        definition for both, because two callers need the same answer and a
        second copy is free to drift: ``channels()`` subscribes to these
        topics, and ``secret_for_topic`` resolves an arriving frame back to the
        secret behind it.  It did drift — the pending code was in the
        subscription and missing from the lookup, so a peer entering the code
        published its hello onto a channel this machine was listening to and
        the frame was then thrown away for want of a secret, which is why a
        pairing could never complete from either side.

        Expired codes are filtered here rather than removed: the removal (and
        the relay resubscription and the user-visible event) belongs to the
        maintenance tick, and doing it from a read would make ``channels()``
        re-enter itself.
        """
        merged = {
            f"pending:{code}": secret for code, secret in self._live_pending().items()
        }
        merged.update(dict(self.config.netpair_secrets or {}))
        return merged

    def _channel_for_topic(self, topic: str) -> tuple[str | None, str | None]:
        """The ``(owner, secret)`` behind one of our netpair topics.

        One lookup answers both, because a caller that took the secret from one
        definition and the owner from another could disagree about whose channel
        a frame arrived on.  A confirmed owner wins over a provisional one when
        the same secret is somehow held by both: the confirmed binding is the
        stricter of the two answers and the one a hello has to satisfy.
        """
        if not topic:
            return None, None
        from internal.transport.relay import netpair_topic

        provisional = None
        for owner, secret in self._all_secrets().items():
            if not secret or netpair_topic(secret) != topic:
                continue
            if is_provisional_owner(owner):
                if provisional is None:
                    provisional = (owner, secret)
                continue
            return owner, secret
        return provisional if provisional is not None else (None, None)

    def secret_for_topic(self, topic: str) -> str | None:
        """The netpair secret behind a relay topic, or None when it is not ours.

        The topic is derived from the secret alone, so it — not a frame's
        self-declared sender — is what says a frame belongs to one of our
        pairing channels.
        """
        return self._channel_for_topic(topic)[1]

    def netpair_password(self) -> str:
        """The passphrase layered onto every netpair channel key.

        The encryption password — the one password the user sets — is the source
        of truth; ``netpair_password`` is the legacy field kept as a fallback so
        a passphrase persisted before the two were merged keeps working.  Read
        fresh on every call, so a settings change reaches the channel keys
        without a restart.

        Both halves matter and they are the *same* expression legacy used.  The
        new stack read ``netpair_password`` alone, which is the same string only
        while something has mirrored it: a config loaded from 1.x carries the
        old plaintext ``encryption_password`` and no ``netpair_password``, so the
        two ends of a pairing derived different keys from the same code and every
        frame on the channel failed to decrypt — the pairing reports success and
        nothing ever syncs, with no error anywhere.
        """
        return str(getattr(self.config, "encryption_password", "") or "") or str(
            getattr(self.config, "netpair_password", "") or ""
        )

    def ensure_netpair_dh_key(self) -> str:
        """This machine's X25519 private key for netpair channels, created once.

        Persisted beside ``relay_secret``, and never transmitted: only its
        public half goes out, inside the pairing hello.  The channel key is
        derived from what the two halves agree on, so a recording of a pairing
        carries nothing that can be used to rederive it — which is what the
        35-bit code alone could never provide.
        """
        private = str(getattr(self.config, "netpair_dh_key", "") or "")
        if private:
            return private
        from internal.security.keyexchange import generate_keypair

        private, _public = generate_keypair()
        self.config.netpair_dh_key = private
        try:
            self._save_config()
        except Exception:
            # The live value is what this run's channels are derived from; it
            # is the next start that has to find it on disk.
            logger.warning("Could not persist the netpair key-agreement key", exc_info=True)
        return private

    def netpair_public_key(self) -> str:
        """The public half of :meth:`ensure_netpair_dh_key`, for the hello."""
        from internal.security.keyexchange import public_from_private

        return public_from_private(self.ensure_netpair_dh_key()) or ""

    def peer_dh_key(self, peer_id) -> str:
        """The stored public key of *peer_id*, or "" when it is not known yet."""
        if not isinstance(peer_id, str) or not peer_id:
            return ""
        return str((getattr(self.config, "netpair_peer_keys", {}) or {}).get(peer_id, "") or "")

    def note_peer_dh_key(self, peer_id, public_key) -> bool:
        """Store the public key a peer's hello carried; True when it changed.

        A change means the peer reinstalled or reset its config, and the channel
        key moves with it.  Nothing has to be told: every publish and every
        decryption derives from the stored value on the spot.  Rejecting a value
        that is not a 32-byte public key is what keeps a peer from writing
        something unusable into the config — the frame it arrived on is one a
        peer sent, so it is data to check, not a value to trust.
        """
        from internal.security.keyexchange import is_public_key

        if not isinstance(peer_id, str) or not peer_id or not is_public_key(public_key):
            return False
        keys = self.config.netpair_peer_keys
        if keys.get(peer_id) == public_key:
            return False
        keys[peer_id] = public_key
        return True

    def netpair_key_for(self, secret: str, peer_id) -> bytes | None:
        """The key one netpair channel is under, or None when it has none.

        The session key once the peer's public half is known — which is every
        frame after the pairing handshake, and the goal of the whole exercise —
        and the code-derived key before that, because the handshake hello is
        what carries the public half and so has to be readable without it.
        """
        from internal.security.keyexchange import shared_secret
        from internal.transport.relay import netpair_key, netpair_session_key

        if not secret:
            return None
        peer_key = self.peer_dh_key(peer_id)
        if peer_key:
            shared = shared_secret(self.ensure_netpair_dh_key(), peer_key)
            if shared is not None:
                return netpair_session_key(secret, shared, self.netpair_password())
        return netpair_key(secret, self.netpair_password())

    def netpair_keys_for_topic(self, topic: str) -> list[bytes]:
        """Every key a frame on *topic* may be under, session key first.

        The list is what ``RelayTransport`` walks when decrypting, so a channel
        keeps reading the handshake while the pairing moves onto the session
        key — and a peer that has not yet learned our public half is still
        heard.  Once a pairing's peer key is stored, the code-derived key stays
        on the list for exactly one kind of frame; the runtime enforces that
        (see ``_receive_relay``), because the list itself cannot tell one frame
        from another.
        """
        if not topic:
            return []
        from internal.transport.relay import netpair_key, netpair_session_key, netpair_topic

        for owner, secret in self._all_secrets().items():
            if not secret or netpair_topic(secret) != topic:
                continue
            keys = []
            peer_key = self.peer_dh_key(owner)
            if peer_key:
                from internal.security.keyexchange import shared_secret

                shared = shared_secret(self.ensure_netpair_dh_key(), peer_key)
                if shared is not None:
                    keys.append(netpair_session_key(secret, shared, self.netpair_password()))
            keys.append(netpair_key(secret, self.netpair_password()))
            return keys
        return []

    def ensure_relay_secret(self) -> str:
        """This machine's own relay secret, generated and persisted on first use.

        It never leaves the device except over the TLS-encrypted LAN link
        (``relay_enroll``), so the public broker never sees it.  Generated in one
        place because three callers need it — the channel list, the enroll frame
        and the clipboard mirror — and a second generation would hand them
        different secrets, which is a channel no peer can derive.
        """
        secret = str(getattr(self.config, "relay_secret", "") or "")
        if secret:
            return secret
        from internal.transport.relay import generate_relay_secret

        secret = generate_relay_secret()
        self.config.relay_secret = secret
        try:
            self._save_config()
        except Exception:
            # The channel below is derived from the live value either way; it is
            # the next start that has to find it on disk.
            logger.warning("Could not persist the relay secret", exc_info=True)
        return secret

    def channels(self):
        """``{topic: key}`` for every relay channel this machine listens on.

        Two families, and both are *derived* from secrets the two ends already
        hold rather than negotiated, so neither side transmits one and the broker
        cannot tell them apart:

        * a netpair channel per code secret — the confirmed pairs plus the codes
          generated and not yet answered, because the partner's hello has to have
          somewhere to arrive;
        * a LAN-relay channel per enrolled pair — the fallback for a device that
          is paired over the local network and is currently away from it, whose
          secret was exchanged over that network's TLS link.

        The second family was missing, so a LAN-paired peer that had gone home
        was published *to* and never heard *from*: its frames arrived on a topic
        this machine had not subscribed to, which reads exactly like a peer that
        is simply not there.
        """
        from internal.transport.relay import derive_key, derive_topic, netpair_topic

        # A netpair channel is a *list* of keys — the DH session key first, the
        # code-derived one after it — because the handshake hello is what
        # carries the public halves the session key is made from, so it has to
        # be readable before that key exists.  The transport tries them in
        # order.  Everything else is a single key and stays one.
        channels = {}
        for owner, secret in self._all_secrets().items():
            if not secret:
                continue
            keys = self.netpair_keys_for_topic(netpair_topic(secret))
            if not keys:
                keys = [self.netpair_key_for(secret, owner)]
            channels[netpair_topic(secret)] = keys
        enrolled = [
            (pid, secret)
            for pid, secret in (getattr(self.config, "peer_relay_secrets", {}) or {}).items()
            if secret
            # A peer reachable both ways is published to on exactly one channel
            # (the netpair one wins, as the mirror does), so it is not
            # subscribed to twice either.
            and pid not in (self.config.netpair_secrets or {})
            and getattr((getattr(self.config, "peers", {}) or {}).get(pid), "paired", False)
        ]
        if enrolled:
            my_secret = self.ensure_relay_secret()
            for _pid, peer_secret in enrolled:
                channels[derive_topic(my_secret, peer_secret)] = derive_key(
                    my_secret, peer_secret
                )
        return channels

    def topic_identity(self, topic: str) -> str | None:
        """The identity a relay topic is bound to, or None when it binds nobody.

        Every topic is derived from a shared secret, so the channel itself — and
        never a frame's self-declared ``source_device`` — says who may speak on
        it.  A confirmed channel (a netpair pair, or an enrolled LAN pair) names
        its owner's device id; a netpair channel still keyed by a provisional tag
        names that tag, which is all the code ever carried; and a
        ``pending:CODE`` channel binds nobody, because the entering partner's
        identity is established by its confirmation hello and by nothing else.
        """
        if not topic:
            return None
        from internal.transport.relay import derive_topic, netpair_topic

        my_secret = str(getattr(self.config, "relay_secret", "") or "")
        if my_secret:
            for pid, peer_secret in (getattr(self.config, "peer_relay_secrets", {}) or {}).items():
                if peer_secret and derive_topic(my_secret, peer_secret) == topic:
                    return pid
        for pid, secret in self._all_secrets().items():
            if not secret or netpair_topic(secret) != topic:
                continue
            if str(pid).startswith("pending:"):
                return None
            return pid
        return None

    def note_hello(self, peer_id, name):
        """Record that a peer said hello, and keep the name it gave.

        The name is written to the config as well as to memory, but **not saved
        here**: every caller already writes the config for the state change that
        brought the hello in, and saving inside this one made a pairing write
        twice — which the pairing tests assert against, and which is work the disk
        does not need.  Persisting the name matters because it used to live only
        in `self._names`, so a restart or an upgrade forgot it until that peer
        next connected, and a history row in the meantime printed the raw peer
        id.  Reported as "升级完以后，它的名字显示也会不太对".
        """
        if not peer_id:
            return
        self._names[peer_id] = name or ""
        self._last_seen[peer_id] = time.time()
        # Not for a provisional tag: the key is replaced by the peer's real id
        # before anything is saved, so a row for it would be a name attached to
        # nothing.
        if not name or peer_id.startswith("pending:"):
            return
        names = getattr(self.config, "netpair_names", None)
        if names is not None and names.get(peer_id) != name:
            names[peer_id] = name

    def note_relay_source(self, source, topic="", key_index=0) -> bool:
        """Record that a relay frame came from ``source``, which may re-key it.

        Four jobs, because one frame establishes all of them.

        *ALIVE* — a peer's online badge is the relay's view of that peer, and a
        hello is not the only frame a peer sends.  Only the handshake touched it,
        so a peer that synced all afternoon still read 离线 ninety seconds after
        its last hello.

        *RE-KEY (entered code)* — the machine that entered a code stores the
        provisional 4-char tag before the confirmation hello arrives, and that
        hello is a single best-effort publish.  Lost, the tag-keyed entry is
        permanent: a phantom "paired" device with no name and no real id, which
        can be sent to and never answers.  Any later frame from that peer
        re-keys it — the frame's id hashes to the tag the code carried, which is
        the same proof the hello would have offered — and the phantom
        disappears on its own.

        *RE-KEY (generated code)* — the mirror-image case: this machine
        generated the code, so its secret sits under ``pending:<code>``, and the
        tag hash proves nothing.  The topic does prove it: a frame that reached
        us *decrypted on that pending topic* was minted by somebody who holds
        the code, and a hello from it is the enterer.  Binding the pending code
        to that device here is what makes the lost-hello direction heal too.
        Only this exact topic is accepted for it, and only when there is a
        single live code (``generate`` keeps at most one).

        *SESSION SEEN* — a frame that decrypted under the channel's current
        (agreed) key, ``key_index == 0``, proves the peer has moved to key
        agreement; from then on its code-keyed ordinary frames are refused
        again (``accepts_code_key``).

        Returns whether an entry moved; the caller's relay is re-subscribed by
        the save itself.
        """
        if not isinstance(source, str) or not source:
            return False
        if source == str(getattr(self.config, "device_id", "") or ""):
            return False
        now = time.time()
        secrets = self.config.netpair_secrets or {}
        self._decrypt_failures.pop(source, None)
        if source in secrets:
            self._last_seen[source] = now
            self._note_session_frame(source, topic, key_index)
            self._ack_hellos(source)
            return False
        from internal.transport.relay import netpair_device_tag

        tag = netpair_device_tag(source)
        if tag and tag != source and tag in secrets:
            secrets[source] = secrets.pop(tag)
            self._last_seen[source] = now
            self._note_session_frame(source, topic, key_index)
            # The wait belonged to the tag; the peer has just proved it is that
            # tag.
            self._waiting_since.pop(tag, None)
            self._forget_hello(tag)
            self._code_key_until[source] = now + CODE_KEY_GRACE
            self._ack_hellos(source)
            try:
                self._save()
            except Exception:
                logger.warning("netpair re-key: saving the pairing failed", exc_info=True)
            return True
        # The pending-code direction: only the topic that code derives counts.
        moved = self._bind_pending_to(source, topic)
        if moved:
            self._note_session_frame(source, topic, key_index)
            self._ack_hellos(source)
        return moved

    def _note_session_frame(self, source, topic, key_index) -> None:
        """Mark that *source* has spoken under the channel's agreed key.

        Only when the channel really has one: an enterer still waiting for the
        confirmation reply has a channel whose only key is the code-derived one,
        so ``key_index == 0`` there means "the only key", not "the session key".
        Reading it the other way closed ``accepts_code_key`` before the reply had
        even been retried, which is exactly the window the retry exists for.
        """
        if key_index != 0:
            return
        if not topic:
            self._session_seen.add(source)
            return
        try:
            if len(self.netpair_keys_for_topic(topic)) > 1:
                self._session_seen.add(source)
        except Exception:
            logger.debug("netpair: channel key lookup failed", exc_info=True)

    def _bind_pending_to(self, source: str, topic: str) -> bool:
        """Re-key a generated code to the device a frame on its topic came from.

        The secret has to be the one this topic was derived from, so a frame on
        any other channel cannot claim a pending code; and only one live code is
        considered because ``generate`` retires the previous one — a second
        could only make "which code did this device enter?" a guess.
        """
        if not topic:
            return False
        from internal.transport.relay import netpair_topic

        live = self._live_pending()
        if len(live) != 1:
            return False
        code, secret = next(iter(live.items()))
        if netpair_topic(secret) != topic:
            return False
        records = self._pending_records()
        records.pop(code, None)
        self.config.netpair_secrets[source] = secret
        self._last_seen[source] = time.time()
        self._forget_hello(f"pending:{code}")
        self._code_key_until[source] = time.time() + CODE_KEY_GRACE
        try:
            self._save()
        except Exception:
            logger.warning("netpair pending re-key: saving the pairing failed", exc_info=True)
        return True

    def confirm_hello(self, peer_id, secret):
        """Publish the confirmation reply, and keep retrying it until acked.

        Returns whether the first attempt went out; a failure is not the end of
        the story either — the retry state below is what makes the reply survive
        a broker that is momentarily unwilling, and the mirror-image failure
        (a lost reply leaving the partner waiting forever) is what this exists
        to fix.
        """
        self._schedule_hello(peer_id, secret, "confirm", CONFIRM_TIMEOUT)
        return self._publish_hello(peer_id, secret, qos=1)

    # ------------------------------------------------------- relay enrollment

    def enroll_peers(self, peer_ids=None):
        """Offer this machine's relay secret to every paired peer.

        The relay-enrolled family in :meth:`channels` is derived from two secrets
        that have to be exchanged before either side can use it, and this is the
        offer half.  Legacy fired these once, when its relay started, so a peer
        that was offline at that instant was never enrolled and nothing retried
        — which is what left the enrolled channel reachable in one direction
        only, and why the sidecar also offers when a peer's LAN link comes up.
        Idempotent and cheap: one frame per peer, over a link that is already
        open, and the peer answers with its own.
        """
        for peer_id in list(peer_ids if peer_ids is not None else (self.config.peers or {})):
            self.offer_enroll(peer_id)

    def offer_enroll(self, peer_id) -> bool:
        """Offer our relay secret to one paired peer; True when it went out.

        LAN only, deliberately, and not because the relay could not carry it:
        the frame carries the secret that decides which public topic this
        machine listens on, so the one transport that may carry it is the one
        where the peer's identity is pinned by a certificate rather than by a
        key both ends already share.
        """
        if self.send_enroll is None or not getattr(
            self.config, "internet_sync_enabled", False
        ):
            return False
        peer = (getattr(self.config, "peers", {}) or {}).get(peer_id)
        if peer is None or not bool(getattr(peer, "paired", False)):
            return False
        payload = {"msg_type": "relay_enroll", "relay_secret": self.ensure_relay_secret()}
        try:
            sent = bool(self.send_enroll(peer_id, payload))
        except Exception:
            logger.warning(
                "relay enroll to %s failed", str(peer_id)[:12], exc_info=True
            )
            return False
        if sent:
            self._enrolled.add(peer_id)
        return sent

    def handle_enroll(self, peer_id, payload) -> bool:
        """Store a paired peer's relay secret, and say whether we owe an answer.

        Answer when the secret is new to us — the peer has rotated it, or this is
        the first we hear of it — or when we have not yet told them ours, which
        is the half that makes a one-way exchange into a two-way one.  Legacy
        answered unconditionally, and two machines running it answer each other
        forever: every answer is a frame that provokes another answer, over a LAN
        link that is already up, with nothing to make it stop.  Both halves are
        what the legacy comment was reaching for ("always answer so the other
        side learns OUR secret"); the guard is what makes it end.
        """
        if not isinstance(peer_id, str) or not peer_id or not isinstance(payload, dict):
            return False
        secret = payload.get("relay_secret")
        if (
            not isinstance(secret, str)
            or len(secret) != RELAY_SECRET_LEN
            or any(char not in RELAY_SECRET_CHARS for char in secret)
        ):
            logger.warning("Ignoring invalid relay_enroll from %s", peer_id[:12])
            return False
        changed = (getattr(self.config, "peer_relay_secrets", {}) or {}).get(peer_id) != secret
        if changed:
            # A new secret means a new channel: the old one is dead, and the
            # relay has to be re-subscribed before the answer goes out, or the
            # peer's frames land on a topic we have already left.
            self.config.peer_relay_secrets[peer_id] = secret
            try:
                self._save()
            except ApplicationError:
                logger.warning("Could not persist a peer's relay secret", exc_info=True)
        return changed or peer_id not in self._enrolled

    def _save(self):
        try:
            self._save_config()
        except Exception as exc:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save internet pairing", retryable=True
            ) from exc
        if self.relay is not None:
            self.relay.refresh_channels()

    def generate(self):
        """Make a fresh pairing code, persisted so it outlives this process.

        The code used to live in an in-memory dict with a silent 10-minute TTL:
        the user copied it, walked to another machine, and by the time it was
        typed the card had forgotten it and the hello was thrown away — with no
        "expired" anywhere.  It is now a row in ``config.netpair_pending`` (with
        its ``created_at``) so ``status`` can show the time left and a restart
        keeps it, and retiring the previous code is reported back to the caller
        so the panel can say the old one stopped working.

        This does *not* refuse when the relay is momentarily offline: a code is
        valid for minutes, the relay may reconnect during them, and failing the
        click would be a worse answer than a code plus a visible
        ``relay_offline`` flag.  ``enter`` still refuses, because it is about to
        depend on a frame going out immediately.
        """
        if not self.config.internet_sync_enabled:
            raise ApplicationError("INTERNET_SYNC_OFF", "Internet sync is disabled")
        now = time.time()
        records = self._pending_records()
        replaced = [
            code
            for code, _record in sorted(
                records.items(),
                key=lambda item: float((item[1] or {}).get("created_at") or 0.0),
            )
        ]
        # One live code at a time.  Two would leave a subscription nobody is
        # going to answer and make the pending self-heal's "which code did this
        # device enter?" a guess; the old one is retired visibly instead.
        records.clear()
        secret = generate_netpair_secret()
        code = generate_netpair_code(self.config.device_id, secret)
        records[code] = {"secret": secret, "created_at": now}
        # Persist immediately: writing the code down and restarting is a normal
        # thing to do with it.
        self._save()
        relay_state = self.relay_state()
        return {
            "code": code,
            "relay": relay_state,
            "online": relay_state == "online",
            "retryable": relay_state != "online",
            "pending_ttl": self.PENDING_TTL,
            "replaced_code": replaced[0] if replaced else None,
            "replaced_codes": replaced,
        }

    def enter(self, code):
        decoded = decode_netpair_code(code)
        if decoded is None:
            raise ApplicationError("INVALID_PAIRING_CODE", "Invalid pairing code")
        peer_id, secret = decoded
        if peer_id == netpair_device_tag(self.config.device_id):
            raise ApplicationError("INVALID_PAIRING_CODE", "Cannot pair with this device")
        if not self.config.internet_sync_enabled:
            raise ApplicationError("INTERNET_SYNC_OFF", "Internet sync is disabled")
        # The confirmation hello rides the relay.  If the relay is not actually
        # connected the secret would be persisted and the pairing would sit
        # there half-done forever — the user would see a paired device that
        # never syncs.  Refuse instead of writing it.
        if self.relay is None or getattr(self.relay, "state", "online") != "online":
            raise ApplicationError(
                "RELAY_OFFLINE", "Relay is not connected", retryable=True
            )
        now = time.time()
        self.config.netpair_secrets[peer_id] = secret
        # The age of an existing wait is not reset by entering the same code
        # again: the deadline is the user's, not this call's.
        self._waiting_since.setdefault(peer_id, now)
        self._schedule_hello(peer_id, secret, "enter", WAIT_TIMEOUT, now=now)
        try:
            self._save()
        except ApplicationError:
            # The config could not be written; leaving the live entry behind
            # would show a wait that the next restart forgets.
            self.config.netpair_secrets.pop(peer_id, None)
            self._waiting_since.pop(peer_id, None)
            self._forget_hello(peer_id)
            raise
        sent = self._publish_hello(peer_id, secret, qos=1)
        if not sent:
            # The frame is a QoS-1 publish: a relay that refuses it is not
            # connected in any useful sense, and keeping the secret would leave
            # the card waiting on a hello that never went out.  Roll back and
            # say so — retryable, so the caller can try again.
            self.config.netpair_secrets.pop(peer_id, None)
            self._waiting_since.pop(peer_id, None)
            self._forget_hello(peer_id)
            try:
                self._save()
            except Exception:
                logger.warning(
                    "netpair enter: rolling back an undeliverable wait failed",
                    exc_info=True,
                )
            raise ApplicationError(
                "RELAY_OFFLINE", "Relay is not connected", retryable=True
            )
        # Only the first half is done: the partner's reply is what turns this
        # tag-keyed entry into a device.  The caller reports the wait rather
        # than a success, because at this instant the pairing does not exist
        # yet on either machine.
        return {"peer_id": peer_id, "waiting": True}

    def _publish_hello(self, target_peer_id, secret, *, qos=1):
        """Publish a hello addressed *to* ``target_peer_id``.

        The payload's ``peer_id`` names the machine the hello is for, not the
        one sending it — the sender is named by the frame's own
        ``source_device``.  That is the whole of how either side works out
        which half of the handshake it is: its own 4-char tag coming back means
        "you generated this code", its own real device id means "you entered
        it".  Addressed with our own tag instead, the reply to an enterer named
        a machine that was not the enterer, so the one frame that completes the
        pairing was discarded by the only machine waiting for it.

        Published at QoS 1 (paho redelivers an unacknowledged QoS>0 message
        across a reconnect, and the receiver's dedup absorbs the duplicate).
        The old single QoS-0 publish is the whole reason one lost frame meant a
        pairing that could never complete.
        """
        if self.relay is None:
            return False
        from internal.protocol.codec import encode_frame
        from internal.transport.relay import netpair_key, netpair_topic

        topic = netpair_topic(secret)
        frame = encode_frame(
            {"msg_type": "netpair_hello", "peer_id": target_peer_id,
             # The public half of our key-agreement key.  This frame is the
             # only place it is ever published, and it is what the peer needs
             # to derive the channel key that this frame itself cannot be
             # protected by.
             "dh_pub": self.netpair_public_key(),
             "device_name": getattr(self.config, "device_name", "") or "", "ts": time.time()},
            source_device=self.config.device_id,
        )
        # The code-derived key, and only it.  This frame is the handshake: it
        # is what tells the peer our public half, so it cannot be protected by
        # the agreed key it exists to establish, and the channel keeps the
        # code-derived key on its read list for exactly this frame (see
        # ``netpair_keys_for_topic``).  Publishing it under the agreed key as
        # well would buy nothing — a peer holds the code key whenever it holds
        # the secret — and would cost something real: a peer that has not
        # learned our public half yet cannot open it, and the transport reads a
        # frame it cannot open as a password mismatch and says so to the user,
        # in the middle of an ordinary pairing.
        sent = False
        try:
            key = netpair_key(secret, self.netpair_password())
            sent = bool(self.relay.publish(frame, topic, key, qos=qos))
        except Exception:
            logger.debug("netpair hello publish failed", exc_info=True)
        return sent

    # ------------------------------------------- handshake retry / expiry

    def _schedule_hello(self, target, secret, role, timeout, *, now=None):
        """Remember that *target* still owes/receives a hello, and when to resend.

        Roles differ only in what ends the wait: an ``enter`` wait ends when the
        confirmation reply completes the pairing (or the deadline passes, with a
        visible failure), while a ``confirm``/``refresh`` hello (we are the
        generator, or the password changed) ends as soon as any frame from the
        peer arrives — that is the proof it is on the channel.
        """
        now = time.time() if now is None else now
        self._hellos[target] = {
            "secret": secret,
            "role": role,
            "attempts": 1,
            "next_at": now + HELLO_BACKOFF[0],
            "deadline": now + timeout,
            "created_at": now,
        }

    def _forget_hello(self, target):
        self._hellos.pop(target, None)

    def _ack_hellos(self, peer_id):
        """A frame arrived from *peer_id*: the hello it was owed has landed."""
        if not isinstance(peer_id, str):
            return
        for target in [
            key
            for key, state in self._hellos.items()
            if key == peer_id or state.get("role") == "enter"
        ]:
            # An enter wait is not settled by just any frame — the hello it waits
            # for is the one that re-keys the tag, and that is handled where the
            # wait is expired or the tag disappears.
            if self._hellos[target].get("role") == "enter":
                if target in (self.config.netpair_secrets or {}):
                    continue
                self._hellos.pop(target, None)
                continue
            self._hellos.pop(target, None)

    def tick(self, now=None) -> list[dict]:
        """Maintenance pass; returns the user-visible events it produced.

        Called from the runtime's maintenance thread.  It is the one clock the
        internet pairing has, and it dovetails the three things that used to
        have no owner: retransmitting a handshake frame that was lost, expiring
        a wait that is never going to be answered (with an event instead of
        silence), and retiring a generated code whose TTL ran out (with its
        subscription refreshed so no ghost topic stays subscribed).

        Each event is ``{"name": str, "payload": dict}``; the runtime publishes
        them under the same names the rest of the pairing traffic uses.
        """
        now = time.time() if now is None else now
        events: list[dict] = []
        online = self._relay_online()
        came_online = online and self._relay_online_seen is False
        self._relay_online_seen = online
        events.extend(self._tick_hellos(now, online, came_online))
        events.extend(self._tick_waits(now))
        expired = self._prune_pending(now)
        if expired:
            self._refresh_channels()
            for code in expired:
                events.append(
                    {
                        "name": "netpair.code.expired",
                        "payload": {"code": code, "reason": "expired"},
                    }
                )
        return events

    def _tick_hellos(self, now, online, came_online):
        """Retransmit every outstanding hello on its backoff schedule."""
        events = []
        secrets = self.config.netpair_secrets or {}
        for target, state in list(self._hellos.items()):
            role = state.get("role")
            if role == "enter" and target not in secrets:
                # The wait ended (the tag was confirmed, cancelled or expired).
                self._hellos.pop(target, None)
                continue
            if role in ("confirm", "refresh") and target not in secrets:
                self._hellos.pop(target, None)
                continue
            if now >= state.get("deadline", 0):
                # The reply is worth re-sending only for a bounded time; after
                # that the pairing stands on our side and the peer's own hello
                # retry is what re-drives it.
                self._hellos.pop(target, None)
                continue
            if now < state.get("next_at", 0) and not came_online:
                continue
            sent = self._publish_hello(target, state.get("secret", ""), qos=1)
            attempts = int(state.get("attempts", 0)) + 1
            state["attempts"] = attempts
            step = HELLO_BACKOFF[min(attempts - 1, len(HELLO_BACKOFF) - 1)]
            state["next_at"] = now + step
            if not sent and role == "enter":
                # A refusal is not the end of the wait: the relay may be back
                # before the deadline, and the tick is what keeps trying.  The
                # user-visible failure is the deadline event, not this.
                logger.debug("netpair hello to %s refused; will retry", str(target)[:12])
        return events

    def _tick_waits(self, now):
        """Expire waits that have run past their deadline, with an event.

        A wait that outlived a restart has no ``since``; the resuming process
        starts a fresh window for it (see ``status``), and the hello retry above
        gives it the same chances the original process had.
        """
        events = []
        secrets = self.config.netpair_secrets or {}
        for tag in list(self._waiting_since):
            if tag not in secrets:
                self._waiting_since.pop(tag, None)
                self._forget_hello(tag)
        now = time.time() if now is None else now
        for tag in [
            pid for pid in secrets if is_provisional_key(pid)
        ]:
            since = self._waiting_since.get(tag)
            if since is None:
                # Resumed after a restart: the clock this wait started on died
                # with the process, so give it the same window from now.
                since = now
                self._waiting_since[tag] = since
                if tag not in self._hellos:
                    self._schedule_hello(
                        tag, secrets[tag], "enter", WAIT_TIMEOUT, now=now
                    )
            deadline = since + WAIT_TIMEOUT
            if now < deadline:
                continue
            secrets.pop(tag, None)
            name = self._names.get(tag, "")
            self._waiting_since.pop(tag, None)
            self._forget_hello(tag)
            try:
                self._save()
            except Exception:
                logger.warning("netpair wait: saving the expiry failed", exc_info=True)
            events.append(
                {
                    "name": "netpair.waiting_failed",
                    "payload": {
                        "peer_id": tag,
                        "name": name,
                        "reason": "timeout",
                        "deadline": deadline,
                    },
                }
            )
        return events

    # ------------------------------------------------------- code keys / health

    def accepts_code_key(self, peer_id, msg_type="") -> bool:
        """Whether an ordinary frame from *peer_id* may ride the code-derived key.

        The transport walks a netpair channel's key list and reports which one
        opened a frame; a non-zero index means the code key.  The rule that used
        to be hard-coded — "only the handshake may use it, ever" — is right once
        both ends know each other's public half, and wrong during the window
        where the confirmation reply was lost: the enterer then has only the
        code key, so *everything* it sends rides it, and the fixed rule dropped
        all of it forever.  Here the window is explicit and short: until the
        peer has sent a frame under the agreed key (``_session_seen``) and while
        the pairing is still inside its grace period.  The handshake families
        are always admitted.
        """
        if not isinstance(peer_id, str) or not peer_id:
            return False
        if msg_type in ("netpair_hello", "netpair_unpair"):
            return True
        if is_provisional_key(peer_id):
            return True
        if peer_id in self._session_seen:
            return False
        until = self._code_key_until.get(peer_id)
        return until is not None and time.time() < until

    def note_undecryptable(self, topic, now=None) -> str | None:
        """A frame on one of our topics would not decrypt; name the peer if it is one.

        The two devices derived different keys — almost always because the
        encryption password was changed on one side only.  The channel is
        otherwise healthy (right envelope, subscribed topic), so this is the one
        moment the user can be told *why* nothing syncs.  Reported after
        :data:`DECRYPT_FAILURES` consecutive failures and then at most once per
        :data:`DECRYPT_REPORT_COOLDOWN`, so a stream of undecryptable frames does
        not turn into a stream of notifications.
        """
        now = time.time() if now is None else now
        owner, _secret = self._channel_for_topic(topic)
        if owner is None or is_provisional_owner(owner):
            # A pending code's channel has no device to name; a password
            # mismatch report for it would send the user after the wrong thing.
            return None
        # A pairing that is still settling is not a password mismatch: while a
        # lost confirmation reply is being retransmitted, the peer legitimately
        # has only the code key, so frames it cannot open are expected.  The
        # grace window is the same one ``accepts_code_key`` uses.
        until = self._code_key_until.get(owner)
        if until is not None and now < until:
            return None
        count = int(self._decrypt_failures.get(owner, 0)) + 1
        self._decrypt_failures[owner] = count
        if count < DECRYPT_FAILURES:
            return None
        last = self._decrypt_reported.get(owner, 0.0)
        if now - last < DECRYPT_REPORT_COOLDOWN:
            return None
        self._decrypt_reported[owner] = now
        self._decrypt_failures[owner] = 0
        return owner

    def rehello_peers(self, now=None) -> list[str]:
        """Re-send a hello to every confirmed peer; returns the peers targeted.

        Called when the encryption password changes: every netpair channel key
        moved, so both ends must re-establish what keys they speak.  The hello is
        the idempotent frame for it (its DH half is pinned; a repeat changes
        nothing and every peer accepts it), and sending it to peers that are on
        the *old* password is what produces the undecryptable frames that
        ``note_undecryptable`` turns into a visible "passwords differ" report —
        there is no other way for the mismatch to become visible to the user.
        """
        now = time.time() if now is None else now
        sent = []
        for peer_id, secret in list((self.config.netpair_secrets or {}).items()):
            if is_provisional_owner(peer_id) or not secret:
                continue
            if peer_id in (getattr(self.config, "removed_peers", {}) or {}):
                continue
            self._schedule_hello(peer_id, secret, "refresh", CONFIRM_TIMEOUT, now=now)
            if self._publish_hello(peer_id, secret, qos=1):
                sent.append(peer_id)
        return sent

    def _relay_online(self) -> bool:
        return self.relay is not None and getattr(self.relay, "state", "off") == "online"

    def _refresh_channels(self):
        """Best-effort resubscription after a pending-code change."""
        if self.relay is None:
            return
        try:
            self.relay.refresh_channels()
        except Exception:
            logger.debug("netpair channel refresh failed", exc_info=True)

    def handle_hello(self, source_device, incoming_tag, name="", topic="", dh_pub=""):
        """One ``netpair_hello`` off the relay, resolved into a pairing step.

        Two halves, told apart by the identity the hello addresses rather than
        by anything the sender claims about itself:

        GENERATOR — the hello names *our device tag*, the one that went into
          the code we generated.  The sender entered that code, so the pairing
          is confirmed: it is stored under the sender's real device id and the
          generated code stops being offered (left live, a second device could
          enter it and pair as well).  A reply is owed, and only from here —
          the enterer learns our real id from that reply and from nowhere else.

        ENTERER — the hello names *our real device id*.  This is the reply to a
          code we entered, so the provisional tag-keyed entry ``enter`` wrote
          is re-keyed to the sender's real id, which is what the peer list, the
          aliases and the relay send ledger are all keyed by.

        A hello addressed to neither is part of some other pair's handshake and
        is ignored rather than guessed at.  Returns what the runtime has to act
        on; ``reply`` is the half that is easy to lose, since without it the
        enterer waits on a frame that never comes.

        The frame is readable by anyone who holds the code-derived secret, so
        the checks below treat it as data rather than as identity.  A confirmed
        channel accepts only its owner as ``source_device``; a hello that
        repeats the pinned DH key exactly is idempotent and accepted (that is
        how a generator re-delivers a confirmation reply the enterer missed),
        and anything that would move a channel, replace its key or strip it
        back to the code-derived key is dropped before any state changes.  A
        code this machine has just generated or entered cannot re-bind a device
        that is already confirmed under a different secret either; the
        supported way to pair it again is to remove it first.
        """
        ignored = {"accepted": False, "role": "", "peer_id": "", "secret": "", "reply": False}
        owner, secret = self._channel_for_topic(topic)
        own_id = str(getattr(self.config, "device_id", "") or "")
        if secret is None or not isinstance(source_device, str) or not source_device:
            return ignored
        if source_device == own_id:
            # Entering one's own code on one's own machine pairs nothing.
            return ignored
        # A confirmed channel belongs to exactly one device id, and a hello is
        # a frame anyone holding the code-derived secret can mint: a source
        # that is not the owner may not move the pairing or replace its key.
        # Only a provisional entry (a code we generated, or the 4-char tag
        # ``enter`` wrote) is re-keyed to the source that has just answered it.
        provisional = is_provisional_owner(owner)
        if not provisional and source_device != owner:
            return ignored
        # A pending channel must not rebind a device that is already confirmed.
        # A new pairing code is held by whoever is meant to pair with it (and,
        # if it leaks, by anyone), so a hello on that channel that names a
        # confirmed owner must not move that owner's secret to the new code:
        # doing so cuts the real peer off its channel and lets the new code's
        # holder answer on the confirmed id.  Re-pairing an existing device
        # means removing it first (``unpair``), which is what drops the old
        # secret and its pinned key.
        existing_secrets = self.config.netpair_secrets or {}
        if (
            provisional
            and not is_provisional_owner(source_device)
            and source_device in existing_secrets
            and existing_secrets[source_device] != secret
        ):
            return ignored
        if incoming_tag == netpair_device_tag(own_id):
            role, reply = "generator", True
        elif incoming_tag == own_id:
            role, reply = "enterer", False
        else:
            return ignored
        # Validate, and match a pin, before touching any state.  Once an owner
        # has a key the hello may only repeat it exactly: a different, empty or
        # malformed value must not replace it or make the channel fall back to
        # the code-derived key.  A confirmed pair with no pin yet may adopt a
        # valid key (that is how a pre-key-agreement pairing is upgraded); a
        # provisional one keeps the legacy tolerance for peers that predate the
        # key agreement and send no ``dh_pub`` at all.  The confirmation reply
        # to a code this machine entered keeps that tolerance too: it is the
        # frame that completes the wait, the runtime may already have re-keyed
        # that wait to the sender's real id, and with no pin there is no key the
        # empty value could strip.
        from internal.security.keyexchange import is_public_key

        dh_pub_valid = is_public_key(dh_pub)
        pinned = self.peer_dh_key(owner) or self.peer_dh_key(source_device)
        if pinned and dh_pub != pinned:
            return ignored
        if not pinned and not provisional and not dh_pub_valid and role != "enterer":
            return ignored
        # Accepted from here on: only now is state allowed to change.
        if role == "generator":
            # The code has been answered: retire it, or a second device could
            # enter the same code and pair as well.
            records = self._pending_records()
            for code in [
                c for c, record in records.items()
                if isinstance(record, dict) and record.get("secret") == secret
            ]:
                records.pop(code, None)
        else:
            # Re-key the provisional entry the entered code wrote.  Only
            # provisional entries (or the source itself, when the confirmation
            # repeats) are moved; an unrelated confirmed owner that happens to
            # share the secret is left alone.
            for key in [
                k
                for k, v in (self.config.netpair_secrets or {}).items()
                if v == secret and (is_provisional_owner(k) or k == source_device)
            ]:
                self.config.netpair_secrets.pop(key, None)
        self.config.netpair_secrets[source_device] = secret
        self._waiting_since.pop(source_device, None)
        if is_provisional_owner(owner) and owner != source_device:
            # This hello is the answer the tag was waiting for: the wait and its
            # retransmissions both end here.
            self._waiting_since.pop(owner, None)
            self._forget_hello(owner)
        self._forget_hello(source_device)
        # A lost confirmation reply leaves the peer with only the code key: its
        # ordinary frames are accepted under it for a bounded grace period
        # (see ``accepts_code_key``), so the pairing still works while the reply
        # is being retransmitted.
        self._code_key_until[source_device] = time.time() + CODE_KEY_GRACE
        self.note_hello(source_device, name or "")
        # The peer's public key, which turns this channel from code-keyed into
        # DH-keyed.  Recorded before the reply is published (the caller does
        # that next), so the reply already rides the session key.
        self.note_peer_dh_key(source_device, dh_pub)
        try:
            self._save()
        except Exception:
            # An inbound relay frame has no caller to report a failure to, and
            # letting this out would take the relay listener down with it.  The
            # pairing stands for this run either way — it is the next save that
            # has to reach the disk, and the panel reads the live state.
            logger.warning("netpair hello: saving the pairing failed", exc_info=True)
        return {
            "accepted": True,
            "role": role,
            "peer_id": source_device,
            "secret": secret,
            "reply": reply,
        }

    def relay_state(self):
        """This machine's own relay link, in the runtime's four words.

        Reported beside the peers because a peer's ``online`` is the relay's
        view of *that peer*: with our own link down every peer reads 离线, and
        the two situations — they are away, or we are — call for different
        things from the reader.  ``off`` is the honest answer when internet
        sync is disabled rather than a connection that is merely lagging.
        """
        if self.relay_state_fn is not None:
            try:
                return self.relay_state_fn()
            except Exception:
                return "connecting"
        # No runtime accessor injected (a host building the service by hand, a
        # test): the attached transport's own state is the honest answer rather
        # than "connecting" forever.
        if self.relay is not None:
            return str(getattr(self.relay, "state", "connecting") or "connecting")
        return "off" if not getattr(self.config, "internet_sync_enabled", False) else "connecting"

    def paired_peers(self):
        """The confirmed internet pairs, in the shape one device row needs.

        Split out of :meth:`status` because the device list draws these peers as
        cards and has to answer exactly what the pairing card answers — the
        alias, the relay's own view of liveness, when it was last heard from —
        and a second computation of ``online`` would be a second answer to the
        same question, free to drift the moment either window changes.

        A provisional base32 tag key is not a device and is absent from here for
        the same reason it is absent from ``status``'s peer list: it only means
        "we entered a code and the peer has not confirmed its identity yet".

        Neither is a device this machine has removed.  Removing one ends both of
        its routes, and the secret goes with the removal; this is the answer for
        the installs that removed a device on a build where it did not, whose
        secret is still on disk.  The relay gate refuses the same ids
        (``_peer_is_internet_reachable``), so the peer is already locked out —
        what this keeps is the pairing card from listing a device the device
        list shows as removed.
        """
        removed = set(getattr(self.config, "removed_peers", {}) or {})
        now = time.time()
        peers = []
        for pid in sorted(self.config.netpair_secrets or {}):
            if is_provisional_key(pid) or pid in removed:
                continue
            name = self._names.get(pid, "")
            if not name:
                # The name this peer gave at hello, which `note_hello` writes to
                # disk as well as to `_names` — because `_names` is memory, so a
                # restart or an upgrade forgot it and this card fell straight
                # from "no name" to the raw peer id.  That is the same defect
                # `netpair_names` was added for; this reader was the one that
                # still skipped it.
                name = (getattr(self.config, "netpair_names", None) or {}).get(pid, "")
            if not name:
                peer = (getattr(self.config, "peers", {}) or {}).get(pid)
                name = getattr(peer, "device_name", "") if peer is not None else ""
            last_seen = self._last_seen.get(pid)
            peers.append(
                {
                    "peer_id": pid,
                    "name": name,
                    "alias": (self.config.netpair_aliases or {}).get(pid, ""),
                    "online": last_seen is not None and now - last_seen <= NETPAIR_ONLINE_WINDOW,
                    "last_seen": last_seen,
                    "paired": True,
                }
            )
        return peers

    def status(self):
        now = time.time()
        # Reads own the maintenance moment the panel poll gives them: retiring
        # an expired code here is what makes the "code expired" state visible the
        # next time the card asks (the tick event covers the push side).
        expired = self._prune_pending(now)
        if expired:
            self._refresh_channels()
        live = self._live_pending(now)
        generated_code = None
        pending_ttl = None
        if live:
            # Newest first; generate() keeps one live, but an older config may
            # still hold several.
            records = self._pending_records()
            generated_code = max(
                live,
                key=lambda code: float((records.get(code) or {}).get("created_at") or 0.0),
            )
            pending_ttl = self._pending_remaining(records.get(generated_code), now)
        waiting = []
        for pid in sorted(self.config.netpair_secrets or {}):
            # A code entered on this machine whose partner has not answered is
            # not a device and must not be listed as one — but it is the state
            # the page is *in* after a code is submitted, and hiding it left the
            # one moment the user most needs an answer with nothing on screen at
            # all.  It is reported instead under its own name, where it can say
            # "waiting" and offer a way back out.
            if is_provisional_key(pid):
                since = self._waiting_since.get(pid)
                waiting.append(
                    {
                        "peer_id": pid,
                        "name": self._names.get(pid, ""),
                        "since": since,
                        # Absolute epoch seconds, or None for a wait resumed by a
                        # process that never saw it start (the clock died with
                        # the old process; the row can still say "waiting").
                        "deadline": (since + WAIT_TIMEOUT) if since else None,
                    }
                )
        relay_state = self.relay_state()
        enabled = bool(getattr(self.config, "internet_sync_enabled", False))
        relay_broker = ""
        if relay_state == "online" and self.relay is not None:
            try:
                relay_broker = str(getattr(self.relay, "current_broker", "") or "")
            except Exception:
                relay_broker = ""
        return {
            "generated_code": generated_code,
            # Seconds the generated code has left, for the countdown the card
            # needs; None when no code is live.
            "pending_ttl": pending_ttl,
            "relay": relay_state,
            # The broker this machine is connected through, so the card can
            # name the relay rather than only saying that one exists.
            "relay_broker": relay_broker,
            "enabled": enabled,
            # Whether a pairing can complete right now.  The phone panel shows
            # the two independently (its relay badge), the desktop uses this for
            # the "生成配对码" warning.  Deliberately not a refusal: a code made
            # while the relay reconnects is still usable once it is back.
            "can_pair": bool(enabled and relay_state == "online"),
            "relay_offline": relay_state != "online",
            "peers": self.paired_peers(),
            # Codes entered on this machine whose partner has not answered yet.
            # Distinct from ``generated_code``, which is the other direction: a
            # code *we* made and are waiting for somebody to type.
            "waiting": waiting,
        }

    def rename(self, peer_id, name):
        if not isinstance(peer_id, str) or peer_id not in (self.config.netpair_secrets or {}):
            raise ApplicationError("NOT_FOUND", "Internet peer not found")
        if not isinstance(name, str):
            raise ApplicationError("INVALID_NAME", "Alias must be text")
        name = name.strip()
        if len(name) > ALIAS_MAX:
            raise ApplicationError("INVALID_NAME", "Alias is too long")
        if name:
            self.config.netpair_aliases[peer_id] = name
        else:
            self.config.netpair_aliases.pop(peer_id, None)
        # The same fact under its other spelling.  A device paired by code and on
        # the LAN at once is renamed from either card, and the LAN row reads
        # `notes` — so writing only the alias left that row showing the name the
        # user had just replaced, which is the one-name rule failing in the
        # direction the device list can see.
        #
        # Only when a saved peer already exists.  An internet-only pairing has no
        # `PeerInfo`, and minting one here would claim a LAN pairing this machine
        # does not have — `_persist` and the pairing repository would both be
        # told about a peer with no certificate to pin.
        peer = (getattr(self.config, "peers", None) or {}).get(peer_id)
        if peer is not None:
            peer.notes = name
        self._save()
        return {"ok": True}

    def unpair(self, peer_id):
        """Remove one internet pair, and tell the partner about it.

        The local half was never in doubt; the partner half is what this adds.
        Remote removal used to be one-sided: this device stopped publishing and
        listening, while the other still listed it as paired, kept mirroring
        clipboard frames into a topic nobody subscribed to, and marked every one
        of them 未送达.  A ``netpair_unpair`` frame on the (confirmed) channel
        fixes that; a pairing still waiting has no partner to tell yet, so it is
        dropped locally, which is exactly what cancelling the wait means.
        """
        if peer_id not in (self.config.netpair_secrets or {}):
            raise ApplicationError("NOT_FOUND", "Internet peer not found")
        self._publish_unpair_notice(peer_id)
        self._drop_state(peer_id)
        self._save()
        return {"ok": True}

    def interrupt_waiting(self, peer_id) -> bool:
        """Cancel a provisional wait (an entered code whose partner never answered).

        The same cleanup ``unpair`` runs, without the ``NOT_FOUND`` an unpair of
        a real peer would raise: the panel's Cancel button is clicking on a row
        the peers list does not contain.
        """
        if not is_provisional_key(peer_id) or peer_id not in (
            self.config.netpair_secrets or {}
        ):
            return False
        self._drop_state(peer_id)
        self._save()
        return True

    def handle_unpair_notice(self, peer_id) -> dict | None:
        """The partner removed this pairing; drop our side and say who it was.

        Returns ``{"peer_id", "name"}`` when an entry was removed (the runtime
        publishes the panel event and refreshes the device list), or None when
        there was nothing of that id to drop.
        """
        if not isinstance(peer_id, str) or peer_id not in (
            self.config.netpair_secrets or {}
        ):
            return None
        name = self._names.get(peer_id, "") or (
            getattr(self.config, "netpair_names", None) or {}
        ).get(peer_id, "")
        self._drop_state(peer_id)
        try:
            self._save()
        except Exception:
            logger.warning(
                "netpair unpair notice: saving the removal failed", exc_info=True
            )
        return {"peer_id": peer_id, "name": name}

    def _publish_unpair_notice(self, peer_id) -> bool:
        """One ``netpair_unpair`` frame to a confirmed partner, at QoS 1."""
        if self.relay is None or is_provisional_key(peer_id):
            return False
        secret = (self.config.netpair_secrets or {}).get(peer_id)
        if not secret:
            return False
        key = self.netpair_key_for(secret, peer_id)
        if key is None:
            return False
        from internal.protocol.codec import encode_frame
        from internal.transport.relay import netpair_topic

        frame = encode_frame(
            {
                "msg_type": "netpair_unpair",
                "peer_id": str(getattr(self.config, "device_id", "") or ""),
                "ts": time.time(),
            },
            source_device=self.config.device_id,
        )
        try:
            return bool(self.relay.publish(frame, netpair_topic(secret), key, qos=1))
        except Exception:
            logger.debug("netpair unpair notice publish failed", exc_info=True)
            return False

    def _drop_state(self, peer_id) -> None:
        """Every local trace of one pairing, short of a save/refresh.

        Shared by ``unpair``, ``interrupt_waiting`` and the partner's unpair
        notice so the three cannot drift: each one has to remove the same six
        structures, and leaving any behind is a pairing that still half exists.
        """
        self.config.netpair_secrets.pop(peer_id, None)
        self.config.netpair_aliases.pop(peer_id, None)
        # The name hello learned, which used to be left on disk: the card was
        # gone but the name outlived it, and a re-pair showed the old one first.
        (getattr(self.config, "netpair_names", {}) or {}).pop(peer_id, None)
        # The peer's key-agreement public key goes with the secret it keys: a
        # pairing that is undone leaves no material behind, and one that is
        # made again stores the peer's current key from its next hello.
        (getattr(self.config, "netpair_peer_keys", {}) or {}).pop(peer_id, None)
        # The secret this peer enrolled over the LAN goes as well, and this is
        # the half that actually severs the internet route.  A device paired
        # both ways holds *two* credentials, and the enrolled one is keyed on
        # the LAN pin rather than on the code -- which this unpair deliberately
        # leaves alone.  Dropping only the netpair secret therefore left
        # ``_peer_is_internet_reachable`` answering True off the survivor: every
        # clipboard, chat and attachment frame kept flowing to a peer the card
        # had just stopped listing, over the enrolled topic, and the inbound
        # gate kept trusting it.  Nothing in the UI could stop it either, since
        # the enrolled channel has no row of its own.
        #
        # This does not survive a re-enrollment: if the two machines are on the
        # same LAN, the peer's next ``relay_enroll`` offer stores its secret
        # again -- which is the LAN fallback working as designed, not the
        # unpair coming undone, and the device row still shows the LAN pairing
        # that owns it.
        (getattr(self.config, "peer_relay_secrets", {}) or {}).pop(peer_id, None)
        self._enrolled.discard(peer_id)
        self._names.pop(peer_id, None)
        self._last_seen.pop(peer_id, None)
        # Which is also how a pairing that is still waiting is called off: its
        # key is the provisional tag, and dropping it is what ends the wait.
        self._waiting_since.pop(peer_id, None)
        self._hellos.pop(peer_id, None)
        self._session_seen.discard(peer_id)
        self._code_key_until.pop(peer_id, None)
        self._decrypt_failures.pop(peer_id, None)
        self._decrypt_reported.pop(peer_id, None)
        # An unpaired peer must stop burning relay retries, and its base64
        # payloads must not linger on disk in relay_pending.json.
        if self.on_unpair is not None:
            self.on_unpair(peer_id)
