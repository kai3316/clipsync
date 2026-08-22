"""Tests for backup/restore config validation (issue #23).

Covers:
  (a) a valid restore persists the config to disk immediately,
  (b) a malformed backup (bad port/web_port) does not crash and the bad
      values are rejected/skipped or clamped,
  (c) _apply_config rejects a string where an int is required.
"""

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import internal.config.config as config_module
from internal.data.backup import _apply_config
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
    zip_path = _make_backup_zip(config_dir, {
        "device_name": "Restored Host",
        "port": 23456,
        "web_port": 23457,
        "language": "en",
        "appearance_mode": "dark",
    })

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
    zip_path = _make_backup_zip(config_dir, {
        "device_name": "Bad Backup",
        "port": "abc",          # wrong type -> skipped, stays default
        "web_port": 70000,      # out of range -> clamped to 65535
        "history_max_entries": 5,
    })

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


# (c) _apply_config rejects a string where an int is required.
def test_apply_config_rejects_string_for_int_field():
    cfg = config_module.Config()
    original_port = cfg.port
    original_entries = cfg.history_max_entries

    _apply_config({"port": "abc"}, cfg)
    assert cfg.port == original_port  # unchanged

    _apply_config({"history_max_entries": "100"}, cfg)
    assert cfg.history_max_entries == original_entries  # unchanged

    _apply_config({"web_port": 12345}, cfg)
    assert cfg.web_port == 12345  # a real int is applied


def test_apply_config_clamps_out_of_range_ints():
    cfg = config_module.Config()
    _apply_config({"port": 99999}, cfg)
    assert cfg.port == 65535

    _apply_config({"history_max_entries": 0}, cfg)
    assert cfg.history_max_entries == 1


def test_apply_config_skips_bad_bools_and_enums():
    cfg = config_module.Config()
    _apply_config({"sync_enabled": "yes"}, cfg)
    assert cfg.sync_enabled is True  # default unchanged

    _apply_config({"appearance_mode": "neon"}, cfg)
    assert cfg.appearance_mode == "system"  # default unchanged

    _apply_config({"language": "fr"}, cfg)
    assert cfg.language == "zh-CN"  # default unchanged
