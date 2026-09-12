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
        self._pending: dict[str, str] = {}
        self._pending_at: float = 0.0
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

    def attach_relay(self, relay):
        self.relay = relay
        return self

    def _prune_pending(self) -> None:
        if self._pending and time.time() - self._pending_at > self.PENDING_TTL:
            self._pending.clear()

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
        """
        self._prune_pending()
        merged = {f"pending:{code}": secret for code, secret in self._pending.items()}
        merged.update(dict(self.config.netpair_secrets or {}))
        return merged

    def secret_for_topic(self, topic: str) -> str | None:
        """The netpair secret behind a relay topic, or None when it is not ours.

        The topic is derived from the secret alone, so it — not a frame's
        self-declared sender — is what says a frame belongs to one of our
        pairing channels.
        """
        if not topic:
            return None
        from internal.transport.relay import netpair_topic
        for secret in self._all_secrets().values():
            if secret and netpair_topic(secret) == topic:
                return secret
        return None

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
        from internal.transport.relay import (
            derive_key,
            derive_topic,
            netpair_key,
            netpair_topic,
        )

        channels = {
            netpair_topic(secret): netpair_key(secret, self.netpair_password())
            for secret in self._all_secrets().values()
            if secret
        }
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
        if peer_id:
            self._names[peer_id] = name or ""
            self._last_seen[peer_id] = time.time()

    def note_relay_source(self, source) -> bool:
        """Record that a relay frame came from ``source``, which may re-key it.

        Two jobs, because one frame establishes both.

        *ALIVE* — a peer's online badge is the relay's view of that peer, and a
        hello is not the only frame a peer sends.  Only the handshake touched it,
        so a peer that synced all afternoon still read 离线 ninety seconds after
        its last hello.

        *RE-KEY* — the machine that entered a code stores the provisional 4-char
        tag before the confirmation hello arrives, and that hello is a single
        best-effort publish.  Lost, the tag-keyed entry is permanent: a phantom
        "paired" device with no name and no real id, which can be sent to and
        never answers.  Any later frame from that peer re-keys it — the frame's
        id hashes to the tag the code carried, which is the same proof the hello
        would have offered — and the phantom disappears on its own.

        Returns whether the entry moved; the caller's relay is re-subscribed by
        the save itself.
        """
        if not isinstance(source, str) or not source:
            return False
        secrets = self.config.netpair_secrets or {}
        if source in secrets:
            self._last_seen[source] = time.time()
            return False
        from internal.transport.relay import netpair_device_tag

        tag = netpair_device_tag(source)
        if not tag or tag == source or tag not in secrets:
            return False
        secrets[source] = secrets.pop(tag)
        self._last_seen[source] = time.time()
        # The wait belonged to the tag; the peer has just proved it is that tag.
        self._waiting_since.pop(tag, None)
        try:
            self._save()
        except Exception:
            logger.warning("netpair re-key: saving the pairing failed", exc_info=True)
        return True

    def confirm_hello(self, peer_id, secret):
        return self._publish_hello(peer_id, secret)

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
        if not self.config.internet_sync_enabled:
            raise ApplicationError("INTERNET_SYNC_OFF", "Internet sync is disabled")
        secret = generate_netpair_secret()
        code = generate_netpair_code(self.config.device_id, secret)
        self._pending = {code: secret}
        self._pending_at = time.time()
        if self.relay is not None:
            self.relay.refresh_channels()
        return {"code": code}

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
        self.config.netpair_secrets[peer_id] = secret
        self._waiting_since[peer_id] = time.time()
        self._save()
        self._publish_hello(peer_id, secret)
        # Only the first half is done: the partner's reply is what turns this
        # tag-keyed entry into a device.  The caller reports the wait rather
        # than a success, because at this instant the pairing does not exist
        # yet on either machine.
        return {"peer_id": peer_id, "waiting": True}

    def _publish_hello(self, target_peer_id, secret):
        """Publish a hello addressed *to* ``target_peer_id``.

        The payload's ``peer_id`` names the machine the hello is for, not the
        one sending it — the sender is named by the frame's own
        ``source_device``.  That is the whole of how either side works out
        which half of the handshake it is: its own 4-char tag coming back means
        "you generated this code", its own real device id means "you entered
        it".  Addressed with our own tag instead, the reply to an enterer named
        a machine that was not the enterer, so the one frame that completes the
        pairing was discarded by the only machine waiting for it.
        """
        if self.relay is None:
            return False
        from internal.protocol.codec import encode_frame
        from internal.transport.relay import netpair_key, netpair_topic
        frame = encode_frame(
            {"msg_type": "netpair_hello", "peer_id": target_peer_id,
             "device_name": getattr(self.config, "device_name", "") or "", "ts": time.time()},
            source_device=self.config.device_id,
        )
        return bool(self.relay.publish(
            frame, netpair_topic(secret),
            netpair_key(secret, self.netpair_password()),
        ))

    def handle_hello(self, source_device, incoming_tag, name="", topic=""):
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
        """
        ignored = {"accepted": False, "role": "", "peer_id": "", "secret": "", "reply": False}
        secret = self.secret_for_topic(topic)
        if secret is None or not isinstance(source_device, str) or not source_device:
            return ignored
        if source_device == self.config.device_id:
            # Entering one's own code on one's own machine pairs nothing.
            return ignored
        own_id = str(getattr(self.config, "device_id", "") or "")
        if incoming_tag == netpair_device_tag(own_id):
            role, reply = "generator", True
            for code in [c for c, s in self._pending.items() if s == secret]:
                self._pending.pop(code, None)
        elif incoming_tag == own_id:
            role, reply = "enterer", False
            for key in [k for k, v in (self.config.netpair_secrets or {}).items() if v == secret]:
                self.config.netpair_secrets.pop(key, None)
        else:
            return ignored
        self.config.netpair_secrets[source_device] = secret
        self._waiting_since.pop(source_device, None)
        self.note_hello(source_device, name or "")
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
        return "off" if not getattr(self.config, "internet_sync_enabled", False) else "connecting"

    def status(self):
        now = time.time()
        self._prune_pending()
        peers = []
        waiting = []
        for pid in sorted(self.config.netpair_secrets or {}):
            # A provisional base32 tag key (see enter) is not a confirmed pair:
            # it only means "we entered a code and the peer has not confirmed
            # its identity yet".  It is not a device and must not be listed as
            # one — but it is the state the page is *in* after a code is
            # submitted, and hiding it left the one moment the user most needs
            # an answer with nothing on screen at all.  It is reported instead
            # under its own name, where it can say "waiting" and offer a way
            # back out.
            if is_provisional_key(pid):
                waiting.append(
                    {
                        "peer_id": pid,
                        "name": self._names.get(pid, ""),
                        "since": self._waiting_since.get(pid),
                    }
                )
                continue
            name = self._names.get(pid, "")
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
        return {
            "generated_code": next(iter(self._pending), None),
            "relay": self.relay_state(),
            "enabled": bool(getattr(self.config, "internet_sync_enabled", False)),
            "peers": peers,
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
        self._save()
        return {"ok": True}

    def unpair(self, peer_id):
        if peer_id not in (self.config.netpair_secrets or {}):
            raise ApplicationError("NOT_FOUND", "Internet peer not found")
        self.config.netpair_secrets.pop(peer_id, None)
        self.config.netpair_aliases.pop(peer_id, None)
        self._names.pop(peer_id, None)
        self._last_seen.pop(peer_id, None)
        # Which is also how a pairing that is still waiting is called off: its
        # key is the provisional tag, and dropping it is what ends the wait.
        self._waiting_since.pop(peer_id, None)
        # An unpaired peer must stop burning relay retries, and its base64
        # payloads must not linger on disk in relay_pending.json.
        if self.on_unpair is not None:
            self.on_unpair(peer_id)
        self._save()
        return {"ok": True}
