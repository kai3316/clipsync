"""``InternetPairingService``: a new pairing code cannot re-bind a confirmed device.

The hello is the one frame on a netpair channel protected by the code-derived
key, because it is what carries the DH public halves the agreed key is made
from.  That key is 35 bits of code secret: whoever holds the code (and, for a
code that leaks, anyone) can mint a hello on its topic.

A hello that names an already-confirmed device and moves that device's secret
slot to the new code is the dangerous shape.  The confirmed peer is still
announcing on the topic its own secret derives, so the victim stops listening
there and the new code's holder sits on the confirmed id; the peer's pinned DH
key alone does not stop it, because repeating that pin satisfies the "only
repeat the pinned key" check while the secret underneath moves.  Re-pairing an
existing device therefore has to go through removal first (``unpair``), which
is what drops the old secret and its pin.
"""

from __future__ import annotations

from internal.config.config import Config
from internal.infrastructure.runtime.internet_pairing import InternetPairingService
from internal.security.keyexchange import generate_keypair
from internal.transport.relay import (
    decode_netpair_code,
    netpair_device_tag,
    netpair_topic,
)

IGNORED = {
    "accepted": False,
    "role": "",
    "peer_id": "",
    "secret": "",
    "reply": False,
}


def a_service(**overrides):
    """A real service over an isolated config; saves are counted, not written."""
    config = Config(device_id="local", device_name="Local", encryption_enabled=False)
    # Set, so nothing reaches the disk through ``ensure_netpair_dh_key``.
    config.netpair_dh_key = generate_keypair()[0]
    for name, value in overrides.items():
        setattr(config, name, value)
    saves: list[None] = []
    return config, InternetPairingService(config, lambda: saves.append(None)), saves


def test_a_new_code_cannot_rebind_a_confirmed_device_even_with_its_old_pin():
    """The leaked-new-code attack: a confirmed id and its own pinned key.

    The attacker holds the code this machine just generated, so the generator
    half of ``handle_hello`` accepts it, and knows the confirmed device's
    static DH public key, so the pin check sees no change.  What must not happen
    is ``netpair_secrets['remote']`` moving to the new secret: that is the real
    peer's channel being taken away and handed to the code's holder.
    """
    old_secret = "K7Q2M9Z"
    pinned = generate_keypair()[1]
    config, service, saves = a_service(
        internet_sync_enabled=True,
        netpair_secrets={"remote": old_secret},
        netpair_peer_keys={"remote": pinned},
    )
    code = service.generate()["code"]
    _, new_secret = decode_netpair_code(code)
    old_channel = service.netpair_keys_for_topic(netpair_topic(old_secret))

    result = service.handle_hello(
        "remote",
        netpair_device_tag(config.device_id),
        "Remote",
        netpair_topic(new_secret),
        dh_pub=pinned,
    )

    assert result == IGNORED
    assert config.netpair_secrets == {"remote": old_secret}
    assert config.netpair_peer_keys == {"remote": pinned}
    assert service.netpair_keys_for_topic(netpair_topic(old_secret)) == old_channel
    # The code is still offered to its intended partner, and nothing was saved.
    assert service.status()["generated_code"] == code
    assert saves == []


def test_a_new_code_cannot_rebind_a_confirmed_device_while_entering():
    """The same move on the side that typed a code.

    An entered code stores its secret under the 4-char tag until the peer's
    hello re-keys it to a real id.  A hello on that provisional topic claiming
    an already-confirmed device must not move that device's slot to the newly
    entered secret either.
    """
    old_secret = "K7Q2M9Z"
    new_secret = "ABCDEFG"
    pinned = generate_keypair()[1]
    tag = netpair_device_tag("generator-device")
    config, service, saves = a_service(
        netpair_secrets={"remote": old_secret, tag: new_secret},
        netpair_peer_keys={"remote": pinned},
    )

    result = service.handle_hello(
        "remote", config.device_id, "Remote", netpair_topic(new_secret), dh_pub=pinned
    )

    assert result == IGNORED
    assert config.netpair_secrets == {"remote": old_secret, tag: new_secret}
    assert config.netpair_peer_keys == {"remote": pinned}
    assert saves == []


def test_a_new_code_still_pairs_a_device_that_was_never_confirmed():
    """First-time pairing is the flow the guard must leave alone."""
    config, service, saves = a_service(internet_sync_enabled=True)
    code = service.generate()["code"]
    _, secret = decode_netpair_code(code)
    public = generate_keypair()[1]

    result = service.handle_hello(
        "newdevice9999",
        netpair_device_tag(config.device_id),
        "New",
        netpair_topic(secret),
        dh_pub=public,
    )

    assert result == {
        "accepted": True,
        "role": "generator",
        "peer_id": "newdevice9999",
        "secret": secret,
        "reply": True,
    }
    assert config.netpair_secrets == {"newdevice9999": secret}
    assert config.netpair_peer_keys == {"newdevice9999": public}
    assert service.status()["generated_code"] is None
    assert saves == [None]


def test_a_new_code_still_pairs_a_legacy_peer_without_a_dh_pub():
    """Peers that predate the key agreement send no usable ``dh_pub``."""
    config, service, saves = a_service(internet_sync_enabled=True)
    code = service.generate()["code"]
    _, secret = decode_netpair_code(code)

    result = service.handle_hello(
        "newdevice9999",
        netpair_device_tag(config.device_id),
        "New",
        netpair_topic(secret),
        dh_pub="",
    )

    assert result["accepted"] is True and result["role"] == "generator"
    assert config.netpair_secrets == {"newdevice9999": secret}
    assert config.netpair_peer_keys == {}
    assert saves == [None]


def test_removing_the_device_first_is_the_supported_way_to_pair_it_again():
    """``unpair`` drops the old secret and pin, so the new code may bind the id."""
    old_secret = "K7Q2M9Z"
    config, service, saves = a_service(
        internet_sync_enabled=True,
        netpair_secrets={"remote": old_secret},
        netpair_peer_keys={"remote": generate_keypair()[1]},
    )
    service.unpair("remote")
    code = service.generate()["code"]
    _, new_secret = decode_netpair_code(code)
    fresh = generate_keypair()[1]

    result = service.handle_hello(
        "remote",
        netpair_device_tag(config.device_id),
        "Remote",
        netpair_topic(new_secret),
        dh_pub=fresh,
    )

    assert result["accepted"] is True and result["role"] == "generator"
    assert config.netpair_secrets == {"remote": new_secret}
    assert config.netpair_peer_keys == {"remote": fresh}
    assert service.status()["generated_code"] is None


def test_a_hello_repeating_a_confirmed_channel_is_still_idempotent():
    """The guard only refuses a *different* secret; a re-delivered hello works."""
    secret = "K7Q2M9Z"
    pinned = generate_keypair()[1]
    config, service, saves = a_service(
        netpair_secrets={"remote": secret}, netpair_peer_keys={"remote": pinned}
    )
    before = service.netpair_keys_for_topic(netpair_topic(secret))

    result = service.handle_hello(
        "remote", config.device_id, "Remote", netpair_topic(secret), dh_pub=pinned
    )

    assert result["accepted"] is True and result["role"] == "enterer"
    assert config.netpair_secrets == {"remote": secret}
    assert config.netpair_peer_keys == {"remote": pinned}
    assert service.netpair_keys_for_topic(netpair_topic(secret)) == before
