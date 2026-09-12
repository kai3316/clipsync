"""Read model behind the transfers surfaces.

The active/history row shapes the panels render live here, so the phone
Companion's API and the desktop runtime's DTO read the same way — a file's size,
its live rate and the localized estimate of what is left are the same numbers on
the phone and in the window.

The *localized* estimate is the reason this is server-side: an ETA needs the
time units of the user's language, and only the translated catalog has those.
"""

from __future__ import annotations

from internal.i18n import T


def format_eta(seconds: float) -> str:
    """Format an ETA in seconds as a short localized string.

    Long transfers need an hours unit: without one, a two-hour estimate renders
    as "120m 0s".
    """
    if not seconds or seconds <= 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return T("transfer.eta_seconds", seconds=seconds)
    if seconds < 3600:
        return T("transfer.eta_minutes", minutes=seconds // 60, seconds=seconds % 60)
    return T("transfer.eta_hours", hours=seconds // 3600, minutes=(seconds % 3600) // 60)
