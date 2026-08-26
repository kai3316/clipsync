#!/usr/bin/env python3
"""ClipSync — Cross-platform clipboard sharing.

Real-time clipboard sync between Windows, macOS, and Linux on the same local network.
Runs as a system tray application with an optional settings GUI.
"""

import atexit
import base64 as _b64
import logging
import os
import secrets
import shutil
import sys
import threading
import time
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog
from urllib.parse import quote, urlparse

# Add project root to Python path so 'internal' package can be found
# (not needed in a PyInstaller-frozen bundle)
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent
from internal.clipboard.format import ContentType as _CT
from internal.clipboard.history import ClipboardHistory
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.clipboard.platform import create_monitor, create_reader, create_writer
from internal.clipboard.source_tracker import is_app_allowed
from internal.config.config import Config, PeerInfo, _config_dir, config_lock, load, save
from internal.i18n import T, set_locale
from internal.platform.autostart import disable_autostart, enable_autostart, is_autostart_enabled
from internal.platform.notify import notification_mgr
from internal.protocol.codec import (
    AICONFIG_MSG_TYPES,
    CHAT_MSG_TYPES,
    DEVICE_PROBE_MSG_TYPES,
    FILE_TRANSFER_MSG_TYPES,
    PAIRING_MSG_TYPES,
    encode_frame,
    encode_message,
)
from internal.security.encryption import (
    EncryptionManager,
)
from internal.security.encryption import (
    make_password_hash as _make_password_hash,
)
from internal.security.encryption import (
    verify_password as _verify_password,
)
from internal.security.pairing import (
    PAIRING_STATUS_PAIRED,
    PAIRING_STATUS_PEER_CONFIRMED,
    CertificateChangedError,
    PairingManager,
)
from internal.security.pairing import (
    fingerprint_pem as _fingerprint_pem,
)
from internal.sync.ai_config import AIConfigManager
from internal.sync.file_transfer import FileTransferManager
from internal.sync.manager import SyncManager
from internal.sync.nearby_chat import ChatFileTooLarge, ChatManager
from internal.system.hotkey import HotkeyManager
from internal.transport.connection import MAX_FRAME_SIZE, PortInUseError, TransportManager
from internal.transport.discovery import Discovery
from internal.ui.dashboard import DashboardWindow
from internal.ui.dialogs import ask_string, ask_yesno, show_error, show_info, show_warning
from internal.ui.settings_window import SettingsWindow
from internal.ui.systray import SystrayApp
from internal.version import __version__
from internal.web.server import WebServer

logger = logging.getLogger(__name__)

_console_handler: logging.StreamHandler | None = None

# A netpair peer counts as "online" when a frame arrived within this window.
# Roughly a keepalive horizon — nothing is polled, so it is the only signal.
NETPAIR_ONLINE_WINDOW = 90.0

# Round 17: internet delivery ("已送达" receipt + offline retransmission).
ACK_WINDOW = 15.0              # how long a relayed clipboard send waits for ack
DELIVERY_SCAN_INTERVAL = 2.0   # background scan cadence for timed-out sends
QUEUE_RETRY_INTERVAL = 60.0    # periodic offline-queue retry cadence
MAX_QUEUE_RETRIES = 5          # per-message retry budget before marking failed
DELIVERY_LEDGER_MAX = 50       # in-memory send rows kept per peer (REST shows 20)
DELIVERY_QUEUE_MAX = 100       # persisted offline rows kept per peer (hard cap)
RELAY_PENDING_FILE = "relay_pending.json"

# Device-connectivity probe ("test connection" on a device card): how long we
# wait for a device_pong after sending device_ping on each available channel.
DEVICE_PING_TIMEOUT = 4.0


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level helpers (must be picklable for macOS multiprocessing)
# ═══════════════════════════════════════════════════════════════════════════════


def _mask_file_name(file_name: str) -> str:
    if not file_name or file_name == "?":
        return file_name
    ext = os.path.splitext(file_name)[1]
    return f"*{ext}" if ext else "*"


def _mask_path(path: str) -> str:
    parent = os.path.basename(os.path.dirname(path))
    return f"{parent}/***" if parent else "***"


def _get_log_dir() -> "Path":
    import platform as _p
    from pathlib import Path

    system = _p.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "ClipSync"
    elif system == "Darwin":
        return Path.home() / "Library" / "Logs" / "ClipSync"
    else:
        return Path.home() / ".local" / "share" / "clipsync"


def _get_log_path() -> "Path":
    return _get_log_dir() / "clipsync.log"


def _hide_dock():
    """Hide the app from the macOS Dock, keeping only the menu bar icon.

    Uses ``NSApplicationActivationPolicyAccessory`` (1) rather than
    ``NSApplicationActivationPolicyProhibited`` (2): Prohibited keeps the app
    out of the Dock but also prevents it from activating its own windows, which
    breaks CTk dialogs and the retrust prompt.  Accessory hides the Dock icon
    while still letting the app front its windows when it needs to.
    """
    if sys.platform != "darwin":
        return
    try:
        from rubicon.objc import ObjCClass

        NSApp = ObjCClass("NSApplication").sharedApplication()
        # NSApplicationActivationPolicyAccessory == 1
        NSApp.setActivationPolicy_(1)
        return
    except Exception:
        pass
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.util.find_library("objc")
        if not lib:
            return
        objc = ctypes.cdll.LoadLibrary(lib)
        objc.objc_getClass.argtypes = (ctypes.c_char_p,)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = (ctypes.c_char_p,)
        objc.sel_registerName.restype = ctypes.c_void_p

        cls = objc.objc_getClass(b"NSApplication")
        sel_shared = objc.sel_registerName(b"sharedApplication")
        sel_policy = objc.sel_registerName(b"setActivationPolicy:")

        proto0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        app = proto0(("objc_msgSend", objc))(cls, sel_shared)

        proto1 = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
        )
        # NSApplicationActivationPolicyAccessory == 1
        proto1(("objc_msgSend", objc))(app, sel_policy, 1)
    except Exception:
        pass


def _lock_file() -> "Path":
    return _config_dir() / ".lock"


def _read_lock() -> dict | None:
    """Read the lock file. Returns None if no lock or corrupted."""
    import json

    lf = _lock_file()
    if not lf.exists():
        return None
    try:
        return json.loads(lf.read_text())
    except Exception:
        return None


def _write_lock(main_pid: int, tray_pid: int | None = None):
    """Write the lock file with main and optional tray PID."""
    import json

    _config_dir().mkdir(parents=True, exist_ok=True)
    # Preserve an existing tray_pid (e.g. written by the macOS tray subprocess)
    # so a later main-process write doesn't overwrite it and orphan the tray.
    if tray_pid is None:
        try:
            existing = _read_lock()
            if existing:
                tray_pid = existing.get("tray_pid")
        except Exception:
            pass
    data: dict = {"pid": main_pid}
    if tray_pid is not None:
        data["tray_pid"] = tray_pid
    _lock_file().write_text(json.dumps(data))


def _remove_lock():
    try:
        lf = _lock_file()
        if not lf.exists():
            return
        data = _read_lock()
        # Only remove our own lock (or a dead one). After a factory reset the
        # replacement instance writes a fresh lock with a new PID; deleting it
        # here would let a second instance start later.
        pid = data.get("pid") if data else None
        if pid is None or pid == os.getpid() or not _pid_alive(pid):
            lf.unlink()
    except Exception:
        pass


def _detect_network_type() -> tuple[str, str]:
    """Best-effort primary network type + interface name.

    Returns (type, interface) where type is "wifi", "ethernet" or "lan".
    Uses the default-route interface so VPN/loopback don't fool it.
    """
    import subprocess
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True, timeout=3,
            ).stdout or ""
            iface = ""
            for line in out.splitlines():
                if line.strip().startswith("interface:"):
                    iface = line.split(":", 1)[1].strip()
                    break
            if not iface:
                return "lan", ""
            hp = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True, text=True, timeout=3,
            ).stdout or ""
            for block in hp.split("\n\n"):
                if f"Device: {iface}" in block:
                    if "Wi-Fi" in block or "AirPort" in block:
                        return "wifi", iface
                    return "ethernet", iface
            return "lan", iface
        if sys.platform.startswith("linux"):
            iface = ""
            try:
                with open("/proc/net/route") as f:
                    for line in f.readlines()[1:]:
                        parts = line.split()
                        if len(parts) >= 3 and parts[1] == "00000000":
                            iface = parts[0]
                            break
            except OSError:
                pass
            if iface and os.path.isdir(f"/sys/class/net/{iface}/wireless"):
                return "wifi", iface
            return ("ethernet", iface) if iface else ("lan", "")
    except Exception:
        pass
    return "lan", ""


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID belongs to a running ClipSync instance."""
    if sys.platform == "win32":
        # os.kill(pid, 0) is unreliable on Windows (signal 0 not supported,
        # raises OSError for unrelated reasons). Use Win32 API to check the
        # process image name so we don't get fooled by PID reuse.
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                0x0400 | 0x0010, False, pid,
            )  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
            if not handle:
                return False
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            ok = kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size),
            )
            kernel32.CloseHandle(handle)
            if not ok:
                return False
            name = buf.value.lower()
            # Frozen PyInstaller builds run as "clipsync.exe" (not "python.exe");
            # accept both so the single-instance guard works for shipped binaries.
            return "python" in name or "clipsync" in name
        except Exception:
            return False
    else:
        # Unix: signal 0 + verify cmdline references this app. Accept both a
        # source run ("python .../main.py") and a frozen run (the "clipsync"
        # binary), since the frozen executable name contains neither "python"
        # nor "main.py".
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return False
        if sys.platform == "darwin":
            # macOS has no /proc, so the cmdline check below is Linux-only.
            # Strengthen the plain signal-0 liveness check (which can't tell
            # a ClipSync process from an unrelated process reusing the PID)
            # by comparing the process image name via `ps`.  Any failure errs
            # on the safe side (True), exactly like the Linux fall-through.
            try:
                import subprocess
                out = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    capture_output=True, text=True, timeout=3,
                ).stdout or ""
                name = out.strip().lower()
                if not name:
                    return True  # can't verify, err on safe side
                return "python" in name or "clipsync" in name
            except Exception:
                return True  # can't verify, err on safe side
        try:
            from pathlib import Path
            cmdline = Path(f"/proc/{pid}/cmdline").read_text()
            return ("python" in cmdline and "clipsync" in cmdline) or "clipsync" in cmdline
        except Exception:
            return True  # can't verify, err on safe side


def _check_and_cleanup_stale_lock() -> bool:
    """Check for stale lock files from crashed instances.

    Kills orphaned tray processes and removes stale lock files.
    Returns True if startup should proceed, False if another instance is running.
    """
    lock = _read_lock()
    if lock is None:
        return True  # No lock file, proceed

    main_pid = lock.get("pid")
    tray_pid = lock.get("tray_pid")

    main_alive = main_pid is not None and _pid_alive(main_pid)
    tray_alive = tray_pid is not None and _pid_alive(tray_pid)

    if main_alive:
        # Another instance is actively running
        logger.warning("Another instance is already running (PID %d)", main_pid)
        return False

    # Main process is dead — kill orphaned tray if it exists
    if tray_alive:
        logger.info("Killing orphaned tray process (PID %d)", tray_pid)
        try:
            import signal
            os.kill(tray_pid, signal.SIGKILL)
        except Exception:
            pass  # SIGKILL not available on Windows, but tray subprocess is macOS-only

    # Clean up stale lock file
    _remove_lock()
    return True


def _run_tray(device_name: str, pipe, parent_pid: int, locale: str = "en"):
    """Run the system tray in a subprocess (macOS only). Must be module-level for multiprocessing."""
    Application.setup_logging()
    set_locale(locale)  # tray menu must follow the app language
    _hide_dock()

    # Write tray PID to lock file so it can be cleaned up on force quit
    _write_lock(parent_pid, tray_pid=os.getpid())
    child_systray = SystrayApp(
        device_name=device_name,
        on_enable_toggle=lambda v: pipe.send(("toggle_sync", v)),
        on_open_dashboard=lambda: pipe.send(("open_dashboard",)),
        on_open_settings=lambda: pipe.send(("open_settings",)),
        on_export_logs=lambda: pipe.send(("export_logs",)),
        on_show_web_qr=lambda: pipe.send(("show_web_qr",)),
        on_send_url=lambda: pipe.send(("send_url",)),
        on_check_update=lambda: pipe.send(("check_update",)),
        on_about=lambda: pipe.send(("about",)),
        on_pause_minutes=lambda m: pipe.send(("pause_minutes", m)),
        on_resume_sync=lambda: pipe.send(("resume_sync",)),
        on_quit=lambda: pipe.send(("quit",)),
        on_tray_failed=lambda: pipe.send(("tray_failed",)),
    )

    def _recv_notifications():
        while True:
            try:
                if pipe.poll(100):
                    msg = pipe.recv()
                    if not isinstance(msg, tuple) or not msg:
                        continue
                    _kind = msg[0]
                    if _kind == "show_notification" and child_systray._tray:
                        try:
                            child_systray._tray.notify(msg[2], title=msg[1])
                        except Exception:
                            pass
                    elif _kind == "set_peers":
                        # Parent pushes the live peer list so the child's
                        # menu stays in sync (the parent's own SystrayApp is
                        # dormant and cannot update the child's menu).
                        try:
                            child_systray.set_peers(list(msg[1] or []))
                        except Exception:
                            pass
                    elif _kind == "set_web_enabled":
                        try:
                            child_systray.set_web_enabled(bool(msg[1]))
                        except Exception:
                            pass
                    elif _kind == "set_syncing":
                        try:
                            child_systray.set_syncing(bool(msg[1]))
                        except Exception:
                            pass
                    elif _kind == "set_pause_deadline":
                        try:
                            deadline = msg[1]
                            child_systray.set_pause_deadline(
                                float(deadline) if deadline is not None else None,
                            )
                        except Exception:
                            pass
            except (EOFError, BrokenPipeError, OSError):
                break
            except Exception:
                pass

    def _parent_watchdog():
        """Monitor parent process; stop tray if parent dies (e.g. force quit)."""
        while True:
            if not _pid_alive(parent_pid):
                logger.info("Parent process %d died, stopping tray", parent_pid)
                if child_systray._tray:
                    child_systray._tray.stop()
                break
            time.sleep(3)

    threading.Thread(target=_recv_notifications, daemon=True).start()
    threading.Thread(target=_parent_watchdog, daemon=True).start()
    child_systray.run()


# ═══════════════════════════════════════════════════════════════════════════════
# Application class
# ═══════════════════════════════════════════════════════════════════════════════


class Application:
    """Central controller for ClipSync lifecycle.

    Lifecycle phases (called in order):
      1. setup_logging()   — static, configures root logger
      2. load_config()
      3. _bootstrap_crypto()
      4. _bootstrap_identity()
      5. _create_services()
      6. _wire_callbacks()
      7. _apply_config()
      8. _create_ui()
      9. _start_services()
     10. _start_threads()
     11. run()             — blocks on root.mainloop(); calls shutdown() on exit
    """

    def __init__(self) -> None:
        import time as _time
        self._start_time = int(_time.time())

        # ── Config ──────────────────────────────────────────────────
        self.cfg: Config | None = None

        # ── Services ────────────────────────────────────────────────
        self.content_filter: ContentFilter | None = None
        self.enc_mgr: EncryptionManager | None = None
        self.pairing_mgr: PairingManager | None = None
        self.clipboard_history: ClipboardHistory | None = None
        self.sync_mgr: SyncManager | None = None
        self.transport_mgr: TransportManager | None = None
        self.file_transfer_mgr: FileTransferManager | None = None
        self.aicfg_mgr: AIConfigManager | None = None  # Round 12: AI-config sync
        self.discovery: Discovery | None = None
        self.web_server: WebServer | None = None

        # ── Hotkey manager ─────────────────────────────────────────
        self.hotkey_mgr: HotkeyManager | None = None

        # ── UI ──────────────────────────────────────────────────────
        self.root: tk.Tk | None = None
        self.systray: SystrayApp | None = None
        self.settings_win: SettingsWindow | None = None
        self.webview_win = None  # WebViewWindow for webview mode
        self._webview_opened_at: float = 0.0  # monotonic time of last dashboard open
        self.dashboard_win: DashboardWindow | None = None

        # ── Threading ───────────────────────────────────────────────
        self._stop_updater = threading.Event()
        self._shutting_down = False
        # Set by factory reset so shutdown() doesn't re-save the deleted config.
        self._skip_save_on_shutdown = False
        # Timed sync pause (tray "Pause sync for N minutes"): the auto-resume
        # timer plus the wall-clock deadline mirrored into the tray menu.
        self._pause_timer: threading.Timer | None = None
        self._pause_deadline: float | None = None
        # Internet (cross-network) relay transport (internal/transport/
        # relay.py), created lazily by _start_internet_sync() when the
        # internet_sync_enabled setting is on.
        self._relay = None
        # Internet pairing code (Round 14): runtime-only state.  ``_netpair_pending``
        # maps a generated code -> its secret until the partner's hello confirms
        # it (confirmation is then persisted in cfg.netpair_secrets).  Names are
        # learned from the hello payload (netpair peers are never in cfg.peers).
        self._netpair_pending: dict[str, str] = {}
        self._netpair_names: dict[str, str] = {}
        # Round 15: peer device_id -> epoch-seconds timestamp of the last frame
        # received from that internet-paired peer (any netpair_hello or
        # mirrored clipboard frame).  Runtime-only — the device page derives
        # "online" from `now - last_seen <= 90s`; nothing here is persisted.
        self._netpair_last_seen: dict[str, float] = {}
        # Device-connectivity probe state (device card "test connection"):
        # ping_id -> {results, replied, event} for in-flight probes.  Runtime
        # only — a probe is a button-press check, nothing is persisted.
        self._device_probes: dict[str, dict] = {}
        self._device_probes_lock = threading.Lock()
        # Round 17: internet delivery ledger + offline queue.  ``_delivery_ledger``
        # maps peer_id -> OrderedDict[msg_id -> {content_hash, ts, status,
        # deadline, preview}] for every clipboard frame we mirrored to that
        # peer (status ∈ sent/delivered/failed/queued).  ``_delivery_queue`` is
        # the persisted subset (status=queued, offline at send time), grouped
        # by peer with the full encoded frame so it can be republished later.
        # Both are guarded by ``_delivery_lock`` and touched by the main thread
        # (send), the relay receive thread (acks), web threads (REST) and a
        # background scanner (timeouts + retries).
        self._delivery_ledger: dict[str, "OrderedDict[str, dict]"] = {}
        self._delivery_queue: dict[str, "OrderedDict[str, dict]"] = {}
        self._delivery_lock = threading.RLock()
        self._delivery_thread: threading.Thread | None = None
        self._delivery_stop_evt = threading.Event()
        # Timestamp of the last silent auto-update check (throttled to ~6h).
        self._last_auto_update_check = 0.0

        # ── Shared mutable state ────────────────────────────────────
        self._discovered_peers: dict[str, dict] = {}
        self._discovered_lock = threading.Lock()
        self._notified_pairings: dict[str, str] = {}
        self._auto_connect_pending: set[str] = set()
        # peer_id -> monotonic timestamp of the last cert-change prompt, used
        # to throttle dialogs while a peer's auto-reconnect keeps presenting a
        # changed certificate.
        self._cert_alert_throttle: dict[str, float] = {}
        # peer_id -> (peer_name, new_cert_pem) for peers whose certificate
        # changed at runtime; kept until the user decides to re-trust or not.
        self._pending_cert_peers: dict[str, tuple[str, str]] = {}
        # transfer_id -> temp zip path, cleaned up when the transfer completes
        # (folder/multi-file sends zip into a temp archive that must not leak).
        self._zip_cleanup: dict[str, str] = {}
        # transfer_id -> (last_ws_push_time, last_ws_progress) for throttling
        # per-chunk WebSocket progress broadcasts.
        self._last_transfer_progress: dict[str, tuple[float, float]] = {}
        # transfer_id -> "incoming"/"outgoing", so _on_transfer_complete can
        # pick the correct notification direction (a receiver must never see
        # "File sent successfully" for a download, nor vice-versa).
        self._transfer_directions: dict[str, str] = {}
        # peer_id -> {code, peer_name, first_seen} for incoming pairing requests,
        # used to surface an "expired" row instead of letting the request vanish.
        self._pairing_req_track: dict[str, dict] = {}
        # peer_id -> threading.Timer holding a not-yet-fired pairing-code
        # notification.  Chat connections auto-generate a shared code for an
        # unpaired peer; the chat invite (same connection, ~1s later) cancels
        # this timer so chatting never surfaces as a pairing request.
        self._pairing_notify_timers: dict[str, threading.Timer] = {}
        # Set once the dashboard has been auto-opened for a silent pairing request
        # (notifications disabled), so we don't pop a window on every request.
        self._pairing_dashboard_opened = False
        # Hotkey-failure bookkeeping: the platform hotkey backend can die
        # immediately after start() (macOS Accessibility permission), so the
        # listener thread liveness is probed once after startup.
        self._hotkey_running = False
        self._hotkey_failure_notified = False
        # Guards re-entry into the "Check for updates" action.
        self._checking_update = False
        # Guards the install half of auto-update: only one of the GitHub
        # download and the P2P blob arrival may stage+apply, even when both
        # land around the same time.  Cleared again on every failure path.
        self._updating = False
        # True once a webview dashboard open actually attached a WS client.
        # Used to keep the "recently opened" guard from blocking a re-open
        # after the window was closed (client disconnected) within 8s.
        self._webview_client_seen = False

        # ── macOS multiprocessing state ─────────────────────────────
        self._parent_conn = None
        self._tray_proc = None
        # Tray-subprocess watchdog: if the child dies (crash / EOF) the app
        # would become headless — no Dock icon and no tray — so we restart it
        # up to a bounded number of times with a short backoff.
        self._macos_tray_restarts = 0
        self._macos_tray_max_restarts = 3
        # Last window size used for the webview dashboard.  None = let the
        # browser use its own last size (no forced --window-size).
        self._webview_size: tuple | None = None

    # ═══════════════════════════════════════════════════════════════
    # Phase 1: Logging (static)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def setup_logging() -> None:
        import logging.handlers

        global _console_handler

        log_dir = _get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)

        raw = os.environ.get("CLIPSYNC_LOG_LEVEL", "").upper()
        level_map = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
                     "WARNING": logging.WARNING, "ERROR": logging.ERROR}
        console_level = level_map.get(raw, logging.INFO)

        file_fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(threadName)-12s "
            "%(name)s:%(lineno)d  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_fmt = logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)-28s  %(message)s",
            datefmt="%H:%M:%S",
        )

        # Root at DEBUG so all messages reach handlers; each handler
        # filters at its own level. File always gets DEBUG; console is
        # controlled by CLIPSYNC_LOG_LEVEL env or settings.
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "clipsync.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_fmt)
        root_logger.addHandler(file_handler)

        _console_handler = logging.StreamHandler(sys.stderr)
        _console_handler.setLevel(console_level)
        _console_handler.setFormatter(console_fmt)
        root_logger.addHandler(_console_handler)

        for noisy in ("zeroconf", "PIL", "cryptography", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    # ═══════════════════════════════════════════════════════════════
    # Phase 2: Config
    # ═══════════════════════════════════════════════════════════════

    def load_config(self) -> None:
        # Remember whether this is a truly-first run BEFORE any save() below
        # creates the config file (bootstrap writes the device identity).
        from internal.config.config import _config_path
        self._first_run = not _config_path().exists()
        self.cfg = load()
        set_locale(self.cfg.language)
        logger.info("=" * 72)
        logger.info("  ClipSync v%s — session start  %s",
                    __version__, time.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("  Platform: %s  |  PID: %d", sys.platform, os.getpid())
        logger.info("=" * 72)
        logger.info("ClipSync starting...")
        logger.info("Device: %s (%s)", self.cfg.device_name, self.cfg.device_id)

    def _show_first_run_onboarding_if_needed(self) -> None:
        """Show the bilingual language picker until a language is actually chosen.

        Runs after the Tk root exists but before services/UI start, so the
        chosen language applies to everything created afterwards.  Existing
        installs that already picked a language never see it.  A dismissed
        picker leaves ``language_chosen`` False, so it reappears on the next
        launch until the user makes a real choice.
        """
        # Session guard: never show the picker twice in one run (even if the
        # user dismisses it, we don't nag again until the next launch).
        if getattr(self, "_onboarding_shown", False):
            return
        if getattr(self.cfg, "language_chosen", False):
            return
        # Web-first: in webview mode the SPA's own first-run wizard owns the
        # language choice (gated by the same language_chosen flag).  Popping
        # a Tk picker on top of it would show two wizards at first launch.
        if self._is_webview():
            return
        self._onboarding_shown = True
        try:
            from internal.ui.onboarding import show_language_onboarding
            chosen = show_language_onboarding(self.root)
        except Exception:
            logger.exception("Failed to show first-run language onboarding")
            return
        if chosen in ("zh-CN", "en"):
            self.cfg.language = chosen
            set_locale(chosen)
            self.cfg.language_chosen = True
            try:
                self._save_cfg_encrypted()
            except Exception:
                logger.debug("Failed to persist first-run language choice", exc_info=True)
        # Dismissed (None): leave language_chosen False so the next launch
        # offers the language picker again.

    # ═══════════════════════════════════════════════════════════════
    # Phase 3: Crypto
    # ═══════════════════════════════════════════════════════════════

    def _bootstrap_crypto(self) -> None:
        cfg = self.cfg

        # ── Auto-start ──────────────────────────────────────────
        if cfg.auto_start and not is_autostart_enabled():
            try:
                enable_autostart()
                logger.info("Auto-start enabled on boot")
            except Exception as e:
                logger.warning("Failed to enable auto-start: %s", e)

        # ── Content filter ──────────────────────────────────────
        self.content_filter = ContentFilter(enabled_categories=cfg.filter_enabled_categories)

        # ── Derive device fingerprint from stored cert ──────────
        device_fingerprint = ""
        if cfg.encryption_enabled and cfg.certificate_pem:
            try:
                device_fingerprint = _fingerprint_pem(cfg.certificate_pem)
            except Exception as exc:
                logger.warning("Failed to derive fingerprint from cert: %s", exc)

        # ── Prompt for encryption password (if hash stored) ─────
        if (cfg.encryption_enabled and cfg.encryption_password_hash
                and not cfg.encryption_password):
            tmp_root = tk.Tk()
            tmp_root.withdraw()
            try:
                entered = ask_string(
                    tmp_root,
                    T("encryption.password_title"),
                    T("encryption.password_prompt"),
                    show="*",
                )
            finally:
                tmp_root.destroy()
            if entered and _verify_password(
                entered, device_fingerprint, cfg.encryption_password_hash,
            ):
                cfg.encryption_password = entered
                logger.info("Encryption password verified")
            elif entered:
                tmp_root2 = tk.Tk()
                tmp_root2.withdraw()
                try:
                    show_error(
                        tmp_root2,
                        T("encryption.wrong_password_title"),
                        T("encryption.wrong_password_msg"),
                    )
                finally:
                    tmp_root2.destroy()
                sys.exit(1)
            else:
                # Dismissed (None) — continuing with an empty key would leave
                # the private key undecryptable and silently break encryption.
                tmp_root3 = tk.Tk()
                tmp_root3.withdraw()
                try:
                    show_error(
                        tmp_root3,
                        T("encryption.password_title"),
                        T("encryption.password_required_msg"),
                    )
                finally:
                    tmp_root3.destroy()
                sys.exit(1)

        logger.info(
            "Encryption config: enabled=%s, password=%s",
            cfg.encryption_enabled,
            "set" if cfg.encryption_password else "not set",
        )

        self.enc_mgr = EncryptionManager(
            device_fingerprint,
            password=cfg.encryption_password if cfg.encryption_enabled else "",
        )

        # ── Decrypt private key if encrypted ────────────────────
        if cfg.encryption_enabled and cfg.private_key_pem:
            pt = self.enc_mgr.decrypt_storage(cfg.private_key_pem)
            if pt is not None:
                if pt != cfg.private_key_pem:
                    logger.info("Private key decrypted from encrypted storage (%d chars)", len(pt))
                cfg.private_key_pem = pt
            else:
                # GCM auth failure — the stored private key is undecryptable
                # (wrong password or corrupt blob).  Fail gracefully like the
                # wrong-password branch above: neutralize the bad blob (so
                # _bootstrap_identity does not crash on the base64 ciphertext
                # in load_pem_private_key) and exit with a clean dialog.
                logger.warning(
                    "Private key decryption FAILED — possibly wrong password "
                    "or corrupted data; exiting cleanly."
                )
                cfg.private_key_pem = ""
                err_root = tk.Tk()
                err_root.withdraw()
                try:
                    show_error(
                        err_root,
                        T("encryption.wrong_password_title"),
                        T("encryption.wrong_password_msg"),
                    )
                finally:
                    err_root.destroy()
                sys.exit(1)

    # ═══════════════════════════════════════════════════════════════
    # Phase 4: Identity / Pairing
    # ═══════════════════════════════════════════════════════════════

    def _bootstrap_identity(self) -> None:
        cfg = self.cfg

        self.pairing_mgr = PairingManager(cfg.device_id, cfg.device_name)
        identity = self.pairing_mgr.load_or_create_identity(
            cfg.private_key_pem, cfg.certificate_pem,
        )
        is_new = cfg.private_key_pem != identity.private_key_pem
        if is_new:
            cfg.private_key_pem = identity.private_key_pem
            cfg.certificate_pem = identity.certificate_pem
        logger.info("Certificate fingerprint: %s", identity.fingerprint_short)

        # Re-create EncryptionManager with correct fingerprint if changed
        if identity.fingerprint != self.enc_mgr._fingerprint:
            self.enc_mgr = EncryptionManager(
                identity.fingerprint,
                password=cfg.encryption_password if cfg.encryption_enabled else "",
            )

        # Migrate old plaintext password to verification hash
        if (cfg.encryption_enabled and cfg.encryption_password
                and not cfg.encryption_password_hash):
            cfg.encryption_password_hash = _make_password_hash(
                cfg.encryption_password, identity.fingerprint,
            )
            logger.info("Migrated plaintext encryption password to verification hash")

        # Save new identity
        if is_new:
            self._save_cfg_encrypted()

        # ── Clipboard history ───────────────────────────────────
        self.clipboard_history = ClipboardHistoryDB(
            max_entries=cfg.history_max_entries,
            enc_mgr=self.enc_mgr if cfg.encryption_enabled else None,
        )

        # ── Register known peers ─────────────────────────────────
        # (device_id, device_name) pairs for peers whose certificate changed
        # vs. the pinned one — surfaced to the user after startup completes.
        self._cert_warnings: list[tuple[str, str]] = []
        for peer in cfg.peers.values():
            try:
                self.pairing_mgr.add_peer(
                    peer.device_id, peer.device_name,
                    peer.public_key_pem, peer.paired,
                )
            except CertificateChangedError:
                self._cert_warnings.append((peer.device_id, peer.device_name))
                logger.warning("Skipping peer %s: certificate changed", peer.device_name)
            except Exception as e:
                logger.warning("Skipping peer %s: %s", peer.device_name, e)

        # ── Pairing notification callback ───────────────────────
        self.pairing_mgr.set_on_new_pairing(self._on_new_pairing)

    # ═══════════════════════════════════════════════════════════════
    # Phase 5: Services
    # ═══════════════════════════════════════════════════════════════

    def _create_services(self) -> None:
        cfg = self.cfg

        # ── Sync Manager ────────────────────────────────────────
        # Low-memory mode polls the clipboard less aggressively.
        _poll = cfg.clipboard_poll_interval
        if cfg.low_memory_mode:
            _poll = max(_poll, 2.0)
        monitor = create_monitor(poll_interval=_poll)
        reader = create_reader()
        writer = create_writer()
        self._monitor = monitor
        self.sync_mgr = SyncManager(
            cfg.device_id, cfg.device_name,
            reader=reader, writer=writer, monitor=monitor,
            history=self.clipboard_history,
            sync_debounce=cfg.sync_debounce,
            retry_enabled=cfg.retry_capture_enabled,
        )
        self.sync_mgr.set_enabled(cfg.sync_enabled)
        # Wire the dedup hash algorithm (sha256 default / simple=md5).  It
        # lives in internal.clipboard.dedup so both history backends share it.
        from internal.clipboard import dedup as _dedup_mod
        _dedup_mod.DEDUP_ALGO = cfg.dedup_method or "sha256"
        # Expire unpinned history rows older than the configured max age
        # (0 disables the limit).
        from internal.clipboard import history_db as _history_db
        _history_db.set_max_age_days(cfg.history_max_age_days or 0)

        # ── Source tracking / app filter ────────────────────────
        # Honor the source_tracking_enabled flag: when disabled, the
        # monitor stops querying the OS for the foreground app.
        monitor.set_source_tracking(cfg.source_tracking_enabled)
        # Wire the app filter (whitelist/blacklist of source processes)
        # into the capture path so disallowed apps are never synced.
        self.sync_mgr.set_app_filter(lambda app_info: is_app_allowed(app_info, cfg))

        # ── Transport ───────────────────────────────────────────
        self.transport_mgr = TransportManager(
            cfg.device_id, cfg.device_name, cfg.port, self.pairing_mgr,
            max_reconnect_attempts=cfg.max_reconnect_attempts,
        )
        if cfg.encryption_enabled:
            self.transport_mgr.set_encryption_manager(self.enc_mgr)

        # ── File Transfer ───────────────────────────────────────
        file_receive_dir = cfg.file_receive_dir if cfg.file_receive_dir else None
        self.file_transfer_mgr = FileTransferManager(
            cfg.device_id,
            output_dir=file_receive_dir,
            transfer_timeout=cfg.transfer_timeout,
        )

        # ── Nearby Chat ─────────────────────────────────────────
        # Chat files reuse the same receive dir clipboard file transfers use
        # (config override, else the platform default).
        chat_receive_dir = (
            cfg.file_receive_dir
            if cfg.file_receive_dir
            else str(Path.home() / "Downloads" / "ClipSync")
        )
        self.chat_mgr = ChatManager(
            cfg.device_id, cfg.device_name, receive_dir=chat_receive_dir,
        )
        # Per-peer chat mutes, persisted in config.  The desktop notification
        # path consults this so a muted device never rings; the web chat UI
        # toggles it via POST /api/chat/mute.
        self._chat_muted: set[str] = set(cfg.chat_muted_peers or [])
        try:
            self.chat_mgr.set_own_fingerprint(
                self.pairing_mgr.get_identity().fingerprint,
            )
        except Exception:
            logger.debug("Could not set own chat fingerprint", exc_info=True)

        # ── AI-config sync (Round 12) ───────────────────────────
        # Metadata inventory + selective file pull between paired peers.
        # Callbacks dereference live state so later transport/web-server
        # re-creation needs no re-wiring.
        self.aicfg_mgr = AIConfigManager(
            self.cfg,
            send_fn=lambda pid, frame: (
                self.transport_mgr.send_to_peer(pid, frame)
                if self.transport_mgr is not None else False
            ),
            connected_fn=lambda: (
                self.transport_mgr.get_connected_peers()
                if self.transport_mgr is not None else []
            ),
            event_fn=self._push_aiconfig_event,
            save_fn=self._save_cfg_encrypted,
        )
        from internal.web.api import aiconfig as _aiconfig_api
        _aiconfig_api.bind(self.aicfg_mgr)

        # ── Internet pairing code (Round 14) ─────────────────────
        # Self-contained REST branch; the Application itself is the bound
        # manager (all netpair logic lives in the methods below).
        from internal.web.api import internetpair as _internetpair_api
        _internetpair_api.bind(self)
        # ── Internet delivery (Round 17) ──────────────────────────
        # Self-contained REST branch over the delivery ledger + queue; the
        # Application itself is the bound manager.
        from internal.web.api import internetdelivery as _internetdelivery_api
        _internetdelivery_api.bind(self)

        # ── Discovery ───────────────────────────────────────────
        self.discovery = Discovery(
            cfg.device_id, cfg.device_name, cfg.port, cfg.service_type,
        )

        # ── Web Companion ───────────────────────────────────────
        def _on_web_nav_url(url: str, device_id: str):
            data = encode_frame({"msg_type": "nav_url", "url": url},
                                source_device=self.cfg.device_id)
            self.transport_mgr.send_to_peer(device_id, data)
            logger.info("Web nav forwarded to peer %s: %s", device_id[:12], url[:80])

        def _on_web_forward_file(file_path: str, device_id: str) -> bool:
            # The target must actually be connected: send_to_peer silently
            # no-ops for a gone peer, which would make the /api/upload handler
            # report success while the file went nowhere.  Return False so the
            # handler can surface a "target not connected" error.
            try:
                if device_id not in (self.transport_mgr.get_connected_peers() or []):
                    logger.warning("Web forward target %s is not connected", device_id[:12])
                    return False
            except Exception:
                logger.debug("get_connected_peers failed during web forward", exc_info=True)
                return False

            def _send_fn(data: bytes):
                self.transport_mgr.send_to_peer(device_id, data)
            try:
                transfer_id = self.file_transfer_mgr.send_file(file_path, _send_fn)
                if transfer_id:
                    self._transfer_directions[transfer_id] = "outgoing"
                logger.info("Web upload forwarded to peer %s: %s",
                            device_id[:12], os.path.basename(file_path))
                return True
            except Exception as e:
                logger.error("Failed to forward uploaded file: %s", e)
                return False

        def _on_web_window_close():
            """Close the webview browser window without killing the app."""
            if self.webview_win is not None:
                self.webview_win.stop()
                self.webview_win = None
            # A fresh open is a fresh chance for a client to attach; the
            # "recently opened" duplicate-window guard only applies while a
            # client actually loaded (see _open_webview_dashboard).
            self._webview_client_seen = False

        self.web_server = WebServer(
            cfg, self.clipboard_history, self.sync_mgr,
            get_connected_ids=lambda: self.transport_mgr.get_connected_peers(),
            # Reconnect progress for offline device cards ("reconnecting N/M");
            # empty dict when the transport layer is gone (shutdown ordering).
            get_reconnect_states=lambda: (
                self.transport_mgr.get_reconnect_states()
                if self.transport_mgr is not None else {}
            ),
            on_nav_url=_on_web_nav_url,
            on_forward_file=_on_web_forward_file,
            get_overview_data=self._get_overview_data,
            on_device_action=self._handle_web_device_action,
            on_device_test=self._device_test_connection,
            on_transfer_action=self._handle_web_transfer_action,
            on_get_transfers=lambda: (
                self.file_transfer_mgr.get_transfers(),
                self.file_transfer_mgr.get_history(),
            ) if self.file_transfer_mgr else ([], []),
            on_speed_test_start=lambda: self.file_transfer_mgr.start_speed_test(
                self.transport_mgr.broadcast,
                has_peers_fn=lambda: bool(self.transport_mgr.get_connected_peers()),
            ) if self.file_transfer_mgr else False,
            on_speed_test_poll=lambda: self.file_transfer_mgr.get_speed_test() if self.file_transfer_mgr else {},
            on_window_close=_on_web_window_close,
            on_toggle_discovery=self._on_toggle_discovery,
            on_toggle_visibility=self._on_toggle_visibility,
            on_settings_change=self._on_web_settings_change,
            on_show_web_qr=lambda: self.root.after(0, self._show_web_qr),
            on_send_url=lambda: self.root.after(0, self._do_send_url),
            get_discovered_peers=lambda: self._snapshot_discovered_peers(),
            on_open_file=self._open_file,
            on_open_folder=self._open_folder,
            on_restart=self._restart_app,
            get_pending_pairings=self._get_pending,
            get_relay_state=self._get_relay_state,
            get_current_relay_broker=self._get_current_relay_broker,
            get_resolved_hashes=lambda: self.transport_mgr.get_resolved_hashes(),
            enc_mgr=self._make_save_enc(),
            get_certs=self._get_certs,
            get_diagnostics=self._get_diagnostics,
            on_update_download=self._handle_update_download,
            on_update_install=self._handle_update_install,
            on_diagnostics_request=self._handle_diagnostics_request,
            on_web_upload=self._on_web_upload,
            # Nearby Chat (web/PWA surface)
            chat_mgr=self.chat_mgr,
            get_chat_devices=self._web_chat_devices,
            chat_send_fn=self._web_chat_send_fn,
            chat_start_session=self._web_chat_start_session,
            get_chat_muted=self._get_chat_muted,
            set_chat_muted=self._set_chat_muted,
        )

        # The web sync-pause API delegates to the host's single timed-pause
        # implementation instead of arming a second auto-resume timer.
        from internal.web.api import sync_control as _sync_control
        _sync_control.set_host_pause_hooks(
            self._pause_sync_for_minutes, self._resume_timed_pause,
        )

        # ── Live history push to web clients ────────────────────────
        # The sync manager records every local/remote clipboard change into
        # history, but nothing notified the web UI about it. Wire a callback
        # so the WebSocket layer broadcasts `history_updated` immediately
        # after each add — this is what makes newly copied items appear in
        # the history panel without a manual refresh.
        def _on_history_change():
            if self.web_server is not None:
                try:
                    self.web_server.ws_manager.broadcast_history()
                except Exception:
                    logger.debug("Failed to broadcast history to web clients", exc_info=True)

        self.sync_mgr.on_history_change = _on_history_change

    # ═══════════════════════════════════════════════════════════════
    # Phase 6: Callback wiring
    # ═══════════════════════════════════════════════════════════════

    def _wire_callbacks(self) -> None:
        # ── Sync → Transport ────────────────────────────────────
        self.sync_mgr.on_send = self._on_local_sync

        # ── Transport → Sync / File Transfer ────────────────────
        self.transport_mgr.set_on_peer_message(self._on_peer_message)

        # ── File transfer callbacks ─────────────────────────────
        self.file_transfer_mgr.set_on_transfer_progress(self._on_transfer_progress)
        self.file_transfer_mgr.set_on_transfer_complete(self._on_transfer_complete)
        self.file_transfer_mgr.set_on_file_received(self._on_file_received)
        self.file_transfer_mgr.set_on_transfer_request(self._on_transfer_request)

        # ── Sync manager callbacks ──────────────────────────────
        # A failed remote clipboard write is otherwise silent; surface it so
        # the user knows the received content never reached the clipboard.
        self.sync_mgr.set_on_write_error(
            lambda: self._notify(
                "notify_sync",
                T("sync.write_failed_title"),
                T("sync.write_failed_msg"),
            )
        )

        # ── Discovery callbacks ─────────────────────────────────
        self.discovery.set_callbacks(self._on_peer_found, self._on_peer_lost)

        # ── Nearby chat callbacks ───────────────────────────────
        self._wire_chat_callbacks()

        # ── Security alerts ──────────────────────────────────────
        self.transport_mgr.set_on_security_alert(self._on_security_alert)

        # ── Wake recovery ───────────────────────────────────────
        self.transport_mgr.set_on_wake(self.discovery._wake_recovery)

        # ── Hotkey callbacks ──────────────────────────────────────
        self._wire_hotkeys()

    def _wire_chat_callbacks(self) -> None:
        """Register Nearby-chat manager callbacks (fired on worker threads).

        Every callback marshals state onto the Tk main thread for the desktop
        UI AND pushes real-time updates to web clients.  The ChatManager docs
        require UI work to happen on the Tk main thread; the WebSocketManager
        broadcast is thread-safe, so the web push happens directly here.
        """
        cm = self.chat_mgr
        if cm is None:
            return
        cm.set_on_incoming_invite(self._on_chat_incoming_invite)
        cm.set_on_invite_response(self._on_chat_invite_response)
        cm.set_on_sessions_changed(self._chat_sessions_changed)
        cm.set_on_message(self._on_chat_message)
        cm.set_on_file_progress(self._chat_file_progress)
        cm.set_on_file_done(self._chat_file_done)

    def _push_web_chat_sessions(self) -> None:
        """Push the full chat session list to web clients (thread-safe).

        Called from chat worker-thread callbacks and from the invite dialog
        path so the web chat tab always reflects the current sessions.
        """
        try:
            sessions = self.chat_mgr.get_sessions() \
                if getattr(self, "chat_mgr", None) is not None else []
            self._push_web("broadcast_chat_sessions", sessions)
        except Exception:
            logger.debug("Failed to push chat sessions to web", exc_info=True)

    def _chat_sessions_changed(self, *args) -> None:
        """Sessions changed on a worker thread: refresh desktop + push to web."""
        self._push_web_chat_sessions()
        self._chat_event_from_worker()

    def _chat_file_progress(self, session_id: str, transfer_id: str,
                            fraction: float) -> None:
        """File progress on a worker thread: push to web + refresh desktop."""
        self._push_web("broadcast_chat_progress", session_id, transfer_id, fraction)
        self._chat_event_from_worker()

    def _chat_file_done(self, session_id: str, transfer_id: str, success: bool,
                        saved_path: str, status: str) -> None:
        """File done on a worker thread: push to web + refresh desktop."""
        self._push_web(
            "broadcast_chat_file_done", session_id, transfer_id, success,
            saved_path or "", status or "",
        )
        self._push_web_chat_sessions()
        self._chat_event_from_worker()

    def _chat_event_from_worker(self, *args) -> None:
        """Chat state changed on a worker thread — hop to the Tk thread.

        Coalesced: file progress fires once per 256 KB chunk; without a
        pending guard a large transfer would enqueue thousands of after(0)
        callbacks, each triggering a full conversation rebuild.
        """
        if getattr(self, "_chat_refresh_pending", False):
            return
        self._chat_refresh_pending = True
        try:
            self.root.after(0, self._chat_event_on_main)
        except Exception:
            self._chat_refresh_pending = False
            logger.debug("chat event marshal failed", exc_info=True)

    def _chat_event_on_main(self) -> None:
        """Push a chat refresh into the dashboard (Tk main thread only)."""
        self._chat_refresh_pending = False
        if getattr(self, "dashboard_win", None) is not None:
            try:
                self.dashboard_win._refresh_chat()
            except Exception:
                logger.debug("dashboard chat refresh failed", exc_info=True)

    def _dashboard_visible(self) -> bool:
        win = getattr(getattr(self, "dashboard_win", None), "_window", None)
        if win is None:
            return False
        try:
            return bool(win.winfo_viewable())
        except Exception:
            return False

    # ── Chat: incoming invitation + messages ─────────────────────

    def _on_chat_incoming_invite(self, invite: dict) -> None:
        try:
            self.root.after(0, lambda: self._chat_handle_incoming_invite(invite))
        except Exception:
            logger.debug("chat invite marshal failed", exc_info=True)

    def _chat_handle_incoming_invite(self, invite: dict) -> None:
        sid = invite.get("session_id", "")
        peer_name = invite.get("peer_name", "") or "?"
        fp = invite.get("fingerprint_short", "") or ""
        greeting = invite.get("greeting", "") or ""
        if not sid:
            return
        # Paired peers are already trusted end-to-end (cert-pinned TLS), so
        # skip the invite dialog for them — chatting with a known device is
        # as frictionless as clipboard sync itself.  Unpaired strangers still
        # always ask for explicit consent.
        peer_id = invite.get("peer_id", "")
        # A chat invite is its own consent flow (invite/accept + fingerprint);
        # it must NOT also force a pairing request.  The connection handshake
        # auto-generated a shared pairing code for this unpaired peer — drop
        # it (and the web row) so chatting alone never surfaces as "wants to
        # pair".  Paired peers have nothing pending to drop.
        try:
            if peer_id and self.pairing_mgr is not None \
                    and not self.pairing_mgr.is_peer_paired(peer_id):
                # Cancel the debounced pairing notification before it fires so
                # the other device never SEES a pairing prompt for a chat (the
                # code was generated at connect time, before this invite frame).
                pending = self._pairing_notify_timers.pop(peer_id, None)
                if pending is not None:
                    pending.cancel()
                self._notified_pairings.pop(peer_id, None)
                self.pairing_mgr.discard_pending_pairing(peer_id)
                self._notified_pairings.pop(peer_id, None)
                self._pairing_req_track.pop(peer_id, None)
                self._push_web("broadcast", "pairing_resolved", {"peer_id": peer_id})
        except Exception:
            logger.debug("chat invite pairing cleanup failed", exc_info=True)
        # Trusted-LAN direct chat: the invite is an internal session handshake,
        # not a consent gate.  Any device reachable on the network can chat
        # immediately — no accept/decline banner, no waiting for approval.
        try:
            if peer_id:
                self._chat_respond_invite(sid, True)
                return
        except Exception:
            logger.debug("chat auto-accept failed", exc_info=True)
        title = T("chat.notify_invite_title")
        message = T("chat.invite_banner_title", name=peer_name)
        if fp:
            message += "\n" + T("chat.invite_fingerprint", fingerprint=fp)
        if greeting:
            message += "\n" + T("chat.invite_greeting", greeting=greeting)
        message += "\n" + T("chat.invite_prompt")
        if self._is_webview():
            def _on_result(result):
                accepted = bool(
                    result is not None and result.get("action") == "accept"
                )
                self._chat_respond_invite(sid, accepted)
            try:
                self._web_dialog_async(
                    "confirm", _on_result, title=title, message=message,
                    accept_label=T("chat.accept"), reject_label=T("chat.decline"),
                    timeout=120,
                )
            except Exception:
                logger.debug("web invite dialog failed", exc_info=True)
            # The chat tab needs the pending invite right away even though the
            # consent dialog is handled out-of-band.
            self._push_web_chat_sessions()
            return
        if self._dashboard_visible():
            try:
                accepted = ask_yesno(self.root, title, message)
            except Exception:
                accepted = False
            self._chat_respond_invite(sid, accepted)
        else:
            # Window hidden: surface an in-app toast; the invite stays
            # pending in the dashboard where the user can act on it later.
            try:
                self._web_toast(
                    f"{title} · "
                    + T("chat.notify_invite_msg", name=peer_name, fingerprint=fp or "—"))
            except Exception:
                logger.debug("chat invite toast failed", exc_info=True)
            self._chat_event_on_main()

    def _chat_respond_invite(self, sid: str, accepted: bool) -> None:
        peer_id = self._chat_peer_id_for_sid(sid)
        send_fn = self._chat_send_fn(peer_id)
        try:
            if accepted:
                self.chat_mgr.accept_invitation(sid, send_fn)
            else:
                self.chat_mgr.decline_invitation(sid, send_fn)
        except Exception:
            logger.debug("chat invite response failed", exc_info=True)
        self._chat_event_on_main()

    def _on_chat_invite_response(self, session_id: str, peer_id: str,
                                 accepted: bool) -> None:
        # Answer to OUR invitation — nothing special to show right now; just
        # refresh the dashboard so the session status updates.
        self._chat_event_from_worker()

    def _on_chat_message(self, session_id: str, entry_dict: dict) -> None:
        # Web push is thread-safe (WebSocketManager.broadcast takes a manager
        # lock) and fires directly from the chat worker thread.
        self._push_web("broadcast_chat_message", session_id, entry_dict)
        self._push_web_chat_sessions()
        try:
            self.root.after(
                0, lambda: self._chat_message_on_main(session_id, entry_dict),
            )
        except Exception:
            logger.debug("chat message marshal failed", exc_info=True)

    def _chat_message_on_main(self, session_id: str, entry_dict: dict) -> None:
        try:
            kind = entry_dict.get("kind")
            outgoing = entry_dict.get("outgoing")
            # A muted peer never rings — the web chat UI's bell toggle sets
            # the mute (persisted in config), and this is the notification
            # path it must suppress.  Unknown peer (None) stays unmuted so an
            # orphaned session doesn't silently go quiet.
            peer_id = self._chat_peer_id_for_sid(session_id) or ""
            muted = bool(peer_id and peer_id in self._chat_muted)
            if (kind == "text" and not outgoing and not self._dashboard_visible()
                    and not muted and getattr(self.cfg, "sound_enabled", True)):
                peer_name = self._chat_peer_name_for_sid(session_id) or "?"
                text = (entry_dict.get("text") or "")[:120]
                self._web_toast(
                    f"{T('chat.notify_message_title')}: "
                    + T("chat.notify_message_msg", name=peer_name, text=text))
        except Exception:
            logger.debug("chat message toast failed", exc_info=True)
        self._chat_event_on_main()

    def _chat_peer_id_for_sid(self, sid: str) -> str | None:
        try:
            for s in self.chat_mgr.get_sessions():
                if s.get("session_id") == sid:
                    return s.get("peer_id")
        except Exception:
            return None
        return None

    def _chat_peer_name_for_sid(self, sid: str) -> str | None:
        try:
            for s in self.chat_mgr.get_sessions():
                if s.get("session_id") == sid:
                    return s.get("peer_name") or s.get("peer_id")
        except Exception:
            return None
        return None

    def _chat_send_fn(self, peer_id: str | None):
        """Build a send closure bound to *peer_id* (broadcast when unknown).

        The closure sends over the LAN P2P channel and, only when that fails,
        mirrors the frame to the peer over the public relay — so chat reaches
        internet-only paired devices (netpair pairing-code peers, or LAN-paired
        peers that enrolled a relay secret) across networks.  LAN-first avoids
        delivering the same frame TWICE to a dual-connected peer: chat has no
        content dedup (unlike clipboard history), so an unconditional relay
        mirror would append every message twice.  The closure returns whether
        EITHER path delivered the frame — chat's ``_send_frame`` tests
        ``result is True`` — so an internet-only peer (LAN send fails) still
        counts as delivered.  Broadcast (unknown peer) is never relay-mirrored:
        internet peers are always paired, so an invite to an unpaired LAN
        device stays LAN-only.
        """
        if not peer_id:
            return self.transport_mgr.broadcast

        def _send(data: bytes):
            lan_ok = self.transport_mgr.send_to_peer(peer_id, data)
            if lan_ok:
                # Round 17: a chat frame delivered over LAN P2P to an
                # internet-reachable peer is ledger-recorded too, so the
                # peer's relay_ack is not dropped as an unknown msg_id (the
                # relay path already records inside _relay_publish_to_peer).
                try:
                    from internal.protocol.codec import decode_message
                    decoded = decode_message(data)
                    if getattr(decoded, "msg_type", "") in CHAT_MSG_TYPES \
                            and hasattr(self, "_delivery_ledger") \
                            and self._peer_is_internet_reachable(peer_id):
                        payload = getattr(decoded, "_raw_payload", {}) or {}
                        sid = payload.get("session_id", "")
                        self._delivery_on_sent(
                            peer_id,
                            getattr(decoded, "msg_id", "") or "",
                            "",
                            self._delivery_chat_preview(
                                getattr(decoded, "msg_type", ""), payload),
                            kind=getattr(decoded, "msg_type", "chat"),
                            session_id=str(sid) if isinstance(sid, str) else "",
                        )
                except Exception:
                    logger.debug("chat delivery ledger failed", exc_info=True)
                return True
            return self._relay_publish_to_peer(data, peer_id)
        # Internet-only peer (internet-reachable but with no live LAN P2P
        # connection): every file byte must ride the relay, so tell
        # ChatManager to chunk the file relay-safe and refuse files past the
        # relay cap — otherwise a 256 KiB chunk would exceed MAX_RELAY_PAYLOAD
        # and the transfer would stall exactly like it used to.  A
        # LAN-connected (dual) peer keeps the 256 KiB LAN wire format so
        # pre-update LAN peers still interoperate unchanged.
        if peer_id:
            try:
                connected = set(self.transport_mgr.get_connected_peers() or [])
            except Exception:
                connected = set()
            if peer_id not in connected and self._peer_is_internet_reachable(peer_id):
                # RELAY_CHUNK_SIZE / RELAY_FILE_CAP are class attributes on
                # ChatManager (already imported at module scope) — no local
                # import here: a NameError inside the send closure would be
                # swallowed by chat's frame handler and kill the whole chat.
                _send.chunk_size = ChatManager.RELAY_CHUNK_SIZE
                _send.internet_cap = ChatManager.RELAY_FILE_CAP
        return _send

    def _peer_is_internet_reachable(self, peer_id: str) -> bool:
        """True when *peer_id* can be reached over the public relay.

        Either a confirmed pairing-code peer (``cfg.netpair_secrets``) or a
        LAN-paired peer whose relay secret we know (``cfg.peer_relay_secrets``
        with a paired ``cfg.peers`` entry).  ``_chat_start_session`` uses this
        to start a chat without a LAN address; ``_relay_publish_to_peer`` is
        the transport half (it re-derives the channel + key).
        """
        if peer_id in (getattr(self.cfg, "netpair_secrets", {}) or {}):
            return True
        if peer_id in (getattr(self.cfg, "peer_relay_secrets", {}) or {}):
            peer = self.cfg.peers.get(peer_id)
            return peer is not None and getattr(peer, "paired", False)
        return False

    # ── Chat: dashboard passthroughs ─────────────────────────────

    def get_device_states(self) -> list[dict]:
        """Unified, deduplicated view of every known device and its state.

        One entry per device, keyed by the canonical real ``device_id`` (or the
        hashed mDNS id when the real id is still unknown).  Each entry carries
        explicit state flags so the UI never has to stitch together the three
        underlying sources (pairing / transport / discovery) itself:

            {"peer_id", "name", "paired", "pairing", "connected",
             "address", "port", "fingerprint_short"}

        ``connected`` means a live TCP/TLS connection, which is NOT the same as
        ``paired`` (trusted) — a device mid-pairing is connected but not paired.
        """
        from internal.transport.discovery import Discovery

        try:
            connected = set(self.transport_mgr.get_connected_peers() or [])
            resolved = self.transport_mgr.get_resolved_hashes() or {}
        except Exception:
            connected, resolved = set(), {}

        def _hash(real: str) -> str:
            try:
                return Discovery._hash_device_id(real)
            except Exception:
                return ""

        devices: dict[str, dict] = {}
        seen_hashes: set[str] = set()

        def _ensure(real: str) -> dict:
            d = devices.get(real)
            if d is None:
                d = {
                    "peer_id": real, "name": real, "paired": False,
                    "pairing": False, "connected": False,
                    "address": "", "port": 0, "fingerprint_short": "",
                    "session": "",
                }
                devices[real] = d
                h = _hash(real)
                if h:
                    seen_hashes.add(h)
            return d

        # 1. Paired peers — canonical, always shown (even when offline).
        try:
            for peer in self.pairing_mgr.get_known_peers():
                if not getattr(peer, "paired", False):
                    continue
                pid = peer.device_id
                if pid == self.cfg.device_id:
                    continue
                d = _ensure(pid)
                d["paired"] = True
                d["name"] = peer.device_name or pid
                d["connected"] = pid in connected
        except Exception:
            logger.debug("get_device_states: pairing list failed", exc_info=True)

        # 2. Pairing-in-progress peers.
        try:
            for item in self.pairing_mgr.get_pending_pairings() or []:
                pid = item[0] if isinstance(item, (tuple, list)) and item else ""
                if not pid or pid == self.cfg.device_id:
                    continue
                real = resolved.get(pid, pid)
                d = _ensure(real)
                d["pairing"] = True
                d["connected"] = d["connected"] or (real in connected or pid in connected)
        except Exception:
            logger.debug("get_device_states: pending list failed", exc_info=True)

        # 3. Connected (TCP) peers — resolve hashes to real ids, skip anon.
        for pid in connected:
            if pid.startswith("__anon__"):
                continue
            real = resolved.get(pid, pid)
            if real == self.cfg.device_id:
                continue
            d = _ensure(real)
            d["connected"] = True

        # 4. Discovered peers (hashed mDNS ids).
        try:
            for hash_id, info in self._snapshot_discovered_peers().items():
                if hash_id in seen_hashes:
                    continue
                real = resolved.get(hash_id, hash_id)
                if real == self.cfg.device_id:
                    continue
                d = _ensure(real)
                d["name"] = info.get("name") or d["name"]
                d["address"] = info.get("address", "")
                d["port"] = info.get("port", 0)
                seen_hashes.add(hash_id)
        except Exception:
            logger.debug("get_device_states: discovery list failed", exc_info=True)

        # 5. Attach the chat-session status (if any) so device + session state
        #    live in one view.  Sessions are keyed by the canonical real id.
        try:
            if self.chat_mgr is not None:
                for s in self.chat_mgr.get_sessions():
                    pid = s.get("peer_id", "")
                    real = resolved.get(pid, pid)
                    d = _ensure(real)
                    if not d["name"] or d["name"] == d["peer_id"]:
                        d["name"] = s.get("peer_name") or d["name"]
                    d["session"] = s.get("status", "")
        except Exception:
            logger.debug("get_device_states: session list failed", exc_info=True)

        # 6. Auto-reconnect progress: expose "reconnecting (attempt N/M)" so
        #    a paired device that dropped shows activity instead of looking
        #    plainly offline.  Reconnect bookkeeping is keyed by whichever id
        #    form scheduling used — try the real id, then its hash.
        try:
            reconnect_states = self.transport_mgr.get_reconnect_states() \
                if self.transport_mgr is not None else {}
        except Exception:
            reconnect_states = {}
        if reconnect_states:
            for d in devices.values():
                if d["connected"]:
                    continue
                st = reconnect_states.get(d["peer_id"])
                if st is None:
                    st = reconnect_states.get(_hash(d["peer_id"]))
                if st is not None:
                    d["reconnecting"] = True
                    d["reconnect_attempt"] = int(st.get("attempts", 0))
                    d["reconnect_max"] = int(st.get("max_attempts", 0))

        # Fill address/port + fingerprint for peers without a discovered entry.
        for real, d in devices.items():
            if d["connected"] and not d["fingerprint_short"]:
                try:
                    d["fingerprint_short"] = ChatManager.shorten_fingerprint(
                        self.transport_mgr.get_peer_fingerprint(real),
                    )
                except Exception:
                    d["fingerprint_short"] = ""
            if not d["address"]:
                try:
                    d["address"], d["port"] = self._chat_device_address(real)
                except Exception:
                    pass

        result = list(devices.values())
        result.sort(key=lambda x: (
            not x["paired"], not x["connected"], (x["name"] or "").lower(),
        ))
        return result

    def _get_chat_devices(self) -> list[dict]:
        """Merge PAIRED peers (always shown) with UNPAIRED discovered peers."""
        return [
            {
                "peer_id": d["peer_id"],
                "name": d["name"],
                "address": d["address"],
                "port": d["port"],
                "paired": d["paired"],
                "fingerprint_short": d["fingerprint_short"],
            }
            for d in self.get_device_states()
        ]

    def _chat_device_address(self, peer_id: str) -> tuple[str, int]:
        """Resolve the best-known (address, port) for a peer (any id form)."""
        try:
            hashed = Discovery._hash_device_id(peer_id)
        except Exception:
            hashed = peer_id
        with self._discovered_lock:
            info = (self._discovered_peers.get(peer_id)
                    or self._discovered_peers.get(hashed))
        if info:
            return info["address"], info["port"]
        try:
            saved = self.transport_mgr.get_saved_address(peer_id)
        except Exception:
            saved = None
        if saved:
            return saved[1], saved[2]
        peer_cfg = self.cfg.peers.get(peer_id)
        if peer_cfg and peer_cfg.last_ip:
            return peer_cfg.last_ip, peer_cfg.last_port or self.cfg.port
        return "", 0

    def _chat_start_session(self, peer_id: str, peer_name: str,
                            fingerprint_short: str) -> str | None:
        """Open a chat session, connecting first if the peer is offline."""
        try:
            resolved = self.transport_mgr.get_resolved_hashes() or {}
            real_id = resolved.get(peer_id, peer_id)
        except Exception:
            real_id = peer_id
        try:
            connected = set(self.transport_mgr.get_connected_peers() or [])
        except Exception:
            connected = set()
        if real_id in connected or peer_id in connected:
            return self.chat_mgr.start_session(
                real_id, peer_name, fingerprint_short or "",
                self._chat_send_fn(real_id),
            )
        address, port = self._chat_device_address(peer_id)
        if not address:
            # Internet-only paired peer (netpair or relay-enrolled): no LAN
            # address to connect to, but the per-peer send_fn mirrors chat
            # frames over the public relay — start the session directly so an
            # internet pairing code alone is enough to open a conversation.
            if self._peer_is_internet_reachable(peer_id):
                try:
                    return self.chat_mgr.start_session(
                        real_id, peer_name, fingerprint_short or "",
                        self._chat_send_fn(real_id),
                    )
                except Exception:
                    logger.debug("chat start: relay session failed", exc_info=True)
                    return None
            logger.warning("chat start: no address for peer %s", peer_id[:12])
            try:
                self.root.after(0, lambda: self._notify_info(
                    T("chat.title"),
                    T("chat.err_connect_timeout", name=peer_name),
                ))
            except Exception:
                pass
            return None
        try:
            self.transport_mgr.connect_to_peer(
                peer_id, peer_name, address, port, no_auto_pairing=True,
            )
        except Exception as e:
            logger.debug("chat start: connect failed: %s", e)
            return None

        def _poll():
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    now_connected = set(self.transport_mgr.get_connected_peers() or [])
                    now_resolved = self.transport_mgr.get_resolved_hashes() or {}
                    real_now = now_resolved.get(peer_id, peer_id)
                except Exception:
                    now_connected, real_now = set(), peer_id
                if real_now in now_connected or peer_id in now_connected:
                    try:
                        sid = self.chat_mgr.start_session(
                            real_now, peer_name, fingerprint_short or "",
                            self._chat_send_fn(real_now),
                        )
                    except Exception:
                        logger.debug("chat start: start_session failed", exc_info=True)
                        sid = None
                    self._chat_event_from_worker()
                    # Open the freshly invited session instead of making the
                    # user hunt for it in the session list.
                    if sid:
                        self.root.after(0, lambda s=sid: self._chat_select_session(s))
                    return
                time.sleep(0.3)
            try:
                self.root.after(0, lambda: self._notify_info(
                    T("chat.title"),
                    T("chat.err_connect_timeout", name=peer_name),
                ))
            except Exception:
                pass

        threading.Thread(target=_poll, daemon=True, name="chat-connect").start()
        return None

    def _chat_get_sessions(self) -> list[dict]:
        try:
            return self.chat_mgr.get_sessions()
        except Exception:
            return []

    def _chat_select_session(self, session_id: str) -> None:
        """Open a chat session in the dashboard (Tk main thread only)."""
        try:
            dash = getattr(self, "dashboard_win", None)
            if dash is not None:
                dash._chat_select_session(session_id)
        except Exception:
            logger.debug("chat auto-select failed", exc_info=True)

    def _chat_get_messages(self, session_id: str) -> list[dict]:
        try:
            return self.chat_mgr.get_messages(session_id)
        except Exception:
            return []

    def _chat_mark_read(self, session_id: str) -> None:
        try:
            self.chat_mgr.mark_session_read(session_id)
        except Exception:
            pass

    def _chat_send_text(self, session_id: str, text: str) -> bool:
        try:
            return self.chat_mgr.send_text(
                session_id, text,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except Exception:
            return False

    def _chat_resend_text(self, session_id: str, entry_id: str) -> bool:
        try:
            return self.chat_mgr.resend_text(
                session_id, entry_id,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except Exception:
            return False

    def _chat_send_file(self, session_id: str, file_path: str):
        try:
            return self.chat_mgr.send_file(
                session_id, file_path,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except ChatFileTooLarge:
            logger.info("chat: refusing file above internet relay cap")
            return None
        except Exception:
            return None

    def _chat_accept_invite(self, session_id: str) -> bool:
        try:
            return self.chat_mgr.accept_invitation(
                session_id,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except Exception:
            return False

    def _chat_decline_invite(self, session_id: str) -> bool:
        try:
            return self.chat_mgr.decline_invitation(
                session_id,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except Exception:
            return False

    def _chat_close_session(self, session_id: str) -> bool:
        try:
            return self.chat_mgr.close_session(session_id)
        except Exception:
            return False

    def _chat_cancel_file(self, session_id: str, entry_id: str) -> bool:
        try:
            return self.chat_mgr.cancel_file(session_id, entry_id)
        except Exception:
            return False

    def _chat_accept_file(self, session_id: str, transfer_id: str) -> bool | None:
        # ChatManager.accept_file returns None when the offer is already gone
        # (swept by the stale-receive reaper) — a distinct sentinel from False
        # (offer exists but can't be accepted right now).  Transmit it so the
        # desktop UI can tell the user the offer expired instead of failing.
        try:
            return self.chat_mgr.accept_file(
                session_id, transfer_id,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except Exception:
            return False

    def _chat_decline_file(self, session_id: str, transfer_id: str) -> bool:
        try:
            return self.chat_mgr.decline_file(
                session_id, transfer_id,
                self._chat_send_fn(self._chat_peer_id_for_sid(session_id)),
            )
        except Exception:
            return False

    # ── Chat: web API callbacks ──────────────────────────────────
    # Thin wrappers the web backend (internal/web/api/chat.py) uses so it can
    # reuse the exact desktop device-merge, connect-first and per-peer send_fn
    # logic without duplicating it.

    def _web_chat_devices(self) -> list[dict]:
        """Chat device list for the web API (same merge as desktop)."""
        return self._get_chat_devices()

    def _web_chat_start_session(self, peer_id: str, peer_name: str):
        """Open a chat session for the web API (connect-first if offline).

        Returns ``{"session_id": <id>}`` when a session is live,
        ``{"connecting": True}`` while an async connect is in flight (a
        ``chat_sessions`` push arrives once the invited session lands), or
        ``{"ok": False, "error": <reason>}`` when the invite was refused
        (rate limit / slots full / peer unreachable) — so the web UI can
        tell "waiting" apart from "failed" instead of showing "Connecting…"
        forever.
        """
        try:
            resolved = self.transport_mgr.get_resolved_hashes() or {}
            real_id = resolved.get(peer_id, peer_id)
        except Exception:
            real_id = peer_id
        try:
            connected = set(self.transport_mgr.get_connected_peers() or [])
        except Exception:
            connected = set()
        if real_id in connected or peer_id in connected:
            sid = self.chat_mgr.start_session(
                real_id, peer_name, "", self._chat_send_fn(real_id),
            )
            if sid:
                return {"session_id": sid}
            return {"ok": False, "error": "invite_failed"}
        address, port = self._chat_device_address(peer_id)
        if not address:
            logger.warning("web chat start: no address for peer %s", peer_id[:12])
            return {"ok": False, "error": "peer_unreachable"}
        try:
            self.transport_mgr.connect_to_peer(
                peer_id, peer_name, address, port, no_auto_pairing=True,
            )
        except Exception:
            logger.debug("web chat start: connect failed", exc_info=True)
            return {"ok": False, "error": "connect_failed"}

        def _poll():
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    now_connected = set(self.transport_mgr.get_connected_peers() or [])
                    now_resolved = self.transport_mgr.get_resolved_hashes() or {}
                    real_now = now_resolved.get(peer_id, peer_id)
                except Exception:
                    now_connected, real_now = set(), peer_id
                if real_now in now_connected or peer_id in now_connected:
                    try:
                        self.chat_mgr.start_session(
                            real_now, peer_name, "", self._chat_send_fn(real_now),
                        )
                    except Exception:
                        logger.debug("web chat start: start_session failed", exc_info=True)
                    self._chat_event_from_worker()
                    return
                time.sleep(0.3)
            try:
                self.root.after(0, lambda: self._notify_info(
                    T("chat.title"),
                    T("chat.err_connect_timeout", name=peer_name),
                ))
            except Exception:
                pass

        threading.Thread(target=_poll, daemon=True, name="web-chat-connect").start()
        return {"connecting": True}

    def _web_chat_send_fn(self, peer_id: str | None):
        """Per-peer send closure for web chat API handlers."""
        return self._chat_send_fn(peer_id)

    def _get_chat_muted(self) -> list[str]:
        """Current muted chat peer_ids (for the web chat UI)."""
        return sorted(self._chat_muted)

    def _set_chat_muted(self, peer_id: str, muted: bool) -> list[str]:
        """Mute/unmute a chat peer and persist the set to config.

        Called from the web chat UI's bell toggle.  The set feeds both the
        desktop notification path (``_chat_message_on_main``) and the web
        unread badge (loaded back via GET /api/chat/sessions).
        """
        peer_id = (peer_id or "").strip()
        if peer_id:
            if muted:
                self._chat_muted.add(peer_id)
            else:
                self._chat_muted.discard(peer_id)
        try:
            self.cfg.chat_muted_peers = sorted(self._chat_muted)
            save(self.cfg, self._make_save_enc())
        except Exception:
            logger.debug("chat mute persist failed", exc_info=True)
        return self._get_chat_muted()

    def _wire_hotkeys(self) -> None:
        """Register global hotkey callbacks from config and start the listener."""
        if not getattr(self.cfg, "hotkeys_enabled", False):
            self.hotkey_mgr = None
            logger.info("Global hotkeys disabled by config")
            return
        self.hotkey_mgr = HotkeyManager()

        def _cb_factory(action: str):
            if action == "show_window":
                return lambda: self.root.after(0, self.open_dashboard)
            elif action == "toggle_monitor":
                return lambda: self.root.after(0, self._toggle_monitor)
            elif action == "paste_plain":
                return lambda: self.root.after(0, self._paste_plain)
            elif action.startswith("paste_"):
                n = int(action.split("_")[1])
                return lambda n=n: self.root.after(0, lambda: self._paste_nth(n))
            else:
                return lambda: None

        self.hotkey_mgr.reload_from_config(self.cfg.hotkeys, _cb_factory)
        self.hotkey_mgr.start()
        self._hotkey_running = self.hotkey_mgr.running
        if not self.hotkey_mgr.running:
            self._hotkey_failure_notified = True
            self._notify_hotkey_failure()
        logger.info("Global hotkeys registered (%d shortcuts)", len(self.cfg.hotkeys))

    def _check_hotkey_health(self) -> None:
        """Surface a one-time warning if the hotkey backend failed at startup.

        ``HotkeyManager.running`` stays True even when the platform listener
        thread dies immediately (e.g. macOS missing Accessibility permission),
        so probe the listener thread liveness once after startup.  On macOS
        also check ``is_trusted()``: a CGEvent tap can be *created* yet never
        fire when the app is not in the Accessibility whitelist, which the
        liveness probe alone cannot detect.
        """
        if getattr(self, "_hotkey_failure_notified", False) or self._shutting_down:
            return
        mgr = self.hotkey_mgr
        if mgr is None:
            return
        thread = getattr(mgr, "_thread", None)
        alive = thread is not None and thread.is_alive()
        if not mgr.running or not alive:
            self._hotkey_failure_notified = True
            self._notify_hotkey_failure()
            return
        # macOS: tap created but the process isn't Accessibility-trusted →
        # the listener stays alive but never receives events.
        try:
            if not mgr.is_trusted():
                self._hotkey_failure_notified = True
                self._notify_hotkey_failure()
        except Exception:
            logger.debug("Hotkey is_trusted check failed", exc_info=True)
        # Partial failure: the OS refused specific combinations (typically
        # already claimed by another application).  The listener is healthy,
        # but those hotkeys silently never fire — surface them once so the
        # user knows to pick different shortcuts in settings.
        try:
            failed = mgr.failed_shortcuts()
        except Exception:
            failed = []
        if failed:
            self._hotkey_failure_notified = True
            detail = ", ".join(s for _hid, s in failed[:4])
            try:
                self._notify_error(
                    T("hotkey.failed_title"),
                    f"{T('hotkey.failed_msg')}\n{detail}",
                )
            except Exception:
                logger.debug("Could not surface partial hotkey failure", exc_info=True)

    def _notify_hotkey_failure(self) -> None:
        """Notify the user that global hotkeys are unavailable (once)."""
        try:
            self._notify_error(T("hotkey.failed_title"), T("hotkey.failed_msg"))
        except Exception:
            logger.debug("Could not surface hotkey failure dialog", exc_info=True)


    def _paste_nth(self, n: int) -> None:
        """Paste the nth history item (1-indexed) directly to the clipboard."""
        import base64

        items = self.clipboard_history.get_all()
        if n < 1 or n > len(items):
            logger.debug("paste_%d: index out of range (%d items)", n, len(items))
            return

        entry = items[n - 1]
        types: dict = entry.get("types", {})
        if not types:
            logger.debug("paste_%d: entry has no types", n)
            return

        _type_map = {
            "TEXT": _CT.TEXT, "HTML": _CT.HTML,
            "IMAGE": _CT.IMAGE_PNG, "IMAGE_EMF": _CT.IMAGE_EMF,
            "RTF": _CT.RTF,
            "FILE": _CT.FILE, "URL": _CT.URL,
        }
        ctypes: dict = {}
        for key, b64_data in types.items():
            ct = _type_map.get(key)
            if ct is not None:
                ctypes[ct] = base64.b64decode(b64_data)

        if ctypes:
            content = ClipboardContent(types=ctypes)
            self.sync_mgr.reset_dedup_for_restore()
            create_writer().write(content)
            logger.info("paste_%d: %d type(s) written to clipboard", n, len(ctypes))
        else:
            logger.debug("paste_%d: no writable types", n)

    def _paste_plain(self) -> None:
        """Paste plain text from the most recent clipboard entry."""
        import base64

        items = self.clipboard_history.get_all()
        for entry in items:
            types = entry.get("types", {})
            text_b64 = types.get("TEXT") or types.get("HTML") or types.get("RTF")
            if not text_b64:
                continue
            try:
                decoded = base64.b64decode(text_b64).decode("utf-8")
            except Exception:
                continue
            content = ClipboardContent(types={_CT.TEXT: decoded.encode("utf-8")})
            self.sync_mgr.reset_dedup_for_restore()
            create_writer().write(content)
            logger.info("paste_plain: text written to clipboard (%d chars)", len(decoded))
            return

        logger.debug("paste_plain: no text content in history")

    def _toggle_monitor(self) -> None:
        """Toggle clipboard monitoring on/off via the global hotkey."""
        enabled = not self.cfg.sync_enabled
        # An explicit toggle outranks a pending timed pause (see
        # _on_systray_toggle).
        self._clear_pause_state()
        self.sync_mgr.set_enabled(enabled)
        self.cfg.sync_enabled = enabled
        self._save_cfg_encrypted()
        self._set_systray_syncing(enabled)
        self._notify("notify_sync", T("ui.clipboard_sync"),
                     T("notify.sync_active") if enabled else T("notify.sync_paused"))
        logger.info("Sync %s (toggle_monitor hotkey)", "enabled" if enabled else "paused")

    # ── Callback implementations ──────────────────────────────────

    def _on_local_sync(self, msg) -> None:
        # "Plain text only": strip rich-text formats from OUTGOING clips so
        # peers always receive/paste plain text (this device's clipboard and
        # history keep the full-fidelity copy, mirroring the sensitivity
        # filter's local-original policy).  Stripping the message — rather
        # than the platform write on either end — keeps message bytes and
        # written bytes identical, so the receiver's read-back dedup cannot
        # echo the clip back as a new capture.
        if getattr(self.cfg, "plain_text_only", False):
            msg.content = strip_rich_formats(msg.content)
        if self.content_filter.is_active and self.content_filter.is_sensitive(msg.content):
            sensitivity = self.content_filter.describe_sensitivity(msg.content)
            logger.info("Filtering sensitive content: %s", sensitivity)
            # Local history intentionally keeps the original clip (you should
            # see what you copied); only what leaves this device is redacted.
            # Tell the user the peer received [FILTERED] placeholders instead.
            msg.content = self.content_filter.filter_content(msg.content)
            now = time.monotonic()
            if now - getattr(self, "_last_filter_warn", 0.0) > 10.0:
                self._last_filter_warn = now
                if self._web_has_clients():
                    self._web_toast(T("filter.sender_blocked"), 3000)
                else:
                    self._notify("notify_sync", T("ui.clipboard_sync"),
                                 T("filter.sender_blocked"))
        data = encode_message(msg)
        if len(data) > MAX_FRAME_SIZE:
            size_mb = len(data) / (1024 * 1024)
            logger.warning(
                "Clipboard content too large to sync: %.1f MB (limit: %d MB)",
                size_mb, MAX_FRAME_SIZE // (1024 * 1024),
            )
            self._notify(
                "notify_sync",
                T("notify.sync_skipped"),
                T("sync.oversize", size=size_mb),
            )
            return
        self.transport_mgr.broadcast(data)
        # Internet mode mirrors the frame to public-relay channels so paired
        # peers outside the LAN receive it too (LAN delivery stays primary;
        # receivers' dedup collapses double deliveries).
        self._relay_publish_frame(data)

    def _on_peer_message(self, msg, peer_id: str | None = None,
                         *, via_relay: bool = False) -> None:
        """Route one decoded frame.  *via_relay* marks frames that arrived
        over the public relay so a device_pong echoes back on the same channel
        (device_* probes answer on the transport they came in on)."""
        msg_type = getattr(msg, "msg_type", "clipboard")
        # Round 17: any frame from a peer is an "active" signal — retry its
        # offline queue (cheap no-op when the peer has nothing queued).
        try:
            self._delivery_peer_active(peer_id)
        except Exception:
            logger.debug("delivery retry on peer message failed", exc_info=True)
        if msg_type in DEVICE_PROBE_MSG_TYPES:
            try:
                self._handle_device_probe(
                    msg_type, getattr(msg, "_raw_payload", {}) or {},
                    peer_id or "", via_relay)
            except Exception:
                logger.debug("device probe handling failed", exc_info=True)
            return
        if msg_type == "nav_url":
            url = getattr(msg, "_raw_payload", {}).get("url", "") or ""
            # Only ever open http/https from a peer. Anything else (file://,
            # custom OS schemes) would let a peer launch local handlers.
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                import webbrowser
                logger.info("Opening URL from peer: %s", url[:80])
                webbrowser.open(url)
                self._web_toast(f"{T('nav_url.title')}: {url[:120]}")
            else:
                logger.warning("Ignoring unsafe nav_url from peer: %s", url[:80])
            return
        if msg_type == "update_request":
            self._handle_update_request(peer_id)
            return
        # Nearby-chat JSON frames take precedence: they are consent-gated and
        # must never be routed into clipboard sync or file transfers.
        if msg_type in CHAT_MSG_TYPES:
            raw_payload = getattr(msg, "_raw_payload", {})
            # Per-peer send closure: LAN P2P + (for internet-paired peers) a
            # relay mirror, so replies/accepts/pongs back to a remote chat
            # partner also cross networks.  Empty peer_id (broadcast invites
            # to unpaired LAN devices) stays a plain LAN broadcast.
            send_fn = self._chat_send_fn(peer_id)
            try:
                fp_short = ChatManager.shorten_fingerprint(
                    self.transport_mgr.get_peer_fingerprint(peer_id or ""),
                )
            except Exception:
                fp_short = ""
            accepted = self.chat_mgr.handle_message(
                msg_type, raw_payload, peer_id or "", fp_short, send_fn,
            )
            # Round 17: a chat frame the chat layer processed earns a relay_ack
            # so the internet sender can mark its message delivered.
            if accepted and peer_id:
                self._maybe_send_relay_ack(msg, peer_id)
            return
        # Binary chunks: offer them to the chat layer first; only fall through
        # to clipboard file transfers when the chat layer did not claim them.
        if msg_type == "file_chunk":
            raw_payload = getattr(msg, "_raw_payload", {})
            if peer_id:
                send_fn = (lambda data, pid=peer_id: self.transport_mgr.send_to_peer(pid, data))
            else:
                send_fn = self.transport_mgr.broadcast
            try:
                if self.chat_mgr.handle_binary_chunk(raw_payload, peer_id or "", send_fn):
                    return
            except Exception:
                logger.debug("chat handle_binary_chunk raised", exc_info=True)
        if msg_type in FILE_TRANSFER_MSG_TYPES:
            raw_payload = getattr(msg, "_raw_payload", {})
            # Respond only to the sending peer (acks, rejections, progress,
            # chunks) instead of broadcasting to every connected device.
            if peer_id:
                send_fn = (lambda data, pid=peer_id: self.transport_mgr.send_to_peer(pid, data))
            else:
                send_fn = self.transport_mgr.broadcast
            self.file_transfer_mgr.handle_message(
                msg_type, raw_payload, send_fn, peer_id or "",
            )
            return
        if msg_type in PAIRING_MSG_TYPES:
            raw_payload = getattr(msg, "_raw_payload", {})
            self._handle_pairing_message(msg_type, raw_payload, peer_id)
            return
        if msg_type == "relay_enroll":
            self._handle_relay_enroll(
                getattr(msg, "_raw_payload", {}), peer_id)
            return
        # Round 17: internet "delivered" receipt.  Routed here so a relay_ack
        # arriving over the relay OR the LAN (same frame stream) both resolve.
        if msg_type == "relay_ack":
            self._handle_relay_ack(getattr(msg, "_raw_payload", {}), peer_id)
            return
        # AI-config sync (Round 12): paired-only inventory / pull frames.
        if msg_type in AICONFIG_MSG_TYPES:
            self.aicfg_mgr.handle_message(
                msg_type, getattr(msg, "_raw_payload", {}), peer_id or "")
            return
        # "Plain text only": enforce THIS device's preference on incoming
        # clips too (a peer with the toggle off still sends rich text).
        # Stripping before handle_remote_message keeps its dedup bookkeeping
        # consistent with what actually lands on the clipboard, so the local
        # monitor's read-back matches and nothing is re-broadcast.
        if msg_type == "clipboard" and getattr(self.cfg, "plain_text_only", False):
            msg.content = strip_rich_formats(msg.content)
        if msg_type == "clipboard":
            # Round 17: a clipboard frame that was actually ACCEPTED into
            # history earns a relay_ack back to its internet-reachable sender,
            # turning the relay's QoS0 best-effort into a "已送达" receipt.
            accepted = self.sync_mgr.handle_remote_message(msg)
            if accepted and peer_id:
                self._maybe_send_relay_ack(msg, peer_id)
            return
        self.sync_mgr.handle_remote_message(msg)

    def _on_transfer_progress(self, transfer_id: str, progress: float) -> None:
        logger.debug("File transfer %s: %.0f%%", transfer_id[:8], progress * 100)
        # Throttle WebSocket progress pushes: a large transfer emits one event
        # per 256 KB chunk, and without throttling that floods the web server
        # (thousands of WS frames). Push at most ~10 Hz at ~1% granularity,
        # but always push the final 100%.
        now = time.monotonic()
        last_t, last_p = self._last_transfer_progress.get(transfer_id, (0.0, -1.0))
        if progress < 1.0 and (now - last_t < 0.1 and progress - last_p < 0.01):
            return
        self._last_transfer_progress[transfer_id] = (now, progress)
        # Include the live transfer state so the web panel can render
        # "finalizing" (and other states) instead of a stuck "Sending 100%".
        state = "transferring"
        if self.file_transfer_mgr is not None:
            try:
                for t in self.file_transfer_mgr.get_transfers():
                    if t.get("transfer_id") == transfer_id:
                        state = t.get("state", "transferring") or "transferring"
                        break
            except Exception:
                logger.debug("Could not read transfer state for web push", exc_info=True)
        # Direction (up for outgoing / down for incoming) rides the payload so
        # the web UI can render the correct transfer arrow.  The ws_manager
        # convenience method folds it into the transfer_progress event.
        direction = self._transfer_directions.get(transfer_id, "outgoing")
        direction = "up" if direction == "outgoing" else "down"
        self._push_web("broadcast_transfer_progress",
                       transfer_id, progress, state, direction)

    def _reject_incoming_transfer(self, transfer_id: str, send_fn) -> None:
        """Reject an incoming transfer and forget its direction entry.

        Rejected transfers never fire ``_on_transfer_complete`` (rejection is
        intentionally silent for the receiver), so without popping here the
        ``_transfer_directions`` dict would accumulate one entry per rejected
        request forever.
        """
        self._transfer_directions.pop(transfer_id, None)
        self.file_transfer_mgr.reject_transfer(transfer_id, send_fn)

    def _on_transfer_complete(self, transfer_id: str, success: bool, cancelled: bool = False,
                              status: str = "") -> None:
        logger.info("File transfer %s: %s (status=%s, cancelled=%s)",
                    transfer_id[:8], "complete" if success else "failed",
                    status or "unknown", cancelled)
        # Send confirmations are only meaningful for OUTGOING transfers.  The
        # receiver of an incoming file already gets "File received" from
        # _on_file_received; a failed download must not be reported as if this
        # device were the sender ("File transfer failed").
        direction = self._transfer_directions.pop(transfer_id, "outgoing")
        if cancelled:
            # A user-initiated cancel is NOT a failure — never log/push it as one.
            self._notify("notify_transfer", T("ui.file_transfer"), T("transfer.cancelled"))
        elif success:
            if direction == "outgoing":
                self._notify("notify_transfer", T("ui.file_transfer"), T("transfer.send_success"))
        else:
            self._notify("notify_transfer", T("ui.file_transfer"),
                         self._transfer_failure_message(status, direction))
        self._last_transfer_progress.pop(transfer_id, None)
        # Remove any temp zip archive created for a folder/multi-file send.
        tmp = self._zip_cleanup.pop(transfer_id, None)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self._push_web("broadcast_transfer_complete", transfer_id, success, cancelled)

    def _transfer_failure_message(self, status: str, direction: str) -> str:
        """Map a transfer status to a specific, localized failure message.

        Unknown/internal statuses fall back to the generic send/receive error so
        the user always sees something meaningful instead of a blanket "failed".
        """
        key = {
            "error_disk": "transfer.err_disk",
            "error_size_mismatch": "transfer.err_size_mismatch",
            "error_missing_chunks": "transfer.err_missing_chunks",
            "error_security": "transfer.err_security",
            "peer_offline": "transfer.err_peer_offline",
            "error_timeout": "transfer.err_timeout",
            "rejected": "transfer.rejected",
        }.get(status)
        if key:
            return T(key)
        return T("transfer.send_failed") if direction == "outgoing" else T("transfer.receive_failed")

    def _on_file_received(self, transfer_id: str, saved_path: str, file_name: str) -> None:
        if self.file_transfer_mgr.take_received_kind(transfer_id) == "update":
            logger.info("Update blob received from peer: %s", _mask_path(saved_path))
            # Marshal onto the main thread: this callback fires on a network
            # recv thread, and the install path tears the process down — a
            # sys.exit() here would only kill the recv thread and leave the
            # update half-applied until the next manual quit.
            try:
                self.root.after(
                    0, lambda p=saved_path: self._finish_update_install(
                        p, None, "p2p"),
                )
            except Exception:
                logger.debug("Could not schedule update install", exc_info=True)
            return
        logger.info("File received: %s -> %s",
                    _mask_file_name(file_name), _mask_path(saved_path))
        self._notify("notify_transfer", T("notify.file_received"),
                     T("transfer.received", name=file_name))
        self._play_transfer_sound()

    def _on_web_upload(self, file_name: str, file_size: int, saved_path: str) -> None:
        """A phone uploaded a file to this computer via the web companion."""
        logger.info("Web upload received: %s (%d bytes) -> %s",
                    _mask_file_name(file_name), file_size, _mask_path(saved_path))
        transfer_id = ""
        if self.file_transfer_mgr is not None:
            try:
                transfer_id = self.file_transfer_mgr.record_web_upload(
                    file_name, file_size, saved_path)
            except Exception:
                logger.debug("record_web_upload failed", exc_info=True)
        self._notify("notify_transfer", T("notify.file_received"),
                     T("transfer.received", name=file_name))
        self._play_transfer_sound()
        # Tell connected web clients so the transfers panel refreshes without
        # a manual reload (the web upload never ran through the P2P manager,
        # which normally broadcasts this itself).
        if transfer_id:
            self._push_web("broadcast_transfer_complete", transfer_id, True)

    def _play_transfer_sound(self) -> None:
        """Play the transfer notification sound when sound is enabled.

        The master ``notifications_enabled`` switch silences sound too: a user
        who disabled all notifications shouldn't be startled by audio.  Both
        flags must be on for the sound to play.
        """
        if not getattr(self.cfg, "sound_enabled", False):
            return
        if not self._notifications_enabled():
            return
        try:
            notification_mgr.play_sound()
        except Exception:
            logger.debug("play_sound failed", exc_info=True)

    def _on_transfer_request(self, transfer_id: str, file_name: str, file_size: int,
                             mime_type: str, send_fn) -> None:
        logger.info("File request: %s (%d bytes, %s)", file_name, file_size, mime_type)
        self._transfer_directions[transfer_id] = "incoming"
        sender = self._sender_name_for_send_fn(send_fn)
        # Notification body carries the file name and sender when available so
        # the user can decide whether to accept without opening the dialog.
        if file_name and sender:
            body = f"{file_name} — {sender}"
        elif file_name:
            body = file_name
        elif sender:
            body = sender
        else:
            body = T("transfer.incoming_title")
        self._notify("notify_transfer", T("transfer.incoming"), body)
        self.root.after(0, lambda: self._show_transfer_request_dialog(
            transfer_id, file_name, file_size, mime_type, send_fn, sender_name=sender))

    def _sender_name_for_send_fn(self, send_fn) -> str:
        """Best-effort resolve the sending peer's display name from a send_fn.

        The P2P send_fn is a closure capturing the peer id as its first
        default argument; broadcast send_fns (relay path) carry no peer
        identity and return an empty string.
        """
        try:
            defaults = getattr(send_fn, "__defaults__", None)
            if not defaults:
                return ""
            peer_id = defaults[0]
            if not peer_id:
                return ""
            for pid, name in self.transport_mgr.get_connected_peers_with_names():
                if pid == peer_id:
                    return name
            for peer in self.pairing_mgr.get_known_peers():
                if peer.device_id == peer_id:
                    return peer.device_name
        except Exception:
            logger.debug("Could not resolve sender name from send_fn", exc_info=True)
        return ""

    def _show_transfer_request_dialog(self, transfer_id, file_name, file_size,
                                       mime_type, send_fn, sender_name: str = ""):
        # ── Webview mode: push dialog to web UI ────────────────────
        if self._is_webview():
            # The dialog round-trip waits up to two minutes for a human;
            # run it on a worker thread so the Tk main loop (hotkeys, tray
            # polling, timers) never stalls.  Only the result handling hops
            # back onto the main thread.
            def _on_result(result):
                if result and result.get("action") == "accept":
                    if self.file_transfer_mgr.is_transfer_available(transfer_id):
                        self.file_transfer_mgr.accept_transfer(transfer_id, send_fn)
                    else:
                        self._notify_info(T("transfer.incoming"),
                                          T("transfer.no_longer_available"))
                else:
                    self._reject_incoming_transfer(transfer_id, send_fn)

            self._web_dialog_async(
                "transfer_request",
                _on_result,
                title=T("transfer.incoming"),
                message=T("transfer.incoming_title"),
                file_name=file_name,
                file_size=file_size,
                sender=sender_name or self._sender_name_for_send_fn(send_fn),
            )
            return

        def _accept(manager, dlg):
            if manager.is_transfer_available(transfer_id):
                manager.accept_transfer(transfer_id, send_fn)
            else:
                self._notify_info(T("transfer.incoming"),
                                  T("transfer.no_longer_available"))
            dlg.destroy()

        import platform as _platform
        _is_macos = _platform.system() == "Darwin"
        _is_linux = _platform.system() == "Linux"

        def _fmt_size(n):
            if n >= 1_000_000_000:
                return f"{n/1_000_000_000:.1f} GB"
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f} MB"
            if n >= 1_000:
                return f"{n/1_000:.1f} KB"
            return f"{n} B"

        dw, dh = 400, 240
        if self.root.winfo_viewable():
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = rx + (rw - dw) // 2
            y = ry + (rh - dh) // 2
        else:
            x = (self.root.winfo_screenwidth() - dw) // 2
            y = (self.root.winfo_screenheight() - dh) // 2

        if _is_macos or _is_linux:
            dlg = tk.Toplevel(self.root)
            dlg.title(T("transfer.incoming"))
            dlg.resizable(False, False)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")

            body = tk.Frame(dlg)
            body.pack(fill="both", expand=True, padx=24, pady=20)

            tk.Label(body, text=T("transfer.incoming_title"),
                     font=("Helvetica", 16, "bold")).pack(anchor="w", pady=(0, 12))

            tk.Label(body, text=file_name,
                     font=("Helvetica", 14, "bold")).pack(anchor="w", pady=(0, 4))

            if sender_name:
                tk.Label(body, text=T("transfer.from_device", name=sender_name),
                         font=("Helvetica", 12), fg="#2980B9").pack(anchor="w", pady=(0, 4))

            tk.Label(body, text=T("transfer.incoming_detail",
                                  name=file_name, size=_fmt_size(file_size)),
                     font=("Helvetica", 12), fg="gray").pack(anchor="w", pady=(0, 16))

            btn_row = tk.Frame(body)
            btn_row.pack(fill="x")

            tk.Button(btn_row, text=T("transfer.reject"), width=12,
                      relief="solid", bd=1, fg="#E74C3C",
                      command=lambda: (
                          self._reject_incoming_transfer(transfer_id, send_fn),
                          dlg.destroy(),
                      )).pack(side="left")

            tk.Button(btn_row, text=T("transfer.accept"), width=12,
                      bg="#27AE60", fg="white",
                      command=lambda: _accept(self.file_transfer_mgr, dlg)).pack(side="right")

            dlg.update()
            dlg.transient(self.root)
            try:
                dlg.grab_set()
            except Exception:
                pass
            dlg.protocol("WM_DELETE_WINDOW", lambda: (
                self._reject_incoming_transfer(transfer_id, send_fn),
                dlg.destroy(),
            ))
            dlg.wait_window()
        else:
            import customtkinter as ctk
            dlg = ctk.CTkToplevel(self.root)
            dlg.title(T("transfer.incoming"))
            dlg.resizable(False, False)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")

            body = ctk.CTkFrame(dlg, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=24, pady=20)

            ctk.CTkLabel(
                body, text=T("transfer.incoming_title"),
                font=ctk.CTkFont(size=16, weight="bold"),
            ).pack(anchor="w", pady=(0, 12))

            ctk.CTkLabel(
                body, text=file_name,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(anchor="w", pady=(0, 4))

            if sender_name:
                ctk.CTkLabel(
                    body, text=T("transfer.from_device", name=sender_name),
                    font=ctk.CTkFont(size=12),
                    text_color=("#2980B9", "#5DADE2"),
                ).pack(anchor="w", pady=(0, 4))

            ctk.CTkLabel(
                body, text=T("transfer.incoming_detail", name=file_name, size=_fmt_size(file_size)),
                font=ctk.CTkFont(size=12),
                text_color=("gray50", "gray60"),
            ).pack(anchor="w", pady=(0, 16))

            btn_row = ctk.CTkFrame(body, fg_color="transparent")
            btn_row.pack(fill="x")

            ctk.CTkButton(
                btn_row, text=T("transfer.reject"), width=90, height=34,
                fg_color="transparent", border_width=1,
                text_color=("#E74C3C", "#C0392B"),
                border_color=("#E74C3C", "#C0392B"),
                hover_color=("#FADBD8", "#5B2C2C"),
                command=lambda: (
                    self._reject_incoming_transfer(transfer_id, send_fn),
                    dlg.destroy(),
                ),
            ).pack(side="left")

            ctk.CTkButton(
                btn_row, text=T("transfer.accept"), width=90, height=34,
                fg_color=("#27AE60", "#2ECC71"),
                hover_color=("#1E8449", "#27AE60"),
                command=lambda: _accept(self.file_transfer_mgr, dlg),
            ).pack(side="right")

            dlg.update()
            dlg.transient(self.root)
            try:
                dlg.grab_set()
            except Exception:
                pass
            dlg.protocol("WM_DELETE_WINDOW", lambda: (
                self._reject_incoming_transfer(transfer_id, send_fn),
                dlg.destroy(),
            ))
            dlg.wait_window()

    def _on_peer_found(self, peer_id: str, peer_name: str, address: str, port: int) -> None:
        with self._discovered_lock:
            self._discovered_peers[peer_id] = {
                "name": peer_name, "address": address, "port": port,
            }
        logger.info("Peer discovered: %s (%s) at %s:%d",
                    peer_name, peer_id, address, port)
        self._maybe_auto_connect(peer_id, peer_name, address, port)

    def _maybe_auto_connect(self, hashed_id: str, peer_name: str, address: str, port: int) -> None:
        """Auto-connect to a freshly-discovered peer that is already paired.

        Discovery reports peers by a hashed device id while pairing stores them
        by real device id, so resolve the hash against our known peers first.
        This lets a paired device that was rebooted or restarted re-attach
        automatically instead of sitting at "waiting for pairing".
        """
        if self.transport_mgr is None or self.pairing_mgr is None:
            return
        real_id = None
        for peer in self.pairing_mgr.get_known_peers():
            if Discovery._hash_device_id(peer.device_id) == hashed_id:
                real_id = peer.device_id
                break
        if real_id is None or not self.pairing_mgr.is_peer_paired(real_id):
            return
        if real_id in self.transport_mgr.get_connected_peers():
            return
        # De-duplicate: the discovery thread can fire multiple "found" events
        # for the same peer in quick succession while a connect is in flight.
        with self._discovered_lock:
            if hashed_id in self._auto_connect_pending:
                return
            self._auto_connect_pending.add(hashed_id)
        logger.info("Auto-connecting to paired peer %s (%s) at %s:%d",
                    peer_name, real_id[:12], address, port)
        self.transport_mgr.connect_to_peer(real_id, peer_name, address, port)

    def _on_peer_lost(self, peer_id: str) -> None:
        with self._discovered_lock:
            self._discovered_peers.pop(peer_id, None)
            # Allow a future re-discovery to auto-connect again.
            self._auto_connect_pending.discard(peer_id)
        self.transport_mgr.disconnect_peer(peer_id)
        # Fail any outgoing file transfers destined for this peer immediately
        # instead of letting them hang in "awaiting_ack"/"finalizing" for the
        # full 60-120s timeout.  Discovery reports the *hashed* id here, while
        # transfers are keyed by the real device id — resolve the hash first
        # (same lookup _maybe_auto_connect uses) or the fast-fail matches nothing.
        real_id = None
        try:
            if self.pairing_mgr is not None:
                for peer in self.pairing_mgr.get_known_peers():
                    if Discovery._hash_device_id(peer.device_id) == peer_id:
                        real_id = peer.device_id
                        break
        except Exception:
            logger.debug("Failed to resolve hashed peer id", exc_info=True)
        resolved_id = real_id or peer_id
        if self.file_transfer_mgr is not None:
            self.file_transfer_mgr.fail_peer_transfers(resolved_id)
        # Nearby chat sessions are keyed by the real device id (extracted
        # from the peer cert), so mark the resolved id disconnected too.
        if getattr(self, "chat_mgr", None) is not None:
            try:
                self.chat_mgr.mark_peer_disconnected(resolved_id)
            except Exception:
                logger.debug("chat mark_peer_disconnected failed", exc_info=True)

    def _snapshot_discovered_peers(self) -> dict:
        """Return a thread-safe snapshot of the discovered peers dict.

        The discovery thread mutates ``_discovered_peers`` under the lock while
        web-server threads may read it (via broadcast_devices / _send_snapshot);
        returning a copy under the lock avoids "dictionary changed size during
        iteration" races.
        """
        with self._discovered_lock:
            return dict(self._discovered_peers)

    # Seconds an auto-generated pairing-code notification is held before it
    # fires, so a chat invite (arriving on the same connection) can cancel it.
    # Real pairings notify after this delay; the code is stored immediately.
    PAIRING_NOTIFY_DEBOUNCE = 1.2

    def _on_new_pairing(self, peer_id: str, code: str, peer_name: str) -> None:
        prev = self._notified_pairings.get(peer_id)
        if prev == code:
            return
        self._notified_pairings[peer_id] = code
        # Chat connections reach unpaired peers and must NOT surface a pairing
        # prompt — chat has its own invite/accept + fingerprint consent.  The
        # shared code is generated synchronously at connection-accept time, but
        # a chat invite arrives on the SAME connection within ~a second, so
        # hold the notification briefly; the chat handler cancels it before the
        # user ever sees a pairing request.  Real pairings simply notify ~1s
        # later (the code is already stored in the pairing manager).
        timer = threading.Timer(
            self.PAIRING_NOTIFY_DEBOUNCE,
            lambda: self._pairing_flush_notify(peer_id, code, peer_name),
        )
        timer.daemon = True
        self._pairing_notify_timers[peer_id] = timer
        timer.start()

    def _pairing_flush_notify(self, peer_id: str, code: str, peer_name: str) -> None:
        """Fire the debounced pairing notification (see _on_new_pairing)."""
        self._pairing_notify_timers.pop(peer_id, None)
        # A chat invite (or a resolved pairing) superseded this code during the
        # debounce window — skip the notification entirely.
        if self._notified_pairings.get(peer_id) != code:
            return
        # Track the request so the devices refresh can surface "expired"
        # instead of letting the row vanish silently after the 5-minute window.
        try:
            self._pairing_req_track[peer_id] = {
                "code": code, "peer_name": peer_name,
                "first_seen": time.time(),
            }
        except Exception:
            logger.debug("Could not track pairing request", exc_info=True)
        self._notify(
            "notify_pairing",
            "Pairing Request",
            T("notify.pairing_request", name=peer_name, code=code),
        )
        # With desktop notifications off, the request is invisible in classic
        # mode and would expire silently — open the dashboard once (or toast in
        # webview mode) so the user actually sees it.
        notifications_active = (
            getattr(self.cfg, "notify_pairing", True)
            and getattr(notification_mgr, "enabled", True)
        )
        if not notifications_active:
            self._pairing_notify_fallback(peer_name, code)
        self._push_web("broadcast", "pairing_request", {
            "peer_id": peer_id, "peer_name": peer_name, "code": code,
            # SAS shown on BOTH devices during pairing — the user compares
            # them before confirming (defeats pairing-code MITM).
            "sas": self._pairing_sas(peer_id),
        })
        # Do NOT force-open the dashboard here: that pops a new window on
        # every pairing request even when the user is already in the web UI.
        # The pairing request is pushed over WebSocket and shown in the
        # device page; the OS notification carries the code as well.

    def _pairing_notify_fallback(self, peer_name: str, code: str) -> None:
        """Surface an incoming pairing request when desktop notifications are off."""
        msg = T("notify.pairing_request", name=peer_name, code=code)
        if self._is_webview():
            self._web_toast(msg, 6000)
            return
        # Classic mode: open the dashboard once so the request is visible.
        if not self._pairing_dashboard_opened:
            self._pairing_dashboard_opened = True
            self.root.after(0, self.open_dashboard)

    # ═══════════════════════════════════════════════════════════════
    # Phase 7: Apply config
    # ═══════════════════════════════════════════════════════════════

    def _apply_config(self) -> None:
        cfg = self.cfg
        notification_mgr.enabled = cfg.notifications_enabled
        # Notifications are in-app web toasts (no OS channel), so no
        # platform-availability check is needed — a toast renders whenever a
        # web page is attached, regardless of notify-send/pystray support.
        if cfg.log_level:
            level = getattr(logging, cfg.log_level.upper(), None)
            if level is not None:
                # Only change console handler; file handler stays at DEBUG
                for h in logging.getLogger().handlers:
                    if h is _console_handler:
                        h.setLevel(level)
                        break

    def _on_web_settings_change(self, updated: dict, special: dict | None = None) -> dict | None:
        """Apply settings changed via the web UI to live services.

        The config has already been mutated and persisted by the settings
        API; here we push each change into the running components so the
        new value takes effect immediately (no restart required).

        *special* carries action keys (password, clear_password,
        factory_reset, regenerate_web_token, clear_web_token) that are not
        plain config fields.  The returned dict (if any) is merged into the
        HTTP response so the client can update its local state.
        """
        response: dict = {}
        special = special or {}
        # A language change in the web UI must also switch the backend T()
        # locale — otherwise desktop/webview dialogs (QR, send-URL, transfer
        # requests) keep showing the previous language's titles.
        if "language" in updated:
            set_locale(str(updated["language"]))
        if "sync_enabled" in updated:
            enabled = bool(updated["sync_enabled"])
            # An explicit toggle outranks a pending timed pause (see
            # _on_systray_toggle).
            self._clear_pause_state()
            self.sync_mgr.set_enabled(enabled)
            if self.systray is not None:
                self._set_systray_syncing(enabled)
        if "internet_sync_enabled" in updated:
            self._apply_internet_sync_enabled(updated["internet_sync_enabled"])
        elif "relay_brokers" in updated and self._relay is not None:
            # Broker list edited while the relay is live — recycle it so the
            # new endpoints take effect immediately (no restart needed).
            try:
                self._relay.restart()
            except Exception:
                logger.debug("relay restart after broker change failed",
                             exc_info=True)
        if "netpair_password" in updated and self._relay is not None:
            # Pairing passphrase set/cleared — re-derive the netpair channel
            # keys so the change applies to live traffic immediately.
            try:
                self._relay.refresh_channels()
            except Exception:
                logger.debug(
                    "relay refresh_channels after netpair_password change "
                    "failed", exc_info=True)
        if "filter_enabled_categories" in updated and self.content_filter is not None:
            self.content_filter.enabled_categories = updated["filter_enabled_categories"]
        if "source_tracking_enabled" in updated and getattr(self, "_monitor", None) is not None:
            self._monitor.set_source_tracking(updated["source_tracking_enabled"])
        if "notifications_enabled" in updated:
            notification_mgr.enabled = bool(updated["notifications_enabled"])
        if "history_max_entries" in updated and self.clipboard_history is not None:
            try:
                self.clipboard_history.MAX_ENTRIES = int(updated["history_max_entries"])
            except (TypeError, ValueError):
                logger.warning("Invalid history_max_entries: %s", updated["history_max_entries"])
        if "appearance_mode" in updated:
            try:
                from customtkinter import set_appearance_mode
                set_appearance_mode(updated["appearance_mode"])
            except Exception:
                logger.debug("Failed to apply appearance_mode live", exc_info=True)
        if "log_level" in updated:
            level = getattr(logging, updated["log_level"].upper(), None)
            if level is not None:
                for h in logging.getLogger().handlers:
                    if h is _console_handler:
                        h.setLevel(level)
                        break

        # ── Live-apply the remaining fields so changes take effect
        # immediately instead of only after a restart ─────────────
        if "auto_start" in updated:
            try:
                if updated["auto_start"]:
                    enable_autostart()
                else:
                    disable_autostart()
            except Exception:
                logger.debug("Failed to apply auto_start live", exc_info=True)

        if "web_enabled" in updated and self.web_server is not None:
            try:
                if updated["web_enabled"]:
                    if not self.web_server.is_running:
                        self.web_server.start()
                elif self.cfg.ui_backend == "webview":
                    # The webview dashboard IS served by this server, so keep
                    # it running and only stop serving non-local clients (the
                    # companion's job) — WebServer._companion_client_ok()
                    # enforces that. Otherwise the dashboard breaks the moment
                    # the companion is turned off.
                    logger.info("Web companion off; keeping server up for the "
                                "local webview dashboard")
                elif self.web_server.is_running:
                    self.web_server.stop()
            except Exception:
                logger.debug("Failed to apply web_enabled live", exc_info=True)

        if "sync_debounce" in updated and self.sync_mgr is not None:
            try:
                self.sync_mgr._sync_debounce = max(0.05, float(updated["sync_debounce"]))
            except (TypeError, ValueError):
                pass

        if "retry_capture_enabled" in updated and self.sync_mgr is not None:
            self.sync_mgr._retry_enabled = bool(updated["retry_capture_enabled"])

        if "hotkeys_enabled" in updated:
            try:
                if bool(updated["hotkeys_enabled"]):
                    if self.hotkey_mgr is None:
                        self._wire_hotkeys()
                    elif not self.hotkey_mgr.running:
                        self.hotkey_mgr.start()
                elif self.hotkey_mgr is not None:
                    self.hotkey_mgr.stop()
                    self.hotkey_mgr = None
            except Exception:
                logger.debug("Failed to apply hotkeys_enabled live", exc_info=True)

        if "clipboard_poll_interval" in updated and getattr(self, "_monitor", None) is not None:
            try:
                val = max(0.1, float(updated["clipboard_poll_interval"]))
                _mon = self._monitor
                if hasattr(_mon, "_poll_interval"):
                    _mon._poll_interval = val
                if hasattr(_mon, "_idle_poll_interval"):
                    _mon._idle_poll_interval = max(val * 2.0, 2.0)
            except (TypeError, ValueError):
                pass

        if "low_memory_mode" in updated and getattr(self, "_monitor", None) is not None:
            try:
                base = float(getattr(self.cfg, "clipboard_poll_interval", 1.0) or 1.0)
                val = max(base, 2.0) if updated["low_memory_mode"] else base
                _mon = self._monitor
                if hasattr(_mon, "_poll_interval"):
                    _mon._poll_interval = val
            except Exception:
                logger.debug("Failed to apply low_memory_mode live", exc_info=True)

        if "dedup_method" in updated:
            from internal.clipboard import dedup as _dedup_mod
            _dedup_mod.DEDUP_ALGO = updated["dedup_method"] or "sha256"
        if "history_max_age_days" in updated and self.clipboard_history is not None:
            try:
                from internal.clipboard import history_db as _history_db
                _history_db.set_max_age_days(updated.get("history_max_age_days") or 0)
            except (TypeError, ValueError):
                logger.debug("Invalid history_max_age_days: %s",
                             updated.get("history_max_age_days"))

        if "max_reconnect_attempts" in updated and self.transport_mgr is not None:
            try:
                self.transport_mgr._max_reconnect_attempts = max(1, int(updated["max_reconnect_attempts"]))
            except (TypeError, ValueError):
                pass

        # AI-config sync (Round 12): the watch list changed via web settings —
        # recollect and rebroadcast the inventory.  (The dedicated
        # POST /api/aiconfig/paths editor also triggers on_watch_list_changed
        # itself; this branch covers the same field arriving through the
        # main POST /api/settings form.)
        if "ai_config_paths" in updated and self.aicfg_mgr is not None:
            try:
                self.aicfg_mgr.on_watch_list_changed()
            except Exception:
                logger.debug("aiconfig watch-list refresh failed", exc_info=True)

        if "transfer_timeout" in updated and self.file_transfer_mgr is not None:
            try:
                self.file_transfer_mgr._transfer_timeout = max(30.0, float(updated["transfer_timeout"]))
            except (TypeError, ValueError):
                pass

        if "file_receive_dir" in updated and self.file_transfer_mgr is not None:
            try:
                from pathlib import Path
                new_dir = (updated["file_receive_dir"] or "").strip()
                if new_dir:
                    d = Path(new_dir)
                    d.mkdir(parents=True, exist_ok=True)
                    self.file_transfer_mgr._output_dir = d
                    # Keep the web upload landing dir in sync with the P2P one,
                    # or a phone upload would go to the new dir while the Files
                    # list / download / delete still target the old startup dir.
                    if self.web_server is not None:
                        self.web_server._upload_dir = d
                    # Nearby-chat received files follow the same receive dir;
                    # update the ChatManager too so new chat downloads confine
                    # against the current root (older files stay under their
                    # original root and are still served).
                    if getattr(self, "chat_mgr", None) is not None:
                        self.chat_mgr.set_receive_dir(str(d))
            except Exception:
                logger.debug("Failed to apply file_receive_dir live", exc_info=True)

        # ── Special actions (not plain config fields) ────────────
        if "regenerate_web_token" in special:
            import secrets
            self.cfg.web_token = secrets.token_urlsafe(16)
            self._save_cfg_encrypted()
            # Echo the fresh token back so the client can re-initialize its API
            # session in place (a reload would carry the stale URL token and hit
            # a 403 dead-end).
            response["token_updated"] = True
            response["web_token"] = self.cfg.web_token
            logger.info("Web token regenerated")

        if "clear_web_token" in special:
            self.cfg.web_token = ""
            self._save_cfg_encrypted()
            response["token_updated"] = True
            response["web_token"] = ""
            logger.info("Web token cleared")

        if "password" in special and special["password"]:
            self.cfg.encryption_enabled = True
            self.cfg.encryption_password = special["password"]
            self._save_cfg_encrypted()
            response["password_set"] = True
            logger.info("Encryption password set via web UI")

        if "clear_password" in special:
            self.cfg.encryption_password = ""
            self.cfg.encryption_password_hash = ""
            self._save_cfg_encrypted()
            response["password_set"] = False
            logger.info("Encryption password cleared via web UI")

        if "set_translate_key" in special:
            key = special["set_translate_key"]
            if isinstance(key, str) and key.strip():
                self.cfg.translate_api_key = key.strip()
                self._save_cfg_encrypted()
                response["translate_key_set"] = True
                logger.info("Translation API key set via web UI")
            else:
                # Blank/whitespace key means "clear it".
                self.cfg.translate_api_key = ""
                self._save_cfg_encrypted()
                response["translate_key_set"] = False
                logger.info("Blank translation API key treated as cleared")

        if "clear_translate_key" in special:
            self.cfg.translate_api_key = ""
            self._save_cfg_encrypted()
            response["translate_key_set"] = False
            logger.info("Translation API key cleared via web UI")

        if "factory_reset" in special:
            self.root.after(0, self._do_factory_reset)
            response["ok"] = True

        return response if response else None

    def _do_factory_reset(self) -> None:
        """Delete all ClipSync data files and restart the application.

        Runs on the Tk main thread (scheduled via ``root.after``) so the
        process can cleanly relaunch itself after deleting its own config.
        """
        import subprocess
        import sys

        from internal.config.config import _config_dir
        config_dir = _config_dir()
        deleted = []
        # favorites.json is the legacy favorites store: the web API migrates it
        # into an empty favorites.db, so deleting only the DB would let a stale
        # legacy file resurrect every favorite (and its groups) on the next
        # launch.  Delete it here too for a truly clean slate.
        for fname in ("config.json", "clipboard_history.json",
                      "clipboard_history.db",
                      # WAL sidecars MUST go with the DB: the history DB runs
                      # in WAL mode with one long-lived connection, so both
                      # files exist while the app is running.  Deleting only
                      # the .db leaves the stale -wal behind; SQLite then
                      # replays its committed frames into the fresh empty DB
                      # on next start — resurrecting the very history the
                      # factory reset was supposed to destroy.
                      "clipboard_history.db-wal", "clipboard_history.db-shm",
                      "favorites.db",
                      "favorites.json", "clipsync.log"):
            fpath = config_dir / fname
            try:
                if fpath.exists():
                    fpath.unlink()
                    deleted.append(fname)
            except OSError as e:
                logger.warning("Factory reset: failed to delete %s: %s", fpath, e)
        for pattern in (".config_tmp_*.json", ".history_tmp_*.json",
                        # Quarantine copies hold the OLD identity / private
                        # key / clipboard rows — a clean slate removes them.
                        "config.json.corrupt-*",
                        "clipboard_history.db.corrupt-*"):
            for tmpf in list(config_dir.glob(pattern)):
                try:
                    tmpf.unlink()
                except OSError:
                    pass
        logger.info("Factory reset: deleted %s; restarting", deleted or "no files")

        # Browser-side state (webview localStorage: group registry, mutes,
        # theme, onboarding flag) lives outside the config dir and cannot be
        # deleted here.  Write a one-shot marker the web server turns into
        # __CLIPSYNC_RESET__ on the next page load, so the frontend clears its
        # stale localStorage too.  A companion "web_fresh_pending" marker
        # re-surfaces the web onboarding wizard exactly once (kept in sync with
        # the desktop language picker, which config deletion re-arms) — not on
        # every subsequent refresh.
        try:
            (config_dir / "factory_reset_pending").write_text("1", encoding="utf-8")
            (config_dir / "web_fresh_pending").write_text("1", encoding="utf-8")
        except OSError:
            logger.debug("Factory reset: could not write reset marker", exc_info=True)

        # Remove the single-instance lock so the new process can start,
        # spawn a fresh instance, then exit this one without re-saving config.
        try:
            (config_dir / ".lock").unlink()
        except OSError:
            pass
        # Factory reset exits WITHOUT calling shutdown(), so anything shutdown
        # would normally tear down must be cleaned here — close the dashboard
        # window so the restart opens a fresh one.
        if self.webview_win is not None:
            try:
                self.webview_win.stop()
            except Exception:
                logger.debug("Factory reset: webview stop failed", exc_info=True)
        # Frozen (PyInstaller) builds: sys.argv[0] == sys.executable, so keep
        # only sys.argv[1:] to avoid a stray duplicate exe argument.
        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv
        # Spawn with an isolated temp dir so the child's onefile _MEI
        # extraction can't be raced/removed by this process's exit cleanup.
        self._spawn_restart_process(args)
        # Do NOT re-save the deleted config during shutdown — shutdown() would
        # otherwise recreate config.json with the OLD device identity/peers.
        self._skip_save_on_shutdown = True
        if self.root:
            self.root.quit()
        sys.exit(0)

    def _spawn_restart_process(self, args: list[str]) -> None:
        """Start a fresh ClipSync instance (factory reset / restart).

        Frozen PyInstaller ONE-FILE builds extract to ``%TEMP%\\_MEI<num>`` and
        clean up stale ``_MEI*`` dirs on startup / remove their own dir on
        exit.  When this path spawns a child while the current process is
        still tearing down, the two instances' ``_MEI`` cleanups can race and
        delete a LIVE extraction; the surviving process then fails on the next
        lazy import with ``base_library.zip`` not found (seen after factory
        reset + language selection).  Give the child a PRIVATE temp directory
        so its ``_MEI`` extraction never shares the ``_MEI*`` namespace with
        this process.
        """
        import subprocess
        if getattr(sys, "frozen", False):
            import os
            import secrets
            import tempfile as _tf
            child_temp = os.path.join(
                _tf.gettempdir(), "clipsync_restart", secrets.token_hex(6),
            )
            try:
                os.makedirs(child_temp, exist_ok=True)
            except OSError:
                child_temp = None
            if child_temp:
                env = dict(os.environ)
                env["TMP"] = child_temp
                env["TEMP"] = child_temp
                env["TMPDIR"] = child_temp
                try:
                    subprocess.Popen(args, env=env)
                except Exception:
                    logger.warning(
                        "Restart: failed to spawn new process (isolated temp)", exc_info=True,
                    )
                    subprocess.Popen(args)  # fall back to a normal spawn
                return
        try:
            subprocess.Popen(args)
        except Exception:
            logger.warning("Restart: failed to spawn new process", exc_info=True)

    def _restart_app(self) -> None:
        """Spawn a fresh instance and exit this one.

        Used by the web UI's "Restart App" action (and the Modern/Classic
        switch), which in webview mode previously only closed the browser
        window while the app kept running with the old ui_backend.
        """
        # Unlink the single-instance lock BEFORE spawning: the new process may
        # read it while this PID is still alive and bail with "already running".
        try:
            (_config_dir() / ".lock").unlink()
        except OSError:
            pass
        # A restart also skips shutdown(): close the dashboard window so the
        # relaunched instance opens a fresh one.
        if self.webview_win is not None:
            try:
                self.webview_win.stop()
            except Exception:
                logger.debug("Restart: webview stop failed", exc_info=True)
        # In a frozen (PyInstaller) build sys.argv[0] equals sys.executable, so
        # sys.argv[1:] avoids a stray duplicate exe argument; from source argv[0]
        # is the script path and must be kept.
        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv
        self._spawn_restart_process(args)
        # The new instance owns the config now; don't re-save/rewrite it on exit.
        self._skip_save_on_shutdown = True
        # Do NOT sys.exit() from the web handler thread — that raises SystemExit
        # before the HTTP response is sent, so the UI shows a false failure even
        # though the restart worked.  Give the response time to flush, then exit
        # from the main thread instead.
        try:
            if self.root is not None:
                self.root.after(300, self._exit_process)
                return
        except Exception:
            logger.warning("Restart: could not schedule exit", exc_info=True)
        self._exit_process()

    def _exit_process(self) -> None:
        """Stop the Tk loop and exit the process (main thread only)."""
        try:
            if self.root is not None:
                self.root.quit()
        except Exception:
            logger.warning("Could not quit Tk root cleanly", exc_info=True)
        sys.exit(0)

    # ═══════════════════════════════════════════════════════════════
    # Phase 8: UI
    # ═══════════════════════════════════════════════════════════════

    def _create_ui(self) -> None:
        # ── Root window (needed for systray + mainloop in both modes) ─
        self.root = tk.Tk()
        _hide_dock()
        self.root.title(T("ui.app_name"))
        # A withdrawn parent prevents child windows (CTkToplevel / tk.Toplevel)
        # from displaying on macOS and many Linux window managers (GNOME, KDE).
        # Use a 1px fully-transparent root so it stays mapped but invisible.
        if sys.platform in ("darwin", "linux"):
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"1x1+{sw // 2}+{sh // 2}")
            self.root.attributes("-alpha", 0)
        else:
            self.root.geometry("1x1+0+0")
            self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)

        # ── Appearance mode (CTk only) ───────────────────────────────
        if self.cfg.ui_backend == "ctk":
            from customtkinter import set_appearance_mode, set_default_color_theme

            set_appearance_mode(self.cfg.appearance_mode)
            # Use the project's aurora theme (cyan→violet, matching the web UI)
            # instead of CustomTkinter's stock blue.
            try:
                from internal.ui.fonts import theme_file_path

                set_default_color_theme(theme_file_path())
            except Exception:
                logger.debug("Custom CTk theme not found; using stock blue",
                             exc_info=True)

        # ── Platform UI font (CTk dialogs exist in both backends) ─────
        # CTk defaults every font to "Roboto", which is missing on most
        # macOS/Linux installs → Tk falls back to a dated default. Resolve an
        # installed platform UI font and align Tk's default font too, so the
        # CTk dialogs/windows read as native next to the web UI.
        try:
            from internal.ui.fonts import (
                configure_platform_font,
                install_platform_font_patch,
            )

            install_platform_font_patch()
            configure_platform_font(self.root)
        except Exception:
            logger.debug("Platform font setup skipped", exc_info=True)

        # ── HiDPI / fractional scaling ───────────────────────────────
        # CTk renders at 1x on HiDPI Linux/Windows displays unless widget and
        # window scaling are raised to match the monitor DPI.  macOS is left
        # to Tk's native Retina handling (the helper returns 1.0 there).  A
        # detection failure is a no-op (stays at 1x).
        try:
            from internal.ui.fonts import apply_ui_scaling

            apply_ui_scaling(self.root)
        except Exception:
            logger.debug("HiDPI scaling setup skipped", exc_info=True)

        # ── Systray ──────────────────────────────────────────────────
        self.systray = SystrayApp(
            device_name=self.cfg.device_name,
            on_enable_toggle=lambda e: self.root.after(0, self._on_systray_toggle, e),
            on_open_dashboard=self.open_dashboard,
            on_open_settings=self.open_settings,
            on_export_logs=self.export_logs,
            on_show_web_qr=self._show_web_qr,
            on_send_url=lambda: self.root.after(0, self._do_send_url),
            on_check_update=lambda: self.root.after(0, self._check_for_update),
            on_about=lambda: self.root.after(0, self._show_about),
            on_pause_minutes=lambda m: self.root.after(0, self._pause_sync_for_minutes, m),
            on_resume_sync=lambda: self.root.after(0, self._resume_timed_pause),
            on_quit=lambda: self.root.after(0, self.shutdown),
            on_tray_failed=lambda: self._web_toast(
                f"{T('tray.failed_title')}: {T('tray.failed_msg')}"),
        )
        # Seed the parent's tray state so the initial menu matches the config
        # (the macOS subprocess receives it via _push_tray_state once spawned).
        self._set_systray_web_enabled(self.cfg.web_enabled)
        self._set_systray_syncing(self.cfg.sync_enabled)

    # ═══════════════════════════════════════════════════════════════
    # Phase 9: Start services
    # ═══════════════════════════════════════════════════════════════

    def _start_services(self) -> None:
        # On Linux, warn if no clipboard tool (xclip/wl-clipboard) is installed
        if sys.platform == "linux":
            from internal.clipboard.clipboard_linux import check_clipboard_tools
            msg = check_clipboard_tools()
            if msg:
                self.root.after(800, lambda: show_warning(self.root, T("ui.clipboard_unavailable"), msg))

        self.sync_mgr.start()
        try:
            self.transport_mgr.start_server()
        except PortInUseError:
            show_error(
                self.root,
                T("ui.port_in_use"),
                T("ui.port_in_use_msg", port=self.cfg.port),
            )
            sys.exit(1)
        self.discovery.start()
        # Webview mode always needs the web server
        if self.cfg.web_enabled or self.cfg.ui_backend == "webview":
            if not self.cfg.web_token:
                self.cfg.web_token = secrets.token_urlsafe(16)
                self._save_cfg_encrypted()
                logger.info("Generated new web companion token")
            base_port = int(self.cfg.web_port)
            started = bool(self.web_server.start()) and self.web_server.is_running
            if not started:
                for port in range(base_port + 1, base_port + 6):
                    self.cfg.web_port = port
                    started = bool(self.web_server.start()) and self.web_server.is_running
                    if started:
                        self._save_cfg_encrypted()
                        break
            if not started:
                msg = (
                    T("ui.web_start_failed", port=base_port, lo=base_port, hi=base_port + 5)
                )
                show_error(self.root, T("ui.web_companion"), msg)
                self.cfg.web_enabled = False
                self._set_systray_web_enabled(False)
                if self.cfg.ui_backend == "webview":
                    self.cfg.ui_backend = "ctk"
                    self._save_cfg_encrypted()
                return
            # Respect the user's companion setting. In webview mode the server
            # stays up for the local dashboard even when the companion is off
            # (non-local clients are refused by WebServer._companion_client_ok),
            # so don't force it back on here.
            logger.info("Web companion ready (ui_backend=%s, web_enabled=%s)",
                        self.cfg.ui_backend, self.cfg.web_enabled)

    # ═══════════════════════════════════════════════════════════════
    # Phase 10: Background threads
    # ═══════════════════════════════════════════════════════════════

    def _start_threads(self) -> None:
        # Resume a timed sync pause that a restart interrupted (e.g. an
        # auto-update relaunch) before any UI reads the sync state — runs
        # after _create_ui/_start_services so tray + sync_mgr both exist.
        self._restore_timed_pause()
        # Internet (cross-network) sync: bring the public-relay transport up
        # when the setting is on (it stays fully offline otherwise).
        if getattr(self.cfg, "internet_sync_enabled", False):
            try:
                self._start_internet_sync()
            except Exception:
                logger.debug("Internet sync startup failed", exc_info=True)
        updater = threading.Thread(target=self._update_peers_loop, daemon=True)
        updater.start()
        # AI-config sync (Round 12): collect the watch-list inventory once at
        # startup; it is broadcast to connected paired peers and re-sent on
        # each peer connect / watch-list change.
        if self.aicfg_mgr is not None:
            try:
                sent = self.aicfg_mgr.refresh_and_broadcast()
                if sent:
                    logger.info("AI-config inventory broadcast to %d peer(s)", sent)
            except Exception:
                logger.debug("Startup aiconfig inventory failed", exc_info=True)

        logger.info("ClipSync is ready. System tray icon should appear.")

        # Auto-open dashboard on startup
        self.root.after(500, self.open_dashboard)
        # Surface any certificate-changed peers (one non-blocking dialog)
        # once the main loop is running.
        self.root.after(1200, self._prompt_cert_warnings_startup)
        # Probe the hotkey backend once (macOS Accessibility failure kills the
        # listener thread immediately after start()).
        if getattr(self, "_hotkey_running", False) and not getattr(self, "_hotkey_failure_notified", False):
            self.root.after(1500, self._check_hotkey_health)

        if sys.platform == "darwin":
            self._start_macos_tray()
        else:
            tray_thread = threading.Thread(target=self.systray.run, daemon=True)
            tray_thread.start()

    def _start_macos_tray(self) -> None:
        """Start the macOS tray subprocess and its message-poll watchdog."""
        import multiprocessing

        multiprocessing.freeze_support()
        # _spawn_macos_tray schedules the poll loop, so a restart (after the
        # subprocess dies) resumes polling the new pipe automatically.
        self._spawn_macos_tray()

    def _spawn_macos_tray(self) -> None:
        """Spawn the macOS tray subprocess with a fresh pipe.

        Also pushes the current tray state (peers / web / syncing) so the
        freshly-built child menu starts with the right data instead of its
        defaults, then starts polling the new pipe.
        """
        import multiprocessing

        try:
            parent_conn, child_conn = multiprocessing.Pipe()
            notification_mgr.set_pipe(parent_conn)
            self._parent_conn = parent_conn
            self._tray_proc = multiprocessing.Process(
                target=_run_tray,
                args=(self.cfg.device_name, child_conn, os.getpid(), self.cfg.language),
                daemon=True,
            )
            self._tray_proc.start()
            logger.info("macOS tray subprocess started (PID %d)", self._tray_proc.pid)
            self._push_tray_state()
            self.root.after(500, self._poll_macos_tray)
        except Exception:
            logger.exception("Failed to spawn macOS tray subprocess")
            self._parent_conn = None
            self._tray_proc = None

    def _poll_macos_tray(self) -> None:
        """Poll the parent→child pipe for tray actions (main thread, via after).

        When the subprocess dies (EOF / broken pipe) the poll loop stops and
        hands off to ``_on_tray_subprocess_died``, which schedules a restart;
        the restart's ``_spawn_macos_tray`` starts a fresh poll loop.
        """
        if self._shutting_down:
            return
        try:
            while self._parent_conn is not None and self._parent_conn.poll():
                self._handle_tray_msg(self._parent_conn.recv())
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError):
            # Tray subprocess is gone (EOF/broken pipe) — restart it so
            # the app doesn't go headless (no Dock icon, no tray).
            self._on_tray_subprocess_died()
            return
        except Exception:
            # An unexpected handler error must not kill the tray channel
            # for the rest of the session — log and keep polling.
            logger.exception("Unhandled error in tray poll; continuing")
        self.root.after(500, self._poll_macos_tray)

    def _on_tray_subprocess_died(self) -> None:
        """Handle a dead tray subprocess: clear state, then schedule a restart.

        The app has no Dock icon and no tray once the child dies, so it becomes
        headless.  Restart it with a short backoff, up to a bounded number of
        attempts, and stop entirely during shutdown.
        """
        self._parent_conn = None
        self._tray_proc = None
        if self._shutting_down:
            return
        if self._macos_tray_restarts >= self._macos_tray_max_restarts:
            logger.warning(
                "macOS tray subprocess died %d time(s); not restarting again. "
                "The app may be headless — restart ClipSync to recover the tray.",
                self._macos_tray_restarts,
            )
            return
        if self._shutting_down:
            return
        self._macos_tray_restarts += 1
        logger.warning(
            "macOS tray subprocess died; restarting (%d/%d)",
            self._macos_tray_restarts, self._macos_tray_max_restarts,
        )
        try:
            self.root.after(10000, self._spawn_macos_tray)
        except Exception:
            logger.debug("Could not schedule macOS tray restart", exc_info=True)

    def _push_tray_state(self) -> None:
        """Push the current peers / web / syncing state to the tray subprocess.

        On macOS the tray runs in a subprocess whose ``SystrayApp`` is a
        separate instance from ``self.systray``; calling ``set_*`` on
        ``self.systray`` only updates the dormant parent object.  This sends
        the live state over the pipe so the child's menu actually rebuilds.
        Safe to call on any platform / any time — no-op unless the macOS tray
        subprocess is alive, and it never raises.
        """
        if sys.platform != "darwin":
            return
        conn = self._parent_conn
        proc = self._tray_proc
        if conn is None or proc is None or not proc.is_alive():
            return
        try:
            peers = list(getattr(self.systray, "_peers", []) or [])
            web = bool(getattr(self.systray, "_web_enabled", False))
            syncing = bool(getattr(self.systray, "_syncing", True))
            pause_deadline = getattr(self.systray, "_pause_deadline", None)
        except Exception:
            logger.debug("Failed to read systray state", exc_info=True)
            return
        try:
            # Route through notification_mgr.send_pipe so all pipe writers share
            # one lock — Connection.send isn't internally synchronized, and the
            # notification sender thread writes to this same pipe concurrently.
            notification_mgr.send_pipe(("set_peers", peers))
            notification_mgr.send_pipe(("set_web_enabled", web))
            notification_mgr.send_pipe(("set_syncing", syncing))
            notification_mgr.send_pipe(("set_pause_deadline", pause_deadline))
        except Exception:
            # A full pipe (or a subprocess that died between is_alive() and
            # send) must never crash the main thread.
            logger.debug("Failed to push tray state to macOS subprocess", exc_info=True)

    # ── Systray state setters (route through the pipe on macOS) ──────
    # Each keeps updating the parent's dormant SystrayApp so the parent state
    # stays authoritative, then forwards to the subprocess on macOS.  On other
    # platforms the direct call is unchanged.

    def _set_systray_peers(self, peers: list[str]) -> None:
        self.systray.set_peers(peers)
        if sys.platform == "darwin":
            self._push_tray_state()

    def _set_systray_web_enabled(self, enabled: bool) -> None:
        self.systray.set_web_enabled(enabled)
        if sys.platform == "darwin":
            self._push_tray_state()

    def _set_systray_syncing(self, enabled: bool) -> None:
        self.systray.set_syncing(enabled)
        if sys.platform == "darwin":
            self._push_tray_state()

    def _handle_tray_msg(self, msg: tuple) -> None:
        cmd = msg[0]
        if cmd == "toggle_sync":
            self._on_systray_toggle(msg[1])
        elif cmd == "pause_minutes":
            self.root.after(0, self._pause_sync_for_minutes, msg[1])
        elif cmd == "resume_sync":
            self.root.after(0, self._resume_timed_pause)
        elif cmd == "open_dashboard":
            self.open_dashboard()
        elif cmd == "open_settings":
            self.open_settings()
        elif cmd == "export_logs":
            self.export_logs()
        elif cmd == "show_web_qr":
            self._show_web_qr()
        elif cmd == "send_url":
            self.root.after(0, self._do_send_url)
        elif cmd == "check_update":
            self._check_for_update()
        elif cmd == "about":
            self._show_about()
        elif cmd == "tray_failed":
            self._web_toast(f"{T('tray.failed_title')}: {T('tray.failed_msg')}")
        elif cmd == "quit":
            self.shutdown()

    def _update_peers_loop(self) -> None:
        prev_display: list[str] = []
        prev_connected: set[str] = set()
        cleanup_counter = 0
        while not self._stop_updater.is_set():
            # One bad iteration (a transient service error, Tk tearing down
            # during exit) must not kill this daemon thread silently — that
            # would freeze the tray/device status and stop stale-transfer
            # cleanup and auto-update checks for the rest of the session.
            try:
                connected_ids = self.transport_mgr.get_connected_peers()
                # Cache known peers once — reused for display names below
                known_peers = self.pairing_mgr.get_known_peers()
                peer_display = []
                for d in self.get_device_states():
                    if d["paired"]:
                        suffix = "connected" if d["connected"] else "offline"
                    elif d["pairing"]:
                        suffix = "pairing…"
                    elif d["connected"]:
                        suffix = "connected"  # consented chat, not paired
                    else:
                        suffix = "found"
                    peer_display.append(f"{d['name']}  ({suffix})")
                if peer_display != prev_display:
                    prev_display = peer_display
                    self.root.after(0, lambda pd=list(peer_display): self._set_systray_peers(pd))
                    self._push_web("broadcast_devices")

                connected_set = set(connected_ids)
                for pid in connected_set - prev_connected:
                    found = next((p for p in known_peers if p.device_id == pid), None)
                    name = found.device_name if found else pid[:12]
                    self._notify("notify_device_connect",
                                 T("notify.device_connected_title"),
                                 T("notify.device_connected", name=name))
                    # AI-config sync: a freshly connected paired peer missed
                    # our startup inventory broadcast — send it now (the
                    # transport has no on-connect callback; this 3s poll loop
                    # is the existing "peer came up" hook).
                    peer_cfg = self.cfg.peers.get(pid)
                    if peer_cfg is not None and peer_cfg.paired \
                            and self.aicfg_mgr is not None:
                        try:
                            self.aicfg_mgr.send_inventory_to(pid)
                        except Exception:
                            logger.debug("aiconfig inv on connect failed",
                                         exc_info=True)
                for pid in prev_connected - connected_set:
                    found = next((p for p in known_peers if p.device_id == pid), None)
                    name = found.device_name if found else pid[:12]
                    self._notify("notify_device_connect",
                                 T("notify.device_disconnected_title"),
                                 T("notify.device_disconnected", name=name))
                prev_connected = connected_set

                cleanup_counter += 1
                if cleanup_counter >= 10:
                    cleanup_counter = 0
                    try:
                        self.file_transfer_mgr.cleanup_stale_transfers()
                    except Exception:
                        pass

                # Periodic auto-update check (once per ~6 hours, silent unless an
                # update is available).  Fully gated by the auto_update_check
                # setting: OFF means this loop never contacts the network.
                self._maybe_auto_update_check()
            except Exception:
                logger.exception("Device-status loop iteration failed")

            self._stop_updater.wait(3)

    # ═══════════════════════════════════════════════════════════════
    # Phase 11: Event loop
    # ═══════════════════════════════════════════════════════════════

    def run(self) -> None:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    # ═══════════════════════════════════════════════════════════════
    # Shutdown
    # ═══════════════════════════════════════════════════════════════

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._stop_updater.set()
        # Drop the public-relay link before tearing down managers: its
        # receive callbacks route into them.
        try:
            self._stop_internet_sync()
        except Exception:
            logger.debug("Internet sync shutdown failed", exc_info=True)
        logger.info("Shutting down...")
        # Kill the timed-pause auto-resume timer first: firing into a torn-
        # down UI would only log an error, but cancelling is cleaner.
        try:
            self._clear_pause_state()
        except Exception:
            logger.debug("Pause state cleanup failed", exc_info=True)
        self.sync_mgr.stop()
        if getattr(self, "chat_mgr", None) is not None:
            try:
                self.chat_mgr.shutdown()
            except Exception:
                logger.debug("chat shutdown failed", exc_info=True)
        self.discovery.stop()
        self.transport_mgr.stop_server()
        if self.webview_win is not None:
            self.webview_win.stop()
        # Ask any open dashboard window to close itself so we don't leave
        # orphaned browser windows/processes behind when the app quits.
        try:
            self._push_web("broadcast", "close_window", {})
        except Exception:
            logger.debug("Failed to broadcast close_window", exc_info=True)
        if self.web_server:
            self.web_server.stop()

        # Cancel any debounced pairing notifications still in flight (they'd
        # fire against a half-torn-down app otherwise).
        for _pid, timer in list(self._pairing_notify_timers.items()):
            try:
                timer.cancel()
            except Exception:
                pass
        self._pairing_notify_timers.clear()

        if not self._skip_save_on_shutdown:
            for peer in self.pairing_mgr.get_known_peers():
                self.cfg.peers[peer.device_id] = PeerInfo(
                    device_id=peer.device_id,
                    device_name=peer.device_name,
                    public_key_pem=peer.certificate_pem,
                    paired=peer.paired,
                )
            with config_lock:
                self._persist_peer_addresses()
                self._save_cfg_encrypted()

        # Release OS-level resources: Windows message-only window + registered
        # hotkeys, macOS CGEvent tap, Linux pynput listener.
        try:
            if self.hotkey_mgr is not None:
                self.hotkey_mgr.stop()
        except Exception:
            logger.debug("Failed to stop hotkey manager", exc_info=True)

        # Terminate macOS tray subprocess
        if sys.platform == "darwin" and self._tray_proc is not None:
            try:
                self._tray_proc.terminate()
                self._tray_proc.join(timeout=3)
            except Exception:
                pass

        _remove_lock()

        if self.root:
            self.root.quit()

    # ═══════════════════════════════════════════════════════════════
    # Config persistence helpers
    # ═══════════════════════════════════════════════════════════════

    def _make_save_enc(self) -> EncryptionManager | None:
        if not self.cfg.encryption_enabled:
            return None
        if self.cfg.encryption_password:
            self.cfg.encryption_password_hash = _make_password_hash(
                self.cfg.encryption_password,
                self.pairing_mgr.get_identity().fingerprint,
            )
        else:
            self.cfg.encryption_password_hash = ""
        return EncryptionManager(
            self.pairing_mgr.get_identity().fingerprint,
            password=self.cfg.encryption_password,
        )

    def _save_cfg_encrypted(self) -> None:
        save(self.cfg, self._make_save_enc())

    # ═══════════════════════════════════════════════════════════════
    # UI action handlers
    # ═══════════════════════════════════════════════════════════════

    def _track_modal_dialog(self, dlg) -> None:
        """Register a fire-and-forget modal popup (no ``wait_window``).

        These dialogs return to the caller immediately, so nothing releases
        their window-manager grab when they close through an unusual path —
        leaving every other window unclickable.  Binding ``<Destroy>`` makes
        sure the grab is dropped and the GC-guard reference cleared however
        the dialog goes away.
        """
        def _on_destroy(event):
            if event.widget is not dlg:
                return  # child widgets destroy first; only react to the dialog
            try:
                dlg.grab_release()
            except Exception:
                pass
            if getattr(self, "_active_dialog", None) is dlg:
                self._active_dialog = None

        dlg.bind("<Destroy>", _on_destroy)
        # Keep a reference to prevent premature garbage collection on macOS
        self._active_dialog = dlg

    def _show_web_qr(self) -> None:
        """Show a popup window with the web companion QR code."""
        if self._is_webview():
            # Wait for a web client so the pushed QR dialog isn't dropped.
            self._run_when_webview_ready(self._do_show_web_qr)
        else:
            self.root.after(0, self._do_show_web_qr)

    def _do_show_web_qr(self) -> None:
        token = self.cfg.web_token
        port = self.cfg.web_port
        ip = WebServer._get_lan_ip()
        # The QR opens the phone companion page (history / send / files),
        # not the full desktop dashboard.
        if token:
            url = f"http://{ip}:{port}/mobile.html?token={quote(token, safe='')}"
        else:
            url = f"http://{ip}:{port}/mobile.html"

        # ── Webview mode: push QR code to web UI ────────────────────
        if self._is_webview():
            import base64
            from io import BytesIO

            import qrcode
            img = qrcode.make(url)
            img = img.convert("RGB")
            img = img.resize((220, 220))
            buf = BytesIO()
            img.save(buf, "PNG")
            data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            mgr = self.web_server.dialog_mgr if self.web_server else None
            if mgr is not None:
                mgr.push(
                    "qr_code",
                    title=T("web.qr_title"),
                    url=url,
                    qr_data_url=data_url,
                )
            return

        from io import BytesIO

        import customtkinter as ctk
        import qrcode
        from PIL import Image

        dlg = ctk.CTkToplevel(self.root)
        dlg.title(T("web.qr_title"))
        dlg.resizable(False, False)

        w, h = 320, 485
        if self.root.winfo_viewable():
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = rx + (rw - w) // 2
            y = ry + (rh - h) // 2
        else:
            x = (self.root.winfo_screenwidth() - w) // 2
            y = (self.root.winfo_screenheight() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)

        ctk.CTkLabel(
            body, text=T("web.qr_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(0, 12))

        if token:
            img = qrcode.make(url)
            img = img.convert("RGB")
            img = img.resize((220, 220), Image.LANCZOS)
            qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(220, 220))
            qr_label = ctk.CTkLabel(body, image=qr_img, text="")
            qr_label.image = qr_img  # keep reference
            qr_label.pack(pady=(0, 12))
        else:
            ctk.CTkLabel(
                body, text=T("web.no_token"),
                font=ctk.CTkFont(size=14), text_color=("gray50", "gray60"),
            ).pack(pady=(0, 12))

        url_row = ctk.CTkFrame(body, corner_radius=8, fg_color=("gray90", "gray17"))
        url_row.pack(fill="x", pady=(0, 10))
        url_label = ctk.CTkLabel(
            url_row, text=url,
            font=ctk.CTkFont(size=11, family="monospace"), wraplength=200,
            text_color=("gray50", "gray70"),
        )
        url_label.pack(side="left", padx=(12, 6), pady=10)

        def _copy_url():
            dlg.clipboard_clear()
            dlg.clipboard_append(url)
            copy_btn.configure(text=T("web.copied"))
            dlg.after(2000, lambda: copy_btn.configure(text=T("ui.copy")))

        copy_btn = ctk.CTkButton(
            url_row, text=T("ui.copy"), width=50, height=28,
            font=ctk.CTkFont(size=11),
            command=_copy_url,
        )
        copy_btn.pack(side="right", padx=(0, 6))

        # Send a local file straight to the phone: it lands in the web-shared
        # directory the phone page lists, so the phone downloads it from its
        # Files tab within ~5s — no pairing required.
        ctk.CTkButton(
            body, text=T("web.send_file_to_phone"), width=190, height=34,
            font=ctk.CTkFont(size=12),
            fg_color=("#0891B2", "#0E1328"),
            hover_color=("#0EA5C4", "#1A2542"),
            command=lambda: self._send_file_to_phone(dlg),
        ).pack(pady=(0, 8))

        ctk.CTkButton(
            body, text=T("ui.close"), width=100, height=38,
            font=ctk.CTkFont(size=13),
            fg_color=("gray85", "gray20"),
            hover_color=("gray75", "gray30"),
            command=dlg.destroy,
        ).pack(pady=(4, 0))

        dlg.update()
        dlg.transient(self.root)
        try:
            dlg.grab_set()
        except Exception:
            pass
        self._track_modal_dialog(dlg)

    def _send_file_to_phone(self, parent=None) -> None:
        """Send a local file to the phone companion page for download.

        Copies the chosen file into the web-shared directory the phone page
        lists (``/api/files``) and downloads from (``/api/download``).  No
        pairing is involved: the phone sees the file on its next ~5 s poll of
        the Files tab, and can download it straight to the device.
        """
        try:
            import tkinter.filedialog
            path = tkinter.filedialog.askopenfilename(parent=parent)
        except Exception:
            logger.debug("send-to-phone file dialog failed", exc_info=True)
            return
        if not path:
            return
        try:
            import shutil
            from internal.sync.file_transfer import _sanitize_file_name
            from internal.web.server import _get_upload_dir
            dest_dir = _get_upload_dir(self.cfg)
            name = _sanitize_file_name(os.path.basename(path))
            dest = os.path.join(dest_dir, name)
            stem, ext = os.path.splitext(name)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(dest_dir, f"{stem} ({counter}){ext}")
                counter += 1
            shutil.copy2(path, dest)
            self._notify_info(
                T("web.send_file_to_phone"),
                T("web.send_file_to_phone_msg", name=os.path.basename(dest)),
            )
        except Exception:
            logger.warning("send-to-phone copy failed", exc_info=True)
            try:
                self._notify_error(
                    T("web.send_file_to_phone"), T("web.send_file_to_phone_fail"),
                )
            except Exception:
                pass

    def _on_web_action(self, action: dict) -> None:
        """Handle web server control actions from dashboard / settings."""
        act = action.get("action", "")
        if act == "start":
            if not self.web_server:
                return
            if not self.cfg.web_token:
                self.cfg.web_token = secrets.token_urlsafe(16)
                self._save_cfg_encrypted()
            self.web_server.start()
            if not self.web_server.is_running:
                self._notify_error(
                    T("ui.web_companion"),
                    T("ui.web_start_failed2", port=self.cfg.web_port),
                )
                return
            self._set_systray_web_enabled(True)
            logger.info("Web companion started via dashboard")
        elif act == "stop":
            if self.web_server:
                self.web_server.stop()
            self._set_systray_web_enabled(False)
            logger.info("Web companion stopped via dashboard")
        elif act == "restart":
            if self.web_server:
                self.web_server.stop()
                if not self.cfg.web_token:
                    self.cfg.web_token = secrets.token_urlsafe(16)
                    self._save_cfg_encrypted()
                self.web_server.start()
                if not self.web_server.is_running:
                    self._notify_error(
                        T("ui.web_companion"),
                        T("ui.web_start_failed2", port=self.cfg.web_port),
                    )
                    return
                self._set_systray_web_enabled(True)
            logger.info("Web companion restarted via dashboard")

    # ═══════════════════════════════════════════════════════════════

    def export_logs(self) -> None:
        self.root.after(0, self._do_export_logs)

    def _do_export_logs(self) -> None:
        log_path = _get_log_path()
        dest = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Log File As",
            initialfile=f"clipsync_{time.strftime('%Y%m%d_%H%M%S')}.log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
            defaultextension=".log",
        )
        if not dest:
            return
        try:
            shutil.copy2(log_path, dest)
            self._notify_info(T("log.exported_title"), T("log.exported_msg", dest=dest))
            logger.info("Log exported to %s", dest)
        except FileNotFoundError:
            self._notify_warning(
                T("log.not_found_title"),
                T("log.not_found_msg", path=log_path),
            )
        except PermissionError:
            self._notify_error(
                T("ui.error_title"),
                T("log.permission_error_msg", dest=dest),
            )
            logger.error("Permission denied exporting log to %s", dest)
        except OSError as e:
            self._notify_error(T("ui.error_title"), f"{T('ui.export_failed_msg')}{e}")
            logger.error("Failed to export log: %s", e)

    def _check_for_update(self) -> None:
        """Check GitHub for a newer ClipSync release and notify the result.

        Re-entry is guarded (a second click while a check is in flight is a
        no-op), a "checking…" state is shown, and the result always surfaces:
        a desktop notification when enabled, otherwise an info dialog.
        """
        if getattr(self, "_checking_update", False):
            return
        self._checking_update = True
        self._web_toast(T("notify.update_checking"))

        def _present(title: str, message: str) -> None:
            self._web_toast(f"{title}: {message}")
            if not self._notifications_enabled():
                self._notify_info(title, message)

        def _worker():
            from internal.system.updater import check_for_update
            try:
                result = check_for_update()
            except Exception as exc:
                logger.warning("Update check failed: %s", exc)
                result = {}
            finally:
                self._checking_update = False

            def _done():
                if self._shutting_down:
                    return
                if result.get("available"):
                    _present(
                        T("tray.update_available", version=result["latest"]),
                        f"ClipSync {result['latest']}\n{result.get('url', '')}",
                    )
                    self._offer_update_install(result)
                elif result.get("latest"):
                    _present(T("tray.up_to_date"),
                             f"ClipSync {result.get('current', '')}")
                else:
                    _present(T("tray.update_failed"), "ClipSync")

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True, name="update-check").start()

    def _offer_update_install(self, result: dict) -> None:
        """Ask the user to download + install an available update."""
        if ask_yesno(
            self.root,
            T("ui.app_name"),
            T("tray.update_install_prompt", version=result.get("latest", "")),
        ):
            self._download_and_install_update()

    def _download_and_install_update(self, from_peers: bool = True) -> None:
        """Download the latest release, stage it, then apply + restart.

        The download runs on a worker thread (it can take tens of seconds);
        staging + applying run on the main thread, after which the app exits.
        *from_peers* asks connected peers for a cached copy first (M2); it is
        disabled on the P2P-fallback re-entry so an unverifiable peer blob
        cannot ping-pong between devices.
        """
        import tempfile

        from internal.system.updater import download_latest_release

        self._web_toast(T("notify.update_downloading"))

        if from_peers:
            # Ask connected peers first (M2): a peer with the cached asset responds
            # by sending it back; the GitHub download below is the fallback.
            self._request_update_from_peers()

        def _worker():
            dest_dir = tempfile.mkdtemp(prefix="clipsync_update_")
            try:
                path, reason = download_latest_release(dest_dir)
            except Exception as exc:
                logger.exception("Update download failed")
                path, reason = None, str(exc)
            self.root.after(
                0, lambda: self._finish_update_install(path, reason, "github"))

        threading.Thread(target=_worker, daemon=True, name="update-install").start()

    def _discard_update_blob(self, path: str, verdict: str) -> None:
        """Throw away a rejected update blob and tell the user why."""
        try:
            os.remove(path)
        except OSError:
            logger.debug("Could not remove rejected update blob %s", path,
                         exc_info=True)
        key = ("notify.update_rejected_hash" if verdict == "hash_mismatch"
               else "notify.update_rejected_old")
        logger.warning("Update blob discarded (%s): %s", verdict,
                       _mask_path(path))
        self._web_toast(T(key))

    def _finish_update_install(self, path, reason, source: str = "github") -> None:
        """Verify, then stage + apply an update asset — or surface the failure.

        *path* is either a freshly downloaded GitHub asset (*source* "github",
        already size- and hash-checked during download) or a P2P-received blob
        (*source* "p2p", verified here against the GitHub release digest).
        Re-entrant arrivals (both paths completing at once) are collapsed to
        the first one via ``self._updating``; later ones log and skip.
        """
        if getattr(self, "_updating", False):
            logger.info("Update install already in progress — skipping %s "
                        "arrival", source)
            return

        if not path:
            show_error(self.root, T("ui.app_name"),
                       reason or T("tray.update_install_failed"))
            return

        from internal.system.updater import (
            fetch_latest_asset_info,
            verify_update_blob,
        )

        ok, verdict = False, "no_release_info"
        release_info = None
        if source != "github":
            # Only non-GitHub (P2P) arrivals need the release digest fetched;
            # that lookup can block for ~30s (urlopen + backoff), and the
            # GitHub path short-circuits in verify_update_blob when
            # release_info is None — so never run it on the UI thread here.
            try:
                release_info = fetch_latest_asset_info()
            except Exception as exc:  # defensive — the helper never raises
                logger.debug("Release info lookup failed: %s", exc)
        try:
            ok, verdict = verify_update_blob(
                path, release_info, __version__, source=source)
        except Exception:
            logger.exception("Update verification crashed")
            verdict = "hash_mismatch"

        if not ok:
            if verdict == "no_release_info":
                # A peer-sent blob has no authoritative reference to check
                # against — never install it.  Fall back to the GitHub
                # download path (without re-broadcasting to peers).
                logger.warning("P2P update rejected: no release info available "
                               "to verify against — falling back to GitHub")
                self._download_and_install_update(from_peers=False)
                return
            self._discard_update_blob(path, verdict)
            return

        from internal.system.applier import apply_and_restart, stage_update
        from internal.system.updater import cache_asset

        # From here on only one install may run; every failure path clears the
        # flag again before returning.
        self._updating = True

        # Keep the verified asset so we can serve it to other LAN devices (M2).
        cache_asset(path)

        staged = stage_update(path)
        if staged is None:
            self._updating = False
            show_error(self.root, T("ui.app_name"), T("tray.update_install_failed"))
            return
        if apply_and_restart(staged):
            self._skip_save_on_shutdown = True
            # Quit through the normal loop instead of sys.exit(): this runs
            # on the Tk main thread, and SystemExit raised inside an after()
            # callback does not reliably end the process.  _exit_process
            # stops the mainloop, run()'s finally runs shutdown(), then
            # main() returns and the update helper replaces the binary.
            try:
                self.root.after(300, self._exit_process)
                return
            except Exception:
                logger.debug("Could not schedule update exit", exc_info=True)
            self._exit_process()
        self._updating = False
        show_error(self.root, T("ui.app_name"), T("tray.update_install_failed"))

    def _maybe_auto_update_check(self) -> None:
        """Fire the silent periodic check when due — never when disabled.

        With ``auto_update_check`` off this must not touch the network at all
        (not even the throttle timestamp), so toggling back ON starts a fresh
        6h window instead of an immediate request.
        """
        if not getattr(self.cfg, "auto_update_check", True):
            return
        if time.monotonic() - self._last_auto_update_check >= 6 * 3600:
            self._last_auto_update_check = time.monotonic()
            self._auto_check_for_update()

    def _handle_update_install(self) -> dict:
        """POST /api/update/install: run the full tray download→apply chain.

        The actual work is marshalled onto the UI thread because it ends in a
        process exit; this handler just accepts the request.
        """
        if self._shutting_down or getattr(self, "_updating", False):
            return {"ok": False, "error": "update already in progress"}
        try:
            self.root.after(0, self._download_and_install_update)
            return {"ok": True}
        except Exception:
            logger.debug("Could not schedule web-triggered update", exc_info=True)
            return {"ok": False, "error": "could not start update"}

    def _auto_check_for_update(self) -> None:
        """Silent periodic check: only surfaces a result when an update is available."""
        from internal.system.updater import check_for_update

        def _worker():
            try:
                result = check_for_update()
            except Exception as exc:
                logger.debug("Auto update check failed: %s", exc)
                return
            if result.get("available"):
                self.root.after(0, lambda: self._offer_update_install(result))

        threading.Thread(target=_worker, daemon=True, name="auto-update-check").start()

    def _request_update_from_peers(self) -> None:
        """Broadcast an update_request to connected peers (M2 P2P update).

        Any peer that has a cached update asset responds by sending it back
        (kind="update"), which _on_file_received then stages + applies.
        """
        from internal.protocol.codec import encode_frame
        try:
            self.transport_mgr.broadcast(encode_frame({"msg_type": "update_request"}))
            logger.info("Broadcast update_request to peers")
        except Exception:
            logger.warning("Failed to broadcast update_request", exc_info=True)

    def _handle_update_request(self, peer_id: str | None) -> None:
        """Serve our cached update asset to a peer that asked for it (M2)."""
        if not peer_id:
            return
        from internal.system.updater import get_cached_asset
        cached = get_cached_asset()
        if not cached:
            logger.info("Peer %s asked for an update, but none is cached", peer_id[:12])
            return
        send_fn = (lambda data, pid=peer_id: self.transport_mgr.send_to_peer(pid, data))
        try:
            self.file_transfer_mgr.send_file(cached, send_fn, kind="update")
        except Exception:
            logger.exception("Failed to serve cached update to peer %s", peer_id[:12])

    def _notifications_enabled(self) -> bool:
        """Return True if the desktop notification backend is active."""
        return bool(getattr(notification_mgr, "enabled", True))

    def _show_about(self) -> None:
        """Show a compact About dialog — never dump the user into the whole
        Settings window (the tray About is a quick look, not a settings dive)."""
        try:
            from internal.version import __version__
            show_info(
                self.root,
                T("tray.about_title"),
                f"ClipSync  {__version__}\n\n"
                f"{T('settings_window.about_desc')}\n\n"
                f"{T('tray.about_message')}\n"
                "https://github.com/kai3316/clipsync",
            )
        except Exception:
            logger.debug("Could not show About dialog", exc_info=True)

    def send_file(self) -> None:
        self.root.after(0, self._do_send_file)

    def send_folder(self) -> None:
        self.root.after(0, self._do_send_folder)

    def _do_send_file(self) -> None:
        file_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Select Files to Send",
            filetypes=[("All files", "*")],
        )
        if not file_paths:
            return
        if len(file_paths) == 1:
            self._send_single_path(file_paths[0])
        else:
            self._send_as_zip(file_paths)

    def _do_send_folder(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Select Folder to Send",
        )
        if not folder:
            return
        self._send_as_zip([folder])

    def _do_send_url(self) -> None:
        """Send a URL to a selected device. Reads clipboard for URL pre-fill."""
        # Try to read clipboard for URL pre-fill
        prefill = ""
        try:
            clip_text = self.root.clipboard_get()
            if clip_text and self._is_webview() is False:
                import re
                if clip_text and re.match(r'^https?://', clip_text.strip()):
                    prefill = clip_text.strip()
        except Exception:
            pass

        # ── Webview mode: push URL input dialog to web UI ──────────
        if self._is_webview():
            def _on_url_result(result):
                if result is None or result.get("action") != "send":
                    return
                url = (result.get("value") or "").strip()
                if url:
                    self._send_url_to_peer(url)

            def _show_url_input():
                # The dialog round-trip waits up to two minutes for a human;
                # run it on a worker thread so the Tk main loop (hotkeys,
                # tray polling, timers) never stalls.  Only the result
                # handling hops back onto the main thread.
                self._web_dialog_async(
                    "url_input", _on_url_result,
                    title=T("nav_url.title"),
                    message=T("nav_url.prompt"),
                    prefill=prefill,
                )
            self._run_when_webview_ready(_show_url_input)
            return

        # URL input dialog
        import re

        import customtkinter as ctk
        dlg = ctk.CTkToplevel(self.root)
        dlg.title(T("nav_url.title"))
        dlg.resizable(False, False)

        dw, dh = 440, 170
        if self.root.winfo_viewable():
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = rx + (rw - dw) // 2
            y = ry + (rh - dh) // 2
        else:
            x = (self.root.winfo_screenwidth() - dw) // 2
            y = (self.root.winfo_screenheight() - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{x}+{y}")

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=16)

        ctk.CTkLabel(
            body, text=T("nav_url.prompt"),
            font=ctk.CTkFont(size=13),
        ).pack(anchor="w", pady=(0, 8))

        url_var = tk.StringVar(value=prefill)
        entry = ctk.CTkEntry(body, textvariable=url_var, height=36,
                             font=ctk.CTkFont(size=12))
        entry.pack(fill="x", pady=(0, 12))
        entry.focus_set()
        entry.icursor(len(prefill))

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x")

        ctk.CTkButton(
            btn_row, text=T("ui.cancel"), width=80, height=32,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray70"),
            border_color=("gray60", "gray50"),
            hover_color=("gray85", "gray25"),
            command=dlg.destroy,
        ).pack(side="left")

        def _send():
            url = url_var.get().strip()
            dlg.destroy()
            if not url:
                return
            if not re.match(r'^https?://', url):
                url = "https://" + url
            self.root.after(0, lambda u=url: self._send_url_to_peer(u))

        ctk.CTkButton(
            btn_row, text=T("transfer.send"), width=80, height=32,
            command=_send,
        ).pack(side="right")

        dlg.update()
        dlg.transient(self.root)
        try:
            dlg.grab_set()
            dlg.focus_force()
        except Exception:
            pass
        dlg.bind("<Return>", lambda e: _send())
        self._track_modal_dialog(dlg)

    def _send_url_to_peer(self, url: str) -> None:
        """Pick a peer and send the URL (deferred from dialog callback)."""
        def _deliver(peer_id):
            if peer_id is None:
                return
            data = encode_frame({"msg_type": "nav_url", "url": url},
                                source_device=self.cfg.device_id)
            self.transport_mgr.send_to_peer(peer_id, data)
            logger.info("Sent URL to peer %s: %s", peer_id[:12], url[:80])
            self._web_toast(f"{T('nav_url.title')}: {url[:120]}")

        self._pick_peer_then(_deliver)

    def _pick_peer_then(self, on_picked) -> None:
        """Resolve a target peer and continue via ``on_picked(peer_id|None)``.

        The callback always runs on the main thread.  Classic mode keeps the
        modal dialog's ``wait_window()`` nested event loop (safe to block in);
        webview mode has no such loop, so its round-trip runs on a worker
        thread — a plain wait there would stall hotkeys / tray / timers for
        up to two minutes.
        """
        if not self._is_webview():
            on_picked(self._pick_peer())
            return

        peers = self.transport_mgr.get_connected_peers_with_names()
        if not peers:
            self._web_toast(T("transfer.no_peers"))
            on_picked(None)
            return
        if len(peers) == 1:
            on_picked(peers[0][0])
            return

        peer_list = [{"device_id": pid, "device_name": pname} for pid, pname in peers]

        def _on_result(result):
            if result and result.get("action") == "select":
                on_picked(result.get("value"))
            else:
                on_picked(None)

        self._web_dialog_async(
            "pick_peer", _on_result,
            title=T("transfer.select_peer"),
            peers=peer_list,
        )

    def _pick_peer(self) -> str | None:
        """Show a modal dialog to select which peer to send to (classic UI).

        Returns peer_id or None if cancelled. If only one peer is connected,
        returns it without showing a dialog.

        Classic mode only: this blocks in ``wait_window()`` until the user
        answers (a nested Tk event loop, so timers keep running).  Webview
        mode must go through :meth:`_pick_peer_then`, which resolves the
        target off the main thread.
        """
        peers = self.transport_mgr.get_connected_peers_with_names()
        if not peers:
            if self.cfg.web_enabled:
                self._pick_peer_phone_guide()
            else:
                show_error(self.root, T("transfer.error"), T("transfer.no_peers"))
            return None
        if len(peers) == 1:
            return peers[0][0]

        # Multiple peers — show selection dialog
        import platform as _platform
        _is_macos = _platform.system() == "Darwin"
        _is_linux = _platform.system() == "Linux"

        dw, dh = 340, 100 + min(len(peers) * 38, 300)
        if self.root.winfo_viewable():
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = rx + (rw - dw) // 2
            y = ry + (rh - dh) // 2
        else:
            x = (self.root.winfo_screenwidth() - dw) // 2
            y = (self.root.winfo_screenheight() - dh) // 2

        selected = tk.StringVar()
        if peers:
            selected.set(peers[0][0])

        result = [None]

        def _confirm():
            result[0] = selected.get()
            dlg.destroy()

        if _is_macos or _is_linux:
            dlg = tk.Toplevel(self.root)
            dlg.title(T("transfer.select_peer"))
            dlg.resizable(False, False)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")

            body = tk.Frame(dlg)
            body.pack(fill="both", expand=True, padx=20, pady=16)

            tk.Label(body, text=T("transfer.select_peer"),
                     font=("Helvetica", 14, "bold")).pack(anchor="w", pady=(0, 10))

            for pid, name in peers:
                tk.Radiobutton(body, text=name, variable=selected, value=pid,
                               font=("Helvetica", 12)).pack(anchor="w", pady=3)

            btn_row = tk.Frame(body)
            btn_row.pack(fill="x", pady=(12, 0))

            tk.Button(btn_row, text=T("ui.cancel"), width=10,
                      relief="solid", bd=1,
                      command=dlg.destroy).pack(side="left")

            tk.Button(btn_row, text=T("transfer.send"), width=10,
                      command=_confirm).pack(side="right")

            dlg.update()
            dlg.transient(self.root)
            try:
                dlg.grab_set()
                dlg.focus_force()
            except Exception:
                pass
        else:
            import customtkinter as ctk
            dlg = ctk.CTkToplevel(self.root)
            dlg.title(T("transfer.select_peer"))
            dlg.resizable(False, False)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")

            body = ctk.CTkFrame(dlg, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=20, pady=16)

            ctk.CTkLabel(
                body, text=T("transfer.select_peer"),
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(anchor="w", pady=(0, 10))

            for pid, name in peers:
                ctk.CTkRadioButton(
                    body, text=name, variable=selected, value=pid,
                    font=ctk.CTkFont(size=13),
                ).pack(anchor="w", pady=3)

            btn_row = ctk.CTkFrame(body, fg_color="transparent")
            btn_row.pack(fill="x", pady=(12, 0))

            ctk.CTkButton(
                btn_row, text=T("ui.cancel"), width=80, height=32,
                fg_color="transparent", border_width=1,
                text_color=("gray40", "gray70"),
                border_color=("gray60", "gray50"),
                hover_color=("gray85", "gray25"),
                command=dlg.destroy,
            ).pack(side="left")

            ctk.CTkButton(
                btn_row, text=T("transfer.send"), width=80, height=32,
                command=_confirm,
            ).pack(side="right")

            dlg.update()
            dlg.transient(self.root)
            try:
                dlg.grab_set()
                dlg.focus_force()
            except Exception:
                pass

        dlg.wait_window()
        return result[0]

    def _pick_peer_phone_guide(self) -> None:
        """Show guidance for transferring files to a phone via Web Companion."""
        import customtkinter as ctk

        dlg = ctk.CTkToplevel(self.root)
        dlg.title(T("transfer.phone_title"))
        dlg.resizable(False, False)

        dw, dh = 420, 240
        if self.root.winfo_viewable():
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = rx + (rw - dw) // 2
            y = ry + (rh - dh) // 2
        else:
            x = (self.root.winfo_screenwidth() - dw) // 2
            y = (self.root.winfo_screenheight() - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{x}+{y}")

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)

        ctk.CTkLabel(
            body, text=T("transfer.phone_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", pady=(0, 10))

        msg_frame = ctk.CTkFrame(body, fg_color="transparent")
        msg_frame.pack(fill="x", pady=(0, 16))

        ctk.CTkLabel(
            msg_frame, text=T("transfer.phone_msg"),
            font=ctk.CTkFont(size=12),
            justify="left", wraplength=370,
        ).pack(anchor="w")

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x")

        ctk.CTkButton(
            btn_row, text=T("ui.cancel"), width=90, height=34,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray70"),
            border_color=("gray60", "gray50"),
            hover_color=("gray85", "gray25"),
            command=dlg.destroy,
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text=T("transfer.phone_action"), width=130, height=34,
            command=lambda: (
                dlg.destroy(),
                self._show_web_qr(),
            ),
        ).pack(side="right")

        dlg.update()
        dlg.transient(self.root)
        try:
            dlg.grab_set()
            dlg.focus_force()
        except Exception:
            pass
        self._track_modal_dialog(dlg)

    def _send_single_path(self, file_path: str) -> None:
        """Send a single file directly (no zipping)."""

        def _send(peer_id):
            if peer_id is None:
                return
            self._transmit_file_to_peer(peer_id, file_path)

        self._pick_peer_then(_send)

    def _transmit_file_to_peer(self, peer_id: str, file_path: str) -> None:
        """Send one file to an already-resolved peer."""
        def _send_fn(data: bytes):
            self.transport_mgr.send_to_peer(peer_id, data)

        try:
            transfer_id = self.file_transfer_mgr.send_file(file_path, _send_fn)
            if transfer_id:
                self._transfer_directions[transfer_id] = "outgoing"
            logger.info("File transfer initiated: %s", transfer_id[:8])
            self._notify("notify_transfer", T("ui.file_transfer"),
                         T("transfer.sending_file", name=os.path.basename(file_path)))
        except FileNotFoundError:
            self._notify_error(T("ui.error_title"), f"{T('ui.file_not_found_msg')}{file_path}")
        except PermissionError:
            self._notify_error("Error",
                       f"Permission denied reading:\n{file_path}")
        except OSError as e:
            self._notify_error(T("ui.error_title"), f"{T('ui.send_failed_msg')}{e}")
            logger.error("Failed to send file: %s", e)

    def _send_as_zip(self, paths: list[str]) -> None:
        """Zip one or more files/folders into a temp archive and send it.

        Shows a progress dialog so the user can track the archiving and
        cancel if needed.  Zipping runs in a background thread to keep
        the UI responsive.
        """
        self._pick_peer_then(lambda peer_id: self._zip_and_send_to_peer(peer_id, paths))

    def _zip_and_send_to_peer(self, peer_id: str | None, paths: list[str]) -> None:
        """Zip and send *paths* to an already-resolved peer."""
        if peer_id is None:
            return

        def _send_fn(data: bytes):
            self.transport_mgr.send_to_peer(peer_id, data)

        import tempfile
        import zipfile
        from pathlib import Path

        def _safe_remove(path):
            try:
                if path and path.exists():
                    path.unlink()
            except OSError:
                pass

        # ── Count files for progress tracking ──────────────────────
        total_files = 0
        for path in paths:
            p = Path(path)
            if p.is_file():
                total_files += 1
            elif p.is_dir():
                total_files += sum(1 for fp in p.rglob("*") if fp.is_file())
        if total_files == 0:
            self._notify_error(T("ui.error_title"), T("ui.no_files_to_send"))
            return

        names = [os.path.basename(p.rstrip(os.sep).rstrip("/")) for p in paths]
        base = names[0] if len(names) == 1 else f"files-{len(names)}"
        zip_name = f"{base}.zip"

        # ── Webview mode: push progress dialog, work in thread ─────
        if self._is_webview():
            mgr = self.web_server.dialog_mgr if self.web_server else None
            if mgr is None:
                self._notify_error(T("ui.error_title"), T("ui.web_server_unavailable"))
                return

            dialog_id = mgr.push(
                "progress",
                title=T("transfer.creating_archive"),
                message=T("transfer.zipping", name=zip_name),
                progress=0,
                progress_text=T("transfer.preparing"),
            )
            if dialog_id is None:
                self._notify_error(T("ui.error_title"), T("ui.no_web_clients"))
                return

            def _worker_web():
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp_path = Path(tmp.name)

                    with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_DEFLATED) as zf:
                        file_count = 0
                        for path in paths:
                            if mgr.is_cancelled(dialog_id):
                                break
                            p = Path(path)
                            if p.is_file():
                                zf.write(str(p), p.name)
                                file_count += 1
                                frac = file_count / total_files
                                cur = file_count
                                mgr.update_progress(dialog_id, frac,
                                    T("transfer.zipping_progress", current=cur, total=total_files))
                            elif p.is_dir():
                                for fpath in sorted(p.rglob("*")):
                                    if mgr.is_cancelled(dialog_id):
                                        break
                                    if fpath.is_file():
                                        arcname = str(fpath.relative_to(p.parent))
                                        zf.write(str(fpath), arcname)
                                        file_count += 1
                                        frac = file_count / total_files
                                        cur = file_count
                                        mgr.update_progress(dialog_id, frac,
                                            T("transfer.zipping_progress", current=cur, total=total_files))

                    if mgr.is_cancelled(dialog_id):
                        _safe_remove(tmp_path)
                        mgr.close(dialog_id)
                        return

                    transfer_id = self.file_transfer_mgr.send_file(
                        str(tmp_path), _send_fn,
                    )
                    logger.info("Zip transfer initiated: %s (%d files)", transfer_id[:8], total_files)
                    if transfer_id:
                        self._transfer_directions[transfer_id] = "outgoing"
                        # Unlink the temp archive when the transfer finishes
                        # (handled in _on_transfer_complete) so folder sends
                        # don't leak zip copies in the system temp dir.
                        self._zip_cleanup[transfer_id] = str(tmp_path)
                    else:
                        _safe_remove(tmp_path)
                    mgr.close(dialog_id)
                    self._web_toast(T("transfer.sending_file", name=zip_name))
                except FileNotFoundError:
                    _safe_remove(tmp_path)
                    mgr.close(dialog_id)
                    self._notify_error(T("ui.error_title"), T("ui.file_not_found"))
                except PermissionError:
                    _safe_remove(tmp_path)
                    mgr.close(dialog_id)
                    self._notify_error(T("ui.error_title"), T("ui.permission_denied"))
                except OSError as e:
                    logger.error("Failed to zip and send: %s", e)
                    _safe_remove(tmp_path)
                    mgr.close(dialog_id)
                    self._notify_error(T("ui.error_title"), f"{T('ui.archive_failed')}\n{e}")

            threading.Thread(target=_worker_web, daemon=True, name="zip-sender-web").start()
            return

        # ── CTk / tk progress dialog ───────────────────────────────
        cancel_event = threading.Event()

        import customtkinter as _ctk

        dw, dh = 420, 170
        if self.root.winfo_viewable():
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = rx + (rw - dw) // 2
            y = ry + (rh - dh) // 2
        else:
            x = (self.root.winfo_screenwidth() - dw) // 2
            y = (self.root.winfo_screenheight() - dh) // 2

        import platform as _platform
        _is_macos = _platform.system() == "Darwin"
        _is_linux = _platform.system() == "Linux"

        if _is_macos or _is_linux:
            import tkinter as _tk
            from tkinter import ttk as _ttk

            dlg = _tk.Toplevel(self.root)
            dlg.title(T("transfer.creating_archive"))
            dlg.resizable(False, False)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")
            dlg.protocol("WM_DELETE_WINDOW", lambda: cancel_event.set())

            _tk.Label(dlg, text=T("transfer.zipping", name=zip_name),
                      font=("Helvetica", 13, "bold")).pack(pady=(20, 10))

            progress_bar = _ttk.Progressbar(dlg, length=370, mode="determinate")
            progress_bar.pack(pady=(0, 8))

            status_var = tk.StringVar(value=T("transfer.preparing"))
            _tk.Label(dlg, textvariable=status_var, font=("Helvetica", 11)).pack()

            _tk.Button(dlg, text=T("ui.cancel"),
                       command=lambda: cancel_event.set()).pack(pady=(12, 16))

            def _set_progress(val):
                progress_bar["value"] = val * 100
            def _set_status(text):
                status_var.set(text)

            dlg.update()
            dlg.transient(self.root)
            try:
                dlg.grab_set()
            except Exception:
                pass
        else:
            dlg = _ctk.CTkToplevel(self.root)
            dlg.title(T("transfer.creating_archive"))
            dlg.resizable(False, False)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")
            dlg.protocol("WM_DELETE_WINDOW", lambda: cancel_event.set())

            body = _ctk.CTkFrame(dlg, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=24, pady=(20, 12))

            _ctk.CTkLabel(
                body, text=T("transfer.zipping", name=zip_name),
                font=_ctk.CTkFont(size=13, weight="bold"),
            ).pack(anchor="w", pady=(0, 12))

            progress_bar = _ctk.CTkProgressBar(body, width=370, height=14)
            progress_bar.pack(fill="x", pady=(0, 8))
            progress_bar.set(0)

            status_var = tk.StringVar(value=T("transfer.preparing"))
            _ctk.CTkLabel(
                body, textvariable=status_var,
                font=_ctk.CTkFont(size=11),
                text_color=("gray50", "gray60"),
            ).pack(anchor="w")

            _ctk.CTkButton(
                dlg, text=T("ui.cancel"), width=90, height=30,
                fg_color="transparent", border_width=1,
                text_color=("gray40", "gray60"),
                border_color=("gray60", "gray50"),
                hover_color=("gray85", "gray25"),
                font=_ctk.CTkFont(size=12),
                command=lambda: cancel_event.set(),
            ).pack(pady=(0, 16))

            def _set_progress(val):
                progress_bar.set(val)
            def _set_status(text):
                status_var.set(text)

            dlg.update()
            dlg.transient(self.root)
            try:
                dlg.grab_set()
            except Exception:
                pass

        # Keep a reference to prevent premature garbage collection on macOS
        self._active_dialog = dlg

        # ── Background worker ──────────────────────────────────────
        def _worker():
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_DEFLATED) as zf:
                    file_count = 0
                    for path in paths:
                        if cancel_event.is_set():
                            break
                        p = Path(path)
                        if p.is_file():
                            zf.write(str(p), p.name)
                            file_count += 1
                            frac = file_count / total_files
                            cur = file_count
                            self.root.after(0, lambda f=frac, c=cur: (
                                _set_progress(f),
                                _set_status(
                                    T("transfer.zipping_progress", current=c, total=total_files)),
                            ))
                        elif p.is_dir():
                            for fpath in sorted(p.rglob("*")):
                                if cancel_event.is_set():
                                    break
                                if fpath.is_file():
                                    arcname = str(fpath.relative_to(p.parent))
                                    zf.write(str(fpath), arcname)
                                    file_count += 1
                                    frac = file_count / total_files
                                    cur = file_count
                                    self.root.after(0, lambda f=frac, c=cur: (
                                        _set_progress(f),
                                        _set_status(
                                            T("transfer.zipping_progress", current=c, total=total_files)),
                                    ))

                if cancel_event.is_set():
                    _safe_remove(tmp_path)
                    self.root.after(0, dlg.destroy)
                    return

                transfer_id = self.file_transfer_mgr.send_file(
                    str(tmp_path), _send_fn,
                )
                logger.info("Zip transfer initiated: %s (%d files)", transfer_id[:8], total_files)
                if transfer_id:
                    self._transfer_directions[transfer_id] = "outgoing"
                    # Unlink the temp archive when the transfer finishes
                    # (see _on_transfer_complete) so folder sends don't leak.
                    self._zip_cleanup[transfer_id] = str(tmp_path)
                else:
                    _safe_remove(tmp_path)
                self.root.after(0, lambda: (
                    dlg.destroy(),
                    self._notify("notify_transfer", T("ui.file_transfer"),
                                 T("transfer.sending_file", name=zip_name)),
                ))
            except FileNotFoundError:
                _safe_remove(tmp_path)
                self.root.after(0, lambda: (dlg.destroy(), self._notify_error(
                    T("ui.error_title"), T("ui.file_not_found"))))
            except PermissionError:
                _safe_remove(tmp_path)
                self.root.after(0, lambda: (dlg.destroy(), self._notify_error(
                    T("ui.error_title"), T("ui.permission_denied"))))
            except OSError as e:
                logger.error("Failed to zip and send: %s", e)
                _safe_remove(tmp_path)
                self.root.after(0, lambda e=e: (dlg.destroy(), self._notify_error(
                    "Error", f"Failed to create archive:\n{e}")))

        threading.Thread(target=_worker, daemon=True, name="zip-sender").start()

    def open_settings(self, tab: str | None = None) -> None:
        if self.cfg.ui_backend == "webview":
            # In webview mode, settings live in the web UI. Open the window if
            # needed and wait for a client, then tell it to show the panel.
            self._run_when_webview_ready(
                lambda: self._push_web("broadcast", "open_settings", {"tab": tab})
            )
        else:
            self.root.after(0, lambda: self._create_settings_window(tab))

    def _create_settings_window(self, tab: str | None = None) -> None:
        if self.settings_win is not None:
            self.settings_win.show()
            self._switch_settings_panel(tab)
            return

        def _on_closed():
            self.settings_win = None

        self.settings_win = SettingsWindow(
            root=self.root,
            get_config=self._get_cfg,
            save_config=self._save_cfg_and_peers,
            on_closed=_on_closed,
            on_quit=self.shutdown,
            on_export_logs=self.export_logs,
            get_filter_categories=lambda: self.content_filter.enabled_categories,
            set_filter_categories=lambda cats: (
                setattr(self.content_filter, 'enabled_categories', cats),
                setattr(self.cfg, 'filter_enabled_categories', cats),
            ),
            get_log_text=lambda: _get_log_path().read_text(encoding="utf-8")
            if _get_log_path().exists() else "No log file yet.",
            set_skip_save_on_shutdown=lambda v: setattr(
                self, "_skip_save_on_shutdown", v,
            ),
            on_web_action=self._on_web_action,
        )
        self.settings_win.show()
        self._switch_settings_panel(tab)

    def _switch_settings_panel(self, tab: str | None) -> None:
        """Best-effort switch the settings window to *tab* (e.g. "about")."""
        if not tab or self.settings_win is None:
            return
        try:
            self.settings_win._switch_panel(tab)
        except Exception:
            logger.debug("Could not switch settings panel to %s", tab, exc_info=True)

    # ── Web dialog helper ──────────────────────────────────────────

    def _run_when_webview_ready(self, action) -> None:
        """Open the webview window if needed, then run `action` once a web
        client is connected.  Without a connected client, WebSocket pushes
        (dialogs, open_settings) are silently dropped, so tray actions fired
        before the window opened must wait for the client to attach.
        """
        def _drain() -> None:
            if self._shutting_down:
                return
            try:
                ready = (self.web_server is not None
                         and self.web_server.ws_manager.client_count > 0)
            except Exception:
                ready = False
            if ready:
                try:
                    action()
                except Exception:
                    logger.debug("pending webview action failed", exc_info=True)
            else:
                self.root.after(250, _drain)

        if self.webview_win is None or not self.webview_win.is_running():
            self._open_webview_dashboard()
        self.root.after(0, _drain)

    def _web_dialog(self, dialog_type: str, **kwargs):
        """Show a dialog via the web UI and return the response (blocking).

        Blocks for up to ``timeout`` seconds waiting for a human response.
        NEVER call this from the Tk main thread — the wait is a plain
        event-wait that does not pump the Tk event loop, so hotkeys, tray
        polling and every ``after()`` timer would freeze until the user
        answers.  Use :meth:`_web_dialog_async` on the main thread instead,
        or call this directly from a background thread.
        """
        mgr = self.web_server.dialog_mgr if self.web_server else None
        if mgr is None:
            return None
        return mgr.show(dialog_type, **kwargs)

    def _web_dialog_async(self, dialog_type: str, on_result, **kwargs) -> None:
        """Run a blocking web-dialog round-trip on a worker thread.

        ``on_result(result)`` is invoked on the Tk main thread with the
        response dict, or None on timeout / no connected clients.  The dialog
        itself waits up to two minutes for a human response; running that
        wait on a worker keeps the main loop responsive, and only the result
        handling (which may touch widgets) hops back onto the main thread.
        """
        def _worker():
            result = self._web_dialog(dialog_type, **kwargs)
            try:
                self.root.after(0, lambda: on_result(result))
            except Exception:
                logger.debug("web-dialog callback scheduling failed", exc_info=True)

        threading.Thread(
            target=_worker, daemon=True, name=f"web-dialog-{dialog_type}",
        ).start()

    def _web_toast(self, message: str, duration: int = 3000) -> None:
        """Push a non-blocking toast notification to the web UI.

        The master ``notifications_enabled`` switch gates every toast, so
        turning notifications OFF silences in-app toasts too.  A toast only
        renders where a web page (webview dashboard / remote access) is
        attached; no OS-level notification is ever shown.
        """
        if not self._notifications_enabled():
            return
        ws = getattr(self, "web_server", None)
        mgr = getattr(ws, "dialog_mgr", None) if ws is not None else None
        if mgr is not None:
            mgr.toast(message, duration)

    def _web_has_clients(self) -> bool:
        """Return True when at least one web client is attached.

        A ``_web_toast`` is only visible when a web page (the webview
        dashboard or a phone's remote-access page) is actually connected;
        otherwise it is silently dropped and the desktop needs a real
        notification instead.
        """
        try:
            return (
                self.web_server is not None
                and self.web_server.ws_manager.client_count > 0
            )
        except Exception:
            logger.debug("web_has_clients check failed", exc_info=True)
            return False

    def _notify(self, cfg_flag: str, title: str, message: str) -> None:
        """Show a notification as a web toast, gated by a per-type toggle.

        Returns early (no notification) when ``cfg.<cfg_flag>`` is False.
        The master ``notifications_enabled`` switch is enforced in
        ``_web_toast``, so it applies on top of these toggles.

        ``sound_enabled`` ("通知提示音") still gates these toasts for
        backward compatibility with the old desktop notifications: a user
        who turned it OFF was living without notifications, and stays that
        way.
        """
        if not getattr(self.cfg, "sound_enabled", True):
            return
        if not getattr(self.cfg, cfg_flag, True):
            return
        self._web_toast(f"{title}: {message}")

    def _notify_error(self, title: str, message: str) -> None:
        """Show an error — toast in webview mode, CTk dialog in CTk mode."""
        if self._is_webview():
            self._web_toast(f"{title}: {message}", 5000)
        else:
            show_error(self.root, title, message)

    def _notify_warning(self, title: str, message: str) -> None:
        """Show a warning — toast in webview mode, CTk dialog in CTk mode."""
        if self._is_webview():
            self._web_toast(f"{title}: {message}", 4000)
        else:
            show_warning(self.root, title, message)

    def _notify_info(self, title: str, message: str) -> None:
        """Show info — toast in webview mode, CTk dialog in CTk mode."""
        if self._is_webview():
            self._web_toast(f"{title}: {message}", 3000)
        else:
            show_info(self.root, title, message)

    def _is_webview(self) -> bool:
        return self.cfg is not None and self.cfg.ui_backend == "webview"

    def _push_web(self, method_name: str, *args) -> None:
        """Invoke a WebSocketManager broadcast helper by name.

        Used to push real-time updates (devices, transfers, pairing) to the
        web UI. No-op when the web server isn't running, and never raises.
        """
        if self.web_server is None:
            return
        try:
            fn = getattr(self.web_server.ws_manager, method_name, None)
            if fn is not None:
                fn(*args)
        except Exception:
            logger.debug("Failed to push %s to web clients", method_name, exc_info=True)

    def _push_aiconfig_event(self, event: dict) -> None:
        """Push an AI-config event (e.g. aiconfig_file landing result) to
        every connected WS client.  Fired from transport reader threads; the
        WebSocketManager broadcast is thread-safe.  Never raises."""
        if not isinstance(event, dict):
            return
        self._push_web("broadcast", "aiconfig_file", event)



    def _handle_diagnostics_request(self, action: str) -> dict:
        """Open the relevant OS permission / firewall settings, or re-apply a Windows rule."""
        try:
            import platform as _platform
            import subprocess as _sp
            if action == "firewall":
                if _platform.system() == "Darwin":
                    # macOS has no CLI to grant the Application Firewall, and
                    # the Local Network permission (macOS 15+) can only be
                    # toggled in System Settings — so open the exact pane.
                    if self._macos_open_settings(
                            "x-apple.systempreferences:com.apple.preference.security?Firewall"):
                        return {"ok": True}
                    return {"ok": False,
                            "error": "Could not open the macOS firewall settings."}
                if _platform.system() == "Windows":
                    # Prefer re-applying the allow rule (idempotent). netsh needs
                    # admin rights — retry elevated via a UAC prompt, then fall
                    # back to opening the firewall settings page so the user can
                    # allow the ports manually.
                    if (self.web_server is not None
                            and self.web_server._open_firewall(self.cfg.port, self.cfg.web_port)):
                        return {"ok": True}
                    if (self.web_server is not None
                            and self.web_server._open_firewall_elevated(self.cfg.port, self.cfg.web_port)):
                        return {"ok": True}
                    if self._open_windows_settings("ms-settings:network-firewall"):
                        return {"ok": True}
                    return {"ok": False,
                            "error": "Could not create the firewall rule (admin rights may be "
                                     "required) or open the firewall settings."}
                if _platform.system() == "Linux":
                    # Grant both ports on the active firewall (ufw / firewalld)
                    # via a PolicyKit GUI auth prompt — the Linux equivalent of
                    # the Windows UAC repair. If pkexec is unavailable, surface
                    # the exact command so the user can run it as root.
                    fw_name, script = self._linux_firewall_allow_script(
                        self.cfg.port, self.cfg.web_port)
                    if not fw_name:
                        # No active firewall detected — nothing to request.
                        return {"ok": True}
                    try:
                        _sp.Popen(["pkexec", "sh", "-c", script])
                        return {"ok": True}
                    except Exception:
                        return {"ok": False,
                                "error": f"Allow the ports manually as root: {script}"}
                return {"ok": False, "error": "Firewall settings are not supported on this OS."}
            if action == "local_network":
                if _platform.system() == "Darwin":
                    # The macOS 15+ Local Network permission is OS-enforced and
                    # cannot be granted by CLI — open the exact pane for it.
                    if self._macos_open_settings(
                            "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"):
                        return {"ok": True}
                    return {"ok": False,
                            "error": "Could not open the macOS Local Network permission settings."}
                if _platform.system() == "Windows":
                    # Windows has no dedicated local-network permission page on most
                    # builds — the firewall & network settings is the closest target.
                    if self._open_windows_settings("ms-settings:network-firewall"):
                        return {"ok": True}
                    return {"ok": False,
                            "error": "Could not open the Windows network/firewall settings."}
                if _platform.system() == "Linux":
                    # No local-network permission exists on Linux — nothing to do.
                    return {"ok": True}
                return {"ok": False, "error": "Permission settings are not supported on this OS."}
            return {"ok": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _linux_firewall_allow_script(port: int, web_port: int) -> tuple[str | None, str | None]:
        """Return (firewall_name, shell_script) opening both ports on Linux.

        Detects the active firewall (ufw or firewalld) the same way the
        diagnostics check does and returns the exact commands an admin shell
        needs to run. Returns (None, None) when no firewall is active.
        """
        import subprocess as _sp
        for cmd, name in ((["ufw", "status"], "ufw"),
                          (["systemctl", "is-active", "firewalld"], "firewalld")):
            try:
                _out = _sp.run(cmd, capture_output=True, text=True, timeout=3).stdout or ""
            except Exception:
                continue
            # Whole-word match: "active" is a substring of "inactive", so a
            # bare `in` check would misread `ufw status` = "Status: inactive".
            if "active" in _out.split():
                if name == "ufw":
                    return ("ufw", f"ufw allow {port}/tcp && ufw allow {web_port}/tcp")
                return ("firewalld",
                        f"firewall-cmd --permanent --add-port={port}/tcp && "
                        f"firewall-cmd --permanent --add-port={web_port}/tcp && "
                        f"firewall-cmd --reload")
        return (None, None)

    @staticmethod
    def _macos_open_settings(uri: str) -> bool:
        """Open a macOS System Settings pane (best-effort)."""
        try:
            import subprocess as _sp
            _sp.Popen(["open", uri])
            return True
        except Exception:
            return False

    @staticmethod
    def _open_windows_settings(uri: str) -> bool:
        """Open a Windows Settings URI through several launchers.

        `os.startfile` on a ms-settings: URI works on most builds but can
        fail on some systems; fall back to explorer.exe and finally to the
        Settings app binary itself.
        """
        import os as _os
        import subprocess as _sp
        try:
            _os.startfile(uri)
            return True
        except Exception:
            pass
        try:
            _sp.Popen(["explorer.exe", uri])
            return True
        except Exception:
            pass
        try:
            _sp.Popen([_os.path.join(
                _os.environ.get("WINDIR", r"C:\Windows"),
                "ImmersiveControlPanel", "SystemSettings.exe"), uri])
            return True
        except Exception:
            return False

    def _get_overview_data(self) -> dict:
        """Return aggregated dashboard overview data for the web UI."""
        import platform as _platform
        import time as _time
        connected = self.transport_mgr.get_connected_peers() if self.transport_mgr else []
        paired = getattr(self.pairing_mgr, 'get_paired_peers', lambda: [])() if self.pairing_mgr else []
        # Count active transfers
        active_tx = 0
        try:
            tx_list = self.file_transfer_mgr.get_transfers() if self.file_transfer_mgr else []
            active_tx = sum(1 for t in tx_list if t.get('status') not in ('completed', 'cancelled', 'failed'))
        except Exception:
            pass
        # Uptime
        uptime = int(_time.time()) - getattr(self, '_start_time', int(_time.time()))
        # ── History stats ──────────────────────────────────────────
        hist = self.clipboard_history.get_all() if self.clipboard_history else []
        import datetime as _dt
        _today = _dt.date.today()
        def _is_today(ts):
            try:
                return _dt.datetime.fromtimestamp(float(ts)).date() == _today
            except Exception:
                return False
        history_today = sum(1 for e in hist if _is_today(e.get("timestamp", 0)))
        history_pinned = sum(1 for e in hist if e.get("pinned"))
        history_images = sum(
            1 for e in hist
            if str(e.get("content_type", "")).upper() in ("IMAGE", "IMAGE_PNG", "IMAGE_EMF", "PICTURE")
        )
        # ── Transfer stats ─────────────────────────────────────────
        tx_hist = self.file_transfer_mgr.get_history() if self.file_transfer_mgr else []
        transfer_completed = sum(1 for t in tx_hist if t.get("success"))
        transfer_bytes = sum(int(t.get("file_size", 0) or 0) for t in tx_hist if t.get("success"))
        # ── Connected peers (names for the live device chips) ─────
        connected_names = []
        try:
            connected_names = [
                name for _pid, name in self.transport_mgr.get_connected_peers_with_names()
            ]
        except Exception:
            pass
        # ── Recent clipboard activity feed ─────────────────────────
        recent_items = [
            {
                "text": (e.get("text_preview") or "")[:80],
                "type": str(e.get("content_type") or "TEXT"),
                "time": e.get("timestamp", 0),
                "pinned": bool(e.get("pinned")),
            }
            for e in hist[:6]
        ]
        ntype, niface = _detect_network_type()
        return {
            'connected_count': len(connected),
            'paired_count': len(paired),
            'discovered_count': len(self._snapshot_discovered_peers()),
            'connected_names': connected_names,
            'history_count': len(hist),
            'history_today': history_today,
            'history_pinned': history_pinned,
            'history_images': history_images,
            'active_transfers': active_tx,
            'transfer_completed': transfer_completed,
            'transfer_bytes': transfer_bytes,
            'discovering': bool(self.discovery and self.discovery.is_browsing),
            'visible': bool(self.discovery and self.discovery.is_advertising),
            'sync_enabled': self.cfg.sync_enabled if self.cfg else True,
            'web_enabled': self.cfg.web_enabled if self.cfg else True,
            'uptime_seconds': uptime,
            'local_ip': WebServer._get_lan_ip(),
            'port': self.cfg.web_port if self.cfg else 0,
            'platform': _platform.system(),
            'version': __version__,
            'network_type': ntype,
            'network_detail': niface,
            'recent_items': recent_items,
            'recent_activity': '',
        }

    def _handle_web_device_action(self, action: str, peer_id: str, *args) -> bool:
        """Handle device actions from the web UI."""
        try:
            if action == 'pair':
                code = args[0] if args else ''
                return self._on_pair(peer_id, code)
            elif action == 'unpair':
                self._on_unpair(peer_id)
                return True
            elif action == 'reject':
                self.pairing_mgr.reject_pairing(peer_id)
                self._push_web("broadcast", "pairing_resolved", {"peer_id": peer_id})
                return True
            elif action == 'connect':
                return self._on_connect(peer_id)
            elif action == 'disconnect':
                self._on_disconnect(peer_id)
                return True
            elif action == 'forget':
                self._on_remove(peer_id)
                return True
            elif action == 'edit_note':
                note = args[0] if args else ''
                self._on_edit_note(peer_id, note)
                return True
        except Exception as e:
            logger.error("Device action %s failed: %s", action, e)
        return False

    def _handle_web_transfer_action(self, action: str, transfer_id: str) -> bool:
        """Handle transfer actions from the web UI."""
        try:
            if not self.file_transfer_mgr:
                return False
            # Resolve the send function so pause/resume/cancel actually reach
            # the peer.  A None send_fn would drop every frame in
            # _send_as_frame, so the sender never saw file_pause /
            # file_complete(cancelled) and the transfer would hang/fail.
            send_fn = (
                self.file_transfer_mgr.get_transfer_send_fn(transfer_id)
                or self.transport_mgr.broadcast
            )
            if action == 'cancel':
                self.file_transfer_mgr.cancel_transfer(transfer_id, send_fn)
            elif action == 'pause':
                self.file_transfer_mgr.pause_transfer(transfer_id, send_fn)
            elif action == 'resume':
                self.file_transfer_mgr.resume_transfer(transfer_id, send_fn)
            elif action == 'accept':
                self.file_transfer_mgr.accept_transfer(transfer_id, send_fn)
            elif action == 'reject':
                self.file_transfer_mgr.reject_transfer(transfer_id, send_fn)
            elif action == 'retry':
                # Re-send a FAILED outgoing transfer from history to its
                # original peer (the transfers panel offers Retry on failed
                # rows; failed rows persist in history with source + peer).
                return self._retry_transfer_from_history(transfer_id)
            else:
                return False
            return True
        except Exception as e:
            logger.error("Transfer action %s failed: %s", action, e)
        return False

    def _retry_transfer_from_history(self, transfer_id: str) -> bool:
        """Start a fresh transfer for a failed outgoing one (web Retry action).

        Looks up the failed row in transfer history (failed transfers persist
        there with their source path and destination peer), then re-sends the
        file to the same peer.  Returns True when a new transfer was started.
        """
        if self.file_transfer_mgr is None:
            return False
        entry = next(
            (e for e in self.file_transfer_mgr.get_history()
             if e.get("transfer_id") == transfer_id),
            None,
        )
        if entry is None or entry.get("direction") != "up":
            return False
        source_path = entry.get("source_path") or ""
        peer_id = entry.get("peer_id") or ""
        if not source_path or not peer_id or not os.path.isfile(source_path):
            logger.debug(
                "Retry transfer %s: missing source (%s) or peer (%s)",
                transfer_id[:8], _mask_path(source_path), peer_id[:12],
            )
            return False

        def _send_fn(data: bytes, pid=peer_id):
            self.transport_mgr.send_to_peer(pid, data)

        try:
            new_id = self.file_transfer_mgr.send_file(source_path, _send_fn)
        except OSError as e:
            logger.warning("Retry transfer failed for %s: %s", source_path, e)
            return False
        if not new_id:
            return False
        self._transfer_directions[new_id] = "outgoing"
        logger.info("Retried transfer %s as %s to peer %s",
                    transfer_id[:8], new_id[:8], peer_id[:12])
        return True

    def open_dashboard(self) -> None:
        if self._is_webview():
            self.root.after(0, self._open_webview_dashboard)
        else:
            self.root.after(0, self._create_dashboard_window)

    def _open_webview_dashboard(self) -> None:
        """Open the web UI in a browser app-mode window (idempotent).

        The only reliable signal that a dashboard window is open is a live
        WebSocket client: the SPA keeps a WS connection while the window shows,
        so client_count>0 means the dashboard itself is open. Process tracking
        is NOT reliable
        here — Chrome --app hands off to an already-running instance and the
        spawned process exits, so is_running() goes False even while the window
        is open. Gating on is_running() is exactly what made every call spawn a
        NEW browser window (the "multiple windows / many processes" bug).
        """
        # Import here to avoid circular imports
        from internal.ui.webview_window import WebViewWindow

        if self.webview_win is not None:
            ws_live = 0
            if self.web_server is not None:
                # client_count is a property (int), not a method — read it
                # directly; the attribute default is an int too.
                ws_live = getattr(self.web_server.ws_manager, "client_count", 0)
            if ws_live > 0:
                self._webview_client_seen = True
            # Grace period after opening: the SPA takes a moment to load and
            # attach its WS client, so rapid repeated clicks during that window
            # must not spawn duplicate browser windows.  Once a client actually
            # loaded, "recently opened" no longer implies the window is up —
            # the user may have closed it (the WS client disconnected), so the
            # tray "Show Dashboard" action must re-open it rather than stay
            # dead for the whole grace period.
            recently_opened = (time.monotonic() - self._webview_opened_at) < 8.0
            if ws_live > 0 or (recently_opened and not self._webview_client_seen):
                logger.debug("Dashboard already open (ws_clients=%d)", ws_live)
                return

        url = (
            f"http://127.0.0.1:{self.cfg.web_port}"
            f"/index.html?token={quote(self.cfg.web_token or '', safe='')}"
        )
        # Opening window size: 1152x648 (was the browser default ~960x720;
        # tuned wider / shorter per request).  A previously-requested size
        # (same process re-open) is reused.
        size = getattr(self, "_webview_size", None)
        self.webview_win = WebViewWindow(
            url=url, title=T("ui.app_name"),
            width=size[0] if size and size[0] else 1152,
            height=size[1] if size and size[1] else 648,
        )
        self.webview_win.start()
        self._webview_opened_at = time.monotonic()
        self._webview_client_seen = False
        # Track the requested size so subsequent opens reuse it instead of
        # snapping back to the default.
        try:
            self._webview_size = (self.webview_win._width, self.webview_win._height)
        except Exception:
            self._webview_size = None
        # Never log the URL with its ?token= query — it would leak the web token
        # into a (previously world-readable) log file.
        logger.info("WebView dashboard opened: %s", url.split("?")[0])

    def _create_dashboard_window(self) -> None:
        if self.dashboard_win is not None:
            self.dashboard_win.show()
            return

        self.dashboard_win = DashboardWindow(
            root=self.root,
            get_config=self._get_cfg,
            save_config=self._save_cfg_and_peers,
            get_peers=self._get_peers,
            # Reconnect progress for offline paired rows ("reconnecting N/M");
            # empty dict when the transport layer is gone (shutdown ordering).
            get_reconnect_states=lambda: (
                self.transport_mgr.get_reconnect_states()
                if self.transport_mgr is not None else {}
            ),
            on_quit=self.shutdown,
            get_sync_enabled=lambda: self.cfg.sync_enabled,
            set_sync_enabled=lambda v: (
                self._clear_pause_state(),
                self.sync_mgr.set_enabled(v), self._set_systray_syncing(v),
            ),
            get_discovering=lambda: self.discovery.is_browsing,
            get_visible=lambda: self.discovery.is_advertising,
            on_toggle_discovery=self._on_toggle_discovery,
            on_toggle_visibility=self._on_toggle_visibility,
            on_open_settings=self.open_settings,
            on_send_file=self.send_file,
            on_send_folder=self.send_folder,
            on_toggle_autostart=lambda enabled: (
                enable_autostart() if enabled else disable_autostart()
            ),
            get_transfers=lambda: self.file_transfer_mgr.get_transfers(),
            on_cancel_transfer=lambda tid: self.file_transfer_mgr.cancel_transfer(
                tid, self.file_transfer_mgr.get_transfer_send_fn(tid) or self.transport_mgr.broadcast,
            ),
            on_pause_transfer=lambda tid: self.file_transfer_mgr.pause_transfer(
                tid, self.file_transfer_mgr.get_transfer_send_fn(tid) or self.transport_mgr.broadcast,
            ),
            on_resume_transfer=lambda tid: self.file_transfer_mgr.resume_transfer(
                tid, self.file_transfer_mgr.get_transfer_send_fn(tid) or self.transport_mgr.broadcast,
            ),
            get_pending_pairings=self._get_pending,
            on_pair=self._on_pair,
            on_unpair=self._on_unpair,
            on_connect_peer=self._on_connect,
            on_disconnect_peer=self._on_disconnect,
            on_remove_peer=self._on_remove,
            get_history=self._get_history,
            search_history=self._search_history,
            copy_from_history=self._copy_from_history,
            clear_history=self._clear_history,
            delete_history_item=self._delete_history_item,
            get_transfer_history=lambda: self.file_transfer_mgr.get_history(),
            on_speed_test=lambda: self.file_transfer_mgr.start_speed_test(
                self.transport_mgr.broadcast,
                has_peers_fn=lambda: bool(self.transport_mgr.get_connected_peers()),
            ),
            get_speed_test_result=lambda: self.file_transfer_mgr.get_speed_test(),
            clear_transfer_history=self._clear_transfer_history,
            delete_transfer_history_item=lambda entry: (
                self.file_transfer_mgr.delete_history_item(entry)
                and self.dashboard_win._refresh_transfers()
            ),
            on_open_file=self._open_file,
            on_open_folder=self._open_folder,
            on_retry_transfer=self._retry_file_transfer,
            on_edit_note=self._on_edit_note,
            on_web_action=self._on_web_action,
            # Nearby chat
            get_chat_devices=self._get_chat_devices,
            chat_start_session=self._chat_start_session,
            get_chat_sessions=self._chat_get_sessions,
            get_chat_messages=self._chat_get_messages,
            mark_session_read=self._chat_mark_read,
            chat_send_text=self._chat_send_text,
            chat_resend_text=self._chat_resend_text,
            chat_send_file=self._chat_send_file,
            chat_accept_invite=self._chat_accept_invite,
            chat_decline_invite=self._chat_decline_invite,
            chat_close_session=self._chat_close_session,
            chat_cancel_file=self._chat_cancel_file,
            chat_accept_file=self._chat_accept_file,
            chat_decline_file=self._chat_decline_file,
            on_chat_event=self._chat_event_on_main,
        )
        self.dashboard_win.show()

    # ═══════════════════════════════════════════════════════════════
    # Dashboard / Settings callbacks
    # ═══════════════════════════════════════════════════════════════

    def _get_cfg(self) -> Config:
        return self.cfg

    def _persist_peer_addresses(self) -> None:
        """Copy last-known peer addresses from the transport into cfg.peers.

        Runs under the caller's config_lock. Lets a paired peer be reached
        right after restart, even before mDNS re-discovers it.
        """
        try:
            for pid, (_name, addr, port) in self.transport_mgr.get_peer_addresses().items():
                if pid in self.cfg.peers:
                    self.cfg.peers[pid].last_ip = addr
                    self.cfg.peers[pid].last_port = port
        except Exception:
            logger.debug("Failed to persist peer addresses", exc_info=True)

    def _save_cfg_and_peers(self) -> None:
        # Hold the shared config lock so web-server threads can't concurrently
        # iterate/mutate cfg.peers while we snapshot it (avoids RuntimeError).
        with config_lock:
            for peer in self.pairing_mgr.get_known_peers():
                existing = self.cfg.peers.get(peer.device_id)
                self.cfg.peers[peer.device_id] = PeerInfo(
                    device_id=peer.device_id,
                    device_name=peer.device_name,
                    public_key_pem=peer.certificate_pem,
                    paired=peer.paired,
                    notes=existing.notes if existing else "",
                )
            self._persist_peer_addresses()
            self._save_cfg_encrypted()

    def _get_peers(self) -> list[tuple]:
        known = []
        discovered = []
        seen_ids: set[str] = set()
        known_names: set[str] = set()
        connected_ids = set(self.transport_mgr.get_connected_peers())
        resolved = self.transport_mgr.get_resolved_hashes()

        rev_resolved: dict[str, set] = {}
        for h_id, r_id in resolved.items():
            rev_resolved.setdefault(r_id, set()).add(h_id)

        for p in self.pairing_mgr.get_known_peers():
            connected = p.device_id in connected_ids
            if not connected:
                for h_id in rev_resolved.get(p.device_id, []):
                    if h_id in connected_ids:
                        connected = True
                        break
            notes = ""
            if p.device_id in self.cfg.peers:
                notes = self.cfg.peers[p.device_id].notes
            known.append((p.device_id, p.device_name, p.paired, connected, notes))
            seen_ids.add(p.device_id)
            known_names.add(p.device_name.lower())

        for hash_id, real_id in resolved.items():
            if real_id in seen_ids:
                seen_ids.add(hash_id)

        def _name_matches_known(disc_name: str) -> bool:
            dl = disc_name.lower()
            # Strip the unique "-<hash4>" suffix the mDNS instance name now
            # carries so a suffixed advertisement still matches the known
            # full name by its truncated prefix.
            if len(dl) > 5 and dl[-5] == "-" and all(c in "0123456789abcdef" for c in dl[-4:]):
                dl = dl[:-5]
            for kn in known_names:
                if dl == kn or dl.startswith(kn) or kn.startswith(dl):
                    return True
            return False

        with self._discovered_lock:
            for peer_id, info in list(self._discovered_peers.items()):
                if peer_id in seen_ids:
                    continue
                if _name_matches_known(info["name"]):
                    continue
                discovered.append((peer_id, info["name"], False, False, ""))

        return known + discovered

    def _get_certs(self) -> list:
        """Return known peers with their certificate fingerprints for the web UI.

        Each entry: {"device_id", "device_name", "fingerprint_short",
        "fingerprint", "paired"}.  Uses getattr so missing attributes on a
        PeerIdentity degrade gracefully to ''.
        """
        try:
            peers = self.pairing_mgr.get_known_peers() if self.pairing_mgr else []
        except Exception:
            logger.exception("Failed to list known peers for certs API")
            return []
        result = []
        for peer in peers:
            result.append({
                "device_id": getattr(peer, "device_id", ""),
                "device_name": getattr(peer, "device_name", ""),
                "fingerprint_short": getattr(peer, "fingerprint_short", ""),
                "fingerprint": getattr(peer, "fingerprint", ""),
                "paired": bool(getattr(peer, "paired", False)),
            })
        return result

    @staticmethod
    def _is_private_ip(ip: str) -> bool:
        """Return True when *ip* is in a private LAN range (RFC 1918)."""
        if ip.startswith("192.168."):
            return True
        if ip.startswith("10."):
            return True
        if ip.startswith("172."):
            try:
                return 16 <= int(ip.split(".")[1]) <= 31
            except (IndexError, ValueError):
                return False
        return False

    @staticmethod
    def _classify_network(lan_ip: str) -> tuple[bool, str, str | None]:
        """Classify the detected LAN IP for the diagnostics network check.

        Returns (ok, detail, guidance).  guidance is None when the check
        passes.
        """
        if not lan_ip or lan_ip.startswith("127."):
            return (False, "No LAN address detected",
                    "No LAN address detected — check that WiFi/Ethernet is connected to a network.")
        if lan_ip.startswith("169.254."):
            return (False, "Link-local address (169.254.x.x)",
                    "No DHCP address (169.254 link-local) — check that WiFi/Ethernet is connected to a network.")
        if Application._is_private_ip(lan_ip):
            return (True, f"Private LAN ({lan_ip})", None)
        return (False, f"Public/routable IP ({lan_ip})",
                "This device appears to be on a public/routable IP — you may be behind a VPN or on an "
                "isolated network. VPNs and client isolation prevent LAN discovery.")

    # ═══════════════════════════════════════════════════════════════
    # Round 19 — comprehensive diagnostics groups
    # ═══════════════════════════════════════════════════════════════
    # Every probe below is defensive: a missing manager / a probe failure
    # yields a warn/fail item with a hint instead of raising, so the panel
    # always gets a complete, renderable shape even on a degraded app.

    @staticmethod
    def _diag_item(item_id, status, detail, hint=None,
                   detail_key=None, detail_params=None,
                   hint_key=None, hint_params=None) -> dict:
        """One diagnostic item: ``{id, status, detail, hint, *_key/*_params}``.

        ``status`` is one of "ok" | "warn" | "fail".  ``detail``/``hint`` are
        raw fallback strings; the optional ``*_key``/``*_params`` let the web
        panel resolve a localized string first (mirroring the flat ``checks``
        contract).
        """
        item = {
            "id": item_id,
            "status": status,
            "detail": detail or "",
            "hint": hint or None,
        }
        if detail_key:
            item["detail_key"] = detail_key
            item["detail_params"] = dict(detail_params or {})
        if hint_key:
            item["hint_key"] = hint_key
            item["hint_params"] = dict(hint_params or {})
        return item

    @staticmethod
    def _diag_fmt_duration(secs) -> str:
        secs = max(int(secs or 0), 0)
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m {secs % 60}s"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h {mins % 60}m"
        days = hours // 24
        return f"{days}d {hours % 24}h"

    @staticmethod
    def _diag_fmt_bytes(n) -> str:
        n = int(n or 0)
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.1f} GB"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f} MB"
        if n >= 1_000:
            return f"{n / 1_000:.1f} KB"
        return f"{n} B"

    @staticmethod
    def _diag_effective_data_dir(cfg) -> "Path":
        """Resolve the effective data directory (custom ``cfg.data_dir`` wins)."""
        custom = str(getattr(cfg, "data_dir", "") or "").strip()
        if custom:
            return Path(custom)
        return _config_dir()

    @staticmethod
    def _diag_dir_writable(path: "Path") -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return os.access(str(path), os.W_OK)
        except Exception:
            return False

    @staticmethod
    def _diag_trash_size(mgr) -> int:
        """Total bytes in the AI-config recoverable trash (0 when empty)."""
        base = mgr._trash_base()
        if not base.is_dir():
            return 0
        total = 0
        for p in base.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
        return total

    def _build_diag_groups(self, ctx: dict) -> dict:
        """Comprehensive grouped diagnostics (Round 19).

        ``ctx`` carries the already-computed network/firewall probe results
        from ``_get_diagnostics`` so the groups stay consistent with the flat
        ``checks`` list.  Returns ``{group_id: {label_key, items: [...]}}``.
        """
        return {
            "system": self._diag_group_system(),
            "network": self._diag_group_network(ctx),
            "internet": self._diag_group_internet(),
            "ai_config": self._diag_group_aiconfig(),
            "chat": self._diag_group_chat(),
            "transfer": self._diag_group_transfer(),
            "filesystem": self._diag_group_filesystem(),
        }

    def _diag_group_system(self) -> dict:
        items = []
        try:
            from internal.version import __version__ as _ver
        except Exception:
            _ver = "?"
        items.append(self._diag_item(
            "app_version", "ok", f"Version {_ver}",
            detail_key="diag.v2.item.app_version.detail",
            detail_params={"version": _ver}))
        try:
            uptime = max(int(time.time()) - int(getattr(self, "_start_time", 0) or 0), 0)
        except Exception:
            uptime = 0
        _up = self._diag_fmt_duration(uptime)
        items.append(self._diag_item(
            "uptime", "ok", f"Running for {_up}",
            detail_key="diag.v2.item.uptime.detail",
            detail_params={"uptime": _up}))
        # Data directory + writability.
        try:
            data_dir = self._diag_effective_data_dir(self.cfg)
            writable = self._diag_dir_writable(data_dir)
            if writable:
                items.append(self._diag_item(
                    "data_dir", "ok", str(data_dir),
                    detail_key="diag.v2.item.data_dir.detail",
                    detail_params={"dir": str(data_dir)}))
            else:
                items.append(self._diag_item(
                    "data_dir", "warn", f"{data_dir} (not writable)",
                    detail_key="diag.v2.item.data_dir.warn.detail",
                    detail_params={"dir": str(data_dir)},
                    hint="Data directory is not writable — backups and history may fail.",
                    hint_key="diag.v2.item.data_dir.warn.hint"))
        except Exception:
            items.append(self._diag_item(
                "data_dir", "fail", "Data directory unavailable",
                detail_key="diag.v2.item.data_dir.fail.detail",
                hint="Could not access the data directory.",
                hint_key="diag.v2.item.data_dir.fail.hint"))
        # Log path + writability.
        try:
            log_path = _get_log_path()
            log_writable = self._diag_dir_writable(log_path.parent)
            if log_writable:
                items.append(self._diag_item(
                    "log_path", "ok", str(log_path),
                    detail_key="diag.v2.item.log_path.detail",
                    detail_params={"path": str(log_path)}))
            else:
                items.append(self._diag_item(
                    "log_path", "warn", str(log_path),
                    detail_key="diag.v2.item.log_path.warn.detail",
                    detail_params={"path": str(log_path)},
                    hint="Log directory is not writable — logging may fail.",
                    hint_key="diag.v2.item.log_path.warn.hint"))
        except Exception:
            items.append(self._diag_item(
                "log_path", "warn", "Log path unavailable",
                detail_key="diag.v2.item.log_path.unavailable.detail"))
        return {"label_key": "diag.v2.group.system", "items": items}

    def _diag_group_network(self, ctx: dict) -> dict:
        items = []
        lan_ip = ctx.get("lan_ip", "")
        # LAN IP (cached).
        if not lan_ip or lan_ip.startswith("127."):
            items.append(self._diag_item(
                "lan_ip", "fail", "No LAN address detected",
                detail_key="diag.v2.item.lan_ip.nolan.detail",
                hint="No LAN address detected — check that WiFi/Ethernet is connected to a network.",
                hint_key="diag.v2.item.lan_ip.warn.hint"))
        elif lan_ip.startswith("169.254."):
            items.append(self._diag_item(
                "lan_ip", "warn", f"Link-local address ({lan_ip})",
                detail_key="diag.v2.item.lan_ip.linklocal.detail",
                detail_params={"lan_ip": lan_ip},
                hint="No DHCP address (169.254 link-local) — check that WiFi/Ethernet is connected to a network.",
                hint_key="diag.v2.item.lan_ip.warn.hint"))
        elif self._is_private_ip(lan_ip):
            items.append(self._diag_item(
                "lan_ip", "ok", f"{lan_ip} (private LAN)",
                detail_key="diag.v2.item.lan_ip.ok.detail",
                detail_params={"lan_ip": lan_ip}))
        else:
            items.append(self._diag_item(
                "lan_ip", "warn", f"{lan_ip} (public/routable)",
                detail_key="diag.v2.item.lan_ip.public.detail",
                detail_params={"lan_ip": lan_ip},
                hint="This device appears to be on a public/routable IP — you may be behind a VPN or on an isolated network.",
                hint_key="diag.v2.item.lan_ip.warn.hint"))
        # TCP port listening.
        port = ctx.get("port", 0)
        if ctx.get("server_running"):
            items.append(self._diag_item(
                "tcp_port", "ok", f"Listening on port {port}",
                detail_key="diag.v2.item.tcp_port.ok.detail",
                detail_params={"port": port}))
        else:
            items.append(self._diag_item(
                "tcp_port", "fail", f"Not listening on port {port}",
                detail_key="diag.v2.item.tcp_port.fail.detail",
                detail_params={"port": port},
                hint="Another app may be using the port, or the firewall blocks it. Try a different port in Settings → Network.",
                hint_key="diag.v2.item.tcp_port.fail.hint"))
        # mDNS service registration.
        if ctx.get("discovery_running"):
            items.append(self._diag_item(
                "mdns_service", "ok", "mDNS discovery active",
                detail_key="diag.v2.item.mdns_service.ok.detail"))
        else:
            items.append(self._diag_item(
                "mdns_service", "fail", "mDNS discovery not active",
                detail_key="diag.v2.item.mdns_service.fail.detail",
                hint="mDNS discovery isn't active. If you're on a guest/enterprise WiFi, AP/client isolation blocks discovery.",
                hint_key="diag.v2.item.mdns_service.fail.hint"))
        # Web service (enabled / port / running).
        web_enabled = bool(getattr(self.cfg, "web_enabled", False))
        web_port = ctx.get("web_port", 0)
        if not web_enabled:
            items.append(self._diag_item(
                "web_service", "warn", "Disabled",
                detail_key="diag.v2.item.web_service.off.detail",
                hint="Enable remote access in Settings → Remote access to control this device from a phone or browser.",
                hint_key="diag.v2.item.web_service.off.hint"))
        elif ctx.get("web_running"):
            items.append(self._diag_item(
                "web_service", "ok", f"Enabled on :{web_port}",
                detail_key="diag.v2.item.web_service.ok.detail",
                detail_params={"web_port": web_port}))
        else:
            items.append(self._diag_item(
                "web_service", "fail", "Enabled but not running",
                detail_key="diag.v2.item.web_service.fail.detail"))
        # Firewall recommendation (reuses the flat-check probe result).
        if ctx.get("fw_ok"):
            items.append(self._diag_item(
                "firewall", "ok", ctx.get("fw_detail") or "No firewall blockage detected",
                detail_key=ctx.get("fw_detail_key") or "diag.v2.item.firewall.ok.detail",
                detail_params=ctx.get("fw_detail_params") or {}))
        else:
            items.append(self._diag_item(
                "firewall", "fail", ctx.get("fw_detail") or "Firewall may be blocking",
                detail_key=ctx.get("fw_detail_key") or "diag.v2.item.firewall.fail.detail",
                detail_params=ctx.get("fw_detail_params") or {},
                hint=ctx.get("fw_guidance") or "The firewall may be blocking ClipSync.",
                hint_key=ctx.get("fw_guidance_key") or "diag.v2.item.firewall.fail.hint",
                hint_params=ctx.get("fw_guidance_params") or {}))
        return {"label_key": "diag.v2.group.network", "items": items}

    def _diag_group_internet(self) -> dict:
        items = []
        enabled = bool(getattr(self.cfg, "internet_sync_enabled", False))
        if enabled:
            items.append(self._diag_item(
                "internet_enabled", "ok", "Enabled",
                detail_key="diag.v2.item.internet_enabled.ok.detail"))
        else:
            items.append(self._diag_item(
                "internet_enabled", "warn", "Disabled",
                detail_key="diag.v2.item.internet_enabled.off.detail",
                hint="Enable internet sync in Settings → Internet sync to sync across networks.",
                hint_key="diag.v2.item.internet_enabled.off.hint"))
        # Relay state.
        try:
            relay_state = self._get_relay_state()
        except Exception:
            relay_state = "off"
        if relay_state == "online":
            items.append(self._diag_item(
                "relay_state", "ok", "Connected to relay",
                detail_key="diag.v2.item.relay_state.online.detail"))
        elif relay_state == "connecting":
            items.append(self._diag_item(
                "relay_state", "warn", "Connecting to relay…",
                detail_key="diag.v2.item.relay_state.connecting.detail"))
        elif relay_state == "error":
            items.append(self._diag_item(
                "relay_state", "fail", "Relay connection error",
                detail_key="diag.v2.item.relay_state.error.detail",
                hint="The relay could not be reached. Check your internet connection.",
                hint_key="diag.v2.item.relay_state.error.hint"))
        else:
            items.append(self._diag_item(
                "relay_state", "warn", "Relay off (internet sync disabled)",
                detail_key="diag.v2.item.relay_state.off.detail"))
        # Broker list — inferred from config + relay state (no real connect).
        brokers = [b for b in getattr(self.cfg, "relay_brokers", [])
                   if isinstance(b, str) and b]
        if not brokers:
            items.append(self._diag_item(
                "brokers", "fail", "No brokers configured",
                detail_key="diag.v2.item.brokers.fail.detail",
                hint="Add at least one public MQTT relay in Settings → Internet sync.",
                hint_key="diag.v2.item.brokers.fail.hint"))
        elif relay_state == "online":
            items.append(self._diag_item(
                "brokers", "ok", f"{len(brokers)} broker(s) configured, reachable",
                detail_key="diag.v2.item.brokers.ok.detail",
                detail_params={"count": len(brokers)}))
        elif relay_state == "error":
            items.append(self._diag_item(
                "brokers", "fail", f"{len(brokers)} broker(s) configured, none reachable",
                detail_key="diag.v2.item.brokers.fail_reachable.detail",
                detail_params={"count": len(brokers)},
                hint="None of the configured relays could be reached. Check your internet connection.",
                hint_key="diag.v2.item.brokers.fail_reachable.hint"))
        else:
            items.append(self._diag_item(
                "brokers", "warn", f"{len(brokers)} broker(s) configured",
                detail_key="diag.v2.item.brokers.warn.detail",
                detail_params={"count": len(brokers)}))
        # Internet-paired (netpair) device count.
        netpair = getattr(self.cfg, "netpair_secrets", {}) or {}
        items.append(self._diag_item(
            "netpair_count", "ok", f"{len(netpair)} internet-paired device(s)",
            detail_key="diag.v2.item.netpair_count.detail",
            detail_params={"count": len(netpair)}))
        # Pending offline-queue sends.
        try:
            counts = self._delivery_counts()
            pending = sum(counts.get("peers", {}).values()) if isinstance(counts, dict) else 0
        except Exception:
            pending = -1
        if pending < 0:
            items.append(self._diag_item(
                "pending_count", "warn", "Delivery stats unavailable",
                detail_key="diag.v2.item.pending_count.unavailable.detail"))
        elif pending == 0:
            items.append(self._diag_item(
                "pending_count", "ok", "No pending sends",
                detail_key="diag.v2.item.pending_count.ok.detail"))
        else:
            items.append(self._diag_item(
                "pending_count", "warn", f"{pending} pending send(s)",
                detail_key="diag.v2.item.pending_count.warn.detail",
                detail_params={"count": pending},
                hint="Some clipboard items are queued and will be sent when the peer reconnects.",
                hint_key="diag.v2.item.pending_count.warn.hint"))
        return {"label_key": "diag.v2.group.internet", "items": items}

    def _diag_group_aiconfig(self) -> dict:
        items = []
        mgr = getattr(self, "aicfg_mgr", None)
        roots = [r for r in (getattr(self.cfg, "ai_config_paths", []) or [])
                 if isinstance(r, str) and r]
        if roots:
            items.append(self._diag_item(
                "watch_roots", "ok", f"{len(roots)} root(s)",
                detail_key="diag.v2.item.watch_roots.ok.detail",
                detail_params={"count": len(roots)}))
        else:
            items.append(self._diag_item(
                "watch_roots", "warn", "No watch roots",
                detail_key="diag.v2.item.watch_roots.warn.detail",
                hint="Add watch directories in AI config settings to inventory AI tool configs.",
                hint_key="diag.v2.item.watch_roots.warn.hint"))
        if mgr is None:
            for it in ("local_entries", "last_collected", "trash_size"):
                items.append(self._diag_item(
                    it, "warn", "Unavailable",
                    detail_key="diag.v2.item.unavailable.detail",
                    hint="This data isn't available in the current state.",
                    hint_key="diag.v2.item.unavailable.hint"))
            return {"label_key": "diag.v2.group.ai_config", "items": items}
        try:
            summary = mgr.local_summary()
            entry_count = int(summary.get("entry_count", 0) or 0)
            items.append(self._diag_item(
                "local_entries", "ok", f"{entry_count} local file(s)",
                detail_key="diag.v2.item.local_entries.detail",
                detail_params={"count": entry_count}))
            collected = float(summary.get("collected_at", 0.0) or 0.0)
            if collected > 0:
                ago = self._diag_fmt_duration(int(time.time() - collected))
                items.append(self._diag_item(
                    "last_collected", "ok", f"{ago} ago",
                    detail_key="diag.v2.item.last_collected.ok.detail",
                    detail_params={"ago": ago}))
            else:
                items.append(self._diag_item(
                    "last_collected", "warn", "Never collected",
                    detail_key="diag.v2.item.last_collected.warn.detail",
                    hint="Run a collection from the AI config panel, or add watch roots.",
                    hint_key="diag.v2.item.last_collected.warn.hint"))
        except Exception:
            items.append(self._diag_item(
                "local_entries", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
            items.append(self._diag_item(
                "last_collected", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
        try:
            trash_size = self._diag_trash_size(mgr)
            items.append(self._diag_item(
                "trash_size", "ok" if trash_size == 0 else "warn",
                self._diag_fmt_bytes(trash_size),
                detail_key="diag.v2.item.trash_size.detail",
                detail_params={"size": self._diag_fmt_bytes(trash_size)},
                hint=None if trash_size == 0 else "Recycle bin is non-empty — restore or clear it from the AI config panel.",
                hint_key=None if trash_size == 0 else "diag.v2.item.trash_size.warn.hint"))
        except Exception:
            items.append(self._diag_item(
                "trash_size", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
        return {"label_key": "diag.v2.group.ai_config", "items": items}

    def _diag_group_chat(self) -> dict:
        items = []
        mgr = getattr(self, "chat_mgr", None)
        if mgr is None:
            items.append(self._diag_item(
                "chat_sessions", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
                hint="This data isn't available in the current state.",
                hint_key="diag.v2.item.unavailable.hint"))
        else:
            try:
                count = len(mgr.get_sessions())
                items.append(self._diag_item(
                    "chat_sessions", "ok", f"{count} active session(s)",
                    detail_key="diag.v2.item.chat_sessions.detail",
                    detail_params={"count": count}))
            except Exception:
                items.append(self._diag_item(
                    "chat_sessions", "warn", "Unavailable",
                    detail_key="diag.v2.item.unavailable.detail"))
        return {"label_key": "diag.v2.group.chat", "items": items}

    def _diag_group_transfer(self) -> dict:
        items = []
        mgr = getattr(self, "file_transfer_mgr", None)
        if mgr is None:
            for it in ("active_transfers", "transfer_failures"):
                items.append(self._diag_item(
                    it, "warn", "Unavailable",
                    detail_key="diag.v2.item.unavailable.detail",
                    hint="This data isn't available in the current state.",
                    hint_key="diag.v2.item.unavailable.hint"))
            return {"label_key": "diag.v2.group.transfer", "items": items}
        try:
            active = sum(1 for t in mgr.get_transfers()
                         if t.get("status") not in ("completed", "cancelled", "failed"))
            items.append(self._diag_item(
                "active_transfers", "ok", f"{active} in progress",
                detail_key="diag.v2.item.active_transfers.detail",
                detail_params={"count": active}))
        except Exception:
            items.append(self._diag_item(
                "active_transfers", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
        try:
            hist = mgr.get_history()
            failures = sum(1 for t in hist if not t.get("success"))
            if failures:
                items.append(self._diag_item(
                    "transfer_failures", "warn", f"{failures} failed transfer(s)",
                    detail_key="diag.v2.item.transfer_failures.warn.detail",
                    detail_params={"count": failures},
                    hint="Check the Transfers panel and retry any failed transfers.",
                    hint_key="diag.v2.item.transfer_failures.warn.hint"))
            else:
                items.append(self._diag_item(
                    "transfer_failures", "ok", "No recent failures",
                    detail_key="diag.v2.item.transfer_failures.ok.detail"))
        except Exception:
            items.append(self._diag_item(
                "transfer_failures", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
        return {"label_key": "diag.v2.group.transfer", "items": items}

    def _diag_group_filesystem(self) -> dict:
        items = []
        # History database file size.
        try:
            db_path = (getattr(self.clipboard_history, "_db_path", None)
                       if self.clipboard_history is not None else None)
            if db_path and Path(db_path).exists():
                size = Path(db_path).stat().st_size
                items.append(self._diag_item(
                    "history_db_size", "ok", self._diag_fmt_bytes(size),
                    detail_key="diag.v2.item.history_db_size.detail",
                    detail_params={"size": self._diag_fmt_bytes(size)}))
            else:
                items.append(self._diag_item(
                    "history_db_size", "warn", "Missing or unreadable",
                    detail_key="diag.v2.item.history_db_size.warn.detail",
                    hint="History database is missing or unreadable — history may be lost.",
                    hint_key="diag.v2.item.history_db_size.warn.hint"))
        except Exception:
            items.append(self._diag_item(
                "history_db_size", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
        # Free disk space on the data directory.
        try:
            data_dir = self._diag_effective_data_dir(self.cfg)
            usage = shutil.disk_usage(str(data_dir))
            free = int(getattr(usage, "free", 0) or 0)
            low = free < 500 * 1024 * 1024  # < 500 MB
            items.append(self._diag_item(
                "disk_free", "warn" if low else "ok",
                f"{self._diag_fmt_bytes(free)} free" + (" (low)" if low else ""),
                detail_key=("diag.v2.item.disk_free.warn.detail" if low
                            else "diag.v2.item.disk_free.ok.detail"),
                detail_params={"free": self._diag_fmt_bytes(free)},
                hint="Low disk space — history and transfers may fail." if low else None,
                hint_key="diag.v2.item.disk_free.warn.hint" if low else None))
        except Exception:
            items.append(self._diag_item(
                "disk_free", "warn", "Unavailable",
                detail_key="diag.v2.item.unavailable.detail"))
        return {"label_key": "diag.v2.group.filesystem", "items": items}

    def _get_diagnostics(self) -> dict:
        """Return a live snapshot of core service state + actionable network checks."""
        import platform as _platform

        from internal.version import __version__

        port = int(getattr(self.cfg, "port", 0) or 0)
        web_port = int(getattr(self.cfg, "web_port", 0) or 0)

        # Defensive getattr for every manager: a degraded/partial app must
        # still produce a complete diagnostics payload (round 19 requirement).
        transport_mgr = getattr(self, "transport_mgr", None)
        discovery = getattr(self, "discovery", None)
        web_server = getattr(self, "web_server", None)

        server_running = bool(transport_mgr is not None
                              and getattr(transport_mgr, "_running", False))
        discovery_running = bool(discovery is not None
                                 and getattr(discovery, "is_browsing", False))
        advertising = bool(discovery is not None
                           and getattr(discovery, "is_advertising", False))
        web_running = bool(web_server is not None and web_server.is_running)

        try:
            lan_ip = web_server._get_lan_ip() if web_server is not None else ""
        except Exception:
            lan_ip = ""

        checks = []

        # 1. TCP server port
        if server_running:
            checks.append({"id": "server_port", "ok": True,
                           "detail": f"TCP server listening on {port}",
                           "detail_key": "diag.server_port.ok.detail",
                           "detail_params": {"port": port}, "guidance": None})
        else:
            checks.append({"id": "server_port", "ok": False,
                           "detail": f"TCP server not listening on {port}",
                           "detail_key": "diag.server_port.fail.detail",
                           "detail_params": {"port": port},
                           "guidance": (f"Port {port} is not listening — another app may be using it, "
                                        "or the firewall blocks it. Try a different port in Settings → Network."),
                           "guidance_key": "diag.server_port.fail.guidance",
                           "guidance_params": {"port": port}})

        # 2. mDNS discovery
        if discovery_running:
            checks.append({"id": "discovery", "ok": True,
                           "detail": "mDNS discovery active",
                           "detail_key": "diag.discovery.ok.detail", "guidance": None})
        else:
            checks.append({"id": "discovery", "ok": False,
                           "detail": "mDNS discovery not active",
                           "detail_key": "diag.discovery.fail.detail",
                           "guidance": ("mDNS discovery isn't active. If you're on a guest/enterprise WiFi, "
                                        "AP/client isolation blocks discovery — connect both devices to the "
                                        "same private network."),
                           "guidance_key": "diag.discovery.fail.guidance"})

        # 3. Advertising (device visible on network)
        if advertising:
            checks.append({"id": "advertising", "ok": True,
                           "detail": "device visible on network",
                           "detail_key": "diag.advertising.ok.detail", "guidance": None})
        else:
            checks.append({"id": "advertising", "ok": False,
                           "detail": "device not advertising",
                           "detail_key": "diag.advertising.fail.detail",
                           "guidance": "This device isn't advertising — enable 'Visible' in the overview.",
                           "guidance_key": "diag.advertising.fail.guidance"})

        # 4. Web companion
        if web_running:
            checks.append({"id": "web_companion", "ok": True,
                           "detail": f"Remote access on :{web_port}",
                           "detail_key": "diag.web_companion.ok.detail",
                           "detail_params": {"web_port": web_port}, "guidance": None})
        else:
            checks.append({"id": "web_companion", "ok": False,
                           "detail": "Remote access not running",
                           "detail_key": "diag.web_companion.fail.detail",
                           "guidance": "Remote access isn't running — enable it in Settings → Remote access.",
                           "guidance_key": "diag.web_companion.fail.guidance"})

        # 5. Network classification
        network_ok, network_detail, network_guidance = self._classify_network(lan_ip)
        network_detail_key, network_params = "diag.network.private.detail", {"lan_ip": lan_ip}
        network_guidance_key = None
        if not network_ok:
            if not lan_ip or lan_ip.startswith("127."):
                network_detail_key = "diag.network.nolan.detail"
                network_guidance_key = "diag.network.nolan.guidance"
            elif lan_ip.startswith("169.254."):
                network_detail_key = "diag.network.linklocal.detail"
                network_guidance_key = "diag.network.linklocal.guidance"
            else:
                network_detail_key = "diag.network.public.detail"
                network_guidance_key = "diag.network.public.guidance"
                network_params = {"lan_ip": lan_ip}
        checks.append({"id": "network", "ok": network_ok,
                       "detail": network_detail, "guidance": network_guidance,
                       "detail_key": network_detail_key, "detail_params": network_params,
                       "guidance_key": network_guidance_key,
                       "guidance_params": network_params})

        # 6. Firewall — best-effort OS-level check with a "request" action.
        fw_ok, fw_detail, fw_guidance = True, "No firewall blockage detected", None
        fw_detail_key, fw_detail_params = "diag.firewall.ok.detail", {}
        fw_guidance_key, fw_guidance_params = None, {}
        try:
            import subprocess as _sp
            if _platform.system() == "Darwin":
                _out = _sp.run(
                    ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"],
                    capture_output=True, text=True, timeout=3,
                ).stdout or ""
                if "enabled" in _out.lower():
                    fw_detail = "macOS firewall is enabled"
                    fw_detail_key = "diag.firewall.macos_ok.detail"
                    fw_guidance = ("The macOS firewall is on. If other devices can't reach this "
                                   "computer, allow ClipSync: System Settings → Network → Firewall → "
                                   "Options, or tap 'Request permission' to open it.")
                    fw_guidance_key = "diag.firewall.macos_ok.guidance"
                    # Only fail the check when discovery is also failing (strong signal).
                    fw_ok = discovery_running
            elif _platform.system() == "Windows":
                try:
                    # Both the TCP sync port and the web companion port need
                    # to be open — a single-port rule would otherwise show up
                    # as a "wrong port" mismatch.
                    fw_ports = [self.cfg.port, self.cfg.web_port]
                    ok, detail = web_server.check_firewall_rule(fw_ports) if web_server is not None else (True, "")
                    if not ok:
                        fw_ok, fw_detail = False, detail
                        fw_guidance = ("The Windows firewall may be blocking ClipSync. Tap "
                                       "'Request permission' to add an allow rule for ports "
                                       f"{self.cfg.port} and {self.cfg.web_port}.")
                        fw_guidance_key = "diag.firewall.win_fail.guidance"
                        fw_guidance_params = {"port": f"{self.cfg.port}, {self.cfg.web_port}"}
                        if detail.startswith("Wrong port"):
                            # Stale rule with the wrong port — surface both values.
                            import re as _re
                            _m = _re.search(r"\(got ([^)]+), needs ([^)]+)\)", detail)
                            fw_detail_key = "diag.firewall.wrongport.detail"
                            fw_detail_params = {"actual": _m.group(1), "port": _m.group(2)} if _m else {}
                        else:
                            fw_detail_key = "diag.firewall.win_blocked.detail"
                except Exception:
                    pass
            elif _platform.system() == "Linux":
                # ufw / firewalld detection + port allow check (best-effort).
                fw_detail = "No Linux firewall detected"
                fw_detail_key = "diag.firewall.linux_none.detail"
                for cmd, name in ((["ufw", "status"], "ufw"),
                                  (["systemctl", "is-active", "firewalld"], "firewalld")):
                    try:
                        _out = _sp.run(cmd, capture_output=True, text=True, timeout=3).stdout or ""
                    except Exception:
                        continue
                    # Whole-word match: "active" is a substring of "inactive",
                    # so a bare `in` check would misread "Status: inactive".
                    if "active" in _out.split():
                        fw_detail = f"{name} firewall is active"
                        fw_detail_key = "diag.firewall.linux_active.detail"
                        fw_detail_params = {"name": name}
                        fw_guidance = (f"The {name} firewall is on. If other devices can't reach "
                                       f"this computer, allow ports {self.cfg.port} and "
                                       f"{self.cfg.web_port}: 'sudo ufw allow {self.cfg.port}/tcp' and "
                                       f"'sudo ufw allow {self.cfg.web_port}/tcp' (or the firewalld equivalent).")
                        fw_guidance_key = "diag.firewall.linux_active.guidance"
                        fw_guidance_params = {"name": name, "port": self.cfg.port,
                                              "web_port": self.cfg.web_port}
                        fw_ok = discovery_running
                        break
        except Exception:
            pass
        checks.append({"id": "firewall", "ok": fw_ok, "detail": fw_detail,
                       "detail_key": fw_detail_key, "detail_params": fw_detail_params,
                       "guidance": fw_guidance,
                       "guidance_key": fw_guidance_key, "guidance_params": fw_guidance_params})

        # 7. Permissions — macOS Local Network (15+) is required for LAN discovery.
        perm_ok, perm_detail, perm_guidance = True, "No permission issues detected", None
        perm_detail_key, perm_detail_params = "diag.permissions.ok.detail", {}
        perm_guidance_key, perm_guidance_params = None, {}
        try:
            if _platform.system() == "Darwin":
                _mv = [int(x) for x in _platform.mac_ver()[0].split(".")[:2]]
                if len(_mv) == 2 and _mv[0] >= 15:
                    if not discovery_running:
                        perm_ok = False
                        perm_detail = "Local Network permission may be missing (macOS 15+)"
                        perm_detail_key = "diag.permissions.fail.detail"
                        perm_guidance = ("macOS 15+ needs 'Local Network' permission to discover other "
                                         "devices. Tap 'Request permission' to open System Settings → "
                                         "Privacy & Security → Local Network and allow ClipSync.")
                        perm_guidance_key = "diag.permissions.fail.guidance"
                    else:
                        perm_detail = "Local Network permission granted"
                        perm_detail_key = "diag.permissions.ok_macos.detail"
        except Exception:
            pass
        checks.append({"id": "permissions", "ok": perm_ok, "detail": perm_detail,
                       "detail_key": perm_detail_key, "detail_params": perm_detail_params,
                       "guidance": perm_guidance,
                       "guidance_key": perm_guidance_key, "guidance_params": perm_guidance_params})

        # 8. mDNS service (Linux: avahi-daemon is required for discovery).
        mdns_ok, mdns_detail, mdns_guidance = True, "mDNS service available", None
        mdns_detail_key, mdns_guidance_key = "diag.mdns.ok.detail", None
        try:
            if _platform.system() == "Linux":
                _out = _sp.run(["systemctl", "is-active", "avahi-daemon"],
                               capture_output=True, text=True, timeout=3).stdout or ""
                if "active" not in _out.lower():
                    mdns_ok = False
                    mdns_detail = "avahi-daemon is not running"
                    mdns_detail_key = "diag.mdns.fail.detail"
                    mdns_guidance = ("mDNS discovery needs avahi-daemon. Install/start it: "
                                     "'sudo apt install avahi-daemon' then 'sudo systemctl start avahi-daemon'.")
                    mdns_guidance_key = "diag.mdns.fail.guidance"
        except Exception:
            pass
        checks.append({"id": "mdns", "ok": mdns_ok, "detail": mdns_detail,
                       "detail_key": mdns_detail_key, "guidance": mdns_guidance,
                       "guidance_key": mdns_guidance_key})

        # 9. Clipboard tool (Linux: xclip / wl-paste needed to read the clipboard).
        if _platform.system() == "Linux":
            _clip_ok = bool(_sp.run(["sh", "-c", "command -v xclip || command -v wl-paste"],
                                    capture_output=True, text=True, timeout=3).stdout.strip())
            checks.append({"id": "clipboard_tool", "ok": _clip_ok,
                           "detail": "clipboard tool present" if _clip_ok else "no xclip / wl-paste",
                           "detail_key": "diag.clipboard_tool.ok.detail" if _clip_ok
                                        else "diag.clipboard_tool.fail.detail",
                           "guidance": None if _clip_ok else ("Clipboard capture needs xclip (X11) or "
                                                             "wl-paste (Wayland). Install one: "
                                                             "'sudo apt install xclip' or 'sudo apt install wl-clipboard'."),
                           "guidance_key": None if _clip_ok else "diag.clipboard_tool.fail.guidance"})
        # summary: "fail" if a critical check (server/discovery/network) is down,
        # "warn" if only advertising/web is down, else "ok".
        critical_ids = ("server_port", "discovery", "network", "mdns")
        if any(not c["ok"] for c in checks if c["id"] in critical_ids):
            summary = "fail"
        elif any(not c["ok"] for c in checks):
            summary = "warn"
        else:
            summary = "ok"

        transport_mgr = getattr(self, "transport_mgr", None)
        pairing_mgr = getattr(self, "pairing_mgr", None)
        connected = (transport_mgr.get_connected_peers()
                     if transport_mgr is not None else [])
        paired = (pairing_mgr.get_paired_peers()
                  if pairing_mgr is not None else [])

        # Round 19: comprehensive grouped diagnostics.  Reuses the probe
        # results computed above so the flat checks and the groups agree.
        groups = self._build_diag_groups({
            "port": port,
            "web_port": web_port,
            "server_running": server_running,
            "discovery_running": discovery_running,
            "advertising": advertising,
            "web_running": web_running,
            "lan_ip": lan_ip,
            "fw_ok": fw_ok,
            "fw_detail": fw_detail,
            "fw_detail_key": fw_detail_key,
            "fw_detail_params": fw_detail_params,
            "fw_guidance": fw_guidance,
            "fw_guidance_key": fw_guidance_key,
            "fw_guidance_params": fw_guidance_params,
        })

        return {
            "v2": True,
            "summary": summary,
            "checks": checks,
            "groups": groups,
            "discovery_running": discovery_running,
            "server_running": server_running,
            "connected_count": len(connected),
            "paired_count": len(paired),
            "web_companion_running": web_running,
            "web_port": web_port,
            "lan_ip": lan_ip,
            "os": _platform.system(),
            "version": __version__,
        }

    def _handle_update_download(self) -> dict:
        """Download the latest release into the user's Downloads folder."""
        from pathlib import Path

        from internal.system.updater import download_latest_release
        dest_dir = str(Path.home() / "Downloads")
        try:
            path, reason = download_latest_release(dest_dir)
        except Exception as exc:
            logger.exception("Update download failed")
            return {"ok": False, "path": "", "error": str(exc)}
        if path:
            return {"ok": True, "path": path, "error": None}
        # The updater returns a readable (localized) reason for expected
        # failures — e.g. no release asset for this platform — so the user
        # sees the real cause instead of a generic "download failed".
        return {"ok": False, "path": "", "error": reason or "download failed"}

    def _pairing_sas(self, peer_id: str) -> str:
        """Short Authentication String shown on both ends of a pairing.

        Derived from this device's and the peer's certificate fingerprints
        (internal/security/fingerprint.py).  Empty when either fingerprint is
        unknown — the UI then simply omits the SAS row.
        """
        try:
            from internal.security.fingerprint import sas_code
            mine = ""
            theirs = ""
            if self.pairing_mgr is not None:
                mine = self.pairing_mgr.get_identity().fingerprint
                theirs = self.pairing_mgr.get_peer_fingerprint(peer_id)
            if mine and theirs:
                return sas_code(mine, theirs)
        except Exception:
            logger.debug("SAS derivation failed for %s", (peer_id or "")[:12],
                         exc_info=True)
        return ""

    def _get_pending(self) -> list:
        pending = self.pairing_mgr.get_pending_pairings() if self.pairing_mgr else []
        # Attach the pairing SAS as a 5th element so the web devices API can
        # surface it on the confirmation card (see internal/web/api/devices.py).
        result = [
            tuple(p) + (self._pairing_sas(p[0]),)
            for p in pending if isinstance(p, (tuple, list)) and p
        ]
        # The pairing manager drops expired requests entirely, so a request that
        # times out silently vanishes from the devices refresh.  Surface an
        # "expired" row for one refresh instead (and notify once).
        if not self._pairing_req_track:
            return result
        try:
            _PAIRING_TIMEOUT_SECS = 300  # matches internal.security.pairing.PAIRING_TIMEOUT
            now = time.time()
            live_ids = {
                p[0] for p in pending if isinstance(p, (tuple, list)) and p
            }
            for pid, info in list(self._pairing_req_track.items()):
                if pid in live_ids:
                    continue  # still pending
                # Not pending anymore — either resolved (paired/rejected) or
                # expired.  Only surface "expired" once, and only when the
                # request actually lived past the pairing timeout.
                if info.get("surfaced_expired"):
                    continue
                if now - info.get("first_seen", now) >= _PAIRING_TIMEOUT_SECS:
                    info["surfaced_expired"] = True
                    result.append((
                        pid, info.get("code", ""), info.get("peer_name", pid), "expired",
                    ))
                    self._notify("notify_pairing", "Pairing", T("pairing.state.expired"))
                self._pairing_req_track.pop(pid, None)
        except Exception:
            logger.debug("Pairing expiry tracking failed", exc_info=True)
        return result

    def _on_pair(self, peer_id: str, code: str) -> bool:
        result = self.pairing_mgr.confirm_pairing(peer_id, code)
        if result:
            status = self.pairing_mgr.get_pairing_status(peer_id)
            # Two-sided handshake: tell the peer we confirmed, so its UI can
            # move out of "pending" (and prompt the user there if needed).
            self._send_pairing_msg(peer_id, "pairing_confirm")
            if status == PAIRING_STATUS_PAIRED:
                self._notify("notify_pairing", "Pairing", T("pairing.notify.completed"))
            else:
                self._notify("notify_pairing", "Pairing", T("pairing.state.confirmed_waiting"))
            self._save_cfg_and_peers()
            if peer_id not in self.transport_mgr.get_connected_peers():
                self._on_connect(peer_id)
            self._push_web("broadcast", "pairing_resolved", {"peer_id": peer_id, "status": status})
            # Refresh the device list so the paired device shows its new
            # status immediately instead of waiting for the next poll cycle.
            self._push_web("broadcast_devices")
        return result

    def _on_unpair(self, peer_id: str) -> None:
        self.pairing_mgr.unpair_peer(peer_id)
        self.pairing_mgr.reject_pairing(peer_id)
        # An unpaired peer is no longer trusted — close any live chat.
        self._close_chat_for_peer(peer_id)
        if peer_id in self.cfg.peers:
            self.cfg.peers[peer_id].paired = False
        # Tell the peer we unpaired, before the connection is torn down.
        self._send_pairing_msg(peer_id, "pairing_unpair")
        self.transport_mgr.forget_peer(peer_id)
        # Round 17: a forgotten peer must stop burning relay retries, and its
        # base64 payloads must not linger on disk in relay_pending.json.
        getattr(self, "_delivery_clear_peer", lambda pid: None)(peer_id)
        self._save_cfg_encrypted()
        self._push_web("broadcast_devices")

    def _send_pairing_msg(self, peer_id: str, msg_type: str) -> None:
        """Send a pairing lifecycle message to one peer (best effort)."""
        try:
            self.transport_mgr.send_to_peer(peer_id, encode_frame({"msg_type": msg_type}))
        except Exception:
            logger.debug("Could not send %s to peer %s", msg_type, peer_id[:12], exc_info=True)

    def _close_chat_for_peer(self, peer_id: str) -> None:
        """End any nearby-chat session with *peer_id* (e.g. after unpair)."""
        cm = getattr(self, "chat_mgr", None)
        if cm is None:
            return
        try:
            for sess in cm.get_sessions():
                if sess.get("peer_id") == peer_id and sess["status"] in (
                    "inviting", "invited", "active",
                ):
                    cm.close_session(sess["session_id"], notify_peer=False)
        except Exception:
            logger.debug("close chat for peer failed", exc_info=True)

    def _handle_pairing_message(self, msg_type: str, payload: dict, peer_id: str | None) -> None:
        """A peer told us about its pairing decision. Keep both sides in sync."""
        if not peer_id:
            return
        if msg_type == "pairing_confirm":
            status = self.pairing_mgr.mark_peer_confirmed(peer_id)
            if status == PAIRING_STATUS_PAIRED:
                self._notify("notify_pairing", "Pairing", T("pairing.notify.completed"))
                self._save_cfg_and_peers()
            elif status == PAIRING_STATUS_PEER_CONFIRMED:
                name = self._cfg_peer_name(peer_id)
                self._notify(
                    "notify_pairing",
                    T("pairing.notify.peer_confirmed"),
                    T("pairing.notify.peer_confirmed_msg", name=name),
                )
            self._push_web("broadcast", "pairing_resolved", {"peer_id": peer_id, "status": status})
            self._push_web("broadcast_devices")
        elif msg_type == "pairing_reject":
            self.pairing_mgr.mark_peer_rejected(peer_id)
            name = self._cfg_peer_name(peer_id)
            self._notify(
                "notify_pairing",
                T("pairing.notify.peer_rejected"),
                T("pairing.notify.peer_rejected_msg", name=name),
            )
            self._push_web("broadcast_devices")
        elif msg_type == "pairing_unpair":
            self.pairing_mgr.mark_peer_unpaired(peer_id)
            # The peer dropped the trust relationship; end the chat too.
            self._close_chat_for_peer(peer_id)
            name = self._cfg_peer_name(peer_id)
            self._notify(
                "notify_pairing",
                T("pairing.notify.unpaired_by_peer"),
                T("pairing.notify.unpaired_by_peer_msg", name=name),
            )
            if peer_id in self.cfg.peers:
                self.cfg.peers[peer_id].paired = False
            self._save_cfg_encrypted()
            self._push_web("broadcast_devices")

    def _cfg_peer_name(self, peer_id: str) -> str:
        """Best-effort display name for a peer from config or the pairing manager."""
        peer_cfg = self.cfg.peers.get(peer_id)
        if peer_cfg and peer_cfg.device_name:
            return peer_cfg.device_name
        for p in self.pairing_mgr.get_known_peers():
            if p.device_id == peer_id:
                return p.device_name
        return ""

    def _on_retrust_peer(self, peer_id: str) -> None:
        """Re-trust a device whose certificate changed (reinstall/reset).

        Replaces the pinned certificate with the freshly-presented one, keeps
        the peer paired, persists the config, and tries to reconnect.

        When no new certificate is available (the startup prompt, where the
        device hasn't connected yet), the peer is instead restored from config
        so it stays known; a later connection re-prompts with the real cert.
        """
        pending = self._pending_cert_peers.pop(peer_id, None)
        new_cert = pending[1] if pending else None
        peer_name = pending[0] if pending else self._cfg_peer_name(peer_id)
        try:
            if new_cert:
                if not self.pairing_mgr.update_peer_certificate(peer_id, new_cert):
                    # Peer not known to the pairing manager (e.g. skipped at
                    # startup) — (re)add it now, keeping it paired.
                    self.pairing_mgr.add_peer(
                        peer_id, peer_name or peer_id, new_cert, paired=True,
                    )
                peer_cfg = self.cfg.peers.get(peer_id)
                if peer_cfg:
                    peer_cfg.public_key_pem = new_cert
                    peer_cfg.paired = True
            else:
                # Startup prompt: no new cert is available yet — restore the
                # peer from config so it stays known and paired.
                peer_cfg = self.cfg.peers.get(peer_id)
                if peer_cfg:
                    self.pairing_mgr.add_peer(
                        peer_id, peer_cfg.device_name, peer_cfg.public_key_pem,
                        paired=peer_cfg.paired,
                    )
                    peer_cfg.paired = True
            self._save_cfg_and_peers()
        except Exception as e:
            logger.error("Failed to re-trust peer %s: %s", peer_id[:12], e)
            return
        logger.info("Re-trusted peer %s (%s)", peer_name or peer_id, peer_id[:12])
        self._on_connect(peer_id)

    def _on_keep_peer_unpaired(self, peer_id: str) -> None:
        """Keep a changed-cert device unpaired instead of re-trusting it."""
        self._pending_cert_peers.pop(peer_id, None)
        self.pairing_mgr.unpair_peer(peer_id)
        if peer_id in self.cfg.peers:
            self.cfg.peers[peer_id].paired = False
        try:
            self.transport_mgr.disconnect_peer(peer_id, reject=True)
        except Exception:
            logger.debug("disconnect_peer failed for %s", peer_id[:12], exc_info=True)
        self._save_cfg_encrypted()
        self._push_web("broadcast_devices")
        logger.info("Kept peer %s unpaired after certificate change", peer_id[:12])

    def _on_disconnect(self, peer_id: str) -> None:
        logger.info("User initiated disconnect from %s", peer_id)
        self.transport_mgr.disconnect_peer(peer_id, reject=True)
        # A user-initiated disconnect also abandons any outgoing file transfer
        # to that peer — fail it now rather than after a long timeout.
        if self.file_transfer_mgr is not None:
            self.file_transfer_mgr.fail_peer_transfers(peer_id)
        if getattr(self, "chat_mgr", None) is not None:
            try:
                self.chat_mgr.mark_peer_disconnected(peer_id)
            except Exception:
                logger.debug("chat mark_peer_disconnected failed", exc_info=True)

    def _on_connect(self, peer_id: str) -> bool:
        info = None
        with self._discovered_lock:
            info = self._discovered_peers.get(peer_id)
        if not info:
            hashed = Discovery._hash_device_id(peer_id)
            with self._discovered_lock:
                info = self._discovered_peers.get(hashed)
        if not info:
            resolved = self.transport_mgr.get_resolved_hashes()
            hash_id = None
            for h_id, r_id in resolved.items():
                if r_id == peer_id:
                    hash_id = h_id
                    break
            if hash_id:
                with self._discovered_lock:
                    info = self._discovered_peers.get(hash_id)
            if not info:
                peers = self.pairing_mgr.get_known_peers()
                target = next(
                    (p for p in peers if p.device_id == peer_id), None,
                )
                if target:
                    with self._discovered_lock:
                        for pid, pinfo in self._discovered_peers.items():
                            pname = pinfo["name"].lower()
                            tname = target.device_name.lower()
                            if pname == tname or tname.startswith(pname):
                                info = pinfo
                                break
        if not info:
            # Fall back to the last known address for a known/paired peer
            # even if it isn't currently advertising on mDNS (e.g. it dropped
            # off discovery for a moment right after pairing). Without this,
            # auto-connect after pairing fails with "peer not in discovered
            # list" and the user has to manually retry until it reappears.
            saved = self.transport_mgr.get_saved_address(peer_id)
            if saved:
                sname, saddr, sport = saved
                info = {"name": sname, "address": saddr, "port": sport}
        if not info:
            # Persisted last-known address (survives app restarts, unlike the
            # in-memory saved-address map which is populated per connection).
            peer_cfg = self.cfg.peers.get(peer_id)
            if peer_cfg and peer_cfg.last_ip:
                info = {
                    "name": peer_cfg.device_name or peer_cfg.device_id,
                    "address": peer_cfg.last_ip,
                    "port": peer_cfg.last_port or self.cfg.port,
                }
        if info:
            logger.info("User initiated pairing with %s (peer_id=%s)",
                        info["name"], peer_id[:12])
            peer_cfg = self.cfg.peers.get(peer_id)
            if peer_cfg:
                peer_cfg.last_ip = info["address"]
                peer_cfg.last_port = info["port"]
            self.transport_mgr.connect_to_peer(
                peer_id, info["name"], info["address"], info["port"],
            )
            # True means "connection attempt initiated" — the peer was found
            # on the network.  The TCP/TLS handshake itself completes
            # asynchronously and its result is reported via the transport.
            return True
        logger.warning("Cannot connect: peer %s not in discovered list",
                      peer_id[:12])
        return False

    def _on_remove(self, peer_id: str) -> None:
        self.pairing_mgr.remove_peer(peer_id)
        self.transport_mgr.disconnect_peer(peer_id)
        with self._discovered_lock:
            self._discovered_peers.pop(peer_id, None)
        self.cfg.peers.pop(peer_id, None)
        self._save_cfg_encrypted()
        self._push_web("broadcast_devices")

    def _on_edit_note(self, peer_id: str, note: str) -> None:
        if peer_id in self.cfg.peers:
            self.cfg.peers[peer_id].notes = note
            self._save_cfg_encrypted()

    # ═══════════════════════════════════════════════════════════════
    # History helpers
    # ═══════════════════════════════════════════════════════════════

    def _get_history(self) -> list:
        return self.clipboard_history.get_all()

    def _search_history(self, query: str) -> list:
        return self.clipboard_history.search(query)

    def _copy_from_history(self, entry_id) -> bool:
        # Resolve by entry_id (not a position) — the dashboard may show a
        # filtered/search subset whose indices differ from the full list.
        _, entry = self.clipboard_history.find_by_id(entry_id)
        if entry is None or "types" not in entry:
            return False
        types: dict = {}
        _type_map = {
            "TEXT": _CT.TEXT, "HTML": _CT.HTML,
            "IMAGE": _CT.IMAGE_PNG, "IMAGE_EMF": _CT.IMAGE_EMF,
            "RTF": _CT.RTF,
            "FILE": _CT.FILE, "URL": _CT.URL,
        }
        for key, b64_data in entry["types"].items():
            ct = _type_map.get(key)
            if ct is not None:
                types[ct] = _b64.b64decode(b64_data)
        if types:
            content = ClipboardContent(types=types, image_fmt=entry.get("image_fmt") or "")
            # Clear dedup state so the monitor event from this write
            # is not suppressed — the restored content will sync to peers.
            self.sync_mgr.reset_dedup_for_restore()
            create_writer().write(content)
            # paste_to_top: re-using an old item surfaces it as the most
            # recent history entry.
            if self.cfg.paste_to_top and entry.get("entry_id"):
                try:
                    self.clipboard_history.touch(entry["entry_id"])
                except Exception:
                    logger.debug("Failed to touch history entry", exc_info=True)
            return True
        return False

    def _clear_history(self) -> None:
        self.clipboard_history.clear()

    def _delete_history_item(self, entry_id) -> bool:
        return self.clipboard_history.delete_by_id(entry_id)

    def _clear_transfer_history(self) -> None:
        self.file_transfer_mgr.clear_history()

    def _open_file(self, file_path: str) -> None:
        """Open a file with the default OS application."""
        import subprocess
        import sys as _sys
        resolved = os.path.abspath(file_path) if file_path else ""
        if not os.path.isfile(resolved):
            self._notify_error(T("ui.file_not_found_title"),
                       T("ui.file_not_found_msg", path=file_path))
            return
        try:
            if _sys.platform == "win32":
                os.startfile(resolved)
            elif _sys.platform == "darwin":
                subprocess.run(["open", resolved], check=True)
            else:
                subprocess.run(["xdg-open", resolved], check=True)
        except Exception as e:
            logger.error("Failed to open file %s: %s", resolved, e)
            self._notify_error(T("ui.open_failed_title"),
                       T("ui.open_failed_msg", path=resolved))

    def _open_folder(self, file_path: str) -> None:
        """Open the containing folder in the OS file manager."""
        import subprocess
        import sys as _sys
        resolved = os.path.abspath(file_path) if file_path else ""
        if os.path.isfile(resolved):
            folder = os.path.dirname(resolved)
        elif os.path.isdir(resolved):
            folder = resolved
        else:
            self._notify_error(T("ui.file_not_found_title"),
                       T("ui.file_not_found_msg", path=file_path))
            return
        if not os.path.isdir(folder):
            self._notify_error(T("ui.folder_not_found_title"),
                       T("ui.folder_not_found_msg", path=folder))
            return
        try:
            if _sys.platform == "win32":
                # Use explorer.exe directly instead of os.startfile()
                # to avoid any file-association misrouting that could
                # launch a new instance of the app.
                subprocess.Popen(["explorer", folder])
            elif _sys.platform == "darwin":
                subprocess.run(["open", folder], check=True)
            else:
                subprocess.run(["xdg-open", folder], check=True)
        except Exception as e:
            logger.error("Failed to open folder %s: %s", folder, e)
            self._notify_error(T("ui.open_failed_title"),
                       T("ui.open_failed_msg", path=folder))

    def _retry_file_transfer(self, file_path: str) -> None:
        """Retry sending a file that previously failed."""

        def _send(peer_id):
            if peer_id is None:
                return

            def _send_fn(data: bytes):
                self.transport_mgr.send_to_peer(peer_id, data)

            try:
                transfer_id = self.file_transfer_mgr.send_file(file_path, _send_fn)
                if transfer_id:
                    self._transfer_directions[transfer_id] = "outgoing"
                logger.info("Retried file transfer: %s (%s)", file_path, transfer_id[:8])
                self._notify("notify_transfer", T("ui.file_transfer"),
                             T("transfer.sending_file", name=os.path.basename(file_path)))
            except OSError as e:
                logger.error("Failed to retry sending file %s: %s", file_path, e)

        self._pick_peer_then(_send)

    # ═══════════════════════════════════════════════════════════════
    # Discovery / visibility toggles
    # ═══════════════════════════════════════════════════════════════

    def _on_toggle_discovery(self, enabled: bool) -> None:
        if enabled:
            self.discovery.start_browsing()
        else:
            self.discovery.stop_browsing()

    def _on_toggle_visibility(self, enabled: bool) -> None:
        if enabled:
            self.discovery.start_advertising()
        else:
            self.discovery.stop_advertising()

    def _on_security_alert(self, peer_name: str, peer_id: str, expected: str,
                           received: str, new_cert_pem: str) -> None:
        """Handle a certificate-change alert from the transport (any thread).

        Records the newly-presented certificate so it can be re-trusted, and
        schedules a Trust-again / Keep-unpaired prompt on the main thread.
        Throttled per peer so a reconnecting device doesn't stack dialogs.
        """
        if not peer_id:
            logger.warning("Cert change alert without peer_id for %s", peer_name)
            return
        now = time.monotonic()
        last = self._cert_alert_throttle.get(peer_id)
        if last is not None and now - last < 30.0:
            logger.debug("Cert-change prompt for %s suppressed (throttled)", peer_id[:12])
            return
        self._cert_alert_throttle[peer_id] = now
        if new_cert_pem:
            self._pending_cert_peers[peer_id] = (peer_name, new_cert_pem)
        logger.warning(
            "Certificate changed for %s (%s) — prompting user "
            "(expected=%s..., got=%s...)",
            peer_name, peer_id[:12],
            expected[:16] if expected else "n/a",
            received[:16] if received else "n/a",
        )
        self.root.after(0, lambda: self._show_cert_change_dialog(peer_name, peer_id))

    def _show_cert_change_dialog(self, peer_name: str, peer_id: str) -> None:
        """Present the Trust-again / Keep-unpaired choice (main thread)."""
        message = T("cert.changed_message", name=peer_name)

        def _apply(choice):
            if choice is True:
                self._on_retrust_peer(peer_id)
            elif choice is False:
                self._on_keep_peer_unpaired(peer_id)
            else:
                # No interactive UI available — inform via notification/toast
                # and leave the peer's state unchanged (throttle prevents
                # prompt spam).
                logger.info("No UI to prompt for cert change of %s — notifying only",
                            peer_id[:12])
                self._notify_info(T("cert.changed_title"), message)

        self._ask_retrust_choice(message, _apply)

    def _prompt_cert_warnings_startup(self) -> None:
        """Show one dialog listing peers whose certificates changed at startup.

        Scheduled after the main loop is running so it never blocks startup.
        """
        warnings = getattr(self, "_cert_warnings", []) or []
        if not warnings:
            return
        # Consume the list so the prompt shows at most once per session.
        warnings, self._cert_warnings = list(warnings), []
        names = ", ".join(name for _, name in warnings[:3])
        if len(warnings) > 3:
            names += f" +{len(warnings) - 3}"
        message = T("cert.changed_message", name=names)

        def _apply(choice):
            if choice is True:
                for peer_id, _name in warnings:
                    self._on_retrust_peer(peer_id)
            elif choice is False:
                for peer_id, _name in warnings:
                    self._on_keep_peer_unpaired(peer_id)
            else:
                logger.info("No UI available for the startup cert-change prompt")

        self._ask_retrust_choice(message, _apply)

    def _ask_retrust_choice(self, message: str, on_choice) -> None:
        """Ask Trust-again vs Keep-unpaired, without blocking the main thread.

        ``on_choice(True|False|None)`` runs on the main thread.  True/False is
        the user's answer; None means no UI was available (or the web dialog
        timed out / had no client attached).
        """
        title = T("cert.changed_title")
        if self._is_webview():
            mgr = self.web_server.dialog_mgr if self.web_server else None
            ws = mgr.ws_manager if mgr else None
            if ws is None or ws.client_count == 0:
                on_choice(None)
                return

            def _on_result(result):
                on_choice(None if result is None
                          else result.get("action") == "accept")

            # The confirm dialog waits up to two minutes for a human; run it
            # on a worker so the Tk main loop keeps servicing hotkeys, tray
            # polling and timers while the dialog is open.
            self._web_dialog_async(
                "confirm", _on_result, title=title, message=message,
                accept_label=T("cert.trust_again"),
                reject_label=T("cert.keep_unpaired"),
                timeout=120,
            )
            return
        on_choice(self._ask_retrust_desktop(title, message))

    def _ask_retrust_desktop(self, title: str, message: str) -> bool:
        """Themed Trust-again / Keep-unpaired dialog for the desktop UI.

        Returns True for "Trust again", False for "Keep unpaired".
        """
        import customtkinter as ctk

        dlg = ctk.CTkToplevel(self.root)
        dlg.title(title)
        dlg.resizable(False, False)

        w, h = 440, 230
        if self.root.winfo_viewable():
            pw, ph = self.root.winfo_width(), self.root.winfo_height()
            px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
        else:
            x = (self.root.winfo_screenwidth() - w) // 2
            y = (self.root.winfo_screenheight() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        result = [True]

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)

        ctk.CTkLabel(
            body, text="⚠️", font=ctk.CTkFont(size=22),
        ).pack(anchor="w", pady=(0, 6))

        ctk.CTkLabel(
            body, text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#F39C12",
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(
            body, text=message, justify="left",
            font=ctk.CTkFont(size=12),
            text_color=("gray30", "gray80"),
            wraplength=390,
        ).pack(anchor="w")

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", pady=(18, 0))

        def _close(value):
            result[0] = value
            dlg.destroy()

        ctk.CTkButton(
            btn_row, text=T("cert.keep_unpaired"), width=110, height=32,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray60"),
            border_color=("gray60", "gray50"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=12),
            command=lambda: _close(False),
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text=T("cert.trust_again"), width=110, height=32,
            fg_color="#F39C12",
            font=ctk.CTkFont(size=12),
            command=lambda: _close(True),
        ).pack(side="right")

        dlg.update()
        dlg.transient(self.root)
        try:
            dlg.grab_set()
        except Exception:
            pass
        dlg.protocol("WM_DELETE_WINDOW", lambda: _close(False))
        dlg.wait_window()
        return result[0]

    # ═══════════════════════════════════════════════════════════════
    # Systray toggle + timed sync pause
    # ═══════════════════════════════════════════════════════════════

    def _clear_pause_state(self) -> None:
        """Drop any pending timed-pause bookkeeping (idempotent).

        Cancels the auto-resume timer and clears the tray countdown.  Called
        when a timed pause starts (to reset any previous one) and whenever
        the user toggles sync by hand — an explicit toggle is authoritative,
        and a stale timer firing later must not resurrect/kill a state the
        user chose since.
        """
        timer = self._pause_timer
        self._pause_timer = None
        self._pause_deadline = None
        if timer is not None:
            timer.cancel()
        if self.systray is not None:
            try:
                self.systray.set_pause_deadline(None)
            except Exception:
                logger.debug("Failed to clear tray pause display", exc_info=True)
        # Drop the persisted twin as well: an explicit toggle/resume must also
        # clear the deadline ON DISK, or a restart resurrects a pause the user
        # already ended (and a web/dashboard toggle that saved config just
        # before reaching this method would otherwise leave the stale deadline
        # behind).  During shutdown it is deliberately KEPT: quitting mid-pause
        # resumes the remaining countdown on the next launch.
        cfg = getattr(self, "cfg", None)
        if cfg is not None and not getattr(self, "_shutting_down", False):
            try:
                if float(getattr(cfg, "timed_pause_until", 0.0) or 0.0):
                    cfg.timed_pause_until = 0.0
                    self._save_cfg_encrypted()
            except Exception:
                logger.debug("Failed to persist cleared pause deadline",
                             exc_info=True)

    def _pause_sync_for_minutes(self, minutes: int) -> None:
        """Pause clipboard sync for *minutes*, then auto-resume.

        Reuses the ordinary toggle path so cfg/tray/manager stay consistent;
        the only extra state is the resume timer plus the tray countdown.
        """
        if self._shutting_down:
            return
        try:
            minutes = max(1, min(int(minutes), 24 * 60))
        except (TypeError, ValueError):
            return
        # Any explicit toggle below must first clear a previous pause's
        # timer/deadline so the two never fight over the final state.
        self._clear_pause_state()
        if self.cfg.sync_enabled:
            self._on_systray_toggle(False)
        else:
            # Already paused manually — just attach the timed resume.
            self._set_systray_syncing(False)
        self._pause_deadline = time.time() + minutes * 60
        # Persist the deadline so a restart / auto-update relaunch inside the
        # pause window re-arms it (_restore_timed_pause) instead of leaving the
        # persisted sync_enabled=False stuck forever — the resume timer is
        # runtime-only state that dies with this process.
        self.cfg.timed_pause_until = self._pause_deadline
        self._save_cfg_encrypted()
        if self.systray is not None:
            try:
                self.systray.set_pause_deadline(self._pause_deadline)
            except Exception:
                logger.debug("Failed to show tray pause countdown", exc_info=True)
        self._pause_timer = threading.Timer(
            minutes * 60.0, self._fire_auto_resume,
        )
        self._pause_timer.daemon = True
        self._pause_timer.start()
        self._notify("notify_sync", T("ui.clipboard_sync"),
                     T("tray.paused_left", minutes=minutes))
        logger.info("Sync paused for %d minute(s)", minutes)

    def _fire_auto_resume(self) -> None:
        """Timer thread body: hop onto the UI thread to resume."""
        try:
            self.root.after(0, self._auto_resume_from_pause)
        except Exception:
            # Root already destroyed (app quit during the pause window).
            logger.debug("Auto-resume scheduling failed (UI closed)",
                         exc_info=True)

    def _auto_resume_from_pause(self) -> None:
        self._pause_timer = None
        self._pause_deadline = None
        if self.systray is not None:
            try:
                self.systray.set_pause_deadline(None)
            except Exception:
                logger.debug("Failed to clear tray pause display", exc_info=True)
        if self._shutting_down or self.cfg.sync_enabled:
            return
        logger.info("Timed sync pause elapsed — resuming")
        self._on_systray_toggle(True)

    def _resume_timed_pause(self) -> None:
        """Manual "Resume Sync Now" from the tray."""
        had_pause = self._pause_deadline is not None
        self._clear_pause_state()
        if self.cfg.sync_enabled:
            return  # nothing to do; state already consistent
        logger.info("Sync resumed via tray (%s)",
                    "timed pause" if had_pause else "manual")
        self._on_systray_toggle(True)

    # ═══════════════════════════════════════════════════════════════
    # Internet (cross-network) sync over public MQTT relays
    # ═══════════════════════════════════════════════════════════════

    def _ensure_relay_secret(self) -> str:
        """This device's relay secret, generated and persisted on first use.

        Never leaves the device except via the TLS-encrypted LAN channel
        (relay_enroll), so the public broker never sees it.
        """
        if not self.cfg.relay_secret:
            from internal.transport.relay import generate_relay_secret
            self.cfg.relay_secret = generate_relay_secret()
            try:
                self._save_cfg_and_peers()
            except Exception:
                logger.debug("Failed persisting new relay secret", exc_info=True)
        return self.cfg.relay_secret

    def _relay_channels(self) -> dict[str, bytes]:
        """{topic: key} for every paired peer whose relay secret we know, plus
        every ACTIVE netpair channel (persisted pairs + generated-but-not-yet-
        confirmed pairing codes, so the partner's hello can arrive).

        Derivation is symmetric, so both ends compute identical topics/keys
        from their two secrets without ever transmitting them again.
        """
        from internal.transport.relay import (
            derive_key, derive_topic, netpair_key, netpair_topic,
        )
        my_secret = self._ensure_relay_secret()
        channels: dict[str, bytes] = {}
        for pid, peer_secret in list(self.cfg.peer_relay_secrets.items()):
            peer = self.cfg.peers.get(pid)
            if peer is None or not peer.paired or not peer_secret:
                continue
            channels[derive_topic(my_secret, peer_secret)] = derive_key(
                my_secret, peer_secret)
        for pid, secret in self._netpair_secrets_all().items():
            if not secret:
                continue
            if pid == self.cfg.device_id or pid == f"pending:{self.cfg.device_id}":
                continue  # never listen on a channel derived from our own code
            channels[netpair_topic(secret)] = netpair_key(
                secret, self._netpair_pw())
        return channels

    def _netpair_pw(self) -> str:
        """Configured pairing passphrase ("" when unset).

        Read fresh from cfg on every call so a settings change applies to the
        channel keys immediately, without a restart.
        """
        return getattr(self.cfg, "netpair_password", "") or ""

    # ------------------------------------------- internet pairing code state

    def _netpair_secrets_all(self) -> dict[str, str]:
        """Every netpair secret this device should listen on: persisted pairs
        plus codes it generated that are still awaiting confirmation."""
        out: dict[str, str] = {}
        for k, v in (getattr(self.cfg, "netpair_secrets", {}) or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                out[k] = v
        for code, v in getattr(self, "_netpair_pending", {}).items():
            out.setdefault(f"pending:{code}", v)
        return out

    def _netpair_secret_for_topic(self, topic: str) -> str | None:
        """Map a received relay topic back to the netpair secret behind it."""
        if not topic:
            return None
        from internal.transport.relay import netpair_topic
        for secret in self._netpair_secrets_all().values():
            if secret and netpair_topic(secret) == topic:
                return secret
        return None

    def _get_current_relay_broker(self) -> str:
        """Endpoint URL of the broker the relay is currently connected to
        ("" when offline/disabled).  Surfaced to the web UI so a user can tell
        at a glance whether two paired devices are on the same broker."""
        if not getattr(self.cfg, "internet_sync_enabled", False):
            return ""
        relay = getattr(self, "_relay", None)
        if relay is None:
            return ""
        try:
            return relay.current_broker
        except Exception:
            return ""

    def _on_relay_state(self, state: str) -> None:
        logger.info("Internet sync relay state: %s", state)
        try:
            if getattr(self, "web_server", None) is not None:
                payload = {"state": state}
                try:
                    relay = getattr(self, "_relay", None)
                    if relay is not None:
                        broker = relay.current_broker
                        if broker:
                            payload["broker"] = broker
                except Exception:
                    logger.debug("relay current_broker read failed", exc_info=True)
                # WebServer itself has no broadcast — the WebSocketManager does
                # (the "relay_state WS broadcast failed" AttributeError in the
                # logs was this call resolving to a missing method).
                self.web_server.ws_manager.broadcast("relay_state", payload)
        except Exception:
            logger.debug("relay_state WS broadcast failed", exc_info=True)
        # Round 17: coming online is a retransmission trigger — flush the
        # offline queue for every peer that queued frames while we were down.
        if state == "online":
            try:
                self._delivery_on_relay_online()
            except Exception:
                logger.debug("delivery retry on relay-online failed",
                             exc_info=True)

    def _get_relay_state(self) -> str:
        """Current internet-sync state for late-joining web clients.

        WS ``relay_state`` events only reach clients attached at transition
        time; GET /api/settings polls this so a freshly loaded dashboard shows
        the truth instead of assuming "off".
        """
        if not getattr(self.cfg, "internet_sync_enabled", False):
            return "off"
        relay = getattr(self, "_relay", None)
        if relay is None:
            # Enabled but the transport is not up yet (startup ordering) —
            # report connecting rather than a misleading "off".
            return "connecting"
        try:
            return relay.state
        except Exception:
            return "connecting"

    def _on_relay_frame(self, frame_bytes: bytes,
                        topic: str | None = None, *, now: float | None = None) -> None:
        """A clipboard frame arrived through the public relay.

        It is a standard ClipSync frame — decode and feed the very same
        message router used for LAN frames.  Replies (acks etc.) ride the
        LAN connection when the peer happens to be connected; the relay is
        one-way clipboard content only in this MVP.  Internet pairing-code
        ``netpair_hello`` frames are routed to the netpair handshake instead
        (they never enter the clipboard/file/chat routers).
        """
        try:
            from internal.protocol.codec import decode_message
            sync_msg = decode_message(frame_bytes)
        except Exception:
            logger.debug("Relay frame failed to decode", exc_info=True)
            return
        if sync_msg is None:
            return
        # Never ingest our own frames: a self-entered netpair code or a
        # self-published relay-enroll topic would otherwise echo straight
        # back into the clipboard/file/chat routers.
        source = getattr(sync_msg, "source_device", "") or None
        if source and source == self.cfg.device_id:
            logger.debug("Dropping self-originated relay frame")
            return
        # Round 15: any frame from a confirmed internet-paired peer counts as
        # "seen" (both the netpair_hello handshake and mirrored clipboard
        # frames), so the device page can show online / last-synced state.
        if source and source in (getattr(self.cfg, "netpair_secrets", {}) or {}):
            getattr(self, "_netpair_last_seen", {})[source] = (
                time.time() if now is None else now)
        # Round 17: any frame from a peer is an "active" signal — retry that
        # peer's offline queue so queued clipboard frames flush as soon as the
        # peer proves reachable.
        try:
            self._delivery_peer_active(source)
        except Exception:
            logger.debug("delivery retry on relay frame failed", exc_info=True)
        if getattr(sync_msg, "msg_type", "") == "netpair_hello":
            try:
                self._handle_netpair_hello(
                    getattr(sync_msg, "_raw_payload", {}),
                    source,
                    topic,
                )
            except Exception:
                logger.debug("netpair hello handling failed", exc_info=True)
            return
        try:
            self._on_peer_message(sync_msg, source, via_relay=True)
        except Exception:
            logger.debug("Relay frame handling failed", exc_info=True)

    def _start_internet_sync(self) -> None:
        if self._relay is not None:
            return
        from internal.transport.relay import RelayTransport, build_paho_client
        transport = RelayTransport(
            brokers=list(self.cfg.relay_brokers),
            get_channels=self._relay_channels,
            on_frame=self._on_relay_frame,
            on_state=self._on_relay_state,
            client_factory=build_paho_client,
        )
        self._relay = transport
        transport.start()
        # Round 17: load any previously-persisted offline queue and start the
        # timeout-scan / queue-retry thread (also triggered by relay-online).
        try:
            self._delivery_start()
        except Exception:
            logger.debug("delivery thread start failed", exc_info=True)
        # Enroll every paired peer so both sides learn each other's relay
        # secret over the encrypted LAN channel (offline peers pick it up on
        # their next enroll reply exchange once they connect).
        for pid in list(self.cfg.peers):
            self._send_relay_enroll(pid)

    def _stop_internet_sync(self) -> None:
        # Round 17: persist any still-queued rows before tearing down.
        try:
            self._delivery_stop()
        except Exception:
            logger.debug("delivery stop failed", exc_info=True)
        transport, self._relay = self._relay, None
        if transport is not None:
            transport.stop()

    def _send_relay_enroll(self, peer_id: str) -> None:
        """Offer our relay secret to a paired peer over its LAN connection.

        Idempotent and cheap: the receiver stores it, then replies with its
        own secret, so a single successful exchange teaches both sides.
        """
        if not self.cfg.internet_sync_enabled:
            return
        peer = self.cfg.peers.get(peer_id)
        if peer is None or not peer.paired:
            return
        try:
            self.transport_mgr.send_to_peer(
                peer_id,
                encode_frame({"msg_type": "relay_enroll",
                              "relay_secret": self._ensure_relay_secret()}),
            )
        except Exception:
            logger.debug("relay enroll to %s failed", peer_id[:12], exc_info=True)

    def _handle_relay_enroll(self, payload: dict, peer_id: str | None) -> None:
        """Store a paired peer's relay secret (sent over its TLS channel)."""
        if not peer_id or not isinstance(payload, dict):
            return
        secret = payload.get("relay_secret")
        if (not isinstance(secret, str) or len(secret) != 64
                or any(c not in "0123456789abcdef" for c in secret)):
            logger.debug("Ignoring invalid relay_enroll from %s",
                         (peer_id or "")[:12])
            return
        known = self.cfg.peer_relay_secrets.get(peer_id)
        if known != secret:
            self.cfg.peer_relay_secrets[peer_id] = secret
            try:
                self._save_cfg_and_peers()
            except Exception:
                logger.debug("Failed persisting peer relay secret", exc_info=True)
        if self._relay is not None:
            self._relay.refresh_channels()
        # Always answer so the other side learns OUR secret even if it knew
        # an older one — this is what bootstraps both directions.
        self._send_relay_enroll(peer_id)

    def _relay_publish_frame(self, frame_bytes: bytes) -> None:
        """Mirror an outbound clipboard frame to all enrolled paired peers.

        A device that is BOTH a LAN-paired relay-enroll peer (its secret
        learned via ``relay_enroll``) AND an internet pairing-code peer
        (``netpair_secrets``) can be reached over two derived topics.  We
        publish to exactly one of them — the netpair channel wins when both
        exist — so the broker and receiver aren't handed the same frame twice
        (the receiver's content dedup would collapse it, but the duplicate
        transit is pure waste).  Both ends of a pairing derive the same
        channel set, so the peer's subscription always covers whichever topic
        we publish on.

        Round 17: clipboard frames also enter the delivery ledger — a successful
        publish is tracked as sent (awaits a relay_ack), a failed one (relay
        offline / no live channel) is persisted to the offline queue for later
        retransmission.
        """
        transport = self._relay
        if transport is None or not self.cfg.internet_sync_enabled:
            return
        # Delivery metadata is only relevant for clipboard frames (chat and
        # file frames keep their old best-effort mirror with no ledger).
        delivery = None
        try:
            from internal.protocol.codec import decode_message
            _d = decode_message(frame_bytes)
            if _d is not None and getattr(_d, "msg_type", "clipboard") == "clipboard":
                delivery = {
                    "msg_id": getattr(_d, "msg_id", "") or "",
                    "content_hash": (_d.content.hash_key()
                                     if getattr(_d, "content", None) else ""),
                    "preview": self._delivery_preview(_d),
                }
        except Exception:
            logger.debug("delivery metadata decode failed", exc_info=True)
        netpair_secrets = getattr(self.cfg, "netpair_secrets", {}) or {}
        from internal.transport.relay import derive_key, derive_topic
        my_secret = self._ensure_relay_secret()
        for pid, peer_secret in list(self.cfg.peer_relay_secrets.items()):
            if pid in netpair_secrets:
                continue  # reached via the netpair channel below — no double send
            peer = self.cfg.peers.get(pid)
            if peer is None or not peer.paired or not peer_secret:
                continue
            topic = derive_topic(my_secret, peer_secret)
            key = derive_key(my_secret, peer_secret)
            try:
                ok = transport.publish(frame_bytes, topic, key)
            except Exception:
                logger.debug("relay publish to %s failed", pid[:12],
                             exc_info=True)
                ok = False
            if delivery:
                if ok:
                    self._delivery_on_sent(
                        pid, delivery["msg_id"], delivery["content_hash"],
                        delivery["preview"])
                else:
                    self._delivery_enqueue(
                        pid, delivery["msg_id"], delivery["content_hash"],
                        delivery["preview"], frame_bytes)
        # Netpair channels: mirror to every CONFIRMED pairing-code peer too.
        # (Generated-but-unconfirmed codes stay subscribed but are not used for
        # clipboard mirroring — nothing has been confirmed yet.)
        from internal.transport.relay import netpair_key, netpair_topic
        for pid, peer_secret in (getattr(self.cfg, "netpair_secrets", {}) or {}).items():
            if not isinstance(peer_secret, str) or not peer_secret:
                continue
            if pid == self.cfg.device_id:
                continue  # a stray self-entry must never mirror to ourselves
            try:
                ok = transport.publish(frame_bytes,
                                       netpair_topic(peer_secret),
                                       netpair_key(peer_secret, self._netpair_pw()))
            except Exception:
                logger.debug("netpair publish failed", exc_info=True)
                ok = False
            if delivery:
                if ok:
                    self._delivery_on_sent(
                        pid, delivery["msg_id"], delivery["content_hash"],
                        delivery["preview"])
                else:
                    self._delivery_enqueue(
                        pid, delivery["msg_id"], delivery["content_hash"],
                        delivery["preview"], frame_bytes)

    def _relay_publish_to_peer(self, frame_bytes: bytes, peer_id: str) -> bool:
        """Mirror one chat frame to a single internet-reachable peer.

        Used by ``_chat_send_fn`` so chat frames reach devices across networks.
        The peer is keyed by its real device id, which is the dict key in
        ``cfg.netpair_secrets`` (pairing-code pairs) or ``cfg.peer_relay_secrets``
        (LAN-paired peers that exchanged relay secrets over the encrypted LAN
        channel); a netpair entry wins when a peer is both (one publish, not two).

        Returns True only when the frame was actually handed to the relay
        (``_chat_send_fn`` uses it so an internet-only peer still counts as
        delivered); anything else — relay absent / ``internet_sync_enabled`` off /
        unknown or unpaired peer / any failure — is a silent False.

        The frame must already carry ``source_device`` (chat's ``_send_frame``
        encodes it) so the receiving side can attribute it back to this peer —
        the relay has no connection object to infer the sender from.

        Chat file BYTES (``file_chunk`` frames) DO cross the relay, so an
        internet-only paired device can receive files up to the relay cap
        (ChatManager picks a relay-safe chunk size + size cap for those peers).
        No double delivery: ``_chat_send_fn`` is LAN-first, so a chunk only
        reaches this relay path when the LAN send already failed — a
        dual-connected peer receives it exactly once, over whichever path
        delivered it.  Chunks ride at QoS 1 so a best-effort public broker
        redelivers a dropped packet (receiver-side ``_seen`` + per-index chunk
        dedup absorb at-least-once duplicates).
        """
        transport = self._relay
        if transport is None or not self.cfg.internet_sync_enabled:
            return False
        if not peer_id:
            return False
        # Best-effort type sniff: only picks QoS 1 for binary file chunks.
        # A frame that fails to decode still publishes at QoS 0, matching the
        # pre-file-transfer behavior (decode was never a gate here).
        decoded = None
        is_chunk = False
        try:
            from internal.protocol.codec import decode_message
            decoded = decode_message(frame_bytes)
            is_chunk = getattr(decoded, "msg_type", "") == "file_chunk"
        except Exception:
            logger.debug("relay publish to peer: frame decode failed",
                         exc_info=True)
        from internal.transport.relay import (
            derive_key, derive_topic, netpair_key, netpair_topic,
        )
        secret = (getattr(self.cfg, "netpair_secrets", {}) or {}).get(peer_id)
        if isinstance(secret, str) and secret:
            topic, key = netpair_topic(secret), netpair_key(secret, self._netpair_pw())
        else:
            peer_secret = (
                getattr(self.cfg, "peer_relay_secrets", {}) or {}).get(peer_id)
            peer = self.cfg.peers.get(peer_id)
            if not peer_secret or peer is None \
                    or not getattr(peer, "paired", False):
                return False
            my_secret = self._ensure_relay_secret()
            topic, key = (derive_topic(my_secret, peer_secret),
                          derive_key(my_secret, peer_secret))
        try:
            ok = transport.publish(
                frame_bytes, topic, key, qos=1 if is_chunk else 0) is True
        except Exception:
            logger.debug("relay publish to %s failed", str(peer_id)[:12],
                         exc_info=True)
            return False
        # Round 17: relayed CHAT frames get a delivery receipt too (ledger
        # sent → ack → delivered / failed), same relay_ack mechanism as
        # clipboard.  No offline queue for chat — that stays clipboard-only.
        if ok and decoded is not None \
                and getattr(decoded, "msg_type", "") in CHAT_MSG_TYPES \
                and hasattr(self, "_delivery_ledger"):
            try:
                payload = getattr(decoded, "_raw_payload", {}) or {}
                sid = payload.get("session_id", "")
                self._delivery_on_sent(
                    peer_id,
                    getattr(decoded, "msg_id", "") or "",
                    "",
                    self._delivery_chat_preview(getattr(decoded, "msg_type", ""),
                                                payload),
                    kind=getattr(decoded, "msg_type", "chat"),
                    session_id=str(sid) if isinstance(sid, str) else "",
                )
            except Exception:
                logger.debug("chat delivery ledger failed", exc_info=True)
        return ok

    # -------------------------------------------- internet delivery (Round 17)
    #
    # "已送达" receipt + offline retransmission.  Clipboard frames mirrored to
    # internet peers get a ledger row (status sent → ack → delivered, or →
    # failed on ack timeout).  If the relay is offline / the peer has no live
    # channel at send time the frame is persisted to <config>/relay_pending.json
    # and republished on reconnect, on a peer-active signal, and on a timer.

    def _delivery_queue_path(self) -> "Path":
        """Path of the persisted offline-queue file (atomic-write target)."""
        from internal.config.config import _config_dir
        return _config_dir() / RELAY_PENDING_FILE

    def _delivery_ws(self, peer_id: str, msg_id: str, status: str,
                     content_hash: str = "", kind: str = "clipboard",
                     session_id: str = "") -> None:
        """Broadcast an ``internet_delivery`` WS event to every web client.

        ``kind`` discriminates clipboard ("clipboard") from relayed chat frames
        (the chat ``msg_type``, e.g. "chat_text"); ``session_id`` lets the chat
        panel locate the session an event belongs to.  Both are empty/no-op for
        the other payload type.
        """
        try:
            if getattr(self, "web_server", None) is not None:
                self.web_server.ws_manager.broadcast("internet_delivery", {
                    "peer_id": peer_id,
                    "msg_id": msg_id,
                    "status": status,
                    "content_hash": content_hash or "",
                    "kind": kind or "clipboard",
                    "session_id": session_id or "",
                })
        except Exception:
            logger.debug("internet_delivery WS broadcast failed", exc_info=True)

    @staticmethod
    def _delivery_preview(msg) -> str:
        """First 40 chars of a decoded clipboard message's text (for REST)."""
        try:
            types = getattr(getattr(msg, "content", None), "types", {}) or {}
            for ct in (_CT.TEXT, _CT.HTML, _CT.RTF):
                raw = types.get(ct)
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    return text[:40]
        except Exception:
            pass
        return ""

    @staticmethod
    def _delivery_chat_preview(msg_type: str, payload: dict) -> str:
        """Short preview of a chat frame for the delivery ledger / REST.

        chat_text shows the message text; file offers show the file name; the
        rest fall back to the msg_type so a row is still identifiable.
        """
        try:
            if msg_type == "chat_text":
                text = str(payload.get("text", "") or "").strip()
                if text:
                    return text[:40]
            elif msg_type == "chat_file_offer":
                name = str(payload.get("file_name", "") or "").strip()
                if name:
                    return name[:40]
        except Exception:
            pass
        return msg_type or "chat"

    def _delivery_bounded(self, peer_id: str) -> None:
        """Keep per-peer ledger/queue sizes within sane caps.

        The ledger only feeds the REST "recent 20" view; the queue cap
        prevents an offline spell from growing relay_pending.json without
        bound.  Evicted queue rows are surfaced as failed (WS + ledger) so the
        UI never silently loses a tracked send.
        """
        led = self._delivery_ledger.get(peer_id)
        if led:
            while len(led) > DELIVERY_LEDGER_MAX:
                led.popitem(last=False)
        q = self._delivery_queue.get(peer_id)
        if q and len(q) > DELIVERY_QUEUE_MAX:
            while len(q) > DELIVERY_QUEUE_MAX:
                _mid, oldest = q.popitem(last=False)
                if led is not None and _mid in led:
                    led[_mid]["status"] = "failed"
                    led[_mid]["deadline"] = None
                    self._delivery_ws(peer_id, _mid, "failed",
                                      oldest.get("content_hash", ""),
                                      oldest.get("kind", "clipboard"),
                                      oldest.get("session_id", ""))

    def _delivery_content_delivered(self, peer_id: str, content_hash: str,
                                    skip_msg_id: str | None = None) -> bool:
        """True when the same content_hash already reached ``peer_id``.

        Content-level ack fallback: a broker redelivery / re-copy of content
        that was already confirmed delivered must not be re-reported as failed
        merely because its own ack never came back.
        """
        if not content_hash:
            return False
        for e in (self._delivery_ledger.get(peer_id) or {}).values():
            if e.get("msg_id") == skip_msg_id:
                continue
            if e.get("content_hash") == content_hash \
                    and e.get("status") == "delivered":
                return True
        return False

    def _delivery_on_sent(self, peer_id: str, msg_id: str, content_hash: str,
                          preview: str, kind: str = "clipboard",
                          session_id: str = "") -> None:
        """Record a freshly-published send (clipboard or relayed chat) in the ledger."""
        if not peer_id or not msg_id:
            return
        now = time.time()
        with self._delivery_lock:
            led = self._delivery_ledger.setdefault(peer_id, OrderedDict())
            led[msg_id] = {
                "msg_id": msg_id,
                "content_hash": content_hash,
                "ts": now,
                "status": "sent",
                "deadline": now + ACK_WINDOW,
                "preview": preview,
                "kind": kind or "clipboard",
                "session_id": session_id or "",
            }
            if content_hash and self._delivery_content_delivered(
                    peer_id, content_hash, skip_msg_id=msg_id):
                led[msg_id]["status"] = "delivered"
                led[msg_id]["deadline"] = None
                status = "delivered"
            else:
                status = "sent"
            q = self._delivery_queue.get(peer_id)
            if q and msg_id in q:
                del q[msg_id]          # defensive: never double-track a msg
                self._delivery_persist_queue()
            self._delivery_bounded(peer_id)
        self._delivery_ws(peer_id, msg_id, status, content_hash, kind, session_id)

    def _delivery_enqueue(self, peer_id: str, msg_id: str, content_hash: str,
                          preview: str, frame_bytes: bytes,
                          kind: str = "clipboard", session_id: str = "") -> None:
        """Persist a clipboard frame that could not be published right now."""
        if not peer_id or not msg_id:
            return
        import base64 as _b
        with self._delivery_lock:
            q = self._delivery_queue.setdefault(peer_id, OrderedDict())
            if msg_id in q:
                return  # already queued
            q[msg_id] = {
                "msg_id": msg_id,
                "content_hash": content_hash,
                "ts": time.time(),
                "retries": 0,
                "preview": preview,
                "kind": kind or "clipboard",
                "session_id": session_id or "",
                "frame_b64": _b.b64encode(frame_bytes).decode("ascii"),
            }
            led = self._delivery_ledger.setdefault(peer_id, OrderedDict())
            led[msg_id] = {
                "msg_id": msg_id,
                "content_hash": content_hash,
                "ts": q[msg_id]["ts"],
                "status": "queued",
                "deadline": None,
                "preview": preview,
                "kind": kind or "clipboard",
                "session_id": session_id or "",
            }
            self._delivery_bounded(peer_id)
            self._delivery_persist_queue()
        self._delivery_ws(peer_id, msg_id, "queued", content_hash, kind, session_id)

    def _delivery_persist_queue(self) -> None:
        """Atomically write the offline queue to disk (crash-safe)."""
        try:
            import json as _json
            path = self._delivery_queue_path()
            # Nothing to persist and no stale file to clear — skip the write so
            # a fresh install doesn't create an empty relay_pending.json.
            if not self._delivery_queue and not path.exists():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                _json.dumps({"version": 1, "peers": self._delivery_queue},
                            ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception:
            logger.debug("Failed persisting relay pending queue", exc_info=True)

    def _delivery_load_queue(self) -> None:
        """Load a previously persisted offline queue into memory at startup."""
        try:
            import json as _json
            path = self._delivery_queue_path()
            if not path.exists():
                return
            data = _json.loads(path.read_text(encoding="utf-8"))
            peers = data.get("peers", {}) if isinstance(data, dict) else {}
            if not isinstance(peers, dict):
                return
            with self._delivery_lock:
                for peer_id, msgs in peers.items():
                    if not isinstance(peer_id, str) or not isinstance(msgs, dict):
                        continue
                    od: OrderedDict = OrderedDict()
                    for msg_id, e in msgs.items():
                        if isinstance(msg_id, str) and isinstance(e, dict):
                            od[msg_id] = e
                    if not od:
                        continue
                    self._delivery_queue[peer_id] = od
                    led = self._delivery_ledger.setdefault(
                        peer_id, OrderedDict())
                    for msg_id, e in od.items():
                        led[msg_id] = {
                            "msg_id": msg_id,
                            "content_hash": e.get("content_hash", ""),
                            "ts": float(e.get("ts", 0.0) or 0.0),
                            "status": "queued",
                            "deadline": None,
                            "preview": e.get("preview", ""),
                            "kind": e.get("kind", "clipboard"),
                            "session_id": e.get("session_id", ""),
                        }
        except Exception:
            logger.debug("Failed loading relay pending queue", exc_info=True)

    def _delivery_mark_timeout(self, peer_id: str, msg_id: str) -> None:
        """Ack window expired: fail the send (unless content-level ack)."""
        with self._delivery_lock:
            led = self._delivery_ledger.get(peer_id)
            if not led or msg_id not in led:
                return
            entry = led[msg_id]
            if entry.get("status") != "sent":
                return
            content_hash = entry.get("content_hash", "")
            if content_hash and self._delivery_content_delivered(
                    peer_id, content_hash, skip_msg_id=msg_id):
                entry["status"] = "delivered"
                entry["deadline"] = None
                status = "delivered"
            else:
                entry["status"] = "failed"
                entry["deadline"] = None
                status = "failed"
            kind = entry.get("kind", "clipboard")
            session_id = entry.get("session_id", "")
        self._delivery_ws(peer_id, msg_id, status, content_hash, kind, session_id)

    def _delivery_scan_expired(self) -> None:
        """Background sweep: fail every sent row whose ack window elapsed."""
        now = time.time()
        with self._delivery_lock:
            expired = []
            for peer_id, led in list(self._delivery_ledger.items()):
                for msg_id, e in list(led.items()):
                    if e.get("status") == "sent" and e.get("deadline") \
                            and e["deadline"] <= now:
                        expired.append((peer_id, msg_id))
        for peer_id, msg_id in expired:
            try:
                self._delivery_mark_timeout(peer_id, msg_id)
            except Exception:
                logger.debug("delivery timeout mark failed", exc_info=True)

    def _delivery_retry_peer(self, peer_id: str) -> None:
        """Republish every queued frame for one peer.

        Successful republish: drop the queue row, reset the ledger to sent with
        a fresh ack window, WS queued→sent.  Failed republish: bump retries;
        past ``MAX_QUEUE_RETRIES`` mark the row failed and drop it.
        """
        if not peer_id or not hasattr(self, "_delivery_lock"):
            return
        with self._delivery_lock:
            q = self._delivery_queue.get(peer_id)
            if not q:
                return
            items = [(mid, dict(e)) for mid, e in list(q.items())]
        changed = False
        for msg_id, entry in items:
            try:
                import base64 as _b
                frame_bytes = _b.b64decode(entry.get("frame_b64", "") or "")
                ok = self._relay_publish_to_peer(frame_bytes, peer_id)
            except Exception:
                logger.debug("delivery republish to %s failed",
                             str(peer_id)[:12], exc_info=True)
                ok = False
            with self._delivery_lock:
                q2 = self._delivery_queue.get(peer_id)
                if not q2 or msg_id not in q2:
                    continue  # changed concurrently — leave it alone
                content_hash = entry.get("content_hash", "")
                if ok:
                    del q2[msg_id]
                    led = self._delivery_ledger.setdefault(
                        peer_id, OrderedDict())
                    now = time.time()
                    led[msg_id] = {
                        "msg_id": msg_id,
                        "content_hash": content_hash,
                        "ts": now,
                        "status": "sent",
                        "deadline": now + ACK_WINDOW,
                        "preview": entry.get("preview", ""),
                        "kind": entry.get("kind", "clipboard"),
                        "session_id": entry.get("session_id", ""),
                    }
                    changed = True
                    self._delivery_ws(peer_id, msg_id, "sent", content_hash,
                                      entry.get("kind", "clipboard"),
                                      entry.get("session_id", ""))
                else:
                    q2[msg_id]["retries"] = entry.get("retries", 0) + 1
                    if q2[msg_id]["retries"] >= MAX_QUEUE_RETRIES:
                        del q2[msg_id]
                        led = self._delivery_ledger.setdefault(
                            peer_id, OrderedDict())
                        if msg_id in led:
                            led[msg_id]["status"] = "failed"
                            led[msg_id]["deadline"] = None
                        changed = True
                        self._delivery_ws(peer_id, msg_id, "failed",
                                          content_hash,
                                          entry.get("kind", "clipboard"),
                                          entry.get("session_id", ""))
                if not q2:
                    self._delivery_queue.pop(peer_id, None)
        if changed:
            with self._delivery_lock:
                self._delivery_persist_queue()

    def _delivery_retry_queue(self) -> None:
        """Retry the entire offline queue (relay-online + 60s timer)."""
        if not hasattr(self, "_delivery_lock"):
            return
        with self._delivery_lock:
            peers = list(self._delivery_queue.keys())
        for pid in peers:
            try:
                self._delivery_retry_peer(pid)
            except Exception:
                logger.debug("delivery retry for %s failed",
                             str(pid)[:12], exc_info=True)

    def _delivery_peer_active(self, peer_id: str) -> None:
        """Retry a peer's queue when any frame from it arrives (active signal)."""
        if not peer_id:
            return
        try:
            self._delivery_retry_peer(peer_id)
        except Exception:
            logger.debug("delivery retry on peer-active failed", exc_info=True)

    def _delivery_on_relay_online(self) -> None:
        """Relay came online — flush every peer's offline queue."""
        self._delivery_retry_queue()

    def _delivery_start(self) -> None:
        """Load the persisted queue and start the background scan/retry thread."""
        if getattr(self, "_delivery_thread", None) is not None:
            return
        if not hasattr(self, "_delivery_stop_evt"):
            return
        self._delivery_stop_evt.clear()
        self._delivery_load_queue()
        self._delivery_thread = threading.Thread(
            target=self._delivery_run, name="internet-delivery", daemon=True)
        self._delivery_thread.start()

    def _delivery_stop(self) -> None:
        """Persist any queued rows and stop the background thread."""
        if not hasattr(self, "_delivery_stop_evt"):
            return
        self._delivery_stop_evt.set()
        thread, self._delivery_thread = self._delivery_thread, None
        if thread is not None:
            try:
                thread.join(timeout=2.0)
            except Exception:
                logger.debug("delivery thread join failed", exc_info=True)
        try:
            self._delivery_persist_queue()
        except Exception:
            logger.debug("delivery final persist failed", exc_info=True)

    def _delivery_clear_peer(self, peer_id: str) -> None:
        """Drop a peer's delivery ledger + offline queue and re-persist.

        Called when a peer is unpaired/forgotten: an unpaired peer must stop
        burning relay retries, and its base64 payloads must not linger on disk
        in relay_pending.json.  Defensive on missing delivery state so test
        stubs / early teardown are no-ops.
        """
        if not peer_id:
            return
        lock = getattr(self, "_delivery_lock", None)
        if lock is None:
            return
        with lock:
            queue = getattr(self, "_delivery_queue", None)
            if queue is not None:
                queue.pop(peer_id, None)
            ledger = getattr(self, "_delivery_ledger", None)
            if ledger is not None:
                ledger.pop(peer_id, None)
        persist = getattr(self, "_delivery_persist_queue", None)
        if persist is not None:
            persist()

    def _delivery_run(self) -> None:
        """Lightweight loop: 2s timeout scan, 60s queue retry, 1s granularity."""
        last_scan = time.monotonic()
        last_retry = time.monotonic()
        while not getattr(self, "_delivery_stop_evt",
                          threading.Event()).wait(1.0):
            now = time.monotonic()
            if now - last_scan >= DELIVERY_SCAN_INTERVAL:
                try:
                    self._delivery_scan_expired()
                except Exception:
                    logger.debug("delivery scan failed", exc_info=True)
                last_scan = now
            if now - last_retry >= QUEUE_RETRY_INTERVAL:
                try:
                    self._delivery_retry_queue()
                except Exception:
                    logger.debug("delivery queue retry failed", exc_info=True)
                last_retry = now

    def _handle_relay_ack(self, payload: dict, peer_id: str | None) -> None:
        """Route an incoming ``relay_ack`` receipt to its ledger row."""
        if not peer_id or not isinstance(payload, dict):
            return
        msg_id = payload.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            return
        content_hash = ""
        kind = "clipboard"
        session_id = ""
        with self._delivery_lock:
            led = self._delivery_ledger.get(peer_id)
            if not led or msg_id not in led:
                return  # unknown msg_id — ignore
            entry = led[msg_id]
            if entry.get("status") == "delivered":
                return
            content_hash = entry.get("content_hash", "")
            kind = entry.get("kind", "clipboard")
            session_id = entry.get("session_id", "")
            entry["status"] = "delivered"
            entry["deadline"] = None
            q = self._delivery_queue.get(peer_id)
            if q and msg_id in q:
                del q[msg_id]
                self._delivery_persist_queue()
        self._delivery_ws(peer_id, msg_id, "delivered", content_hash,
                          kind, session_id)

    def _maybe_send_relay_ack(self, msg, peer_id: str | None) -> None:
        """Ack a relayed clipboard / chat frame that the receiver accepted.

        Published on the same encrypted relay channel (netpair channel wins
        over LAN-relay enrollment), keyed only by the source frame's msg_id —
        the sender's ledger resolves it back to the exact message.
        """
        if not peer_id:
            return
        mt = getattr(msg, "msg_type", "clipboard")
        if mt != "clipboard" and mt not in CHAT_MSG_TYPES:
            return
        try:
            if not self._peer_is_internet_reachable(peer_id):
                return
            ack_frame = encode_frame(
                {"msg_type": "relay_ack",
                 "msg_id": getattr(msg, "msg_id", ""),
                 "ts": time.time()},
                source_device=self.cfg.device_id,
            )
            self._relay_publish_to_peer(ack_frame, peer_id)
        except Exception:
            logger.debug("relay ack send failed", exc_info=True)

    def _delivery_render_sends(self, peer_id: str, led: dict,
                               queue: dict, now: float) -> list[dict]:
        """REST row list for one peer's ledger, newest first, capped at 20."""
        out: list[dict] = []
        for msg_id, e in led.items():
            if e.get("status") == "queued":
                status = "queued"
                ts = e.get("ts", now)
            else:
                status = e.get("status", "sent")
                ts = e.get("ts", now)
            out.append({
                "msg_id": msg_id,
                "ts": ts,
                "status": status,
                "preview": e.get("preview", ""),
                "content_hash": e.get("content_hash", ""),
                "kind": e.get("kind", "clipboard"),
                "session_id": e.get("session_id", ""),
            })
        out.sort(key=lambda s: float(s.get("ts", 0.0) or 0.0), reverse=True)
        return out[:20]

    def _delivery_status(self, peer_id: str = "") -> dict:
        """REST: ``{pending, sends:[{msg_id, ts, status, preview, content_hash}]}``.

        With ``peer_id`` the response is scoped to that peer; without it the
        rows are merged across all peers (still capped at the 20 most recent).
        """
        now = time.time()
        with self._delivery_lock:
            if peer_id:
                led = self._delivery_ledger.get(peer_id, {})
                queue = self._delivery_queue.get(peer_id, {})
                return {
                    "pending": len(queue),
                    "sends": self._delivery_render_sends(peer_id, led, queue, now),
                }
            sends: list[dict] = []
            total_pending = 0
            for pid, led in self._delivery_ledger.items():
                queue = self._delivery_queue.get(pid, {})
                total_pending += len(queue)
                sends.extend(self._delivery_render_sends(pid, led, queue, now))
            sends.sort(key=lambda s: float(s.get("ts", 0.0) or 0.0),
                       reverse=True)
            return {"pending": total_pending, "sends": sends[:20]}

    def _delivery_counts(self) -> dict:
        """Per-peer queued counts for device-page badges: ``{peers:{id:N}}``."""
        with self._delivery_lock:
            return {"peers": {pid: len(q) for pid, q in self._delivery_queue.items()}}

    # ---------------------------------------------------- internet pairing

    def _netpair_generate(self) -> tuple[dict, int]:
        """Generate a fresh internet pairing code for THIS device (REST)."""
        from internal.transport.relay import (
            generate_netpair_code, generate_netpair_secret,
        )
        if not self.cfg.internet_sync_enabled:
            return {"ok": False, "error": "internet sync is off"}, 400
        secret = generate_netpair_secret()
        code = generate_netpair_code(self.cfg.device_id, secret)
        self._netpair_pending[code] = secret
        # Subscribe to the code's topic so the partner's hello is received
        # (RelayTransport re-reads get_channels on refresh).
        if self._relay is not None:
            try:
                self._relay.refresh_channels()
            except Exception:
                logger.debug("netpair generate refresh failed", exc_info=True)
        return {"ok": True, "code": code}, 200

    def _netpair_enter(self, code: str) -> tuple[dict, int]:
        """Enter a pairing code from another device and send our hello (REST)."""
        from internal.transport.relay import decode_netpair_code
        decoded = decode_netpair_code(code)
        if decoded is None:
            return {"ok": False, "error": "invalid pairing code"}, 400
        peer_id, secret = decoded
        if not self.cfg.internet_sync_enabled:
            return {"ok": False, "error": "internet sync is off"}, 400
        # Entering a code we generated ourselves is a no-op: the code's
        # device tag is our own, so this would only pair us with ourselves.
        from internal.transport.relay import netpair_device_tag
        if peer_id == netpair_device_tag(self.cfg.device_id):
            return {"ok": False,
                    "error": "cannot pair with this device"}, 400
        self.cfg.netpair_secrets[peer_id] = secret
        try:
            self._save_cfg_and_peers()
        except Exception:
            logger.debug("Failed persisting netpair secret", exc_info=True)
        if self._relay is not None:
            try:
                self._relay.refresh_channels()
            except Exception:
                logger.debug("netpair enter refresh failed", exc_info=True)
        self._send_netpair_hello(peer_id, secret)
        return {"ok": True, "peer_id": peer_id}, 200

    def _netpair_status(self, now: float | None = None) -> tuple[dict, int]:
        """Current netpair state for late-joining web clients (REST).

        Round 15 extension — per peer: ``name`` = the peer's device name
        (unknown → ""), ``alias`` = this device's user-set memo ("" = unset,
        the web UI falls back to ``name``), ``online`` = a frame was received
        within the last ``NETPAIR_ONLINE_WINDOW`` seconds, ``last_seen`` = the
        epoch-seconds of that last frame (or None), ``paired`` = True (this
        list only contains confirmed internet pairs).
        """
        code = next(iter(getattr(self, "_netpair_pending", {})), None)
        if now is None:
            now = time.time()
        peers = []
        for pid in (getattr(self.cfg, "netpair_secrets", {}) or {}):
            alias = (getattr(self.cfg, "netpair_aliases", {}) or {}).get(pid, "")
            name = getattr(self, "_netpair_names", {}).get(pid, "")
            if not name:
                peer = self.cfg.peers.get(pid)
                if peer is not None:
                    name = getattr(peer, "device_name", "")
            last_seen = getattr(self, "_netpair_last_seen", {}).get(pid)
            online = (last_seen is not None
                      and (now - last_seen) <= NETPAIR_ONLINE_WINDOW)
            peers.append({
                "peer_id": pid,
                "name": name,
                "alias": alias,
                "online": bool(online),
                "last_seen": last_seen,
                "paired": True,
            })
        peers.sort(key=lambda p: p["peer_id"])
        return {"generated_code": code, "peers": peers}, 200

    def _netpair_rename(self, peer_id: str,
                        name: str | None = None) -> tuple[dict, int]:
        """Set or clear this device's alias for an internet-paired peer (REST).

        Local-only: the peer is never told.  ``name`` empty (or whitespace)
        clears the alias.  400 when the peer is not a confirmed internet pair
        or the alias value is not a plain string (or exceeds 120 chars).
        """
        secrets = getattr(self.cfg, "netpair_secrets", {}) or {}
        if not isinstance(peer_id, str) or peer_id not in secrets:
            return {"ok": False, "error": "unknown peer"}, 400
        if not isinstance(name, str):
            return {"ok": False, "error": "invalid name"}, 400
        name = name.strip()
        if len(name) > 120:
            return {"ok": False, "error": "name too long"}, 400
        aliases = getattr(self.cfg, "netpair_aliases", {})
        if name:
            aliases[peer_id] = name
        else:
            aliases.pop(peer_id, None)
        try:
            self._save_cfg_and_peers()
        except Exception:
            logger.debug("Failed persisting netpair alias", exc_info=True)
        return {"ok": True}, 200

    def _netpair_unpair(self, peer_id: str) -> tuple[dict, int]:
        """Unpair this device from an internet-paired peer (REST).

        Removes the shared secret + alias + runtime name/last-seen state, then
        refreshes the relay channels so the peer's topic is no longer
        subscribed.  If the peer was ALSO a LAN-paired cfg.peers member (same
        device id), the LAN relationship is left untouched — only the internet
        pairing is severed.  This is a one-sided break: the remote side keeps
        its copy of the secret until it unpairs itself.
        """
        secrets = getattr(self.cfg, "netpair_secrets", {}) or {}
        if not isinstance(peer_id, str) or peer_id not in secrets:
            return {"ok": False, "error": "unknown peer"}, 400
        secrets.pop(peer_id, None)
        (getattr(self.cfg, "netpair_aliases", {}) or {}).pop(peer_id, None)
        self._netpair_names.pop(peer_id, None)
        getattr(self, "_netpair_last_seen", {}).pop(peer_id, None)
        try:
            self._save_cfg_and_peers()
        except Exception:
            logger.debug("Failed persisting netpair unpair", exc_info=True)
        # Round 17: the unpaired peer must stop burning relay retries, and its
        # base64 payloads must not linger on disk in relay_pending.json.
        getattr(self, "_delivery_clear_peer", lambda pid: None)(peer_id)
        if self._relay is not None:
            try:
                self._relay.refresh_channels()
            except Exception:
                logger.debug("netpair unpair refresh failed", exc_info=True)
        # Keep every web tab on this device in step: the acting tab removes the
        # row via the REST response, but sibling tabs only learn about it
        # through a WS push (ws.js folds status:"unpaired" into the peer list).
        try:
            if getattr(self, "web_server", None) is not None:
                self.web_server.ws_manager.broadcast(
                    "netpair_peer", {"peer_id": peer_id, "status": "unpaired"})
        except Exception:
            logger.debug("netpair_peer unpair WS broadcast failed", exc_info=True)
        return {"ok": True}, 200

    def _netpair_test(self, body=None) -> tuple[dict, int]:
        """POST /api/internetpair/test — probe the configured relay brokers.

        Opens a short TCP (+TLS) socket to each broker independently (the live
        relay client is untouched), so the user gets a per-broker reachability
        + latency readout without any chance of disrupting an active session.
        An optional ``{"brokers": [...]}`` body probes that list instead of
        the persisted cfg.relay_brokers (used when the user tests edits before
        saving them).
        """
        from internal.transport.relay import probe_relay_endpoint
        brokers = None
        if isinstance(body, dict):
            cand = body.get("brokers")
            if isinstance(cand, list):
                brokers = [str(b).strip() for b in cand if isinstance(b, str) and b.strip()]
        if not brokers:
            brokers = list(getattr(self.cfg, "relay_brokers", []) or [])
        if not brokers:
            return {"ok": False, "error": "no relay brokers configured"}, 400
        results: list[dict] = []
        _lock = threading.Lock()

        def _probe(endpoint: str) -> None:
            r = probe_relay_endpoint(endpoint)
            with _lock:
                results.append(r)

        threads = [
            threading.Thread(target=_probe, args=(b,), daemon=True) for b in brokers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=6.0)
        ok_count = sum(1 for r in results if r.get("ok"))
        results.sort(key=lambda r: (
            not r.get("ok"), r.get("latency_ms") is None, r.get("latency_ms") or 0,
        ))
        return {
            "ok": True,
            "results": results,
            "summary": f"{ok_count}/{len(brokers)} reachable",
        }, 200

    def _handle_device_probe(self, msg_type: str, payload: dict,
                             peer_id: str, via_relay: bool) -> None:
        """A ``device_ping``/``device_pong`` from a paired peer.

        A ping is answered with a pong echoing the same ping_id + ts, sent back
        on the channel it arrived on (LAN ``transport_mgr`` or public relay).
        A pong resolves the matching in-flight probe for that channel and
        records the round-trip latency (both timestamps are local monotonic —
        this side sent the ping and receives the pong).
        """
        chan = "relay" if via_relay else "lan"
        if msg_type == "device_ping":
            pong = {
                "msg_type": "device_pong",
                "ping_id": str(payload.get("ping_id") or ""),
                "ts": payload.get("ts"),
            }
            frame = encode_frame(pong, source_device=self.cfg.device_id)
            if via_relay:
                self._relay_publish_to_peer(frame, peer_id)
            else:
                # LAN peer may have just dropped — a failed send is a valid
                # negative probe result, not an error to crash on.
                self.transport_mgr.send_to_peer(peer_id, frame)
            return
        if msg_type == "device_pong":
            ping_id = str(payload.get("ping_id") or "")
            if not ping_id:
                return
            with self._device_probes_lock:
                entry = self._device_probes.get(ping_id)
                if entry is None:
                    return
                res = entry["results"].get(chan)
                if res is None:
                    return
                res["ok"] = True
                res["latency_ms"] = (time.monotonic() - res["send_ts"]) * 1000.0
                entry["replied"].add(chan)
                entry["event"].set()

    def _device_test_connection(self, peer_id: str) -> dict:
        """Probe every reachable channel to *peer_id* and measure RTT.

        Returns ``{ok, results: [{channel, ok, latency_ms?, error?}]}`` where
        ``channel`` is ``lan`` and/or ``relay`` — whichever the peer is
        reachable through.  Each channel gets its own device_ping; a pong must
        echo the same ping_id back within ``DEVICE_PING_TIMEOUT`` seconds.
        """
        lan_channels: list[str] = []
        try:
            connected = set(self.transport_mgr.get_connected_peers() or set())
            if peer_id in connected:
                lan_channels = ["lan"]
        except Exception:
            logger.debug("device test: LAN connectivity check failed",
                         exc_info=True)
        relay_ok = False
        try:
            relay_ok = bool(self._peer_is_internet_reachable(peer_id))
        except Exception:
            logger.debug("device test: relay reachability check failed",
                         exc_info=True)
        channels = lan_channels + (["relay"] if relay_ok else [])
        if not channels:
            return {
                "ok": False,
                "results": [],
                "error": "no_channel",
            }
        ping_id = secrets.token_hex(6)
        entry = {
            "results": {
                c: {
                    "channel": c,
                    "ok": False,
                    "send_ts": time.monotonic(),
                    "latency_ms": None,
                    "error": "timeout",
                } for c in channels
            },
            "replied": set(),
            "event": threading.Event(),
        }
        with self._device_probes_lock:
            self._device_probes[ping_id] = entry
        # Fire one ping per channel; a failed send marks that channel failed
        # up-front (no pong will arrive for it).
        ts = time.monotonic()
        frame = encode_frame(
            {"msg_type": "device_ping", "ping_id": ping_id, "ts": ts},
            source_device=self.cfg.device_id,
        )
        for c in channels:
            try:
                if c == "lan":
                    self.transport_mgr.send_to_peer(peer_id, frame)
                else:
                    self._relay_publish_to_peer(frame, peer_id)
            except Exception:
                logger.debug("device test: %s ping send failed", c,
                             exc_info=True)
                entry["results"][c]["error"] = "send_failed"
        # Wait for every channel to pong (or the timeout to elapse).
        deadline = time.monotonic() + DEVICE_PING_TIMEOUT
        while time.monotonic() < deadline:
            with self._device_probes_lock:
                if len(entry["replied"]) >= len(channels):
                    break
            entry["event"].wait(min(deadline - time.monotonic(), 0.2))
        with self._device_probes_lock:
            self._device_probes.pop(ping_id, None)
        results = [
            {
                "channel": r["channel"],
                "ok": bool(r["ok"]),
                "latency_ms": r["latency_ms"],
                "error": None if r["ok"] else r["error"],
            }
            for r in entry["results"].values()
        ]
        results.sort(key=lambda r: not r["ok"])
        return {
            "ok": any(r["ok"] for r in results),
            "results": results,
        }

    def _send_netpair_hello(self, target_peer_id: str, secret: str) -> None:
        """Publish a netpair_hello frame on the secret's channel."""
        transport = self._relay
        if transport is None or not self.cfg.internet_sync_enabled:
            return
        try:
            from internal.transport.relay import netpair_key, netpair_topic
            payload = {
                "msg_type": "netpair_hello",
                "peer_id": target_peer_id,
                "device_name": self.cfg.device_name,
                "ts": time.time(),
            }
            frame = encode_frame(payload, source_device=self.cfg.device_id)
            transport.publish(frame, netpair_topic(secret), netpair_key(secret, self._netpair_pw()))
        except Exception:
            logger.debug("netpair hello publish failed", exc_info=True)

    def _handle_netpair_hello(self, payload: dict,
                              source_device: str | None,
                              topic: str | None) -> None:
        """A netpair_hello arrived on one of our netpair channels.

        Two roles, distinguished by the hello's ``peer_id`` field:

        GENERATOR (A) — the hello names OUR device tag (the one we put in the
          code).  The frame's source_device is the enterer's real id: persist
          netpair_secrets[enterer_id] = secret, broadcast ``netpair_peer`` and
          reply with a hello so the enterer also confirms identity.

        ENTERER (B) — the reply hello names OUR real device id.  The frame's
          source_device is the generator's real id: re-key the provisional tag
          entry to the generator's real id and broadcast ``netpair_peer``.
        """
        if not isinstance(payload, dict):
            return
        secret = self._netpair_secret_for_topic(topic or "")
        if secret is None:
            return
        from internal.transport.relay import netpair_device_tag
        incoming_tag = payload.get("peer_id")
        peer_id = source_device if isinstance(source_device, str) else ""
        # A frame from ourselves (e.g. entering our own code on the same
        # machine) must never become a paired peer.
        if peer_id and peer_id == self.cfg.device_id:
            return
        our_tag = netpair_device_tag(self.cfg.device_id)
        if incoming_tag == self.cfg.device_id:
            # Reply to us — we are the ENTERER.  Only accept when we are not
            # also the tag's owner (a real 12-hex id never equals a base32 tag).
            if not peer_id:
                return
            # Re-key the provisional tag entry for this secret to the real id.
            for k in [k for k, v in (getattr(self.cfg, "netpair_secrets", {}) or {}).items()
                      if v == secret and k != peer_id]:
                self.cfg.netpair_secrets.pop(k, None)
            self.cfg.netpair_secrets[peer_id] = secret
            name = payload.get("device_name") or peer_id
            self._netpair_names[peer_id] = name
        elif incoming_tag == our_tag:
            # First hello from the code's holder — we are the GENERATOR.
            if not peer_id:
                return
            self.cfg.netpair_secrets[peer_id] = secret
            name = payload.get("device_name") or peer_id
            self._netpair_names[peer_id] = name
            # The pairing is confirmed — drop the pending code entry.
            for code in [c for c, s in getattr(self, "_netpair_pending", {}).items()
                         if s == secret]:
                self._netpair_pending.pop(code, None)
            # Reply so the enterer also confirms identity.
            self._send_netpair_hello(peer_id, secret)
        else:
            return  # not a reply to our code / not for us — ignore
        # A confirmed handshake is itself proof the peer is alive: mark it seen
        # so the device page shows online immediately after pairing (the source
        # may not be in netpair_secrets at the _on_relay_frame check above — the
        # generator only adds the enterer's real id here).
        if peer_id:
            getattr(self, "_netpair_last_seen", {})[peer_id] = time.time()
            # Round 17: a hello is an active signal — retry the peer's queue.
            try:
                self._delivery_peer_active(peer_id)
            except Exception:
                logger.debug("delivery retry on hello failed", exc_info=True)
        try:
            self._save_cfg_and_peers()
        except Exception:
            logger.debug("Failed persisting netpair confirmation", exc_info=True)
        if self._relay is not None:
            try:
                self._relay.refresh_channels()
            except Exception:
                logger.debug("netpair refresh after hello failed", exc_info=True)
        try:
            if getattr(self, "web_server", None) is not None:
                self.web_server.ws_manager.broadcast(
                    "netpair_peer", {"peer_id": peer_id, "name": name,
                                     "status": "paired"})
        except Exception:
            logger.debug("netpair_peer WS broadcast failed", exc_info=True)

    def _apply_internet_sync_enabled(self, enabled: bool) -> None:
        """Live-apply the internet_sync_enabled setting."""
        enabled = bool(enabled)
        if enabled:
            self._start_internet_sync()
        else:
            self._stop_internet_sync()

    def _restore_timed_pause(self) -> None:
        """Re-arm a timed pause that a restart interrupted (phase-10 hook).

        A pending "pause for N minutes" used to die with the process while its
        effect (sync_enabled=False) stayed persisted — an auto-update relaunch
        mid-pause left sync disabled forever.  The deadline is persisted in
        the config now: restarting INSIDE the window re-arms the remaining
        time; restarting after it expired re-enables sync.
        """
        try:
            deadline = float(getattr(self.cfg, "timed_pause_until", 0.0) or 0.0)
        except (TypeError, ValueError):
            deadline = 0.0
        if deadline <= 0.0:
            return
        self.cfg.timed_pause_until = 0.0
        remaining = deadline - time.time()
        if remaining <= 1.0:
            logger.info("Timed sync pause elapsed while app was closed — resuming")
            if not self.cfg.sync_enabled:
                self._on_systray_toggle(True)
            else:
                self._save_cfg_encrypted()
            return
        minutes = max(1, int(remaining // 60) + (1 if remaining % 60 else 0))
        self._pause_sync_for_minutes(minutes)

    def _on_systray_toggle(self, enabled: bool) -> None:
        # An explicit toggle outranks any pending timed pause: clear its
        # timer/countdown first so it cannot fire into a state the user
        # just chose by hand.
        self._clear_pause_state()
        # sync_mgr is the authoritative source of truth: reconcile the tray
        # checkbox and config from its actual state so the optimistic tray
        # flip can never leave them disagreeing.
        try:
            self.sync_mgr.set_enabled(enabled)
            actual = getattr(self.sync_mgr, "_enabled", enabled)
        except Exception:
            logger.warning("Failed to toggle sync state", exc_info=True)
            actual = not enabled
        self.cfg.sync_enabled = actual
        self._save_cfg_encrypted()
        self._set_systray_syncing(actual)
        self._notify("notify_sync", T("ui.clipboard_sync"),
                     T("notify.sync_active") if actual else T("notify.sync_paused"))
        logger.info("Sync %s", "enabled" if actual else "paused")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════


def _prewarm_codecs() -> None:
    """Eagerly load the text codecs a one-file build imports lazily.

    PyInstaller one-file builds keep the individual ``encodings.*`` codecs in
    ``base_library.zip`` inside the ``_MEI`` extraction dir and import them on
    first use. Under the one-file temp-dir race that dir can be deleted while
    the process is still alive, so the first such import — e.g.
    ``pathlib.Path.write_text(encoding="ascii")`` → ``encodings.ascii`` — dies
    with ``FileNotFoundError: base_library.zip``. Importing them up front,
    while ``base_library.zip`` is guaranteed intact, moves every codec load out
    of the racy window.
    """
    import codecs
    import locale

    _names = {
        "ascii", "latin-1", "utf-8", "utf-8-sig", "utf-16", "utf-16-le",
        "utf-16-be", "utf-32", "utf-32-le", "utf-32-be",
        "unicode_escape", "raw_unicode_escape", "hex", "base64_codec",
        "idna", "punycode", "cp1252", "cp437",
    }
    # Path.read_text()/write_text() without an explicit encoding fall back to
    # the locale's preferred encoding (e.g. cp936/gbk on zh-CN Windows), which
    # is lazily imported from encodings.* just like "ascii". Warm it plus the
    # stdio codecs so the next default-encoding I/O can't re-enter the racy
    # base_library.zip import this function exists to avoid.
    _names.add(locale.getpreferredencoding(False))
    for _stream in (sys.stdout, sys.stderr):
        try:
            _enc = _stream.encoding
        except Exception:
            _enc = None
        if _enc:
            _names.add(_enc)
    for _name in _names:
        try:
            codecs.lookup(_name)
        except Exception:
            pass


def main():
    _prewarm_codecs()
    Application.setup_logging()

    # Prevent duplicate instances (macOS tray icon bug)
    if not _check_and_cleanup_stale_lock():
        # Another instance is running — show error and exit
        _r = tk.Tk()
        _r.withdraw()
        show_error(_r, "ClipSync", T("ui.already_running"))
        _r.destroy()
        sys.exit(1)

    # Claim the single-instance lock immediately, before any heavy startup
    # work. Writing it late (after _start_services) left a window in which a
    # second launch also passed the stale-lock check, and two one-file
    # instances racing their _MEI extraction dirs is what deletes a live
    # base_library.zip in the first place.
    _write_lock(os.getpid())
    atexit.register(_remove_lock)

    app = Application()
    app.load_config()
    app._bootstrap_crypto()
    app._bootstrap_identity()
    app._create_services()
    app._wire_callbacks()
    app._apply_config()
    app._create_ui()
    app._show_first_run_onboarding_if_needed()
    app._start_services()
    # A webview-mode fresh install that downgraded to the ctk backend (all web
    # ports busy) skipped the picker above (it deferred to the SPA wizard) —
    # re-check once services are up so the first-run language wizard still
    # appears on this launch.
    app._show_first_run_onboarding_if_needed()
    app._start_threads()

    app.run()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
