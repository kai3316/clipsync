"""Settings window for ClipSync — Network, Content Filter, and About.

This is a secondary window accessed from the dashboard. It contains
configuration that users rarely change: network settings, content
filtering preferences, and version information.
"""

import logging
import os
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from internal.clipboard.filter import ALL_CATEGORIES
from internal.i18n import T, available_locales, set_locale
from internal.platform.notify import notification_mgr
from internal.ui.dialogs import ask_yesno, show_info, show_warning
from internal.web.server import WebServer

logger = logging.getLogger(__name__)


def _confirm_danger(parent, title: str, message: str) -> bool:
    """Show a confirmation dialog for dangerous actions. Returns True if confirmed."""
    return ask_yesno(parent, title, message)


class SettingsWindow:
    """Settings window with sidebar navigation — separate from the main dashboard."""

    def __init__(
        self,
        root: tk.Tk,
        get_config: Callable,
        save_config: Callable,
        on_closed: Callable | None = None,
        on_export_logs: Callable | None = None,
        get_filter_categories: Callable | None = None,
        set_filter_categories: Callable | None = None,
        get_log_text: Callable | None = None,
        on_quit: Callable | None = None,
        set_skip_save_on_shutdown: Callable[[bool], None] | None = None,
    ):
        self._root = root
        self._get_config = get_config
        self._save_config = save_config
        self._on_closed = on_closed
        self._on_export_logs = on_export_logs
        self._get_filter_categories = get_filter_categories
        self._set_filter_categories = set_filter_categories
        self._get_log_text = get_log_text
        self._on_quit = on_quit
        # Lets the host application suppress its config re-save during
        # shutdown() after a restart / factory reset (otherwise shutdown
        # would recreate config.json with the old settings).
        self._set_skip_save_on_shutdown = set_skip_save_on_shutdown

        self._window: ctk.CTkToplevel | None = None
        self._dark_mode = get_config().appearance_mode == "dark"
        self._current_panel = "network"
        self._refresh_job: str | None = None

        # Widget references
        self._sidebar_buttons: dict[str, ctk.CTkButton] = {}
        self._panels: dict[str, ctk.CTkFrame] = {}
        self._content_frame: ctk.CTkFrame | None = None
        self._status_label: ctk.CTkLabel | None = None

        # Widget refs
        self._log_text: ctk.CTkTextbox | None = None

        # Form vars
        self._port_var: tk.StringVar | None = None
        self._svc_var: tk.StringVar | None = None
        self._relay_var: tk.StringVar | None = None
        self._filter_vars: dict[str, tk.BooleanVar] = {}
        # Advanced panel vars
        self._history_max_var: tk.StringVar | None = None
        self._file_receive_dir_var: tk.StringVar | None = None
        self._sync_debounce_var: tk.StringVar | None = None
        self._poll_interval_var: tk.StringVar | None = None
        self._max_reconnect_var: tk.StringVar | None = None
        self._transfer_timeout_var: tk.StringVar | None = None
        self._log_level_var: tk.StringVar | None = None
        self._language_var: tk.StringVar | None = None
        self._notifications_var: tk.BooleanVar | None = None
        # Web companion vars
        self._web_enabled_var: tk.BooleanVar | None = None
        self._web_port_var: tk.StringVar | None = None
        self._web_history_limit_var: tk.StringVar | None = None
        self._web_token_var: tk.StringVar | None = None
        self._web_ip_label: ctk.CTkLabel | None = None
        self._web_qr_label: ctk.CTkLabel | None = None
        self._web_qr_image: ctk.CTkImage | None = None
        self._web_url_label: ctk.CTkLabel | None = None
        self._web_copy_btn: ctk.CTkButton | None = None

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
                self._window.attributes("-topmost", True)
                self._window.after(200, lambda: self._window.attributes("-topmost", False))
                if self._window.winfo_viewable():
                    self._switch_panel(self._current_panel)
                    return
                self._window.destroy()
                self._window = None
            except tk.TclError:
                self._window = None

        logger.info("Opening ClipSync settings")
        ctk.set_appearance_mode("dark" if self._dark_mode else "light")
        ctk.set_default_color_theme("blue")

        self._window = ctk.CTkToplevel(self._root)
        self._window.title(T("settings_window.title"))
        self._window.geometry("740x620")
        self._window.minsize(680, 560)
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)

        self._window.update_idletasks()
        sw = self._window.winfo_screenwidth()
        sh = self._window.winfo_screenheight()
        w, h = 740, 620
        self._window.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._build_ui()
        self._switch_panel("network")

        try:
            self._window.attributes("-topmost", True)
            self._window.after(200, lambda: self._window.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _on_close(self):
        if self._window is not None:
            self._window.destroy()
            self._window = None
            self._sidebar_buttons.clear()
            self._panels.clear()
            self._filter_vars.clear()
        if self._on_closed is not None:
            self._on_closed()

    # ═══════════════════════════════════════════════════════════════
    # UI construction
    # ═══════════════════════════════════════════════════════════════

    def _build_ui(self):
        outer = ctk.CTkFrame(self._window, fg_color="transparent")
        outer.pack(fill="both", expand=True)

        # Header
        header = ctk.CTkFrame(outer, corner_radius=0, fg_color=("#1A5276", "#1B2A3A"))
        header.pack(fill="x")
        h_inner = ctk.CTkFrame(header, fg_color="transparent")
        h_inner.pack(fill="x", padx=20, pady=(14, 14))

        ctk.CTkLabel(
            h_inner, text=T("ui.settings"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=("#FFFFFF", "#E0E0E0"),
        ).pack(side="left")

        self._theme_btn = ctk.CTkButton(
            h_inner, text=T("ui.theme_dark") if not self._dark_mode else T("ui.theme_light"),
            width=90, height=32, fg_color="transparent",
            border_width=1, border_color=("#7F8C8D", "#566573"),
            text_color=("#FFFFFF", "#E0E0E0"),
            hover_color=("#5D6D7E", "#4A5568"),
            command=self._toggle_theme,
        )
        self._theme_btn.pack(side="right")

        # Body: sidebar | content
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True)

        self._build_sidebar(body)

        sep = ctk.CTkFrame(body, width=1, fg_color=("gray75", "gray30"))
        sep.pack(side="left", fill="y")

        self._content_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._content_frame.pack(side="left", fill="both", expand=True)

        # Build panels
        self._panels["network"] = self._build_network_panel()
        self._panels["appearance"] = self._build_appearance_panel()
        self._panels["web_companion"] = self._build_web_companion_panel()
        self._panels["filter"] = self._build_filter_panel()
        self._panels["security"] = self._build_security_panel()
        self._panels["advanced"] = self._build_advanced_panel()
        self._panels["logs"] = self._build_logs_panel()
        self._panels["about"] = self._build_about_panel()

        # Footer
        footer = ctk.CTkFrame(outer, height=44, corner_radius=0,
                              fg_color=("gray90", "gray15"))
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        f_inner = ctk.CTkFrame(footer, fg_color="transparent")
        f_inner.pack(fill="x", padx=20, pady=8)

        self._status_label = ctk.CTkLabel(
            f_inner, text=T("footer.ready"), text_color=("gray50", "gray60"),
            font=ctk.CTkFont(size=11),
        )
        self._status_label.pack(side="left")

        ctk.CTkButton(
            f_inner, text=T("ui.close"), width=60, height=28,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray70"),
            border_color=("gray60", "gray50"),
            hover_color=("gray85", "gray25"),
            command=self._on_close,
        ).pack(side="right")

    # ── Sidebar ────────────────────────────────────────────────────

    def _build_sidebar(self, body):
        sidebar = ctk.CTkFrame(body, width=180, fg_color="transparent")
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        inner = ctk.CTkFrame(sidebar, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=8, pady=16)

        nav = [
            ("network",       T("settings_nav.network")),
            ("appearance",    T("settings_nav.appearance")),
            ("web_companion", T("settings_nav.web_companion")),
            ("filter",        T("settings_nav.filter")),
            ("security",      T("settings_nav.security")),
            ("advanced",      T("settings_nav.advanced")),
            ("logs",          T("settings_nav.logs")),
            ("about",         T("settings_nav.about")),
        ]

        for key, label in nav:
            btn = ctk.CTkButton(
                inner, text=label, anchor="w",
                height=40, corner_radius=8,
                fg_color="transparent",
                text_color=("gray30", "gray80"),
                hover_color=("gray85", "gray25"),
                font=ctk.CTkFont(size=13),
                command=lambda k=key: self._switch_panel(k),
            )
            btn.pack(fill="x", pady=2)
            self._sidebar_buttons[key] = btn

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
        if key == "logs":
            self._refresh_log_text(self._log_text)

    # ═══════════════════════════════════════════════════════════════
    # Panel: Network
    # ═══════════════════════════════════════════════════════════════

    def _build_network_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")
        cfg = self._get_config()

        ctk.CTkLabel(
            panel, text=T("settings_window.network_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 16))

        card = ctk.CTkFrame(panel, corner_radius=12)
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card, text=T("network.connection"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(row1, text=T("network.tcp_port"), width=100, anchor="w").pack(side="left")
        self._port_var = tk.StringVar(value=str(cfg.port))
        ctk.CTkEntry(row1, textvariable=self._port_var, width=80, height=32).pack(side="left", padx=(12, 8))
        ctk.CTkLabel(
            row1, text=T("settings_window.port_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(side="left")

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(row2, text=T("network.service_type"), width=100, anchor="w").pack(side="left")
        self._svc_var = tk.StringVar(value=cfg.service_type)
        ctk.CTkEntry(row2, textvariable=self._svc_var, height=32).pack(
            side="left", fill="x", expand=True, padx=(12, 0))

        # Relay card
        card2 = ctk.CTkFrame(panel, corner_radius=12)
        card2.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card2, text=T("network.relay"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        ctk.CTkLabel(
            card2, text=T("network.relay_url"), anchor="w",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=16)
        self._relay_var = tk.StringVar(value=cfg.relay_url)
        ctk.CTkEntry(card2, textvariable=self._relay_var, height=32).pack(
            fill="x", padx=16, pady=(4, 6))

        ctk.CTkLabel(
            card2,
            text=T("settings_window.relay_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(anchor="w", padx=16, pady=(0, 14))

        ctk.CTkButton(
            panel, text=T("settings_window.save_network"),
            width=200, height=36, command=self._on_save_network,
        ).pack(anchor="w")

        return panel

    def _on_save_network(self):
        try:
            port = int(self._port_var.get())
            if not 1024 <= port <= 65535:
                raise ValueError("Port out of range")
        except ValueError:
            show_warning(self._window, T("dialog.invalid"), T("settings_window.port_invalid"))
            return

        cfg = self._get_config()
        cfg.port = port
        cfg.service_type = self._svc_var.get().strip()
        cfg.relay_url = self._relay_var.get().strip()
        self._save_config()
        show_info(
            self._window,
            T("dialog.saved"),
            T("settings_window.network_saved"),
        )

    # ═══════════════════════════════════════════════════════════════
    # Panel: Appearance
    # ═══════════════════════════════════════════════════════════════

    def _build_appearance_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")

        ctk.CTkLabel(
            panel, text=T("settings_window.appearance_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(
            panel, text=T("settings_window.appearance_desc"),
            font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
            justify="left", wraplength=500,
        ).pack(anchor="w", pady=(0, 20))

        # Theme selector — segmented button for System / Light / Dark
        cfg = self._get_config()
        current = cfg.appearance_mode  # "system", "light", "dark"

        self._appearance_var = tk.StringVar(value=current)

        theme_card = ctk.CTkFrame(panel, corner_radius=12,
                                  fg_color=("gray95", "gray17"))
        theme_card.pack(fill="x")

        t_inner = ctk.CTkFrame(theme_card, fg_color="transparent")
        t_inner.pack(fill="x", padx=20, pady=20)

        ctk.CTkLabel(
            t_inner, text=T("settings_window.theme_label"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(0, 12))

        modes = [
            ("system", T("settings_window.theme_system")),
            ("light",  T("settings_window.theme_light")),
            ("dark",   T("settings_window.theme_dark")),
        ]

        for mode, label in modes:
            btn = ctk.CTkRadioButton(
                t_inner, text=label, variable=self._appearance_var, value=mode,
                font=ctk.CTkFont(size=13),
                command=lambda m=mode: self._on_appearance_change(m),
            )
            btn.pack(anchor="w", pady=3)

        ctk.CTkLabel(
            t_inner, text=T("settings_window.theme_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray55", "gray55"),
        ).pack(anchor="w", pady=(10, 0))

        # ── UI Backend toggle ──────────────────────────────────────
        ui_card = ctk.CTkFrame(panel, corner_radius=12,
                                fg_color=("gray95", "gray17"))
        ui_card.pack(fill="x", pady=(16, 0))

        ui_inner = ctk.CTkFrame(ui_card, fg_color="transparent")
        ui_inner.pack(fill="x", padx=20, pady=20)

        ctk.CTkLabel(
            ui_inner, text=T("settings_window.ui_backend_label"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(0, 12))

        current_ui = cfg.ui_backend  # "webview" or "ctk"
        self._ui_backend_var = tk.StringVar(value=current_ui)

        ui_modes = [
            ("webview", T("settings_window.ui_modern")),
            ("ctk",     T("settings_window.ui_classic")),
        ]
        for mode, label in ui_modes:
            ctk.CTkRadioButton(
                ui_inner, text=label, variable=self._ui_backend_var, value=mode,
                font=ctk.CTkFont(size=13),
                command=lambda m=mode: self._on_ui_backend_change(m),
            ).pack(anchor="w", pady=3)

        ctk.CTkLabel(
            ui_inner, text=T("settings_window.ui_backend_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray55", "gray55"),
        ).pack(anchor="w", pady=(10, 0))

        return panel

    def _on_ui_backend_change(self, mode: str):
        cfg = self._get_config()
        cfg.ui_backend = mode
        self._save_config()
        show_info(
            self._window,
            T("settings_window.ui_backend_label"),
            T("settings_window.ui_backend_restart"),
        )

    def _on_appearance_change(self, mode: str):
        ctk.set_appearance_mode(mode)
        self._dark_mode = (mode == "dark")
        # Update header theme button
        if hasattr(self, '_theme_btn') and self._theme_btn:
            self._theme_btn.configure(
                text=T("ui.theme_light") if self._dark_mode else T("ui.theme_dark")
            )
        cfg = self._get_config()
        cfg.appearance_mode = mode
        self._save_config()
        try:
            self._status_label.configure(text=T("footer.settings_saved"))
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════
    # Panel: Web Companion
    # ═══════════════════════════════════════════════════════════════

    def _build_web_companion_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")
        cfg = self._get_config()

        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(
            scroll, text=T("settings_window.web_companion_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            scroll, text=T("settings_window.web_companion_desc"),
            font=ctk.CTkFont(size=11), text_color=("gray50", "gray60"),
            wraplength=480,
        ).pack(anchor="w", pady=(0, 16))

        # ── Enable toggle ──────────────────────────────────────
        card1 = ctk.CTkFrame(scroll, corner_radius=12)
        card1.pack(fill="x", pady=(0, 12))

        self._web_enabled_var = tk.BooleanVar(value=cfg.web_enabled)
        sw = ctk.CTkSwitch(
            card1, text=T("settings_window.web_enable"),
            variable=self._web_enabled_var,
            font=ctk.CTkFont(size=13),
        )
        sw.pack(anchor="w", padx=16, pady=(14, 14))

        # ── Port ───────────────────────────────────────────────
        card2 = ctk.CTkFrame(scroll, corner_radius=12)
        card2.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card2, text=T("settings_window.web_port"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 8))

        row = ctk.CTkFrame(card2, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 4))
        self._web_port_var = tk.StringVar(value=str(cfg.web_port))
        ctk.CTkEntry(
            row, textvariable=self._web_port_var, width=80, height=32,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            row, text=T("settings_window.web_port_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(side="left")

        # ── History limit ──────────────────────────────────────
        ctk.CTkLabel(
            card2, text=T("settings_window.web_history_limit"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 4))

        row2 = ctk.CTkFrame(card2, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(0, 8))
        self._web_history_limit_var = tk.StringVar(value=str(cfg.web_history_limit))
        ctk.CTkEntry(
            row2, textvariable=self._web_history_limit_var, width=80, height=32,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            row2, text=T("settings_window.web_history_limit_desc"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
        ).pack(side="left")

        # ── Token management ────────────────────────────────────
        ctk.CTkLabel(
            card2, text=T("settings_window.web_token"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 2))

        token_row = ctk.CTkFrame(card2, fg_color="transparent")
        token_row.pack(fill="x", padx=16, pady=(4, 4))
        self._web_token_var = tk.StringVar(value=cfg.web_token or "")
        token_entry = ctk.CTkEntry(
            token_row, textvariable=self._web_token_var, height=32,
            state="readonly", font=ctk.CTkFont(size=11),
        )
        token_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        ctk.CTkButton(
            token_row, text=T("settings_window.web_token_regenerate"),
            width=90, height=32, font=ctk.CTkFont(size=11),
            command=self._on_regenerate_token,
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            token_row, text=T("settings_window.web_token_clear"),
            width=60, height=32, font=ctk.CTkFont(size=11),
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray60"),
            border_color=("gray60", "gray50"),
            command=self._on_clear_token,
        ).pack(side="left")

        # ── LAN IP ────────────────────────────────────────────
        ctk.CTkLabel(
            card2, text=T("settings_window.web_ip"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 4))
        self._web_ip_label = ctk.CTkLabel(
            card2, text=WebServer._get_lan_ip(),
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(0, 8))

        # ── QR Code ───────────────────────────────────────────
        ctk.CTkLabel(
            card2, text=T("settings_window.web_qr"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(12, 4))

        qr_frame = ctk.CTkFrame(card2, corner_radius=8,
                                fg_color=("gray95", "gray17"))
        qr_frame.pack(padx=16, pady=(4, 4))
        self._web_qr_label = ctk.CTkLabel(qr_frame, text="")
        self._web_qr_label.pack(padx=20, pady=20)

        ctk.CTkLabel(
            card2, text=T("settings_window.web_qr_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray55", "gray55"),
        ).pack(anchor="w", padx=16, pady=(0, 2))

        # ── URL ───────────────────────────────────────────────
        ctk.CTkLabel(
            card2, text=T("settings_window.web_local_url"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(12, 4))
        url_row = ctk.CTkFrame(card2, fg_color="transparent")
        url_row.pack(fill="x", padx=16, pady=(4, 14))
        self._web_url_label = ctk.CTkLabel(
            url_row, text="", font=ctk.CTkFont(size=11), text_color=("gray50", "gray60"),
        )
        self._web_url_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._web_copy_btn = ctk.CTkButton(
            url_row, text=T("ui.copy"), width=60, height=28,
            font=ctk.CTkFont(size=11),
            command=self._on_copy_url,
        )
        self._web_copy_btn.pack(side="right")

        # Refresh QR and URL
        self._refresh_web_qr()

        # ── Save button ───────────────────────────────────────
        ctk.CTkButton(
            scroll, text=T("settings_window.save_web"),
            width=200, height=36, command=self._on_save_web,
        ).pack(anchor="w", pady=(4, 16))

        return panel

    def _refresh_web_qr(self):
        if not self._web_qr_label:
            return
        try:

            import qrcode as _qrcode
            from PIL import Image

            token = self._web_token_var.get() if self._web_token_var else ""
            port = self._web_port_var.get() if self._web_port_var else "19991"
            ip = WebServer._get_lan_ip()
            url = f"http://{ip}:{port}?token={token}" if token else f"http://{ip}:{port}"

            if self._web_url_label:
                self._web_url_label.configure(text=url)

            if token:
                img = _qrcode.make(url)
                img = img.convert("RGB")
                img = img.resize((200, 200), Image.LANCZOS)
                self._web_qr_image = ctk.CTkImage(
                    light_image=img, dark_image=img, size=(200, 200),
                )
                self._web_qr_label.configure(image=self._web_qr_image, text="")
            else:
                self._web_qr_label.configure(
                    image=None,
                    text=T("settings_window.web_token_none_hint"),
                    font=ctk.CTkFont(size=11),
                    text_color=("gray50", "gray60"),
                )
        except Exception as e:
            logger.debug("QR code generation failed: %s", e)
            if self._web_qr_label:
                self._web_qr_label.configure(
                    image=None,
                    text=T("settings_window.web_qr_unavailable"),
                )

    def _on_regenerate_token(self):
        import secrets
        new_token = secrets.token_urlsafe(16)
        if self._web_token_var:
            self._web_token_var.set(new_token)
        self._refresh_web_qr()

    def _on_clear_token(self):
        if self._web_token_var:
            self._web_token_var.set("")
        self._refresh_web_qr()

    def _on_copy_url(self):
        url = self._web_url_label.cget("text") if self._web_url_label else ""
        if url and self._web_copy_btn:
            self._window.clipboard_clear()
            self._window.clipboard_append(url)
            self._web_copy_btn.configure(text=T("web.copied"))

            def _reset_copy_label():
                if self._web_copy_btn is not None:
                    try:
                        if self._web_copy_btn.winfo_exists():
                            self._web_copy_btn.configure(text=T("ui.copy"))
                    except Exception:
                        pass

            self._window.after(2000, _reset_copy_label)

    def _on_save_web(self):
        cfg = self._get_config()
        try:
            port = int(self._web_port_var.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            show_warning(self._window, T("dialog.invalid"), T("settings_window.port_invalid"))
            return

        try:
            limit = int(self._web_history_limit_var.get()) if self._web_history_limit_var else 5
            if not 1 <= limit <= 20:
                raise ValueError
        except ValueError:
            show_warning(self._window, T("dialog.invalid"), T("settings_window.val_web_history_limit"))
            return

        cfg.web_enabled = self._web_enabled_var.get()
        cfg.web_port = port
        cfg.web_history_limit = limit
        cfg.web_token = self._web_token_var.get()
        self._save_config()

        if self._status_label:
            self._status_label.configure(text=T("settings_window.web_saved"))
        msg = T("settings_window.web_saved") + "\n\n" + T("settings_window.web_restart_note")
        show_info(self._window, T("dialog.saved"), msg)

    # ═══════════════════════════════════════════════════════════════
    # Panel: Content Filter
    # ═══════════════════════════════════════════════════════════════

    def _build_filter_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")

        ctk.CTkLabel(
            panel, text=T("settings_window.filter_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(
            panel,
            text=T("settings_window.filter_desc"),
            font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
            justify="left",
        ).pack(anchor="w", pady=(0, 16))

        card = ctk.CTkFrame(panel, corner_radius=12)
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card, text=T("settings_window.filter_categories"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        active = self._get_filter_categories() if self._get_filter_categories else []

        for category in ALL_CATEGORIES:
            label = T(f"filter.{category}")
            var = tk.BooleanVar(value=category in active)
            self._filter_vars[category] = var
            ctk.CTkSwitch(
                card, text=label,
                variable=var,
                font=ctk.CTkFont(size=12),
            ).pack(anchor="w", padx=16, pady=(2, 6))

        # Spacer at bottom of card
        ctk.CTkFrame(card, height=8, fg_color="transparent").pack()

        ctk.CTkButton(
            panel, text=T("settings_window.save_filter"),
            width=200, height=36, command=self._on_save_filter,
        ).pack(anchor="w")

        return panel

    def _on_save_filter(self):
        enabled = [cat for cat in ALL_CATEGORIES if self._filter_vars.get(cat, tk.BooleanVar()).get()]
        if self._set_filter_categories:
            self._set_filter_categories(enabled)
        cfg = self._get_config()
        cfg.filter_enabled_categories = enabled
        self._save_config()
        show_info(self._window, T("dialog.saved"), T("settings_window.filter_saved"))

    # ═══════════════════════════════════════════════════════════════
    # Panel: Advanced
    # ═══════════════════════════════════════════════════════════════

    def _build_security_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")
        cfg = self._get_config()

        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(
            scroll, text=T("security.title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            scroll, text=T("settings_window.security_desc"),
            font=ctk.CTkFont(size=11), text_color=("gray50", "gray60"),
        ).pack(anchor="w", pady=(0, 14))

        # ── Card 1: Encryption toggle ──────────────────────────────
        card1 = ctk.CTkFrame(scroll, corner_radius=12)
        card1.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            card1, text=T("security.data_encryption"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        features = T("settings_window.security_features")
        for i, desc in enumerate(features):
            ctk.CTkLabel(
                card1, text=f"  {i+1}. {desc}",
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray70"),
                anchor="w", justify="left",
            ).pack(anchor="w", padx=20, pady=(2, 0))

        self._enc_enabled_var = tk.BooleanVar(value=cfg.encryption_enabled)
        ctk.CTkSwitch(
            card1, text=T("settings_window.enable_encryption"),
            variable=self._enc_enabled_var,
            font=ctk.CTkFont(size=13),
        ).pack(anchor="w", padx=16, pady=(14, 14))

        # ── Card 2: Pre-shared password ────────────────────────────
        card2 = ctk.CTkFrame(scroll, corner_radius=12)
        card2.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            card2, text=T("security.pre_shared_password"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))
        ctk.CTkLabel(
            card2,
            text=T("settings_window.password_hint"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
            anchor="w", justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 4))

        # Status indicator — password is never loaded from disk (hash only)
        _pw_status = T("security.password_set") if cfg.encryption_password_hash else T("security.no_password")
        self._enc_pw_status = ctk.CTkLabel(
            card2, text=_pw_status,
            font=ctk.CTkFont(size=11),
            text_color=("#27AE60", "#2ECC71") if cfg.encryption_password_hash else ("gray50", "gray60"),
        )
        self._enc_pw_status.pack(anchor="w", padx=20, pady=(0, 8))

        pw_row = ctk.CTkFrame(card2, fg_color="transparent")
        pw_row.pack(fill="x", padx=16, pady=(0, 14))
        # Password field always starts empty — plaintext never stored on disk
        self._enc_password_var = tk.StringVar(value="")
        self._enc_password_entry = ctk.CTkEntry(
            pw_row, textvariable=self._enc_password_var,
            height=32, width=200, show="*",
            placeholder_text=T("settings_window.password_placeholder"),
        )
        self._enc_password_entry.pack(side="left", padx=(0, 6))
        self._show_pw_btn = ctk.CTkButton(
            pw_row, text=T("settings_window.show"), width=50, height=32,
            fg_color="transparent", border_width=1,
            text_color=("gray50", "gray60"),
            border_color=("gray70", "gray40"),
            font=ctk.CTkFont(size=11),
            command=self._toggle_password_visibility,
        )
        self._show_pw_btn.pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            pw_row, text=T("settings_window.clear_password"), width=60, height=32,
            fg_color="transparent", border_width=1,
            text_color=("#E74C3C", "#C0392B"),
            border_color=("#E74C3C", "#C0392B"),
            hover_color=("#FADBD8", "#3C1A1A"),
            font=ctk.CTkFont(size=11),
            command=self._on_clear_password,
        ).pack(side="left")

        # ── Save button ──────────────────────────────────────────
        ctk.CTkButton(
            scroll, text=T("settings_window.save_security"),
            width=200, height=36, command=self._on_save_security,
        ).pack(anchor="w", pady=(4, 16))

        return panel

    def _toggle_password_visibility(self):
        if self._enc_password_entry.cget("show") == "*":
            self._enc_password_entry.configure(show="")
            self._show_pw_btn.configure(text=T("settings_window.hide"))
        else:
            self._enc_password_entry.configure(show="*")
            self._show_pw_btn.configure(text=T("settings_window.show"))

    def _on_clear_password(self):
        cfg = self._get_config()
        self._enc_password_var.set("")
        cfg.encryption_password = ""
        if cfg.encryption_password_hash:
            cfg.encryption_password_hash = ""
            self._enc_pw_status.configure(
                text=T("security.no_password"),
                text_color=("gray50", "gray60"),
            )
        self._save_config()
        if self._status_label:
            self._status_label.configure(text=T("settings_window.password_cleared"))

    def _on_save_security(self):
        cfg = self._get_config()
        enc_enabled = self._enc_enabled_var.get()
        new_password = self._enc_password_var.get().strip()
        if new_password:
            # A blank field leaves the stored password untouched; only a
            # newly typed password re-encrypts the private key.
            if not ask_yesno(
                self._window,
                T("security.title"),
                T("settings_window.password_change_confirm"),
            ):
                return
            cfg.encryption_password = new_password
            self._enc_password_var.set("")
        # Commit the encryption toggle only after any confirmation above
        # passed, so cancelling the password change never leaves the shared
        # in-memory config mutated for a later unrelated save.
        cfg.encryption_enabled = enc_enabled
        # Update status indicator
        if cfg.encryption_password_hash:
            self._enc_pw_status.configure(
                text=T("security.password_set"),
                text_color=("#27AE60", "#2ECC71"),
            )
        else:
            self._enc_pw_status.configure(
                text=T("security.no_password"),
                text_color=("gray50", "gray60"),
            )
        self._save_config()
        if self._status_label:
            self._status_label.configure(text=T("settings_window.security_saved"))

    def _build_advanced_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")
        cfg = self._get_config()

        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(
            scroll, text=T("settings_window.advanced_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            scroll, text=T("settings_window.advanced_hint"),
            font=ctk.CTkFont(size=11), text_color=("gray50", "gray60"),
        ).pack(anchor="w", pady=(0, 14))

        def _desc(parent, text):
            ctk.CTkLabel(
                parent, text=text, wraplength=420,
                font=ctk.CTkFont(size=11), text_color=("gray50", "gray60"),
                anchor="w", justify="left",
            ).pack(anchor="w", padx=16, pady=(0, 10))

        def _row(parent):
            r = ctk.CTkFrame(parent, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=(0, 2))
            return r

        # ── Card 1: Clipboard & Sync ──────────────────────────────
        card1 = ctk.CTkFrame(scroll, corner_radius=12)
        card1.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            card1, text=T("settings_window.clipboard_sync_section"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        r = _row(card1)
        ctk.CTkLabel(r, text=T("settings_window.history_max"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._history_max_var = tk.StringVar(value=str(cfg.history_max_entries))
        ctk.CTkEntry(r, textvariable=self._history_max_var,
                     width=80, height=32).pack(side="right")
        _desc(card1, T("settings_window.history_max_desc"))

        r = _row(card1)
        ctk.CTkLabel(r, text=T("settings_window.sync_debounce"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._sync_debounce_var = tk.StringVar(value=str(cfg.sync_debounce))
        ctk.CTkEntry(r, textvariable=self._sync_debounce_var,
                     width=80, height=32).pack(side="right")
        _desc(card1, T("settings_window.sync_debounce_desc"))

        r = _row(card1)
        ctk.CTkLabel(r, text=T("settings_window.poll_interval"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._poll_interval_var = tk.StringVar(value=str(cfg.clipboard_poll_interval))
        ctk.CTkEntry(r, textvariable=self._poll_interval_var,
                     width=80, height=32).pack(side="right")
        _desc(card1, T("settings_window.poll_interval_desc"))

        # ── Card 2: File Transfer ─────────────────────────────────
        card2 = ctk.CTkFrame(scroll, corner_radius=12)
        card2.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            card2, text=T("settings_window.file_transfer_section"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        ctk.CTkLabel(
            card2, text=T("settings_window.receive_dir"), anchor="w",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=16, pady=(0, 4))
        dir_row = ctk.CTkFrame(card2, fg_color="transparent")
        dir_row.pack(fill="x", padx=16, pady=(0, 2))
        self._file_receive_dir_var = tk.StringVar(value=cfg.file_receive_dir)
        ctk.CTkEntry(dir_row, textvariable=self._file_receive_dir_var,
                     height=32, placeholder_text="~/Downloads/ClipSync").pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            dir_row, text=T("ui.browse"), width=80, height=32,
            command=self._browse_receive_dir,
        ).pack(side="right")
        _desc(card2, T("settings_window.receive_dir_desc"))

        r = _row(card2)
        ctk.CTkLabel(r, text=T("settings_window.transfer_timeout"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._transfer_timeout_var = tk.StringVar(value=str(cfg.transfer_timeout))
        ctk.CTkEntry(r, textvariable=self._transfer_timeout_var,
                     width=80, height=32).pack(side="right")
        _desc(card2, T("settings_window.transfer_timeout_desc"))

        # ── Card 3: Connection ────────────────────────────────────
        card3 = ctk.CTkFrame(scroll, corner_radius=12)
        card3.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            card3, text=T("settings_window.connection_section"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        r = _row(card3)
        ctk.CTkLabel(r, text=T("settings_window.max_reconnect"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._max_reconnect_var = tk.StringVar(value=str(cfg.max_reconnect_attempts))
        ctk.CTkEntry(r, textvariable=self._max_reconnect_var,
                     width=80, height=32).pack(side="right")
        _desc(card3, T("settings_window.max_reconnect_desc"))

        # ── Card 4: Logging & Notifications ───────────────────────
        card4 = ctk.CTkFrame(scroll, corner_radius=12)
        card4.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            card4, text=T("settings_window.logging_section"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        r = _row(card4)
        ctk.CTkLabel(r, text=T("settings_window.log_level"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._log_level_var = tk.StringVar(value=cfg.log_level)
        ctk.CTkOptionMenu(
            r, variable=self._log_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR"],
            width=120, height=32,
        ).pack(side="right")

        # Language selector
        r = _row(card4)
        ctk.CTkLabel(r, text=T("settings.language"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._language_var = tk.StringVar(value=cfg.language)
        ctk.CTkOptionMenu(
            r, variable=self._language_var,
            values=available_locales(),
            width=120, height=32,
        ).pack(side="right")

        self._notifications_var = tk.BooleanVar(value=cfg.notifications_enabled)
        ctk.CTkSwitch(
            card4, text=T("settings_window.enable_notifications"),
            variable=self._notifications_var,
            font=ctk.CTkFont(size=13),
        ).pack(anchor="w", padx=16, pady=(8, 14))

        # ── Danger Zone ───────────────────────────────────────────
        danger_label = ctk.CTkLabel(
            scroll, text=T("settings_window.danger_zone"),
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=("#E74C3C", "#E74C3C"),
        )
        danger_label.pack(anchor="w", pady=(20, 4))
        ctk.CTkLabel(
            scroll, text=T("settings_window.danger_zone_desc"),
            font=ctk.CTkFont(size=11), text_color=("gray50", "gray60"),
        ).pack(anchor="w", pady=(0, 10))

        danger_frame = ctk.CTkFrame(scroll, corner_radius=12, border_width=1,
                                     border_color=("#E74C3C", "#C0392B"), fg_color="transparent")
        danger_frame.pack(fill="x", padx=0, pady=(0, 10))

        ctk.CTkButton(
            danger_frame, text=T("settings_window.factory_reset"),
            width=180, height=36, fg_color=("#7F8C8D", "#566573"),
            hover_color=("#95A5A6", "#7F8C8D"),
            command=self._on_factory_reset,
        ).pack(padx=16, pady=(4, 4))

        if self._on_quit:
            ctk.CTkButton(
                danger_frame, text=T("tray.quit"),
                width=180, height=36, fg_color=("#E74C3C", "#C0392B"),
                hover_color=("#C0392B", "#A93226"),
                command=self._on_quit_from_settings,
            ).pack(padx=16, pady=(4, 4))

        ctk.CTkButton(
            danger_frame, text=T("settings_window.restart_app"),
            width=180, height=36, fg_color=("#F39C12", "#E67E22"),
            hover_color=("#E67E22", "#D35400"),
            command=self._on_restart,
        ).pack(padx=16, pady=(4, 14))

        # ── Save button ──────────────────────────────────────────
        ctk.CTkButton(
            scroll, text=T("settings_window.save_advanced"),
            width=200, height=36, command=self._on_save_advanced,
        ).pack(anchor="w", pady=(4, 16))

        return panel

    def _browse_receive_dir(self):
        from pathlib import Path
        from tkinter import filedialog
        directory = filedialog.askdirectory(
            parent=self._window,
            title=T("settings_window.receive_dir"),
            initialdir=self._file_receive_dir_var.get() or str(Path.home() / "Downloads" / "ClipSync"),
        )
        if directory:
            self._file_receive_dir_var.set(directory)

    def _on_save_advanced(self):
        from pathlib import Path
        errors = []

        try:
            history_max = int(self._history_max_var.get())
            if not 10 <= history_max <= 1000:
                raise ValueError
        except ValueError:
            errors.append(T("settings_window.val_history_entries"))
            history_max = None

        try:
            debounce = float(self._sync_debounce_var.get())
            if not 0.1 <= debounce <= 5.0:
                raise ValueError
        except ValueError:
            errors.append(T("settings_window.val_sync_debounce"))
            debounce = None

        try:
            poll = float(self._poll_interval_var.get())
            if not 0.1 <= poll <= 5.0:
                raise ValueError
        except ValueError:
            errors.append(T("settings_window.val_poll_interval"))
            poll = None

        receive_dir = self._file_receive_dir_var.get().strip()
        if receive_dir:
            expanded = os.path.expanduser(receive_dir)
            if not Path(expanded).parent.exists():
                errors.append(T("settings_window.val_receive_dir"))

        try:
            timeout = float(self._transfer_timeout_var.get())
            if not 30 <= timeout <= 3600:
                raise ValueError
        except ValueError:
            errors.append(T("settings_window.val_transfer_timeout"))
            timeout = None

        try:
            max_reconnect = int(self._max_reconnect_var.get())
            if not 1 <= max_reconnect <= 100:
                raise ValueError
        except ValueError:
            errors.append(T("settings_window.val_max_reconnect"))
            max_reconnect = None

        if errors:
            show_warning(self._window, T("settings_window.validation_error"), "\n".join(errors))
            return

        cfg = self._get_config()
        cfg.history_max_entries = history_max
        cfg.file_receive_dir = receive_dir
        cfg.sync_debounce = debounce
        cfg.clipboard_poll_interval = poll
        cfg.max_reconnect_attempts = max_reconnect
        cfg.transfer_timeout = timeout
        cfg.log_level = self._log_level_var.get()
        cfg.language = self._language_var.get()
        cfg.notifications_enabled = self._notifications_var.get()
        # Apply the notification toggle live — no restart required.
        notification_mgr.enabled = cfg.notifications_enabled
        self._save_config()
        set_locale(cfg.language)

        if self._status_label:
            self._status_label.configure(text=T("footer.advanced_saved"))
        show_info(
            self._window,
            T("dialog.saved"),
            T("settings_window.advanced_saved"),
        )

    def _restart_app(self) -> None:
        import subprocess
        import sys

        from internal.config.config import _config_dir
        # Remove lock file so the new instance won't see "already running"
        try:
            (_config_dir() / ".lock").unlink()
        except Exception:
            pass
        # Spawn a new instance and exit. In a frozen (PyInstaller) build
        # sys.argv[0] equals sys.executable, so sys.argv[1:] avoids a stray
        # duplicate exe argument; from source argv[0] is the script path and
        # must be kept.
        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv
        try:
            subprocess.Popen(args)
        except Exception:
            pass
        # Tell the host not to re-save config on shutdown — after a factory
        # reset shutdown() would otherwise recreate config.json with the old
        # device identity/peers, silently undoing the reset.
        if self._set_skip_save_on_shutdown is not None:
            try:
                self._set_skip_save_on_shutdown(True)
            except Exception:
                logger.debug("Could not set skip-save-on-shutdown", exc_info=True)
        # Quit the Tk mainloop first, then exit — mirroring main.py's
        # _exit_process. sys.exit() straight from a Tk callback otherwise
        # surfaces a noisy _tkinter.TclError traceback.
        try:
            self._root.quit()
        except Exception:
            logger.debug("Could not quit Tk root cleanly", exc_info=True)
        sys.exit(0)

    def _on_quit_from_settings(self) -> None:
        if not _confirm_danger(
            self._window,
            T("tray.quit"),
            T("settings_window.quit_confirm"),
        ):
            return
        if self._on_quit:
            self._on_quit()

    def _on_restart(self) -> None:
        if not _confirm_danger(
            self._window,
            T("settings_window.restart_app"),
            T("settings_window.restart_confirm"),
        ):
            return
        self._save_config()
        self._restart_app()

    def _on_factory_reset(self) -> None:
        if not _confirm_danger(
            self._window,
            T("settings_window.factory_reset"),
            T("settings_window.factory_reset_confirm"),
        ):
            return
        from internal.config.config import _config_dir
        config_dir = _config_dir()
        deleted = []
        errors = []
        # Match the web path's factory reset (src/main.py _do_factory_reset):
        # remove the SQLite databases too so "deletes all data" is actually true.
        for fname in ["config.json", "clipboard_history.json",
                      "clipboard_history.db", "favorites.db", "clipsync.log"]:
            fpath = config_dir / fname
            try:
                if fpath.exists():
                    fpath.unlink()
                    deleted.append(fname)
            except OSError as e:
                errors.append(f"{fname}: {e}")
        # Clean up stale temp files
        for pattern in [".config_tmp_*.json", ".history_tmp_*.json"]:
            for tmpf in list(config_dir.glob(pattern)):
                try:
                    tmpf.unlink()
                except OSError:
                    pass
        if errors:
            show_warning(
                self._window, T("dialog.error"),
                T("settings_window.factory_reset_error") + "\n" + "\n".join(errors),
            )
        logger.info(
            "Factory reset: deleted %s from %s", deleted, str(config_dir),
        )
        self._restart_app()

    # ═══════════════════════════════════════════════════════════════
    # Panel: Logs
    # ═══════════════════════════════════════════════════════════════

    def _build_logs_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            header, text=T("settings_window.logs_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")

        btn_row = ctk.CTkFrame(header, fg_color="transparent")
        btn_row.pack(side="right")

        ctk.CTkButton(
            btn_row, text="⟳  " + T("ui.refresh"), width=90, height=30,
            fg_color="transparent", border_width=1,
            text_color=("gray40", "gray70"),
            border_color=("gray60", "gray50"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=11),
            command=lambda: self._refresh_log_text(self._log_text),
        ).pack(side="left", padx=(0, 6))

        if self._on_export_logs:
            ctk.CTkButton(
                btn_row, text="\U0001F4BE  " + T("ui.export_logs").rstrip("..."), width=80, height=30,
                font=ctk.CTkFont(size=11),
                command=self._on_export_logs,
            ).pack(side="left")

        card = ctk.CTkFrame(panel, corner_radius=12)
        card.pack(fill="both", expand=True)

        self._log_text = ctk.CTkTextbox(card, font=ctk.CTkFont(size=11), wrap="word")
        self._log_text.pack(fill="both", expand=True, padx=8, pady=8)
        self._refresh_log_text(self._log_text)

        return panel

    def _refresh_log_text(self, widget):
        if self._get_log_text:
            try:
                text = self._get_log_text()
                widget.configure(state="normal")
                widget.delete("1.0", "end")
                if not text:
                    text = T("settings_window.no_logs")
                widget.insert("1.0", text)
                widget.see("end")
                widget.configure(state="disabled")
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════
    # Panel: About
    # ═══════════════════════════════════════════════════════════════

    def _build_about_panel(self):
        panel = ctk.CTkFrame(self._content_frame, fg_color="transparent")

        center = ctk.CTkFrame(panel, fg_color="transparent")
        center.pack(expand=True, fill="both")

        ctk.CTkLabel(
            center, text="\U0001F4CB", font=ctk.CTkFont(size=40),
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            center, text="ClipSync",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            center, text=T("settings_window.about_version"),
            font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
        ).pack()

        ctk.CTkLabel(
            center,
            text=T("settings_window.about_desc"),
            font=ctk.CTkFont(size=13),
            justify="center",
        ).pack(pady=(20, 18))

        feat_card = ctk.CTkFrame(center, corner_radius=12,
                                fg_color=("gray95", "gray17"))
        feat_card.pack(fill="x", padx=20)

        feature_icons = ["✅", "\U0001F512", "\U0001F4C4", "⚡", "\U0001F6AB", "\U0001F4E4"]
        feature_texts = T("settings_window.about_features")
        for icon, desc in zip(feature_icons, feature_texts):
            row = ctk.CTkFrame(feat_card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=3)
            ctk.CTkLabel(row, text=icon, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 8))
            ctk.CTkLabel(row, text=desc, font=ctk.CTkFont(size=12)).pack(side="left")

        ctk.CTkButton(
            center, text=T("settings_window.show_data_folder"), width=200, height=34,
            fg_color="transparent", border_width=1,
            border_color=("#2980B9", "#3498DB"),
            text_color=("#2980B9", "#3498DB"),
            hover_color=("#D6EAF8", "#1A3A4A"),
            font=ctk.CTkFont(size=12),
            command=self._open_data_folder,
        ).pack(pady=(18, 20))

        return panel

    def _open_data_folder(self):
        """Open the config directory in the system file explorer."""
        import platform
        import subprocess

        from internal.config.config import _config_dir
        path = str(_config_dir())
        try:
            system = platform.system()
            if system == "Windows":
                os.startfile(path)
            elif system == "Darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as e:
            logger.warning("Failed to open data folder: %s", e)

    # ═══════════════════════════════════════════════════════════════
    # Theme toggle
    # ═══════════════════════════════════════════════════════════════

    def _toggle_theme(self):
        self._dark_mode = not self._dark_mode
        new_mode = "dark" if self._dark_mode else "light"
        ctk.set_appearance_mode(new_mode)
        # Keep the Appearance radio buttons in sync with the header toggle.
        if getattr(self, "_appearance_var", None) is not None:
            self._appearance_var.set(new_mode)
        self._theme_btn.configure(
            text=T("ui.theme_light") if self._dark_mode else T("ui.theme_dark")
        )
        # Persist to config so it survives restarts
        cfg = self._get_config()
        cfg.appearance_mode = new_mode
        self._save_config()
