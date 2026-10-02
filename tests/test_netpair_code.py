"""The internet-pairing code: the 12-character thing a user reads aloud.

This module exists because the safety properties of that code lost their tests.
They lived in `tests/test_internet_pairing.py`, which was built on the deleted Tk
application and went with it; the end-to-end coverage that replaced it
(`tests/sidecar/test_lan_runtime.py`) enters a *valid* code and asserts the
pairing that follows, which exercises none of the rejection paths below.

What is at stake is small but sharp. The code carries a 35-bit secret — guessable
by brute force on its own, which is why the channel key is re-derived from an
X25519 exchange and the code only routes (see `netpair_session_key`). What the
checksum buys is different: a mistyped code must fail *closed*, because the user
who typed it is one keystroke away from pairing their machine to a stranger's
session.
"""

import pytest

from internal.transport import relay as R  # noqa: N812


def a_code(device_id="a1b2c3d4e5f6", secret=None):
    if secret is None:
        secret = R.generate_netpair_secret()
    return R.generate_netpair_code(device_id, secret), secret


class TestTheCodeRoundTrips:
    def test_a_generated_code_decodes_to_its_tag_and_secret(self):
        code, secret = a_code()
        tag, decoded = R.decode_netpair_code(code)
        assert decoded == secret
        assert tag == R.netpair_device_tag("a1b2c3d4e5f6")

    def test_the_device_tag_is_the_same_tag_the_code_carried(self):
        """The tag is what confirms the partner's hello frame names us."""
        code, _ = a_code(device_id="0123456789ab")
        tag, _ = R.decode_netpair_code(code)
        assert tag == R.netpair_device_tag("0123456789ab")

    def test_the_tag_is_stable_across_case_and_padding_of_the_device_id(self):
        upper = R.netpair_device_tag("A1B2C3D4E5F6")
        lower = R.netpair_device_tag("a1b2c3d4e5f6")
        padded = R.netpair_device_tag("  a1b2c3d4e5f6  ")
        assert upper == lower == padded

    def test_two_devices_do_not_share_a_tag(self):
        """Lossy, but not so lossy that two machines collide in one session."""
        tags = {R.netpair_device_tag(f"{i:012x}") for i in range(500)}
        # 20 bits of tag: a few collisions in 500 draws are expected, a flood is
        # not.  Measured, this is 0-1.
        assert len(tags) > 490


class TestATypoFailsClosed:
    """Every case here is a user mistyping, and every one must return None."""

    def test_a_wrong_checksum_is_rejected(self):
        code, _ = a_code()
        flat = code.replace("-", "")
        # Change the last character, which is the checksum, to a different one
        # from the same alphabet.
        other = next(c for c in R.NETPAIR_ALPHABET if c != flat[11])
        assert R.decode_netpair_code(flat[:11] + other) is None

    def test_a_flipped_data_character_is_caught_by_the_checksum(self):
        """The case the checksum exists for: a typo in the payload, not the sum."""
        code, _ = a_code()
        flat = code.replace("-", "")
        other = next(c for c in R.NETPAIR_ALPHABET if c != flat[3])
        assert R.decode_netpair_code(flat[:3] + other + flat[4:]) is None

    @pytest.mark.parametrize("bad", ["", "ABC", "A" * 11, "A" * 13, "ABCD-EFGH-IJKL-M"])
    def test_a_wrong_length_is_rejected(self, bad):
        assert R.decode_netpair_code(bad) is None

    def test_a_character_outside_the_alphabet_is_rejected(self):
        """The alphabet is missing the characters people misread.

        `0`, `1`, `O`, `I` and `L` are all absent (measured: the alphabet is
        `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`), which is what makes a code read off
        a screen transcribable by hand.  Substituting one for a real character
        must therefore be refused outright rather than decoded into a *different*
        valid-looking code.
        """
        code, _ = a_code()
        flat = code.replace("-", "")
        excluded = [c for c in "01OIL" if c not in R.NETPAIR_ALPHABET]
        assert excluded, (
            "every character this test substitutes is in the alphabet, so the "
            "rejection it asserts is not being tested"
        )
        for bad in excluded:
            assert R.decode_netpair_code(flat[:3] + bad + flat[4:]) is None

    def test_a_non_string_is_rejected_rather_than_raising(self):
        for bad in (None, 12345, b"ABCD-EFGH-IJKL", ["A"], object()):
            assert R.decode_netpair_code(bad) is None

    def test_a_valid_code_is_not_rejected(self):
        """The guard on the guard: the rejections above are not vacuous."""
        code, secret = a_code()
        assert R.decode_netpair_code(code) == (R.netpair_device_tag("a1b2c3d4e5f6"), secret)


class TestTheFormatTheUserTypes:
    def test_the_code_is_three_groups_of_four(self):
        code, _ = a_code()
        assert len(code) == 14
        groups = code.split("-")
        assert [len(g) for g in groups] == [4, 4, 4]

    def test_the_code_uses_only_the_readable_alphabet(self):
        secret = R.generate_netpair_secret()
        code = R.generate_netpair_code("a1b2c3d4e5f6", secret)
        assert set(code.replace("-", "")) <= set(R.NETPAIR_ALPHABET)

    def test_case_and_separators_do_not_matter_on_entry(self):
        """A user reading the code off a screen may type it either way."""
        code, secret = a_code()
        assert R.decode_netpair_code(code.lower()) == (
            R.netpair_device_tag("a1b2c3d4e5f6"),
            secret,
        )
        assert R.decode_netpair_code(code.replace("-", " ")) == (
            R.netpair_device_tag("a1b2c3d4e5f6"),
            secret,
        )
        assert R.decode_netpair_code(f" {code} ") == (
            R.netpair_device_tag("a1b2c3d4e5f6"),
            secret,
        )

    def test_a_bad_character_class_is_refused_at_generation(self):
        """The producer validates too: a malformed secret must not be encodable."""
        for bad in ("", "TOO-SHORT", "A" * 8, "abc0123", "ABC012!"):
            with pytest.raises(ValueError):
                R.generate_netpair_code("a1b2c3d4e5f6", bad)


class TestTheSecretIsNotTheKey:
    """Why a cracked code is not a cracked conversation."""

    def test_the_topic_is_derived_and_not_the_secret(self):
        secret = R.generate_netpair_secret()
        topic = R.netpair_topic(secret)
        assert secret not in topic
        assert topic.startswith(R.NETPAIR_TOPIC_PREFIX)

    def test_two_secrets_do_not_share_a_topic(self):
        topics = {R.netpair_topic(R.generate_netpair_secret()) for _ in range(200)}
        assert len(topics) == 200

    def test_the_netpair_topic_cannot_collide_with_a_relay_topic(self):
        """Two different channels of the same broker, and they must not meet.

        The netpair topic routes a pairing handshake; `derive_topic` routes the
        relay channel between already-paired devices.  They are separate prefixes
        on purpose, and a device that mixed them would hand its pairing frames to
        the relay path.
        """
        secret = R.generate_netpair_secret()
        assert R.netpair_topic(secret).startswith(R.NETPAIR_TOPIC_PREFIX)
        assert not R.netpair_topic(secret).startswith(R.TOPIC_PREFIX)
        assert not R.derive_topic(secret, secret).startswith(R.NETPAIR_TOPIC_PREFIX)

    def test_an_empty_password_is_byte_identical_to_the_secret_only_key(self):
        """Existing pairings must keep working across the passphrase feature.

        This is the property that made the passphrase addition backward
        compatible, and nothing else in the suite asserts it.
        """
        secret = R.generate_netpair_secret()
        assert R.netpair_key(secret, "") == R.netpair_key(secret)

    def test_a_password_changes_the_key(self):
        secret = R.generate_netpair_secret()
        assert R.netpair_key(secret, "") != R.netpair_key(secret, "Str0ng!Passw")

    def test_the_key_is_32_bytes_for_any_input(self):
        secret = R.generate_netpair_secret()
        assert len(R.netpair_key(secret)) == 32
        assert len(R.netpair_key(secret, "Str0ng!Passw")) == 32

    def test_the_session_key_depends_on_the_exchange_not_only_the_code(self):
        """The point of the DH step: a recorded channel survives a cracked code.

        Same secret and password, different `shared` -- a recording made with one
        exchange must not decrypt under another.
        """
        secret = R.generate_netpair_secret()
        first = R.netpair_session_key(secret, b"\x01" * 32, "")
        second = R.netpair_session_key(secret, b"\x02" * 32, "")
        assert first != second

    def test_the_session_key_is_not_the_static_key(self):
        secret = R.generate_netpair_secret()
        assert R.netpair_session_key(secret, b"\x01" * 32, "") != R.netpair_key(secret, "")


class TestThePassphraseRules:
    # `NETPAIR_PASSPHRASE_MIN` is 12, so every sample below is long enough
    # unless the case is specifically about length.
    def test_a_strong_passphrase_is_accepted(self):
        assert R.netpair_passphrase_error("Str0ng!Passw") is None

    @pytest.mark.parametrize(
        "bad,tag",
        [
            ("Ab1!x", "length"),
            ("A" * 199 + "a1!", "length"),
            ("weakpass1!ab", "upper"),
            ("WEAKPASS1!AB", "lower"),
            ("NoDigits!!!a", "digit"),
            ("NoSpecial123", "special"),
        ],
    )
    def test_each_missing_rule_names_itself(self, bad, tag):
        assert R.netpair_passphrase_error(bad) == tag

    def test_a_non_string_is_a_type_error_not_a_crash(self):
        for bad in (None, 1234, ["x"]):
            assert R.netpair_passphrase_error(bad) == "type"

    def test_a_space_is_not_a_special_character(self):
        """A trailing space is a typo, not strength.

        Long enough to clear the length rule, so only the special-character rule
        can produce the answer.
        """
        assert R.netpair_passphrase_error("Str0ng Passw") == "special"
