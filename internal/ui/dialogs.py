"""Themed dialog replacements for tkinter.messagebox.

Uses CTkToplevel for consistent look with the rest of the app.
"""

import customtkinter as ctk

from internal.i18n import T

# ── Icon + color scheme per dialog type ──────────────────────────
# Plain glyphs (not emoji) so they render everywhere, including Linux
# systems without an emoji font. They are shown large and colored.
_INFO_ICON = "i"
_WARN_ICON = "!"
_ERROR_ICON = "×"

_INFO_COLOR = "#0891B2"
_WARN_COLOR = "#F39C12"
_ERROR_COLOR = "#E74C3C"


def _system_prefers_dark() -> bool:
    """Best-effort OS-level dark-mode detection. Returns False on any failure.

    Windows: HKCU Personalize AppsUseLightTheme == 0 means dark.
    macOS:   ``defaults read -g AppleInterfaceStyle`` == "Dark".
    Linux:   gsettings ``color-scheme``/``gtk-theme`` containing "dark".
    """
    try:
        import sys
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            try:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(value) == 0
            finally:
                winreg.CloseKey(key)
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            return out == "Dark"
        # Linux (GTK/GNOME-based). Both keys are tried, then we bail.
        import subprocess
        for schema, key in (
            ("org.gnome.desktop.interface", "color-scheme"),
            ("org.gnome.desktop.interface", "gtk-theme"),
        ):
            try:
                out = subprocess.run(
                    ["gsettings", "get", schema, key],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip().lower()
                if "dark" in out:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _is_dark_mode(appearance_mode: str) -> bool:
    """Resolve an appearance-mode string ('system'|'light'|'dark') to a
    concrete boolean. 'system' follows the OS preference, defaulting to
    light when the OS setting cannot be detected."""
    if appearance_mode == "dark":
        return True
    if appearance_mode == "light":
        return False
    return _system_prefers_dark()


def _dialog(parent, title, message, icon, accent_color, buttons):
    """Shared dialog builder. Returns True if first button was clicked."""
    dlg = ctk.CTkToplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)

    # Position centered over parent, or screen-center if parent is withdrawn.
    # IMPORTANT: Do NOT call update_idletasks() or transient() before the
    # window is realized (geometry + update). On macOS, doing so with a
    # withdrawn parent prevents the window from ever becoming visible.
    w, h = 420, 180
    if parent.winfo_viewable():
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
    else:
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
    dlg.geometry(f"{w}x{h}+{x}+{y}")

    result = [False]

    # ── Content ──────────────────────────────────────────────
    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(24, 16))

    # Icon + title row
    title_row = ctk.CTkFrame(body, fg_color="transparent")
    title_row.pack(fill="x", pady=(0, 10))

    ctk.CTkLabel(
        title_row, text=icon, font=ctk.CTkFont(size=24, weight="bold"),
        text_color=accent_color,
    ).pack(side="left", padx=(0, 10))

    ctk.CTkLabel(
        title_row, text=title,
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color=accent_color,
    ).pack(side="left")

    # Message
    ctk.CTkLabel(
        body, text=message, justify="left",
        font=ctk.CTkFont(size=12),
        text_color=("gray30", "gray80"),
        wraplength=370,
    ).pack(fill="x")

    # ── Buttons ──────────────────────────────────────────────
    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(fill="x", padx=24, pady=(0, 20))

    default_btn = None
    for i, (label, color) in enumerate(buttons):
        if isinstance(color, tuple):
            # Tuple (light, dark) — derive a distinct hover for both modes
            hover = tuple(_darken(c, 0.12) for c in color)
        elif color == "transparent":
            hover = ("gray85", "gray25")
        elif color.startswith("#"):
            hover = _darken(color, 0.15)
        else:
            hover = color
        btn = ctk.CTkButton(
            btn_row, text=label, width=90, height=32,
            fg_color=color,
            hover_color=hover,
            font=ctk.CTkFont(size=12),
            command=lambda v=i: _close(v),
        )
        if i == 0:
            default_btn = btn
        btn.pack(side="right", padx=(6 if i > 0 else 0, 0))

    def _close(value):
        try:
            if dlg.winfo_exists():
                result[0] = (value == 0)
                dlg.destroy()
        except Exception:
            pass

    dlg.update()
    dlg.transient(parent)
    try:
        dlg.grab_set()
    except Exception:
        pass

    # Keyboard handling: Enter triggers the primary button, Escape the cancel
    # (last) button. Return "break" so a focused child widget can't also fire.
    try:
        dlg.bind("<Return>", lambda e: (_close(0), "break")[1])
        dlg.bind("<Escape>", lambda e: (_close(len(buttons) - 1), "break")[1])
        if default_btn is not None:
            default_btn.focus_force()
    except Exception:
        pass

    dlg.protocol("WM_DELETE_WINDOW", lambda: _close(1))
    dlg.wait_window()
    return result[0]


def _darken(hex_color, amount):
    """Darken a hex color by the given amount (0-1)."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = max(0, int(r * (1 - amount)))
    g = max(0, int(g * (1 - amount)))
    b = max(0, int(b * (1 - amount)))
    return f"#{r:02x}{g:02x}{b:02x}"


def show_info(parent, title, message):
    """Show an info dialog with an OK button."""
    return _dialog(parent, title, message, _INFO_ICON, _INFO_COLOR,
                   [(T("ui.ok"), _INFO_COLOR)])


def show_warning(parent, title, message):
    """Show a warning dialog with an OK button."""
    return _dialog(parent, title, message, _WARN_ICON, _WARN_COLOR,
                   [(T("ui.ok"), _WARN_COLOR)])


def show_error(parent, title, message):
    """Show an error dialog with an OK button."""
    return _dialog(parent, title, message, _ERROR_ICON, _ERROR_COLOR,
                   [(T("ui.ok"), _ERROR_COLOR)])


def ask_yesno(parent, title, message):
    """Show a confirmation dialog with Yes/No buttons. Returns True if Yes."""
    return _dialog(parent, title, message, _WARN_ICON, _WARN_COLOR,
                   [(T("ui.yes"), _INFO_COLOR), (T("ui.no"), ("gray65", "gray45"))])


def ask_string(parent, title, prompt, initial_value="", show=""):
    """Show a themed input dialog. Returns the entered string, or None if
    cancelled. Uses CTkEntry for consistent look. Pass show='*' for passwords."""
    import customtkinter as ctk

    dlg = ctk.CTkToplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)

    # See _dialog() for why we don't call update_idletasks() or transient() here.
    w, h = 400, 160
    if parent.winfo_viewable():
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
    else:
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
    dlg.geometry(f"{w}x{h}+{x}+{y}")

    result = [None]

    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(20, 12))

    ctk.CTkLabel(
        body, text=prompt, justify="left",
        font=ctk.CTkFont(size=12),
        text_color=("gray30", "gray80"),
        wraplength=350,
    ).pack(fill="x", pady=(0, 10))

    if show:
        # Use tkinter Entry for password masking (CTkEntry doesn't support show=)
        import tkinter as tk
        entry_var = tk.StringVar(value=initial_value)
        entry = tk.Entry(
            body, textvariable=entry_var, show=show,
            font=("TkDefaultFont", 13), relief="solid", borderwidth=1,
        )
        entry.pack(fill="x", ipady=6)
        entry.select_range(0, "end")
        entry.focus_set()
    else:
        entry_var = ctk.StringVar(value=initial_value)
        entry = ctk.CTkEntry(
            body, textvariable=entry_var, height=34,
            font=ctk.CTkFont(size=13),
        )
        entry.pack(fill="x")
        entry.select_range(0, "end")
        entry.focus_set()

    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(fill="x", padx=24, pady=(0, 20))

    default_btn = ctk.CTkButton(
        btn_row, text=T("ui.save"), width=90, height=32,
        fg_color=_INFO_COLOR,
        font=ctk.CTkFont(size=12),
        command=lambda: _on_close(entry_var.get()),
    )
    ctk.CTkButton(
        btn_row, text=T("ui.cancel"), width=90, height=32,
        fg_color="transparent", border_width=1,
        text_color=("gray40", "gray60"),
        border_color=("gray60", "gray50"),
        hover_color=("gray85", "gray25"),
        font=ctk.CTkFont(size=12),
        command=lambda: _on_close(None),
    ).pack(side="right", padx=(6, 0))
    default_btn.pack(side="right")

    # The entry-level bindings clear/commit immediately and return "break" so
    # the dialog-level bindings below don't also fire against a destroyed
    # dialog. Dialog-level bindings keep Return/Escape working even after the
    # user tabs/clicks onto a button.
    entry.bind("<Return>", lambda e: (_on_close(entry_var.get()), "break")[1])
    entry.bind("<Escape>", lambda e: (_on_close(None), "break")[1])

    def _on_close(value):
        try:
            if dlg.winfo_exists():
                result[0] = value
                dlg.destroy()
        except Exception:
            pass

    dlg.update()
    dlg.transient(parent)
    try:
        dlg.grab_set()
    except Exception:
        pass

    try:
        dlg.bind("<Return>", lambda e: (_on_close(entry_var.get()), "break")[1])
        dlg.bind("<Escape>", lambda e: (_on_close(None), "break")[1])
        default_btn.focus_force()
    except Exception:
        pass

    dlg.protocol("WM_DELETE_WINDOW", lambda: _on_close(None))
    dlg.wait_window()
    return result[0]
