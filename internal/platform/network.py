"""Primary network type detection, shared by the legacy and Tauri hosts.

Both the desktop dashboard and the phone Companion's overview show the same
"Wi-Fi / Ethernet" label, so the detection lives here instead of in either
host's UI code.
"""

import os
import subprocess
import sys

WIFI = "wifi"
ETHERNET = "ethernet"
LAN = "lan"


def detect_network_type() -> tuple[str, str]:
    """Best-effort primary network type + interface name.

    Returns (type, interface) where type is "wifi", "ethernet" or "lan".
    Uses the default-route interface so VPN/loopback don't fool it.
    """
    try:
        if sys.platform == "darwin":
            return _detect_darwin()
        if sys.platform.startswith("linux"):
            return _detect_linux()
    except Exception:
        pass
    return LAN, ""


def _detect_darwin() -> tuple[str, str]:
    out = (
        subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
        or ""
    )
    iface = ""
    for line in out.splitlines():
        if line.strip().startswith("interface:"):
            iface = line.split(":", 1)[1].strip()
            break
    if not iface:
        return LAN, ""
    ports = (
        subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
        or ""
    )
    for block in ports.split("\n\n"):
        if f"Device: {iface}" in block:
            if "Wi-Fi" in block or "AirPort" in block:
                return WIFI, iface
            return ETHERNET, iface
    return LAN, iface


def _detect_linux() -> tuple[str, str]:
    iface = ""
    try:
        with open("/proc/net/route") as routes:
            for line in routes.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    iface = parts[0]
                    break
    except OSError:
        pass
    if iface and os.path.isdir(f"/sys/class/net/{iface}/wireless"):
        return WIFI, iface
    return (ETHERNET, iface) if iface else (LAN, "")
