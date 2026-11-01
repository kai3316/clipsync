"""The live-handshake binding added to the informational SAS.

The pairing *code* stays certificate-derived so a reconnect does not change the
code two people are comparing.  The SAS is bound to the two negotiated nonces
when both builds advertise the version that promises it, which makes that code
specific to the connection it is shown for instead of to the certificate pair.
"""

from internal.security.fingerprint import sas_code
from internal.security.handshake import PAIRING_CONTEXT_VERSION, pairing_transcript


def test_a_peer_without_the_version_gets_the_certificate_only_derivation():
    exchange = {
        "peer_version": PAIRING_CONTEXT_VERSION - 1,
        "our_nonce": "aaaa",
        "peer_nonce": "bbbb",
    }
    assert pairing_transcript(exchange) == ""
    assert sas_code("AA:BB", "CC:DD") == sas_code("CC:DD", "AA:BB")


def test_both_new_builds_get_the_same_nonce_context_whatever_side_they_are():
    dialer = {
        "peer_version": PAIRING_CONTEXT_VERSION,
        "our_nonce": "aaaa",
        "peer_nonce": "bbbb",
    }
    acceptor = {
        "peer_version": PAIRING_CONTEXT_VERSION,
        "our_nonce": "bbbb",
        "peer_nonce": "aaaa",
    }
    context = pairing_transcript(dialer)
    assert context == pairing_transcript(acceptor)
    assert context == "aaaa|bbbb"
    assert sas_code("AA:BB", "CC:DD", context) == sas_code("CC:DD", "AA:BB", context)
    assert sas_code("AA:BB", "CC:DD", context) != sas_code("AA:BB", "CC:DD")


def test_a_missing_version_or_exchange_never_invents_a_context():
    assert pairing_transcript(None) == ""
    assert pairing_transcript({}) == ""
    assert pairing_transcript({"peer_version": "not-a-number"}) == ""
    assert pairing_transcript({"peer_version": PAIRING_CONTEXT_VERSION}) == "|"
