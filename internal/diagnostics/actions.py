"""OS repair actions the diagnostics report's hints point at.

Two actions exist, matching the legacy ``/api/diagnostics/request`` contract:

``firewall``
    Windows: re-apply the allow rule for the TCP sync port and the web
    companion port (unelevated first, then through a UAC prompt), falling back
    to opening the firewall settings page.  Linux: grant both ports on the
    active ufw/firewalld through a PolicyKit prompt.  macOS: open the Firewall
    pane (there is no CLI to grant it).
``local_network``
    macOS 15+ Local Network permission: open the exact System Settings pane.
    Windows has no dedicated page, so the firewall/network page is the closest
    target; Linux has no such permission.

The platform work lives here, not in a caller, so the legacy web panel and the
native desktop request exactly the same repair.
"""

import os
import platform
import subprocess

FIREWALL_PANE = "x-apple.systempreferences:com.apple.preference.security?Firewall"
LOCAL_NETWORK_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"
)
WINDOWS_FIREWALL_URI = "ms-settings:network-firewall"


def macos_open_settings(uri: str) -> bool:
    """Open a macOS System Settings pane (best-effort)."""
    try:
        subprocess.Popen(["open", uri])
        return True
    except Exception:
        return False


def open_windows_settings(uri: str) -> bool:
    """Open a Windows Settings URI through several launchers.

    ``os.startfile`` on a ms-settings: URI works on most builds but can fail on
    some systems; fall back to explorer.exe and finally to the Settings app
    binary itself.
    """
    try:
        os.startfile(uri)
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(["explorer.exe", uri])
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(
            [
                os.path.join(
                    os.environ.get("WINDIR", r"C:\Windows"),
                    "ImmersiveControlPanel",
                    "SystemSettings.exe",
                ),
                uri,
            ]
        )
        return True
    except Exception:
        return False


def linux_firewall_allow_script(port: int, web_port: int) -> tuple[str | None, str | None]:
    """Return (firewall_name, shell_script) opening both ports on Linux.

    Detects the active firewall (ufw or firewalld) the same way the diagnostics
    check does and returns the exact commands an admin shell needs to run.
    Returns (None, None) when no firewall is active.
    """
    for cmd, name in (
        (["ufw", "status"], "ufw"),
        (["systemctl", "is-active", "firewalld"], "firewalld"),
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout or ""
        except Exception:
            continue
        # Whole-word match: "active" is a substring of "inactive", so a bare
        # `in` check would misread `ufw status` = "Status: inactive".
        if "active" in out.split():
            if name == "ufw":
                return ("ufw", f"ufw allow {port}/tcp && ufw allow {web_port}/tcp")
            return (
                "firewalld",
                f"firewall-cmd --permanent --add-port={port}/tcp && "
                f"firewall-cmd --permanent --add-port={web_port}/tcp && "
                f"firewall-cmd --reload",
            )
    return (None, None)


def request(action: str, cfg) -> dict:
    """Open the relevant OS permission/firewall settings, or re-apply a rule.

    Returns ``{"ok": bool}`` on success and ``{"ok": False, "error": str}``
    otherwise — never raises, so the caller can surface the reason verbatim.
    """
    try:
        system = platform.system()
        port = int(getattr(cfg, "port", 0) or 0)
        web_port = int(getattr(cfg, "web_port", 0) or 0)

        if action == "firewall":
            if system == "Darwin":
                # macOS has no CLI to grant the Application Firewall, and the
                # Local Network permission (macOS 15+) can only be toggled in
                # System Settings — so open the exact pane.
                if macos_open_settings(FIREWALL_PANE):
                    return {"ok": True}
                return {"ok": False, "error": "Could not open the macOS firewall settings."}
            if system == "Windows":
                # Prefer re-applying the allow rule (idempotent). netsh needs
                # admin rights — retry elevated via a UAC prompt, then fall
                # back to opening the firewall settings page so the user can
                # allow the ports manually.
                from internal.web.server import WebServer

                if WebServer._open_firewall(port, web_port):
                    return {"ok": True}
                if WebServer._open_firewall_elevated(port, web_port):
                    return {"ok": True}
                if open_windows_settings(WINDOWS_FIREWALL_URI):
                    return {"ok": True}
                return {
                    "ok": False,
                    "error": "Could not create the firewall rule (admin rights may be "
                    "required) or open the firewall settings.",
                }
            if system == "Linux":
                # Grant both ports on the active firewall (ufw / firewalld)
                # via a PolicyKit GUI auth prompt — the Linux equivalent of
                # the Windows UAC repair. If pkexec is unavailable, surface
                # the exact command so the user can run it as root.
                name, script = linux_firewall_allow_script(port, web_port)
                if not name:
                    # No active firewall detected — nothing to request.
                    return {"ok": True}
                try:
                    subprocess.Popen(["pkexec", "sh", "-c", script])
                    return {"ok": True}
                except Exception:
                    return {"ok": False, "error": f"Allow the ports manually as root: {script}"}
            return {"ok": False, "error": "Firewall settings are not supported on this OS."}

        if action == "local_network":
            if system == "Darwin":
                # The macOS 15+ Local Network permission is OS-enforced and
                # cannot be granted by CLI — open the exact pane for it.
                if macos_open_settings(LOCAL_NETWORK_PANE):
                    return {"ok": True}
                return {
                    "ok": False,
                    "error": "Could not open the macOS Local Network permission settings.",
                }
            if system == "Windows":
                # Windows has no dedicated local-network permission page on most
                # builds — the firewall & network settings is the closest target.
                if open_windows_settings(WINDOWS_FIREWALL_URI):
                    return {"ok": True}
                return {
                    "ok": False,
                    "error": "Could not open the Windows network/firewall settings.",
                }
            if system == "Linux":
                # No local-network permission exists on Linux — nothing to do.
                return {"ok": True}
            return {"ok": False, "error": "Permission settings are not supported on this OS."}

        return {"ok": False, "error": f"Unknown action: {action}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
