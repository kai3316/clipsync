"""Read model behind the web Companion's overview page."""

from __future__ import annotations

import datetime
import logging
import platform
import time

from internal.application.use_cases.history import row_dto
from internal.config.config import known_device_names
from internal.platform import friendly_platform_name
from internal.platform.network import detect_network_type
from internal.version import __version__

logger = logging.getLogger(__name__)

# Content-type labels the history rows carry for an image entry.  The web
# overview's "images" counter matches the desktop dashboard's.
IMAGE_TYPES = ("IMAGE", "IMAGE_PNG", "IMAGE_EMF", "PICTURE")


def _internet_paired_peers(runtime) -> list[dict]:
    """The confirmed code pairings, as the internet pairing service lists them.

    Same source the device list draws its relay-only rows from (see
    ``LanRuntime._refresh``), so the counters and the rows beside them cannot
    disagree about how many devices hold a pairing.

    Degrades to an empty list: a runtime built before the service exists, a
    locked install, or a mobile/CLI runtime has no internet pairing, and the
    overview still has to render.  Nothing here raises past this function.
    """
    service = getattr(runtime, "internet_pairing", None)
    if service is None:
        return []
    try:
        return list(service.paired_peers() or [])
    except Exception:
        logger.debug("Internet pairing peers for the overview failed", exc_info=True)
        return []


def _count_connected_sync_peers(
    runtime, cfg=None, internet_peers: list[dict] | None = None
) -> tuple[int, list[str]]:
    """Connected peers that are paired — an active *sync session*, not a socket.

    A chat-only or mid-pairing connection is live but not a trusted sync peer,
    so counting it would disagree with the frontend's paired-only math.

    ``internet_peers`` is the confirmed code-pairing list (``paired_peers()``);
    it is a parameter so a caller that already read it does not read it twice.
    A device reachable over the relay counts as connected the same way an
    internet pairing row already renders ``connection_state: online`` — one
    pairing, whichever route is up, and one row per device.

    Each name is the one **this machine** calls that device — the user's own,
    when they gave it one — resolved through the same map the history rows use.
    The transport's own ``device_name`` is only the fallback: it is what the peer
    says about itself (or the label this machine happened to dial with), so
    reading it alone put the peer's name on the overview's chips while the device
    list beside them showed the name the user had typed.
    """
    names_by_id = known_device_names(cfg)
    try:
        paired_ids = {
            getattr(peer, "device_id", "") for peer in runtime.pairing.get_paired_peers()
        }
    except Exception:
        paired_ids = set()
    internet = _internet_paired_peers(runtime) if internet_peers is None else internet_peers
    try:
        connected = list(runtime.transport.get_connected_peers() or [])
    except Exception:
        connected = []
    # The LAN session count: a connected peer whose certificate is pinned.
    lan_connected = {pid for pid in connected if pid in paired_ids}
    counted = set(lan_connected)
    names: list[str] = []
    try:
        names = [
            names_by_id.get(pid) or name
            for pid, name in runtime.transport.get_connected_peers_with_names()
            if pid in paired_ids
        ]
    except Exception:
        logger.debug("Connected peer names unavailable", exc_info=True)
    # ...plus the code-paired devices the relay currently reaches.  One device
    # with both routes is still one device: its id is already counted when its
    # LAN session is up, and the relay view of the same id must not add a second.
    for peer in internet:
        pid = str(peer.get("peer_id") or "")
        if not pid or pid in counted or not peer.get("online"):
            continue
        counted.add(pid)
        names.append(names_by_id.get(pid) or peer.get("name") or pid[:12])
    return len(counted), names


def _history_rows(history) -> list[dict]:
    if history is None:
        return []
    try:
        return history.get_all()
    except Exception:
        logger.debug("History snapshot for the overview failed", exc_info=True)
        return []


def _is_today(timestamp) -> bool:
    try:
        return datetime.datetime.fromtimestamp(float(timestamp)).date() == datetime.date.today()
    except Exception:
        return False


def build_overview(cfg, history, runtime, start_time, lan_ip="", source_label=None) -> dict:
    """Aggregate the dashboard counters the web overview panel renders.

    Mirrors the legacy ``Application._get_overview_data``.  Every group
    degrades to a zero instead of failing the page: a locked or partially
    started application still has to render its overview.
    """
    internet_peers = _internet_paired_peers(runtime)
    connected_count, connected_names = _count_connected_sync_peers(
        runtime, cfg, internet_peers=internet_peers
    )
    try:
        paired_ids = {
            getattr(peer, "device_id", "") for peer in runtime.pairing.get_paired_peers()
        }
    except Exception:
        paired_ids = set()
    # A pairing is a device, not a route: the same device may hold a pinned LAN
    # certificate *and* a code pairing, and the device list draws one row for it,
    # so the count is the union of the two sources rather than their sum.
    paired_ids |= {str(peer.get("peer_id") or "") for peer in internet_peers}
    paired_ids.discard("")
    paired_count = len(paired_ids)

    active_transfers = 0
    transfer_completed = 0
    try:
        active_transfers = sum(
            1
            for item in runtime.file_transfer.get_transfers() or []
            if item.get("status") not in ("completed", "cancelled", "failed")
        )
        transfer_completed = sum(
            1 for item in runtime.file_transfer.get_history() or [] if item.get("success")
        )
    except Exception:
        logger.debug("Transfer counters for the overview failed", exc_info=True)

    rows = _history_rows(history)
    discovered_count = 0
    discovering = visible = False
    try:
        discovered_count = len(runtime.discovered_peers())
    except Exception:
        logger.debug("Discovery count for the overview failed", exc_info=True)
    try:
        discovery = runtime.discovery_state()
        discovering = bool(discovery.get("enabled"))
        visible = bool(discovery.get("visible"))
    except Exception:
        logger.debug("Discovery state for the overview failed", exc_info=True)

    network_type, network_detail = detect_network_type()
    return {
        "connected_count": connected_count,
        "paired_count": paired_count,
        "discovered_count": discovered_count,
        "connected_names": connected_names,
        "history_count": len(rows),
        "history_today": sum(1 for row in rows if _is_today(row.get("timestamp", 0))),
        "history_pinned": sum(1 for row in rows if row.get("pinned")),
        "history_images": sum(
            1 for row in rows if str(row.get("content_type", "")).upper() in IMAGE_TYPES
        ),
        "active_transfers": active_transfers,
        "transfer_completed": transfer_completed,
        "discovering": discovering,
        "visible": visible,
        "sync_enabled": bool(getattr(cfg, "sync_enabled", True)),
        "web_enabled": bool(getattr(cfg, "web_enabled", True)),
        "uptime_seconds": max(0, int(time.time()) - int(start_time or time.time())),
        "local_ip": lan_ip,
        "port": getattr(cfg, "web_port", 0),
        "platform": friendly_platform_name(platform.system()),
        "version": __version__,
        # This device's own name.  The dashboard and the phone both read it from
        # here rather than from the page they were served, which is the copy
        # that a rename on another surface leaves behind — the name is a fact
        # about the device, and this payload is the only one they re-read.
        "device_name": getattr(cfg, "device_name", ""),
        "network_type": network_type,
        "network_detail": network_detail,
        # The feed is the newest six history rows, built by the same helper the
        # history page's list uses — so a row here is a row there, and every
        # action a window can take on one it can take on the other.  The preview
        # is capped at what the feed's single line shows, which is also what this
        # projection used to ship; the id is what an action needs, and the full
        # clip is read back by it.
        "recent_items": [
            row_dto(row, source_label, preview_limit=80) for row in rows[:6]
        ],
    }
