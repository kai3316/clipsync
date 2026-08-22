"""Auto-start on boot functionality for ClipSync.

Provides cross-platform support for enabling, disabling, and checking
the "launch on login" / "start on boot" behaviour of ClipSync.

Platform support:
    - Windows  : Registry Run key (HKCU)
    - macOS    : LaunchAgent plist
    - Linux    : XDG autostart .desktop file
"""

from __future__ import annotations

import os
import platform as _platform
import sys

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_executable_info():
    """Return ``(executable_path, arguments)`` for the auto-start command.

    When the application is bundled with PyInstaller (``sys.frozen`` is set),
    ``sys.executable`` points to the packaged executable and no extra
    arguments are needed.

    When running from source, ``sys.executable`` is the Python interpreter
    and ``sys.argv[0]`` gives the entry-point script path.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller / cx_Freeze / py2app bundled executable
        return os.path.abspath(sys.executable), []

    # Running from source – build the command as ``python <script>``
    python = sys.executable
    script = os.path.abspath(sys.argv[0])
    return python, [script]


def _get_display_name():
    """Human-readable name used inside registry / plist / .desktop entries."""
    return "ClipSync"


# ---------------------------------------------------------------------------
# Windows (Registry Run key)
# ---------------------------------------------------------------------------

def _enable_windows():
    """Create ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\ClipSync``."""
    import winreg

    exe, args = _get_executable_info()
    # Build a properly quoted command line
    parts = [f'"{exe}"'] + [f'"{a}"' for a in args]
    command = " ".join(parts)

    key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    try:
        winreg.SetValueEx(key, _get_display_name(), 0, winreg.REG_SZ, command)
    finally:
        key.Close()


def _disable_windows():
    """Remove the ClipSync value from the Run registry key."""
    import winreg

    try:
        key = winreg.OpenKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
    except FileNotFoundError:
        return  # nothing to remove

    try:
        winreg.DeleteValue(key, _get_display_name())
    except FileNotFoundError:
        pass  # value didn't exist – already clean
    finally:
        key.Close()


def _is_enabled_windows():
    """Return ``True`` if the Run registry value exists."""
    import winreg

    try:
        key = winreg.OpenKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
    except FileNotFoundError:
        return False

    try:
        winreg.QueryValueEx(key, _get_display_name())
        return True
    except FileNotFoundError:
        return False
    finally:
        key.Close()


# ---------------------------------------------------------------------------
# macOS (LaunchAgent plist)
# ---------------------------------------------------------------------------

def _plist_path():
    """Absolute path to the LaunchAgent plist."""
    return os.path.expanduser(
        "~/Library/LaunchAgents/com.clipsync.plist"
    )


def _enable_macos():
    """Create a LaunchAgent plist that starts ClipSync on login."""
    exe, args = _get_executable_info()
    # ProgramArguments must be an array of strings
    program_args = [exe] + args

    # Build a minimal XML plist by hand (no plistlib dependency needed)
    args_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in program_args)
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.clipsync</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
"""
    os.makedirs(os.path.dirname(_plist_path()), exist_ok=True)
    with open(_plist_path(), "w", encoding="utf-8") as f:
        f.write(plist_content)


def _disable_macos():
    """Remove the LaunchAgent plist file."""
    path = _plist_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _is_enabled_macos():
    """Return ``True`` if the LaunchAgent plist exists."""
    return os.path.isfile(_plist_path())


def _xml_escape(text: str) -> str:
    """Escape a string for inclusion in XML text content."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ---------------------------------------------------------------------------
# Linux (XDG autostart .desktop file)
# ---------------------------------------------------------------------------

def _desktop_path():
    """Absolute path to the XDG autostart .desktop file."""
    return os.path.expanduser(
        "~/.config/autostart/clipsync.desktop"
    )


def _enable_linux():
    """Create an XDG autostart .desktop entry."""
    exe, args = _get_executable_info()
    # The Exec key expects a single command string
    command = " ".join([exe] + args)

    desktop_entry = f"""[Desktop Entry]
Type=Application
Name={_get_display_name()}
Comment=Cross-platform clipboard sharing
Exec={command}
Terminal=false
X-GNOME-Autostart-enabled=true
"""

    os.makedirs(os.path.dirname(_desktop_path()), exist_ok=True)
    with open(_desktop_path(), "w", encoding="utf-8") as f:
        f.write(desktop_entry)


def _disable_linux():
    """Remove the XDG autostart .desktop file."""
    path = _desktop_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _is_enabled_linux():
    """Return ``True`` if the .desktop file exists."""
    return os.path.isfile(_desktop_path())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enable_autostart():
    """Enable ClipSync to start automatically on user login.

    The implementation is chosen based on :func:`platform.system`:

    * **Windows** – Creates a string value in the
      ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` registry
      key named ``ClipSync``.

    * **macOS** – Creates a LaunchAgent property list at
      ``~/Library/LaunchAgents/com.clipsync.plist`` with ``RunAtLoad``
      enabled.

    * **Linux** – Creates an XDG autostart ``.desktop`` file at
      ``~/.config/autostart/clipsync.desktop``.

    Raises :exc:`OSError` if the current platform is not supported.
    """
    system = _platform.system()
    if system == "Windows":
        _enable_windows()
    elif system == "Darwin":
        _enable_macos()
    elif system == "Linux":
        _enable_linux()
    else:
        raise OSError(f"Unsupported platform: {system}")


def disable_autostart():
    """Remove the auto-start entry so ClipSync no longer launches on login.

    This is a safe no-op when no entry exists (e.g. already removed, or
    auto-start was never enabled).

    Raises :exc:`OSError` if the current platform is not supported.
    """
    system = _platform.system()
    if system == "Windows":
        _disable_windows()
    elif system == "Darwin":
        _disable_macos()
    elif system == "Linux":
        _disable_linux()
    else:
        raise OSError(f"Unsupported platform: {system}")


def is_autostart_enabled():
    """Return ``True`` if an auto-start entry currently exists.

    Returns ``False`` on unsupported platforms (instead of raising).
    """
    system = _platform.system()
    if system == "Windows":
        return _is_enabled_windows()
    elif system == "Darwin":
        return _is_enabled_macos()
    elif system == "Linux":
        return _is_enabled_linux()
    return False
