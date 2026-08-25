"""Configuration management.

Config is stored as JSON in the user's config directory:
  Windows: %APPDATA%/ClipSync/config.json
  macOS:   ~/Library/Application Support/ClipSync/config.json
"""

import json
import logging
import os
import platform
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from internal.security.encryption import EncryptionManager

logger = logging.getLogger(__name__)

# Guards the shared Config instance. save() and load() hold it, and callers
# that mutate Config fields across threads (e.g. the coordinator) should hold
# it around multi-field mutations so concurrent dict iteration / torn reads
# cannot occur. RLock so save()/load() may be called from within the lock.
config_lock = threading.RLock()


@dataclass
class PeerInfo:
    device_id: str
    device_name: str
    public_key_pem: str = ""  # pinned after pairing
    paired: bool = False
    notes: str = ""  # user-assigned alias or memo
    last_ip: str = ""  # last known address, so a paired peer can be reached
    last_port: int = 0  # even when it is momentarily off mDNS / across restarts


@dataclass
class Config:
    # Schema version for one-way migrations on load. v1 configs stored
    # filter_enabled_categories=[] to mean "all enabled"; v2 uses None=all.
    config_version: int = 2
    device_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    device_name: str = field(default_factory=platform.node)
    port: int = 19990
    service_type: str = "_clipsync._tcp.local."
    peers: dict[str, PeerInfo] = field(default_factory=dict)
    sync_enabled: bool = True
    # Wall-clock deadline (time.time()) of a timed "pause for N minutes";
    # 0 = none.  The auto-resume timer itself is runtime-only state, so the
    # deadline is persisted: a restart / auto-update relaunch inside the
    # pause window re-arms it instead of leaving sync_enabled=False stuck
    # on forever with nothing left to re-enable it.
    timed_pause_until: float = 0.0
    auto_start: bool = False
    # None = not configured → all redaction categories enabled (default ON).
    # [] = user explicitly disabled redaction. Non-empty = that subset.
    filter_enabled_categories: list[str] | None = None
    relay_url: str = ""
    private_key_pem: str = ""
    certificate_pem: str = ""
    # Advanced settings
    history_max_entries: int = 50
    # Age-based cleanup for unpinned history entries, in days (dual limit
    # together with history_max_entries).  0 disables age-based cleanup.
    history_max_age_days: float = 0.0
    file_receive_dir: str = ""
    sync_debounce: float = 0.3
    clipboard_poll_interval: float = 1.0
    max_reconnect_attempts: int = 10
    transfer_timeout: float = 120.0
    log_level: str = "INFO"
    notifications_enabled: bool = True
    # Per-type notification toggles (each defaults ON; the master
    # notifications_enabled switch above still gates all of them).
    notify_device_connect: bool = True
    notify_transfer: bool = True
    notify_pairing: bool = True
    notify_sync: bool = True
    # Per-peer chat message mutes (peer_ids whose incoming messages must not
    # produce a desktop notification / sound).  Managed from the web chat UI.
    chat_muted_peers: list[str] = field(default_factory=list)
    # Security
    encryption_enabled: bool = True
    encryption_password: str = ""       # runtime only — never persisted
    encryption_password_hash: str = ""  # persisted verification token
    # UI preferences
    appearance_mode: str = "system"     # "system", "light", "dark"
    language: str = "zh-CN"             # locale code: "en", "zh-CN" (default Chinese)
    language_chosen: bool = False       # True once the user picked a language (first-run onboarding)
    # Clipboard behavior
    paste_to_top: bool = True           # move pasted item to top
    low_memory_mode: bool = False       # reduce polling frequency / disable previews
    retry_capture_enabled: bool = True  # multi-round retry capture
    dedup_method: str = "sha256"        # "sha256" or "simple"
    # Strip rich-text formats (HTML/RTF) from every local clipboard write —
    # remote receives and pastes land as plain text only.  Images and file
    # lists are content, not formatting, and are kept.
    plain_text_only: bool = False

    # Periodic silent update check against GitHub releases (~every 6h).  OFF
    # means the background loop never contacts the network for update checks;
    # manual "check for updates" stays available either way.
    auto_update_check: bool = True

    # App filter (blacklist/whitelist apps from clipboard monitoring)
    app_filter_enabled: bool = False
    app_filter_mode: str = "blacklist"  # "blacklist" or "whitelist"
    app_filter_list: list[str] = field(default_factory=list)  # list of process names

    # Source tracking
    source_tracking_enabled: bool = True  # track which app produced clipboard content

    # UI preferences
    ui_backend: str = "webview"        # "webview" or "ctk"
    ui_animation_enabled: bool = True
    # "通知提示音" — the user-facing master notification switch.  On some
    # platforms a notification always plays a sound, so OFF means no
    # notifications at all, not just silence.  Default ON so fresh installs
    # keep getting notifications (see Application._notify).
    sound_enabled: bool = True

    # Data management
    favorites_path: str = ""           # empty = default location
    data_dir: str = ""                 # custom data directory (empty = default)

    # Web companion
    web_enabled: bool = False
    web_port: int = 19991
    web_token: str = ""
    web_history_limit: int = 30

    # Translation (LibreTranslate-compatible endpoint). Empty url = the
    # public LibreTranslate instance; when both url and key are empty the
    # translate endpoint falls back to a free anonymous service.
    translate_url: str = ""
    translate_api_key: str = ""   # never exposed to web clients

    # Hotkeys
    hotkeys: dict[str, str] = field(default_factory=lambda: {
        "quick_paste": "Ctrl+`",
        "paste_1": "Ctrl+1",
        "paste_2": "Ctrl+2",
        "paste_3": "Ctrl+3",
        "paste_4": "Ctrl+4",
        "paste_5": "Ctrl+5",
        "paste_6": "Ctrl+6",
        "paste_7": "Ctrl+7",
        "paste_8": "Ctrl+8",
        "paste_9": "Ctrl+9",
        "paste_plain": "Ctrl+Shift+V",
        "toggle_monitor": "Ctrl+Shift+M",
        "show_window": "Ctrl+Shift+Space",
    })

    # Global hotkeys are off by default; the user can enable them in settings.
    hotkeys_enabled: bool = False

    # Internet (cross-network) sync over public MQTT-over-WebSocket relays
    # (see internal/transport/relay.py).  Zero-cost design: the editable
    # broker list defaults to free public services, payloads stay E2E-
    # encrypted, and the relay only ever sees ciphertext + an unguessable
    # topic derived from both devices' secrets.
    internet_sync_enabled: bool = False
    relay_brokers: list[str] = field(default_factory=lambda: [
        "wss://broker.emqx.io:8084/mqtt",
        "wss://broker.hivemq.com:8884/mqtt",
        "wss://test.mosquitto.org:8081/mqtt",
    ])
    # This device's own relay secret — generated lazily on first use and
    # never transmitted in the clear (enrollment rides the TLS LAN channel).
    relay_secret: str = ""
    # peer device_id → that peer's relay secret, learned via relay_enroll.
    peer_relay_secrets: dict[str, str] = field(default_factory=dict)
    # Internet pairing-code (Round 14): peer device_id → the shared secret that
    # was typed in from (or confirmed via) a pairing code.  Unlike
    # peer_relay_secrets, no prior LAN pairing is required — the code is the
    # bootstrap.
    netpair_secrets: dict[str, str] = field(default_factory=dict)

    # AI-config sync (Round 12): watch-list root directories whose AI tool
    # config files (CLAUDE.md, memory notes, skills/, .mcp.json, ...) are
    # advertised — as metadata only (rel path + sha256 + size + mtime) — to
    # paired peers.  "~" expands to the current user's home at collection
    # time.  Empty list = feature off for this device (nothing is collected
    # and no inventory is broadcast).
    ai_config_paths: list[str] = field(default_factory=list)

    def add_peer(self, peer: PeerInfo):
        self.peers[peer.device_id] = peer


def _config_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return Path(base) / "ClipSync"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ClipSync"
    else:
        return Path.home() / ".config" / "clipsync"


def _log_dir() -> Path:
    """Directory the application writes its rotating log file to.

    Deliberately distinct from ``_config_dir()``: on macOS and Linux logs live
    in the conventional log/data directory, not the config directory (which
    holds data files and secrets).
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return Path(base) / "ClipSync"
    elif system == "Darwin":
        return Path.home() / "Library" / "Logs" / "ClipSync"
    else:
        return Path.home() / ".local" / "share" / "clipsync"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _cleanup_stale_temps():
    """Remove stale .config_tmp_*.json files from a previous crashed save."""
    try:
        config_dir = _config_dir()
        if config_dir.exists():
            for f in config_dir.glob(".config_tmp_*.json"):
                try:
                    f.unlink()
                    logger.debug("Cleaned up stale temp config: %s", f.name)
                except OSError:
                    pass
    except Exception:
        pass


# Per-field type rules applied when loading config.json.  A hand-edited or
# partially corrupted file must degrade field-by-field (skip the bad value,
# keep the default) instead of resetting the whole identity because one value
# has the wrong type.  Rule shapes mirror backup.py's _APPLY_SCHEMA:
#   "str"      any string
#   "bool"     only a Python bool
#   "int"      Python int (bool rejected)
#   "float"    int or float (bool rejected), coerced to float
#   "strlist"  list of strings, or None (the filter_enabled_categories
#              sentinel meaning "all enabled")
#   "strdict"  dict mapping strings to plain strings (peer_relay_secrets)
#   "hotkeys"  dict mapping shortcut-id strings to shortcut strings
_FIELD_RULES: dict[str, tuple] = {
    "device_id": ("str",),
    "device_name": ("str",),
    "port": ("int",),
    "service_type": ("str",),
    "sync_enabled": ("bool",),
    "timed_pause_until": ("float",),
    "auto_start": ("bool",),
    "filter_enabled_categories": ("strlist",),
    "relay_url": ("str",),
    "private_key_pem": ("str",),
    "certificate_pem": ("str",),
    "history_max_entries": ("int",),
    "history_max_age_days": ("float",),
    "file_receive_dir": ("str",),
    "sync_debounce": ("float",),
    "clipboard_poll_interval": ("float",),
    "max_reconnect_attempts": ("int",),
    "transfer_timeout": ("float",),
    "log_level": ("str",),
    "notifications_enabled": ("bool",),
    "notify_device_connect": ("bool",),
    "notify_transfer": ("bool",),
    "notify_pairing": ("bool",),
    "notify_sync": ("bool",),
    "chat_muted_peers": ("strlist_nonnull",),
    "encryption_enabled": ("bool",),
    "encryption_password_hash": ("str",),
    "appearance_mode": ("str",),
    "language": ("str",),
    "language_chosen": ("bool",),
    "paste_to_top": ("bool",),
    "low_memory_mode": ("bool",),
    "retry_capture_enabled": ("bool",),
    "dedup_method": ("str",),
    "plain_text_only": ("bool",),
    "auto_update_check": ("bool",),
    "app_filter_enabled": ("bool",),
    "app_filter_mode": ("str",),
    "app_filter_list": ("strlist_nonnull",),
    "source_tracking_enabled": ("bool",),
    "ui_backend": ("str",),
    "ui_animation_enabled": ("bool",),
    "sound_enabled": ("bool",),
    "favorites_path": ("str",),
    "data_dir": ("str",),
    "web_enabled": ("bool",),
    "web_port": ("int",),
    "web_token": ("str",),
    "web_history_limit": ("int",),
    "translate_url": ("str",),
    "translate_api_key": ("str",),
    "hotkeys": ("hotkeys",),
    "hotkeys_enabled": ("bool",),
    "internet_sync_enabled": ("bool",),
    "relay_brokers": ("strlist_nonnull",),
    "relay_secret": ("str",),
    "peer_relay_secrets": ("strdict",),
    "netpair_secrets": ("strdict",),
    "ai_config_paths": ("strlist_nonnull",),
}

# Sentinel returned by _validate_field when a value must be skipped.
_SKIP_FIELD = object()


def _validate_field(key: str, value: object):
    """Return the validated value for *key*, or ``_SKIP_FIELD``.

    Never raises; an invalid value simply leaves the Config default in place.
    """
    rule = _FIELD_RULES.get(key)
    if rule is None:
        return value  # not in the schema — caller's explicit list governs
    kind = rule[0]
    if kind == "str":
        return value if isinstance(value, str) else _SKIP_FIELD
    if kind == "bool":
        return value if isinstance(value, bool) else _SKIP_FIELD
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return _SKIP_FIELD
        return value
    if kind == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _SKIP_FIELD
        return float(value)
    if kind == "strlist":
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            return _SKIP_FIELD
        return value
    if kind == "strlist_nonnull":
        # Like strlist but None is NOT a valid value — a null falls through to
        # the field's list default (e.g. app_filter_list → []).
        if value is None:
            return _SKIP_FIELD
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            return _SKIP_FIELD
        return value
    if kind == "hotkeys":
        if not isinstance(value, dict):
            return _SKIP_FIELD
        if not all(isinstance(k, str) and isinstance(v, str)
                   for k, v in value.items()):
            return _SKIP_FIELD
        return value
    if kind == "strdict":
        if not isinstance(value, dict):
            return _SKIP_FIELD
        if not all(isinstance(k, str) and isinstance(v, str)
                   for k, v in value.items()):
            return _SKIP_FIELD
        return value
    return _SKIP_FIELD


def _archive_corrupt_config(path: Path) -> None:
    """Preserve an unreadable config.json before degrading to defaults.

    A fresh Config has a brand-new device id / private key / empty pairing
    table, and the next save() overwrites the file — without this rename the
    old identity would be gone for good.  The archived copy gives the user a
    chance to recover it manually.
    """
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = path.with_name(f"{path.name}.corrupt-{stamp}")
        os.replace(path, archived)
        logger.warning("Unreadable config preserved as %s", archived.name)
    except OSError:
        logger.debug("Could not archive corrupt config", exc_info=True)


def load() -> Config:
    with config_lock:
        _cleanup_stale_temps()
        path = _config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                logger.warning("Failed to parse config, using defaults", exc_info=True)
                _archive_corrupt_config(path)
                return Config()
            if not isinstance(data, dict):
                logger.warning(
                    "Config root is %s instead of an object, using defaults",
                    type(data).__name__,
                )
                _archive_corrupt_config(path)
                return Config()
            cfg = Config()
            for key in (
                "device_id", "device_name", "port", "service_type",
                "sync_enabled", "timed_pause_until", "auto_start",
                "filter_enabled_categories",
                "relay_url",
                "private_key_pem", "certificate_pem",
                "history_max_entries", "history_max_age_days",
                "file_receive_dir",
                "sync_debounce", "clipboard_poll_interval",
                "max_reconnect_attempts", "transfer_timeout",
                "log_level", "notifications_enabled",
                "notify_device_connect", "notify_transfer",
                "notify_pairing", "notify_sync",
                "chat_muted_peers",
                "encryption_enabled",
                "encryption_password_hash",
                "appearance_mode",
                "language",
                "language_chosen",
                "paste_to_top", "low_memory_mode", "retry_capture_enabled",
                "dedup_method", "plain_text_only", "auto_update_check",
                "app_filter_enabled", "app_filter_mode",
                "app_filter_list", "source_tracking_enabled",
                "ui_backend", "ui_animation_enabled", "sound_enabled",
                "favorites_path", "data_dir",
                "web_enabled", "web_port",
                "web_token", "web_history_limit",
                "translate_url", "translate_api_key",
                "hotkeys", "hotkeys_enabled",
                "internet_sync_enabled", "relay_brokers",
                "relay_secret", "peer_relay_secrets",
                "netpair_secrets",
                "ai_config_paths",
            ):
                if key in data:
                    value = _validate_field(key, data[key])
                    if value is _SKIP_FIELD:
                        logger.warning(
                            "Config field '%s' has invalid type %s — keeping default",
                            key, type(data[key]).__name__,
                        )
                        continue
                    setattr(cfg, key, value)
            # Migrate from old plaintext password (now stored on next save as hash)
            if "encryption_password" in data and data["encryption_password"]:
                cfg.encryption_password = data["encryption_password"]
            # Migrate from old filter_sensitive bool
            if "filter_sensitive" in data and not data.get("filter_enabled_categories"):
                if data["filter_sensitive"]:
                    cfg.filter_enabled_categories = ["credit_card", "ssn", "api_key", "private_key", "password"]
            # Config v1 stored filter_enabled_categories=[] to mean "all
            # categories enabled"; v2 distinguishes None=all from []=disabled.
            # Preserve the old default for existing configs by upgrading []→None
            # (so redaction stays ON), while a v2 save of [] remains a real
            # "disable everything" choice.
            if data.get("config_version", 1) < 2 and cfg.filter_enabled_categories == []:
                cfg.filter_enabled_categories = None
            # Migrate from legacy dict-format peers (pre-list) to list format:
            #   {"device_id": {device_name, public_key_pem, paired, notes}, ...}
            peers_data = data.get("peers", [])
            if isinstance(peers_data, dict):
                peers_data = [
                    {"device_id": pid, **pinfo}
                    for pid, pinfo in peers_data.items()
                    if isinstance(pinfo, dict)
                ]
            if not isinstance(peers_data, list):
                logger.warning(
                    "Config 'peers' has invalid type %s — ignoring peers",
                    type(peers_data).__name__,
                )
                peers_data = []
            for peer_data in peers_data:
                if not isinstance(peer_data, dict):
                    continue
                device_id = peer_data.get("device_id")
                device_name = peer_data.get("device_name")
                if not isinstance(device_id, str) or not device_id:
                    logger.warning("Skipping peer with invalid device_id: %r",
                                   device_id)
                    continue
                if not isinstance(device_name, str):
                    logger.warning("Skipping peer %s with invalid device_name",
                                   device_id)
                    continue
                public_key_pem = peer_data.get("public_key_pem", "")
                paired = peer_data.get("paired", False)
                notes = peer_data.get("notes", "")
                last_ip = peer_data.get("last_ip", "")
                last_port = peer_data.get("last_port", 0)
                if not isinstance(public_key_pem, str):
                    public_key_pem = ""
                if not isinstance(paired, bool):
                    paired = False
                if not isinstance(notes, str):
                    notes = ""
                if not isinstance(last_ip, str):
                    last_ip = ""
                if not isinstance(last_port, int) or isinstance(last_port, bool):
                    last_port = 0
                cfg.peers[device_id] = PeerInfo(
                    device_id=device_id,
                    device_name=device_name,
                    public_key_pem=public_key_pem,
                    paired=paired,
                    notes=notes,
                    last_ip=last_ip,
                    last_port=last_port,
                )
            return cfg
        return Config()


def save(cfg: Config, enc_mgr: "EncryptionManager | None" = None):
    with config_lock:
        cfg.config_version = 2
        config_dir = _config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = _config_path()

        # Encrypt private key before writing to disk if encryption is enabled
        private_key_to_save = cfg.private_key_pem
        if cfg.encryption_enabled and enc_mgr and cfg.private_key_pem:
            private_key_to_save = enc_mgr.encrypt_storage(cfg.private_key_pem)
            logger.debug("Config save: private_key_pem encrypted for at-rest storage")

        data = {
            "config_version": cfg.config_version,
            "device_id": cfg.device_id,
            "device_name": cfg.device_name,
            "port": cfg.port,
            "service_type": cfg.service_type,
            "sync_enabled": cfg.sync_enabled,
            "timed_pause_until": cfg.timed_pause_until,
            "auto_start": cfg.auto_start,
            "filter_enabled_categories": cfg.filter_enabled_categories,
            "relay_url": cfg.relay_url,
            "private_key_pem": private_key_to_save,
            "certificate_pem": cfg.certificate_pem,
            "history_max_entries": cfg.history_max_entries,
            "history_max_age_days": cfg.history_max_age_days,
            "file_receive_dir": cfg.file_receive_dir,
            "sync_debounce": cfg.sync_debounce,
            "clipboard_poll_interval": cfg.clipboard_poll_interval,
            "max_reconnect_attempts": cfg.max_reconnect_attempts,
            "transfer_timeout": cfg.transfer_timeout,
            "log_level": cfg.log_level,
            "notifications_enabled": cfg.notifications_enabled,
            "notify_device_connect": cfg.notify_device_connect,
            "notify_transfer": cfg.notify_transfer,
            "notify_pairing": cfg.notify_pairing,
            "notify_sync": cfg.notify_sync,
            "chat_muted_peers": cfg.chat_muted_peers,
            "encryption_enabled": cfg.encryption_enabled,
            "encryption_password_hash": cfg.encryption_password_hash,
            "appearance_mode": cfg.appearance_mode,
            "language": cfg.language,
            "language_chosen": cfg.language_chosen,
            "paste_to_top": cfg.paste_to_top,
            "low_memory_mode": cfg.low_memory_mode,
            "retry_capture_enabled": cfg.retry_capture_enabled,
            "dedup_method": cfg.dedup_method,
            "plain_text_only": cfg.plain_text_only,
            "auto_update_check": cfg.auto_update_check,
            "app_filter_enabled": cfg.app_filter_enabled,
            "app_filter_mode": cfg.app_filter_mode,
            "app_filter_list": cfg.app_filter_list,
            "source_tracking_enabled": cfg.source_tracking_enabled,
            "ui_backend": cfg.ui_backend,
            "ui_animation_enabled": cfg.ui_animation_enabled,
            "sound_enabled": cfg.sound_enabled,
            "favorites_path": cfg.favorites_path,
            "data_dir": cfg.data_dir,
            "web_enabled": cfg.web_enabled,
            "web_port": cfg.web_port,
            "web_token": cfg.web_token,
            "web_history_limit": cfg.web_history_limit,
            "translate_url": cfg.translate_url,
            "translate_api_key": cfg.translate_api_key,
            "hotkeys": cfg.hotkeys,
            "hotkeys_enabled": cfg.hotkeys_enabled,
            "internet_sync_enabled": cfg.internet_sync_enabled,
            "relay_brokers": cfg.relay_brokers,
            "relay_secret": cfg.relay_secret,
            "peer_relay_secrets": cfg.peer_relay_secrets,
            "netpair_secrets": cfg.netpair_secrets,
            "ai_config_paths": cfg.ai_config_paths,
            "peers": [
                {
                    "device_id": p.device_id,
                    "device_name": p.device_name,
                    "public_key_pem": p.public_key_pem,
                    "paired": p.paired,
                    "notes": p.notes,
                    "last_ip": p.last_ip,
                    "last_port": p.last_port,
                }
                for p in cfg.peers.values()
            ],
        }
        # Atomic save: write to temp file then rename
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(config_dir), prefix=".config_tmp_", suffix=".json",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)  # atomic on same filesystem
            # mkstemp creates the temp file with 0600; os.replace keeps that
            # inode, so the final file is already private. Re-assert it for
            # filesystems where replace may reset perms (non-Windows guard).
            if os.name != "nt":
                try:
                    os.chmod(config_path, 0o600)
                except OSError:
                    pass
            logger.debug("Config saved to %s", config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
