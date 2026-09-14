"""Devices page — the backend facts the web panel is built on.

The rest of this file used to be static wiring assertions over the shipped
JavaScript (markup, CSS, class names, locale keys); those are gone.  What
remains is what the page actually reads: `get_devices`'s payload — a
forgotten device still broadcasting on the LAN stays in the Removed archive
and is not re-surfaced under Discovered, and the local device's `os` field is
already a friendly brand name — plus the cross-OS mapping that produces it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Peer:
    def __init__(self, device_id, name, paired=True):
        self.device_id = device_id
        self.device_name = name
        self.paired = paired
        self.os = ""
        self.notes = ""


class _DevCfg:
    device_id = "self1"
    device_name = "Self"

    def __init__(self, peers=None):
        self.peers = peers or {}
        self.removed_peers = {}


def _removed_peer(device_id, name, removed_at=100.0):
    p = _Peer(device_id, name)
    p.removed_at = removed_at
    p.last_ip = None
    p.last_port = None
    return p


def test_discovered_skips_removed_archive():
    from internal.web.api.devices import get_devices

    cfg = _DevCfg()
    cfg.removed_peers = {"gone": _removed_peer("gone", "Forgotten")}

    # The forgotten device is still broadcasting on the LAN — it must NOT be
    # listed under "Discovered" (that would double-list it against the Removed
    # archive), only in the archive for Restore/Purge.
    data, status = get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {"gone": {"name": "Forgotten"}},
    )
    assert status == 200
    ids = [d["device_id"] for d in data["devices"]]
    assert "gone" not in ids
    assert any(r["device_id"] == "gone" for r in data["removed"])


def test_local_device_os_is_friendly_name():
    from internal.web.api.devices import get_devices

    cfg = _DevCfg()
    data, status = get_devices(cfg, lambda: [])
    assert status == 200
    local = data["devices"][0]
    # platform.system() ("Darwin") is mapped to the brand ("macOS") so the
    # backend value is already display-ready for the card icon + label.
    assert local["os"] in ("macOS", "Windows", "Linux", "Android", "iOS")
    assert local["os"] != "Darwin"


def test_friendly_platform_name_maps_known_systems():
    """The mapping the payload above depends on — and the only place the
    macOS / Linux / iOS brands are pinned on a Windows-only machine."""
    from internal.platform import friendly_platform_name

    assert friendly_platform_name("Darwin") == "macOS"
    assert friendly_platform_name("darwin") == "macOS"
    assert friendly_platform_name("Mac OS X") == "macOS"
    assert friendly_platform_name("Windows") == "Windows"
    assert friendly_platform_name("Linux") == "Linux"
    assert friendly_platform_name("Android") == "Android"
    assert friendly_platform_name("iOS") == "iOS"
    # Unknown systems fall through to the raw kernel name.
    assert friendly_platform_name("FreeBSD") == "freebsd"
