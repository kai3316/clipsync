"""Live diagnostics report shared by the legacy web panel and the desktop.

The payload has two shapes and both are kept:

* ``checks`` — the legacy flat list, each entry carrying an ``ok`` flag and
  i18n keys for the web panel.
* ``groups`` — the round-19 grouped items (``system``, ``network``,
  ``internet``, ``ai_config``, ``chat``, ``transfer``, ``filesystem``).

:func:`build_report` is the single implementation for both clients: the legacy
``Application._get_diagnostics`` and the sidecar's ``diagnostics.report``
command call it with the same probe inputs, so the web panel and the native
page cannot disagree about the state of the machine.

Every probe is defensive. A missing manager or a failed OS probe produces a
warn/fail item with a hint instead of raising, so a degraded application still
renders a complete report — that is the whole point of the page.
"""

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from internal.data.logs import log_path
from internal.platform import friendly_platform_name
from internal.version import __version__

# A failure in one of these makes the overall summary "fail"; anything else
# can only lower it to "warn".
SUMMARY_CRITICAL_IDS = ("server_port", "discovery", "network", "mdns")
STATUSES = ("ok", "warn", "fail")
DISK_LOW_BYTES = 500 * 1024 * 1024
PROBE_TIMEOUT = 3


# ── pure helpers (formatting / classification) ───────────────────────────


def is_private_ip(ip: str) -> bool:
    """Return True when *ip* is in a private LAN range (RFC 1918)."""
    if ip.startswith("192.168."):
        return True
    if ip.startswith("10."):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def classify_network(lan_ip: str) -> tuple[bool, str, str | None]:
    """Classify the detected LAN IP for the diagnostics network check.

    Returns (ok, detail, guidance).  guidance is None when the check passes.
    """
    if not lan_ip or lan_ip.startswith("127."):
        return (
            False,
            "No LAN address detected",
            "No LAN address detected — check that WiFi/Ethernet is connected to a network.",
        )
    if lan_ip.startswith("169.254."):
        return (
            False,
            "Link-local address (169.254.x.x)",
            "No DHCP address (169.254 link-local) — check that WiFi/Ethernet is connected to a network.",  # noqa: E501
        )
    if is_private_ip(lan_ip):
        return (True, f"Private LAN ({lan_ip})", None)
    return (
        False,
        f"Public/routable IP ({lan_ip})",
        "This device appears to be on a public/routable IP — you may be behind a VPN or on an "
        "isolated network. VPNs and client isolation prevent LAN discovery.",
    )


def fmt_duration(secs) -> str:
    secs = max(int(secs or 0), 0)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m {secs % 60}s"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h {mins % 60}m"
    days = hours // 24
    return f"{days}d {hours % 24}h"


def fmt_bytes(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


def effective_data_dir(cfg) -> Path:
    """Resolve the effective data directory (custom ``cfg.data_dir`` wins)."""
    from internal.config.config import _config_dir

    custom = str(getattr(cfg, "data_dir", "") or "").strip()
    if custom:
        return Path(custom)
    return _config_dir()


def dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(str(path), os.W_OK)
    except Exception:
        return False


def trash_size(mgr) -> int:
    """Total bytes in the AI-config recoverable trash (0 when empty)."""
    base = mgr._trash_base()
    if not base.is_dir():
        return 0
    total = 0
    for p in base.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def item(
    item_id,
    status,
    detail,
    hint=None,
    detail_key=None,
    detail_params=None,
    hint_key=None,
    hint_params=None,
) -> dict:
    """One diagnostic item: ``{id, status, detail, hint, *_key/*_params}``.

    ``status`` is one of "ok" | "warn" | "fail".  ``detail``/``hint`` are raw
    fallback strings; the optional ``*_key``/``*_params`` let the web panel
    resolve a localized string first (mirroring the flat ``checks`` contract).
    """
    entry = {
        "id": item_id,
        "status": status,
        "detail": detail or "",
        "hint": hint or None,
    }
    if detail_key:
        entry["detail_key"] = detail_key
        entry["detail_params"] = dict(detail_params or {})
    if hint_key:
        entry["hint_key"] = hint_key
        entry["hint_params"] = dict(hint_params or {})
    return entry


# ── OS probes ────────────────────────────────────────────────────────────


def firewall_probe(cfg, discovery_running: bool, web=None) -> dict:
    """Probe the OS firewall for the ports ClipSync needs.

    ``web`` is the companion server when one exists; its (static)
    ``check_firewall_rule`` is preferred so an injected test double is honored,
    falling back to the real rule reader when the companion is off.

    Returns the ``ok``/``detail``/``*_key``/``*_params`` fields the flat
    ``firewall`` check and the network group's firewall item share.
    """
    result = {
        "ok": True,
        "detail": "No firewall blockage detected",
        "detail_key": "diag.firewall.ok.detail",
        "detail_params": {},
        "guidance": None,
        "guidance_key": None,
        "guidance_params": {},
    }
    try:
        system = platform.system()
        if system == "Darwin":
            out = (
                subprocess.run(
                    ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"],
                    capture_output=True,
                    text=True,
                    timeout=PROBE_TIMEOUT,
                ).stdout
                or ""
            )
            if "enabled" in out.lower():
                result["detail"] = "macOS firewall is enabled"
                result["detail_key"] = "diag.firewall.macos_ok.detail"
                result["guidance"] = (
                    "The macOS firewall is on. If other devices can't reach this "
                    "computer, allow ClipSync: System Settings → Network → Firewall → "
                    "Options, or tap 'Request permission' to open it."
                )
                result["guidance_key"] = "diag.firewall.macos_ok.guidance"
                # Only fail the check when discovery is also failing (strong signal).
                result["ok"] = bool(discovery_running)
        elif system == "Windows":
            try:
                # Both the TCP sync port and the web companion port need to be
                # open — a single-port rule would otherwise show up as a
                # "wrong port" mismatch.
                from internal.web.server import WebServer

                checker = getattr(web, "check_firewall_rule", None) or WebServer.check_firewall_rule
                ok, detail = checker([cfg.port, cfg.web_port])
                if not ok:
                    result["ok"] = False
                    result["detail"] = detail
                    result["guidance"] = (
                        "The Windows firewall may be blocking ClipSync. Tap "
                        "'Request permission' to add an allow rule for ports "
                        f"{cfg.port} and {cfg.web_port}."
                    )
                    result["guidance_key"] = "diag.firewall.win_fail.guidance"
                    result["guidance_params"] = {"port": f"{cfg.port}, {cfg.web_port}"}
                    if detail.startswith("Wrong port"):
                        # Stale rule with the wrong port — surface both values.
                        match = re.search(r"\(got ([^)]+), needs ([^)]+)\)", detail)
                        result["detail_key"] = "diag.firewall.wrongport.detail"
                        result["detail_params"] = (
                            {"actual": match.group(1), "port": match.group(2)} if match else {}
                        )
                    else:
                        result["detail_key"] = "diag.firewall.win_blocked.detail"
            except Exception:
                pass
        elif system == "Linux":
            # ufw / firewalld detection + port allow check (best-effort).
            result["detail"] = "No Linux firewall detected"
            result["detail_key"] = "diag.firewall.linux_none.detail"
            for cmd, name in (
                (["ufw", "status"], "ufw"),
                (["systemctl", "is-active", "firewalld"], "firewalld"),
            ):
                try:
                    out = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT
                    ).stdout or ""
                except Exception:
                    continue
                # Whole-word match: "active" is a substring of "inactive", so a
                # bare `in` check would misread "Status: inactive".
                if "active" in out.split():
                    result["detail"] = f"{name} firewall is active"
                    result["detail_key"] = "diag.firewall.linux_active.detail"
                    result["detail_params"] = {"name": name}
                    result["guidance"] = (
                        f"The {name} firewall is on. If other devices can't reach "
                        f"this computer, allow ports {cfg.port} and "
                        f"{cfg.web_port}: 'sudo ufw allow {cfg.port}/tcp' and "
                        f"'sudo ufw allow {cfg.web_port}/tcp' (or the firewalld equivalent)."  # noqa: E501
                    )
                    result["guidance_key"] = "diag.firewall.linux_active.guidance"
                    result["guidance_params"] = {
                        "name": name,
                        "port": cfg.port,
                        "web_port": cfg.web_port,
                    }
                    result["ok"] = bool(discovery_running)
                    break
    except Exception:
        pass
    return result


def permissions_probe(discovery_running: bool) -> dict:
    """macOS Local Network permission (15+) check; a no-op elsewhere."""
    result = {
        "ok": True,
        "detail": "No permission issues detected",
        "detail_key": "diag.permissions.ok.detail",
        "detail_params": {},
        "guidance": None,
        "guidance_key": None,
        "guidance_params": {},
    }
    try:
        if platform.system() == "Darwin":
            version = [int(x) for x in platform.mac_ver()[0].split(".")[:2]]
            if len(version) == 2 and version[0] >= 15:
                if not discovery_running:
                    result["ok"] = False
                    result["detail"] = "Local Network permission may be missing (macOS 15+)"
                    result["detail_key"] = "diag.permissions.fail.detail"
                    result["guidance"] = (
                        "macOS 15+ needs 'Local Network' permission to discover other "
                        "devices. Tap 'Request permission' to open System Settings → "
                        "Privacy & Security → Local Network and allow ClipSync."
                    )
                    result["guidance_key"] = "diag.permissions.fail.guidance"
                else:
                    result["detail"] = "Local Network permission granted"
                    result["detail_key"] = "diag.permissions.ok_macos.detail"
    except Exception:
        pass
    return result


def mdns_probe() -> dict:
    """Linux avahi-daemon check; a no-op elsewhere."""
    result = {
        "ok": True,
        "detail": "mDNS service available",
        "detail_key": "diag.mdns.ok.detail",
        "guidance": None,
        "guidance_key": None,
    }
    try:
        if platform.system() == "Linux":
            out = (
                subprocess.run(
                    ["systemctl", "is-active", "avahi-daemon"],
                    capture_output=True,
                    text=True,
                    timeout=PROBE_TIMEOUT,
                ).stdout
                or ""
            )
            if "active" not in out.lower():
                result["ok"] = False
                result["detail"] = "avahi-daemon is not running"
                result["detail_key"] = "diag.mdns.fail.detail"
                result["guidance"] = (
                    "mDNS discovery needs avahi-daemon. Install/start it: "
                    "'sudo apt install avahi-daemon' then 'sudo systemctl start avahi-daemon'."
                )
                result["guidance_key"] = "diag.mdns.fail.guidance"
    except Exception:
        pass
    return result


def clipboard_tool_probe():
    """Linux xclip / wl-paste check; None on other platforms."""
    if platform.system() != "Linux":
        return None
    try:
        ok = bool(
            subprocess.run(
                ["sh", "-c", "command -v xclip || command -v wl-paste"],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT,
            ).stdout.strip()
        )
    except Exception:
        ok = False
    return {
        "id": "clipboard_tool",
        "ok": ok,
        "detail": "clipboard tool present" if ok else "no xclip / wl-paste",
        "detail_key": "diag.clipboard_tool.ok.detail" if ok else "diag.clipboard_tool.fail.detail",
        "guidance": None
        if ok
        else (
            "Clipboard capture needs xclip (X11) or "
            "wl-paste (Wayland). Install one: "
            "'sudo apt install xclip' or 'sudo apt install wl-clipboard'."
        ),
        "guidance_key": None if ok else "diag.clipboard_tool.fail.guidance",
    }


# ── flat checks ──────────────────────────────────────────────────────────


def build_checks(
    cfg, server_running, discovery_running, advertising, web_running, lan_ip, web=None
):
    """The legacy flat check list (9 items, i18n-keyed)."""
    port = int(getattr(cfg, "port", 0) or 0)
    web_port = int(getattr(cfg, "web_port", 0) or 0)
    checks = []

    if server_running:
        checks.append(
            {
                "id": "server_port",
                "ok": True,
                "detail": f"TCP server listening on {port}",
                "detail_key": "diag.server_port.ok.detail",
                "detail_params": {"port": port},
                "guidance": None,
            }
        )
    else:
        checks.append(
            {
                "id": "server_port",
                "ok": False,
                "detail": f"TCP server not listening on {port}",
                "detail_key": "diag.server_port.fail.detail",
                "detail_params": {"port": port},
                "guidance": (
                    f"Port {port} is not listening — another app may be using it, "
                    "or the firewall blocks it. Try a different port in Settings → Network."
                ),
                "guidance_key": "diag.server_port.fail.guidance",
                "guidance_params": {"port": port},
            }
        )

    if discovery_running:
        checks.append(
            {
                "id": "discovery",
                "ok": True,
                "detail": "mDNS discovery active",
                "detail_key": "diag.discovery.ok.detail",
                "guidance": None,
            }
        )
    else:
        checks.append(
            {
                "id": "discovery",
                "ok": False,
                "detail": "mDNS discovery not active",
                "detail_key": "diag.discovery.fail.detail",
                "guidance": (
                    "mDNS discovery isn't active. If you're on a guest/enterprise WiFi, "
                    "AP/client isolation blocks discovery — connect both devices to the "
                    "same private network."
                ),
                "guidance_key": "diag.discovery.fail.guidance",
            }
        )

    if advertising:
        checks.append(
            {
                "id": "advertising",
                "ok": True,
                "detail": "device visible on network",
                "detail_key": "diag.advertising.ok.detail",
                "guidance": None,
            }
        )
    else:
        checks.append(
            {
                "id": "advertising",
                "ok": False,
                "detail": "device not advertising",
                "detail_key": "diag.advertising.fail.detail",
                "guidance": "This device isn't advertising — enable 'Visible' in the overview.",
                "guidance_key": "diag.advertising.fail.guidance",
            }
        )

    if web_running:
        checks.append(
            {
                "id": "web_companion",
                "ok": True,
                "detail": f"Remote access on :{web_port}",
                "detail_key": "diag.web_companion.ok.detail",
                "detail_params": {"web_port": web_port},
                "guidance": None,
            }
        )
    else:
        checks.append(
            {
                "id": "web_companion",
                "ok": False,
                "detail": "Remote access not running",
                "detail_key": "diag.web_companion.fail.detail",
                "guidance": "Remote access isn't running — enable it in Settings → Remote access.",  # noqa: E501
                "guidance_key": "diag.web_companion.fail.guidance",
            }
        )

    network_ok, network_detail, network_guidance = classify_network(lan_ip)
    network_detail_key, network_params = "diag.network.private.detail", {"lan_ip": lan_ip}
    network_guidance_key = None
    if not network_ok:
        if not lan_ip or lan_ip.startswith("127."):
            network_detail_key = "diag.network.nolan.detail"
            network_guidance_key = "diag.network.nolan.guidance"
        elif lan_ip.startswith("169.254."):
            network_detail_key = "diag.network.linklocal.detail"
            network_guidance_key = "diag.network.linklocal.guidance"
        else:
            network_detail_key = "diag.network.public.detail"
            network_guidance_key = "diag.network.public.guidance"
            network_params = {"lan_ip": lan_ip}
    checks.append(
        {
            "id": "network",
            "ok": network_ok,
            "detail": network_detail,
            "guidance": network_guidance,
            "detail_key": network_detail_key,
            "detail_params": network_params,
            "guidance_key": network_guidance_key,
            "guidance_params": network_params,
        }
    )

    firewall = firewall_probe(cfg, discovery_running, web)
    checks.append(
        {
            "id": "firewall",
            "ok": firewall["ok"],
            "detail": firewall["detail"],
            "detail_key": firewall["detail_key"],
            "detail_params": firewall["detail_params"],
            "guidance": firewall["guidance"],
            "guidance_key": firewall["guidance_key"],
            "guidance_params": firewall["guidance_params"],
        }
    )

    permissions = permissions_probe(discovery_running)
    checks.append(
        {
            "id": "permissions",
            "ok": permissions["ok"],
            "detail": permissions["detail"],
            "detail_key": permissions["detail_key"],
            "detail_params": permissions["detail_params"],
            "guidance": permissions["guidance"],
            "guidance_key": permissions["guidance_key"],
            "guidance_params": permissions["guidance_params"],
        }
    )

    mdns = mdns_probe()
    checks.append(
        {
            "id": "mdns",
            "ok": mdns["ok"],
            "detail": mdns["detail"],
            "detail_key": mdns["detail_key"],
            "guidance": mdns["guidance"],
            "guidance_key": mdns["guidance_key"],
        }
    )

    clipboard_tool = clipboard_tool_probe()
    if clipboard_tool is not None:
        checks.append(clipboard_tool)

    return checks, firewall


def summarize(checks) -> str:
    """'fail' when a critical check is down, 'warn' for a non-critical one."""
    if any(not c["ok"] for c in checks if c["id"] in SUMMARY_CRITICAL_IDS):
        return "fail"
    if any(not c["ok"] for c in checks):
        return "warn"
    return "ok"


# ── grouped items ────────────────────────────────────────────────────────


def _group_system(cfg, start_time) -> dict:
    items = []
    items.append(
        item(
            "app_version",
            "ok",
            f"Version {__version__}",
            detail_key="diag.v2.item.app_version.detail",
            detail_params={"version": __version__},
        )
    )
    try:
        uptime = max(int(time.time()) - int(start_time or 0), 0)
    except Exception:
        uptime = 0
    pretty = fmt_duration(uptime)
    items.append(
        item(
            "uptime",
            "ok",
            f"Running for {pretty}",
            detail_key="diag.v2.item.uptime.detail",
            detail_params={"uptime": pretty},
        )
    )
    # Data directory + writability.
    try:
        data_dir = effective_data_dir(cfg)
        if dir_writable(data_dir):
            items.append(
                item(
                    "data_dir",
                    "ok",
                    str(data_dir),
                    detail_key="diag.v2.item.data_dir.detail",
                    detail_params={"dir": str(data_dir)},
                )
            )
        else:
            items.append(
                item(
                    "data_dir",
                    "warn",
                    f"{data_dir} (not writable)",
                    detail_key="diag.v2.item.data_dir.warn.detail",
                    detail_params={"dir": str(data_dir)},
                    hint="Data directory is not writable — backups and history may fail.",
                    hint_key="diag.v2.item.data_dir.warn.hint",
                )
            )
    except Exception:
        items.append(
            item(
                "data_dir",
                "fail",
                "Data directory unavailable",
                detail_key="diag.v2.item.data_dir.fail.detail",
                hint="Could not access the data directory.",
                hint_key="diag.v2.item.data_dir.fail.hint",
            )
        )
    # Log path + writability.
    try:
        path = log_path()
        if path is not None and dir_writable(path.parent):
            items.append(
                item(
                    "log_path",
                    "ok",
                    str(path),
                    detail_key="diag.v2.item.log_path.detail",
                    detail_params={"path": str(path)},
                )
            )
        elif path is not None:
            items.append(
                item(
                    "log_path",
                    "warn",
                    str(path),
                    detail_key="diag.v2.item.log_path.warn.detail",
                    detail_params={"path": str(path)},
                    hint="Log directory is not writable — logging may fail.",
                    hint_key="diag.v2.item.log_path.warn.hint",
                )
            )
        else:
            raise OSError("log path unavailable")
    except Exception:
        items.append(
            item(
                "log_path",
                "warn",
                "Log path unavailable",
                detail_key="diag.v2.item.log_path.unavailable.detail",
            )
        )
    return {"label_key": "diag.v2.group.system", "items": items}


def _group_network(cfg, ctx) -> dict:
    items = []
    lan_ip = ctx.get("lan_ip", "")
    if not lan_ip or lan_ip.startswith("127."):
        items.append(
            item(
                "lan_ip",
                "fail",
                "No LAN address detected",
                detail_key="diag.v2.item.lan_ip.nolan.detail",
                hint="No LAN address detected — check that WiFi/Ethernet is connected to a network.",  # noqa: E501
                hint_key="diag.v2.item.lan_ip.warn.hint",
            )
        )
    elif lan_ip.startswith("169.254."):
        items.append(
            item(
                "lan_ip",
                "warn",
                f"Link-local address ({lan_ip})",
                detail_key="diag.v2.item.lan_ip.linklocal.detail",
                detail_params={"lan_ip": lan_ip},
                hint="No DHCP address (169.254 link-local) — check that WiFi/Ethernet is connected to a network.",  # noqa: E501
                hint_key="diag.v2.item.lan_ip.warn.hint",
            )
        )
    elif is_private_ip(lan_ip):
        items.append(
            item(
                "lan_ip",
                "ok",
                f"{lan_ip} (private LAN)",
                detail_key="diag.v2.item.lan_ip.ok.detail",
                detail_params={"lan_ip": lan_ip},
            )
        )
    else:
        items.append(
            item(
                "lan_ip",
                "warn",
                f"{lan_ip} (public/routable)",
                detail_key="diag.v2.item.lan_ip.public.detail",
                detail_params={"lan_ip": lan_ip},
                hint="This device appears to be on a public/routable IP — you may be behind a VPN or on an isolated network.",  # noqa: E501
                hint_key="diag.v2.item.lan_ip.warn.hint",
            )
        )

    port = ctx.get("port", 0)
    if ctx.get("server_running"):
        items.append(
            item(
                "tcp_port",
                "ok",
                f"Listening on port {port}",
                detail_key="diag.v2.item.tcp_port.ok.detail",
                detail_params={"port": port},
            )
        )
    else:
        items.append(
            item(
                "tcp_port",
                "fail",
                f"Not listening on port {port}",
                detail_key="diag.v2.item.tcp_port.fail.detail",
                detail_params={"port": port},
                hint="Another app may be using the port, or the firewall blocks it. Try a different port in Settings → Network.",  # noqa: E501
                hint_key="diag.v2.item.tcp_port.fail.hint",
            )
        )

    if ctx.get("discovery_running"):
        items.append(
            item(
                "mdns_service",
                "ok",
                "mDNS discovery active",
                detail_key="diag.v2.item.mdns_service.ok.detail",
            )
        )
    else:
        items.append(
            item(
                "mdns_service",
                "fail",
                "mDNS discovery not active",
                detail_key="diag.v2.item.mdns_service.fail.detail",
                hint="mDNS discovery isn't active. If you're on a guest/enterprise WiFi, AP/client isolation blocks discovery.",  # noqa: E501
                hint_key="diag.v2.item.mdns_service.fail.hint",
            )
        )

    web_enabled = bool(getattr(cfg, "web_enabled", False))
    web_port = ctx.get("web_port", 0)
    if not web_enabled:
        items.append(
            item(
                "web_service",
                "warn",
                "Disabled",
                detail_key="diag.v2.item.web_service.off.detail",
                hint="Enable remote access in Settings → Remote access to control this device from a phone or browser.",  # noqa: E501
                hint_key="diag.v2.item.web_service.off.hint",
            )
        )
    elif ctx.get("web_running"):
        items.append(
            item(
                "web_service",
                "ok",
                f"Enabled on :{web_port}",
                detail_key="diag.v2.item.web_service.ok.detail",
                detail_params={"web_port": web_port},
            )
        )
    else:
        items.append(
            item(
                "web_service",
                "fail",
                "Enabled but not running",
                detail_key="diag.v2.item.web_service.fail.detail",
            )
        )

    if ctx.get("fw_ok"):
        items.append(
            item(
                "firewall",
                "ok",
                ctx.get("fw_detail") or "No firewall blockage detected",
                detail_key=ctx.get("fw_detail_key") or "diag.v2.item.firewall.ok.detail",
                detail_params=ctx.get("fw_detail_params") or {},
            )
        )
    else:
        items.append(
            item(
                "firewall",
                "fail",
                ctx.get("fw_detail") or "Firewall may be blocking",
                detail_key=ctx.get("fw_detail_key") or "diag.v2.item.firewall.fail.detail",
                detail_params=ctx.get("fw_detail_params") or {},
                hint=ctx.get("fw_guidance") or "The firewall may be blocking ClipSync.",
                hint_key=ctx.get("fw_guidance_key") or "diag.v2.item.firewall.fail.hint",
                hint_params=ctx.get("fw_guidance_params") or {},
            )
        )
    return {"label_key": "diag.v2.group.network", "items": items}


def _group_internet(cfg, relay_state, pending_count) -> dict:
    items = []
    if bool(getattr(cfg, "internet_sync_enabled", False)):
        items.append(
            item(
                "internet_enabled",
                "ok",
                "Enabled",
                detail_key="diag.v2.item.internet_enabled.ok.detail",
            )
        )
    else:
        items.append(
            item(
                "internet_enabled",
                "warn",
                "Disabled",
                detail_key="diag.v2.item.internet_enabled.off.detail",
                hint="Enable internet sync in Settings → Internet sync to sync across networks.",  # noqa: E501
                hint_key="diag.v2.item.internet_enabled.off.hint",
            )
        )

    if relay_state == "online":
        items.append(
            item(
                "relay_state",
                "ok",
                "Connected to relay",
                detail_key="diag.v2.item.relay_state.online.detail",
            )
        )
    elif relay_state == "connecting":
        items.append(
            item(
                "relay_state",
                "warn",
                "Connecting to relay…",
                detail_key="diag.v2.item.relay_state.connecting.detail",
            )
        )
    elif relay_state == "error":
        items.append(
            item(
                "relay_state",
                "fail",
                "Relay connection error",
                detail_key="diag.v2.item.relay_state.error.detail",
                hint="The relay could not be reached. Check your internet connection.",
                hint_key="diag.v2.item.relay_state.error.hint",
            )
        )
    else:
        items.append(
            item(
                "relay_state",
                "warn",
                "Relay off (internet sync disabled)",
                detail_key="diag.v2.item.relay_state.off.detail",
            )
        )

    brokers = [b for b in getattr(cfg, "relay_brokers", []) if isinstance(b, str) and b]
    if not brokers:
        items.append(
            item(
                "brokers",
                "fail",
                "No brokers configured",
                detail_key="diag.v2.item.brokers.fail.detail",
                hint="Add at least one public MQTT relay in Settings → Internet sync.",
                hint_key="diag.v2.item.brokers.fail.hint",
            )
        )
    elif relay_state == "online":
        items.append(
            item(
                "brokers",
                "ok",
                f"{len(brokers)} broker(s) configured, reachable",
                detail_key="diag.v2.item.brokers.ok.detail",
                detail_params={"count": len(brokers)},
            )
        )
    elif relay_state == "error":
        items.append(
            item(
                "brokers",
                "fail",
                f"{len(brokers)} broker(s) configured, none reachable",
                detail_key="diag.v2.item.brokers.fail_reachable.detail",
                detail_params={"count": len(brokers)},
                hint="None of the configured relays could be reached. Check your internet connection.",  # noqa: E501
                hint_key="diag.v2.item.brokers.fail_reachable.hint",
            )
        )
    else:
        items.append(
            item(
                "brokers",
                "warn",
                f"{len(brokers)} broker(s) configured",
                detail_key="diag.v2.item.brokers.warn.detail",
                detail_params={"count": len(brokers)},
            )
        )

    netpair = getattr(cfg, "netpair_secrets", {}) or {}
    items.append(
        item(
            "netpair_count",
            "ok",
            f"{len(netpair)} internet-paired device(s)",
            detail_key="diag.v2.item.netpair_count.detail",
            detail_params={"count": len(netpair)},
        )
    )

    if pending_count < 0:
        items.append(
            item(
                "pending_count",
                "warn",
                "Delivery stats unavailable",
                detail_key="diag.v2.item.pending_count.unavailable.detail",
            )
        )
    elif pending_count == 0:
        items.append(
            item(
                "pending_count",
                "ok",
                "No pending sends",
                detail_key="diag.v2.item.pending_count.ok.detail",
            )
        )
    else:
        items.append(
            item(
                "pending_count",
                "warn",
                f"{pending_count} pending send(s)",
                detail_key="diag.v2.item.pending_count.warn.detail",
                detail_params={"count": pending_count},
                hint="Some clipboard items are queued and will be sent when the peer reconnects.",  # noqa: E501
                hint_key="diag.v2.item.pending_count.warn.hint",
            )
        )
    return {"label_key": "diag.v2.group.internet", "items": items}


def _group_ai_config(cfg, mgr) -> dict:
    items = []
    tools = [t for t in (getattr(cfg, "ai_config_tools", []) or []) if isinstance(t, str) and t]
    custom = [
        c
        for c in (getattr(cfg, "ai_config_custom_paths", []) or [])
        if isinstance(c, str) and c
    ]
    roots = tools + custom
    if roots:
        items.append(
            item(
                "watch_roots",
                "ok",
                f"{len(roots)} profile root(s)",
                detail_key="diag.v2.item.watch_roots.ok.detail",
                detail_params={"count": len(roots)},
            )
        )
    else:
        items.append(
            item(
                "watch_roots",
                "warn",
                "No watch roots",
                detail_key="diag.v2.item.watch_roots.warn.detail",
                hint="Enable AI tool profiles in AI config settings to inventory AI tool configs.",  # noqa: E501
                hint_key="diag.v2.item.watch_roots.warn.hint",
            )
        )
    if mgr is None:
        for entry_id in ("local_entries", "last_collected", "trash_size"):
            items.append(
                item(
                    entry_id,
                    "warn",
                    "Unavailable",
                    detail_key="diag.v2.item.unavailable.detail",
                    hint="This data isn't available in the current state.",
                    hint_key="diag.v2.item.unavailable.hint",
                )
            )
        return {"label_key": "diag.v2.group.ai_config", "items": items}
    try:
        summary = mgr.local_summary()
        entry_count = int(summary.get("entry_count", 0) or 0)
        items.append(
            item(
                "local_entries",
                "ok",
                f"{entry_count} local file(s)",
                detail_key="diag.v2.item.local_entries.detail",
                detail_params={"count": entry_count},
            )
        )
        collected = float(summary.get("collected_at", 0.0) or 0.0)
        if collected > 0:
            ago = fmt_duration(int(time.time() - collected))
            items.append(
                item(
                    "last_collected",
                    "ok",
                    f"{ago} ago",
                    detail_key="diag.v2.item.last_collected.ok.detail",
                    detail_params={"ago": ago},
                )
            )
        else:
            items.append(
                item(
                    "last_collected",
                    "warn",
                    "Never collected",
                    detail_key="diag.v2.item.last_collected.warn.detail",
                    hint="Run a collection from the AI config panel, or add watch roots.",
                    hint_key="diag.v2.item.last_collected.warn.hint",
                )
            )
    except Exception:
        items.append(
            item(
                "local_entries",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
            )
        )
        items.append(
            item(
                "last_collected",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
            )
        )
    try:
        size = trash_size(mgr)
        pretty = fmt_bytes(size)
        items.append(
            item(
                "trash_size",
                "ok" if size == 0 else "warn",
                pretty,
                detail_key="diag.v2.item.trash_size.detail",
                detail_params={"size": pretty},
                hint=None
                if size == 0
                else "Recycle bin is non-empty — restore or clear it from the AI config panel.",
                hint_key=None if size == 0 else "diag.v2.item.trash_size.warn.hint",
            )
        )
    except Exception:
        items.append(
            item(
                "trash_size",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
            )
        )
    return {"label_key": "diag.v2.group.ai_config", "items": items}


def _group_chat(mgr) -> dict:
    items = []
    if mgr is None:
        items.append(
            item(
                "chat_sessions",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
                hint="This data isn't available in the current state.",
                hint_key="diag.v2.item.unavailable.hint",
            )
        )
    else:
        try:
            count = len(mgr.get_sessions())
            items.append(
                item(
                    "chat_sessions",
                    "ok",
                    f"{count} active session(s)",
                    detail_key="diag.v2.item.chat_sessions.detail",
                    detail_params={"count": count},
                )
            )
        except Exception:
            items.append(
                item(
                    "chat_sessions",
                    "warn",
                    "Unavailable",
                    detail_key="diag.v2.item.unavailable.detail",
                )
            )
    return {"label_key": "diag.v2.group.chat", "items": items}


def _group_transfer(mgr) -> dict:
    items = []
    if mgr is None:
        for entry_id in ("active_transfers", "transfer_failures"):
            items.append(
                item(
                    entry_id,
                    "warn",
                    "Unavailable",
                    detail_key="diag.v2.item.unavailable.detail",
                    hint="This data isn't available in the current state.",
                    hint_key="diag.v2.item.unavailable.hint",
                )
            )
        return {"label_key": "diag.v2.group.transfer", "items": items}
    try:
        active = sum(
            1
            for t in mgr.get_transfers()
            if t.get("status") not in ("completed", "cancelled", "failed")
        )
        items.append(
            item(
                "active_transfers",
                "ok",
                f"{active} in progress",
                detail_key="diag.v2.item.active_transfers.detail",
                detail_params={"count": active},
            )
        )
    except Exception:
        items.append(
            item(
                "active_transfers",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
            )
        )
    try:
        history = mgr.get_history()
        failures = sum(1 for t in history if not t.get("success"))
        if failures:
            items.append(
                item(
                    "transfer_failures",
                    "warn",
                    f"{failures} failed transfer(s)",
                    detail_key="diag.v2.item.transfer_failures.warn.detail",
                    detail_params={"count": failures},
                    hint="Check the Transfers panel and retry any failed transfers.",
                    hint_key="diag.v2.item.transfer_failures.warn.hint",
                )
            )
        else:
            items.append(
                item(
                    "transfer_failures",
                    "ok",
                    "No recent failures",
                    detail_key="diag.v2.item.transfer_failures.ok.detail",
                )
            )
    except Exception:
        items.append(
            item(
                "transfer_failures",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
            )
        )
    return {"label_key": "diag.v2.group.transfer", "items": items}


def _group_filesystem(cfg, history) -> dict:
    items = []
    try:
        db_path = getattr(history, "_db_path", None) if history is not None else None
        if db_path and Path(db_path).exists():
            size = Path(db_path).stat().st_size
            pretty = fmt_bytes(size)
            items.append(
                item(
                    "history_db_size",
                    "ok",
                    pretty,
                    detail_key="diag.v2.item.history_db_size.detail",
                    detail_params={"size": pretty},
                )
            )
        else:
            items.append(
                item(
                    "history_db_size",
                    "warn",
                    "Missing or unreadable",
                    detail_key="diag.v2.item.history_db_size.warn.detail",
                    hint="History database is missing or unreadable — history may be lost.",
                    hint_key="diag.v2.item.history_db_size.warn.hint",
                )
            )
    except Exception:
        items.append(
            item(
                "history_db_size",
                "warn",
                "Unavailable",
                detail_key="diag.v2.item.unavailable.detail",
            )
        )
    try:
        usage = shutil.disk_usage(str(effective_data_dir(cfg)))
        free = int(getattr(usage, "free", 0) or 0)
        low = free < DISK_LOW_BYTES
        pretty = fmt_bytes(free)
        items.append(
            item(
                "disk_free",
                "warn" if low else "ok",
                f"{pretty} free" + (" (low)" if low else ""),
                detail_key=(
                    "diag.v2.item.disk_free.warn.detail"
                    if low
                    else "diag.v2.item.disk_free.ok.detail"
                ),
                detail_params={"free": pretty},
                hint="Low disk space — history and transfers may fail." if low else None,
                hint_key="diag.v2.item.disk_free.warn.hint" if low else None,
            )
        )
    except Exception:
        items.append(
            item(
                "disk_free", "warn", "Unavailable", detail_key="diag.v2.item.unavailable.detail"
            )
        )
    return {"label_key": "diag.v2.group.filesystem", "items": items}


def build_groups(cfg, ctx, *, chat=None, ai_config=None, file_transfer=None, history=None) -> dict:
    """The grouped (round 19) diagnostics payload."""
    return {
        "system": _group_system(cfg, ctx.get("start_time", 0)),
        "network": _group_network(cfg, ctx),
        "internet": _group_internet(
            cfg, ctx.get("relay_state", "off"), ctx.get("pending_count", -1)
        ),
        "ai_config": _group_ai_config(cfg, ai_config),
        "chat": _group_chat(chat),
        "transfer": _group_transfer(file_transfer),
        "filesystem": _group_filesystem(cfg, history),
    }


def build_report(
    *,
    cfg,
    lan_ip="",
    web=None,
    web_running=False,
    transport=None,
    discovery=None,
    pairing=None,
    chat=None,
    ai_config=None,
    file_transfer=None,
    history=None,
    start_time=0.0,
    relay_state="off",
    delivery_counts=None,
) -> dict:
    """Build the full diagnostics payload from live (or absent) managers.

    ``relay_state`` is a string or a zero-argument callable returning one;
    ``delivery_counts`` is an optional callable returning ``{"peers": {id: n}}``.
    Every access is defensive — a bare/degraded host still yields a complete,
    renderable report.
    """
    port = int(getattr(cfg, "port", 0) or 0)
    web_port = int(getattr(cfg, "web_port", 0) or 0)

    server_running = bool(transport is not None and getattr(transport, "_running", False))
    discovery_running = bool(discovery is not None and getattr(discovery, "is_browsing", False))
    advertising = bool(discovery is not None and getattr(discovery, "is_advertising", False))
    web_running = bool(web_running)

    checks, firewall = build_checks(
        cfg, server_running, discovery_running, advertising, web_running, lan_ip, web
    )

    try:
        connected = transport.get_connected_peers() if transport is not None else []
    except Exception:
        connected = []
    try:
        paired = pairing.get_paired_peers() if pairing is not None else []
    except Exception:
        paired = []

    try:
        state = relay_state() if callable(relay_state) else relay_state
    except Exception:
        state = "off"
    if state not in ("off", "connecting", "online", "error"):
        state = "off"
    try:
        counts = delivery_counts() if callable(delivery_counts) else delivery_counts
        pending = sum(counts.get("peers", {}).values()) if isinstance(counts, dict) else -1
    except Exception:
        pending = -1

    ctx = {
        "port": port,
        "web_port": web_port,
        "server_running": server_running,
        "discovery_running": discovery_running,
        "advertising": advertising,
        "web_running": web_running,
        "lan_ip": lan_ip,
        "start_time": start_time,
        "relay_state": state,
        "pending_count": pending,
        "fw_ok": firewall["ok"],
        "fw_detail": firewall["detail"],
        "fw_detail_key": firewall["detail_key"],
        "fw_detail_params": firewall["detail_params"],
        "fw_guidance": firewall["guidance"],
        "fw_guidance_key": firewall["guidance_key"],
        "fw_guidance_params": firewall["guidance_params"],
    }

    return {
        "v2": True,
        "summary": summarize(checks),
        "checks": checks,
        "groups": build_groups(
            cfg, ctx, chat=chat, ai_config=ai_config,
            file_transfer=file_transfer, history=history,
        ),
        "discovery_running": discovery_running,
        "server_running": server_running,
        "connected_count": len(connected),
        "paired_count": len(paired),
        "web_companion_running": web_running,
        "web_port": web_port,
        "lan_ip": lan_ip,
        "os": friendly_platform_name(platform.system()),
        "version": __version__,
    }
