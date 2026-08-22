"""Platform-aware font resolution for the CustomTkinter UI.

CustomTkinter's default font family is hardcoded to "Roboto", which is not
installed on most macOS / Linux systems.  When the family is missing, Tk falls
back to its own dated default (Arial on Windows, the system default elsewhere),
which makes the CTk backend look visibly non-native next to the web UI.

This module resolves the best actually-installed UI font for the current
platform and provides :func:`make_font` so every CTkFont call site gets the
same family — while still honouring an explicit ``family=`` (e.g. the monospace
URL rows) and every other CTkFont keyword.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache

import customtkinter as ctk

# Preferred UI font families per platform, best-first.  The first family that
# is actually installed on the machine wins (checked via tkinter.font.families
# on the running Tk interpreter).  Fallbacks at the end of each list are the
# last-resort fonts that virtually every system ships.
_THEME_FILE_NAME = "clipsync.json"


def resource_path(*parts: str) -> str:
    """Resolve a bundled resource path, whether running from source or frozen.

    PyInstaller extracts bundled data into a temp dir exposed via
    ``sys._MEIPASS``; from source the file lives under the project root.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


def theme_file_path() -> str:
    """Return the absolute path to the bundled CustomTkinter theme JSON."""
    return resource_path("assets", "themes", _THEME_FILE_NAME)


_PLATFORM_FONTS: dict[str, list[str]] = {
    "darwin": [
        ".AppleSystemUIFont",
        "SF Pro Text",
        "Helvetica Neue",
        "PingFang SC",
        "Hiragino Sans GB",
        "Arial",
    ],
    "win32": [
        "Segoe UI",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Tahoma",
        "Arial",
    ],
    "linux": [
        "Noto Sans",
        "Ubuntu",
        "Cantarell",
        "DejaVu Sans",
        "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Liberation Sans",
        "Arial",
    ],
}


def _platform_key() -> str:
    """Return the platform bucket we use to pick a font list."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in ("win32", "cygwin"):
        return "win32"
    return "linux"


@lru_cache(maxsize=1)
def _installed_families() -> frozenset[str]:
    """Cached set of font families the running Tk interpreter knows about."""
    try:
        import tkinter.font as tkfont

        return frozenset(tkfont.families())
    except Exception:
        return frozenset()


def _tk_default_family() -> str:
    """Return Tk's own default font family (safe last resort)."""
    try:
        import tkinter.font as tkfont

        return str(tkfont.nametofont("TkDefaultFont").cget("family"))
    except Exception:
        return "TkDefaultFont"


def resolve_ui_font_family() -> str:
    """Return the best available UI font family for this platform."""
    candidates = _PLATFORM_FONTS.get(_platform_key(), _PLATFORM_FONTS["linux"])
    installed = _installed_families()
    for family in candidates:
        if family in installed:
            return family
    # None of the preferred families are installed: fall back to Tk's own
    # default family so we never point at a missing font name.
    return _tk_default_family()


def make_font(family: str | None = None, **kwargs) -> ctk.CTkFont:
    """Create a CTkFont using the platform UI font unless ``family`` is given.

    Every keyword (``size``, ``weight``, ``slant``, ...) is passed through to
    :class:`customtkinter.CTkFont`, so this is a drop-in replacement for
    ``ctk.CTkFont(...)``.
    """
    if family is None:
        family = resolve_ui_font_family()
    return ctk.CTkFont(family=family, **kwargs)


def configure_platform_font(root) -> None:
    """Align Tk's default font with the platform UI font.

    Called once at startup after the root window exists.  CTk widgets all go
    through :func:`make_font`; this also nudges the plain-Tk widgets embedded
    in CTk (e.g. the internal Entry) onto the same family and a saner size.
    """
    try:
        import tkinter.font as tkfont

        family = resolve_ui_font_family()
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family=family)
        root.option_add("*Font", default_font.name)
    except Exception:
        pass


_theme_done = False


def configure_platform_theme() -> None:
    """Apply the project's aurora CTk theme, once per process.

    CustomTkinter ships with a stock blue theme; this project's web UI uses a
    cyan→violet→pink palette, so the CTk backend should match.  Idempotent —
    safe to call from every window builder without reloading the theme JSON.
    """
    global _theme_done
    if _theme_done:
        return
    try:
        ctk.set_default_color_theme(theme_file_path())
        _theme_done = True
    except Exception:
        # Theme file missing (unpackaged dev tree) → keep stock blue.
        _theme_done = True


_install_done = False


def install_platform_font_patch() -> None:
    """Patch ``ctk.CTkFont`` so ``family=None`` resolves the platform UI font.

    CustomTkinter defaults every font without an explicit family to "Roboto",
    which is missing on most macOS / Linux installs.  Rather than touch all
    ~200 ``ctk.CTkFont(...)`` call sites, we wrap the class constructor once:
    when the caller passes no family (or ``family=None``), we inject the
    resolved platform family and forward everything else unchanged.  The class
    object is patched in place, so both our ``ctk.CTkFont(...)`` call sites and
    CustomTkinter's internal widget font creation pick it up.

    Safe to call more than once; the patch is applied only the first time.
    """
    global _install_done
    if _install_done:
        return

    try:
        _orig_init = ctk.CTkFont.__init__

        def _patched_init(self, family=None, *args, **kwargs):
            if family is None:
                family = resolve_ui_font_family()
            _orig_init(self, family, *args, **kwargs)

        ctk.CTkFont.__init__ = _patched_init
        _install_done = True
    except Exception:
        # A customtkinter version that renames/moves CTkFont must not break
        # startup — the UI just falls back to its stock "Roboto" default.
        _install_done = True
