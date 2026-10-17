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


def decode_console_output(data) -> str:
    """Decode bytes captured from a Windows console tool (netsh, PowerShell…).

    ``subprocess``'s ``text=True`` decodes with the ANSI codepage, but these
    tools emit whatever the *console output* codepage is — UTF-8 on plenty of
    Windows 11 installs even where ``GetACP()`` still reports 936 — and
    querying the console CP is no help either: a windowed app has no console
    attached, so the answer does not describe the child process.

    Getting it wrong is not cosmetic.  The ANSI codec raised
    ``UnicodeDecodeError`` inside subprocess's reader THREAD, which printed a
    traceback and returned EMPTY output, so a failing netsh call reported no
    reason at all.  Forcing ``errors="replace"`` fixed the crash but surfaced
    the elevation error as mojibake: ``璇锋眰鐨勬搷浣�`` is UTF-8
    "请求的操作需要提升" read as GBK.

    UTF-8 is self-validating, which makes a strict attempt a reliable
    detector: real GBK text almost never decodes cleanly as UTF-8.  Fall back
    to the OEM then ANSI codepage (both Windows-only codecs, hence the
    LookupError guard), and finally to a lossy read that cannot raise.
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    for codec in ("utf-8", "oem", "mbcs"):
        try:
            return data.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")
