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
    auto_start: bool = False
    # None = not configured → all redaction categories enabled (default ON).
    # [] = user explicitly disabled redaction. Non-empty = that subset.
    filter_enabled_categories: list[str] | None = None
    relay_url: str = ""
    private_key_pem: str = ""
    certificate_pem: str = ""
    # Advanced settings
    history_max_entries: int = 50
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

    # App filter (blacklist/whitelist apps from clipboard monitoring)
    app_filter_enabled: bool = False
    app_filter_mode: str = "blacklist"  # "blacklist" or "whitelist"
    app_filter_list: list[str] = field(default_factory=list)  # list of process names

    # Source tracking
    source_tracking_enabled: bool = True  # track which app produced clipboard content

    # UI preferences
    ui_backend: str = "webview"        # "webview" or "ctk"
    ui_animation_enabled: bool = True
    sound_enabled: bool = False

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


def load() -> Config:
    with config_lock:
        _cleanup_stale_temps()
        path = _config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                logger.warning("Failed to parse config, using defaults", exc_info=True)
                return Config()
            cfg = Config()
            for key in (
                "device_id", "device_name", "port", "service_type",
                "sync_enabled", "auto_start",
                "filter_enabled_categories",
                "relay_url",
                "private_key_pem", "certificate_pem",
                "history_max_entries", "file_receive_dir",
                "sync_debounce", "clipboard_poll_interval",
                "max_reconnect_attempts", "transfer_timeout",
                "log_level", "notifications_enabled",
                "notify_device_connect", "notify_transfer",
                "notify_pairing", "notify_sync",
                "encryption_enabled",
                "encryption_password_hash",
                "appearance_mode",
                "language",
                "language_chosen",
                "paste_to_top", "low_memory_mode", "retry_capture_enabled",
                "dedup_method", "app_filter_enabled", "app_filter_mode",
                "app_filter_list", "source_tracking_enabled",
                "ui_backend", "ui_animation_enabled", "sound_enabled",
                "favorites_path", "data_dir",
                "web_enabled", "web_port",
                "web_token", "web_history_limit",
                "translate_url", "translate_api_key",
                "hotkeys", "hotkeys_enabled",
            ):
                if key in data:
                    setattr(cfg, key, data[key])
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
            for peer_data in peers_data:
                try:
                    peer = PeerInfo(
                        device_id=peer_data["device_id"],
                        device_name=peer_data["device_name"],
                        public_key_pem=peer_data.get("public_key_pem", ""),
                        paired=peer_data.get("paired", False),
                        notes=peer_data.get("notes", ""),
                    )
                    cfg.peers[peer.device_id] = peer
                except (KeyError, TypeError):
                    continue
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
            "device_id": cfg.device_id,
            "device_name": cfg.device_name,
            "port": cfg.port,
            "service_type": cfg.service_type,
            "sync_enabled": cfg.sync_enabled,
            "auto_start": cfg.auto_start,
            "filter_enabled_categories": cfg.filter_enabled_categories,
            "relay_url": cfg.relay_url,
            "private_key_pem": private_key_to_save,
            "certificate_pem": cfg.certificate_pem,
            "history_max_entries": cfg.history_max_entries,
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
            "encryption_enabled": cfg.encryption_enabled,
            "encryption_password_hash": cfg.encryption_password_hash,
            "appearance_mode": cfg.appearance_mode,
            "language": cfg.language,
            "language_chosen": cfg.language_chosen,
            "paste_to_top": cfg.paste_to_top,
            "low_memory_mode": cfg.low_memory_mode,
            "retry_capture_enabled": cfg.retry_capture_enabled,
            "dedup_method": cfg.dedup_method,
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
            "peers": [
                {
                    "device_id": p.device_id,
                    "device_name": p.device_name,
                    "public_key_pem": p.public_key_pem,
                    "paired": p.paired,
                    "notes": p.notes,
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
