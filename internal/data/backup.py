"""Backup and restore for ClipSync data.

Creates timestamped zip archives containing config.json, history.json,
and favorites.json.  Favorites are exported from SQLite when the DB is
in use, falling back to the legacy JSON file when the DB doesn't exist.
"""

import contextlib
import json
import logging
import os
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from internal.config.config import FIELD_RULES, Config, PeerInfo
from internal.fsutil import unique_export_path

if TYPE_CHECKING:
    from internal.clipboard.history_db import ClipboardHistoryDB

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR_NAME = "backups"


def _get_backup_dir(backup_dir: str | None = None) -> Path:
    """Return the backup directory, creating it if necessary."""
    from internal.config.config import _config_dir

    base = Path(backup_dir) if backup_dir else _config_dir() / DEFAULT_BACKUP_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _get_favorites_path() -> Path:
    """Return the path to the legacy favorites JSON file."""
    from internal.config.config import _config_dir

    return _config_dir() / "favorites.json"


def _get_favorites_db_path() -> Path:
    """Return the path to the SQLite favorites database."""
    from internal.config.config import _config_dir

    return _config_dir() / "favorites.db"


class BackupIncompleteError(RuntimeError):
    """A backup that could not include everything it promises.

    Raised instead of being folded into "there was nothing to include".  The
    favorites export used to return ``False`` for both an empty database and a
    database it could not read: the caller then copied the legacy JSON file if
    one happened to exist, and otherwise packed an archive *without*
    ``favorites.json`` and reported success.  A user who then wiped the machine
    had a backup of everything except the thing that failed.
    """


def _export_favorites_to_json(filepath: Path) -> int:
    """Export favorites from SQLite to a JSON file for backup.

    Returns the number of favorites written — 0 is a legitimate answer ("there
    are none", and the file is written as ``[]`` so the archive says so).
    A database that exists but cannot be read raises :class:`BackupIncompleteError`:
    an unreadable store is not an empty one, and the difference is the whole
    question a backup exists to answer.
    """
    import sqlite3

    db_path = _get_favorites_db_path()
    if not db_path.exists():
        return -1  # no database at all: the caller falls back to the legacy file
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                'SELECT id, title, content, "group", position, created, updated '
                "FROM favorites ORDER BY created DESC"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("Backup aborted: favorites database could not be read: %s", exc)
        raise BackupIncompleteError("favorites database could not be read") from exc

    favorites = []
    for row in rows:
        entry = {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "group": row[3],
            "position": row[4],
            "created": row[5],
        }
        if row[6] is not None:
            entry["updated"] = row[6]
        favorites.append(entry)

    filepath.write_text(
        json.dumps(favorites, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(favorites)


def _import_favorites_from_json(filepath: Path) -> int:
    """Import favorites from a JSON file into the SQLite database.

    Returns the number of imported entries.
    """
    import sqlite3

    if not filepath.is_file():
        return 0

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(data, list):
        return 0

    db_path = _get_favorites_db_path()
    os.makedirs(db_path.parent, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS favorites (
                id       TEXT PRIMARY KEY,
                title    TEXT NOT NULL DEFAULT '',
                content  TEXT NOT NULL DEFAULT '',
                "group"  TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created  REAL NOT NULL,
                updated  REAL
            );
        """)
        conn.commit()

        count = 0
        with conn:
            for item in data:
                conn.execute(
                    "INSERT OR REPLACE INTO favorites "
                    '(id, title, content, "group", position, created, updated) '
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        # id-less legacy entries would all collapse onto the
                        # single pk "" row under INSERT OR REPLACE; mint a uuid
                        # so N entries restore as N rows (mirrors favorites.py).
                        item.get("id") or uuid.uuid4().hex[:12],
                        item.get("title", ""),
                        item.get("content", ""),
                        item.get("group", ""),
                        item.get("position", 0),
                        item.get("created", 0),
                        item.get("updated"),
                    ),
                )
                count += 1
        return count
    finally:
        conn.close()


def backup_config_dict(cfg: Config) -> dict:
    """The config a backup carries, as the JSON written to ``config.json``.

    Every field of :class:`Config` is either in here or named in
    :data:`BACKUP_SKIPPED` with the reason it is not.  The contract matters
    because the drift it prevents is silent: a preference nobody exports is
    restored as its *default*, and several of these defaults are the permissive
    ones — a backup made on a machine that had turned automatic file receiving
    off came back with it on, and nothing in the archive said so.
    """
    # Snapshot the peers dict first: pairing threads add/remove peers on
    # cfg.peers, and iterating the live dict mid-backup raises "dictionary
    # changed size during iteration".
    peers = [
        {
            "device_id": p.device_id,
            "device_name": p.device_name,
            "paired": p.paired,
            "notes": p.notes,
        }
        for p in list(cfg.peers.values())
    ]
    return {
        # ---- identity and transport ----
        "device_id": cfg.device_id,
        "device_name": cfg.device_name,
        "port": cfg.port,
        "service_type": cfg.service_type,
        "sync_enabled": cfg.sync_enabled,
        "auto_start": cfg.auto_start,
        "filter_enabled_categories": cfg.filter_enabled_categories,
        # ---- internet (cross-network) relay ----
        # A backup→restore cycle must not silently drop these; they are
        # re-applied through the same schema on restore.  The relay secret and
        # the per-peer secrets ride along so internet sync survives a restore
        # without re-enrolling over the LAN.
        "internet_sync_enabled": cfg.internet_sync_enabled,
        "relay_brokers": list(cfg.relay_brokers),
        "relay_private_brokers": list(cfg.relay_private_brokers),
        "relay_username": cfg.relay_username,
        "relay_password": cfg.relay_password,
        # The relay's per-message ceiling sizes the file chunks sent over it.
        # Restoring onto a device that then talks to the same broker needs the
        # same number, or its chunks get dropped in flight.
        "relay_max_message_bytes": cfg.relay_max_message_bytes,
        # ...and the pace those chunks are offered at, the other half of the
        # same setting: a broker refuses a chunk past its ceiling and drops most
        # of a burst offered too fast.  Both numbers describe one relay.
        "relay_max_bytes_per_second": cfg.relay_max_bytes_per_second,
        "relay_secret": cfg.relay_secret,
        "peer_relay_secrets": {
            k: v
            for k, v in (cfg.peer_relay_secrets or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        },
        "netpair_secrets": {
            k: v
            for k, v in (cfg.netpair_secrets or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        },
        # The key-agreement material those secrets key.  A restore that kept
        # the secrets but not these would come back on the code-derived key
        # while the peer still held this device's *old* public key — and a
        # channel that has agreed refuses everything but the handshake, so the
        # pairing would read as confirmed and sync nothing, in one direction,
        # with no error anywhere.
        "netpair_dh_key": cfg.netpair_dh_key,
        "netpair_peer_keys": {
            k: v
            for k, v in (getattr(cfg, "netpair_peer_keys", {}) or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        },
        # User-assigned per-peer aliases, so a restore keeps the friendly names
        # set on the device page (strictly local — never sent to the peer).
        "netpair_aliases": {
            k: v
            for k, v in (cfg.netpair_aliases or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        },
        "netpair_names": {
            k: v
            for k, v in (cfg.netpair_names or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        },
        # ---- security posture ----
        # The fields a restore that left them out would reset to their
        # defaults, two of which are the permissive ones (``file_open_to_all``,
        # ``chat_open_to_all``).  Restoring those defaults makes a restored
        # machine *more* open than the one the backup came from.
        "chat_open_to_all": cfg.chat_open_to_all,
        "file_open_to_all": cfg.file_open_to_all,
        "log_sharing": cfg.log_sharing,
        "encryption_enabled": cfg.encryption_enabled,
        "web_enabled": cfg.web_enabled,
        "web_port": cfg.web_port,
        "web_history_limit": cfg.web_history_limit,
        "translate_url": cfg.translate_url,
        # ---- what this machine decided about a peer ----
        # Without these a restore un-mutes conversations, un-pauses sync and
        # forgets that a device was removed on purpose.
        "chat_muted_peers": list(cfg.chat_muted_peers),
        "sync_paused_peers": list(cfg.sync_paused_peers),
        "removed_peers": list(cfg.removed_peers),
        "peers": peers,
        # ---- AI config watch list ----
        "ai_config_tools": list(cfg.ai_config_tools),
        "ai_config_custom_paths": list(cfg.ai_config_custom_paths),
        # ---- history and files ----
        "history_max_entries": cfg.history_max_entries,
        "history_max_age_days": cfg.history_max_age_days,
        "file_receive_dir": cfg.file_receive_dir,
        "sync_debounce": cfg.sync_debounce,
        "clipboard_poll_interval": cfg.clipboard_poll_interval,
        "max_reconnect_attempts": cfg.max_reconnect_attempts,
        "transfer_timeout": cfg.transfer_timeout,
        # ---- appearance and capture behaviour ----
        "log_level": cfg.log_level,
        "notifications_enabled": cfg.notifications_enabled,
        "appearance_mode": cfg.appearance_mode,
        "language": cfg.language,
        "language_chosen": cfg.language_chosen,
        "paste_to_top": cfg.paste_to_top,
        "low_memory_mode": cfg.low_memory_mode,
        "retry_capture_enabled": cfg.retry_capture_enabled,
        "dedup_method": cfg.dedup_method,
        "plain_text_only": cfg.plain_text_only,
        # ---- paste behaviour and recording rules (1.3.0) ----
        # Preferences like the rest: a restore that dropped them would silently
        # re-enable password-manager recording, re-record whatever was on the
        # clipboard at launch, and lift the size cap on a machine whose owner
        # had turned those protections on.
        "auto_paste_enabled": cfg.auto_paste_enabled,
        "skip_password_manager_clips": cfg.skip_password_manager_clips,
        "skip_prelaunch_clipboard": cfg.skip_prelaunch_clipboard,
        "max_non_file_paste_mb": cfg.max_non_file_paste_mb,
        "source_exclusion_patterns": list(cfg.source_exclusion_patterns),
        # ---- file transfer limits (1.3.1) ----
        # A restore that dropped these would quietly put a machine back at the
        # shipped ceilings after its owner had lowered one on purpose.
        "max_file_mb": cfg.max_file_mb,
        "confirm_large_file_mb": cfg.confirm_large_file_mb,
        "relay_paste_max_mb": cfg.relay_paste_max_mb,
        "app_filter_enabled": cfg.app_filter_enabled,
        "app_filter_mode": cfg.app_filter_mode,
        "app_filter_list": cfg.app_filter_list,
        "source_tracking_enabled": cfg.source_tracking_enabled,
        "ui_backend": cfg.ui_backend,
        "ui_animation_enabled": cfg.ui_animation_enabled,
        "sound_enabled": cfg.sound_enabled,
        "auto_update_check": cfg.auto_update_check,
        # ---- notifications ----
        # A restore that forgot these silently turned every notice back on.
        "notify_device_connect": cfg.notify_device_connect,
        "notify_pairing": cfg.notify_pairing,
        "notify_sync": cfg.notify_sync,
        "notify_transfer": cfg.notify_transfer,
        # ---- phone companion reachability ----
        # The token is a credential and stays behind; the switch and the shape
        # of the page are preferences.
        "companion_remote_enabled": cfg.companion_remote_enabled,
        "companion_remote_mode": cfg.companion_remote_mode,
        "companion_remote_page_url": cfg.companion_remote_page_url,
        "companion_remote_brokers": list(cfg.companion_remote_brokers),
        # ---- global hotkeys ----
        # Without them a backup→restore cycle silently reset every custom
        # shortcut to the defaults.  Only well-formed str→str pairs: json.dumps
        # would otherwise stringify non-string keys and bake junk in.
        "hotkeys": {
            k: v
            for k, v in (cfg.hotkeys or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        },
        "hotkeys_enabled": bool(getattr(cfg, "hotkeys_enabled", False)),
    }


# Fields of :class:`Config` a backup deliberately does not carry, each with the
# reason.  Listed rather than merely omitted so "not backed up" is a decision a
# reader can find, and so ``tests/test_backup_contents.py`` can hold the whole
# Config surface to the pair (exported / explained) instead of trusting that
# nobody forgot one.
BACKUP_SKIPPED: dict[str, str] = {
    # This machine's identity: a restore mints its own keys and device id
    # rather than impersonating the machine the archive came from.
    "device_id": "re-minted by the restoring device (see _NOT_RESTORED)",
    "private_key_pem": "device identity, re-minted rather than imported",
    "certificate_pem": "device identity, re-minted rather than imported",
    # Credentials and secrets of the restoring machine.
    "encryption_password": "never written to disk; the hash is machine-bound",
    "encryption_password_hash": "bound to the local fingerprint; re-set after restore",
    "web_token": "local web credential",
    # The flag travels with the token it describes.  A restored machine holding
    # ``web_token_disabled`` but no token would serve nothing and could not mint
    # one, so the companion would stay off with no way back short of the UI:
    # better to let the restore mint a fresh token and let the phone re-pair.
    "web_token_disabled": "meaningless without web_token; the restore mints a fresh one",
    "translate_api_key": "credential for a third-party service",
    "companion_remote_token": "local companion credential",
    "netpair_password": "pairing secret, pair again instead",
    # Live state a restore must not revive.
    "netpair_pending": "a pairing window that has long since closed",
    "timed_pause_until": "armed by a timer this path would not set",
    "config_version": "written by the loader for this file",
    "data_dir": "a path on the machine being restored",
    "favorites_path": "a path on the machine being restored",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _unique_backup_path(base: Path) -> Path:
    """Reserve a backup file name nothing else holds.

    The old name was the second-precision timestamp and ``os.replace`` at the
    end, so two backups in the same second silently became one: the second
    replaced the first, and the first was gone with no error anywhere.  The
    shared ``unique_export_path`` stamps microseconds, adds a random tail and
    creates the name with ``O_EXCL``, so a collision is refused by the
    filesystem rather than hoped against.
    """
    return unique_export_path(base, "clipsync_backup", ".zip")


def create_backup(
    cfg: Config,
    history: "ClipboardHistoryDB",
    backup_dir: str | None = None,
) -> str:
    """Create a timestamped backup zip file.

    The archive contains:
      - config.json      (full config export; see ``backup_config_dict``)
      - history.json     (full history export)
      - favorites.json   (exported from SQLite, or the legacy JSON file, or
                          ``[]`` when there are none)
      - manifest.json    (what the archive holds and how much of it)

    Returns the absolute path to the backup file.  Raises
    :class:`BackupIncompleteError` when a component could not be read: an archive
    missing a component must not be reported as a good backup.
    """
    from internal.data.export import export_history_json

    base = _get_backup_dir(backup_dir)
    zip_path = _unique_backup_path(base)

    try:
        with tempfile.TemporaryDirectory(prefix="clipsync_backup_") as tmp:
            tmpdir = Path(tmp)

            # --- config.json ---
            data = backup_config_dict(cfg)
            (tmpdir / "config.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # --- history.json ---
            hist_path = str(tmpdir / "history.json")
            export_history_json(history, hist_path)
            # A backup must never ship a truncated/unparseable history.json — an
            # interrupted or buggy export would otherwise produce an archive whose
            # history silently fails to restore.  Verify it round-trips before
            # packaging and abort the backup otherwise.
            try:
                hist_rows = json.loads(Path(hist_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Backup aborted: history export is not valid JSON: %s", exc)
                zip_path.unlink(missing_ok=True)
                raise

            # --- favorites.json ---
            # The SQLite store is the real one when it exists.  A store that cannot
            # be read aborts the backup (see ``_export_favorites_to_json``); only
            # "there is no database at all" falls back to the legacy JSON file, and
            # "neither exists" writes an explicit empty list rather than leaving the
            # member out -- a missing member is indistinguishable from a failed
            # write to whoever restores the archive.
            fav_json_path = tmpdir / "favorites.json"
            fav_count = _export_favorites_to_json(fav_json_path)
            if fav_count < 0:
                leg_path = _get_favorites_path()
                if leg_path.is_file():
                    import shutil

                    shutil.copy2(str(leg_path), str(fav_json_path))
                    try:
                        legacy = json.loads(fav_json_path.read_text(encoding="utf-8"))
                        fav_count = len(legacy) if isinstance(legacy, list) else 0
                    except (json.JSONDecodeError, OSError) as exc:
                        logger.error("Backup aborted: legacy favorites file is unreadable: %s", exc)
                        zip_path.unlink(missing_ok=True)
                        raise BackupIncompleteError("legacy favorites file is unreadable") from exc
                else:
                    fav_json_path.write_text("[]", encoding="utf-8")
                    fav_count = 0

            # --- manifest.json ---
            # What the archive holds, so a restore can say "this backup has 0
            # favorites" instead of guessing from a missing member, and so a
            # truncated archive is recognisable before it is restored over live data.
            (tmpdir / "manifest.json").write_text(
                json.dumps(
                    {
                        "format": 1,
                        "created": datetime.now(UTC).isoformat(timespec="seconds"),
                        "app_version": cfg.version if hasattr(cfg, "version") else "",
                        "components": {
                            "config": 1,
                            "history": len(hist_rows) if isinstance(hist_rows, list) else 0,
                            "favorites": fav_count,
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            # --- Create zip ---
            # Write to <name>.part then rename, so an interrupted backup never
            # leaves a truncated zip at the final path that list_backups would
            # present as a real, restorable backup.
            zip_part = zip_path.with_name(zip_path.name + ".part")
            try:
                with zipfile.ZipFile(str(zip_part), "w", zipfile.ZIP_DEFLATED) as zf:
                    for child in tmpdir.iterdir():
                        zf.write(str(child), arcname=child.name)
                os.replace(zip_part, zip_path)
            except Exception:
                # The outer handler removes the reserved name; this one only has
                # the half-written part to clean up.
                with contextlib.suppress(OSError):
                    zip_part.unlink(missing_ok=True)
                raise
    except BaseException:
        # The name was reserved before anything was written.  Any failure
        # between here and the rename leaves an empty zip under a name that
        # `list_backups` would present as a real, restorable backup.
        with contextlib.suppress(OSError):
            zip_path.unlink(missing_ok=True)
        raise

    # The archive contains plaintext clipboard history and config data; keep
    # it private (not world-readable).  A failure here is worth a line in the
    # log: on POSIX it means the archive is readable by every user on the
    # machine, and silence would leave the user believing otherwise.  It is not
    # fatal, because Windows has no equivalent of the mode and a backup that
    # exists with loose permissions still beats no backup at all.
    try:
        os.chmod(str(zip_path), 0o600)
    except OSError as exc:
        logger.warning("Backup created but could not be made owner-only: %s", exc)

    logger.info("Backup created: %s", zip_path)
    return str(zip_path)


def restore_backup(
    backup_path: str,
    cfg: Config,
    history: "ClipboardHistoryDB",
) -> dict:
    """Restore from a backup zip file.

    Returns a summary dict: {"config": bool, "history": int, "favorites": int}.
    The 'config' field is True if config.json was found in the backup.
    """
    from internal.data.export import import_history_json

    result: dict = {"config": False, "history": 0, "favorites": 0}

    zip_path = Path(backup_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    if not zipfile.is_zipfile(str(zip_path)):
        raise ValueError(f"Not a valid zip file: {backup_path}")

    def _assert_safe_zip_member(name: str) -> None:
        """Reject zip members that would escape the extraction dir.

        Defense-in-depth against zip-slip: the backup is created by our own
        create_backup, but a restored archive could be hand-crafted.  Reject
        absolute paths, drive letters and any ``..`` path component.
        """
        norm = name.replace("\\", "/")
        if norm.startswith("/") or norm.startswith("\\"):
            raise ValueError(f"Unsafe absolute path in backup: {name!r}")
        # Drive letter (Windows): e.g. "C:/..."
        if len(norm) >= 2 and norm[1] == ":":
            raise ValueError(f"Unsafe drive path in backup: {name!r}")
        if ".." in [part for part in norm.split("/")]:
            raise ValueError(f"Unsafe '..' path in backup: {name!r}")

    with tempfile.TemporaryDirectory(prefix="clipsync_restore_") as tmp:
        tmpdir = Path(tmp)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            for info in zf.infolist():
                _assert_safe_zip_member(info.filename)
            zf.extractall(str(tmpdir))

        # --- manifest (optional: archives from older builds do not have one) ---
        manifest: dict = {}
        manifest_file = tmpdir / "manifest.json"
        if manifest_file.is_file():
            try:
                parsed = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest = parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, OSError):
                logger.warning("Backup manifest is unreadable; restoring without it")

        # --- Restore config ---
        config_file = tmpdir / "config.json"
        if config_file.is_file():
            try:
                raw = json.loads(config_file.read_text(encoding="utf-8"))
                _apply_config(raw, cfg)
                result["config"] = True
                logger.info("Restored config from backup")
            except Exception as exc:
                logger.warning("Failed to restore config: %s", exc)
                result.setdefault("errors", []).append("config")

        # --- Restore history ---
        history_file = tmpdir / "history.json"
        if history_file.is_file():
            try:
                count = import_history_json(str(history_file), history)
                result["history"] = count
                logger.info("Restored %d history entries from backup", count)
            except Exception as exc:
                logger.warning("Failed to restore history: %s", exc)
                result.setdefault("errors", []).append("history")

        # --- Restore favorites ---
        fav_file = tmpdir / "favorites.json"
        if fav_file.is_file():
            try:
                favorites_data = json.loads(fav_file.read_text(encoding="utf-8"))
                if not isinstance(favorites_data, list) or any(
                    not isinstance(item, dict) for item in favorites_data
                ):
                    raise ValueError("Invalid favorites backup structure")
                imported = _import_favorites_from_json(fav_file)
                if imported != len(favorites_data):
                    raise ValueError("Favorites import incomplete")
                # The manifest is what makes "this archive held 12 favourites
                # and restored 12" checkable; a mismatch means the archive was
                # truncated or edited, which the caller must hear about.
                declared = (manifest.get("components") or {}).get("favorites")
                if isinstance(declared, int) and declared != len(favorites_data):
                    raise ValueError(
                        f"Backup declares {declared} favorites but holds {len(favorites_data)}"
                    )
                result["favorites"] = imported
                logger.info("Restored %d favorites from backup", imported)
            except Exception as exc:
                logger.warning("Failed to restore favorites: %s", exc)
                result.setdefault("errors", []).append("favorites")
        else:
            declared = (manifest.get("components") or {}).get("favorites")
            if isinstance(declared, int) and declared > 0:
                # The archive says it carried favourites and has no member for
                # them: report the loss rather than restoring "nothing".
                logger.warning(
                    "Backup declares %d favorites but carries no favorites.json", declared
                )
                result.setdefault("errors", []).append("favorites")

    return result


def list_backups(backup_dir: str | None = None) -> list[dict]:
    """List available backups.

    Returns a list of dicts: {"filename": str, "size": int, "date": str}
    Sorted by date, newest first.
    """
    base = _get_backup_dir(backup_dir)
    backups: list[dict] = []
    if not base.is_dir():
        return backups

    for f in base.iterdir():
        if not f.is_file() or not zipfile.is_zipfile(str(f)):
            continue
        try:
            st = f.stat()
            dt = datetime.fromtimestamp(st.st_mtime)
            backups.append(
                {
                    "filename": f.name,
                    # Absolute path so the frontend can pass it straight back to
                    # restore_backup() without relying on the process CWD.
                    "path": str(f),
                    "size": st.st_size,
                    "date": dt.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except OSError:
            continue

    backups.sort(key=lambda b: b["date"], reverse=True)
    return backups


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Value sets for enum-constrained string fields.  Field names and defaults
# mirror the Config dataclass in internal/config/config.py — keep them in
# sync when the Config schema changes.
_APPEARANCE_MODES = {"system", "light", "dark", "sepia", "midnight", "contrast"}
_LANGUAGES = {"en", "zh-CN", "zh-TW"}
_DEDUP_METHODS = {"sha256", "simple"}
_APP_FILTER_MODES = {"blacklist", "whitelist"}
_UI_BACKENDS = {"ctk", "webview"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Per-field validation rules applied during restore.  A backup's config.json is
# untrusted input, so every field is type- and range-checked before it is written
# onto the live Config.
#
# The rule for each field is *derived* from ``config.FIELD_RULES``, which is the
# single table that says what a field is and what it may hold.  This file used to
# keep a second, hand-written copy, and the two had drifted: it accepted
# ``web_history_limit`` up to 100000 and ``max_reconnect_attempts`` up to 1000,
# so a restored backup passed the restore and was then silently clamped to 500
# and 100 by the next ``config.load()`` -- the restore reported success while
# changing the value.  Derived, a bound cannot be true here and false there.
#
#   int / float with bounds  the numeric fields, clamped to their inclusive range
#   bool / str               as in FIELD_RULES
#   strlist[_nonnull]        list of strings; null allowed only for "strlist"
#   strdict                  str -> str mappings (hotkey bindings and friends)
#   enum                     the closed sets a field chooses between, which
#                            FIELD_RULES cannot express because it only checks
#                            the type: a hand-edited backup must not install
#                            ``language: "klingon"``.
_BACKUP_KIND = {
    "int": "int",
    "float": "float",
    "bool": "bool",
    "str": "str",
    "strlist": "strlist",
    "strlist_nonnull": "strlist_nonnull",
    "strdict": "strdict",
    # Hotkey bindings are a str -> str map on restore too; FIELD_RULES calls the
    # shape "hotkeys" to say what it is for, not what it must be.
    "hotkeys": "strdict",
    # A service type is a string; the config loader re-validates it on the next
    # start (see ``valid_service_type``).  The restore path only has to know the
    # rule shape ("service_type" was added to FIELD_RULES for the loader/settings
    # surfaces; treating it as a plain string here keeps that change from being
    # an unmapped-kind error at import time).
    "service_type": "str",
}

# The fields whose value must come from a closed set, and that set.
_ENUM_RULES: dict[str, tuple] = {
    "log_level": ("enum", _LOG_LEVELS, True),
    "appearance_mode": ("enum", _APPEARANCE_MODES),
    "language": ("enum", _LANGUAGES),
    "dedup_method": ("enum", _DEDUP_METHODS),
    "app_filter_mode": ("enum", _APP_FILTER_MODES),
    "ui_backend": ("enum", _UI_BACKENDS),
}

# Fields a backup carries but a restore deliberately does not apply.  Each is a
# secret or a live handle rather than a preference: ``private_key_pem`` and
# ``certificate_pem`` are the device identity a restore must not overwrite (the
# local device keeps its own), ``web_token`` and ``translate_api_key`` are
# credentials, and ``timed_pause_until`` is armed by a timer this path would not
# set.  Listed rather than merely omitted, so "not restored" is a decision a
# reader can find.
_NOT_RESTORED = frozenset(
    {
        # Identity: pinned to this machine, re-minted rather than imported.
        "device_id", "private_key_pem", "certificate_pem", "encryption_password_hash",
        # Credentials.
        "web_token", "translate_api_key",
        # A live pairing wait is transient state, not a preference: restoring an
        # old code would revive a secret whose ten-minute window closed long ago
        # and make the pairing card claim a wait that nobody can answer.  The
        # live config keeps its own pending table untouched by a restore.
        "netpair_pending",
        # Live handles and machine state.
        "timed_pause_until",
    }
)

_APPLY_SCHEMA: dict[str, tuple] = {}
for _name, _rule in FIELD_RULES.items():
    if _name in _NOT_RESTORED:
        continue
    _kind = _ENUM_RULES.get(_name) or (_BACKUP_KIND.get(_rule[0]),)
    if _kind[0] is None:
        # A rule shape this path does not restore.  Loud rather than silent: an
        # unmapped kind means a field was added upstream and not considered here.
        raise RuntimeError(f"backup: no restore rule for {_name!r} ({_rule[0]!r})")
    if len(_rule) == 3:
        _kind = (_kind[0], _rule[1], _rule[2])
    _APPLY_SCHEMA[_name] = _kind
del _name, _rule, _kind

# "peers" is handled separately (structured list-of-dicts, merged into
# cfg.peers) rather than through the scalar setattr path above.
_PEER_SCHEMA_KEY = "peers"

# Sentinel returned by _validate_config_value when a field must be skipped.
_SKIP = object()


def _validate_config_value(value: object, rule: tuple):
    """Validate/coerce one backup config value against a schema rule.

    Returns the value to apply, or ``_SKIP`` when the value is invalid and the
    field must be left untouched.  Never raises.
    """
    kind = rule[0]
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return _SKIP
        lo, hi = rule[1], rule[2]
        return max(lo, min(hi, value))
    if kind == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _SKIP
        coerced = float(value)
        # Bounds are optional, the same as the "int" rule's: a field whose
        # Config range is finite carries them so a restore cannot persist a
        # value the loader would clamp back on the next start.
        if len(rule) >= 3:
            lo, hi = rule[1], rule[2]
            return max(lo, min(hi, coerced))
        return coerced
    if kind == "bool":
        return value if isinstance(value, bool) else _SKIP
    if kind == "str":
        return value if isinstance(value, str) else _SKIP
    if kind == "enum":
        if not isinstance(value, str):
            return _SKIP
        if len(rule) > 2 and rule[2]:  # normalize (e.g. log_level -> upper)
            value = value.strip().upper()
        return value if value in rule[1] else _SKIP
    if kind == "strlist":
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            return _SKIP
        return value
    if kind == "strlist_nonnull":
        # Like "strlist" but a null is NOT valid — it falls through to the
        # field's list default instead of setting cfg.relay_brokers = None
        # (which would crash the relay startup's list() call on next start).
        if value is None:
            return _SKIP
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            return _SKIP
        return value
    if kind == "strdict":
        # Hotkey bindings (and other str→str maps): keep only well-formed
        # id → shortcut pairs so a hand-edited backup can never put a
        # non-string into the reload path.  The host would refuse a malformed
        # binding at registration time anyway, but the Config should stay
        # clean too.  If a non-empty map validates to nothing, skip it
        # entirely rather than replace the live bindings with {} from invalid
        # input.
        if not isinstance(value, dict):
            return _SKIP
        cleaned = {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
        if value and not cleaned:
            return _SKIP
        return cleaned
    return _SKIP


def _validate_peer_entries(value: object) -> list[dict] | None:
    """Structurally validate the peers list from a backup.

    Returns a list of clean peer dicts, or None when *value* is not a list at
    all.  Individual malformed entries are dropped; ``public_key_pem`` is
    deliberately NOT carried over — pinned keys are re-exchanged on reconnect,
    and importing a stale pin would only produce cert-mismatch lockouts.
    """
    if not isinstance(value, list):
        return None
    peers: list[dict] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            logger.warning("Backup restore skipped non-object peer entry")
            continue
        device_id = entry.get("device_id")
        device_name = entry.get("device_name")
        if not isinstance(device_id, str) or not device_id:
            logger.warning("Backup restore skipped peer with invalid device_id")
            continue
        if not isinstance(device_name, str):
            logger.warning(
                "Backup restore skipped peer %s with invalid device_name",
                device_id,
            )
            continue
        paired = entry.get("paired", False)
        if not isinstance(paired, bool):
            paired = False
        notes = entry.get("notes", "")
        if not isinstance(notes, str):
            notes = ""
        if device_id in seen:
            continue
        seen.add(device_id)
        peers.append(
            {
                "device_id": device_id,
                "device_name": device_name,
                "paired": paired,
                "notes": notes,
            }
        )
    return peers


def _apply_config(data: dict, cfg: Config) -> None:
    """Apply validated config fields from backup data.

    Every field in *data* is type- and range-checked against ``_APPLY_SCHEMA``
    before being written onto *cfg*.  The ``peers`` list is structurally
    validated and merged into ``cfg.peers`` (existing entries are refreshed,
    new ones added).  Unknown fields and fields whose value has the wrong type
    are skipped with a warning; numeric fields that are out of range are
    clamped to the nearest boundary.  A malformed backup can therefore never
    put the Config into an unusable state or crash a transport server on the
    next start.
    """
    for key, value in data.items():
        if key == _PEER_SCHEMA_KEY:
            peers = _validate_peer_entries(value)
            if peers is None:
                logger.warning(
                    "Backup restore skipped invalid peers payload (%s)",
                    type(value).__name__,
                )
                continue
            # Under the same lock the runtime's peer writer takes: a restore can
            # run while the transport is publishing peer state, and this is the
            # one place that adds to ``cfg.peers`` without holding it.
            from internal.config.config import config_lock
            with config_lock:
                for p in peers:
                    existing = cfg.peers.get(p["device_id"])
                    if existing is not None:
                        existing.device_name = p["device_name"]
                        existing.paired = p["paired"]
                        # Keep a user-assigned note unless the backup carries one.
                        if p["notes"]:
                            existing.notes = p["notes"]
                    else:
                        cfg.peers[p["device_id"]] = PeerInfo(**p)
            continue
        if key not in _APPLY_SCHEMA or not hasattr(cfg, key):
            continue
        validated = _validate_config_value(value, _APPLY_SCHEMA[key])
        if validated is _SKIP:
            logger.warning(
                "Backup restore skipped invalid config field '%s' (%s)",
                key,
                type(value).__name__,
            )
            continue
        setattr(cfg, key, validated)
