"""First-run onboarding: a bilingual language picker.

Shown once on the very first launch, before any UI language has been chosen.
Every label is shown in BOTH languages so a user who reads either one can
complete the step and see what each option means.
"""

import customtkinter as ctk

# (locale_code, native_name, other_language_name)
LANGUAGE_OPTIONS = [
    ("zh-CN", "简体中文", "Simplified Chinese"),
    ("en", "English", "英语"),
]


def show_language_onboarding(parent) -> str | None:
    """Show the bilingual first-run language picker as a modal dialog.

    Returns the chosen locale code, or ``None`` if the dialog was dismissed
    (the caller keeps the default language).  Never raises for UI reasons.
    """
    dlg = ctk.CTkToplevel(parent)
    dlg.title("选择语言  ·  Choose Language")
    dlg.resizable(False, False)

    w, h = 480, 400
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

    result: list[str | None] = [None]

    def _pick(code: str):
        result[0] = code
        try:
            dlg.destroy()
        except Exception:
            pass

    # Closing the window (X / Esc) dismisses without changing the language.
    dlg.protocol("WM_DELETE_WINDOW", lambda: dlg.destroy())

    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=28, pady=(26, 18))

    # ── Title ────────────────────────────────────────────────────
    ctk.CTkLabel(
        body, text="🌐",
        font=ctk.CTkFont(size=30),
    ).pack(pady=(0, 6))
    ctk.CTkLabel(
        body, text="选择语言 · Choose Language",
        font=ctk.CTkFont(size=20, weight="bold"),
    ).pack(pady=(0, 4))
    ctk.CTkLabel(
        body,
        text="首次使用 ClipSync，请选择界面语言\nPlease choose your interface language",
        font=ctk.CTkFont(size=12),
        text_color=("gray40", "gray60"),
        justify="center",
    ).pack(pady=(0, 18))

    # ── Language options (each shown in both languages) ──────────
    for code, native, other in LANGUAGE_OPTIONS:
        card = ctk.CTkFrame(
            body, corner_radius=12,
            fg_color=("gray92", "gray17"),
            border_width=1, border_color=("gray75", "gray30"),
            cursor="hand2",
        )
        card.pack(fill="x", pady=6)
        card.bind("<Button-1>", lambda _e, c=code: _pick(c))
        ctk.CTkLabel(
            card, text=native,
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(14, 0))
        ctk.CTkLabel(
            card, text=other,
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray60"),
        ).pack(pady=(0, 14))

    # ── Footer hint ──────────────────────────────────────────────
    ctk.CTkLabel(
        body,
        text="选择后可在 设置 → 外观 中随时更改\nChangeable anytime in Settings → Appearance",
        font=ctk.CTkFont(size=11),
        text_color=("gray45", "gray60"),
        justify="center",
    ).pack(pady=(14, 0))

    dlg.wait_window()
    return result[0]
