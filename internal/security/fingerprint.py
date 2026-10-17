"""Short Authentication String (SAS) for out-of-band pairing verification.

A 6-digit pairing code alone is brute-forceable by an on-path attacker during
a public-network pairing, so both devices additionally derive — from their TLS
certificate fingerprints — one identical short code ("3A2F-91C4").  The user
compares it visually across devices before confirming: a MITM presenting its
own certificate produces a different SAS on each side, which the comparison
exposes.

Derivation is deliberately symmetric (the two fingerprints are sorted before
hashing) so either device can compute the same string from local knowledge
alone — nothing about the SAS travels over the wire.
"""

import hashlib

SAS_LENGTH = 8  # hex characters before grouping


def normalize_fingerprint(fingerprint: str) -> str:
    """Canonical form of a certificate fingerprint for hashing.

    Accepts the colon-separated display form produced by
    ``pairing.fingerprint_pem`` as well as bare hex; case-insensitive.
    """
    return str(fingerprint or "").replace(":", "").replace(" ", "").strip().upper()


def sas_code(fp_a: str, fp_b: str) -> str:
    """Deterministic, symmetric SAS from two certificate fingerprints.

    Both inputs are normalized, sorted, concatenated and SHA-256 hashed; the
    first 8 hex characters are uppercased and grouped 4-4 ("3A2F-91C4").

    Determinism: same inputs -> same output.  Symmetry:
    ``sas_code(a, b) == sas_code(b, a)``.  Distinct device pairs yield
    distinct codes with overwhelming probability.
    """
    a, b = sorted([normalize_fingerprint(fp_a), normalize_fingerprint(fp_b)])
    digest = hashlib.sha256((a + b).encode("ascii")).hexdigest()
    short = digest[:SAS_LENGTH].upper()
    return f"{short[:4]}-{short[4:]}"
