"""Settings window for ClipSync — Network, Content Filter, and About.

This is a secondary window accessed from the dashboard. It contains
configuration that users rarely change: network settings, content
filtering preferences, and version information.
"""

import logging
import os
import sys
import threading
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from internal.clipboard.filter import ALL_CATEGORIES
from internal.i18n import T, available_locales, set_locale
from internal.platform.notify import notification_mgr
from internal.ui.dialogs import _is_dark_mode, ask_yesno, show_info, show_warning
from internal.web.server import WebServer

logger = logging.getLogger(__name__)


def _confirm_danger(parent, title: str, message: str) -> bool:
    """Show a confirmation dialog for dangerous actions. Returns True if confirmed."""
    return ask_yesno(parent, title, message)


def _strip_ascii_ellipsis(text: str) -> str:
    """Remove a trailing ASCII '...' only — never a unicode ellipsis '…'.

    ``rstrip("...")`` would strip any sequence of '.' characters and is
    locale-fragile (translated labels may end in '…' or other punctuation).
    """
    if text.endswith("..."):
        return text[:-3]
    return text


# Friendly names shown in the language dropdown.  The config still stores the
# locale code ("en", "zh-CN"); only the displayed label is human-readable.
_LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "en": "English",
    "zh-CN": "简体中文",
}


def _language_display_map() -> dict[str, str]:
    """Map each available locale code to its display name.

    Unknown locales (if any are added later) fall back to their raw code so
    the dropdown never shows an empty/incorrect label.
    """
    return {
        code: _LANGUAGE_DISPLAY_NAMES.get(code, code)
        for code in available_locales()
    }


# ── Window-geometry persistence ───────────────────────────────────────
# The settings window is DESTROYED on every close on every platform, so its
# size/position is otherwise lost each time.  These best-effort helpers store
# the geometry in a small sidecar JSON next to the config so a reopen (or a
# restart) shows the window where the user left it.  All I/O is guarded.

def _settings_state_path() -> str:
    try:
        from internal.config.config import _config_dir
        return str(_config_dir() / "settings_geometry.json")
    except Exception:
        return ""


def _save_settings_geometry(geom: str) -> None:
    if not geom:
        return
    path = _settings_state_path()
    if not path:
        return
    try:
        import json
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"settings_geometry": geom}, f)
    except Exception:
        logger.debug("Could not persist settings geometry", exc_info=True)


def _load_settings_geometry() -> str | None:
    path = _settings_state_path()
    if not path:
        return None
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        geom = data.get("settings_geometry") if isinstance(data, dict) else None
        return geom if isinstance(geom, str) else None
    except Exception:
        return None


def _settings_geometry_on_screen(geom: str, sw: int, sh: int) -> bool:
    """Return True if the saved geometry is at least partially on-screen."""
    import re
    m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geom)
    if not m:
        return False
    try:
        w, h = int(m.group(1)), int(m.group(2))
        x, y = int(m.group(3)), int(m.group(4))
    except ValueError:
        return False
    if w <= 0 or h <= 0:
        return False
    if x + w <= 0 or y + h <= 0 or x >= sw or y >= sh:
        return False
    return True


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
        on_theme_changed: Callable | None = None,
        on_web_action: Callable | None = None,
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
        self._on_theme_changed = on_theme_changed
        self._web_action_cb = on_web_action
        # Lets the host application suppress its config re-save during
        # shutdown() after a restart / factory reset (otherwise shutdown
        # would recreate config.json with the old settings).
        self._set_skip_save_on_shutdown = set_skip_save_on_shutdown

        self._window: ctk.CTkToplevel | None = None
        self._dark_mode = _is_dark_mode(get_config().appearance_mode)
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
        self._filter_vars: dict[str, tk.BooleanVar] = {}
        # Advanced panel vars
        self._history_max_var: tk.StringVar | None = None
        self._history_max_age_var: tk.StringVar | None = None
        self._plain_text_only_var: tk.BooleanVar | None = None
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

        # Settings search (filters the sidebar by every panel's visible text)
        self._search_var: tk.StringVar | None = None
        self._search_job: str | None = None
        self._search_query: str = ""
        self._search_entry: ctk.CTkEntry | None = None
        self._panel_texts: dict[str, list[str]] = {}
        self._nav_base_labels: dict[str, str] = {}
        self._nav_order: list[str] = []
        # (widget, original_text_color) pairs recolored while a query matches
        self._search_highlighted: list[tuple[object, object]] = []

    # ═══════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════

    def _flash_topmost(self) -> None:
        """Raise the window above others briefly (best-effort).

        The un-flash runs via ``after(200, ...)``; a user who closes the
        window inside that window used to leave a callback touching a
        destroyed widget (TclError noise in the Tk callback handler).
        """
        if self._window is None:
            return
        try:
            self._window.attributes("-topmost", True)

            def _unflash(w=self._window):
                try:
                    if w.winfo_exists():
                        w.attributes("-topmost", False)
                except Exception:
                    pass

            self._window.after(200, _unflash)
        except tk.TclError:
            pass

    def show(self):
        if self._window is not None:
            try:
                self._window.deiconify()
                self._window.lift()
                self._window.focus_force()
                self._window.update_idletasks()
                self._flash_topmost()
                if self._window.winfo_viewable():
                    self._switch_panel(self._current_panel)
                    return
                self._window.destroy()
                self._window = None
            except tk.TclError:
                self._window = None

        logger.info("Opening ClipSync settings")
        # Re-read the appearance mode each time the window is shown so a
        # "system" mode follows OS changes and external theme changes are
        # picked up on reopen.
        self._dark_mode = _is_dark_mode(self._get_config().appearance_mode)
        ctk.set_appearance_mode("dark" if self._dark_mode else "light")
        from internal.ui.fonts import configure_platform_theme

        configure_platform_theme()

        self._window = ctk.CTkToplevel(self._root)
        self._window.title(T("settings_window.title"))
        self._window.geometry("740x620")
        self._window.minsize(680, 560)
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)

        # Keyboard shortcuts: Escape closes the settings window; ⌘W/Ctrl+W
        # closes it on macOS / other platforms.  Escape is a no-op while an
        # Entry is focused (it would otherwise destroy the window and discard
        # unsaved edits in a text field — e.g. the web token or port).
        self._window.bind("<Escape>", lambda _e: self._on_escape())
        if sys.platform == "darwin":
            self._window.bind("<Command-w>", lambda _e: self._on_close())
        else:
            self._window.bind("<Control-w>", lambda _e: self._on_close())

        self._window.update_idletasks()
        sw = self._window.winfo_screenwidth()
        sh = self._window.winfo_screenheight()

        # Restore the user's last window geometry, falling back to a centered
        # HiDPI-scaled default.
        saved = _load_settings_geometry()
        if saved and _settings_geometry_on_screen(saved, sw, sh):
            try:
                self._window.geometry(saved)
            except tk.TclError:
                saved = None
        if not saved:
            scale = 1.0
            try:
                from internal.ui.fonts import compute_ui_scale
                scale = compute_ui_scale(self._root) or 1.0
            except Exception:
                pass
            # Pass the LOGICAL size — CTk's set_window_scaling converts it to
            # physical pixels — but center using the PHYSICAL size.
            lw, lh = 740, 620
            pw = max(680, int(lw * scale))
            ph = max(560, int(lh * scale))
            x = max(0, (sw - pw) // 2)
            y = max(0, (sh - ph) // 2)
            self._window.geometry(f"{lw}x{lh}+{x}+{y}")

        self._build_ui()
        self._switch_panel("network")

        self._flash_topmost()

    def _on_close(self):
        if self._window is not None:
            # Persist geometry before destroying so a reopen (or restart)
            # shows the window where the user left it.  Use CTk's getter
            # (reverse-scaled LOGICAL geometry) so restoring through
            # CTkToplevel.geometry() doesn't double-apply set_window_scaling.
            try:
                geom = self._window.geometry()
                if geom:
                    _save_settings_geometry(geom)
            except Exception:
                logger.debug("Could not capture settings geometry", exc_info=True)
            if self._search_job is not None:
                try:
                    self._root.after_cancel(self._search_job)
                except Exception:
                    pass
                self._search_job = None
            self._window.destroy()
            self._window = None
            self._sidebar_buttons.clear()
            self._panels.clear()
            self._filter_vars.clear()
            # Widget references held by the search state die with the window.
            self._search_highlighted = []
            self._panel_texts = {}
        if self._on_closed is not None:
            self._on_closed()

    def _on_escape(self):
        """Close the settings window on Escape, unless focus is in a text
        field (Entry / Textbox) — there Escape should do nothing so unsaved
        edits in the web token, port, etc. are not silently discarded."""
        if self._window is None:
            return
        try:
            focused = self._window.focus_get()
            if focused is not None and isinstance(
                focused, (ctk.CTkEntry, ctk.CTkTextbox, tk.Entry, tk.Text)
            ):
                return
        except Exception:
            pass
        self._on_close()

    # ═══════════════════════════════════════════════════════════════
    # UI construction
    # ═══════════════════════════════════════════════════════════════

    def _build_ui(self):
        outer = ctk.CTkFrame(self._window, fg_color="transparent")
        outer.pack(fill="both", expand=True)

        # Header — aurora theme (deep cyan / void, matching the web UI)
        header = ctk.CTkFrame(outer, corner_radius=0, fg_color=("#0891B2", "#0A0E1E"))
        header.pack(fill="x")
        h_inner = ctk.CTkFrame(header, fg_color="transparent")
        h_inner.pack(fill="x", padx=20, pady=(14, 14))

        ctk.CTkLabel(
            h_inner, text=T("ui.settings"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=("#FFFFFF", "#EAF0FA"),
        ).pack(side="left")

        self._theme_btn = ctk.CTkButton(
            h_inner, text=T("ui.theme_dark") if not self._dark_mode else T("ui.theme_light"),
            width=90, height=32, fg_color="transparent",
            border_width=1, border_color=("#A78BFA", "#2A3557"),
            text_color=("#FFFFFF", "#EAF0FA"),
            hover_color=("#7C3AED", "#161C38"),
            command=self._toggle_theme,
        )
        self._theme_btn.pack(side="right")

        # ── Settings search ─────────────────────────────────────────
        # Typing filters the sidebar to sections whose visible settings
        # mention the query; Enter jumps to the first matching section and
        # matches inside the current panel are highlighted in accent cyan.
        search_frame = ctk.CTkFrame(h_inner, fg_color="transparent")
        # Packed side="right" AFTER the theme button → sits to its left.
        search_frame.pack(side="right", padx=(0, 12))
        self._search_var = tk.StringVar()
        self._search_entry = ctk.CTkEntry(
            search_frame, textvariable=self._search_var,
            width=210, height=32,
            placeholder_text=T("settings_window.search_placeholder"),
        )
        self._search_entry.pack(side="left")
        ctk.CTkButton(
            search_frame, text="✕", width=24, height=24,
            fg_color="transparent",
            text_color=("gray50", "gray60"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=11),
            command=self._on_search_clear,
        ).pack(side="left", padx=(4, 0))
        self._search_entry.bind("<KeyRelease>", self._on_search_keyrelease)
        self._search_entry.bind("<Return>", self._on_search_jump)
        # Escape clears the query instead of closing the whole window —
        # "break" stops the toplevel Escape handler from firing.
        self._search_entry.bind("<Escape>", lambda _e: (self._on_search_clear(), "break")[1])

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

        # Snapshot every panel's visible text so the search box can match
        # against real (localized) setting labels.
        self._collect_search_index()

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
            self._nav_base_labels[key] = label
            self._nav_order.append(key)

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
                    fg_color=("#0891B2", "#0E1328"),
                    text_color=("#FFFFFF", "#EAF0FA"),
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color=("gray30", "gray80"),
                )
        self._current_panel = key
        if key == "logs":
            self._refresh_log_text(self._log_text)
        # An active search recolors the sidebar buttons; the switch above
        # just reset them all to default styling, so re-apply the filter.
        if getattr(self, "_search_query", ""):
            self._apply_search_state(self._search_query)

    # ═══════════════════════════════════════════════════════════════
    # Settings search
    # ═══════════════════════════════════════════════════════════════

    def _collect_search_index(self) -> None:
        """Snapshot each panel's visible text for the settings search."""
        self._panel_texts = {}
        for key, panel in self._panels.items():
            self._panel_texts[key] = self._collect_widget_texts(panel)

    @staticmethod
    def _collect_widget_texts(widget) -> list[str]:
        """Collect every descendant widget's text label (best-effort).

        A widget that already exposes a ``text`` stops the descent: CTk
        composites (CTkLabel, CTkButton, …) mirror their own text onto
        private internal widgets, and descending into them double-counts
        every label in the search index.
        """
        texts: list[str] = []
        try:
            children = widget.winfo_children()
        except Exception:
            return texts
        for child in children:
            yielded_text = False
            try:
                t = child.cget("text")
                if isinstance(t, str) and t.strip():
                    texts.append(t)
                    yielded_text = True
            except Exception:
                pass  # widgets without a text attribute are descended into
            if not yielded_text:
                texts.extend(SettingsWindow._collect_widget_texts(child))
        return texts

    def _search_counts(self, query: str) -> dict[str, int]:
        """Map panel-key → number of matching visible labels.

        Matching is a case-insensitive substring over the localized text the
        user actually sees (collected at build time), so both English and
        Chinese queries work without any extra keyword table.
        """
        q = (query or "").strip().casefold()
        if not q:
            return {}
        counts: dict[str, int] = {}
        for key, texts in getattr(self, "_panel_texts", {}).items():
            n = sum(1 for t in texts if q in t.casefold())
            if n:
                counts[key] = n
        return counts

    def _on_search_keyrelease(self, _event=None) -> None:
        if self._search_job is not None:
            try:
                self._root.after_cancel(self._search_job)
            except Exception:
                pass
            self._search_job = None

        def _run():
            self._search_job = None
            query = self._search_var.get() if self._search_var is not None else ""
            self._apply_search_state(query)

        self._search_job = self._root.after(250, _run)

    def _on_search_jump(self, _event=None) -> str:
        """Enter: apply immediately and open the first matching section."""
        if self._search_job is not None:
            try:
                self._root.after_cancel(self._search_job)
            except Exception:
                pass
            self._search_job = None
        query = self._search_var.get() if self._search_var is not None else ""
        self._apply_search_state(query)
        counts = self._search_counts(query)
        for key in getattr(self, "_nav_order", []):
            if key in counts:
                self._switch_panel(key)
                break
        return "break"

    def _on_search_clear(self) -> None:
        if self._search_job is not None:
            try:
                self._root.after_cancel(self._search_job)
            except Exception:
                pass
            self._search_job = None
        if self._search_var is not None:
            self._search_var.set("")
        self._apply_search_state("")

    def _apply_search_state(self, query: str) -> None:
        """Render the sidebar badges/dimming + in-panel highlights."""
        self._clear_search_highlight()
        q = (query or "").strip()
        if not q:
            self._search_query = ""
            self._restore_nav_labels()
            self._set_search_status(None, "")
            return
        self._search_query = q
        counts = self._search_counts(q)
        accent = ("#0891B2", "#22D3EE")
        dim = ("gray60", "gray45")
        matched = set(counts)
        for key, btn in self._sidebar_buttons.items():
            base = self._nav_base_labels.get(key, "")
            try:
                if key in matched:
                    btn.configure(
                        text=f"{base}  ·  {counts[key]}",
                        text_color=accent,
                    )
                else:
                    btn.configure(text=base, text_color=dim)
            except Exception:
                pass
        # Highlight matching labels inside the panel on screen.
        panel = self._panels.get(self._current_panel)
        if panel is not None:
            self._highlight_matches_in_panel(panel, q)
        self._set_search_status(len(counts), q)

    def _highlight_matches_in_panel(self, panel, query: str) -> None:
        """Recolor matching CTkLabels/Buttons; originals kept for restore."""
        import customtkinter as _ctk

        for w in self._iter_widget_tree(panel):
            if not isinstance(w, (_ctk.CTkLabel, _ctk.CTkButton)):
                continue
            try:
                text = w.cget("text")
                if not isinstance(text, str):
                    continue
                if query.casefold() not in text.casefold():
                    continue
                original = w.cget("text_color")
            except Exception:
                continue
            try:
                w.configure(text_color=("#0891B2", "#22D3EE"))
            except Exception:
                continue
            self._search_highlighted.append((w, original))

    @staticmethod
    def _iter_widget_tree(widget):
        try:
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            yield child
            yield from SettingsWindow._iter_widget_tree(child)

    def _clear_search_highlight(self) -> None:
        for w, original in getattr(self, "_search_highlighted", []):
            try:
                if w.winfo_exists():
                    w.configure(text_color=original)
            except Exception:
                pass
        self._search_highlighted = []

    def _nav_default_style(self, key: str) -> dict:
        """Default sidebar styling for *key* (mirrors _switch_panel)."""
        if key == self._current_panel:
            return {
                "text": self._nav_base_labels.get(key, ""),
                "fg_color": ("#0891B2", "#0E1328"),
                "text_color": ("#FFFFFF", "#EAF0FA"),
            }
        return {
            "text": self._nav_base_labels.get(key, ""),
            "fg_color": "transparent",
            "text_color": ("gray30", "gray80"),
        }

    def _restore_nav_labels(self) -> None:
        for key, btn in self._sidebar_buttons.items():
            try:
                btn.configure(**self._nav_default_style(key))
            except Exception:
                pass

    def _set_search_status(self, count: int | None, query: str) -> None:
        if self._status_label is None:
            return
        try:
            if count is None:
                self._status_label.configure(text=T("footer.ready"))
            elif count:
                self._status_label.configure(text=T(
                    "settings_window.search_matches", count=count, query=query))
            else:
                self._status_label.configure(text=T(
                    "settings_window.search_no_matches", query=query))
        except Exception:
            pass

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
        self._dark_mode = _is_dark_mode(mode)
        # Update header theme button
        if hasattr(self, '_theme_btn') and self._theme_btn:
            self._theme_btn.configure(
                text=T("ui.theme_light") if self._dark_mode else T("ui.theme_dark")
            )
        cfg = self._get_config()
        cfg.appearance_mode = mode
        self._save_config()
        self._notify_theme_changed(mode)
        try:
            self._status_label.configure(text=T("footer.settings_saved"))
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════
    # Panel: Web Companion
    # ═══════════════════════════════════════════════════════════════

    def _web_lan_ip(self) -> str:
        """Return the cached LAN IP for the web panel (never blocks).

        ``_get_lan_ip()`` runs ``getaddrinfo`` on the hostname; a slow
        resolver must not stall the settings window, so the probe happens on
        a background thread (see ``_ensure_web_lan_ip``) and this only reads
        the cache.  A 30 s TTL matches the dashboard's web card.
        """
        self._ensure_web_lan_ip()
        return getattr(self, "_lan_ip_val", "")

    def _ensure_web_lan_ip(self) -> None:
        """Kick a background LAN-IP lookup when the cached value is stale.

        Mirrors the dashboard's ``_ensure_web_lan_ip`` pattern: probe off the
        UI thread, backfill via ``root.after`` so widgets are only ever
        touched on the main thread.  A 30 s TTL means reopening settings
        reuses the cached IP instead of calling getaddrinfo again.
        """
        import time

        now = time.monotonic()
        if (now - getattr(self, "_lan_ip_ts", 0.0)) <= 30.0 and getattr(
            self, "_lan_ip_val", ""
        ):
            return  # fresh enough
        if getattr(self, "_lan_ip_probing", False):
            return  # a lookup is already in flight
        self._lan_ip_probing = True

        def _worker():
            try:
                ip = WebServer._get_lan_ip()
            except Exception:
                ip = "127.0.0.1"
            self._lan_ip_val = ip
            self._lan_ip_ts = time.monotonic()
            self._lan_ip_probing = False
            try:
                self._root.after(0, self._apply_web_lan_ip)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="settings-lan-ip").start()

    def _apply_web_lan_ip(self) -> None:
        """Repaint the web IP / QR once a fresh LAN IP arrives (main thread).

        Runs via ``root.after`` from a worker, so it can fire after the
        settings window was destroyed — guard every widget touch.
        """
        try:
            label = getattr(self, "_web_ip_label", None)
            if label is not None:
                try:
                    label.configure(text=self._web_lan_ip())
                except Exception:
                    pass
            self._refresh_web_qr()
        except Exception:
            logger.debug("Could not repaint web IP with fresh LAN IP",
                         exc_info=True)

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
            card2, text=self._web_lan_ip() or T("network.detecting"),
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._web_ip_label.pack(anchor="w", padx=16, pady=(0, 8))

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
            url_row, text="", font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
            wraplength=440, anchor="w", justify="left",
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
            ip = self._web_lan_ip()
            if not ip:
                # First lookup still in flight — keep a neutral placeholder and
                # let _apply_web_lan_ip repaint once a real IP is known (never
                # render a URL/QR from an empty host).
                if self._web_url_label:
                    self._web_url_label.configure(text=T("network.detecting"))
                self._web_qr_label.configure(
                    image=None,
                    text=T("network.detecting"),
                    font=ctk.CTkFont(size=11),
                    text_color=("gray50", "gray60"),
                )
                return
            # A phone scans this QR, so point at the lightweight phone companion
            # page — consistent with the desktop overview card and the tray
            # "Web QR" dialog, instead of the full desktop dashboard.
            url = (
                f"http://{ip}:{port}/mobile.html?token={token}"
                if token else f"http://{ip}:{port}/mobile.html"
            )

            if self._web_url_label:
                display_url = url if len(url) <= 60 else url[:57] + "..."
                self._web_url_label.configure(text=display_url)

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
            # Match the server API's accepted range (1-500); the previous
            # 1-20 cap couldn't even save the default of 30.
            if not 1 <= limit <= 500:
                raise ValueError
        except ValueError:
            show_warning(self._window, T("dialog.invalid"), T("settings_window.val_web_history_limit"))
            return

        cfg.web_enabled = self._web_enabled_var.get()
        cfg.web_port = port
        cfg.web_history_limit = limit
        cfg.web_token = self._web_token_var.get()
        self._save_config()

        if self._web_action_cb is not None:
            # Apply immediately (mirror the dashboard web card and web-settings
            # paths) instead of telling the user a restart is needed: port /
            # token / history-limit changes need a restart of the server, so
            # restart when enabling, stop when disabling.
            self._web_action_cb({"action": "restart" if cfg.web_enabled else "stop"})
            show_info(self._window, T("dialog.saved"), T("settings_window.web_saved"))

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
        ctk.CTkLabel(r, text=T("settings_window.history_max_age"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._history_max_age_var = tk.StringVar(
            value=str(cfg.history_max_age_days))
        ctk.CTkEntry(r, textvariable=self._history_max_age_var,
                     width=80, height=32).pack(side="right")
        _desc(card1, T("settings_window.history_max_age_desc"))

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

        # Plain-text-only toggle — mirrors the clipboard panel's config flag.
        self._plain_text_only_var = tk.BooleanVar(value=bool(cfg.plain_text_only))
        ctk.CTkSwitch(
            card1, text=T("settings_window.plain_text_only"),
            variable=self._plain_text_only_var,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=16, pady=(8, 2))
        _desc(card1, T("settings_window.plain_text_only_desc"))

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

        # Language selector — shows friendly display names ("English",
        # "简体中文") while the config stores the locale code ("en", "zh-CN").
        r = _row(card4)
        ctk.CTkLabel(r, text=T("settings.language"), anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        _lang_display = _language_display_map()
        self._language_var = tk.StringVar(
            value=_lang_display.get(cfg.language, cfg.language))
        ctk.CTkOptionMenu(
            r, variable=self._language_var,
            values=list(_lang_display.values()),
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
            # Age-based retention in days; 0 disables it. Range matches the
            # web settings API so both UIs accept the same values.
            max_age_days = float(self._history_max_age_var.get())
            if not 0 <= max_age_days <= 36500:
                raise ValueError
        except ValueError:
            errors.append(T("settings_window.val_history_max_age"))
            max_age_days = None

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
        cfg.history_max_age_days = max_age_days
        cfg.plain_text_only = self._plain_text_only_var.get()
        cfg.file_receive_dir = receive_dir
        cfg.sync_debounce = debounce
        cfg.clipboard_poll_interval = poll
        cfg.max_reconnect_attempts = max_reconnect
        cfg.transfer_timeout = timeout
        cfg.log_level = self._log_level_var.get()
        # The dropdown holds a display name; translate it back to a locale code.
        _lang_display = _language_display_map()
        _code_by_name = {name: code for code, name in _lang_display.items()}
        chosen = self._language_var.get()
        cfg.language = _code_by_name.get(chosen, chosen)
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
                btn_row, text="\U0001F4BE  " + _strip_ascii_ellipsis(T("ui.export_logs")), width=80, height=30,
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

        # Auto-update-check switch — persists immediately (this panel has no
        # save button).  The periodic loop re-reads the config every tick, so
        # flipping it takes effect at once.
        self._auto_update_var = tk.BooleanVar(
            value=getattr(self._get_config(), "auto_update_check", True))
        ctk.CTkSwitch(
            center, text=T("settings_window.auto_update_check"),
            variable=self._auto_update_var,
            command=self._on_toggle_auto_update,
            font=ctk.CTkFont(size=13),
        ).pack()
        ctk.CTkLabel(
            center,
            text=T("settings_window.auto_update_check_hint"),
            font=ctk.CTkFont(size=11),
            justify="center",
            text_color=("gray50", "gray60"),
        ).pack(padx=30, pady=(2, 12))

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
            border_color=("#0891B2", "#22D3EE"),
            text_color=("#0891B2", "#4CE0F5"),
            hover_color=("#D6F0F8", "#161C38"),
            font=ctk.CTkFont(size=12),
            command=self._open_data_folder,
        ).pack(pady=(18, 20))

        return panel

    def _on_toggle_auto_update(self):
        """Persist the About-panel auto-update-check switch immediately."""
        try:
            cfg = self._get_config()
            cfg.auto_update_check = bool(self._auto_update_var.get())
            self._save_config()
        except Exception:
            logger.debug("Could not persist auto_update_check", exc_info=True)

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
        self._notify_theme_changed(new_mode)

    def _notify_theme_changed(self, new_mode: str):
        """Let the host (main.py) refresh an already-open dashboard's theme."""
        if self._on_theme_changed is not None:
            try:
                self._on_theme_changed(new_mode)
            except Exception:
                logger.debug("theme-changed callback failed", exc_info=True)
