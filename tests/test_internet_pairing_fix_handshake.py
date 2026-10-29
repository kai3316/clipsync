"""Internet pairing regressions: the handshake is retried, visible and bounded.

Covers the review's 严重-5 (a single QoS-0 hello/reply meant a pairing that
could never complete), 严重-6 (generated codes expired silently and lived only
in memory), 中-1 (the mirror-image lost-hello direction and offline generate),
中-4 (a mismatched password had no visible report), 中-9 (one-sided unpair) and
中-10 (regenerating silently invalidated the previous code).

The service is driven with a fake relay that records publishes; nothing here
opens a socket or touches the disk (the config save is a counted callback).
"""

from __future__ import annotations

import time

import pytest

from internal.application.errors import ApplicationError
from internal.config.config import Config
from internal.infrastructure.runtime.internet_pairing import (
    DECRYPT_REPORT_COOLDOWN,
    WAIT_TIMEOUT,
    InternetPairingService,
    is_provisional_key,
)
from internal.protocol.codec import decode_message
from internal.security.keyexchange import generate_keypair
from internal.transport.relay import (
    decode_netpair_code,
    generate_netpair_code,
    generate_netpair_secret,
    netpair_device_tag,
    netpair_topic,
)

PENDING_TTL = InternetPairingService.PENDING_TTL


class FakeRelay:
    """Stand-in for RelayTransport: records what the broker actually took.

    ``attempts`` counts every publish call (a refusal included); ``published``
    holds only the frames the broker accepted, matching what the transport
    returns True for.
    """

    def __init__(self, state="online", ok=True):
        self.state = state
        self.ok = ok
        self.published = []
        self.attempts = 0
        self.refresh_calls = 0

    def publish(self, frame, topic, key, qos=0):
        self.attempts += 1
        if self.ok:
            self.published.append((frame, topic, key, qos))
        return self.ok

    def refresh_channels(self):
        self.refresh_calls += 1


def make_service(device_id="local", device_name="Local", relay=None, **overrides):
    config = Config(device_id=device_id, device_name=device_name, encryption_enabled=False)
    config.netpair_dh_key = generate_keypair()[0]
    config.internet_sync_enabled = True
    for name, value in overrides.items():
        setattr(config, name, value)
    saves = []
    service = InternetPairingService(config, lambda: saves.append(None), relay=relay)
    return config, service, saves


def published_hellos(relay):
    out = []
    for frame, topic, _key, qos in relay.published:
        msg = decode_message(frame)
        if msg is not None and msg.msg_type == "netpair_hello":
            out.append((msg, topic, qos))
    return out


A_ID = "aaaaaaaaaaaa"
B_ID = "bbbbbbbbbbbb"


# ------------------------------------------------ 严重-6: persisted pending codes


def test_a_generated_code_is_persisted_and_reports_its_remaining_time():
    config, service, saves = make_service(relay=FakeRelay())
    result = service.generate()

    code = result["code"]
    peer_tag, secret = decode_netpair_code(code)
    assert result["online"] is True and result["relay"] == "online"
    assert config.netpair_pending == {
        code: {"secret": secret, "created_at": pytest.approx(time.time(), abs=30)}
    }
    assert saves == [None]  # persisted immediately: the user writes the code down

    status = service.status()
    assert status["generated_code"] == code
    assert 0 < status["pending_ttl"] <= PENDING_TTL
    assert (status["can_pair"], status["relay_offline"]) == (True, False)


def test_regenerating_retires_the_previous_code_and_says_so():
    config, service, _saves = make_service()
    first = service.generate()
    second = service.generate()

    assert second["replaced_code"] == first["code"]
    assert second["replaced_codes"] == [first["code"]]
    assert set(config.netpair_pending) == {second["code"]}
    assert service.status()["generated_code"] == second["code"]
    # The retired code's topic is no longer listened on, and a hello entering
    # it can no longer pair anything.
    _tag, old_secret = decode_netpair_code(first["code"])
    assert netpair_topic(old_secret) not in service.channels()
    assert service.handle_hello(
        A_ID, netpair_device_tag(config.device_id), "A",
        netpair_topic(old_secret), dh_pub=generate_keypair()[1],
    )["accepted"] is False


def test_an_expired_code_is_an_event_and_its_subscription_is_dropped():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    result = service.generate()
    refreshed = relay.refresh_calls
    config.netpair_pending[result["code"]]["created_at"] -= PENDING_TTL + 5

    events = service.tick()

    assert [event["name"] for event in events] == ["netpair.code.expired"]
    assert events[0]["payload"]["code"] == result["code"]
    assert config.netpair_pending == {}
    assert relay.refresh_calls > refreshed  # no ghost subscription
    assert service.status()["generated_code"] is None
    assert service.status()["pending_ttl"] is None


def test_a_code_that_outlived_a_restart_is_still_offered_and_listened_on():
    """The pending table is config, so a restart is not the end of the code."""
    config, first, _saves = make_service()
    result = first.generate()
    _tag, secret = decode_netpair_code(result["code"])

    reopened_config = Config(device_id="local", device_name="Local", encryption_enabled=False)
    reopened_config.netpair_dh_key = generate_keypair()[0]
    reopened_config.internet_sync_enabled = True
    reopened_config.netpair_pending = dict(config.netpair_pending)
    reopened = InternetPairingService(reopened_config, lambda: None)

    assert reopened.status()["generated_code"] == result["code"]
    assert netpair_topic(secret) in reopened.channels()


# ------------------------------------------- 严重-5: hello retried, deadline, rollback


def test_entering_a_code_publishes_the_hello_at_qos_one_and_shows_a_deadline():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    code = generate_netpair_code(B_ID, secret)

    entered = service.enter(code)

    assert entered == {"peer_id": netpair_device_tag(B_ID), "waiting": True}
    hellos = published_hellos(relay)
    assert [(topic, qos) for _msg, topic, qos in hellos] == [(netpair_topic(secret), 1)]
    waiting = service.status()["waiting"]
    assert waiting[0]["peer_id"] == netpair_device_tag(B_ID)
    assert waiting[0]["deadline"] == pytest.approx(time.time() + WAIT_TIMEOUT, abs=30)


def test_a_waiting_hello_is_retransmitted_on_a_backoff():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    service.enter(generate_netpair_code(B_ID, secret))
    started = time.time()
    assert len(published_hellos(relay)) == 1

    # Before the first backoff step nothing goes out ...
    service.tick(now=started + 0.5)
    assert len(published_hellos(relay)) == 1
    # ... at 1s the hello is re-sent, at QoS 1 like the first one.
    service.tick(now=started + 1.1)
    assert len(published_hellos(relay)) == 2
    assert published_hellos(relay)[-1][2] == 1
    # The next attempt waits for the second backoff step, not the first.
    service.tick(now=started + 1.5)
    assert len(published_hellos(relay)) == 2
    service.tick(now=started + 3.2)
    assert len(published_hellos(relay)) == 3


def test_a_wait_past_its_deadline_fails_visibly_and_is_cleaned_up():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    tag = netpair_device_tag(B_ID)
    service.enter(generate_netpair_code(B_ID, secret))
    service._waiting_since[tag] = time.time() - WAIT_TIMEOUT - 1

    events = service.tick()

    assert [event["name"] for event in events] == ["netpair.waiting_failed"]
    payload = events[0]["payload"]
    assert payload["peer_id"] == tag and payload["reason"] == "timeout"
    assert config.netpair_secrets == {}
    assert service.status()["waiting"] == []
    assert netpair_topic(secret) not in service.channels()


def test_a_hello_that_cannot_be_published_rolls_the_wait_back():
    relay = FakeRelay(ok=False)
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    tag = netpair_device_tag(B_ID)

    with pytest.raises(ApplicationError) as denied:
        service.enter(generate_netpair_code(B_ID, secret))
    assert denied.value.code == "RELAY_OFFLINE"
    assert denied.value.retryable is True

    assert tag not in config.netpair_secrets
    assert service.status()["waiting"] == []
    assert netpair_topic(secret) not in service.channels()
    # The rolled-back secret is not left waiting for a hello to be retried.
    attempts = relay.attempts
    service.tick(now=time.time() + 10)
    assert relay.attempts == attempts


def test_confirming_a_hello_retries_until_a_frame_from_the_peer_arrives():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    config.netpair_secrets = {B_ID: secret}
    topic = netpair_topic(secret)
    started = time.time()

    assert service.confirm_hello(B_ID, secret) is True
    assert len(published_hellos(relay)) == 1
    service.tick(now=started + 1.1)
    assert len(published_hellos(relay)) == 2  # the lost reply is re-sent

    # The peer answers at all: the reply landed, stop retransmitting.
    service.note_relay_source(B_ID, topic, 0)
    before = len(published_hellos(relay))
    service.tick(now=started + 10)
    assert len(published_hellos(relay)) == before


def test_a_confirmation_that_could_not_be_published_is_retried_by_the_tick():
    relay = FakeRelay(ok=False)
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    config.netpair_secrets = {B_ID: secret}
    started = time.time()

    assert service.confirm_hello(B_ID, secret) is False
    relay.ok = True
    service.tick(now=started + 1.1)

    assert len(published_hellos(relay)) == 1
    assert published_hellos(relay)[0][2] == 1


def test_the_generator_keeps_accepting_code_key_frames_until_the_peer_moves_to_the_session_key():
    config, service, _saves = make_service()
    secret = generate_netpair_secret()
    pinned = generate_keypair()[1]
    config.netpair_secrets = {B_ID: secret}
    config.netpair_peer_keys = {B_ID: pinned}
    topic = netpair_topic(secret)

    assert service.accepts_code_key(B_ID, "clipboard") is False
    assert service.accepts_code_key(netpair_device_tag(A_ID), "clipboard") is True
    assert service.accepts_code_key(B_ID, "netpair_hello") is True

    # The generator accepted B's hello: the reply may be lost from here on, so
    # B's ordinary frames still ride the code key for a bounded grace window.
    result = service.handle_hello(
        B_ID, netpair_device_tag(config.device_id), "B", topic, dh_pub=pinned
    )
    assert result["accepted"] is True and result["role"] == "generator"
    assert service.accepts_code_key(B_ID, "clipboard") is True

    # A frame under the agreed key proves B has the session key: the old rule
    # applies again immediately.
    service.note_relay_source(B_ID, topic, 0)
    assert service.accepts_code_key(B_ID, "clipboard") is False


# ------------------------------- 中-1: pending self-heal and generate while offline


def test_a_frame_on_a_pending_code_channel_binds_it_to_its_sender():
    """The lost hello direction: the frame itself proves who entered the code."""
    config, service, _saves = make_service()
    result = service.generate()
    _tag, secret = decode_netpair_code(result["code"])

    assert service.note_relay_source(B_ID, netpair_topic(secret)) is True

    assert config.netpair_secrets == {B_ID: secret}
    assert config.netpair_pending == {}
    assert service.status()["generated_code"] is None
    # ...and the hello that follows completes it as the generator's half.
    hello = service.handle_hello(
        B_ID,
        netpair_device_tag(config.device_id),
        "B",
        netpair_topic(secret),
        dh_pub=generate_keypair()[1],
    )
    assert hello["accepted"] is True and hello["role"] == "generator"


def test_a_frame_on_another_channel_does_not_claim_a_pending_code():
    config, service, _saves = make_service()
    service.generate()

    assert service.note_relay_source(B_ID, netpair_topic(generate_netpair_secret())) is False
    assert len(config.netpair_pending) == 1


def test_generating_a_code_while_the_relay_is_down_reports_it_instead_of_failing():
    relay = FakeRelay(state="connecting")
    config, service, _saves = make_service(relay=relay)

    result = service.generate()

    assert result["online"] is False and result["relay"] == "connecting"
    assert result["retryable"] is True
    assert config.netpair_pending  # the code still exists, with a visible warning
    status = service.status()
    assert (status["can_pair"], status["relay_offline"]) == (False, True)


# ------------------------------------------------ 中-4: password-mismatch report


def test_undecryptable_frames_report_the_peer_after_a_threshold_and_throttle():
    config, service, _saves = make_service()
    secret = generate_netpair_secret()
    config.netpair_secrets = {B_ID: secret}
    topic = netpair_topic(secret)
    now = time.time()

    assert service.note_undecryptable(topic, now=now) is None
    assert service.note_undecryptable(topic, now=now) is None
    assert service.note_undecryptable(topic, now=now) == B_ID
    # Reported once per cooldown, not once per frame.
    for offset in range(1, 6):
        assert service.note_undecryptable(topic, now=now + offset) is None
    # A frame that decrypts proves the keys agree again; a later mismatch must
    # be able to report afresh, after the cooldown.
    service.note_relay_source(B_ID, topic, 0)
    later = now + DECRYPT_REPORT_COOLDOWN + 5
    assert service.note_undecryptable(topic, now=later) is None
    assert service.note_undecryptable(topic, now=later) is None
    assert service.note_undecryptable(topic, now=later) == B_ID


def test_undecryptable_frames_on_a_pending_channel_name_nobody():
    config, service, _saves = make_service()
    result = service.generate()
    _tag, secret = decode_netpair_code(result["code"])
    for _ in range(5):
        assert service.note_undecryptable(netpair_topic(secret)) is None


def test_changing_the_password_re_sends_a_hello_to_every_confirmed_peer():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    config.netpair_secrets = {B_ID: secret, netpair_device_tag(A_ID): "waiting"}
    started = time.time()

    assert service.rehello_peers(now=started) == [B_ID]
    hellos = published_hellos(relay)
    assert len(hellos) == 1
    assert hellos[0][0]._raw_payload["peer_id"] == B_ID
    assert hellos[0][2] == 1

    # It is retried too, because a peer that was offline must still learn.
    relay.ok = False
    config.netpair_secrets = {B_ID: secret}
    assert service.rehello_peers(now=started) == []
    relay.ok = True
    service.tick(now=started + 1.1)
    assert len(published_hellos(relay)) == 2


# ------------------------------------------------ 中-9: unpair reaches the partner


def test_unpairing_clears_the_learned_name_and_notifies_the_partner():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    secret = generate_netpair_secret()
    config.netpair_secrets = {B_ID: secret}
    config.netpair_names = {B_ID: "Desk"}
    config.netpair_peer_keys = {B_ID: generate_keypair()[1]}

    assert service.unpair(B_ID) == {"ok": True}

    assert config.netpair_secrets == {}
    assert config.netpair_aliases == {}
    assert config.netpair_names == {}
    assert config.netpair_peer_keys == {}
    frame, topic, _key, qos = relay.published[-1]
    assert topic == netpair_topic(secret)
    assert qos == 1
    assert decode_message(frame).msg_type == "netpair_unpair"
    assert service.status()["peers"] == []


def test_an_unpair_notice_from_a_partner_drops_the_pairing_here():
    config, service, _saves = make_service()
    secret = generate_netpair_secret()
    config.netpair_secrets = {B_ID: secret}
    config.netpair_names = {B_ID: "Desk"}

    info = service.handle_unpair_notice(B_ID)

    assert info == {"peer_id": B_ID, "name": "Desk"}
    assert config.netpair_secrets == {} and config.netpair_names == {}
    assert service.status()["peers"] == []
    assert service.handle_unpair_notice("unknown") is None


def test_cancelling_a_wait_is_not_an_unpair_notice():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    tag = netpair_device_tag(B_ID)
    service.enter(generate_netpair_code(B_ID, generate_netpair_secret()))
    published = len(relay.published)

    assert service.interrupt_waiting(tag) is True
    # A wait has no partner identity to tell; cancelling is local.
    assert len(relay.published) == published
    assert config.netpair_secrets == {}
    assert service.interrupt_waiting(tag) is False


# --------------------------------------- the two halves, end to end, with retries


def test_a_lost_confirmation_reply_heals_through_the_retries():
    """The review's 严重-5 reproduction, now asserted to end paired on both sides."""
    relay_a, relay_b = FakeRelay(), FakeRelay()
    a_cfg, a, _ = make_service(A_ID, "A", relay_a)
    b_cfg, b, _ = make_service(B_ID, "B", relay_b)
    started = time.time()

    code = a.generate()["code"]
    entered = b.enter(code)
    assert entered["waiting"] is True
    hello_msg, hello_topic, _qos = published_hellos(relay_b)[-1]
    b_public = hello_msg._raw_payload["dh_pub"]

    # A receives the hello and owes the confirmation; that publish is lost.
    hello = a.handle_hello(
        B_ID,
        hello_msg._raw_payload["peer_id"],
        "B",
        hello_topic,
        dh_pub=b_public,
    )
    assert hello["accepted"] is True and hello["reply"] is True
    assert a.confirm_hello(hello["peer_id"], hello["secret"]) is True
    # The reply is published but not delivered to B (the missing frame this
    # test is about); the value is read for its side effect on the fake relay.
    published_hellos(relay_a)

    # B does not hear it.  Its wait looks stuck for one backoff step ...
    assert b.status()["waiting"]
    b.tick(now=started + 0.5)
    # ... and then the retransmission goes out at QoS 1.
    b.tick(now=started + 1.2)
    retried = published_hellos(relay_b)[-1]
    assert retried[2] == 1
    assert retried[0]._raw_payload["peer_id"] == netpair_device_tag(A_ID)

    # A answers the retransmitted hello, and this time B hears it.
    again = a.handle_hello(
        B_ID,
        retried[0]._raw_payload["peer_id"],
        "B",
        retried[1],
        dh_pub=b_public,
    )
    assert again["accepted"] is True
    assert a.confirm_hello(again["peer_id"], again["secret"]) is True
    delivered = published_hellos(relay_a)[-1][0]
    reply_msg = delivered
    completed = b.handle_hello(
        A_ID,
        reply_msg._raw_payload["peer_id"],
        "A",
        retried[1],
        dh_pub=reply_msg._raw_payload["dh_pub"],
    )

    assert completed["accepted"] is True and completed["role"] == "enterer"
    assert b_cfg.netpair_secrets == {A_ID: again["secret"]}
    assert b.status()["waiting"] == []
    assert [peer["peer_id"] for peer in b.status()["peers"]] == [A_ID]
    assert [peer["peer_id"] for peer in a.status()["peers"]] == [B_ID]
    # Both channels now hold the agreed key and the code key, so ordinary
    # frames are decryptable in both directions.
    assert len(a.channels()[netpair_topic(again["secret"])]) == 2
    assert len(b.channels()[netpair_topic(again["secret"])]) == 2
    # The delivery state is no longer provisional either.
    assert a_cfg.netpair_pending == {}


def test_an_entered_code_that_is_never_answered_ends_with_a_visible_failure():
    relay = FakeRelay()
    config, service, _saves = make_service(relay=relay)
    service.enter(generate_netpair_code(B_ID, generate_netpair_secret()))
    tag = next(iter(config.netpair_secrets))
    assert is_provisional_key(tag)
    service._waiting_since[tag] = time.time() - WAIT_TIMEOUT - 1

    events = service.tick()

    assert events and events[0]["name"] == "netpair.waiting_failed"
    assert events[0]["payload"]["peer_id"] == tag
    assert service.status()["waiting"] == []


def test_a_code_keyed_frame_on_a_single_key_channel_is_not_the_session_key():
    """``key_index == 0`` on a code-key-only channel means "the only key".

    An enterer still waiting for the confirmation reply has exactly one key.
    Reading its frames as session-keyed would close ``accepts_code_key`` before
    the reply is even retried — the lost-reply loop this change exists to fix.
    """
    config, service, _saves = make_service()
    secret = generate_netpair_secret()
    tag = netpair_device_tag(B_ID)
    config.netpair_secrets = {tag: secret}  # entered, not yet confirmed
    topic = netpair_topic(secret)

    # What the runtime does for a frame from the generator on this channel.
    service.note_relay_source(B_ID, topic, 0)

    assert B_ID not in service._session_seen
    assert config.netpair_secrets == {B_ID: secret}  # tag re-keyed, wait ended


def test_undecryptable_frames_during_a_lost_reply_window_are_not_a_mismatch():
    """While a pairing settles, frames we cannot open are expected, not alarming."""
    config, service, _saves = make_service()
    secret = generate_netpair_secret()
    pinned = generate_keypair()[1]
    config.netpair_secrets = {B_ID: secret}
    config.netpair_peer_keys = {B_ID: pinned}
    topic = netpair_topic(secret)
    now = time.time()

    # The generator accepted B's hello: the reply is being retried, and B may
    # still be speaking under the code key.
    assert service.handle_hello(
        B_ID, netpair_device_tag(config.device_id), "B", topic, dh_pub=pinned
    )["accepted"] is True
    for _ in range(6):
        assert service.note_undecryptable(topic, now=now + 1) is None

    # Past the settling window, a run of them is the password mismatch it is.
    late = now + 302
    assert service.note_undecryptable(topic, now=late) is None
    assert service.note_undecryptable(topic, now=late) is None
    assert service.note_undecryptable(topic, now=late) == B_ID


def test_a_wait_resumed_after_a_restart_gets_the_same_retries_again():
    """A provisional secret on disk is a wait, and the new process re-drives it."""
    config, service, _saves = make_service(relay=FakeRelay())
    secret = generate_netpair_secret()
    tag = netpair_device_tag(B_ID)
    config.netpair_secrets = {tag: secret}
    assert service.status()["waiting"][0]["since"] is None

    events = service.tick()

    assert events == []
    assert service.status()["waiting"][0]["since"] is not None
    assert service.status()["waiting"][0]["deadline"] is not None
    assert tag in service._hellos  # hello retransmission resumed
