"""The user's per-device switch over what leaves this machine.

A copy used to go to every paired peer, over the LAN or the relay, whichever
could carry it.  That is right for the two machines this was written for and
wrong the moment a third is paired: a laptop, a work desktop and a machine at a
relative's house are all "paired", so a password copied on the laptop lands in the
history of all three -- and nothing tells the user it happened, because from the
sending side a clip that arrived looks like a clip that worked.

These tests pin the two properties that make the switch mean what a user thinks
it means: off stops *both* routes, and off survives the device being seen under
its hashed mDNS id.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from internal.transport.ids import peer_id_hash  # noqa: E402


class FakeConfig:
    def __init__(self, paused=()):
        # `None` is passed through on purpose: the field is a list by default,
        # but a config file written by hand (or by a future version that drops
        # the key) can carry a null, and `syncs_to` has to read that as "nothing
        # paused" rather than raising on every clip.
        self.sync_paused_peers = paused if paused is None else list(paused)
        self.peers = {}


class FakeRuntime:
    """The two collaborators `syncs_to` reads, and nothing else."""

    def __init__(self, paused=()):
        self.config = FakeConfig(paused)

    def _resolve(self, peer_id):
        # The real one maps a hashed mDNS id to the device's real id; the tests
        # that need that behaviour replace this.
        return peer_id


def syncs_to(paused, peer_id, resolve=None):
    from internal.infrastructure.runtime.lan import LanRuntime

    runtime = FakeRuntime(paused)
    if resolve is not None:
        runtime._resolve = resolve
    return LanRuntime.syncs_to(runtime, peer_id)


class TestTheDefaultIsToSync:
    """The choice between two silent failures, and why this is the one."""

    def test_a_device_not_listed_is_in_scope(self):
        assert syncs_to([], "peer-1") is True

    def test_an_empty_setting_means_every_device(self):
        """Every pairing made before this setting existed keeps working."""
        assert syncs_to(None, "peer-1") is True
        assert syncs_to([], "peer-1") is True

    def test_a_newly_paired_device_syncs_without_a_second_step(self):
        assert syncs_to(["peer-1"], "peer-just-paired") is True


class TestOffMeansOff:
    def test_a_paused_device_is_skipped(self):
        assert syncs_to(["peer-1"], "peer-1") is False

    def test_pausing_one_device_leaves_the_others_alone(self):
        assert syncs_to(["peer-1"], "peer-2") is True

    def test_the_switch_does_not_lose_itself_to_the_hashed_id(self):
        """The same device under its other name must stay off.

        Discovery knows a peer by a hash of its id until a session resolves it to
        the real one, and the caller may hold either.  Matching only the literal
        string would turn the switch back on the moment the id resolved -- the
        user would see the toggle still off while their clips went out.
        """
        real = "a1b2c3d4e5f6"
        hashed = peer_id_hash(real)
        # Paused by real id, asked about by hash.
        assert syncs_to([real], hashed, resolve=lambda _pid: real) is False
        # Paused by hash, asked about by real id.
        assert syncs_to([hashed], real, resolve=lambda _pid: real) is False

    def test_another_device_is_not_caught_by_that_widening(self):
        real = "a1b2c3d4e5f6"
        other = "ffffffffffff"
        assert syncs_to([real], other, resolve=lambda _pid: other) is True


class TestTheFilterIsWhereTheFrameActuallyLeaves:
    """A switch that only filtered one route would be worse than none.

    Checking the predicate is not enough: `_send_local_sync` and `_publish_relay`
    are two separate exits, and a user who turns a device off expects it to stop
    receiving, not to stop receiving *over the local link*.
    """

    def test_both_send_paths_consult_the_switch(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "internal"
            / "infrastructure"
            / "runtime"
            / "lan.py"
        ).read_text(encoding="utf-8")
        lan_sync = source.index("def _send_local_sync")
        relay = source.index("def _publish_relay")
        body_of_local = source[lan_sync:relay]
        assert "self.syncs_to(pid)" in body_of_local, (
            "_send_local_sync no longer consults the per-device switch"
        )
        body_of_relay = source[relay : source.index("def _relay_publish", relay)]
        assert body_of_relay.count("self.syncs_to(peer_id)") >= 1, (
            "_publish_relay does not consult the per-device switch, so a device "
            "turned off still receives over the relay"
        )

    def test_the_relay_filters_every_enrolment_route(self):
        """Both relay loops -- enrolled secret and pairing-code -- must filter.

        They reach the same peer by different keys; leaving one unfiltered means
        the switch works for a device paired one way and not the other, which is
        exactly the kind of half-truth a user cannot diagnose.
        """
        source = (
            Path(__file__).resolve().parents[2]
            / "internal"
            / "infrastructure"
            / "runtime"
            / "lan.py"
        ).read_text(encoding="utf-8")
        relay = source.index("def _publish_relay")
        body = source[relay : source.index("def _relay_publish", relay)]
        assert body.count("self.syncs_to(peer_id)") == 2, (
            f"expected the switch on both relay routes, found "
            f"{body.count('self.syncs_to(peer_id)')}"
        )
