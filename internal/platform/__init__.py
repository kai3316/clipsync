"""Platform-specific utilities for ClipSync."""

import platform as _platform


def friendly_platform_name(system: str | None = None) -> str:
    """Return a user-facing platform name for a platform.system() value.

    platform.system() reports kernel/platform names — ``"Darwin"`` on macOS,
    ``"Windows"``, ``"Linux"`` — that are confusing in the UI: users expect
    the brand.  Maps the known set, passes anything else through unchanged.

    Order matters: ``"darwin"`` contains the substring ``"win"``, so the Mac
    branch must be checked before the Windows branch.
    """
    system = (system or _platform.system() or "").strip().lower()
    if "mac" in system or "darwin" in system:
        return "macOS"
    if "win" in system:
        return "Windows"
    if "linux" in system:
        return "Linux"
    if "android" in system:
        return "Android"
    if "ios" in system:
        return "iOS"
    return system or ""
