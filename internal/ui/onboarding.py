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
        body, text="ClipSync",
        font=ctk.CTkFont(size=30, weight="bold"),
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
    cards: list[ctk.CTkFrame] = []
    focus_color = ("#0891B2", "#22D3EE")
    idle_color = ("gray75", "gray30")
    for code, native, other in LANGUAGE_OPTIONS:
        card = ctk.CTkFrame(
            body, corner_radius=12,
            fg_color=("gray92", "gray17"),
            border_width=1, border_color=idle_color,
            cursor="hand2",
        )
        card.pack(fill="x", pady=6)
        pick_cmd = lambda _e, c=code: _pick(c)
        # Clicks land on the child labels, not the frame, so bind the same
        # handler (and hand cursor) to every label inside the card too —
        # otherwise only the thin card margin responds.
        card.bind("<Button-1>", pick_cmd)
        lbl_native = ctk.CTkLabel(
            card, text=native,
            font=ctk.CTkFont(size=16, weight="bold"),
            cursor="hand2",
        )
        lbl_native.pack(pady=(14, 0))
        lbl_native.bind("<Button-1>", pick_cmd)
        lbl_other = ctk.CTkLabel(
            card, text=other,
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray60"),
            cursor="hand2",
        )
        lbl_other.pack(pady=(0, 14))
        lbl_other.bind("<Button-1>", pick_cmd)

        # Keyboard support: cards are focusable and selectable with
        # Enter/Space; Tab / Shift+Tab cycle between them.
        card.bind("<Return>", lambda _e, c=code: _pick(c))
        card.bind("<space>", lambda _e, c=code: _pick(c))
        card.bind(
            "<FocusIn>",
            lambda _e, c=card: c.configure(border_color=focus_color),
        )
        card.bind(
            "<FocusOut>",
            lambda _e, c=card: c.configure(border_color=idle_color),
        )
        cards.append(card)

    # ── Keyboard navigation between cards ─────────────────────────
    n = len(cards)

    def _focus(index):
        try:
            cards[index % n].focus_set()
        except Exception:
            pass

    def _on_tab(event):
        try:
            cur = cards.index(event.widget) if event.widget in cards else -1
        except Exception:
            cur = -1
        _focus(cur + 1)
        return "break"

    def _on_shift_tab(event):
        try:
            cur = cards.index(event.widget) if event.widget in cards else -1
        except Exception:
            cur = 0
        _focus(cur - 1)
        return "break"

    for card in cards:
        card.bind("<Tab>", _on_tab)
        card.bind("<Shift-Tab>", _on_shift_tab)

    # ── Footer hint ──────────────────────────────────────────────
    ctk.CTkLabel(
        body,
        text="选择后可在 设置 → 外观 中随时更改\nChangeable anytime in Settings → Appearance",
        font=ctk.CTkFont(size=11),
        text_color=("gray45", "gray60"),
        justify="center",
    ).pack(pady=(14, 0))

    # ── Modal behavior + Escape-to-dismiss ────────────────────────
    dlg.update()
    dlg.transient(parent)
    try:
        dlg.grab_set()
    except Exception:
        pass
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    if n:
        _focus(0)

    dlg.wait_window()
    return result[0]
