# Pairing security review: SAS context binding and PAKE

Status: review completed before the 1.3.2 build; one additive change implemented,
PAKE deferred. This note records the threat model and the compatibility
constraints so a later change starts from the same facts.

## What the current pairing does

- The LAN link is TLS with ephemeral key exchange. The certificate is pinned
  after pairing, and a certificate change raises an explicit alert instead of
  being accepted silently.
- Each identity frame carries an Ed25519 proof over both handshake nonces, so a
  recorded identity frame cannot be replayed as a fresh handshake.
- The confirmation flow shows a shared 8-digit code derived from the sorted
  certificate fingerprints, plus a short SAS. The code has a five-minute
  lifetime, a bounded number of attempts, and a per-peer rate limit.
- A first pairing is a numeric-comparison decision: two different certificates
  seen by the two people produce two different codes, which is exactly the
  check that exposes a MITM presenting its own certificate to each side.

## Gaps worth calling by name

1. **The code is deterministic for a certificate pair.** The same two devices
   show the same code on every pairing. A screenshot or a recording of an old
   prompt is therefore a recording of a future prompt, and the code says
   nothing about *which handshake* the user is confirming.
2. **There is no PAKE.** The code is compared, not used as a low-entropy
   shared secret in a password-authenticated key exchange. A PAKE would make
   the transcript itself resistant to an active attacker who can relay between
   two honest devices; numeric comparison already detects that attacker, but
   only if the people actually compare.
3. **Relay-mediated pairing** (`netpair`) uses a 35-bit code and a code-derived
   key for the handshake. It upgrades to a key agreement after exchange, which
   is why the pairing code there is visibly much shorter-lived than the LAN
   flow. This review is about the LAN numeric comparison.

## Decision

- **Implemented:** for peers that advertise handshake version 2, the
  informational SAS is derived from the two certificate fingerprints **and the
  two live handshake nonces**, sorted so both ends get the same value. The
  nonces are not reused outside the connection and are chosen immediately
  before the identity frame is sent.
- **Not implemented:** binding the 8-digit confirmation code to the nonces.
  Both sides would have to do it, and a reconnect during a pending prompt would
  change the code while the two people are reading it. That is a UX and
  protocol design change, not a hardening patch.
- **Not implemented:** SPAKE2 or another PAKE. It needs a second pairing
  protocol and a migration story for existing pairings; the value over numeric
  comparison is real but does not justify a rushed change in a patch release.
- **Not implemented:** changing the code's entropy or length. Eight digits with
  a five-minute window and limited attempts is the existing contract, and a
  longer code makes the comparison worse for the person doing it.

## Compatibility

The handshake version is negotiated, not assumed. A 1.3.2 build and an older
build each read the other's claimed version; when either side is older than 2,
both keep the certificate-only SAS and still pair. New-to-new gets the bound
SAS. The confirmation code is unchanged, so a pending prompt survives a
reconnect and an existing pairing is not invalidated.

## Follow-up, if PAKE is pursued

1. Add a versioned pairing mode with explicit capability negotiation.
2. Keep the numeric comparison as a fallback for one release.
3. Test impersonation, relay, replay and downgrade cases before shipping.
4. Do not reuse the netpair code-derived key path for LAN pairing without a new
   protocol identifier; the two have different entropy assumptions.
