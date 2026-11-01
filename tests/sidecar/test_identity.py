import json

import pytest

from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config, PeerInfo, load, save
from internal.infrastructure.security.device_identity import (
    IdentityInvalidError,
    prepare_identity,
)
from internal.security.encryption import EncryptionManager, make_password_hash
from internal.security.pairing import PairingManager


def existing_identity(password="", enabled=True):
    cfg = Config(encryption_enabled=enabled)
    identity = PairingManager(cfg.device_id, cfg.device_name).load_or_create_identity("", "")
    cfg.private_key_pem = identity.private_key_pem
    cfg.certificate_pem = identity.certificate_pem
    if password:
        cfg.encryption_password_hash = make_password_hash(password, identity.fingerprint)
    return cfg, EncryptionManager(identity.fingerprint, password=password)


def test_first_start_creates_persistent_identity_before_opening_network(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    original_fingerprint = None
    for _ in range(2):
        app = SidecarApplication()
        app.lifecycle.start()
        try:
            fingerprint = app.identity.pairing.get_identity().fingerprint
            if original_fingerprint is not None:
                assert fingerprint == original_fingerprint
            original_fingerprint = fingerprint
            assert app.status()["sync_state"] == "not_started"
            assert app.identity.config.device_id == load().device_id
            raw = (tmp_path / "config.json").read_text(encoding="utf-8")
            assert "BEGIN PRIVATE KEY" not in raw
            assert '"encryption_password":' not in raw
        finally:
            app.lifecycle.stop()
        assert app.identity is None
        assert app.config.private_key_pem == ""


def test_existing_protected_identity_and_history_survive_unlock_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    cfg, encryption = existing_identity("test-password")
    save(cfg, encryption)
    repo = ClipboardHistoryDB(
        storage_path=str(tmp_path / "clipboard_history.db"), enc_mgr=encryption
    )
    repo.add(ClipboardContent(types={ContentType.TEXT: b"protected history"}))
    repo.close()
    original = (tmp_path / "config.json").read_bytes()
    for _ in range(2):
        app = SidecarApplication()
        app.lifecycle.start()
        try:
            assert app.identity is None
            assert app.status()["health"] == "locked"
            app.unlock("test-password")
            assert app.identity.pairing.get_identity().certificate_pem == cfg.certificate_pem
            assert app.require_history().list()["items"][0]["preview"] == "protected history"
            assert (tmp_path / "config.json").read_bytes() == original
        finally:
            app.lifecycle.stop()


@pytest.mark.parametrize("damage", ["missing_key", "missing_certificate", "mismatch", "bad_pem"])
def test_invalid_identity_is_never_replaced_or_saved(tmp_path, monkeypatch, damage):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    cfg, encryption = existing_identity()
    if damage == "missing_key":
        cfg.private_key_pem = ""
    elif damage == "missing_certificate":
        cfg.certificate_pem = ""
    elif damage == "mismatch":
        other, _ = existing_identity()
        cfg.private_key_pem = other.private_key_pem
    else:
        cfg.certificate_pem = "invalid certificate"
    save(cfg, encryption)
    path = tmp_path / "config.json"
    original = path.read_bytes()
    app = SidecarApplication()
    with pytest.raises(ApplicationError) as exc:
        app.lifecycle.start()
    assert exc.value.code == "DATA_INVALID"
    assert path.read_bytes() == original
    assert not (tmp_path / "clipboard_history.db").exists()
    assert not (tmp_path / ".lock").exists()


def test_a_stored_password_survives_its_identity_being_lost(tmp_path, monkeypatch):
    # A password whose salt — the certificate fingerprint — is gone can never be
    # satisfied, but the hash stores no fingerprint, so this is indistinguishable
    # from a lock made with an empty fingerprint that the right password opens.
    # Nothing may guess: the app locks, and a repair pass must leave it alone.
    from internal.data.recovery import inspect, quarantine

    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    cfg, encryption = existing_identity("test-password")
    cfg.private_key_pem = ""
    cfg.certificate_pem = ""
    save(cfg, encryption)
    path = tmp_path / "config.json"
    original = path.read_bytes()
    app = SidecarApplication()
    app.lifecycle.start()
    try:
        assert app.status()["health"] == "locked"
        assert app.identity is None
        assert inspect() == []
        assert quarantine() == []
    finally:
        app.lifecycle.stop()
    assert path.read_bytes() == original


def test_wrong_key_authentication_does_not_become_new_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    cfg, _ = existing_identity()
    save(cfg, EncryptionManager("different-storage-key"))
    original = (tmp_path / "config.json").read_bytes()
    app = SidecarApplication()
    with pytest.raises(ApplicationError) as exc:
        app.lifecycle.start()
    assert exc.value.code == "DATA_INVALID"
    assert (tmp_path / "config.json").read_bytes() == original


def test_existing_peer_trust_is_restored_without_repairing():
    cfg, _ = existing_identity(enabled=False)
    peer, _ = existing_identity()
    cfg.peers[peer.device_id] = PeerInfo(
        peer.device_id, peer.device_name, peer.certificate_pem, paired=True
    )
    cfg.peers["restored"] = PeerInfo("restored", "Restored peer", paired=False)
    prepared = prepare_identity(cfg)
    assert prepared.pairing.is_peer_paired(peer.device_id)
    assert prepared.pairing.get_peer_certificate(peer.device_id) == peer.certificate_pem
    assert not prepared.pairing.is_peer_paired("restored")
    assert len(prepared.pairing.get_known_peers()) == 2
    assert not prepared.needs_save
    assert prepared.config is not cfg


def test_certificate_device_id_must_match():
    cfg, _ = existing_identity(enabled=False)
    cfg.device_id = "different"
    with pytest.raises(IdentityInvalidError):
        prepare_identity(cfg)


def test_existing_history_without_identity_does_not_get_a_different_storage_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config())
    repo = ClipboardHistoryDB(
        storage_path=str(tmp_path / "clipboard_history.db"), enc_mgr=EncryptionManager("")
    )
    repo.add(ClipboardContent(types={ContentType.TEXT: b"prototype history"}))
    repo.close()
    original = (tmp_path / "config.json").read_bytes()
    app = SidecarApplication()
    with pytest.raises(ApplicationError, match="no device identity"):
        app.lifecycle.start()
    assert (tmp_path / "config.json").read_bytes() == original
    reopened = ClipboardHistoryDB(
        storage_path=str(tmp_path / "clipboard_history.db"), enc_mgr=EncryptionManager("")
    )
    try:
        assert reopened.get_all()[0]["text_preview"] == "prototype history"
    finally:
        reopened.close()


def test_legacy_password_migrates_with_existing_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    cfg, encryption = existing_identity("legacy-password")
    cfg.encryption_password_hash = ""
    save(cfg, encryption)
    path = tmp_path / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["encryption_password"] = "legacy-password"
    path.write_text(json.dumps(raw), encoding="utf-8")
    app = SidecarApplication()
    app.lifecycle.start()
    try:
        assert app.status()["health"] == "ready"
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert "encryption_password" not in persisted
        assert persisted["encryption_password_hash"]
        assert persisted["certificate_pem"] == cfg.certificate_pem
    finally:
        app.lifecycle.stop()
    restarted = SidecarApplication()
    restarted.lifecycle.start()
    try:
        assert restarted.status()["health"] == "locked"
        assert restarted.unlock("legacy-password") == {"unlocked": True}
    finally:
        restarted.lifecycle.stop()


def test_identity_save_failure_rolls_back_resources_and_can_retry(tmp_path, monkeypatch):
    import internal.application.bootstrap as bootstrap

    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    with monkeypatch.context() as patch:
        def fail_save(*_):
            raise OSError("sensitive-file-path")

        patch.setattr(bootstrap, "save", fail_save)
        app = SidecarApplication()
        with pytest.raises(ApplicationError) as exc:
            app.lifecycle.start()
        assert exc.value.code == "SAVE_FAILED"
        assert exc.value.retryable
        assert "sensitive-file-path" not in str(exc.value)
        assert app.identity is None
        assert app.history is None
        assert not (tmp_path / ".lock").exists()
    restarted = SidecarApplication()
    restarted.lifecycle.start()
    restarted.lifecycle.stop()
