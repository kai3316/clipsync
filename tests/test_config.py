"""Tests for Config — save/load, the one-way migrations on load, and recovery."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# We need to patch _config_dir and _config_path
import internal.config.config as config_module


class TestConfigSaveLoad:
    def test_roundtrip(self):
        """Save a config, load it back, verify all fields match."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.json"

        # Patch _config_dir and _config_path
        original_dir = config_module._config_dir
        original_path = config_module._config_path
        config_module._config_dir = lambda: tmp_dir
        config_module._config_path = lambda: config_path

        try:
            cfg = config_module.Config()
            cfg.device_name = "Roundtrip Test"
            cfg.port = 23456
            cfg.sync_enabled = False
            cfg.auto_start = True
            cfg.peers["peer1"] = config_module.PeerInfo(
                device_id="peer1",
                device_name="Peer One",
                public_key_pem="pem-data-here",
                paired=True,
            )

            config_module.save(cfg)

            # Verify file exists
            assert config_path.exists()

            # Load it back
            loaded = config_module.load()
            assert loaded.device_name == "Roundtrip Test"
            assert loaded.port == 23456
            assert loaded.sync_enabled is False
            assert loaded.auto_start is True
            assert "peer1" in loaded.peers
            assert loaded.peers["peer1"].device_name == "Peer One"
            assert loaded.peers["peer1"].public_key_pem == "pem-data-here"
            assert loaded.peers["peer1"].paired is True
        finally:
            config_module._config_dir = original_dir
            config_module._config_path = original_path

    def test_peers_save_as_list(self):
        """Peers dict should serialize as a JSON array."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.json"

        original_dir = config_module._config_dir
        original_path = config_module._config_path
        config_module._config_dir = lambda: tmp_dir
        config_module._config_path = lambda: config_path

        try:
            cfg = config_module.Config()
            cfg.peers["a"] = config_module.PeerInfo(device_id="a", device_name="A")
            cfg.peers["b"] = config_module.PeerInfo(device_id="b", device_name="B")
            config_module.save(cfg)

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            assert isinstance(raw["peers"], list)
            assert len(raw["peers"]) == 2
        finally:
            config_module._config_dir = original_dir
            config_module._config_path = original_path

    def test_removed_peers_roundtrip(self):
        """removed_peers archive round-trips through save/load, keeping
        removed_at and the restored-to-known-list fields."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.json"

        original_dir = config_module._config_dir
        original_path = config_module._config_path
        config_module._config_dir = lambda: tmp_dir
        config_module._config_path = lambda: config_path

        try:
            cfg = config_module.Config()
            cfg.peers["peer1"] = config_module.PeerInfo(
                device_id="peer1",
                device_name="Peer One",
                public_key_pem="pem-data-here",
                paired=True,
                notes="old note",
                last_ip="192.168.1.5",
                last_port=37377,
            )
            cfg.removed_peers["peer1"] = config_module.PeerInfo(
                device_id="peer1",
                device_name="Peer One",
                public_key_pem="pem-data-here",
                paired=True,
                notes="old note",
                last_ip="192.168.1.5",
                last_port=37377,
                removed_at=1234567890.5,
            )
            config_module.save(cfg)

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            assert isinstance(raw["removed_peers"], list)
            assert len(raw["removed_peers"]) == 1

            loaded = config_module.load()
            assert "peer1" in loaded.removed_peers
            rp = loaded.removed_peers["peer1"]
            assert rp.device_name == "Peer One"
            assert rp.paired is True
            assert rp.last_ip == "192.168.1.5"
            assert rp.removed_at == 1234567890.5
        finally:
            config_module._config_dir = original_dir
            config_module._config_path = original_path

    def test_old_config_without_removed_peers_loads_empty(self):
        """A config file predating the archive feature must load with an empty
        removed_peers dict (backward compatible)."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.json"
        config_path.write_text(json.dumps({"peers": []}), encoding="utf-8")

        original_dir = config_module._config_dir
        original_path = config_module._config_path
        config_module._config_dir = lambda: tmp_dir
        config_module._config_path = lambda: config_path

        try:
            loaded = config_module.load()
            assert loaded.removed_peers == {}
            assert loaded.peers == {}
        finally:
            config_module._config_dir = original_dir
            config_module._config_path = original_path


class TestConfigRecovery:
    def test_corrupted_json(self):
        """Corrupted config file should fall back to defaults."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.json"
        config_path.write_text("this is not valid json {{{", encoding="utf-8")

        original_dir = config_module._config_dir
        original_path = config_module._config_path
        config_module._config_dir = lambda: tmp_dir
        config_module._config_path = lambda: config_path

        try:
            cfg = config_module.load()
            # Should get defaults, not crash
            assert cfg.port == 19990
            assert isinstance(cfg.device_id, str)
        finally:
            config_module._config_dir = original_dir
            config_module._config_path = original_path

    def test_partial_json(self):
        """Valid JSON but missing fields should use defaults."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.json"
        config_path.write_text('{"device_name": "Partial", "port": 30000}', encoding="utf-8")

        original_dir = config_module._config_dir
        original_path = config_module._config_path
        config_module._config_dir = lambda: tmp_dir
        config_module._config_path = lambda: config_path

        try:
            cfg = config_module.load()
            assert cfg.device_name == "Partial"
            assert cfg.port == 30000
            # Other fields should be defaults
            assert cfg.sync_enabled is True  # default
            assert cfg.auto_start is False  # default
        finally:
            config_module._config_dir = original_dir
            config_module._config_path = original_path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# merged from test_backup_restore.py
# ══════════════════════════════════════════════════

import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.web.api.settings import restore_backup_api


def _make_backup_zip(config_dir: Path, config_data: dict) -> Path:
    """Write a backup zip containing only a config.json into *config_dir*."""
    zip_path = config_dir / "backup.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.json", json.dumps(config_data, ensure_ascii=False))
    return zip_path


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the config module at a temp dir and return (config_dir, config_path)."""
    config_dir = tmp_path / "configdir"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    monkeypatch.setattr(config_module, "_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: config_path)
    return config_dir, config_path


def _restore(zip_path: str, cfg):
    body = json.dumps({"backup_path": zip_path}).encode("utf-8")
    return restore_backup_api(body, cfg, None)


# (a) A valid restore persists the config to disk immediately.
def test_valid_restore_persists_config_to_disk(isolated_config):
    config_dir, config_path = isolated_config
    zip_path = _make_backup_zip(
        config_dir,
        {
            "device_name": "Restored Host",
            "port": 23456,
            "web_port": 23457,
            "language": "en",
            "appearance_mode": "dark",
        },
    )

    cfg = config_module.Config()
    assert cfg.port == 19990  # default

    data, status = _restore(str(zip_path), cfg)

    assert status == 200
    assert data["ok"] is True
    assert data["summary"]["config"] is True
    # In-memory config reflects the restored values
    assert cfg.port == 23456
    assert cfg.web_port == 23457
    assert cfg.device_name == "Restored Host"
    assert cfg.language == "en"
    # The restored config is persisted to disk immediately (issue #23)
    assert config_path.exists()
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["port"] == 23456
    assert on_disk["web_port"] == 23457
    assert on_disk["device_name"] == "Restored Host"


# (b) A malformed backup must not crash; bad values are skipped/clamped.
def test_restore_with_invalid_port_values_does_not_crash(isolated_config):
    config_dir, _config_path = isolated_config
    zip_path = _make_backup_zip(
        config_dir,
        {
            "device_name": "Bad Backup",
            "port": "abc",  # wrong type -> skipped, stays default
            "web_port": 70000,  # out of range -> clamped to 65535
            "history_max_entries": 5,
        },
    )

    cfg = config_module.Config()
    original_port = cfg.port  # 19990
    original_web_port = cfg.web_port  # 19991

    data, status = _restore(str(zip_path), cfg)

    # The request succeeds and nothing crashes.
    assert status == 200
    assert data["ok"] is True
    assert data["summary"]["config"] is True
    # port="abc" is rejected/skipped: the default is left untouched.
    assert cfg.port == original_port
    # web_port=70000 is out of range: clamped to the allowed maximum.
    assert cfg.web_port == 65535
    assert cfg.web_port != original_web_port
    # Other valid fields are still applied.
    assert cfg.history_max_entries == 5
    assert cfg.device_name == "Bad Backup"


# ══════════════════════════════════════════════════
# split from test_round13_wrapup.py — config/backup new-key roundtrip
# ══════════════════════════════════════════════════


def _point_config_at(tmp_path, monkeypatch, data=None):
    import internal.config.config as config_module

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(data or {}), encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: cfg_path)
    return config_module


def test_config_save_load_roundtrips_new_keys(tmp_path, monkeypatch):
    config_module = _point_config_at(tmp_path, monkeypatch, {})
    cfg = config_module.load()
    cfg.internet_sync_enabled = True
    cfg.relay_brokers = ["wss://broker.emqx.io:8884/mqtt"]
    cfg.relay_private_brokers = ["mqtt://mqttyyc.top:1883"]
    cfg.relay_username = "clipsync_mqtt"
    cfg.relay_password = "s3cret!"
    cfg.relay_secret = "ab" * 32
    cfg.peer_relay_secrets = {"peer-1": "cd" * 32}
    cfg.ai_config_tools = ["claude_code", "codex"]
    cfg.ai_config_custom_paths = ["~/ai-configs"]
    # The "I cleared the phone token on purpose" record has to survive a save
    # and a load, because the thing it guards against — the companion minting a
    # token on the next start — happens after a reload, not before.
    cfg.web_token = ""
    cfg.web_token_disabled = True
    config_module.save(cfg)

    cfg2 = config_module.load()
    assert cfg2.internet_sync_enabled is True
    assert cfg2.relay_brokers == cfg.relay_brokers
    assert cfg2.relay_private_brokers == cfg.relay_private_brokers
    assert cfg2.relay_username == cfg.relay_username
    assert cfg2.relay_password == cfg.relay_password
    assert cfg2.relay_secret == cfg.relay_secret
    assert cfg2.peer_relay_secrets == cfg.peer_relay_secrets
    assert cfg2.ai_config_tools == cfg.ai_config_tools
    assert cfg2.ai_config_custom_paths == cfg.ai_config_custom_paths
    assert cfg2.web_token_disabled is True


def test_config_migrates_legacy_ai_config_paths(tmp_path, monkeypatch):
    """A pre-refactor ai_config_paths list is classified on load.

    Old paths that name a built-in profile entry enable that tool; anything
    else survives as a custom path.  The migration is one-way and idempotent —
    a later save persists the new fields, so a re-load never re-runs it.
    """
    config_module = _point_config_at(
        tmp_path,
        monkeypatch,
        {
            "ai_config_paths": [
                "~/.claude/CLAUDE.md",  # → claude_code profile
                "~/.codex/config.toml",  # → codex profile
                "~/ai-notes",  # → custom path
                "  ",  # → dropped
            ],
        },
    )
    cfg = config_module.load()
    assert cfg.ai_config_tools == ["claude_code", "codex"]
    assert cfg.ai_config_custom_paths == ["~/ai-notes"]

    # Idempotent: reloading the same (un-migrated) file yields the same result.
    cfg2 = config_module.load()
    assert cfg2.ai_config_tools == cfg.ai_config_tools
    assert cfg2.ai_config_custom_paths == cfg.ai_config_custom_paths


def test_config_legacy_ai_config_paths_unmatched_keeps_feature_on(tmp_path, monkeypatch):
    """No built-in path matched → all tools stay enabled (feature stays on)."""
    config_module = _point_config_at(
        tmp_path,
        monkeypatch,
        {
            "ai_config_paths": ["~/notes-a", "~/notes-b"],
        },
    )
    cfg = config_module.load()
    from internal.sync.ai_profiles import DEFAULT_TOOL_KEYS

    assert cfg.ai_config_tools == list(DEFAULT_TOOL_KEYS)
    assert cfg.ai_config_custom_paths == ["~/notes-a", "~/notes-b"]


def test_remote_access_comes_on_for_a_config_written_while_it_was_off(tmp_path, monkeypatch):
    """A config written while the default was OFF is brought forward once.

    A token in the config proves nothing — the webview front end mints one
    whether or not the companion was ever enabled — so it is not consulted.
    What matters is that the migration is one-way: the save below writes the new
    version, and the reload after it leaves a choice the user made alone.
    """
    config_module = _point_config_at(
        tmp_path,
        monkeypatch,
        {"config_version": 2, "web_enabled": False, "web_token": "tok"},
    )
    cfg = config_module.load()
    assert cfg.web_enabled is True

    # Off again, on a config this build wrote: a real decision, kept.
    cfg.web_enabled = False
    config_module.save(cfg)
    assert config_module.load().web_enabled is False


def test_remote_access_stays_off_where_it_would_serve_without_a_token(tmp_path, monkeypatch):
    """``web_token_disabled`` is the one state the migration leaves alone.

    It means the user cleared the access token on purpose, and a companion
    started from such a config answers every request without one — so turning it
    on here would not be applying a default, it would be opening the port.
    """
    config_module = _point_config_at(
        tmp_path,
        monkeypatch,
        {"config_version": 2, "web_enabled": False, "web_token_disabled": True},
    )
    assert config_module.load().web_enabled is False


@pytest.fixture()
def _isolated_favorites(tmp_path, monkeypatch):
    """Point the favorites paths at tmp so tests never touch real user data."""
    import internal.data.backup as backup_mod

    monkeypatch.setattr(
        backup_mod,
        "_get_favorites_db_path",
        lambda: tmp_path / "favorites.db",
    )
    monkeypatch.setattr(
        backup_mod,
        "_get_favorites_path",
        lambda: tmp_path / "favorites.json",
    )
    return tmp_path


def test_backup_roundtrips_new_config_keys(tmp_path, _isolated_favorites):
    import internal.data.backup as backup_mod
    from internal.clipboard.history_db import ClipboardHistoryDB
    from internal.config.config import Config

    cfg = Config()
    cfg.internet_sync_enabled = True
    cfg.relay_brokers = ["wss://broker.hivemq.com:8884/mqtt"]
    cfg.relay_private_brokers = ["mqtt://mqttyyc.top:1883", "ws://mqttyyc.top:8083/mqtt"]
    cfg.relay_username = "clipsync_mqtt"
    cfg.relay_password = "s3cret!"
    cfg.relay_secret = "ab" * 32
    cfg.peer_relay_secrets = {"peer-1": "cd" * 32}
    cfg.ai_config_tools = ["gemini"]
    cfg.ai_config_custom_paths = ["~/ai-configs"]

    history = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
    zip_path = backup_mod.create_backup(cfg, history, backup_dir=str(tmp_path / "bk"))

    # The archive itself carries the keys.
    with zipfile.ZipFile(zip_path) as zf:
        exported = json.loads(zf.read("config.json").decode("utf-8"))
    assert exported["internet_sync_enabled"] is True
    assert exported["relay_brokers"] == cfg.relay_brokers
    assert exported["relay_private_brokers"] == cfg.relay_private_brokers
    assert exported["relay_username"] == cfg.relay_username
    assert exported["relay_password"] == cfg.relay_password
    assert exported["peer_relay_secrets"] == {"peer-1": "cd" * 32}
    assert exported["ai_config_tools"] == cfg.ai_config_tools
    assert exported["ai_config_custom_paths"] == cfg.ai_config_custom_paths
    # The web companion's credentials are deliberately not in a backup — neither
    # `web_token` nor the `web_token_disabled` record of clearing it.  A
    # restored machine therefore mints a fresh token and the phone re-pairs,
    # which is the safe direction for a file that travels.
    assert "web_token" not in exported and "web_token_disabled" not in exported

    fresh = Config()  # defaults everywhere
    result = backup_mod.restore_backup(zip_path, fresh, history)
    assert result["config"] is True
    assert fresh.internet_sync_enabled is True
    assert fresh.relay_brokers == cfg.relay_brokers
    assert fresh.relay_private_brokers == cfg.relay_private_brokers
    assert fresh.relay_username == cfg.relay_username
    assert fresh.relay_password == cfg.relay_password
    assert fresh.relay_secret == cfg.relay_secret
    assert fresh.peer_relay_secrets == {"peer-1": "cd" * 32}
    assert fresh.ai_config_tools == cfg.ai_config_tools
    assert fresh.ai_config_custom_paths == cfg.ai_config_custom_paths
