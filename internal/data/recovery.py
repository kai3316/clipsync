"""Move aside the artifacts that keep ClipSync from starting.

The sidecar deliberately refuses to start on a damaged configuration, identity
or history database instead of regenerating the device identity.  The identity
is the storage key for encrypted history and the certificate every peer pinned,
so silently replacing it would discard the history and force a re-pair of every
device — a refusal is right, and ``internal/config/config.py`` says so in
``_archive_corrupt_config``.

What was missing is a way out.  ``factory reset`` is a sidecar command, so it
needs the very process that will not start, and the user's only remaining move
was to find and delete files by hand.  This module is that way out: it re-runs
the same checks the application runs on startup and moves the offending files to
``<name>.corrupt-<stamp>`` beside them.  Nothing is deleted — the old
configuration is one rename away from being restored — and a second run finds
nothing left to do.

The checks live here rather than in ``SidecarApplication`` so that "what the app
refuses to start on" and "what recovery sets aside" cannot drift apart.

One damaged state is deliberately *not* repaired.  When encryption is on, a
password hash is stored and the identity is gone, the app comes up locked and no
password will ever satisfy it — the hash is a bare PBKDF2 digest salted with the
certificate fingerprint that stores no marker of *which* fingerprint it used, so
"made with the fingerprint now destroyed" is byte-identical to "made with an
empty fingerprint", which is a working lock that opens with the right password.
Nothing can tell those two apart, and the lock is a data-protection property
(``factory_reset`` refuses while locked, so a stolen laptop cannot be wiped
without the password), so a repair pass must not touch either.  See the ledger's
"Data-directory repair" entry.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from internal.config.config import Config, config_dir, load
from internal.security.encryption import is_encrypted

logger = logging.getLogger(__name__)

CONFIG_NAME = "config.json"
HISTORY_NAME = "clipboard_history.db"
# SQLite replays a stale write-ahead log into a fresh database, which would
# resurrect the very rows a quarantine is supposed to set aside.
HISTORY_COMPANIONS = ("clipboard_history.db-wal", "clipboard_history.db-shm")
LEGACY_HISTORY_NAME = "clipboard_history.json"

# Reasons, as stable codes: the caller turns them into a sentence in the user's
# language rather than showing whatever text this module happens to hold.
REASON_UNREADABLE = "unreadable"
REASON_IDENTITY = "identity"
REASON_HISTORY_CORRUPT = "history_corrupt"
REASON_HISTORY_IDENTITYLESS = "history_identityless"
REASON_HISTORY_IDENTITY_LOST = "history_identity_lost"

# The fields the identity is made of.  ``config.load()`` drops a field whose
# type is wrong and keeps the default, which is harmless for a port number and
# destructive for the private key, so these are checked strictly first.
IDENTITY_FIELDS = (
    "device_id",
    "private_key_pem",
    "certificate_pem",
    "encryption_password_hash",
    "encryption_password",
)


@dataclass(frozen=True)
class Finding:
    """One artifact that has to move for the app to start again."""

    artifact: str  # "config" | "history"
    reason: str
    files: tuple[str, ...]  # file names, relative to the data directory


def config_problem(path: Path) -> str | None:
    """Why ``config.json`` is unusable, or None when the app can load it.

    Mirrors the check the application runs before ``load()`` is allowed near the
    file: a root that is not an object, a version this build does not know, or
    an identity field of the wrong type are all reasons to stop rather than to
    start with defaults.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return REASON_UNREADABLE
    if not isinstance(data, dict):
        return REASON_UNREADABLE
    version = data.get("config_version", 1)
    if type(version) is not int or not 1 <= version <= 2:
        return REASON_UNREADABLE
    for field in IDENTITY_FIELDS:
        if field in data and not isinstance(data[field], str):
            return REASON_UNREADABLE
    if "encryption_enabled" in data and type(data["encryption_enabled"]) is not bool:
        return REASON_UNREADABLE
    return None


def declares_encryption(path: Path) -> bool:
    """Whether a readable config says encryption is on, tolerating a broken one.

    Used to decide the blast radius of a quarantined identity: history written
    while encryption was on is keyed by the fingerprint of the identity being
    destroyed, so it can never be read again.  A config too broken to answer is
    reported as False, which keeps the history for a manual rescue instead of
    discarding rows that may well be plaintext.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("encryption_enabled") is True


def identity_problem(cfg: Config) -> str | None:
    """Why the stored identity is unusable, or None when the app can use it.

    Only meaningful while the app is unlocked.  With encryption on and a
    verification hash stored, the private key stays encrypted until the user
    supplies the password, so there is nothing to validate yet — and, since a
    wrong password is not damage, nothing to recover either.

    An inventory with no identity at all is likewise not damage: the app creates
    and saves one on first run, which is what a repair pass has to leave room
    for.  Only a *partial* or unusable identity is a finding.
    """
    if cfg.encryption_enabled and cfg.encryption_password_hash:
        return None
    if not cfg.private_key_pem and not cfg.certificate_pem:
        return None
    from internal.infrastructure.security.device_identity import (
        IdentityInvalidError,
        prepare_identity,
    )

    try:
        # In memory only: `prepare_identity` returns a candidate and never saves,
        # so a repair pass cannot write a fresh identity over the damaged one.
        prepare_identity(cfg, "")
    except IdentityInvalidError:
        return REASON_IDENTITY
    return None


def history_problem(path: Path) -> str | None:
    """Why the history database is unusable, or None when the app can open it.

    Integrity only: an encrypted database whose identity is gone is a different
    finding, because the rows are intact and only the key is missing.
    """
    if not path.exists():
        return None
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            return REASON_HISTORY_CORRUPT
    except sqlite3.Error:
        return REASON_HISTORY_CORRUPT
    finally:
        if connection is not None:
            connection.close()
    return None


def records_without_identity(path: Path) -> bool:
    """Whether history exists that no identity can unlock.

    The storage key is derived from the certificate fingerprint, so history
    written before this device had an identity — or history left behind by one
    that was replaced — can never be read again.  Starting over the top of it
    would only produce ciphertext where the user expects their clips.

    What decides is the rows, not the configuration that named them.  A stored
    field is base64 of ``PREFIX || nonce || ciphertext`` when it is encrypted and
    the user's own text when it is not, so history from an install that had
    encryption turned off is readable without any key at all.  Refusing it would
    be refusing the clips themselves: the configuration that said so is the file
    the repair just moved aside, and the replacement says encryption is on.
    """
    # Both artifacts are asked: a readable file beside a keyed one is still
    # history nobody can open.
    legacy = path.parent / LEGACY_HISTORY_NAME
    if legacy.exists() and _json_entries_carry_ciphertext(legacy):
        return True
    if not path.exists():
        return False
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        rows = connection.execute("SELECT text_preview, types FROM history").fetchall()
        return any(_carries_ciphertext(value) for row in rows for value in row)
    except sqlite3.Error:
        # Unreadable counts as records: assuming there are none would hand a
        # fresh identity the job of decrypting rows that are already there.
        return True
    finally:
        # Closed explicitly, not by `with`: on Windows an open handle would
        # block the rename this module exists to perform.
        if connection is not None:
            connection.close()


def _json_entries_carry_ciphertext(path: Path) -> bool:
    """The same question for the pre-database history file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, list):
        return True
    return any(
        _carries_ciphertext(value)
        for entry in data
        if isinstance(entry, dict)
        for value in entry.values()
    )


def _carries_ciphertext(value) -> bool:
    """Whether one stored field is encrypted, whichever column it came from.

    ``SELECT`` hands back the ``types`` column as the JSON text it was written
    as, so the sub-values of an object are looked at too.  Text that is not valid
    base64 — or that decodes to anything but our marker — is the user's own data.
    """
    if isinstance(value, dict):
        return any(_carries_ciphertext(item) for item in value.values())
    if not isinstance(value, str) or not value:
        return False
    if value[:1] in "{[":
        # The `types` column is a JSON object of per-format fields. A clip whose
        # preview is empty — a copied file list, say — is encrypted only here.
        try:
            return _carries_ciphertext(json.loads(value))
        except ValueError:
            return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return is_encrypted(decoded)


def inspect(directory: Path | None = None) -> list[Finding]:
    """Everything that has to move before the application will start again.

    Order matters: a configuration that has to go takes the history with it when
    it was encrypted, because the key went with the identity.
    """
    directory = directory or config_dir()
    config = directory / CONFIG_NAME
    history = directory / HISTORY_NAME
    findings: list[Finding] = []

    problem = config_problem(config)
    if problem is not None:
        findings.append(Finding("config", problem, (CONFIG_NAME,)))
        if declares_encryption(config):
            findings.append(
                Finding("history", REASON_HISTORY_IDENTITY_LOST, _history_files(directory))
            )
        return findings

    cfg = load()
    problem = identity_problem(cfg)
    if problem is not None:
        findings.append(Finding("config", problem, (CONFIG_NAME,)))
        if cfg.encryption_enabled:
            findings.append(
                Finding("history", REASON_HISTORY_IDENTITY_LOST, _history_files(directory))
            )
        return findings

    if cfg.encryption_enabled and cfg.encryption_password_hash:
        # Locked, and the app starts that way: it waits for the password instead
        # of refusing. Which identity keyed the history — and so whether the rows
        # are readable at all — is exactly what the password unlocks, so a repair
        # pass has nothing it can know here. Moving the database would discard
        # records that the very next unlock proves were fine.
        return findings

    corrupt = history_problem(history)
    if corrupt is not None:
        findings.append(Finding("history", corrupt, _history_files(directory)))
    elif not cfg.certificate_pem and records_without_identity(history):
        findings.append(
            Finding("history", REASON_HISTORY_IDENTITYLESS, _history_files(directory))
        )
    return findings


def quarantine(directory: Path | None = None) -> list[dict]:
    """Move every finding aside and report what moved.

    Returns one ``{"artifact", "reason", "files"}`` entry per finding — the
    archived names, so the caller can tell the user what was set aside and where
    it went.  An empty list means the data directory was already usable, which
    makes this safe to offer as a plain "try to repair" action.
    """
    directory = directory or config_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report: list[dict] = []
    for finding in inspect(directory):
        moved: list[str] = []
        for name in finding.files:
            source = directory / name
            try:
                if source.exists():
                    archived = source.with_name(f"{source.name}.corrupt-{stamp}")
                    os.replace(source, archived)
                    moved.append(archived.name)
            except OSError:
                logger.warning("Recovery: could not set aside %s", name, exc_info=True)
        if moved:
            logger.info(
                "Recovery: set aside %s (%s) as %s",
                finding.artifact, finding.reason, ", ".join(moved),
            )
            report.append(
                {"artifact": finding.artifact, "reason": finding.reason, "files": moved}
            )
    return report


def _history_files(directory: Path) -> tuple[str, ...]:
    """The database plus any companions that are actually present."""
    names = [HISTORY_NAME, *HISTORY_COMPANIONS]
    return tuple(name for name in names if (directory / name).exists())
