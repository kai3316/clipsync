"""Prepare a device identity without GUI prompts or persistence side effects."""

from copy import deepcopy
from dataclasses import dataclass

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from internal.config.config import Config
from internal.security.encryption import EncryptionManager, make_password_hash
from internal.security.pairing import PairingManager, fingerprint_pem


@dataclass
class IdentitySession:
    config: Config
    pairing: PairingManager
    encryption: EncryptionManager | None
    needs_save: bool


class IdentityInvalidError(ValueError):
    """Stored identity cannot be used without an explicit recovery action."""


def prepare_identity(config: Config, password: str = "") -> IdentitySession:
    """Validate first, then return a private candidate for the composition root."""
    cfg = deepcopy(config)
    has_key = bool(cfg.private_key_pem)
    has_certificate = bool(cfg.certificate_pem)
    if has_key != has_certificate:
        raise IdentityInvalidError("Device identity is incomplete; recovery is required")
    try:
        fingerprint = fingerprint_pem(cfg.certificate_pem) if has_certificate else ""
        if cfg.encryption_enabled and has_key:
            encryption = EncryptionManager(fingerprint, password=password)
            plaintext = encryption.decrypt_storage(cfg.private_key_pem)
            if plaintext is None:
                raise ValueError("Identity authentication failed")
            cfg.private_key_pem = plaintext
        pairing = PairingManager(cfg.device_id, cfg.device_name)
        identity = pairing.load_or_create_identity(cfg.private_key_pem, cfg.certificate_pem)
        names = identity.certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(names) != 1 or names[0].value != cfg.device_id:
            raise ValueError("Identity certificate does not match device ID")
        if not isinstance(identity.private_key, Ed25519PrivateKey):
            raise ValueError("Unsupported identity key")
        key_public = identity.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        cert_public = identity.certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if key_public != cert_public:
            raise ValueError("Identity key does not match certificate")
        for peer in cfg.peers.values():
            pairing.add_peer(
                peer.device_id, peer.device_name, peer.public_key_pem, peer.paired
            )
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise IdentityInvalidError("Device identity requires recovery") from exc
    cfg.private_key_pem = identity.private_key_pem
    cfg.certificate_pem = identity.certificate_pem
    cfg.encryption_password = password if cfg.encryption_enabled else ""
    encryption = (
        EncryptionManager(identity.fingerprint, password=password)
        if cfg.encryption_enabled else None
    )
    needs_save = not has_key
    if cfg.encryption_enabled and password and (not cfg.encryption_password_hash or not has_key):
        cfg.encryption_password_hash = make_password_hash(password, identity.fingerprint)
        needs_save = True
    return IdentitySession(cfg, pairing, encryption, needs_save)
