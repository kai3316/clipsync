"""Main dashboard window for ClipSync — sidebar navigation with rich panels.

Panels: Overview, Devices (with pairing), History, Transfers.
Settings is a separate window accessed via the sidebar button.
"""

import datetime
import logging
import platform
import socket
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from internal.i18n import T
from internal.ui.dialogs import ask_string, ask_yesno, show_error, show_info

logger = logging.getLogger(__name__)

STATUS_COLOR = "#2ECC71"
PAIRING_COLOR = "#E67E22"
OFFLINE_COLOR = "#95A5A6"
WARN_COLOR = "#F39C12"
ACCENT = "#3498DB"

STATUS_COLORS = {
    "Connected":  "#2ECC71",
    "Paired":     "#F39C12",
    "Discovered": "#3498DB",
    "Pending":    "#95A5A6",
}


class DashboardWindow:
    """Main application window with sidebar + panels."""

    # ── Per-card constants (computed once, not per card) ────────
    _TYPE_COLORS: dict[str, tuple[str, str]] = {
        "TEXT": ("#27AE60", "#2ECC71"),
        "HTML": ("#E67E22", "#F39C12"),
        "IMAGE": ("#8E44AD", "#9B59B6"),
        "IMAGE_EMF": ("#3498DB", "#5DADE2"),
        "RTF": ("#7F8C8D", "#95A5A6"),
        "FILE": ("#C0392B", "#E74C3C"),
        "URL": ("#2980B9", "#3498DB"),
    }

    def __init__(
        self,
        root: tk.Tk,
        get_config: Callable,
        save_config: Callable,
        get_peers: Callable,
        get_sync_enabled: Callable,
        set_sync_enabled: Callable,
        on_open_settings: Callable | None = None,
        on_send_file: Callable | None = None,
        on_send_folder: Callable | None = None,
        on_toggle_autostart: Callable | None = None,
        get_transfers: Callable | None = None,
        on_cancel_transfer: Callable | None = None,
        on_pause_transfer: Callable | None = None,
        on_resume_transfer: Callable | None = None,
        # Pairing
        get_pending_pairings: Callable | None = None,
        on_pair: Callable | None = None,
        on_unpair: Callable | None = None,
        on_connect_peer: Callable | None = None,
        on_disconnect_peer: Callable | None = None,
        on_remove_peer: Callable | None = None,
        # History
        get_history: Callable | None = None,
        search_history: Callable | None = None,
        copy_from_history: Callable | None = None,
        clear_history: Callable | None = None,
        delete_history_item: Callable | None = None,
        # Lifecycle
        on_hidden: Callable | None = None,
        # Discovery / visibility
        get_discovering: Callable | None = None,
        get_visible: Callable | None = None,
        on_toggle_discovery: Callable | None = None,
        on_toggle_visibility: Callable | None = None,
        # Transfers
        get_transfer_history: Callable | None = None,
        on_speed_test: Callable | None = None,
        get_speed_test_result: Callable | None = None,
        clear_transfer_history: Callable | None = None,
        delete_transfer_history_item: Callable | None = None,
        on_open_file: Callable | None = None,
        on_open_folder: Callable | None = None,
        on_retry_transfer: Callable | None = None,
        # Notes
        on_edit_note: Callable | None = None,
        # Web companion
        on_web_action: Callable | None = None,
    ):
        self._root = root
        self._get_config = get_config
        self._save_config = save_config
        self._get_peers = get_peers
        self._get_sync = get_sync_enabled
        self._set_sync = set_sync_enabled
        self._on_open_settings = on_open_settings
        self._on_send_file = on_send_file
        self._on_send_folder = on_send_folder
        self._on_toggle_autostart_cb = on_toggle_autostart
        self._get_transfers = get_transfers
        self._on_cancel_transfer = on_cancel_transfer
        self._on_pause_transfer = on_pause_transfer
        self._on_resume_transfer = on_resume_transfer
        self._get_pending = get_pending_pairings
        self._on_pair = on_pair
        self._on_unpair = on_unpair
        self._on_connect_peer = on_connect_peer
        self._on_disconnect_peer = on_disconnect_peer
        self._on_remove_peer = on_remove_peer
        self._get_history = get_history
        self._search_history = search_history
        self._copy_from_history = copy_from_history
        self._clear_history = clear_history
        self._delete_history_item = delete_history_item
        self._on_hidden = on_hidden
        self._get_discovering = get_discovering
        self._get_visible = get_visible
        self._on_toggle_discovery_cb = on_toggle_discovery
        self._on_toggle_visibility_cb = on_toggle_visibility
        self._get_transfer_history = get_transfer_history
        self._on_speed_test = on_speed_test
        self._get_speed_test_result = get_speed_test_result
        self._clear_transfer_history = clear_transfer_history
        self._delete_transfer_history_item = delete_transfer_history_item
        self._on_open_file = on_open_file
        self._on_open_folder = on_open_folder
        self._on_retry_transfer = on_retry_transfer
        self._on_edit_note = on_edit_note
        self._on_web_action = on_web_action

        self._window: ctk.CTkToplevel | None = None
        self._dark_mode = get_config().appearance_mode == "dark"
        self._current_panel = "overview"
        self._refresh_job: str | None = None
        self._breathing = False
        self._breath_timer: str | None = None

        # Edge snapping state
        self._snap_distance = 15
        self._snap_unsnap_deadzone = 8  # extra px to break free
        self._snap_state: dict | None = None  # {"side": str|None, "side2": str|None}

        # Sidebar
        self._sidebar_buttons: dict[str, ctk.CTkButton] = {}
        self._panels: dict[str, ctk.CTkFrame] = {}
        self._content_frame: ctk.CTkFrame | None = None

        # Form vars
        self._sync_var: tk.BooleanVar | None = None
        self._autostart_var: tk.BooleanVar | None = None
        self._history_search_var: tk.StringVar | None = None
        self._discovery_var: tk.BooleanVar | None = None
        self._visibility_var: tk.BooleanVar | None = None

        # Widget refs
        self._status_dot: ctk.CTkFrame | None = None
        self._status_label: ctk.CTkLabel | None = None
        self._stat_peers: ctk.CTkLabel | None = None
        self._stat_paired: ctk.CTkLabel | None = None
        self._stat_history: ctk.CTkLabel | None = None
        self._stat_transfers: ctk.CTkLabel | None = None
        self._stat_discovery: ctk.CTkLabel | None = None
        self._stat_visibility: ctk.CTkLabel | None = None
        self._sub_peers: ctk.CTkLabel | None = None
        self._sub_paired: ctk.CTkLabel | None = None
        self._sub_history: ctk.CTkLabel | None = None
        self._sub_transfers: ctk.CTkLabel | None = None
        self._sub_discovery: ctk.CTkLabel | None = None
        self._sub_visibility: ctk.CTkLabel | None = None
        self._recent_activity: ctk.CTkLabel | None = None
        self._uptime_label: ctk.CTkLabel | None = None
        self._local_ip_label: ctk.CTkLabel | None = None
        self._start_time: float = 0.0
        self._device_name_label: ctk.CTkLabel | None = None
        self._device_scroll: ctk.CTkScrollableFrame | None = None
        self._transfer_scroll: ctk.CTkScrollableFrame | None = None
        self._transfer_history_scroll: ctk.CTkScrollableFrame | None = None
        self._speed_card: ctk.CTkFrame | None = None
        self._speed_status: ctk.CTkLabel | None = None
        self._speed_progress: ctk.CTkProgressBar | None = None
        self._speed_value: ctk.CTkLabel | None = None
        self._speed_quality: ctk.CTkLabel | None = None
        self._speed_hint: ctk.CTkLabel | None = None
        self._speed_result_row: ctk.CTkFrame | None = None
        self._history_scroll: ctk.CTkScrollableFrame | None = None
        self._pending_frame: ctk.CTkFrame | None = None
        self._footer_label: ctk.CTkLabel | None = None
        self._status_footer: ctk.CTkLabel | None = None
        self._clear_history_btn: ctk.CTkButton | None = None
        self._overview_device_name: ctk.CTkLabel | None = None
        self._last_history_count: int = 0
        self._history_shown: int = 0       # how many cards currently rendered
        self._history_entries: list = []   # full entry list for lazy loading
        self._history_peer_map: dict = {}  # cached peer id→name map
        self._history_more_btn: ctk.CTkButton | None = None

        # Per-card cached objects (created once, reused across all cards)
        self._card_font_bold: ctk.CTkFont | None = None
        self._card_font: ctk.CTkFont | None = None
        self._card_font_small: ctk.CTkFont | None = None
        self._card_font_btn: ctk.CTkFont | None = None
        self._card_type_labels: dict[str, str] | None = None
        self._cached_device_id: str | None = None

        # Web companion widgets (overview)
        self._web_switch_var: tk.BooleanVar | None = None
        self._web_card: ctk.CTkFrame | None = None
        self._web_qr_label: ctk.CTkLabel | None = None
        self._web_url_label: ctk.CTkLabel | None = None
        self._web_copy_btn: ctk.CTkButton | None = None
        self._web_switch: ctk.CTkSwitch | None = None
        self._activity_card: ctk.CTkFrame | None = None
        # Cached QR image + key — regenerated only when token/IP/port change
        self._web_qr_image: ctk.CTkImage | None = None
        self._web_qr_key: tuple | None = None

    # ═══════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════

    def show(self):
        if self._window is not None:
            try:
                self._window.deiconify()
                self._window.lift()
                self._window.focus_force()
                self._window.update_idletasks()
                if self._window.winfo_viewable():
                    self._switch_panel(self._current_panel)
                    self._schedule_refresh()
                    return
                self._window.destroy()
                self._window = None
            except tk.TclError:
                self._window = None

        logger.info("Opening ClipSync dashboard")
        self._dark_mode = self._get_config().appearance_mode == "dark"
        ctk.set_appearance_mode("dark" if self._dark_mode else "light")
        ctk.set_default_color_theme("blue")

        self._window = ctk.CTkToplevel(self._root)
        self._window.title(T("ui.app_name"))
        self._window.geometry("900x640")
        self._window.minsize(780, 560)
        self._window.protocol("WM_DELETE_WINDOW", self._on_hide)

        # Keyboard shortcuts
        self._window.bind("<Escape>", lambda _e: self._on_hide())
        self._window.bind("<Control-q>", lambda _e: self._on_close())

        # Edge snapping
        self._snap_state = {"side": None, "side2": None}
        self._window.bind("<Configure>", self._on_window_move)
        # Also bind to the underlying Tk widget for more reliable drag detection
        self._window.bind("<Configure>", self._on_window_move, add="+")

        self._window.update_idletasks()
        sw = self._window.winfo_screenwidth()
        sh = self._window.winfo_screenheight()
        w, h = 900, 640
        self._window.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._build_ui()
        self._switch_panel("overview")
        self._schedule_refresh()

    def _on_window_move(self, event=None):
        """Snap the window to screen edges when within SNAP_DISTANCE pixels."""
        if self._window is None or self._snap_state is None:
            return
        try:
            x = self._window.winfo_x()
            y = self._window.winfo_y()
            w = self._window.winfo_width()
            h = self._window.winfo_height()
            screen_w = self._window.winfo_screenwidth()
            screen_h = self._window.winfo_screenheight()
        except tk.TclError:
            return

        # If already snapped, require a larger threshold to break free
        extra = self._snap_unsnap_deadzone if self._snap_state.get("side") else 0
        dist = self._snap_distance + extra

        snap_x = x
        snap_y = y
        snapped_side = None
        snapped_side2 = None

        # Left edge
        if abs(x) <= dist:
            snap_x = 0
            snapped_side = "left"
        # Right edge
        elif abs(x + w - screen_w) <= dist:
            snap_x = screen_w - w
            snapped_side = "right"

        # Top edge
        if abs(y) <= dist:
            snap_y = 0
            snapped_side2 = "top"
            if snapped_side is None:
                snapped_side = "top"
        # Bottom edge (account for taskbar — use screen height minus ~40px buffer)
        elif abs(y + h - screen_h) <= dist:
            snap_y = screen_h - h
            snapped_side2 = "bottom"
            if snapped_side is None:
                snapped_side = "bottom"

        # Corner case: if both axes snapped, record both
        if snapped_side2 and snapped_side != snapped_side2:
            self._snap_state = {"side": snapped_side, "side2": snapped_side2}
        else:
            self._snap_state = {"side": snapped_side, "side2": None}

        if snap_x != x or snap_y != y:
            try:
                self._window.geometry(f"+{snap_x}+{snap_y}")
            except tk.TclError:
                pass

    def _cancel_pending_timers(self):
        """Cancel every scheduled after() callback before hide/close.

        Timers left running after the window is destroyed fire against dead
        widgets and raise TclError "invalid command name" (chunked history
        renderer, search debounce, copy-URL button reset).
        """
        for name in (
            "_breath_timer", "_refresh_job", "_history_search_timer",
            "_history_chunk_timer", "_copy_url_timer",
        ):
            timer = getattr(self, name, None)
            if timer is not None:
                try:
                    self._root.after_cancel(timer)
                except Exception:
                    pass
                setattr(self, name, None)

    def _on_hide(self):
        self._breathing = False
        self._cancel_pending_timers()
        if self._window is not None:
            # CTkToplevel.deiconify() after withdraw() is unreliable on
            # Linux (same root cause as the dialog deadlocks).  Destroy
            # the window instead so show() creates a fresh one.
            if sys.platform == "linux":
                try:
                    self._window.destroy()
                except Exception:
                    pass
                self._window = None
            else:
                self._window.withdraw()
        if self._on_hidden:
            self._on_hidden()

    def _on_close(self):
        self._breathing = False
        self._cancel_pending_timers()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _schedule_refresh(self):
        self._refresh_overview()
        if self._current_panel == "devices":
            self._refresh_devices()
        elif self._current_panel == "transfers":
            self._refresh_transfers()
        elif self._current_panel == "history":
            # Lightweight check: only rebuild if count changed
            if self._get_history is not None:
                try:
                    entries = self._get_history()
                    if len(entries) != self._last_history_count:
                        self._last_history_count = len(entries)
                        self._refresh_history_list()
                except Exception:
                    pass
        if self._window is not None:
            self._refresh_job = self._root.after(5000, self._schedule_refresh)

    # ═══════════════════════════════════════════════════════════════
    # UI construction
    # ═══════════════════════════════════════════════════════════════

    def _build_ui(self):
        cfg = self._get_config()
        outer = ctk.CTkFrame(self._window, fg_color="transparent")
        outer.pack(fill="both", expand=True)

        # ── Header ──────────────────────────────────────────────────
        header = ctk.CTkFrame(outer, corner_radius=0, fg_color=("#1A5276", "#1B2A3A"))
        header.pack(fill="x")
        h_inner = ctk.CTkFrame(header, fg_color="transparent")
        h_inner.pack(fill="x", padx=20, pady=(16, 14))
        # Accent line below header
        accent_line = ctk.CTkFrame(header, height=2, fg_color=("#3498DB", "#2980B9"))
        accent_line.pack(fill="x", side="bottom")

        ctk.CTkLabel(
            h_inner, text=T("ui.app_title"),
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=("#FFFFFF", "#E0E0E0"),
        ).pack(side="left")

        ctk.CTkLabel(
            h_inner, text="·",
            font=ctk.CTkFont(size=18),
            text_color=("#5DADE2", "#3498DB"),
        ).pack(side="left", padx=(10, 10))

        self._device_name_label = ctk.CTkLabel(
            h_inner, text=T("overview.this_device_label", name=cfg.device_name),
            font=ctk.CTkFont(size=13),
            text_color=("#D5D8DC", "#ABB2B9"),
        )
        self._device_name_label.pack(side="left")

        # ── Body: sidebar | content ─────────────────────────────────
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True)

        self._build_sidebar(body)

        sep = ctk.CTkFrame(body, width=1, fg_color=("gray75", "gray30"))
        sep.pack(side="left", fill="y")

        self._content_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._content_frame.pack(side="left", fill="both", expand=True)

        # Build panels
        self._panels["overview"] = self._build_overview_panel()
        self._panels["devices"] = self._build_devices_panel()
        self._panels["history"] = self._build_history_panel()
        self._panels["transfers"] = self._build_transfers_panel()

        # ── Footer ──────────────────────────────────────────────────
        footer = ctk.CTkFrame(outer, height=46, corner_radius=0,
                              fg_color=("gray90", "gray15"))
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        # Accent line above footer
        fline = ctk.CTkFrame(footer, height=1, fg_color=("gray75", "gray30"))
        fline.pack(fill="x", side="top")
        f_inner = ctk.CTkFrame(footer, fg_color="transparent")
        f_inner.pack(fill="x", padx=20, pady=8)

        self._status_footer = ctk.CTkLabel(
            f_inner, text=T("footer.ready"),
            text_color=("gray50", "gray60"),
            font=ctk.CTkFont(size=11),
        )
        self._status_footer.pack(side="left")

        ctk.CTkButton(
            f_inner, text=T("ui.hide_to_tray"), width=100, height=28,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray70"),
            border_color=("gray60", "gray50"),
            hover_color=("gray85", "gray25"),
            command=self._on_hide,
        ).pack(side="right")

    # ── Sidebar ────────────────────────────────────────────────────

    def _build_sidebar(self, body):
        sidebar = ctk.CTkFrame(body, width=180, fg_color="transparent")
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        inner = ctk.CTkFrame(sidebar, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=8, pady=16)

        nav = [
            ("overview",       T("nav.overview")),
            ("devices",        T("nav.devices")),
            ("history",        T("nav.history")),
            ("transfers",      T("nav.transfers")),
        ]

        for key, label in nav:
            btn = ctk.CTkButton(
                inner, text=label, anchor="w",
                height=42, corner_radius=10,
                fg_color="transparent",
                text_color=("gray30", "gray80"),
                hover_color=("gray85", "gray25"),
                font=ctk.CTkFont(size=15),
                command=lambda k=key: self._switch_panel(k),
            )
            btn.pack(fill="x", pady=2)
            self._sidebar_buttons[key] = btn

        # Settings button at bottom
        spacer = ctk.CTkFrame(inner, fg_color="transparent")
        spacer.pack(fill="y", expand=True)

        if self._on_open_settings:
            ctk.CTkButton(
                inner, text=T("nav.settings"), anchor="w",
                height=36, corner_radius=8,
                fg_color="transparent", border_width=1,
                text_color=("gray40", "gray70"),
                border_color=("gray60", "gray50"),
                hover_color=("gray85", "gray25"),
                font=ctk.CTkFont(size=14),
                command=self._on_open_settings,
            ).pack(fill="x", pady=(0, 2))

    # ═══════════════════════════════════════════════════════════════
    # Panel switching
    # ═══════════════════════════════════════════════════════════════

    def _switch_panel(self, key: str):
        if self._content_frame is None:
            return
        for pk, panel in self._panels.items():
            if pk == key:
                panel.pack(in_=self._content_frame, fill="both", expand=True,
                          padx=20, pady=16)
            else:
                panel.pack_forget()
        for pk, btn in self._sidebar_buttons.items():
            if pk == key:
                btn.configure(
                    fg_color=("#2A82C7", "#1F6AA5"),
                    text_color=("#FFFFFF", "#FFFFFF"),
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color=("gray30", "gray80"),
                )
        self._current_panel = key

        # Force an immediate redraw so the panel chrome (header, search
        # bar, buttons) appears instantly and old-panel content is
        # cleared, before the potentially slow data refresh kicks in.
        if self._window is not None:
            try:
                self._window.update()
            except tk.TclError:
                pass

        if key == "devices":
            self._refresh_devices()
        elif key == "transfers":
            self._refresh_transfers()
        elif key == "history":
            self._refresh_history_list()
        elif key == "overview":
            self._refresh_overview()

    # ═══════════════════════════════════════════════════════════════
    # Panel: Overview
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _detect_local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            s.connect(("10.254.254.254", 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    _network_info_cache: dict | None = None
    _network_detect_started = False

    @classmethod
    def _detect_network_info(cls) -> dict:
        """Detect current network (WiFi SSID + signal / Ethernet link speed).

        Cached per-process: the probes (``netsh`` / ``powershell``) can take
        up to ~15s and every dashboard rebuild used to re-run them on the UI
        thread, freezing the whole app.
        """
        if cls._network_info_cache is not None:
            return cls._network_info_cache
        result = cls._detect_network_info_uncached()
        cls._network_info_cache = result
        return result

    def _network_info(self) -> dict:
        """Return network info for the overview panel.

        If the (one-time) detection has not run yet, returns a placeholder
        and kicks the probes off the UI thread; the labels update via
        ``after()`` when the result arrives.
        """
        if DashboardWindow._network_info_cache is not None:
            return DashboardWindow._network_info_cache
        if not DashboardWindow._network_detect_started:
            DashboardWindow._network_detect_started = True
            threading.Thread(
                target=self._network_detect_worker, daemon=True,
                name="network-detect",
            ).start()
        return {"type": "unknown", "label": T("network.detecting"), "detail": ""}

    def _network_detect_worker(self):
        try:
            result = DashboardWindow._detect_network_info_uncached()
        except Exception:
            result = {"type": "unknown", "label": T("network.lan"), "detail": ""}
        DashboardWindow._network_info_cache = result
        try:
            self._root.after(0, self._apply_network_info)
        except Exception:
            pass

    def _apply_network_info(self):
        info = DashboardWindow._network_info_cache or {}
        for attr in ("_net_label", "_net_detail_label"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            try:
                if widget.winfo_exists():
                    text = info.get("label" if attr == "_net_label" else "detail", "")
                    widget.configure(text=text)
            except Exception:
                pass

    @staticmethod
    def _detect_network_info_uncached() -> dict:
        """Detect current network: WiFi (SSID + signal) or Ethernet. Returns
        {'type': 'wifi'|'ethernet'|'unknown', 'label': str, 'detail': str}."""
        import re
        import subprocess

        # Try WiFi detection via netsh (Windows)
        try:
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
            )
            ssid_match = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE | re.IGNORECASE)
            signal_match = re.search(r"^\s*Signal\s*:\s*(\d+)%", out, re.MULTILINE | re.IGNORECASE)
            state_match = re.search(r"^\s*State\s*:\s*connected", out, re.MULTILINE | re.IGNORECASE)
            if ssid_match and state_match:
                ssid = ssid_match.group(1).strip()
                signal = int(signal_match.group(1)) if signal_match else 0
                bars = "▂▄▆█" if signal >= 75 else "▂▄▆ " if signal >= 50 else "▂▄  " if signal >= 25 else "▂   "
                return {
                    "type": "wifi",
                    "label": T("network.wifi_label", ssid=ssid),
                    "detail": T("network.signal_detail", percent=str(signal), bars=bars),
                }
        except Exception:
            pass

        # Try Ethernet detection (Windows)
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
                 "Where-Object MediaType -eq '802.3' | Select-Object -First 1 "
                 "-ExpandProperty LinkSpeed"],
                text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
            )
            out = out.strip()
            if out:
                # Convert to human-readable: e.g. "1000000000" → "1 Gbps"
                speed_bps = int(out)
                if speed_bps >= 1_000_000_000:
                    speed_str = f"{speed_bps / 1_000_000_000:.0f} Gbps"
                elif speed_bps >= 1_000_000:
                    speed_str = f"{speed_bps / 1_000_000:.0f} Mbps"
                else:
                    speed_str = f"{speed_bps / 1_000:.0f} Kbps"
                return {
                    "type": "ethernet",
                    "label": T("network.ethernet"),
                    "detail": T("network.link_speed", speed=speed_str),
                }
        except Exception:
            pass

        return {"type": "unknown", "label": T("network.lan"), "detail": ""}

    def _build_overview_panel(self):
        wrapper = ctk.CTkScrollableFrame(self._content_frame, fg_color="transparent")
        panel = ctk.CTkFrame(wrapper, fg_color="transparent")
        panel.pack(fill="both", expand=True)
        cfg = self._get_config()
        net = self._network_info()
        local_ip = self._detect_local_ip()

        ctk.CTkLabel(
            panel, text=T("overview.title"),
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", pady=(0, 10))

        # ── Top row: 3 equal-height columns ────────────────────────────
        top = ctk.CTkFrame(panel, fg_color="transparent")
        top.pack(fill="x")
        top.rowconfigure(0, weight=1)
        for c in range(3):
            top.columnconfigure(c, weight=1, uniform="top3")

        # ── Col 0: Connection ──────────────────────────────────────────
        card_s = ctk.CTkFrame(top, corner_radius=14)
        card_s.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 8))
        title_row = ctk.CTkFrame(card_s, fg_color="transparent")
        title_row.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(title_row, text=T("network.connection"),
                    font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")
        self._start_time = time.time()
        self._uptime_label = ctk.CTkLabel(
            title_row, text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray55", "gray55"),
        )
        self._uptime_label.pack(side="right")
        s_center = ctk.CTkFrame(card_s, fg_color="transparent")
        s_center.pack(fill="x", padx=16)

        # Sync status
        sr = ctk.CTkFrame(s_center, fg_color="transparent")
        sr.pack(fill="x")
        self._status_dot = ctk.CTkFrame(sr, width=14, height=14,
                                        corner_radius=7, fg_color=STATUS_COLOR)
        self._status_dot.pack(side="left", padx=(0, 8))
        self._status_label = ctk.CTkLabel(
            sr, text=T("ui.sync_active"), font=ctk.CTkFont(size=16, weight="bold"),
            text_color=("gray20", "gray85"),
        )
        self._status_label.pack(side="left")

        # Network info
        self._net_label = ctk.CTkLabel(
            s_center, text=net["label"],
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("gray50", "gray60"),
        )
        self._net_label.pack(anchor="w", pady=(10, 0))
        if net["detail"]:
            self._net_detail_label = ctk.CTkLabel(
                s_center, text=net["detail"],
                font=ctk.CTkFont(size=12),
                text_color=("gray50", "gray60"),
            )
            self._net_detail_label.pack(anchor="w", pady=(2, 0))

        # Local address
        self._local_ip_label = ctk.CTkLabel(
            s_center, text=f"{T('network.local_address')}  {local_ip}:{cfg.port}",
            font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
        )
        self._local_ip_label.pack(anchor="w", pady=(8, 0))

        # ── Col 1: This Device ─────────────────────────────────────────
        card_d = ctk.CTkFrame(top, corner_radius=14)
        card_d.grid(row=0, column=1, sticky="nsew", padx=5, pady=(0, 8))
        ctk.CTkLabel(card_d, text=T("overview.this_device"),
                    font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(12, 6))
        d_grid = ctk.CTkFrame(card_d, fg_color="transparent")
        d_grid.pack(fill="x", padx=16, pady=(0, 10))

        # Name row (editable)
        nr = ctk.CTkFrame(d_grid, fg_color="transparent")
        nr.pack(fill="x", pady=2)
        ctk.CTkLabel(nr, text=T("device_info.name"), width=72, anchor="w",
                    font=ctk.CTkFont(size=12)).pack(side="left")
        self._overview_device_name = ctk.CTkLabel(
            nr, text=cfg.device_name, font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
        )
        self._overview_device_name.pack(side="left")
        # Edit button anchored right so long names don't push it off-screen
        ctk.CTkButton(
            nr, text="✎", width=22, height=22,
            fg_color="transparent", border_width=0,
            text_color=("gray55", "gray55"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=10),
            command=self._edit_device_name,
        ).pack(side="right")

        for label, value in [
            (T("device_info.id"), cfg.device_id),
            (T("device_info.platform"), platform.system() + " " + platform.machine()),
            (T("device_info.service"), cfg.service_type.replace("_clipsync._tcp.local.", "clipsync")),
        ]:
            row = ctk.CTkFrame(d_grid, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=72, anchor="w",
                        font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkLabel(row, text=value, font=ctk.CTkFont(size=12),
                        text_color=("gray50", "gray60")).pack(side="left")

        # ── Col 2: Quick Controls ──────────────────────────────────────
        card_c = ctk.CTkFrame(top, corner_radius=14)
        card_c.grid(row=0, column=2, sticky="nsew", padx=(5, 0), pady=(0, 8))
        ctk.CTkLabel(card_c, text=T("overview.settings"),
                    font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(12, 6))
        c_center = ctk.CTkFrame(card_c, fg_color="transparent")
        c_center.pack(fill="x", padx=16)

        self._sync_var = tk.BooleanVar(value=self._get_sync())
        ctk.CTkSwitch(
            c_center, text=T("ui.clipboard_sync"), variable=self._sync_var,
            command=self._on_toggle_sync,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(0, 6))

        self._autostart_var = tk.BooleanVar(value=cfg.auto_start)
        ctk.CTkSwitch(
            c_center, text=T("ui.start_at_login"), variable=self._autostart_var,
            command=self._on_toggle_autostart,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(0, 6))

        # ── Discovery & visibility toggles ───────────────────
        discovering = self._get_discovering() if self._get_discovering else True
        self._discovery_var = tk.BooleanVar(value=discovering)
        ctk.CTkSwitch(
            c_center, text=T("ui.stop_discovery"), variable=self._discovery_var,
            command=self._on_toggle_discovery,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(0, 6))

        visible = self._get_visible() if self._get_visible else True
        self._visibility_var = tk.BooleanVar(value=visible)
        ctk.CTkSwitch(
            c_center, text=T("ui.hide_self"), variable=self._visibility_var,
            command=self._on_toggle_visibility,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(0, 6))

        self._web_switch_var = tk.BooleanVar(value=cfg.web_enabled)
        self._web_switch = ctk.CTkSwitch(
            c_center, text=T("settings_window.web_companion_title"),
            variable=self._web_switch_var,
            command=self._on_web_toggle,
            font=ctk.CTkFont(size=12),
        )
        self._web_switch.pack(anchor="w", pady=(0, 6))

        # ── Web Companion card (shown when enabled, above Activity) ──
        web_card = ctk.CTkFrame(panel, corner_radius=14)
        web_card.pack(fill="x", pady=(8, 10))
        self._web_card = web_card

        web_inner = ctk.CTkFrame(web_card, fg_color="transparent")
        web_inner.pack(fill="x", padx=16, pady=(14, 14))

        # Left: QR code
        qr_frame = ctk.CTkFrame(web_inner, corner_radius=8,
                                fg_color=("gray95", "gray17"),
                                width=120, height=120)
        qr_frame.pack(side="left", padx=(0, 16))
        qr_frame.pack_propagate(False)
        self._web_qr_label = ctk.CTkLabel(qr_frame, text="")
        self._web_qr_label.place(relx=0.5, rely=0.5, anchor="center")

        # Right: URL + copy
        right = ctk.CTkFrame(web_inner, fg_color="transparent")
        right.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(right, text=T("web.qr_title"),
                    font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(0, 2))

        ctk.CTkLabel(right, text=T("settings_window.web_qr_hint"),
                    font=ctk.CTkFont(size=11),
                    text_color=("gray55", "gray55"),
        ).pack(anchor="w", pady=(0, 8))

        url_row = ctk.CTkFrame(right, corner_radius=6,
                               fg_color=("gray90", "gray17"))
        url_row.pack(fill="x")
        self._web_url_label = ctk.CTkLabel(
            url_row, text="", font=ctk.CTkFont(size=11, family="monospace"),
            text_color=("gray50", "gray70"),
        )
        self._web_url_label.pack(side="left", padx=(10, 6), pady=8)

        self._web_copy_btn = ctk.CTkButton(
            url_row, text=T("ui.copy"), width=54, height=28,
            font=ctk.CTkFont(size=11),
            command=self._on_web_copy_url,
        )
        self._web_copy_btn.pack(side="right", padx=(0, 6))

        # Initial state: show if web enabled
        if not cfg.web_enabled:
            web_card.pack_forget()

        # ── Bottom: Activity ───────────────────────────────────────────
        card_a = ctk.CTkFrame(panel, corner_radius=14)
        card_a.pack(fill="both", expand=True)
        self._activity_card = card_a
        top_bar = ctk.CTkFrame(card_a, fg_color="transparent")
        top_bar.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(top_bar, text=T("overview.activity"),
                    font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")
        self._recent_activity = ctk.CTkLabel(
            top_bar, text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray55", "gray55"),
        )
        self._recent_activity.pack(side="right")

        stat_grid = ctk.CTkFrame(card_a, fg_color="transparent")
        stat_grid.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for r in range(2):
            stat_grid.rowconfigure(r, weight=1, uniform="stat_row")
        for c in range(3):
            stat_grid.columnconfigure(c, weight=1, uniform="stat_col")

        accent_colors = ["#2ECC71", "#F39C12", "#3498DB", "#9B59B6", "#1ABC9C", "#E67E22"]
        stat_icons = ["●", "◉", "◷", "⇄", "⌘", "◈"]
        stats_def = [
            (T("stats.connected"), "--", T("stats.online"), "_stat_peers", "_sub_peers"),
            (T("stats.paired"), "--", T("stats.trusted"), "_stat_paired", "_sub_paired"),
            (T("stats.history"), "--", T("stats.items_saved"), "_stat_history", "_sub_history"),
            (T("stats.transfers"), "--", T("stats.active"), "_stat_transfers", "_sub_transfers"),
            (T("stats.discovery"), "--", T("stats.browsing"), "_stat_discovery", "_sub_discovery"),
            (T("stats.visibility"), "--", T("stats.advertising"), "_stat_visibility", "_sub_visibility"),
        ]
        for i, (title, default, subtitle, ref_name, sub_ref) in enumerate(stats_def):
            row = i // 3
            col = i % 3
            box = ctk.CTkFrame(stat_grid, corner_radius=10,
                             fg_color=("gray95", "gray17"))
            box.grid(row=row, column=col, sticky="nsew", padx=3, pady=3)
            # Accent top bar instead of left bar — more modern
            accent = ctk.CTkFrame(box, height=3, fg_color=accent_colors[i])
            accent.pack(fill="x", side="top")
            content = ctk.CTkFrame(box, fg_color="transparent")
            content.pack(fill="both", expand=True, padx=10, pady=(8, 10))
            # Icon + title row
            hdr = ctk.CTkFrame(content, fg_color="transparent")
            hdr.pack(fill="x")
            ctk.CTkLabel(hdr, text=stat_icons[i],
                        font=ctk.CTkFont(size=13),
                        text_color=accent_colors[i],
            ).pack(side="left", padx=(0, 6))
            ctk.CTkLabel(hdr, text=title,
                        font=ctk.CTkFont(size=11),
                        text_color=("gray55", "gray65"),
            ).pack(side="left")
            val = ctk.CTkLabel(content, text=default,
                              font=ctk.CTkFont(size=24, weight="bold"),
                              text_color=("gray20", "gray85"))
            val.pack(anchor="w", pady=(6, 1))
            sub = ctk.CTkLabel(content, text=subtitle,
                              font=ctk.CTkFont(size=11),
                              text_color=("gray55", "gray55"))
            sub.pack(anchor="w")

            if ref_name == "_stat_peers":
                self._stat_peers = val
                self._sub_peers = sub
            elif ref_name == "_stat_paired":
                self._stat_paired = val
                self._sub_paired = sub
            elif ref_name == "_stat_history":
                self._stat_history = val
                self._sub_history = sub
            elif ref_name == "_stat_transfers":
                self._stat_transfers = val
                self._sub_transfers = sub
            elif ref_name == "_stat_discovery":
                self._stat_discovery = val
                self._sub_discovery = sub
            elif ref_name == "_stat_visibility":
                self._stat_visibility = val
                self._sub_visibility = sub

        return wrapper

    def _refresh_overview(self):
        if self._status_dot is None:
            return

        syncing = self._get_sync()
        # Reconcile the quick-controls switch with the real sync state so a
        # toggle from the tray/hotkey (which bypasses _on_toggle_sync) is
        # reflected here too.
        if self._sync_var is not None and self._sync_var.get() != syncing:
            self._sync_var.set(syncing)
        if syncing:
            self._status_label.configure(text=T("ui.sync_active"))
            if not getattr(self, '_breathing', False):
                self._breathing = True
                self._breath_frame = 0
                self._animate_breath()
        else:
            self._breathing = False
            self._status_dot.configure(fg_color=OFFLINE_COLOR)
            self._status_label.configure(text=T("ui.sync_paused"))

        # Stat cards / web card are refreshed on the slow periodic refresh,
        # never from the 5fps breath animation.
        self._refresh_overview_stats()

    def _animate_breath(self):
        """Gentle breathing-light animation for the sync status dot.

        Visual only — heavy stat/web-card refreshes live in
        _refresh_overview_stats (driven by the slow periodic refresh) so this
        5fps loop never does getaddrinfo, QR generation, or data queries.
        """
        if not getattr(self, '_breathing', False) or self._status_dot is None:
            return
        import math
        self._breath_frame += 1
        # Slow sine wave: ~4 second period at 200ms interval
        t = self._breath_frame * 0.08
        brightness = (math.sin(t) + 1) / 2  # 0.0 → 1.0
        # Fixed size, only animate color brightness gently
        # Bright green #2ECC71 (46,204,113) ↔ dim #1a7a3a (26,122,58)
        r = int(26 + brightness * 20)
        g = int(122 + brightness * 82)
        b = int(58 + brightness * 55)
        color = f"#{r:02x}{g:02x}{b:02x}"
        self._status_dot.configure(fg_color=color)
        self._breath_timer = self._root.after(200, self._animate_breath)

    def _refresh_overview_stats(self):
        """Update the overview stat cards, recent activity, and web card.

        Called from the slow periodic refresh (_schedule_refresh), never from
        the 5fps breath animation.
        """
        # Uptime
        if self._uptime_label and self._start_time:
            uptime = int(time.time() - self._start_time)
            if uptime < 120:
                self._uptime_label.configure(text=f"up {uptime}s")
            elif uptime < 7200:
                self._uptime_label.configure(text=f"up {uptime // 60}m")
            else:
                self._uptime_label.configure(text=f"up {uptime // 3600}h {uptime % 3600 // 60:02d}m")

        # Peer stats
        try:
            peers = self._get_peers()
            connected = sum(1 for _, _, _, c, _ in peers if c)
            paired = sum(1 for _, _, p, _, _ in peers if p)

            if self._stat_peers:
                self._stat_peers.configure(text=str(connected))
            if self._sub_peers:
                self._sub_peers.configure(text=T("stats.visible", count=len(peers)))
            if self._stat_paired:
                self._stat_paired.configure(text=str(paired))
            if self._sub_paired:
                self._sub_paired.configure(text=T("stats.trusted"))
        except Exception:
            if self._stat_peers:
                self._stat_peers.configure(text="--")
            if self._sub_peers:
                self._sub_peers.configure(text="")
            if self._stat_paired:
                self._stat_paired.configure(text="--")
            if self._sub_paired:
                self._sub_paired.configure(text="")

        if self._stat_history:
            try:
                history = self._get_history() if self._get_history else []
                count = len(history)
                self._stat_history.configure(text=str(count))
                if self._sub_history:
                    self._sub_history.configure(text=T("stats.items_saved"))
            except Exception:
                self._stat_history.configure(text="--")
                if self._sub_history:
                    self._sub_history.configure(text="")

        if self._stat_transfers:
            try:
                transfers = self._get_transfers() if self._get_transfers else []
                count = len(transfers)
                self._stat_transfers.configure(text=str(count) if count > 0 else "--")
                if self._sub_transfers:
                    if count > 0:
                        self._sub_transfers.configure(text=T("stats.active"))
                    else:
                        self._sub_transfers.configure(text=T("stats.none_active"))
            except Exception:
                self._stat_transfers.configure(text="--")
                if self._sub_transfers:
                    self._sub_transfers.configure(text="")

        # Discovery / visibility status
        if self._stat_discovery:
            browsing = self._get_discovering() if self._get_discovering else False
            self._stat_discovery.configure(
                text=T("stats.on") if browsing else T("stats.off"),
                text_color=("#27AE60", "#2ECC71") if browsing else ("gray50", "gray60"),
            )
            if self._sub_discovery:
                self._sub_discovery.configure(
                    text=T("stats.browsing") if browsing else T("stats.stopped"))
        if self._stat_visibility:
            visible = self._get_visible() if self._get_visible else False
            self._stat_visibility.configure(
                text=T("stats.on") if visible else T("stats.off"),
                text_color=("#27AE60", "#2ECC71") if visible else ("gray50", "gray60"),
            )
            if self._sub_visibility:
                self._sub_visibility.configure(
                    text=T("stats.advertising") if visible else T("stats.hidden"))

        # Recent activity summary
        if self._recent_activity:
            try:
                parts = []
                history = self._get_history() if self._get_history else []
                if history:
                    parts.append(T("activity.clips", count=len(history)))
                transfers = self._get_transfers() if self._get_transfers else []
                if transfers:
                    parts.append(T("activity.transfers", count=len(transfers)))
                if parts:
                    self._recent_activity.configure(text="  ·  ".join(parts))
                else:
                    self._recent_activity.configure(text=T("activity.no_recent"))
            except Exception:
                self._recent_activity.configure(text="")

        self._refresh_web_card()

    # ═══════════════════════════════════════════════════════════════
    # Panel: Devices (with pairing)
    # ═══════════════════════════════════════════════════════════════

    def _build_devices_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")

        ctk.CTkLabel(
            panel, text=T("devices.title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            panel, text=T("devices.subtitle"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(anchor="w", pady=(0, 14))

        # Device list card
        card = ctk.CTkFrame(panel, corner_radius=12)
        card.pack(fill="both", expand=True, pady=(0, 10))

        self._device_scroll = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self._device_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # Section labels (created once, hidden/shown in _refresh_devices)
        self._known_header: ctk.CTkLabel | None = None
        self._discovered_header: ctk.CTkLabel | None = None

        # Pairing section
        card2 = ctk.CTkFrame(panel, corner_radius=12)
        card2.pack(fill="x")

        ctk.CTkLabel(
            card2, text=T("devices.pairing_requests"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 4))

        instr = ctk.CTkLabel(
            card2,
            text=T("devices.pairing_instructions"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
            justify="left",
        )
        instr.pack(anchor="w", padx=16, pady=(0, 8))

        self._pending_frame = ctk.CTkFrame(card2, fg_color="transparent")
        self._pending_frame.pack(fill="x", padx=16, pady=(0, 12))

        return panel

    def _refresh_devices(self):
        if self._device_scroll is None:
            return
        try:
            peers = self._get_peers()
        except Exception:
            peers = []

        # Hash-based change detection: skip rebuild if peer data hasn't changed.
        # Include the pending-pairing list so a brand-new pairing request for
        # an already-known (unpaired) peer still shows its Confirm/Reject card
        # even though the peer list itself is unchanged.
        pending = self._get_pending() if self._get_pending else []
        state_key = (
            tuple(sorted((p[0], p[1], p[2], p[3], p[4]) for p in peers)),
            tuple(sorted(pending)),
        )
        if state_key == getattr(self, '_devices_state_key', None):
            return
        self._devices_state_key = state_key

        for child in self._device_scroll.winfo_children():
            child.destroy()

        if not peers:
            ctk.CTkLabel(
                self._device_scroll,
                text="\U0001F4E1  " + T("empty.no_devices"),
                font=ctk.CTkFont(size=12),
                text_color=("gray50", "gray60"),
                justify="center",
            ).pack(fill="x", expand=True, pady=40)
        else:
            known = [(i, d, p, c, n) for i, d, p, c, n in peers if p or c]
            discovered = [(i, d, p, c, n) for i, d, p, c, n in peers if not p and not c]

            if known:
                self._add_section_header(T("devices.known"), len(known))
                for dev_id, dev_name, paired, connected, notes in known:
                    self._create_device_row(dev_id, dev_name, paired, connected, notes)

            if discovered:
                self._add_section_header(T("devices.discovered_section"), len(discovered))
                for dev_id, dev_name, paired, connected, notes in discovered:
                    self._create_device_row(dev_id, dev_name, paired, connected, notes)

        # Pending pairing codes — per-device confirm / reject buttons
        if self._pending_frame is not None:
            for child in self._pending_frame.winfo_children():
                child.destroy()
            pending = self._get_pending() if self._get_pending else []
            if pending:
                for peer_id, code, peer_name, status in pending:
                    self._create_pending_row(peer_id, code, peer_name, status)
            else:
                empty = ctk.CTkLabel(
                    self._pending_frame, text="\U0001F4E8  " + T("empty.no_pending"),
                    font=ctk.CTkFont(size=11),
                    text_color=("gray40", "gray60"),
                )
                empty.pack(anchor="w")

    def _add_section_header(self, text: str, count: int):
        header = ctk.CTkFrame(self._device_scroll, fg_color="transparent")
        header.pack(fill="x", pady=(8, 2), padx=0)
        ctk.CTkLabel(
            header, text=text,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("gray40", "gray70"),
        ).pack(side="left")
        ctk.CTkLabel(
            header, text=str(count),
            font=ctk.CTkFont(size=11),
            text_color=("gray60", "gray50"),
        ).pack(side="left", padx=(6, 0))

    def _create_device_row(self, dev_id, dev_name, paired, connected, notes=""):
        if connected and paired:
            color, status = STATUS_COLOR, T("device.connected")
        elif connected:
            color, status = PAIRING_COLOR, T("device.connecting")
        elif paired:
            color, status = WARN_COLOR, T("device.paired_offline")
        else:
            color, status = ACCENT, T("device.discovered")

        if connected:
            chip_text = T("devices.status_connected")
        elif paired:
            chip_text = T("devices.status_paired_offline")
        else:
            chip_text = T("devices.status_discovered")

        display_name = dev_name or dev_id[:12]
        display_id = dev_id[:12] if dev_name else dev_id[:16]

        row = ctk.CTkFrame(self._device_scroll, fg_color=("gray95", "gray17"),
                          corner_radius=8)
        row.pack(fill="x", pady=2, padx=2)

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        # ── Row 1: dot + name + device ID + buttons ─────────────
        r1 = ctk.CTkFrame(inner, fg_color="transparent")
        r1.pack(fill="x")

        dot = ctk.CTkFrame(r1, width=12, height=12, corner_radius=6,
                          fg_color=color)
        dot.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            r1, text=display_name,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        # Status chip — muted, next to the device name
        chip = ctk.CTkFrame(r1, fg_color=("gray90", "gray22"), corner_radius=10)
        chip.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            chip, text=chip_text,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("gray50", "gray60"),
        ).pack(padx=8, pady=2)

        # Action buttons — right side of row 1
        ctk.CTkButton(
            r1, text=T("ui.forget"), width=56, height=22,
            fg_color="transparent", border_width=1,
            text_color=("#E74C3C", "#C0392B"),
            border_color=("#E74C3C", "#C0392B"),
            hover_color=("#FADBD8", "#5B2C2C"),
            font=ctk.CTkFont(size=10),
            command=lambda d=dev_id: self._do_remove(d),
        ).pack(side="right", padx=(4, 0))

        if paired:
            ctk.CTkButton(
                r1, text=T("ui.unpair"), width=56, height=22,
                fg_color="transparent", border_width=1,
                text_color=WARN_COLOR,
                border_color=WARN_COLOR,
                hover_color=("#FDEBD0", "#7D5A0B"),
                font=ctk.CTkFont(size=10),
                command=lambda d=dev_id: self._do_unpair(d),
            ).pack(side="right", padx=(4, 0))

        if connected and self._on_disconnect_peer:
            ctk.CTkButton(
                r1, text=T("ui.disconnect"), width=72, height=22,
                fg_color="transparent", border_width=1,
                text_color=("#E67E22", "#F0A04B"),
                border_color=("#E67E22", "#F0A04B"),
                hover_color=("#FDEBD0", "#5B3A1C"),
                font=ctk.CTkFont(size=10),
                command=lambda d=dev_id: self._do_disconnect(d),
            ).pack(side="right", padx=(4, 0))

        if paired and not connected and self._on_connect_peer:
            ctk.CTkButton(
                r1, text=T("ui.reconnect"), width=68, height=22,
                fg_color=ACCENT,
                hover_color=("#2980B9", "#2471A3"),
                font=ctk.CTkFont(size=10),
                command=lambda d=dev_id: self._do_reconnect(d),
            ).pack(side="right", padx=(4, 0))

        if not connected and not paired and self._on_connect_peer:
            ctk.CTkButton(
                r1, text=T("ui.connect"), width=60, height=22,
                fg_color=ACCENT,
                hover_color=("#2980B9", "#2471A3"),
                font=ctk.CTkFont(size=10),
                command=lambda d=dev_id: self._do_connect(d),
            ).pack(side="right", padx=(4, 0))

        ctk.CTkLabel(
            r1, text=display_id,
            font=ctk.CTkFont(size=10),
            text_color=("gray60", "gray50"),
        ).pack(side="right", padx=(8, 8))

        # ── Row 2: status (left) + notes (right) ─────────────────
        detail = status
        if connected:
            detail += "  \U0001F512  " + T("device.encrypted")
        elif paired:
            detail += T("device.reconnect_to_sync")
        else:
            detail += T("device.connect_to_sync")

        r2 = ctk.CTkFrame(inner, fg_color="transparent")
        r2.pack(fill="x", pady=(2, 0))

        ctk.CTkLabel(
            r2, text=detail,
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(side="left")

        if paired:
            note_text = notes if notes else T("device.add_note")
            note_color = ("gray50", "gray60") if notes else ("gray65", "gray55")
            note_label = ctk.CTkLabel(
                r2, text=note_text,
                font=ctk.CTkFont(size=11),
                text_color=note_color,
            )
            note_label.pack(side="right", padx=(0, 4))
            ctk.CTkButton(
                r2, text="✎", width=20, height=20,
                fg_color="transparent", border_width=0,
                text_color=("gray55", "gray55"),
                hover_color=("gray85", "gray25"),
                font=ctk.CTkFont(size=9),
                command=lambda d=dev_id, nl=note_label: self._do_edit_note(d, nl),
            ).pack(side="right")


    # ── Device actions ────────────────────────────────────────────

    def _do_connect(self, peer_id):
        logger.info("User initiated connection to %s", peer_id)
        if self._on_connect_peer:
            self._on_connect_peer(peer_id)

    def _do_reconnect(self, peer_id):
        logger.info("User initiated reconnect to %s", peer_id)
        if self._on_connect_peer:
            self._on_connect_peer(peer_id)

    def _do_disconnect(self, peer_id):
        logger.info("User initiated disconnect from %s", peer_id)
        if self._on_disconnect_peer:
            self._on_disconnect_peer(peer_id)

    def _do_unpair(self, peer_id):
        # Look up device name for the confirmation dialog
        device_name = peer_id[:12]
        try:
            for dev_id, dev_name, _, _, _ in self._get_peers():
                if dev_id == peer_id:
                    if dev_name:
                        device_name = dev_name
                    break
        except Exception:
            pass
        if ask_yesno(self._window, T("devices.unpair_title"), T("devices.unpair_message", name=device_name)):
            self._on_unpair(peer_id)
            self._refresh_devices()

    def _do_remove(self, peer_id):
        # Look up device name for the confirmation dialog
        device_name = peer_id[:12]
        try:
            for dev_id, dev_name, _, _, _ in self._get_peers():
                if dev_id == peer_id:
                    if dev_name:
                        device_name = dev_name
                    break
        except Exception:
            pass
        if ask_yesno(self._window, T("devices.forget_title"), T("devices.forget_message", name=device_name)):
            self._on_remove_peer(peer_id)
            self._refresh_devices()

    def _do_edit_note(self, peer_id, note_label):
        old_note = ""
        try:
            for dev_id, _, _, _, notes in self._get_peers():
                if dev_id == peer_id:
                    old_note = notes
                    break
        except Exception:
            pass
        new_note = ask_string(self._window, T("device.note_title"),
                              T("device.note_prompt"), initial_value=old_note)
        if new_note is None:
            return  # cancelled
        new_note = new_note.strip()
        if self._on_edit_note:
            self._on_edit_note(peer_id, new_note)
        # Update label in-place
        note_label.configure(
            text=new_note if new_note else T("device.add_note"),
            text_color=("gray50", "gray60") if new_note else ("gray65", "gray55"),
        )

    def _do_speed_test(self):
        if not self._on_speed_test:
            return
        # Show progress bar, hide hint & result
        if self._speed_hint:
            self._speed_hint.pack_forget()
        if self._speed_result_row:
            self._speed_result_row.pack_forget()
        if self._speed_progress:
            self._speed_progress.pack(fill="x", padx=14, pady=(4, 0))
            self._speed_progress.set(0)
        if self._speed_status:
            self._speed_status.configure(text=T("transfer.speed_test.running"))
            self._speed_status.pack(anchor="w", padx=14, pady=(2, 0))
        self._on_speed_test()

    def _create_pending_row(self, peer_id: str, code: str, peer_name: str,
                            status: str = "pending"):
        row = ctk.CTkFrame(self._pending_frame, fg_color=("gray90", "gray20"),
                          corner_radius=8)
        row.pack(fill="x", pady=2)

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)

        # Name + status/buttons in one row
        name_row = ctk.CTkFrame(inner, fg_color="transparent")
        name_row.pack(fill="x")

        ctk.CTkLabel(
            name_row, text=peer_name,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        if status == "confirmed_waiting":
            # We confirmed locally; the peer hasn't yet.  Show the waiting
            # state instead of the Confirm button (Reject still cancels).
            ctk.CTkButton(
                name_row, text=T("ui.reject"), width=56, height=24,
                fg_color="transparent", border_width=1,
                text_color=("#E74C3C", "#C0392B"),
                border_color=("#E74C3C", "#C0392B"),
                hover_color=("#FADBD8", "#5B2C2C"),
                font=ctk.CTkFont(size=11),
                command=lambda pid=peer_id: self._on_reject_pairing(pid),
            ).pack(side="right", padx=(4, 0))
            ctk.CTkLabel(
                name_row, text="⏳  " + T("pairing.state.confirmed_waiting"),
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("#B7950B", "#F1C40F"),
            ).pack(side="right", padx=(8, 0))
        else:
            ctk.CTkButton(
                name_row, text=T("ui.reject"), width=56, height=24,
                fg_color="transparent", border_width=1,
                text_color=("#E74C3C", "#C0392B"),
                border_color=("#E74C3C", "#C0392B"),
                hover_color=("#FADBD8", "#5B2C2C"),
                font=ctk.CTkFont(size=11),
                command=lambda pid=peer_id: self._on_reject_pairing(pid),
            ).pack(side="right", padx=(4, 0))
            ctk.CTkButton(
                name_row, text=T("ui.confirm"), width=72, height=24,
                fg_color=STATUS_COLOR,
                hover_color=("#27AE60", "#1E8449"),
                font=ctk.CTkFont(size=11),
                command=lambda pid=peer_id, c=code: self._on_confirm_pairing(pid, c),
            ).pack(side="right", padx=(4, 0))

        code_frame = ctk.CTkFrame(inner, fg_color=("#D6EAF8", "#1A3A4A"),
                                  corner_radius=6)
        code_frame.pack(anchor="w", pady=(4, 0))
        ctk.CTkLabel(
            code_frame, text=T("ui.pairing_code", code=code),
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=ACCENT,
        ).pack(padx=12, pady=6)

        # Status / guidance line: what the user should do next.
        if status == "confirmed_waiting":
            hint = T("pairing.state.confirmed_waiting")
        elif status == "peer_confirmed":
            hint = "✅  " + T("pairing.state.peer_confirmed")
        else:
            hint = T("pairing.guidance")
        ctk.CTkLabel(
            inner, text=hint,
            font=ctk.CTkFont(size=10),
            text_color=("gray55", "gray55"),
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _on_confirm_pairing(self, peer_id: str, code: str):
        if not self._on_pair:
            return
        if not ask_yesno(self._window, T("pairing.verify_title"),
                         T("pairing.verify_message", code=code)):
            return  # user declined the code check — do not pair
        success = self._on_pair(peer_id, code)
        if success:
            # Look up the peer's display name from the pending list.
            device_name = peer_id
            try:
                if self._get_pending:
                    for pid, _code, pname, _status in self._get_pending():
                        if pid == peer_id and pname:
                            device_name = pname
                            break
            except Exception:
                pass
            if self._status_footer:
                self._status_footer.configure(text=T("footer.paired"))
            show_info(self._window, T("dialog.paired"),
                      T("pairing.accepted", name=device_name))
        else:
            show_error(
                self._window,
                T("dialog.failed"),
                T("notify.pairing_failed"),
            )
        self._refresh_devices()

    def _on_reject_pairing(self, peer_id: str):
        if self._on_unpair:
            self._on_unpair(peer_id)
        self._refresh_devices()

    # ═══════════════════════════════════════════════════════════════
    # Panel: History
    # ═══════════════════════════════════════════════════════════════

    def _build_history_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")

        header_row = ctk.CTkFrame(panel, fg_color="transparent")
        header_row.pack(fill="x", pady=(4, 12))

        ctk.CTkLabel(
            header_row, text=T("history.title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            header_row, text=T("ui.refresh"), width=80, height=28,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray60"),
            border_color=("gray55", "gray45"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=11),
            command=self._refresh_history_list,
        ).pack(side="right", padx=(0, 6))

        if self._clear_history:
            self._clear_history_btn = ctk.CTkButton(
                header_row, text=T("ui.clear_all"), width=80, height=28,
                fg_color="transparent", border_width=1,
                text_color=("#E74C3C", "#C0392B"),
                border_color=("#E74C3C", "#C0392B"),
                hover_color=("#FADBD8", "#5B2C2C"),
                font=ctk.CTkFont(size=11),
                state="disabled",
                command=self._on_clear_history,
            )
            self._clear_history_btn.pack(side="right")

        # Search bar
        search_frame = ctk.CTkFrame(panel, corner_radius=8, fg_color=("gray95", "gray17"))
        search_frame.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            search_frame, text="🔍",
            font=ctk.CTkFont(size=14),
        ).pack(side="left", padx=(12, 0), pady=4)

        self._history_search_var = tk.StringVar()
        self._history_search_timer: str | None = None
        self._history_chunk_timer: str | None = None
        self._copy_url_timer: str | None = None
        search_entry = ctk.CTkEntry(
            search_frame, textvariable=self._history_search_var,
            height=32, placeholder_text=T("ui.search"),
            fg_color="transparent",
        )
        search_entry.pack(side="left", fill="x", expand=True, padx=(4, 4), pady=8)
        search_entry.bind("<KeyRelease>", self._on_search_keyrelease)

        ctk.CTkButton(
            search_frame, text="✕", width=24, height=24,
            fg_color="transparent",
            text_color=("gray50", "gray60"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=12),
            command=self._on_clear_search,
        ).pack(side="right", padx=(0, 8), pady=4)

        # History list
        card = ctk.CTkFrame(panel, corner_radius=12)
        card.pack(fill="both", expand=True)

        self._history_scroll = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self._history_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        return panel

    def _refresh_history_list(self):
        if self._history_scroll is None or self._get_history is None:
            return

        query = (self._history_search_var.get().strip()
                 if self._history_search_var else "")

        self._history_entries = (self._search_history(query)
                                 if query and self._search_history
                                 else self._get_history())

        # ── Clear ────────────────────────────────────────────────
        for child in self._history_scroll.winfo_children():
            child.destroy()

        if self._clear_history_btn is not None:
            if not self._history_entries:
                self._clear_history_btn.configure(state="disabled")
            else:
                self._clear_history_btn.configure(state="normal")

        if not self._history_entries:
            if query:
                empty_text = "\U0001F50D  " + T("empty.no_results", query=query)
            else:
                empty_text = "\U0001F4CB  " + T("empty.no_history")
            ctk.CTkLabel(
                self._history_scroll,
                text=empty_text,
                font=ctk.CTkFont(size=12),
                text_color=("gray50", "gray60"),
                justify="center",
            ).pack(fill="x", expand=True, pady=40)
            return

        # ── Build peer map once ──────────────────────────────────
        self._history_peer_map = {}
        for peer in self._get_peers():
            self._history_peer_map[peer[0]] = peer[1]

        # ── Show first batch ─────────────────────────────────────
        self._history_shown = 0
        self._show_history_batch()

    _BATCH_SIZE = 20
    _CHUNK = 2  # cards per idle cycle — keeps UI responsive during build

    def _show_history_batch(self):
        """Render the next batch of history cards, then a 'show more' button
        if entries remain.  Called initially and when the user clicks 'more'."""
        if self._history_more_btn is not None:
            self._history_more_btn.destroy()
            self._history_more_btn = None

        self._batch_target = min(
            self._history_shown + self._BATCH_SIZE,
            len(self._history_entries),
        )
        self._render_card_chunk()

    def _render_card_chunk(self):
        """Create up to _CHUNK cards, then yield to the event loop."""
        self._history_chunk_timer = None
        # Guard against firing after the window was destroyed mid-chain
        # (defense-in-depth on top of _cancel_pending_timers).
        if self._window is None:
            return
        try:
            if not self._window.winfo_exists():
                return
        except Exception:
            return
        start = self._history_shown
        end = min(start + self._CHUNK, self._batch_target)
        for i in range(start, end):
            self._create_history_card(i, self._history_entries[i],
                                       self._history_peer_map)
        self._history_shown = end

        if end < self._batch_target:
            self._history_chunk_timer = self._root.after(1, self._render_card_chunk)
            return

        # Batch complete — show 'more' button if entries remain
        remaining = len(self._history_entries) - end
        if remaining > 0:
            self._history_more_btn = ctk.CTkButton(
                self._history_scroll, height=32, fg_color="transparent",
                border_width=1, border_color=("#2A82C7", "#1F6AA5"),
                text_color=("#2A82C7", "#5DADE2"),
                hover_color=("#EAF2F8", "#1B2A3A"),
                font=ctk.CTkFont(size=12),
                text=T("history.show_more", count=remaining),
                command=self._show_history_batch,
            )
            self._history_more_btn.pack(fill="x", pady=4, padx=4)

    @staticmethod
    def _format_relative_time(timestamp: float) -> str:
        """Format a timestamp as a human-readable relative time string."""
        if not timestamp or timestamp <= 0:
            return ""
        dt = datetime.datetime.fromtimestamp(timestamp)
        diff = (datetime.datetime.now() - dt).total_seconds()
        if diff < 10:
            return T("history.just_now")
        elif diff < 60:
            return T("history.seconds_ago", count=int(diff))
        elif diff < 3600:
            return T("history.minutes_ago", count=int(diff // 60))
        elif diff < 86400:
            return T("history.hours_ago", count=int(diff // 3600))
        elif dt.date() == datetime.datetime.now().date():
            return dt.strftime("%H:%M")
        elif (datetime.datetime.now().date() - dt.date()).days == 1:
            return T("history.yesterday", time=dt.strftime("%H:%M"))
        else:
            return dt.strftime("%m-%d %H:%M")

    @staticmethod
    def _sanitize_preview(raw: str, max_len: int = 120) -> str:
        """Strip control and replacement characters for clean display."""
        if not raw:
            return ""
        cleaned = "".join(
            ch if ch.isprintable() or ch in ("\t", "\n", "\r") else " "
            for ch in raw
        )
        cleaned = " ".join(cleaned.split())
        return cleaned[:max_len]

    def _create_history_card(self, index: int, entry: dict,
                            peer_map: dict[str, str] | None = None):
        # ── Lazy-init cached objects (first call only) ───────────
        if self._card_font_bold is None:
            self._card_font_bold = ctk.CTkFont(size=11, weight="bold")
            self._card_font = ctk.CTkFont(size=11)
            self._card_font_small = ctk.CTkFont(size=10)
            self._card_font_btn = ctk.CTkFont(size=10)
            self._card_type_labels = {
                "TEXT": T("history.type_text"),
                "HTML": T("history.type_html"),
                "IMAGE": T("history.type_image"),
                "IMAGE_EMF": T("history.type_vector_image"),
                "RTF": T("history.type_rich_text"),
                "FILE": T("history.type_file"),
                "URL": T("history.type_url"),
            }
            self._card_type_icons = {
                "TEXT": "📝", "HTML": "🌐", "IMAGE": "🖼️",
                "IMAGE_EMF": "🎨", "RTF": "📋",
                "FILE": "📁", "URL": "🔗",
            }
            self._cached_device_id = self._get_config().device_id

        timestamp = entry.get("timestamp", 0)
        content_type = entry.get("content_type", "Unknown")
        preview = self._sanitize_preview(entry.get("text_preview", ""))
        type_icon = self._card_type_icons.get(content_type, "·")
        type_color = self._TYPE_COLORS.get(content_type, ("#7F8C8D", "#95A5A6"))
        time_str = self._format_relative_time(timestamp)

        source_device = entry.get("source_device", "")
        if source_device and source_device != self._cached_device_id:
            peer_name = peer_map.get(source_device) if peer_map else None
            if peer_name is None:
                peer_name = source_device[:12]
            source_label = T("history.source.remote", name=peer_name)
        else:
            source_label = T("history.source.local")

        type_label = self._card_type_labels.get(content_type, content_type)
        meta = f"{type_label}  ·  {source_label}"

        card = ctk.CTkFrame(self._history_scroll, corner_radius=8,
                           fg_color=("gray95", "gray17"))
        card.pack(fill="x", pady=2, padx=2)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)

        # ── Row 1: icon + preview + time ─────────────────────────
        r1 = ctk.CTkFrame(inner, fg_color="transparent")
        r1.pack(fill="x")

        # Time packed first so it always gets its natural width
        time_lbl = ctk.CTkLabel(
            r1, text=time_str,
            font=self._card_font_small,
            text_color=("gray60", "gray55"),
        )
        time_lbl.pack(side="right", padx=(8, 0))

        ctk.CTkLabel(
            r1, text=type_icon,
            font=ctk.CTkFont(size=14),
            text_color=type_color,
        ).pack(side="left", padx=(0, 8))

        preview_lbl = ctk.CTkLabel(
            r1, text=preview or T("empty.no_preview"),
            font=self._card_font_bold,
            text_color=("gray20", "gray85"),
            anchor="w",
        )
        preview_lbl.pack(side="left", fill="x", expand=True, padx=(0, 4))

        # ── Row 2: meta (left) + buttons (right) ──────────────────
        r2 = ctk.CTkFrame(inner, fg_color="transparent")
        r2.pack(fill="x", pady=(2, 0))

        ctk.CTkLabel(
            r2, text=meta,
            font=self._card_font_small,
            text_color=("gray55", "gray55"),
            anchor="w",
        ).pack(side="left")

        if preview:
            # Operate on the entry's id, not its render position: with a
            # search filter active the card index is into the filtered
            # subset and no longer matches the full history list.
            entry_id = entry.get("entry_id")
            ctk.CTkButton(
                r2, text=T("ui.delete"), width=56, height=24,
                fg_color="transparent", border_width=1,
                text_color=("#E74C3C", "#C0392B"),
                border_color=("#E74C3C", "#C0392B"),
                hover_color=("#FADBD8", "#5B2C2C"),
                font=self._card_font_btn,
                command=lambda eid=entry_id: self._on_delete_history_item(eid),
            ).pack(side="right", padx=(6, 0))
            ctk.CTkButton(
                r2, text=T("ui.copy"), width=56, height=24,
                fg_color=("#27AE60", "#2ECC71"),
                font=self._card_font_btn,
                command=lambda eid=entry_id: self._do_copy_history(eid),
            ).pack(side="right")

    def _do_copy_history(self, entry_id):
        if self._copy_from_history:
            success = self._copy_from_history(entry_id)
            if self._status_footer:
                if success:
                    self._status_footer.configure(
                        text=T("footer.copied"), text_color=("#27AE60", "#2ECC71")
                    )
                else:
                    self._status_footer.configure(
                        text=T("footer.copy_failed"), text_color=("#E74C3C", "#C0392B")
                    )

    def _on_search_keyrelease(self, event):
        if self._history_search_timer is not None:
            self._root.after_cancel(self._history_search_timer)
        self._history_search_timer = self._root.after(300, self._refresh_history_list)

    def _on_clear_search(self):
        if self._history_search_var:
            self._history_search_var.set("")
        self._refresh_history_list()

    def _on_clear_history(self):
        if self._clear_history is None:
            return
        if ask_yesno(self._window, T("history.clear_title"), T("history.clear_confirm")):
            self._clear_history()
            self._refresh_history_list()

    def _on_delete_history_item(self, entry_id):
        if self._delete_history_item is None:
            return
        if ask_yesno(self._window, T("history.delete_title"), T("history.delete_confirm")):
            self._delete_history_item(entry_id)
            # Defer rebuild so the button animation completes first
            self._root.after(50, self._refresh_history_list)

    def _on_clear_transfer_history(self):
        if self._clear_transfer_history is None:
            return
        if ask_yesno(self._window, T("transfers.clear_title"), T("transfers.clear_confirm")):
            self._clear_transfer_history()
            self._root.after(50, self._refresh_transfers)

    # ═══════════════════════════════════════════════════════════════
    # Panel: Transfers
    # ═══════════════════════════════════════════════════════════════

    def _build_transfers_panel(self):
        wrapper = ctk.CTkScrollableFrame(self._content_frame, fg_color="transparent")
        panel = ctk.CTkFrame(wrapper, fg_color="transparent")
        panel.pack(fill="both", expand=True)

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            header, text=T("transfers.title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")

        if self._on_send_file:
            ctk.CTkButton(
                header, text=T("ui.send_file"), width=90, height=30,
                command=self._on_send_file,
            ).pack(side="right", padx=(4, 0))
        if self._on_send_folder:
            ctk.CTkButton(
                header, text=T("ui.send_folder"), width=90, height=30,
                command=self._on_send_folder,
            ).pack(side="right")

        # ── Speed Test card ──────────────────────────────────────
        self._speed_card = ctk.CTkFrame(panel, corner_radius=12,
                                        fg_color=("gray95", "gray17"))
        self._speed_card.pack(fill="x", pady=(0, 8))

        st_top = ctk.CTkFrame(self._speed_card, fg_color="transparent")
        st_top.pack(fill="x", padx=14, pady=(10, 0))

        ctk.CTkLabel(
            st_top, text="⚡ " + T("ui.speed_test"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            st_top, text=T("ui.run"), width=64, height=26,
            fg_color=ACCENT, hover_color=("#2A80C7", "#1A5AA5"),
            font=ctk.CTkFont(size=11),
            command=self._do_speed_test,
        ).pack(side="right")

        # Status / hint label
        self._speed_hint = ctk.CTkLabel(
            self._speed_card, text=T("transfer.speed_test.idle"),
            font=ctk.CTkFont(size=11),
            text_color=("gray55", "gray55"),
        )
        self._speed_hint.pack(anchor="w", padx=14, pady=(2, 0))

        # Progress bar (hidden until test runs)
        self._speed_progress = ctk.CTkProgressBar(
            self._speed_card, height=6,
            progress_color=ACCENT,
        )
        self._speed_progress.pack_forget()

        # Status line during test
        self._speed_status = ctk.CTkLabel(
            self._speed_card, text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=WARN_COLOR,
        )
        self._speed_status.pack_forget()

        # Result row (hidden until done)
        result_row = ctk.CTkFrame(self._speed_card, fg_color="transparent")
        result_row.pack_forget()
        self._speed_result_row = result_row

        self._speed_value = ctk.CTkLabel(
            result_row, text="",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=STATUS_COLOR,
        )
        self._speed_value.pack(side="left", padx=(14, 8), pady=(2, 10))

        self._speed_quality = ctk.CTkLabel(
            result_row, text="",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._speed_quality.pack(side="left", pady=(2, 10))

        # ── Active card — fixed height, scrollable ──────────────────
        active_card = ctk.CTkFrame(panel, corner_radius=12)
        active_card.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(active_card, text=T("transfers.active"),
                     font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))
        self._transfer_scroll = ctk.CTkScrollableFrame(
            active_card, height=180, fg_color="transparent",
        )
        self._transfer_scroll.pack(fill="x", padx=8, pady=(0, 8))

        # ── History card — compact, fixed 60px ───────────────────────
        history_card = ctk.CTkFrame(panel, corner_radius=12)
        history_card.pack(fill="x")

        history_header = ctk.CTkFrame(history_card, fg_color="transparent")
        history_header.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(history_header, text=T("transfers.history"),
                     font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left")
        if self._clear_transfer_history:
            ctk.CTkButton(
                history_header, text=T("transfers.clear"), width=50, height=22,
                fg_color="transparent", border_width=1,
                text_color=("#E74C3C", "#C0392B"),
                border_color=("#E74C3C", "#C0392B"),
                hover_color=("#FADBD8", "#5B2C2C"),
                font=ctk.CTkFont(size=10),
                command=self._on_clear_transfer_history,
            ).pack(side="right")
        self._transfer_history_stats = ctk.CTkLabel(
            history_card, text="",
            font=ctk.CTkFont(size=10),
            text_color=("gray55", "gray55"),
        )
        self._transfer_history_stats.pack(anchor="w", padx=12, pady=(1, 0))
        self._transfer_history_scroll = ctk.CTkScrollableFrame(
            history_card, height=120, fg_color="transparent",
        )
        self._transfer_history_scroll.pack(fill="x", padx=8, pady=(2, 8))

        return wrapper

    def _refresh_transfers(self):
        if self._transfer_scroll is None or self._transfer_history_scroll is None:
            return

        # Hash-based change detection: skip rebuild if nothing changed
        transfers = self._get_transfers() if self._get_transfers else []
        history = self._get_transfer_history() if self._get_transfer_history else []
        speed = (self._get_speed_test_result() if self._get_speed_test_result else None) or {}
        # Change-detection key must use the fields get_transfers() actually
        # returns (transfer_id/state/progress/paused), not id/status — the
        # old keys were always None, so progress and state transitions never
        # triggered a re-render and the panel froze.
        state_key = (
            tuple(
                (
                    t.get("transfer_id"),
                    t.get("state"),
                    round(t.get("progress", 0), 3),
                    t.get("paused", False),
                    round(t.get("speed_bytes_per_sec", 0), 1),
                )
                for t in transfers
            ),
            len(history),
            speed.get("state", ""),
            speed.get("result_mbps", 0),
            speed.get("chunks_sent", 0),
            speed.get("total_chunks", 0),
        )
        if state_key == getattr(self, '_transfers_state_key', None):
            return
        self._transfers_state_key = state_key

        # ── Active transfers ───────────────────────────────────────
        for child in self._transfer_scroll.winfo_children():
            child.destroy()

        if not transfers:
            ctk.CTkLabel(
                self._transfer_scroll,
                text="\U0001F4E4  " + T("empty.no_transfers"),
                font=ctk.CTkFont(size=12),
                text_color=("gray50", "gray60"),
            ).pack(fill="x", pady=8)
        else:
            for t in transfers:
                self._create_transfer_card(t)

        # ── History ─────────────────────────────────────────────────
        for child in self._transfer_history_scroll.winfo_children():
            child.destroy()

        if not history:
            ctk.CTkLabel(
                self._transfer_history_scroll,
                text="\U0001F4E6  " + T("empty.no_transfer_history"),
                font=ctk.CTkFont(size=12),
                text_color=("gray50", "gray60"),
            ).pack(fill="x", pady=8)
            if self._transfer_history_stats:
                self._transfer_history_stats.configure(text="")
        else:
            for h in history[:50]:  # show last 50
                self._create_transfer_history_card(h)
            # Stats
            total = len(history)
            success = sum(1 for h in history if h.get("success"))
            fail = total - success
            total_size = sum(h.get("file_size", 0) for h in history)
            parts = [T("transfer.completed", count=total)]
            if success:
                parts.append(T("transfer.ok", count=success))
            if fail:
                parts.append(T("transfer.fail", count=fail))
            if total_size > 0:
                parts.append(self._format_size(total_size))
            if self._transfer_history_stats:
                self._transfer_history_stats.configure(text=" · ".join(parts))

        # ── Speed test result ──────────────────────────────────────
        if self._speed_card:
            if speed:
                state = speed.get("state", "")
                mbps = speed.get("result_mbps", 0)
                if state == "sending":
                    sent = speed.get("chunks_sent", 0)
                    total = speed.get("total_chunks", 0)
                    if total > 0 and self._speed_progress:
                        self._speed_progress.set(sent / total)
                    if self._speed_status:
                        self._speed_status.configure(
                            text=T("transfer.speed_test_progress", sent=sent, total=total),
                        )
                elif state in ("done", "acknowledged") and mbps > 0:
                    # Hide progress, show result
                    if self._speed_progress:
                        self._speed_progress.pack_forget()
                    if self._speed_status:
                        self._speed_status.pack_forget()
                    if self._speed_hint:
                        self._speed_hint.pack_forget()
                    if mbps > 10:
                        quality = T("transfer.speed.fast")
                        q_color = ("#27AE60", "#2ECC71")
                    elif mbps > 2:
                        quality = T("transfer.speed.good")
                        q_color = ("#F39C12", "#F1C40F")
                    else:
                        quality = T("transfer.speed.slow")
                        q_color = ("#E74C3C", "#C0392B")
                    if self._speed_value:
                        self._speed_value.configure(
                            text=f"{mbps:.1f} Mbps",
                            text_color=("#27AE60", "#2ECC71"),
                        )
                    if self._speed_quality:
                        self._speed_quality.configure(
                            text=quality.capitalize() if quality else "",
                            text_color=q_color,
                        )
                    if self._speed_result_row:
                        self._speed_result_row.pack(fill="x")
                else:
                    # Failed — hide progress, show status in red
                    if self._speed_progress:
                        self._speed_progress.pack_forget()
                    if self._speed_hint:
                        self._speed_hint.pack_forget()
                    if self._speed_result_row:
                        self._speed_result_row.pack_forget()
                    if self._speed_status:
                        self._speed_status.configure(
                            text=T("transfer.speed_test.failed"),
                            text_color=("#E74C3C", "#C0392B"),
                        )
                        self._speed_status.pack(anchor="w", padx=14, pady=(2, 0))
            else:
                # No test data — show idle hint
                if self._speed_hint:
                    self._speed_hint.pack(anchor="w", padx=14, pady=(2, 0))
                if self._speed_progress:
                    self._speed_progress.pack_forget()
                if self._speed_status:
                    self._speed_status.pack_forget()
                if self._speed_result_row:
                    self._speed_result_row.pack_forget()

    def _format_speed(self, bytes_per_sec: float) -> str:
        if bytes_per_sec < 1024:
            return f"{bytes_per_sec:.0f} B/s"
        elif bytes_per_sec < 1024 * 1024:
            return f"{bytes_per_sec / 1024:.1f} KB/s"
        else:
            return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"

    def _format_eta(self, seconds: float) -> str:
        if seconds <= 0:
            return ""
        if seconds < 60:
            return f"{int(seconds)}s left"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m {int(seconds % 60)}s left"
        else:
            return f"{int(seconds / 3600)}h left"

    def _create_transfer_card(self, transfer: dict):
        direction = transfer.get("direction", "down")
        file_name = transfer.get("file_name", "?")
        file_size = transfer.get("file_size", 0)
        progress = transfer.get("progress", 0.0)
        state = transfer.get("state", "unknown")
        speed = transfer.get("speed_bytes_per_sec", 0.0)
        eta = transfer.get("eta_seconds", 0.0)

        arrow = "\U0001F4E4" if direction == "up" else "\U0001F4E5"
        size_str = self._format_size(file_size)

        # Truncate long filenames
        display_name = file_name if len(file_name) <= 28 else file_name[:25] + "..."

        card = ctk.CTkFrame(self._transfer_scroll, corner_radius=8,
                           fg_color=("gray95", "gray17"))
        card.pack(fill="x", pady=2, padx=2)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        # Row 1: file name + size + action buttons
        r1 = ctk.CTkFrame(inner, fg_color="transparent")
        r1.pack(fill="x")

        # Action buttons (pause / resume / cancel) — right side
        paused = transfer.get("paused", False)
        tid = transfer.get("transfer_id", "")
        if state in ("sending", "receiving", "paused"):
            if self._on_cancel_transfer:
                ctk.CTkButton(
                    r1, text=T("ui.cancel"), width=52, height=22,
                    fg_color="transparent", border_width=1,
                    text_color=("#E74C3C", "#C0392B"),
                    border_color=("#E74C3C", "#C0392B"),
                    hover_color=("#FADBD8", "#5B2C2C"),
                    font=ctk.CTkFont(size=10),
                    command=lambda t=tid: self._on_cancel_transfer(t),
                ).pack(side="right", padx=(4, 0))
            if self._on_resume_transfer and paused:
                ctk.CTkButton(
                    r1, text=T("ui.resume"), width=52, height=22,
                    fg_color="transparent", border_width=1,
                    text_color=("#27AE60", "#2ECC71"),
                    border_color=("#27AE60", "#2ECC71"),
                    hover_color=("#D5F5E3", "#1C4A2C"),
                    font=ctk.CTkFont(size=10),
                    command=lambda t=tid: self._on_resume_transfer(t),
                ).pack(side="right", padx=(4, 0))
            if self._on_pause_transfer and not paused:
                ctk.CTkButton(
                    r1, text=T("ui.pause"), width=52, height=22,
                    fg_color="transparent", border_width=1,
                    text_color=("#E67E22", "#F0A04B"),
                    border_color=("#E67E22", "#F0A04B"),
                    hover_color=("#FDEBD0", "#5B3A1C"),
                    font=ctk.CTkFont(size=10),
                    command=lambda t=tid: self._on_pause_transfer(t),
                ).pack(side="right", padx=(4, 0))

        ctk.CTkLabel(
            r1, text=f"{arrow}  {display_name}",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            r1, text=size_str,
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(side="right")

        # Row 2: status + speed + ETA
        state_labels = {
            "awaiting_ack": T("transfer.state.waiting_peer"),
            "pending": T("transfer.state.waiting_acceptance"),
            "receiving": T("transfer.state.receiving"),
            "sending": T("transfer.state.sending"),
            "cancelled": T("transfer.state.cancelled"),
            "paused": T("transfer.state.paused"),
            "awaiting_retransmit": T("transfer.state.awaiting_retransmit"),
        }
        if transfer.get("paused"):
            state = "paused"
        status_text = state_labels.get(state, state.replace("_", " ").title())

        extras = []
        if state in ("receiving", "sending", "paused"):
            extras.append(f"{int(progress * 100)}%")
        if speed > 0:
            extras.append(self._format_speed(speed))
        if eta > 0:
            extras.append(self._format_eta(eta))

        r2 = ctk.CTkFrame(inner, fg_color="transparent")
        r2.pack(fill="x")
        ctk.CTkLabel(
            r2, text="  |  ".join([status_text] + extras) if extras else status_text,
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(side="left")

        # Progress bar
        if state in ("receiving", "sending") and progress >= 0:
            bar = ctk.CTkProgressBar(inner, height=6)
            bar.pack(fill="x", pady=(6, 0))
            bar.set(progress)

    def _create_transfer_history_card(self, entry: dict):
        direction = entry.get("direction", "down")
        file_name = entry.get("file_name", "?")
        file_size = entry.get("file_size", 0)
        success = entry.get("success", False)
        timestamp = entry.get("timestamp", 0)
        saved_path = entry.get("saved_path", "")
        source_path = entry.get("source_path", "")

        arrow = "\U0001F4E4" if direction == "up" else "\U0001F4E5"
        status_icon = "✅" if success else "❌"
        size_str = self._format_size(file_size)
        if timestamp and timestamp > 0:
            time_str = datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        else:
            time_str = ""

        # Truncate long filenames to keep the time/size visible
        display_name = file_name if len(file_name) <= 30 else file_name[:27] + "..."

        card = ctk.CTkFrame(self._transfer_history_scroll, corner_radius=6,
                           fg_color=("gray95", "gray17"))
        card.pack(fill="x", pady=2, padx=2)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)

        # Row 1: icon + filename (left), buttons + size + time (right)
        r1 = ctk.CTkFrame(inner, fg_color="transparent")
        r1.pack(fill="x")

        resolve_path = saved_path or source_path
        has_actions = (
            (resolve_path and success and self._on_open_file and self._on_open_folder)
            or self._delete_transfer_history_item
        )

        # Action buttons on the right
        if has_actions:
            if self._delete_transfer_history_item:
                ctk.CTkButton(
                    r1, text=T("ui.delete"), width=48, height=22,
                    fg_color="transparent", border_width=1,
                    text_color=("#E74C3C", "#C0392B"),
                    border_color=("#E74C3C", "#C0392B"),
                    hover_color=("#FADBD8", "#5B2C2C"),
                    font=ctk.CTkFont(size=10),
                    command=lambda e=entry: self._delete_transfer_history_item(e),
                ).pack(side="right", padx=(4, 0))
            if resolve_path and success and self._on_open_file and self._on_open_folder:
                ctk.CTkButton(
                    r1, text=T("ui.open_folder"), width=72, height=22,
                    fg_color=("gray85", "gray25"),
                    text_color=("gray20", "gray80"),
                    hover_color=("gray75", "gray35"),
                    font=ctk.CTkFont(size=10),
                    command=lambda p=resolve_path: self._on_open_folder(p),
                ).pack(side="right", padx=(4, 0))
                ctk.CTkButton(
                    r1, text=T("ui.open_file"), width=64, height=22,
                    fg_color=("gray85", "gray25"),
                    text_color=("gray20", "gray80"),
                    hover_color=("gray75", "gray35"),
                    font=ctk.CTkFont(size=10),
                    command=lambda p=resolve_path: self._on_open_file(p),
                ).pack(side="right", padx=(4, 0))

        ctk.CTkLabel(
            r1, text=f"{status_icon}  {arrow}  {display_name}",
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            r1, text=f"{size_str}  ·  {time_str}",
            font=ctk.CTkFont(size=10),
            text_color=("gray50", "gray60"),
        ).pack(side="right")

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"

    # ═══════════════════════════════════════════════════════════════
    # Toggle handlers
    # ═══════════════════════════════════════════════════════════════

    def _on_toggle_sync(self):
        enabled = self._sync_var.get()
        self._set_sync(enabled)
        cfg = self._get_config()
        cfg.sync_enabled = enabled
        self._save_config()
        self._refresh_overview()

    def _on_toggle_autostart(self):
        cfg = self._get_config()
        enabled = self._autostart_var.get()
        cfg.auto_start = enabled
        self._save_config()
        if self._on_toggle_autostart_cb:
            try:
                self._on_toggle_autostart_cb(enabled)
            except Exception:
                logger.debug("Auto-start toggle failed", exc_info=True)

    def _on_toggle_discovery(self):
        if self._on_toggle_discovery_cb:
            self._on_toggle_discovery_cb(self._discovery_var.get())

    def _on_toggle_visibility(self):
        if self._on_toggle_visibility_cb:
            self._on_toggle_visibility_cb(self._visibility_var.get())

    def _edit_device_name(self):
        cfg = self._get_config()
        new_name = ask_string(
            self._window,
            T("ui.edit_name"),
            T("ui.edit_name_prompt"),
            initial_value=cfg.device_name,
        )
        if new_name and new_name.strip():
            cfg.device_name = new_name.strip()
            self._save_config()
            if self._device_name_label:
                self._device_name_label.configure(
                    text=T("overview.this_device_label", name=cfg.device_name))
            if self._overview_device_name:
                self._overview_device_name.configure(text=cfg.device_name)
            if self._status_footer:
                self._status_footer.configure(text=T("footer.name_updated"))

    # ── Web Companion (overview) ────────────────────────────────────

    def _on_web_toggle(self):
        enabled = self._web_switch_var.get()
        cfg = self._get_config()
        cfg.web_enabled = enabled
        if enabled and not cfg.web_token:
            import secrets
            cfg.web_token = secrets.token_urlsafe(16)
        self._save_config()
        self._refresh_web_card()
        if self._on_web_action:
            self._on_web_action({"action": "start" if enabled else "stop"})

    def _on_web_copy_url(self):
        url = self._web_url_label.cget("text") if self._web_url_label else ""
        if url and self._web_copy_btn:
            self._window.clipboard_clear()
            self._window.clipboard_append(url)
            self._web_copy_btn.configure(text=T("web.copied"))

            def _reset_copy_label():
                self._copy_url_timer = None
                if self._web_copy_btn is not None:
                    try:
                        if self._web_copy_btn.winfo_exists():
                            self._web_copy_btn.configure(text=T("ui.copy"))
                    except Exception:
                        pass

            self._copy_url_timer = self._root.after(2000, _reset_copy_label)

    def _refresh_web_card(self):
        if self._web_card is None:
            return
        cfg = self._get_config()
        if not cfg.web_enabled:
            self._web_card.pack_forget()
            return
        try:
            self._web_card.pack_info()
        except tk.TclError:
            self._web_card.pack(fill="x", pady=(8, 10),
                                before=self._activity_card)

        from internal.web.server import WebServer
        ip = WebServer._get_lan_ip()
        token = cfg.web_token or ""
        port = cfg.web_port
        url = f"http://{ip}:{port}?token={token}" if token else f"http://{ip}:{port}"
        self._web_url_label.configure(text=url)

        # The QR image is expensive to regenerate (qrcode.make + LANCZOS
        # resize) — cache it and only rebuild when the token / IP / port
        # actually changes. The LAN-IP lookup (getaddrinfo) still runs each
        # slow refresh so IP changes are picked up, but never per-frame.
        qr_key = (ip, token, port)
        if token:
            if qr_key != getattr(self, '_web_qr_key', None) or self._web_qr_image is None:
                try:
                    import qrcode
                    from PIL import Image
                    img = qrcode.make(url)
                    img = img.convert("RGB")
                    img = img.resize((100, 100), Image.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(100, 100))
                    self._web_qr_image = ctk_img
                    self._web_qr_key = qr_key
                except Exception:
                    logger.debug("QR code generation failed", exc_info=True)
                    self._web_qr_label.configure(image=None, text="QR err")
                    return
            self._web_qr_label.configure(image=self._web_qr_image, text="")
            self._web_qr_label.image = self._web_qr_image
        else:
            self._web_qr_label.configure(image=None, text="")

