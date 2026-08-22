"""AES-256-GCM encryption utilities for at-rest and app-layer encryption.

Key hierarchy:
  storage_key = HKDF(device_fingerprint, salt="storage", info="clipsync-at-rest")
  frame_key   = HKDF(sorted(fp_A, fp_B), salt="frame", info="clipsync-payload")

When a pre-shared password is set, PBKDF2(password, fingerprint) is used as
additional input material to HKDF for both keys.
"""

import hashlib
import hmac
import logging
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_NONCE_LEN = 12  # bytes
_TAG_LEN = 16    # AES-GCM tag is 16 bytes
_AES_KEY_LEN = 32
_PBKDF2_ITERATIONS = 600_000
_PW_VERIFY_ITERATIONS = 100_000  # for password verification token
_PW_VERIFY_LEN = 32  # hex chars

# Sentinels for distinguishing encrypted data from plaintext (legacy)
_ENCRYPTED_PREFIX = b"\x01CBE"  # 4-byte marker: version 1 + "CBE" for ClipSync Encrypted


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte AES-256 key from a password using PBKDF2-SHA256.

    Returns a deterministic key for the given (password, salt) pair.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_AES_KEY_LEN,
    )


def make_password_hash(password: str, fingerprint: str) -> str:
    """Derive a one-way verification token from (password, fingerprint).

    The password itself is never stored. This hash lets us verify the
    entered password on startup without keeping the plaintext secret.
    """
    salt = fingerprint.encode("ascii")[:16]
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt,
        _PW_VERIFY_ITERATIONS, dklen=32,
    ).hex()[:_PW_VERIFY_LEN]


def verify_password(password: str, fingerprint: str, stored_hash: str) -> bool:
    """Check an entered password against a stored verification token."""
    return make_password_hash(password, fingerprint) == stored_hash


def _hkdf_expand(ikm: bytes, info: bytes, length: int = _AES_KEY_LEN) -> bytes:
    """HKDF-Expand (RFC 5869) using HMAC-SHA256."""
    t = b""
    okm = b""
    i = 1
    while len(okm) < length:
        t = hmac.new(ikm, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869) using HMAC-SHA256."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _compute_storage_key(device_fingerprint: str, password: str = "") -> bytes:
    """Derive the at-rest storage key from device fingerprint + optional password.

    NOTE: when no password is set, the key is derived solely from the public
    device fingerprint, which is stored in plaintext next to the data — so
    this is obfuscation, not encryption. With a password, PBKDF2(password,
    fingerprint) is folded in, giving real at-rest encryption. Key derivation
    is intentionally unchanged here so existing data stays readable. Never
    log the password or include it in exceptions raised from this function.
    """
    ikm = device_fingerprint.encode("ascii")
    salt = b"clipsync-at-rest-salt"
    if password:
        pw_key = derive_key(password, device_fingerprint.encode("ascii"))
        ikm = bytes(a ^ b for a, b in zip(ikm.ljust(32, b"\x00"), pw_key))
    prk = _hkdf_extract(salt, ikm)
    return _hkdf_expand(prk, b"clipsync-storage-key", _AES_KEY_LEN)


def _compute_frame_key(my_fingerprint: str, peer_fingerprint: str, password: str = "") -> bytes:
    """Derive the per-peer frame encryption key.

    Both peers compute the same key by sorting fingerprints before hashing.
    """
    fps = sorted([my_fingerprint, peer_fingerprint])
    ikm = (fps[0] + fps[1]).encode("ascii")
    salt = b"clipsync-frame-salt"
    if password:
        pw_key = derive_key(password, fps[0].encode("ascii"))
        ikm = bytes(a ^ b for a, b in zip(ikm.ljust(32, b"\x00"), pw_key))
    prk = _hkdf_extract(salt, ikm)
    return _hkdf_expand(prk, b"clipsync-frame-key", _AES_KEY_LEN)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with AES-256-GCM.

    Returns: _ENCRYPTED_PREFIX || nonce (12) || ciphertext || tag (16)
    """
    nonce = secrets.token_bytes(_NONCE_LEN)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    result = _ENCRYPTED_PREFIX + nonce + ct
    logger.debug("Encrypted %d bytes → %d bytes", len(plaintext), len(result))
    return result


def decrypt(data: bytes, key: bytes) -> bytes | None:
    """Decrypt data produced by encrypt(). Returns None on auth failure or if
    the data doesn't have the encrypted prefix (plaintext legacy data)."""
    if not data.startswith(_ENCRYPTED_PREFIX):
        return None  # Not encrypted — caller should treat as plaintext
    try:
        body = data[len(_ENCRYPTED_PREFIX):]
        nonce = body[:_NONCE_LEN]
        ct = body[_NONCE_LEN:]
        aesgcm = AESGCM(key)
        pt = aesgcm.decrypt(nonce, ct, None)
        logger.debug("Decrypted %d bytes → %d bytes", len(data), len(pt))
        return pt
    except Exception as exc:
        logger.debug("Decryption auth failure: %s", exc)
        return None


def is_encrypted(data: bytes) -> bool:
    """Check if data has the encrypted prefix marker."""
    return data.startswith(_ENCRYPTED_PREFIX)


class EncryptionManager:
    """Manages encryption keys and operations for a device instance.

    Thin wrapper that caches derived keys. All crypto is stateless — keys
    are re-derived on each access (PBKDF2 is cached for the session via
    the fingerprint being stable).
    """

    def __init__(self, device_fingerprint: str, password: str = ""):
        self._fingerprint = device_fingerprint
        self._password = password
        self._storage_key_cache: bytes | None = None
        self._frame_key_cache: dict[str, bytes] = {}  # peer_fingerprint -> key
        logger.info(
            "EncryptionManager initialized (fingerprint=%s, password=%s)",
            device_fingerprint[:16] + "..." if device_fingerprint else "(none)",
            "set" if password else "not set",
        )
        if not password:
            logger.warning(
                "At-rest encryption WITHOUT a password is obfuscation only: "
                "the storage key is derived from the public device fingerprint, "
                "which is stored in plaintext. Set a password for real encryption."
            )

    @property
    def storage_key(self) -> bytes:
        if self._storage_key_cache is None:
            self._storage_key_cache = _compute_storage_key(
                self._fingerprint, self._password,
            )
        return self._storage_key_cache

    def get_frame_key(self, peer_fingerprint: str) -> bytes:
        if peer_fingerprint not in self._frame_key_cache:
            self._frame_key_cache[peer_fingerprint] = _compute_frame_key(
                self._fingerprint, peer_fingerprint, self._password,
            )
            logger.debug(
                "Derived frame key for peer %s (cache size=%d)",
                peer_fingerprint[:16] + "...", len(self._frame_key_cache),
            )
        return self._frame_key_cache[peer_fingerprint]

    def encrypt_storage(self, plaintext: str) -> str:
        """Encrypt a string for at-rest storage. Returns base64 of encrypted bytes."""
        ct = encrypt(plaintext.encode("utf-8"), self.storage_key)
        import base64
        return base64.b64encode(ct).decode("ascii")

    def decrypt_storage(self, ciphertext_b64: str) -> str | None:
        """Decrypt a base64-encoded encrypted string.

        Returns the plaintext on success, or the original string if it looks
        like legacy plaintext (no encryption prefix), or None on auth failure.
        """
        import base64
        try:
            data = base64.b64decode(ciphertext_b64)
        except Exception:
            # Not valid base64 — likely legacy plaintext
            return ciphertext_b64
        pt = decrypt(data, self.storage_key)
        if pt is not None:
            return pt.decode("utf-8")
        # Not encrypted — legacy plaintext, return original string unchanged
        if not is_encrypted(data):
            return ciphertext_b64
        return None  # auth failure (bad tag or corrupt data)

    def encrypt_frame(self, plaintext: bytes, peer_fingerprint: str) -> bytes:
        """Encrypt frame payload bytes for app-layer encryption."""
        result = encrypt(plaintext, self.get_frame_key(peer_fingerprint))
        logger.debug(
            "App-layer encrypt: %d bytes plain → %d bytes encrypted (peer=%s)",
            len(plaintext), len(result), peer_fingerprint[:12] + "...",
        )
        return result

    def decrypt_frame(self, data: bytes, peer_fingerprint: str) -> bytes | None:
        """Decrypt frame payload bytes. Returns None on auth failure."""
        result = decrypt(data, self.get_frame_key(peer_fingerprint))
        if result is not None:
            logger.debug(
                "App-layer decrypt: %d bytes → %d bytes (peer=%s)",
                len(data), len(result), peer_fingerprint[:12] + "...",
            )
        else:
            logger.warning(
                "App-layer decrypt failed (auth/format) for peer %s",
                peer_fingerprint[:12] + "...",
            )
        return result
