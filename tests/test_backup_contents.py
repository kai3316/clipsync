"""Every configurable field is either backed up or explained.

The failure this holds shut is silent by construction: a field nobody exports is
written into the archive as *nothing*, and a restore fills it in with the
default.  For most fields that is a preference reset; for ``file_open_to_all``
and ``chat_open_to_all`` it is a machine that accepts files and conversations it
used to refuse — a restore that makes the device *less* private than the one the
backup came from, with neither the archive nor the UI saying so.
"""

from __future__ import annotations

import dataclasses

from internal.config.config import Config
from internal.data.backup import BACKUP_SKIPPED, backup_config_dict


def _config_fields() -> set[str]:
    return {field.name for field in dataclasses.fields(Config)}


def test_every_config_field_is_exported_or_explained():
    exported = set(backup_config_dict(Config()))
    unexplained = _config_fields() - exported - set(BACKUP_SKIPPED)
    assert not unexplained, (
        "these config fields would come back as defaults after a restore, and "
        f"nothing says whether that is intended: {sorted(unexplained)}"
    )


def test_the_skip_list_only_names_real_fields():
    """A stale entry would hide a field somebody renamed."""
    stale = set(BACKUP_SKIPPED) - _config_fields() - {"device_id"}
    assert not stale, f"BACKUP_SKIPPED names fields Config does not have: {sorted(stale)}"


def test_every_skip_entry_has_a_reason():
    empty = [name for name, reason in BACKUP_SKIPPED.items() if not reason.strip()]
    assert empty == [], f"no reason given for skipping: {empty}"


def test_secrets_are_not_in_the_archive():
    exported = backup_config_dict(Config())
    for secret in (
        "private_key_pem",
        "certificate_pem",
        "encryption_password",
        "encryption_password_hash",
        "web_token",
        "translate_api_key",
        "companion_remote_token",
        "netpair_password",
    ):
        assert secret not in exported, f"{secret} must not travel in a backup"


def test_the_permissive_fields_keep_the_value_that_was_set():
    """Not the default — the value the user actually chose.

    ``file_open_to_all`` and ``chat_open_to_all`` default to the open behaviour,
    so asserting on the default proves nothing.  What matters is that a device
    configured to refuse arrives at the other side still refusing.
    """
    configured = Config()
    configured.file_open_to_all = False
    configured.chat_open_to_all = False
    configured.log_sharing = True
    exported = backup_config_dict(configured)

    assert exported["file_open_to_all"] is False
    assert exported["chat_open_to_all"] is False
    assert exported["log_sharing"] is True
